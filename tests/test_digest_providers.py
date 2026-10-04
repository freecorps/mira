"""The digest reads on GitHub, GitLab and Forgejo, against canned API answers."""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

import pytest

from mira.exceptions import ProviderError
from mira.providers._time import epoch_to_iso, iso_to_epoch
from mira.providers.base import repository_ref
from mira.providers.forgejo import ForgejoProvider
from mira.providers.github import GitHubProvider
from mira.providers.gitlab import GitLabProvider

SINCE = iso_to_epoch("2026-09-28T00:00:00Z")
UNTIL = iso_to_epoch("2026-10-05T00:00:00Z")


class _Resp:
    def __init__(self, status: int = 200, data: Any = None) -> None:
        self.status_code = status
        self._data = data
        self.text = "" if data is None else str(data)[:50]
        self.headers: dict[str, str] = {}

    def json(self) -> Any:
        return self._data


class _Client:
    def __init__(self, handler: Any, log: list) -> None:
        self._handler = handler
        self._log = log

    async def __aenter__(self) -> _Client:
        return self

    async def __aexit__(self, *a: Any) -> bool:
        return False

    async def get(self, url: str, **kw: Any) -> _Resp:
        self._log.append((url, dict(kw.get("params") or {})))
        return self._handler(url, kw.get("params") or {})


def _patch(module: str, handler: Any) -> tuple[Any, list]:
    log: list = []
    return patch(f"{module}.httpx.AsyncClient", lambda *a, **k: _Client(handler, log)), log


def test_epoch_iso_round_trip() -> None:
    assert epoch_to_iso(SINCE) == "2026-09-28T00:00:00Z"
    assert iso_to_epoch(epoch_to_iso(UNTIL)) == UNTIL


# ── GitHub ──────────────────────────────────────────────────────────────────


def _gh_pr(number: int, merged_at: str | None, updated_at: str, **kw: Any) -> dict:
    return {
        "number": number,
        "title": f"PR {number}",
        "body": "body",
        "html_url": f"https://github.com/o/r/pull/{number}",
        "user": {"login": "alice"},
        "merged_at": merged_at,
        "updated_at": updated_at,
        "merge_commit_sha": f"sha{number}",
        "base": {"ref": "main"},
        "labels": [{"name": "bug"}, {"bad": True}],
        **kw,
    }


