"""The review's one read of the reviewed commit: archive download and source fetcher."""

from __future__ import annotations

import asyncio
import io
import tarfile
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from mira.index.context import SnapshotSourceFetcher
from mira.models import PRInfo
from mira.platforms import fetch as fetch_module
from mira.platforms.fetch import RepoSnapshot, _snapshot_from_tarball, fetch_snapshot


def _tarball(files: dict[str, bytes], root: str = "owner-repo-abc123") -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as tf:
        for path, data in files.items():
            info = tarfile.TarInfo(f"{root}/{path}")
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))
    return buffer.getvalue()


def _pr() -> PRInfo:
    return PRInfo(
        title="t",
        description="",
        base_branch="main",
        head_branch="feature",
        url="https://github.com/o/r/pull/1",
        number=1,
        owner="o",
        repo="r",
        head_sha="abc123",
    )


class TestSnapshotFromTarball:
    def test_text_files_are_kept_and_every_path_is_listed(self):
        blob = _tarball(
            {
                "src/app.py": b"print('hi')\n",
                "logo.png": b"\x89PNG\r\n\x1a\n\x00\x00",
                "node_modules/lib/index.js": b"module.exports = 1",
                "data.bin": b"abc\x00def",
            }
        )
        snapshot = _snapshot_from_tarball(blob, 1_048_576, "o/r")
        assert snapshot is not None
        assert snapshot.files == {"src/app.py": "print('hi')\n"}
        # Held back, but known to exist: a reader can tell them from a typo.
        assert snapshot.paths == {
            "src/app.py",
            "logo.png",
            "node_modules/lib/index.js",
            "data.bin",
        }

    def test_oversized_file_is_listed_not_held(self):
        blob = _tarball({"big.py": b"x" * 100, "small.py": b"y"})
        snapshot = _snapshot_from_tarball(blob, 50, "o/r")
        assert snapshot is not None
        assert set(snapshot.files) == {"small.py"}
        assert "big.py" in snapshot.paths

    def test_memory_cap_leaves_files_listed_and_marks_the_snapshot_partial(self):
        blob = _tarball({"a.py": b"a" * 60, "b.py": b"b" * 60})
        snapshot = _snapshot_from_tarball(blob, 1000, "o/r", max_text_bytes=100)
        assert snapshot is not None
        assert len(snapshot.files) == 1
        assert snapshot.paths == {"a.py", "b.py"}
        assert snapshot.partial

    def test_corrupt_archive_is_no_snapshot(self):
        assert _snapshot_from_tarball(b"not a tarball", 1000, "o/r") is None


class TestFetchSnapshot:
    @pytest.fixture
    def serve(self, monkeypatch):
        def install(handler):
            real = httpx.AsyncClient

            def client(*args, **kwargs):
                kwargs["transport"] = httpx.MockTransport(handler)
                return real(*args, **kwargs)

            monkeypatch.setattr(fetch_module.httpx, "AsyncClient", client)

        return install

    async def test_downloads_and_decodes(self, serve):
        blob = _tarball({"a.py": b"a = 1\n"})
        serve(lambda request: httpx.Response(200, content=blob))
        snapshot = await fetch_snapshot(
            "https://api.example/tarball/abc", {}, label="o/r", max_bytes=10_000_000
        )
        assert snapshot is not None and snapshot.files == {"a.py": "a = 1\n"}

    async def test_archive_past_the_cap_is_abandoned(self, serve):
        blob = _tarball({f"f{i}.py": bytes(range(256)) * 40 for i in range(20)})
        serve(lambda request: httpx.Response(200, content=blob))
        snapshot = await fetch_snapshot(
            "https://api.example/tarball/abc", {}, label="o/r", max_bytes=len(blob) // 2
        )
        assert snapshot is None

    async def test_refused_download_is_no_snapshot(self, serve):
        serve(lambda request: httpx.Response(404))
        assert (
            await fetch_snapshot("https://api.example/x", {}, label="o/r", max_bytes=1000) is None
        )


