"""What changed across a pull request, cut to fit a prompt, most relevant first.

A reply on a review thread — "fixed", "corrigido", "it's in store.ts now" —
used to be checked against the commented file alone: the diff since the
finding was filtered down to that one path. A fix that lived in a helper, a
caller, another package or a re-export was invisible, and 37 of 123 thread
replies Mira posted in two days said some form of "not visible, please show
me the code" about a change that had in fact been pushed.

This module renders a diff for a prompt without that filter. Every changed
file is listed with its line counts, so nothing is silently missing; the
hunks follow in priority order — the files the caller is about first, then
the files the conversation names (by path or by a symbol whose changed lines
it mentions), then the rest — until the budget runs out. A file that did not
fit says so, and says it can be read with the tools.
"""

from __future__ import annotations

import posixpath
import re
from collections.abc import Iterable
from dataclasses import dataclass

from mira.core.diff_parser import parse_diff
from mira.models import FileDiff

# A file that would only get this little of the remaining budget is listed
# rather than shown as a stub.
_MIN_PARTIAL_CHARS = 600
_MAX_IDENTIFIERS = 40

_BACKTICK_RE = re.compile(r"`([^`\n]{1,200})`")
_WORD_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]{2,}")
# Code-shaped words outside backticks: snake_case, camelCase, PascalCase with
# an inner capital, SCREAMING_CASE. Plain prose words do not match.
_CODEISH_RE = re.compile(
    r"\b(?:[A-Za-z][A-Za-z0-9]*_[A-Za-z0-9_]+|[a-z]+[A-Z][A-Za-z0-9]*|[A-Z][a-z0-9]+[A-Z][A-Za-z0-9]*)\b"
)
_PATHISH_RE = re.compile(
    r"(?<![\w/.-])([\w.-]+(?:/[\w.-]+)*\.[A-Za-z0-9]{1,8}|[\w.-]+(?:/[\w.-]+)+)"
)


@dataclass(frozen=True)
class Mentions:
    """The paths and code identifiers a piece of conversation names."""

    paths: frozenset[str]
    identifiers: frozenset[str]


def mentions_in(*texts: str) -> Mentions:
    """Paths and identifiers named in ``texts`` (replies, a finding, history)."""
    paths: set[str] = set()
    identifiers: list[str] = []

    def _add_identifier(word: str) -> None:
        if word not in identifiers and len(identifiers) < _MAX_IDENTIFIERS:
            identifiers.append(word)

    for text in texts:
        if not text:
            continue
        for span in _BACKTICK_RE.findall(text):
            paths.update(match.removeprefix("./") for match in _PATHISH_RE.findall(span))
            for word in _WORD_RE.findall(span):
                _add_identifier(word)
        for word in _CODEISH_RE.findall(text):
            _add_identifier(word)
        paths.update(match.removeprefix("./") for match in _PATHISH_RE.findall(text))
    return Mentions(frozenset(paths), frozenset(identifiers))


def _names_file(file: FileDiff, paths: Iterable[str]) -> bool:
    candidates = {file.path, *([file.old_path] if file.old_path else [])}
    for mentioned in paths:
        for candidate in candidates:
            if candidate == mentioned or candidate.endswith("/" + mentioned):
                return True
            # A bare filename ("store.ts") names the file of that name.
            if "/" not in mentioned and posixpath.basename(candidate) == mentioned:
                return True
    return False


def _changed_lines(file: FileDiff) -> str:
    return "\n".join(
        line
        for hunk in file.hunks
        for line in hunk.content.splitlines()
        if line[:1] in {"+", "-"} and not line.startswith(("+++", "---"))
    )


def _touches(file: FileDiff, identifiers: frozenset[str]) -> bool:
    if not identifiers:
        return False
    pattern = re.compile(
        r"\b(?:"
        + "|".join(re.escape(w) for w in sorted(identifiers, key=len, reverse=True))
        + r")\b"
    )
    return bool(pattern.search(_changed_lines(file)))


def _rank(file: FileDiff, focus: set[str], mentions: Mentions) -> int:
    if file.path in focus or (file.old_path and file.old_path in focus):
        return 0
    if _names_file(file, mentions.paths):
        return 1
    if _touches(file, mentions.identifiers):
        return 2
    return 3


def render_changes(
    diff_text: str,
    *,
    focus_paths: Iterable[str] = (),
    mentions: Mentions | None = None,
    budget: int = 16_000,
    exclude_paths: Iterable[str] = (),
) -> str:
    """``diff_text`` for a prompt: a list of every changed file, then hunks by priority.

    ``focus_paths`` come first (the commented file); then files ``mentions``
    names; then the rest, in the diff's order. ``budget`` bounds the hunks, not
    the file list, which is one line per file. ``exclude_paths`` are listed
    but never shown — their content is in the prompt already. "" when the
    diff has no files. Raises ``DiffParseError`` on a diff that does not parse.
    """
    files = list(parse_diff(diff_text).files)
    if not files:
        return ""
    focus = set(focus_paths)
    excluded = set(exclude_paths)
    found = mentions or Mentions(frozenset(), frozenset())
    order = sorted(range(len(files)), key=lambda i: (_rank(files[i], focus, found), i))

    shown: dict[str, str] = {}
    status: dict[str, str] = {}
    left = budget
    for index in order:
        file = files[index]
        if file.path in excluded:
            status[file.path] = "shown above"
            continue
        if file.is_binary or not file.hunks:
            status[file.path] = "binary" if file.is_binary else "no textual change"
            continue
        text = "\n".join(h.content.rstrip("\n") for h in file.hunks)
        if len(text) <= left:
            shown[file.path] = text
            status[file.path] = "shown"
            left -= len(text)
        elif left >= _MIN_PARTIAL_CHARS or file.path in focus:
            cut = text[: max(left, 0)]
            cut = cut[: cut.rfind("\n")] if "\n" in cut else cut
            shown[file.path] = cut + "\n… (cut here; read the rest with read_file)"
            status[file.path] = "shown in part"
            left = 0
        else:
            status[file.path] = "not shown — read it with read_file"

    listing = "\n".join(
        f"- {f.path}"
        + (f" (renamed from {f.old_path})" if f.old_path else "")
        + f" (+{f.added_lines} -{f.deleted_lines}) [{status.get(f.path, '')}]"
        for f in (files[i] for i in order)
    )
    blocks = [f"### {path}\n{text}" for path, text in shown.items()]
    header = f"{len(files)} file(s) changed:\n{listing}"
    return header + ("\n\n" + "\n\n".join(blocks) if blocks else "")