class TestGitHub:
    async def test_landed_pull_requests(self) -> None:
        page1 = [
            _gh_pr(3, "2026-10-01T10:00:00Z", "2026-10-02T00:00:00Z"),
            _gh_pr(2, None, "2026-10-01T00:00:00Z"),  # closed, not merged
            _gh_pr(4, "2026-10-06T00:00:00Z", "2026-10-06T00:00:00Z"),  # after the window
            _gh_pr(1, "2026-09-20T00:00:00Z", "2026-09-29T00:00:00Z"),  # before it
        ]

        def handler(url: str, params: dict) -> _Resp:
            if url.endswith("/pulls"):
                assert params["state"] == "closed" and params["base"] == "main"
                return _Resp(data=page1 if params["page"] == 1 else [])
            if url.endswith("/pulls/3/files"):
                return _Resp(data=[{"filename": "src/a.py"}, {"filename": "README.md"}])
            raise AssertionError(url)

        ctx, log = _patch("mira.providers.github", handler)
        with ctx:
            prs = await GitHubProvider("t").list_landed_pull_requests(
                repository_ref("github", "o", "r"), since=SINCE, until=UNTIL, base="main"
            )
        assert [p.number for p in prs] == [3]
        pr = prs[0]
        assert pr.labels == ["bug"] and pr.author == "alice"
        assert pr.files == ["src/a.py", "README.md"]
        assert pr.merge_commit_sha == "sha3" and pr.base_branch == "main"
        # A short page ends the walk.
        assert sum(1 for url, _ in log if url.endswith("/pulls")) == 1

    async def test_walk_stops_at_old_updates(self) -> None:
        full_page = [
            _gh_pr(i, None, "2026-09-01T00:00:00Z") for i in range(100)
        ]  # all older than the window

        def handler(url: str, params: dict) -> _Resp:
            return _Resp(data=full_page)

        ctx, log = _patch("mira.providers.github", handler)
        with ctx:
            prs = await GitHubProvider("t").list_landed_pull_requests(
                repository_ref("github", "o", "r"), since=SINCE, max_files=0
            )
        assert prs == [] and len(log) == 1

    async def test_listing_failure_raises(self) -> None:
        ctx, _ = _patch("mira.providers.github", lambda url, params: _Resp(status=502))
        with ctx, pytest.raises(ProviderError):
            await GitHubProvider("t").list_landed_pull_requests(
                repository_ref("github", "o", "r"), since=SINCE
            )

    async def test_file_failure_degrades(self) -> None:
        def handler(url: str, params: dict) -> _Resp:
            if url.endswith("/files"):
                return _Resp(status=500)
            return _Resp(data=[_gh_pr(3, "2026-10-01T10:00:00Z", "2026-10-02T00:00:00Z")])

        ctx, _ = _patch("mira.providers.github", handler)
        with ctx:
            prs = await GitHubProvider("t").list_landed_pull_requests(
                repository_ref("github", "o", "r"), since=SINCE
            )
        assert prs[0].files == []

    async def test_commits_compare_and_files(self) -> None:
        commit = {
            "sha": "abc",
            "html_url": "https://github.com/o/r/commit/abc",
            "author": {"login": "bob"},
            "commit": {
                "message": "Fix\n\nbody",
                "author": {"name": "Bob"},
                "committer": {"date": "2026-10-01T00:00:00Z"},
            },
            "parents": [{"sha": "p1"}, {"sha": "p2"}],
        }

        def handler(url: str, params: dict) -> _Resp:
            if url.endswith("/commits"):
                assert params["sha"] == "main" and params["since"] == "2026-09-28T00:00:00Z"
                assert params["until"] == "2026-10-05T00:00:00Z"
                return _Resp(data=[commit])
            if "/compare/" in url:
                assert url.endswith("/compare/v1.0...v1.1")
                return _Resp(data={"total_commits": 1, "commits": [commit]})
            if url.endswith("/commits/abc"):
                return _Resp(data={"files": [{"filename": "x.py"}]})
            raise AssertionError(url)

        ctx, _ = _patch("mira.providers.github", handler)
        provider = GitHubProvider("t")
        ref = repository_ref("github", "o", "r")
        with ctx:
            commits = await provider.list_commits(ref, ref="main", since=SINCE, until=UNTIL)
            compared = await provider.compare_commits(ref, "v1.0", "v1.1")
            files = await provider.get_commit_files(ref, "abc")
        assert commits[0].author == "bob" and commits[0].parents == ["p1", "p2"]
        assert commits[0].date == iso_to_epoch("2026-10-01T00:00:00Z")
        assert compared[0].sha == "abc"
        assert files == ["x.py"]

    async def test_commit_listing_failure_raises(self) -> None:
        ctx, _ = _patch("mira.providers.github", lambda url, params: _Resp(status=404))
        with ctx, pytest.raises(ProviderError):
            await GitHubProvider("t").list_commits(
                repository_ref("github", "o", "r"), ref="main", since=SINCE
            )


# ── GitLab ──────────────────────────────────────────────────────────────────


class TestGitLab:
    async def test_landed_merge_requests(self) -> None:
        mrs = [
            {
                "iid": 7,
                "title": "Add thing",
                "description": "d",
                "web_url": "https://gitlab.com/g/p/-/merge_requests/7",
                "author": {"username": "carol"},
                "merged_at": "2026-10-01T00:00:00Z",
                "merge_commit_sha": None,
                "squash_commit_sha": "sq7",
                "target_branch": "main",
                "labels": ["feature"],
            },
            {
                "iid": 6,
                "title": "Old",
                "merged_at": "2026-09-01T00:00:00Z",
                "updated_at": "2026-09-29T00:00:00Z",
            },
        ]

        def handler(url: str, params: dict) -> _Resp:
            if url.endswith("/merge_requests"):
                assert params["state"] == "merged"
                assert params["updated_after"] == "2026-09-28T00:00:00Z"
                assert params["target_branch"] == "main"
                return _Resp(data=mrs)
            if url.endswith("/merge_requests/7/changes"):
                return _Resp(data={"changes": [{"new_path": "src/x.py"}, {"old_path": "gone.py"}]})
            raise AssertionError(url)

        ctx, log = _patch("mira.providers.gitlab", handler)
        with ctx:
            prs = await GitLabProvider("t").list_landed_pull_requests(
                repository_ref("gitlab", "g/sub", "p"), since=SINCE, until=UNTIL, base="main"
            )
        assert [p.number for p in prs] == [7]
        assert prs[0].merge_commit_sha == "sq7" and prs[0].labels == ["feature"]
        assert prs[0].files == ["src/x.py", "gone.py"]
        assert "/projects/g%2Fsub%2Fp/" in log[0][0]

    async def test_commits_compare_and_files(self) -> None:
        commit = {
            "id": "abc",
            "message": "Fix",
            "author_name": "Dan",
            "committed_date": "2026-10-01T00:00:00+00:00",
            "web_url": "u",
            "parent_ids": ["p1"],
        }

        def handler(url: str, params: dict) -> _Resp:
            if url.endswith("/repository/commits"):
                assert params["ref_name"] == "main"
                return _Resp(data=[commit])
            if url.endswith("/repository/compare"):
                assert params == {"from": "v1", "to": "v2"}
                return _Resp(data={"commits": [commit, commit]})
            if url.endswith("/repository/commits/abc/diff"):
                return _Resp(data=[{"new_path": "a"}, {"new_path": "b"}])
            raise AssertionError(url)

        ctx, _ = _patch("mira.providers.gitlab", handler)
        provider = GitLabProvider("t")
        ref = repository_ref("gitlab", "g", "p")
        with ctx:
            commits = await provider.list_commits(ref, ref="main", since=SINCE)
            compared = await provider.compare_commits(ref, "v1", "v2", limit=1)
            files = await provider.get_commit_files(ref, "abc", limit=1)
        assert commits[0].parents == ["p1"] and commits[0].author == "Dan"
        assert len(compared) == 1
        assert files == ["a"]

    async def test_failure_raises(self) -> None:
        ctx, _ = _patch("mira.providers.gitlab", lambda url, params: _Resp(status=403))
        with ctx, pytest.raises(ProviderError):
            await GitLabProvider("t").compare_commits(repository_ref("gitlab", "g", "p"), "a", "b")


