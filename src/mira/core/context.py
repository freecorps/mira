"""Hunk merging and context string building."""

from __future__ import annotations

import re
from dataclasses import replace

from mira.models import FileDiff, HunkInfo


def expand_context(files: list[FileDiff], context_lines: int = 3) -> list[FileDiff]:
    """Merge adjacent/overlapping hunks in each file.

    Hunks whose expanded ranges (with context_lines padding) overlap
    are merged into a single hunk.
    """
    result: list[FileDiff] = []
    for f in files:
        if len(f.hunks) <= 1:
            result.append(f)
            continue

        sorted_hunks = sorted(f.hunks, key=lambda h: h.target_start)
        merged: list[HunkInfo] = [sorted_hunks[0]]

        for hunk in sorted_hunks[1:]:
            prev = merged[-1]
            prev_end = prev.target_start + prev.target_length + context_lines
            hunk_start = hunk.target_start - context_lines

            if hunk_start <= prev_end:
                new_end = max(
                    prev.target_start + prev.target_length,
                    hunk.target_start + hunk.target_length,
                )
                merged[-1] = HunkInfo(
                    source_start=prev.source_start,
                    source_length=prev.source_length + hunk.source_length,
                    target_start=prev.target_start,
                    target_length=new_end - prev.target_start,
                    content=prev.content + "\n" + hunk.content,
                )
            else:
                merged.append(hunk)

        result.append(replace(f, hunks=merged))

    return result


def extract_hunk_lines(file_diff: FileDiff) -> str:
    """Return the raw content of all hunks for a file as a single string.

    Used for validating that LLM-quoted ``existing_code`` actually appears in the diff.
    Strips diff markers (+/-/space prefix) so clean code from the LLM can match.
    """
    lines: list[str] = []
    for hunk in file_diff.hunks:
        for line in hunk.content.splitlines():
            if line.startswith("@@"):
                continue
            if line and line[0] in ("+", "-", " "):
                lines.append(line[1:])
            else:
                lines.append(line)
    return "\n".join(lines)


_HUNK_HEADER_RE = re.compile(r"^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@")

# Width of the line-number gutter. Five digits covers any file a review will
# realistically see; a longer number still renders, only out of alignment.
_GUTTER = 5


def number_hunk_lines(hunk: HunkInfo) -> str:
    """The hunk with each line prefixed by its line number in the new file.

    ``   42 +added``, ``   43  context``, ``      -removed``: removed lines have
    no new-file number, so their gutter is blank. A merged hunk (see
    :func:`expand_context`) carries several ``@@`` headers, and each one
    restarts the count where it says.

    The model files every comment against a new-file line number. Without the
    gutter it has to count from the ``@@`` header itself, and smaller models
    miscount often enough that a real finding lands on the wrong line or,
    past the snapping distance, is dropped as unanchorable.
    """
    new = hunk.target_start
    out: list[str] = []
    blank = " " * _GUTTER
    lines = [line.rstrip("\r") for line in hunk.content.rstrip("\n").split("\n")]
    for index, line in enumerate(lines):
        header = _HUNK_HEADER_RE.match(line)
        if not line and index + 1 < len(lines) and lines[index + 1].startswith("@@"):
            # The seam `expand_context` leaves between two merged hunks, not a
            # line of the file: numbering it would claim a line that is not blank.
            continue
        if header:
            new = int(header.group(2))
            out.append(line)
        elif line.startswith(("-", "\\")):
            out.append(f"{blank} {line}")
        else:
            # Added or context. A context line whose single leading space was
            # lost in transit is still a context line.
            text = line if line.startswith(("+", " ")) else " " + line
            out.append(f"{new:>{_GUTTER}} {text}")
            new += 1
    return "\n".join(out)


_GUTTER_PREFIX_RE = re.compile(r"^\s*\d{1,6} (?=[+\- ])")
# The blank gutter of a removed line, then its marker: exactly the gutter width
# plus its separating space, so ordinary indented code is left alone.
_BLANK_GUTTER_RE = re.compile(rf"^ {{{_GUTTER + 1}}}(?=-)")


def strip_line_gutter(text: str) -> str:
    """Undo :func:`number_hunk_lines` on text a model copied out of the prompt.

    Takes off, per line, a leading line number and then one diff marker, so
    ``existing_code`` quoted with its gutter (``   42 +x = 1``) compares equal
    to the code it quotes (``x = 1``). A line with no gutter loses only a
    leading ``+``/``-`` marker (``++i`` and ``--x`` are left alone).

    Only for a quote that failed to match as written: a line of code can
    start with a number or a minus sign, and stripping those from a quote
    that was already right would break it.
    """
    lines: list[str] = []
    for line in text.splitlines():
        gutter = _GUTTER_PREFIX_RE.match(line)
        if gutter:
            rest = line[gutter.end() :]
            lines.append(rest[1:])
        elif line[:1] in ("+", "-") and line[1:2] != line[:1]:
            lines.append(line[1:])
        elif blank := _BLANK_GUTTER_RE.match(line):
            # A removed line's gutter is blank: `      -    code`.
            lines.append(line[blank.end() + 1 :])
        else:
            lines.append(line)
    return "\n".join(lines)


def build_file_context_string(file_diff: FileDiff, *, numbered: bool = True) -> str:
    """Format a file diff as a markdown string for the LLM prompt.

    ``numbered`` puts the new-file line number in front of every line (see
    :func:`number_hunk_lines`); the prompt tells the model what the gutter is.
    """
    parts: list[str] = []
    lang = file_diff.language or ""

    parts.append(f"### `{file_diff.path}` ({file_diff.change_type.value})")
    if file_diff.old_path:
        parts.append(f"Renamed from `{file_diff.old_path}`")
    parts.append(f"+{file_diff.added_lines} / -{file_diff.deleted_lines} lines\n")

    for hunk in file_diff.hunks:
        parts.append(f"```{lang}")
        parts.append(number_hunk_lines(hunk) if numbered else hunk.content.rstrip())
        parts.append("```\n")

    return "\n".join(parts)