class TestSnapshotSourceFetcher:
    def _provider(self, snapshot: object, content: str = "from api") -> MagicMock:
        provider = MagicMock()
        provider.get_repo_snapshot = AsyncMock(return_value=snapshot)
        provider.get_file_content = AsyncMock(return_value=content)
        provider.get_repo_tree = AsyncMock(return_value=["tree/only.py"])
        return provider

    async def test_reads_come_from_the_snapshot(self):
        snapshot = RepoSnapshot(files={"a.py": "a"}, paths={"a.py", "logo.png"})
        provider = self._provider(snapshot)
        source = SnapshotSourceFetcher(provider, _pr(), "abc123")
        source.start()

        assert await source.fetch("a.py") == "a"
        assert await source.tree() == ["a.py", "logo.png"]
        provider.get_repo_tree.assert_not_awaited()
        # Not in the commit at all: one tree read to be sure, no content read.
        assert await source.fetch("missing.py") is None
        assert await source.fetch("also-missing.py") is None
        provider.get_file_content.assert_not_awaited()
        provider.get_repo_tree.assert_awaited_once()
        provider.get_repo_snapshot.assert_awaited_once()

    async def test_path_the_archive_left_out_is_read_from_the_api(self):
        """`git archive` drops `export-ignore` paths and symlinks."""
        provider = self._provider(RepoSnapshot(files={"a.py": "a"}, paths={"a.py"}))
        provider.get_repo_tree = AsyncMock(return_value=["a.py", "tests/test_a.py"])
        source = SnapshotSourceFetcher(provider, _pr(), "abc123")
        assert await source.fetch("tests/test_a.py") == "from api"

    async def test_held_back_file_falls_back_to_the_api(self):
        snapshot = RepoSnapshot(files={}, paths={"huge.py"})
        provider = self._provider(snapshot, content="big text")
        source = SnapshotSourceFetcher(provider, _pr(), "abc123")
        assert await source.fetch("huge.py") == "big text"

    async def test_no_snapshot_means_per_file_reads(self):
        provider = self._provider(None)
        source = SnapshotSourceFetcher(provider, _pr(), "abc123")
        assert await source.fetch("a.py") == "from api"
        assert await source.tree() == ["tree/only.py"]

    async def test_a_stand_in_provider_is_not_mistaken_for_a_snapshot(self):
        """A mock's placeholder answer has an empty path list, which read as a
        snapshot would say no file exists at all."""
        provider = self._provider(MagicMock())
        source = SnapshotSourceFetcher(provider, _pr(), "abc123")
        assert await source.fetch("a.py") == "from api"

    async def test_turned_off_never_downloads(self):
        provider = self._provider(RepoSnapshot(files={"a.py": "a"}, paths={"a.py"}))
        source = SnapshotSourceFetcher(provider, _pr(), "abc123", use_snapshot=False)
        assert await source.fetch("a.py") == "from api"
        provider.get_repo_snapshot.assert_not_awaited()

    async def test_slow_download_times_out_to_per_file_reads(self):
        async def slow(*args, **kwargs):
            await asyncio.sleep(5)

        provider = self._provider(None)
        provider.get_repo_snapshot = slow
        source = SnapshotSourceFetcher(provider, _pr(), "abc123", timeout=0.05)
        assert await source.fetch("a.py") == "from api"

    async def test_late_archive_does_not_hold_reads_but_still_lands(self):
        """Past the read deadline a read goes to the API; the download carries on
        and serves the searches that need the whole repository."""
        landed = asyncio.Event()

        async def slow(*args, **kwargs):
            await landed.wait()
            return RepoSnapshot(files={"a.py": "from archive"}, paths={"a.py"})

        provider = self._provider(None)
        provider.get_repo_snapshot = slow
        source = SnapshotSourceFetcher(provider, _pr(), "abc123", read_wait=0.02)
        assert await source.fetch("a.py") == "from api"
        assert source.loaded() is None

        landed.set()
        snapshot = await source.snapshot(max_wait=1)
        assert snapshot is not None and source.loaded() is snapshot
        await source.aclose()

    async def test_a_repository_without_an_archive_is_not_asked_again_soon(self):
        provider = self._provider(None)
        first = SnapshotSourceFetcher(provider, _pr(), "abc123")
        assert await first.fetch("a.py") == "from api"
        second = SnapshotSourceFetcher(provider, _pr(), "def456")
        assert await second.fetch("a.py") == "from api"
        provider.get_repo_snapshot.assert_awaited_once()

    async def test_concurrent_reads_of_one_file_share_a_request(self):
        provider = self._provider(None)

        async def slow_content(*args, **kwargs):
            await asyncio.sleep(0.01)
            return "once"

        provider.get_file_content = AsyncMock(side_effect=slow_content)
        source = SnapshotSourceFetcher(provider, _pr(), "abc123", use_snapshot=False)
        results = await asyncio.gather(*(source.fetch("a.py") for _ in range(5)))
        assert results == ["once"] * 5
        assert provider.get_file_content.await_count == 1

    async def test_closed_fetcher_does_not_restart_the_download(self):
        provider = self._provider(RepoSnapshot(files={"a.py": "a"}, paths={"a.py"}))
        source = SnapshotSourceFetcher(provider, _pr(), "abc123")
        await source.aclose()
        assert await source.fetch("a.py") == "from api"
        provider.get_repo_snapshot.assert_not_awaited()
