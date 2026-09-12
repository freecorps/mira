"""Lossless dependency-aware work allocation for review agents."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from typing import TYPE_CHECKING

from mira.core.context import build_file_context_string
from mira.models import FileDiff, HunkInfo, ReviewChunk

if TYPE_CHECKING:
    from mira.llm.base import LLMProviderProtocol


def _estimate_tokens(text: str) -> int:
    return (len(text) + 3) // 4


def _file_token_estimate(file_diff: FileDiff) -> int:
    return _estimate_tokens(build_file_context_string(file_diff))


def _split_file(file: FileDiff, available: int, count: Callable[[str], int]) -> list[FileDiff]:
    """Preserve every hunk, splitting large hunks with original line coordinates.

    A line larger than the window is sent as labelled fragments at the same
    coordinate. No trailing hunks or line contents are silently dropped.
    """

    def fits(hunks: list[HunkInfo]) -> bool:
        return count(build_file_context_string(replace(file, hunks=hunks))) <= available

    pieces: list[FileDiff] = []
    pending: list[HunkInfo] = []
    for hunk in file.hunks:
        if fits([*pending, hunk]):
            pending.append(hunk)
            continue
        if pending:
            pieces.append(replace(file, hunks=pending))
            pending = []
        if fits([hunk]):
            pending = [hunk]
            continue
        lines = hunk.content.splitlines(keepends=True)
        if lines and lines[0].startswith("@@"):
            lines = lines[1:]
        old_offsets, new_offsets = [0], [0]
        for line in lines:
            old_offsets.append(old_offsets[-1] + int(not line.startswith(("+", "\\"))))
            new_offsets.append(new_offsets[-1] + int(not line.startswith(("-", "\\"))))
        old, new = hunk.source_start, hunk.target_start
        position = 0
        while position < len(lines):
            selected: list[str] = []
            old_len = new_len = 0

            def fragment(
                body: list[str],
                a: int,
                b: int,
                label: str = "",
                old_start: int = old,
                new_start: int = new,
            ) -> HunkInfo:
                header = f"@@ -{old_start},{a} +{new_start},{b} @@{label}\n"
                return HunkInfo(old_start, a, new_start, b, header + "".join(body))

            # Binary search avoids repeatedly tokenizing every growing prefix
            # of a multi-thousand-line hunk.
            lo, hi = position, len(lines)
            while lo < hi:
                mid = (lo + hi + 1) // 2
                a = old_offsets[mid] - old_offsets[position]
                b = new_offsets[mid] - new_offsets[position]
                if fits([fragment(lines[position:mid], a, b)]):
                    lo = mid
                else:
                    hi = mid - 1
            if lo > position:
                selected = lines[position:lo]
                old_len = old_offsets[lo] - old_offsets[position]
                new_len = new_offsets[lo] - new_offsets[position]
                position = lo
            if selected:
                pieces.append(replace(file, hunks=[fragment(selected, old_len, new_len)]))
                old += old_len
                new += new_len
                continue
            line = lines[position]
            marker = line[0] if line[:1] in ("+", "-", " ") else ""
            remaining = line[len(marker) :]
            # Metadata (e.g. "\ No newline at end of file") consumes no
            # source lines, including when the marker itself needs fragments.
            a = int(not line.startswith(("+", "\\")))
            b = int(not line.startswith(("-", "\\")))
            part = 1
            while remaining:
                lo, hi = 0, len(remaining)
                label = f" (long line fragment {part})"
                while lo < hi:
                    mid = (lo + hi + 1) // 2
                    if fits([fragment([marker + remaining[:mid]], a, b, label)]):
                        lo = mid
                    else:
                        hi = mid - 1
                if lo == 0:
                    raise ValueError(f"Review token budget cannot fit a diff line for {file.path}")
                pieces.append(
                    replace(file, hunks=[fragment([marker + remaining[:lo]], a, b, label)])
                )
                remaining = remaining[lo:]
                part += 1
            old += a
            new += b
            position += 1
    if pending or not file.hunks:
        pieces.append(replace(file, hunks=pending))
    return pieces


def chunk_files(
    files: list[FileDiff],
    max_tokens: int,
    provider: LLMProviderProtocol | None = None,
    *,
    dependencies: dict[str, set[str]] | None = None,
    max_files: int | None = None,
) -> list[ReviewChunk]:
    """Keep connected changes with one agent and pack independent small groups.

    Large components share a group_id across sequential parts. Limits bound
    each call, never total coverage. max_files is soft for connected changes.
    """
    if not files:
        return []
    available = max_tokens - 2000
    if available <= 0:
        raise ValueError("Review context must leave room for diffs after prompt overhead")
    count = provider.count_tokens if provider else _estimate_tokens
    graph: dict[str, set[str]] = {f.path: set() for f in files}
    for path, neighbors in (dependencies or {}).items():
        if path in graph:
            for other in neighbors & graph.keys():
                graph[path].add(other)
                graph[other].add(path)
    components: list[list[FileDiff]] = []
    seen: set[str] = set()
    for file in files:
        if file.path in seen:
            continue
        stack = [file.path]
        members: set[str] = set()
        while stack:
            path = stack.pop()
            if path in seen:
                continue
            seen.add(path)
            members.add(path)
            stack.extend(sorted(graph[path] - seen, reverse=True))
        components.append([f for f in files if f.path in members])

    chunks: list[ReviewChunk] = []
    sealed: set[int] = set()
    for group_id, group in enumerate(components):
        related = [f.path for f in group]
        group_chunks: list[ReviewChunk] = []
        for file in group:
            for part in _split_file(file, available, count):
                estimate = count(build_file_context_string(part))
                current = group_chunks[-1] if group_chunks else None
                if current and current.token_estimate + estimate <= available:
                    current.files.append(part)
                    current.token_estimate += estimate
                else:
                    group_chunks.append(ReviewChunk([part], estimate, group_id, list(related)))
        if len(group_chunks) == 1:
            candidate = group_chunks[0]
            for chunk in chunks:
                if (
                    chunk.group_id not in sealed
                    and chunk.token_estimate + candidate.token_estimate <= available
                    and (not max_files or len(chunk.files) + len(candidate.files) <= max_files)
                ):
                    chunk.files.extend(candidate.files)
                    chunk.token_estimate += candidate.token_estimate
                    chunk.related_paths.extend(related)
                    break
            else:
                chunks.extend(group_chunks)
        else:
            sealed.add(group_id)
            chunks.extend(group_chunks)
    return chunks
