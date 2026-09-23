"""Regressions for the review findings on the review-speed change (PR #34)."""

from __future__ import annotations

import io
import tarfile
from unittest.mock import AsyncMock, MagicMock

from mira.core.enclosing import _ranges_for
from mira.index.context import SnapshotSourceFetcher
from mira.llm.agentic_tools import AgenticToolExecutor, _in_skipped_dir
from mira.llm.response_parser import LLMComment, LLMReviewResponse, convert_to_review_comments
from mira.models import FileChangeType, FileDiff, HunkInfo, PRInfo
from mira.platforms.fetch import RepoSnapshot, _snapshot_from_tarball


def _tarball(files: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as tf:
        for path, data in files.items():
            info = tarfile.TarInfo(f"root/{path}")
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))
    return buffer.getvalue()


class _Snapshotted:
    def __init__(self, snapshot: RepoSnapshot) -> None:
        self._snapshot = snapshot

    def loaded(self) -> RepoSnapshot:
        return self._snapshot

    async def fetch(self, path: str) -> str | None:
        return self._snapshot.files.get(path)


async def test_a_file_too_big_to_hold_is_not_claimed_searched():
    snapshot = _snapshot_from_tarball(
        _tarball({"a.py": b"a = 1\n", "gen/schema.json": b"x" * 200}), 100, "o/r"
    )
    assert snapshot is not None and snapshot.oversized == {"gen/schema.json"}
    tree = sorted(snapshot.paths)
    out = await AgenticToolExecutor(source_fetcher=_Snapshotted(snapshot), repo_tree=tree).execute(
        "grep_repo", {"pattern": "nowhere"}
    )
    assert "could not be searched" in out and "whole repository" not in out


async def test_an_unreadable_tree_does_not_make_files_disappear():
    provider = MagicMock()
    provider.get_repo_snapshot = AsyncMock(
        return_value=RepoSnapshot(files={"a.py": "a"}, paths={"a.py"})
    )
    provider.get_repo_tree = AsyncMock(side_effect=RuntimeError("tree unavailable"))
    provider.get_file_content = AsyncMock(return_value="from api")
    pr = PRInfo("t", "", "main", "f", "u", 1, "o", "r", head_sha="abc")
    source = SnapshotSourceFetcher(provider, pr, "abc")
    assert await source.fetch("tests/export_ignored.py") == "from api"


def test_skipped_directories_are_whole_segments():
    assert _in_skipped_dir("vendor/lib.py")
    assert _in_skipped_dir("web/node_modules/x.js")
    assert not _in_skipped_dir("src/myvendor/code.py")
    assert not _in_skipped_dir("src/rebuild/step.py")


def test_whole_line_match_beats_a_fragment_elsewhere():
    """`x = 1` is a fragment of `max = 10`; the line that is `x = 1` wins."""
    file = FileDiff(
        path="a.py",
        change_type=FileChangeType.MODIFIED,
        hunks=[
            HunkInfo(10, 3, 10, 3, "@@ -10,3 +10,3 @@\n+max = 10\n middle = 2\n-x = 0\n+x = 1\n")
        ],
    )
    response = LLMReviewResponse(
        comments=[LLMComment(path="a.py", line=40, title="t", body="b", existing_code="x = 1")]
    )
    [comment] = convert_to_review_comments(response, {"a.py"}, [file])
    assert comment.line == 12


_SOURCE = "\n".join(
    ["def first():"]
    + [f"    a{i} = {i}" for i in range(12)]
    + ["    return a0", "", "def second():", "    return 2"]
)


def _removal_after_return() -> FileDiff:
    # The old file had a line after `return a0` (line 14) that this change removes.
    return FileDiff(
        path="m.py",
        change_type=FileChangeType.MODIFIED,
        hunks=[
            HunkInfo(
                13, 3, 13, 2, "@@ -13,3 +13,2 @@\n     a11 = 11\n     return a0\n-    log(a0)\n"
            )
        ],
        language="python",
    )


def test_removal_at_the_end_of_a_function_shows_that_function():
    ranges = _ranges_for(_removal_after_return(), _SOURCE)
    assert any(first == 1 for first, _last, _label in ranges)


def test_comment_change_inside_a_function_keeps_its_context():
    source = "def f():\n    x = 1\n    # why\n" + "".join(f"    y{i} = {i}\n" for i in range(10))
    file = FileDiff(
        path="c.py",
        change_type=FileChangeType.MODIFIED,
        hunks=[HunkInfo(3, 1, 3, 1, "@@ -3,1 +3,1 @@\n-    # old\n+    # why\n")],
        language="python",
    )
    assert _ranges_for(file, source)


def test_nested_function_is_found_inside_a_long_outer_one():
    """The re-parse leaves the outer header out on purpose (see `_nested`)."""
    source = "\n".join(
        ["def create_router(app):"]
        + [f"    setting_{i} = {i}" for i in range(200)]
        + ["", "    @app.get('/x')", "    def handler(request):", "        user = request.user"]
        + [f"        step_{i} = {i}" for i in range(10)]
        + ["        return user", "", "    return app"]
    )
    changed = source.split("\n").index("        return user") + 1
    file = FileDiff(
        path="r.py",
        change_type=FileChangeType.MODIFIED,
        hunks=[HunkInfo(changed, 1, changed, 1, f"@@ -{changed},1 +{changed},1 @@\n-x\n+x\n")],
        language="python",
    )
    [(first, last, label)] = _ranges_for(file, source)
    assert label == "handler"
    assert source.split("\n")[first - 1].strip() == "@app.get('/x')"
    assert last >= changed


def test_multi_line_quote_of_removed_code_copied_with_its_blank_gutter():
    file = FileDiff(
        path="g.py",
        change_type=FileChangeType.MODIFIED,
        hunks=[
            HunkInfo(
                5,
                3,
                5,
                1,
                "@@ -5,3 +5,1 @@\n-    total = compute(items)\n-    save(total)\n+    persist(items)\n",
            )
        ],
    )
    quote = "      -    total = compute(items)\n      -    save(total)"
    response = LLMReviewResponse(
        comments=[LLMComment(path="g.py", line=5, title="t", body="b", existing_code=quote)]
    )
    assert [c.line for c in convert_to_review_comments(response, {"g.py"}, [file])] == [5]