# ── Forgejo ─────────────────────────────────────────────────────────────────


class TestForgejo:
    async def test_landed_pull_requests(self) -> None:
        pulls = [
            {
                "number": 4,
                "title": "Merged",
                "merged": True,
                "merged_at": "2026-10-02T00:00:00Z",
                "updated_at": "2026-10-02T00:00:00Z",
                "merge_commit_sha": "m4",
                "base": {"ref": "main"},
                "labels": [{"name": "enhancement"}],
                "user": {"login": "erin"},
            },
            {
                "number": 5,
                "title": "Other base",
                "merged": True,
                "merged_at": "2026-10-02T00:00:00Z",
                "updated_at": "2026-10-02T00:00:00Z",
                "base": {"ref": "release"},
            },
            {"number": 6, "title": "Closed", "merged": False, "updated_at": "2026-10-01"},
        ]

        def handler(url: str, params: dict) -> _Resp:
            if url.endswith("/pulls"):
                assert params["sort"] == "recentupdate"
                return _Resp(data=pulls)
            if url.endswith("/pulls/4/files"):
                return _Resp(data=[{"filename": "docs/a.md"}])
            raise AssertionError(url)

        ctx, _ = _patch("mira.providers.forgejo", handler)
        with ctx:
            prs = await ForgejoProvider("t").list_landed_pull_requests(
                repository_ref("forgejo", "o", "r"), since=SINCE, until=UNTIL, base="main"
            )
        assert [p.number for p in prs] == [4]
        assert prs[0].files == ["docs/a.md"] and prs[0].labels == ["enhancement"]

    async def test_commits_compare_and_files(self) -> None:
        commit = {
            "sha": "abc",
            "html_url": "u",
            "commit": {"message": "Fix", "committer": {"date": "2026-10-01T00:00:00Z"}},
            "parents": [{"sha": "p1"}],
            "files": [{"filename": "f.py"}],
        }

        def handler(url: str, params: dict) -> _Resp:
            if url.endswith("/commits"):
                assert params["sha"] == "main" and params["files"] == "true"
                return _Resp(data=[commit])
            if "/compare/" in url:
                return _Resp(data={"commits": [commit]})
            if url.endswith("/git/commits/abc"):
                return _Resp(data={"files": [{"filename": "g.py"}]})
            raise AssertionError(url)

        ctx, _ = _patch("mira.providers.forgejo", handler)
        provider = ForgejoProvider("t")
        ref = repository_ref("forgejo", "o", "r")
        with ctx:
            commits = await provider.list_commits(ref, ref="main", since=SINCE, until=UNTIL)
            compared = await provider.compare_commits(ref, "v1", "v2")
            files = await provider.get_commit_files(ref, "abc")
        assert commits[0].files == ["f.py"] and commits[0].parents == ["p1"]
        assert compared[0].sha == "abc"
        assert files == ["g.py"]

    async def test_failure_raises(self) -> None:
        ctx, _ = _patch("mira.providers.forgejo", lambda url, params: _Resp(status=500))
        with ctx, pytest.raises(ProviderError):
            await ForgejoProvider("t").list_landed_pull_requests(
                repository_ref("forgejo", "o", "r"), since=SINCE
            )
