"""Which lines a diff touched, and whether two line ranges are "the same place".

Both features in this package come down to one comparison: a finding Mira made
at ``path:line`` against a line some later change touched. The two sides are
numbered in different revisions of the file, so an exact comparison would miss
a fix that landed after an unrelated edit moved the code down by two lines.
Matching is therefore by path plus a tolerance, and that tolerance is the one
knob the comparison has.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


@dataclass
class FileLines:
    """Line numbers one diff touched in one file.

    ``added`` are new-side numbers (the file after the change). ``removed`` are
    old-side numbers (the file before it), plus — for a pure insertion — the
    old-side line the insertion sits after, so that "added a missing check"
    still points at a place in the code it fixed.
    """

    path: str
    old_path: str = ""
    added: set[int] = field(default_factory=set)
    removed: set[int] = field(default_factory=set)


def changed_lines(diff_text: str) -> dict[str, FileLines]:
    """Map each file in a unified diff to the lines it touched.

    Returns an empty mapping for a diff that cannot be parsed: a caller using
    this as ground truth would rather have no signal than a wrong one.
    """
    if not (diff_text or "").strip():
        return {}
    try:
        import unidiff

        patch = unidiff.PatchSet(diff_text)
    except Exception as exc:  # noqa: BLE001 - malformed diffs are skipped, not fatal
        logger.debug("Could not parse diff for line extraction: %s", exc)
        return {}

    out: dict[str, FileLines] = {}
    for patched in patch:
        if patched.is_binary_file:
            continue
        path = patched.path
        old_path = patched.source_file or ""
        if old_path.startswith("a/"):
            old_path = old_path[2:]
        entry = out.setdefault(path, FileLines(path=path, old_path=old_path))
        for hunk in patched:
            last_source = max(hunk.source_start - 1, 0)
            pending_insertion = False
            for line in hunk:
                if line.is_removed and line.source_line_no:
                    entry.removed.add(int(line.source_line_no))
                    last_source = int(line.source_line_no)
                    pending_insertion = False
                elif line.is_added and line.target_line_no:
                    entry.added.add(int(line.target_line_no))
                    pending_insertion = True
                elif line.is_context and line.source_line_no:
                    if pending_insertion:
                        # The insertion sat between `last_source` and this
                        # line; both are "the place" it changed.
                        entry.removed.add(max(last_source, 1))
                        entry.removed.add(int(line.source_line_no))
                        pending_insertion = False
                    last_source = int(line.source_line_no)
            if pending_insertion:
                entry.removed.add(max(last_source, 1))
    return out


def to_ranges(lines: set[int] | list[int]) -> list[tuple[int, int]]:
    """Collapse line numbers into inclusive ``(start, end)`` runs."""
    ordered = sorted({int(n) for n in lines if int(n) > 0})
    ranges: list[tuple[int, int]] = []
    for number in ordered:
        if ranges and number == ranges[-1][1] + 1:
            ranges[-1] = (ranges[-1][0], number)
        else:
            ranges.append((number, number))
    return ranges


def ranges_overlap(a_start: int, a_end: int, b_start: int, b_end: int, tolerance: int = 0) -> bool:
    """Whether two inclusive ranges overlap once each is widened by ``tolerance``."""
    a_end = max(a_end, a_start)
    b_end = max(b_end, b_start)
    return a_start - tolerance <= b_end and b_start - tolerance <= a_end


def lines_near(start: int, end: int, lines: set[int], tolerance: int = 0) -> bool:
    """Whether any of ``lines`` falls inside ``start..end`` widened by ``tolerance``."""
    end = max(end, start)
    low, high = start - tolerance, end + tolerance
    return any(low <= line <= high for line in lines)
