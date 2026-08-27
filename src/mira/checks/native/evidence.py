"""Turning a parsed diff into the line numbers a check can point at.

Every native check has the same problem: it recognises something in the diff
and then has to say *where*. A finding that says "this pull request removes a
public function" is an assertion; one that says
``src/mira/core/engine.py:412: -def review_diff(...)`` is checkable. These
helpers exist so that every check produces the second kind, and so that they
all count lines the same way.

Line numbers come from the hunk headers, walking the hunk body and advancing
the counter on context and added lines. A deleted line has no line number in
the new file, so it is reported against the position it was removed from —
which is where a reader looking at the diff will find it.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from dataclasses import dataclass

from mira.models import FileDiff, PatchSet

# Diff bodies are quoted verbatim into evidence, and evidence is rendered into
# a markdown comment. Long enough to be useful, short enough that a minified
# line cannot flood a pull request.
MAX_SNIPPET = 400


@dataclass(frozen=True)
class DiffLine:
    """One line of a hunk, with the number a reader can navigate to."""

    path: str
    # "+" added, "-" removed, " " context.
    kind: str
    text: str
    # Line number in the new file. For a removed line, the line it was removed
    # from — the position the diff shows it at.
    line: int

    @property
    def added(self) -> bool:
        return self.kind == "+"

    @property
    def removed(self) -> bool:
        return self.kind == "-"


def iter_lines(file_diff: FileDiff) -> Iterator[DiffLine]:
    """Every line of every hunk, numbered.

    The hunk header is skipped without advancing the counter: ``target_start``
    already points at the first body line, so counting the ``@@`` line would
    put every subsequent number one out. The review-time OSV scan learned this
    the same way, which is why both walkers skip it explicitly rather than
    relying on the header never appearing.
    """
    for hunk in file_diff.hunks:
        line_no = hunk.target_start
        for raw in hunk.content.splitlines():
            if raw.startswith("@@"):
                continue
            if raw.startswith("+"):
                yield DiffLine(file_diff.path, "+", raw[1:], line_no)
                line_no += 1
            elif raw.startswith("-"):
                yield DiffLine(file_diff.path, "-", raw[1:], line_no)
            else:
                yield DiffLine(file_diff.path, " ", raw[1:] if raw else "", line_no)
                line_no += 1


def iter_all(patch_set: PatchSet) -> Iterator[DiffLine]:
    for file_diff in patch_set.files:
        if file_diff.is_binary:
            continue
        yield from iter_lines(file_diff)


def snippet(text: str) -> str:
    """One quoted line, bounded and stripped of trailing whitespace."""
    cleaned = (text or "").rstrip()
    if len(cleaned) <= MAX_SNIPPET:
        return cleaned
    return cleaned[:MAX_SNIPPET] + " …"


def find_in_body(body: str, pattern: re.Pattern[str]) -> tuple[int, str]:
    """``(line number, line)`` of the first match in a body, or ``(0, "")``.

    Used for the pull request's own description, where "line 4 of the body" is
    the only locator there is.
    """
    for index, line in enumerate((body or "").splitlines(), start=1):
        if pattern.search(line):
            return index, line
    return 0, ""
