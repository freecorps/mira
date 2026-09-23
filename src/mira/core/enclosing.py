"""The whole function around each changed line, for the chunk that reviews it.

A diff hunk carries three lines of context either side of a change. That is
enough for a reader who can open the file, and not for a model that cannot:
whether the new branch handles the value the function was called with, or
whether the early return skips the cleanup ten lines down, is decided by code
the hunk leaves out. Larger models guess well or reach for a tool; smaller
ones file what the three lines suggest, or file nothing.

This builds, per review chunk, the post-change source of every function or
class that contains a changed line — numbered the way the diff is — so the
reviewer reads each change inside the code it runs in. Only the chunk's own
files are included, which is also why this is per chunk and not per review:
a prompt carrying the functions of files another chunk reviews spends its
context on code it may not comment on.
"""

from __future__ import annotations

import asyncio
import logging
import re

from mira.index.context import SourceFetcher
from mira.index.extract import SymbolSpan, extract_symbols
from mira.models import FileChangeType, FileDiff

logger = logging.getLogger(__name__)

_HUNK_HEADER_RE = re.compile(r"^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@")

# A symbol longer than this is shown as windows around its changed lines,
# not whole: a 900-line class is not context, it is a second diff.
_MAX_SPAN_LINES = 160
_WINDOW = 30
_COMMENT_MARKS = ("#", "//", "/*", "*", "--", '"""', "'''")


def _changed_and_shown_lines(file: FileDiff) -> tuple[set[int], set[int]]:
    """New-file lines the diff changes, and every new-file line it already shows.

    A removal changes no new-file line, so it is counted at the line that
    follows it — the one that now sits where the removed code was.
    """
    changed: set[int] = set()
    shown: set[int] = set()
    for hunk in file.hunks:
        new = hunk.target_start
        for line in hunk.content.rstrip("\n").split("\n"):
            header = _HUNK_HEADER_RE.match(line)
            if header:
                new = int(header.group(2))
                continue
            if line.startswith("\\"):
                continue
            if line.startswith("-"):
                changed.add(max(new, 1))
                continue
            if line.startswith("+"):
                changed.add(new)
            shown.add(new)
            new += 1
    return changed, shown


def _lines(source: str) -> list[str]:
    """Lines as the diff numbers them: split on newlines only, not on the form
    feeds and Unicode separators ``str.splitlines`` also breaks at."""
    return [line.rstrip("\r") for line in source.split("\n")]


def _innermost(symbols: list[SymbolSpan], line: int) -> SymbolSpan | None:
    holding = [s for s in symbols if s.start_line <= line <= s.end_line]
    return min(holding, key=lambda s: s.end_line - s.start_line) if holding else None


def _nested(
    lines: list[str], symbol: SymbolSpan, line: int, language: str, depth: int = 0
) -> SymbolSpan:
    """The smallest symbol nested inside ``symbol`` that holds ``line``.

    The extractor reports top-level symbols (and Python methods), so a route
    handler defined inside a router factory, or a callback inside a React
    component, comes back as the whole enclosing function. Re-reading the
    body finds the nested definition; its line numbers are shifted back onto
    the file's.
    """
    if depth >= 3 or symbol.end_line - symbol.start_line + 1 <= _MAX_SPAN_LINES:
        return symbol
    body = "\n".join(lines[symbol.start_line : symbol.end_line])
    try:
        inner = extract_symbols(body, language)
    except Exception:  # noqa: BLE001
        return symbol
    offset = symbol.start_line
    for candidate in inner:
        candidate.start_line += offset
        candidate.end_line += offset
    found = _innermost(inner, line)
    if found is None or found.end_line - found.start_line >= symbol.end_line - symbol.start_line:
        return symbol
    return _nested(lines, found, line, language, depth + 1)


def _ranges_for(file: FileDiff, source: str) -> list[tuple[int, int, str]]:
    """``(first, last, label)`` line ranges of the file worth showing whole."""
    changed, shown = _changed_and_shown_lines(file)
    if not changed:
        return []
    try:
        symbols = extract_symbols(source, file.language or "")
    except Exception as exc:  # noqa: BLE001 — a heuristic parser, never a reason to fail
        logger.debug("Symbol extraction failed for %s: %s", file.path, exc)
        return []
    lines = _lines(source)
    total = len(lines)
    wanted: dict[tuple[int, int], str] = {}
    for line in sorted(changed):
        text = lines[line - 1].strip() if 0 < line <= total else ""
        if 0 < line <= total and (not text or text.startswith(_COMMENT_MARKS)):
            # A blank or comment line added between two definitions belongs to
            # neither, and mapping it to the enclosing class shows the class.
            continue
        symbol = _innermost(symbols, line)
        if symbol is None:
            continue
        symbol = _nested(lines, symbol, line, file.language or "")
        first, last = symbol.start_line, min(symbol.end_line, total)
        label = symbol.qualified_name or symbol.name
        if last - first + 1 > _MAX_SPAN_LINES:
            first = max(symbol.start_line, line - _WINDOW)
            last = min(last, line + _WINDOW)
            label = f"{label} (excerpt)"
        # A range the diff already shows almost entirely — a function the PR
        # adds, say — adds nothing but tokens.
        if len(set(range(first, last + 1)) - shown) > max(3, (last - first + 1) // 10):
            wanted.setdefault((first, last), label)

    merged: list[tuple[int, int, str]] = []
    for (first, last), label in sorted(wanted.items()):
        if merged and first <= merged[-1][1] + 1:
            prev_first, prev_last, prev_label = merged[-1]
            merged[-1] = (prev_first, max(prev_last, last), prev_label)
        else:
            merged.append((first, last, label))
    return merged


def _render(path: str, lang: str, lines: list[str], first: int, last: int, label: str) -> str:
    body = "\n".join(f"{n:>5}  {lines[n - 1]}" for n in range(first, last + 1))
    return f"#### `{path}` — `{label}` (lines {first}-{last})\n```{lang}\n{body}\n```\n"


async def build_enclosing_context(
    files: list[FileDiff],
    source_fetcher: SourceFetcher | None,
    char_budget: int,
) -> str:
    """The changed functions of ``files`` as they read after the change, or "".

    Files are taken in chunk order and their functions in file order, until
    ``char_budget`` is spent. Added files are skipped — their diff already is
    the whole file — and so are deleted ones, which have no after.
    """
    if source_fetcher is None or char_budget <= 0:
        return ""
    candidates = [
        f
        for f in files
        if f.change_type in (FileChangeType.MODIFIED, FileChangeType.RENAMED)
        and not f.is_binary
        and f.hunks
    ]
    if not candidates:
        return ""
    sources = await asyncio.gather(
        *(source_fetcher.fetch(f.path) for f in candidates), return_exceptions=True
    )

    blocks: list[str] = []
    used = 0
    for file, source in zip(candidates, sources, strict=True):
        if not isinstance(source, str) or not source:
            continue
        lines = _lines(source)
        lang = file.language or ""
        for first, last, label in _ranges_for(file, source):
            block = _render(file.path, lang, lines, first, min(last, len(lines)), label)
            if used + len(block) > char_budget:
                break
            blocks.append(block)
            used += len(block)
        if used >= char_budget:
            break
    if not blocks:
        return ""
    return (
        "\n\n## Changed functions in full (after this PR)\n\n"
        "The complete functions and classes that contain this part's changed "
        "lines, as they read after the change, numbered like the diff. Use them "
        "to judge each change in the code it runs in — the arguments it receives, "
        "the paths that reach it, the cleanup after it. File comments only on "
        "changed lines of the diff below.\n\n" + "\n".join(blocks)
    )
