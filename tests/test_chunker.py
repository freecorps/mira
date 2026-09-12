"""Tests for token-aware chunking."""

from __future__ import annotations

from mira.core.chunker import _file_token_estimate, chunk_files
from mira.models import FileChangeType, FileDiff, HunkInfo


def _make_file(path: str, content_size: int = 100) -> FileDiff:
    content = "x" * content_size
    return FileDiff(
        path=path,
        change_type=FileChangeType.MODIFIED,
        hunks=[HunkInfo(1, 10, 1, 10, content)],
        added_lines=10,
        deleted_lines=0,
    )


class TestChunkFiles:
    def test_single_file_single_chunk(self):
        files = [_make_file("a.py", 100)]
        chunks = chunk_files(files, max_tokens=10000)
        assert len(chunks) == 1
        assert len(chunks[0].files) == 1

    def test_multiple_files_fit_one_chunk(self):
        files = [_make_file(f"file{i}.py", 100) for i in range(3)]
        chunks = chunk_files(files, max_tokens=10000)
        assert len(chunks) == 1
        assert len(chunks[0].files) == 3

    def test_splits_into_multiple_chunks(self):
        files = [_make_file(f"file{i}.py", 4000) for i in range(5)]
        chunks = chunk_files(files, max_tokens=5000)
        assert len(chunks) > 1

    def test_oversized_file_is_split_without_losing_content(self):
        large = _make_file("big.py", 100000)
        chunks = chunk_files([large], max_tokens=5000)
        assert len(chunks) > 1
        body = "".join(
            h.content.split("\n", 1)[1] for c in chunks for f in c.files for h in f.hunks
        )
        assert body == large.hunks[0].content
        assert all(c.token_estimate <= 3000 for c in chunks)

    def test_empty_input(self):
        chunks = chunk_files([], max_tokens=10000)
        assert chunks == []

    def test_token_estimate(self):
        f = _make_file("a.py", 400)
        est = _file_token_estimate(f)
        assert est > 0
        assert est < 400  # Should be roughly 100 tokens for 400 chars


def test_fragmented_no_newline_marker_does_not_advance_source_coordinates():
    from mira.core.chunker import _split_file

    marker = "\\ No newline at end of file"
    body = f"@@ -10,1 +20,1 @@\n-old\n{marker}\n+new\n"
    file = FileDiff("f.py", FileChangeType.MODIFIED, [HunkInfo(10, 1, 20, 1, body)])

    # Model an unusually expensive metadata line to exercise the fallback
    # splitter, not the ordinary path that already excludes metadata offsets.
    def count(text):
        return len(text) + (1000 if marker in text else 0)

    parts = _split_file(file, 180, count)
    hunks = [h for part in parts for h in part.hunks]
    final = next(h for h in hunks if "+new" in h.content)
    assert (final.source_start, final.target_start) == (11, 20)
    fragments = [h for h in hunks if "long line fragment" in h.content]
    assert fragments
    assert all((h.source_length, h.target_length) == (0, 0) for h in fragments)
    rebuilt = "".join(h.content.split("\n", 1)[1] for h in hunks)
    assert rebuilt == f"-old\n{marker}\n+new\n"
