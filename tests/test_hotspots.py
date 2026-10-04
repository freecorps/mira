"""Change-frequency heatmap and hotspots: scoring, store reads, merge-time
collection, the provider history adapters, the review note and the API."""

from __future__ import annotations

import json
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest

from mira.analytics import collect
from mira.analytics.hotspots import (
    complexity_proxy,
    compute_hotspots,
    directory_of,
    hotspot_review_note,
    rank,
    score_file,
)
from mira.config import HotspotsConfig, MiraConfig
from mira.index.store import FileSummary, IndexStore, SymbolInfo
from mira.models import CommitChurn, FileChangeStat, PRInfo
from mira.providers.forgejo import ForgejoProvider
from mira.providers.github import GitHubProvider
from mira.providers.gitlab import GitLabProvider

DAY = 86400.0


@pytest.fixture
def store(tmp_path: Path) -> IndexStore:
    s = IndexStore(str(tmp_path / "idx.db"), "acme", "app")
    yield s
    s.close()


def _pr(platform: str = "github", number: int = 7) -> PRInfo:
    return PRInfo(
        title="t",
        description="",
        base_branch="main",
        head_branch="feature",
        url=f"https://example.com/acme/app/pull/{number}",
        number=number,
        owner="acme",
        repo="app",
        platform=platform,
    )


def _summary(path: str, loc: int, n_symbols: int) -> FileSummary:
    return FileSummary(
        path=path,
        language="python",
        summary="",
        symbols=[SymbolInfo(f"s{i}", "function", "", "") for i in range(n_symbols)],
        loc=loc,
    )


def _finding(store: IndexStore, fid: str, path: str, *, state: str = "open", at: float) -> None:
    store._conn.execute(
        "INSERT INTO review_findings (id, fingerprint, path, state, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (fid, fid, path, state, at, at),
    )
    store._conn.commit()


# ── scoring (pure) ──


def test_complexity_proxy_is_one_for_unindexed_and_grows_slowly() -> None:
    assert complexity_proxy(0, 0) == 1.0
    small = complexity_proxy(100, 5)
    huge = complexity_proxy(10_000, 500)
    assert 1.0 < small < huge
    # log-scaled: 100x the code is nowhere near 100x the weight
    assert huge / small < 5


def test_score_multiplies_churn_complexity_and_finding_density() -> None:
    plain = score_file("a.py", changes=4)
    assert plain.churn == 4
    assert plain.complexity == 1.0
    assert plain.score == 4
    with_findings = score_file("a.py", changes=4, findings=2)
    assert with_findings.finding_density == 0.5
    assert with_findings.score == pytest.approx(6.0)
    with_lines = score_file("a.py", changes=4, lines_changed=200)
    assert with_lines.churn == 6
    # Either source alone gives touches; the max avoids double counting.
    assert score_file("a.py", changes=2, prs=5).changes == 5


def test_rank_orders_by_score_and_aggregates_directories() -> None:
    report = rank(
        churn_stats=[
            {"path": "src/core/engine.py", "changes": 10, "lines": 500, "last_changed_at": 5},
            {"path": "src/core/util.py", "changes": 2, "lines": 10, "last_changed_at": 4},
            {"path": "README.md", "changes": 3, "lines": 30, "last_changed_at": 3},
        ],
        reviewed_paths={"src/api/routes.py": {1, 2, 3, 4}},
        findings={"src/api/routes.py": 4},
        complexity={"src/core/engine.py": (2000, 80)},
        window_days=90,
    )
    paths = [f.path for f in report.files]
    assert paths[0] == "src/core/engine.py"
    assert set(paths) == {
        "src/core/engine.py",
        "src/core/util.py",
        "README.md",
        "src/api/routes.py",
    }
    assert report.total_files == 4
    assert report.max_score == report.files[0].score
    dirs = {d.path: d for d in report.directories}
    assert dirs["src/core"].files == 2
    assert dirs["src/core"].changes == 12
    assert dirs["."].files == 1
    assert report.sources == {"history": True, "reviews": True, "findings": True, "index": True}


def test_directory_of_depth() -> None:
    assert directory_of("a/b/c/d.py", 2) == "a/b"
    assert directory_of("a/d.py", 2) == "a"
    assert directory_of("d.py", 2) == "."


# ── store reads/writes ──


def test_file_churn_is_idempotent_per_reference(store: IndexStore) -> None:
    now = time.time()
    rows = [
        {"path": "a.py", "reference": "commit:1", "additions": 5, "deletions": 1, "event_at": now},
        {"path": "a.py", "reference": "commit:2", "additions": 2, "deletions": 0, "event_at": now},
        {"path": "b.py", "reference": "commit:2", "additions": 1, "deletions": 1, "event_at": now},
    ]
    assert store.record_file_churn(rows) == 3
    assert store.record_file_churn(rows) == 0
    stats = {r["path"]: r for r in store.file_churn_stats(now - DAY)}
    assert stats["a.py"]["changes"] == 2
    assert stats["a.py"]["lines"] == 8
    assert stats["b.py"]["changes"] == 1
    # Outside the window
    assert store.file_churn_stats(now + 1) == []


def test_churn_history_marker(store: IndexStore) -> None:
    assert store.churn_history_fetched_at("github") == 0.0
    store.mark_churn_history_fetched("github", commits=12, at=123.0)
    assert store.churn_history_fetched_at("github") == 123.0
    store.mark_churn_history_fetched("github", commits=3, at=456.0)
    assert store.churn_history_fetched_at("github") == 456.0
    assert store.churn_history_fetched_at("gitlab") == 0.0


def test_store_signals_feed_compute_hotspots(store: IndexStore) -> None:
    now = time.time()
    store.upsert_summary(_summary("src/big.py", 3000, 40))
    store.upsert_summary(_summary("src/small.py", 20, 1))
    for n in (1, 2, 3):
        store.record_review(
            n,
            f"PR {n}",
            f"u/{n}",
            0,
            0,
            0,
            reviewed_paths=json.dumps(["src/big.py", "src/small.py"]),
            created_at=now - DAY,
        )
    _finding(store, "f1", "src/big.py", at=now)
    _finding(store, "f2", "src/big.py", at=now)
    _finding(store, "f3", "src/small.py", state="dismissed", at=now)

    assert store.reviewed_pr_paths(now - 2 * DAY)["src/big.py"] == {1, 2, 3}
    assert store.finding_counts_by_path(now - DAY) == {"src/big.py": 2}
    assert store.file_complexity()["src/big.py"] == (3000, 40)

    report = compute_hotspots(store, window_days=30, now=now)
    assert [f.path for f in report.files] == ["src/big.py", "src/small.py"]
    big = report.files[0]
    assert big.changes == 3 and big.findings == 2 and big.loc == 3000


def test_compute_hotspots_survives_missing_signals() -> None:
    class Broken:
        def __getattr__(self, name: str) -> Any:
            def _boom(*_a: Any, **_k: Any) -> Any:
                raise RuntimeError("no such table")

            return _boom

    report = compute_hotspots(Broken(), window_days=30)
    assert report.files == []
    assert report.total_files == 0


def test_postgres_store_scopes_churn_by_repo(monkeypatch: pytest.MonkeyPatch) -> None:
    from mira.index import pg_store
    from mira.index.pg_store import PgIndexStore
    from tests.test_pg_store import _FakeConn

    conn = _FakeConn()
    monkeypatch.setattr(pg_store, "_get_conn", lambda url, **_kw: conn)
    monkeypatch.setattr(pg_store, "_new_pg_conn", lambda url: conn)
    a = PgIndexStore("acme", "app", "postgresql://fake")
    b = PgIndexStore("acme", "other", "postgresql://fake")
    row = {"path": "x.py", "reference": "pr:1", "event_at": time.time()}
    assert a.record_file_churn([row]) == 1
    assert a.record_file_churn([row]) == 0
    assert b.record_file_churn([row]) == 1  # same reference, other repository
    assert len(a.file_churn_stats(0)) == 1
    a.mark_churn_history_fetched("github", at=10.0)
    assert a.churn_history_fetched_at("github") == 10.0
    assert b.churn_history_fetched_at("github") == 0.0


# ── review note ──


def _seed_hotspot(store: IndexStore, path: str, changes: int) -> None:
    now = time.time()
    store.record_file_churn(
        [{"path": path, "reference": f"commit:{i}", "event_at": now} for i in range(changes)]
    )


def test_review_note_names_touched_top_hotspots(store: IndexStore) -> None:
    _seed_hotspot(store, "src/hot.py", 6)
    _seed_hotspot(store, "src/cold.py", 1)
    _finding(store, "f1", "src/hot.py", at=time.time())
    note = hotspot_review_note(store, ["src/hot.py", "src/cold.py"], HotspotsConfig())
    assert "### Change Hotspots" in note
    assert "`src/hot.py` (hotspot #1: 6 changes in 90d, 1 prior finding(s))" in note
    # Below review_min_changes (3): one edit is not a pattern.
    assert "src/cold.py" not in note


def test_review_note_is_config_gated_and_empty_without_hits(store: IndexStore) -> None:
    _seed_hotspot(store, "src/hot.py", 6)
    assert hotspot_review_note(store, ["src/hot.py"], HotspotsConfig(review_note=False)) == ""
    assert hotspot_review_note(store, ["src/hot.py"], HotspotsConfig(enabled=False)) == ""
    assert hotspot_review_note(store, ["src/other.py"], HotspotsConfig()) == ""
    assert hotspot_review_note(store, [], HotspotsConfig()) == ""
    # Outside the top N, even if it qualifies on changes.
    _seed_hotspot(store, "src/hotter.py", 9)
    note = hotspot_review_note(store, ["src/hot.py"], HotspotsConfig(review_top_n=1))
    assert note == ""


def test_hotspot_config_defaults() -> None:
    cfg = MiraConfig().analytics.hotspots
    assert cfg.enabled and cfg.review_note
    assert cfg.window_days == 90


# ── merge-time collection ──


class _FakeProvider:
    def __init__(self, commits: list[CommitChurn] | None, changes: list[FileChangeStat]) -> None:
        self.commits = commits
        self.changes = changes
        self.history_calls = 0

    async def get_commit_churn(self, pr_info: PRInfo, **kw: Any) -> list[CommitChurn]:
        self.history_calls += 1
        if self.commits is None:
            raise RuntimeError("rate limited")
        return self.commits

    async def get_pr_change_stats(self, pr_info: PRInfo) -> list[FileChangeStat]:
        return self.changes


@pytest.mark.asyncio
async def test_first_merge_backfills_history_then_records_merges(store: IndexStore) -> None:
    now = time.time()
    provider = _FakeProvider(
        [
            CommitChurn("c1", now - DAY, files=[FileChangeStat("a.py", 3, 1)]),
            CommitChurn("c2", now - 2 * DAY, files=[FileChangeStat("a.py", 1, 0)]),
        ],
        [FileChangeStat("b.py", 10, 2)],
    )
    cfg = MiraConfig()
    written = await collect.record_merge_churn(provider, _pr(), config=cfg, store=store, now=now)
    assert written == 2
    assert store.churn_history_fetched_at("github") == now
    # The backfill already contains this merge; its own stats are not added.
    assert {r["path"] for r in store.file_churn_stats(0)} == {"a.py"}

    written = await collect.record_merge_churn(
        provider, _pr(number=8), config=cfg, store=store, now=now
    )
    assert written == 1
    assert provider.history_calls == 1
    stats = {r["path"]: r for r in store.file_churn_stats(0)}
    assert stats["b.py"]["lines"] == 12


@pytest.mark.asyncio
async def test_failed_backfill_falls_back_and_retries_later(store: IndexStore) -> None:
    provider = _FakeProvider(None, [FileChangeStat("b.py", 1, 0)])
    written = await collect.record_merge_churn(provider, _pr(), config=MiraConfig(), store=store)
    assert written == 1
    assert store.churn_history_fetched_at("github") == 0.0  # will try again


@pytest.mark.asyncio
async def test_churn_collection_is_config_gated(store: IndexStore) -> None:
    cfg = MiraConfig()
    cfg.analytics.hotspots.enabled = False
    provider = _FakeProvider([], [FileChangeStat("b.py", 1, 0)])
    assert await collect.record_merge_churn(provider, _pr(), config=cfg, store=store) == 0
    cfg = MiraConfig()
    cfg.analytics.hotspots.history_max_commits = 0
    assert await collect.record_merge_churn(provider, _pr(), config=cfg, store=store) == 1
    assert provider.history_calls == 0


# ── provider adapters (HTTP fakes) ──


class _Resp:
    def __init__(self, status: int = 200, data: Any = None, text: str = "") -> None:
        self.status_code = status
        self._data = data
        self.text = text

    def json(self) -> Any:
        return self._data


def _fake_client(monkeypatch: pytest.MonkeyPatch, route: Any) -> list[str]:
    calls: list[str] = []

    class _Client:
        def __init__(self, *a: Any, **kw: Any) -> None:
            pass

        async def __aenter__(self) -> _Client:
            return self

        async def __aexit__(self, *a: Any) -> None:
            return None

        async def get(self, url: str, headers: Any = None, params: Any = None) -> _Resp:
            calls.append(url)
            return route(url, params or {})

    from mira.providers import delivery

    monkeypatch.setattr(delivery.httpx, "AsyncClient", _Client)
    return calls


@pytest.mark.asyncio
async def test_github_commit_churn_skips_merge_commits(monkeypatch: pytest.MonkeyPatch) -> None:
    def route(url: str, params: dict) -> _Resp:
        if url.endswith("/commits"):
            assert params["sha"] == "main"
            return _Resp(
                data=[
                    {"sha": "aaa", "parents": [{}]},
                    {"sha": "mmm", "parents": [{}, {}]},
                ]
            )
        if url.endswith("/commits/aaa"):
            return _Resp(
                data={
                    "commit": {
                        "message": "fix\n\nbody",
                        "committer": {"date": "2026-01-02T00:00:00Z"},
                    },
                    "files": [{"filename": "a.py", "additions": 3, "deletions": 2}],
                }
            )
        return _Resp(404)

    calls = _fake_client(monkeypatch, route)
    out = await GitHubProvider("t").get_commit_churn(_pr(), since=0)
    assert len(out) == 1
    assert out[0].sha == "aaa" and out[0].message == "fix"
    assert out[0].files[0].path == "a.py" and out[0].files[0].added_lines == 3
    assert not any(c.endswith("/commits/mmm") for c in calls)


@pytest.mark.asyncio
async def test_github_releases_and_first_commit(monkeypatch: pytest.MonkeyPatch) -> None:
    def route(url: str, params: dict) -> _Resp:
        if url.endswith("/releases"):
            return _Resp(
                data=[
                    {"tag_name": "v2", "published_at": "2026-02-01T00:00:00Z", "html_url": "u2"},
                    {"tag_name": "v-draft", "draft": True, "published_at": "2026-02-02T00:00:00Z"},
                    {"tag_name": "v1", "published_at": "2020-01-01T00:00:00Z"},
                ]
            )
        if url.endswith("/pulls/7/commits"):
            return _Resp(
                data=[
                    {"commit": {"author": {"date": "2026-01-05T00:00:00Z"}}},
                    {"commit": {"author": {"date": "2026-01-03T00:00:00Z"}}},
                ]
            )
        return _Resp(404)

    _fake_client(monkeypatch, route)
    gh = GitHubProvider("t")
    rels = await gh.list_deployment_releases(_pr(), since=1_700_000_000)
    assert [r.tag for r in rels] == ["v2"]
    first = await gh.get_pr_first_commit_at(_pr())
    assert first == pytest.approx(1767398400.0)  # 2026-01-03


@pytest.mark.asyncio
async def test_gitlab_commit_churn_counts_diff_lines(monkeypatch: pytest.MonkeyPatch) -> None:
    def route(url: str, params: dict) -> _Resp:
        if url.endswith("/repository/commits"):
            assert params["ref_name"] == "main"
            return _Resp(
                data=[
                    {"id": "abc", "parent_ids": ["p"], "committed_date": "2026-01-02T00:00:00Z"},
                    {"id": "mrg", "parent_ids": ["p", "q"]},
                ]
            )
        if url.endswith("/repository/commits/abc/diff"):
            return _Resp(
                data=[{"new_path": "a.py", "diff": "@@ -1,2 +1,2 @@\n-old\n+new\n+more\n ctx\n"}]
            )
        if url.endswith("/merge_requests/7/commits"):
            return _Resp(data=[{"authored_date": "2026-01-03T00:00:00Z"}])
        if url.endswith("/releases"):
            return _Resp(data=[{"tag_name": "v1", "released_at": "2026-01-04T00:00:00Z"}])
        return _Resp(404)

    _fake_client(monkeypatch, route)
    gl = GitLabProvider("t")
    out = await gl.get_commit_churn(_pr("gitlab"), since=0)
    assert [c.sha for c in out] == ["abc"]
    assert (out[0].files[0].added_lines, out[0].files[0].deleted_lines) == (2, 1)
    assert await gl.get_pr_first_commit_at(_pr("gitlab")) > 0
    assert [r.tag for r in await gl.list_deployment_releases(_pr("gitlab"))] == ["v1"]


@pytest.mark.asyncio
async def test_forgejo_commit_churn_parses_raw_diff_and_stops_at_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    diff = "diff --git a/a.py b/a.py\n--- a/a.py\n+++ b/a.py\n@@ -1,1 +1,2 @@\n-x\n+y\n+z\n"

    def route(url: str, params: dict) -> _Resp:
        if url.endswith("/pulls/7/commits"):
            return _Resp(data=[{"commit": {"author": {"date": "2026-01-03T00:00:00Z"}}}])
        if url.endswith("/commits"):
            return _Resp(
                data=[
                    {
                        "sha": "new",
                        "parents": [{}],
                        "commit": {"committer": {"date": "2026-01-02T00:00:00Z"}},
                        "files": [{"filename": "a.py"}],
                    },
                    {
                        "sha": "old",
                        "parents": [{}],
                        "commit": {"committer": {"date": "2020-01-02T00:00:00Z"}},
                    },
                ]
            )
        if url.endswith("/git/commits/new.diff"):
            return _Resp(text=diff)
        if url.endswith("/releases"):
            return _Resp(data=[{"tag_name": "v1", "published_at": "2026-01-04T00:00:00Z"}])
        return _Resp(404)

    _fake_client(monkeypatch, route)
    fj = ForgejoProvider("t")
    out = await fj.get_commit_churn(_pr("forgejo"), since=1_700_000_000)
    assert [c.sha for c in out] == ["new"]
    assert (out[0].files[0].added_lines, out[0].files[0].deleted_lines) == (2, 1)
    assert await fj.get_pr_first_commit_at(_pr("forgejo")) > 0
    assert [r.tag for r in await fj.list_deployment_releases(_pr("forgejo"))] == ["v1"]


@pytest.mark.asyncio
async def test_providers_degrade_to_empty_on_http_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    _fake_client(monkeypatch, lambda url, params: _Resp(500))
    for provider, platform in (
        (GitHubProvider("t"), "github"),
        (GitLabProvider("t"), "gitlab"),
        (ForgejoProvider("t"), "forgejo"),
    ):
        assert await provider.get_commit_churn(_pr(platform), since=0) == []
        assert await provider.list_deployment_releases(_pr(platform)) == []
        assert await provider.get_pr_first_commit_at(_pr(platform)) == 0.0


@pytest.mark.asyncio
async def test_listings_keep_what_they_read_when_the_connection_drops(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import httpx

    def route(url: str, params: dict) -> _Resp:
        if url.endswith("/commits") and params.get("page") == 1:
            return _Resp(data=[{"sha": f"c{i}", "parents": [{}]} for i in range(100)])
        if "/commits/" in url:
            return _Resp(data={"commit": {"message": "m"}, "files": []})
        raise httpx.ConnectError("reset")

    _fake_client(monkeypatch, route)
    gh = GitHubProvider("t")
    out = await gh.get_commit_churn(_pr(), since=0, max_commits=150)
    assert len(out) == 100
    assert await gh.list_deployment_releases(_pr()) == []
    assert await gh.get_pr_first_commit_at(_pr()) == 0.0


@pytest.mark.asyncio
async def test_github_first_commit_reads_every_page(monkeypatch: pytest.MonkeyPatch) -> None:
    late = {"commit": {"author": {"date": "2026-01-05T00:00:00Z"}}}
    early = {"commit": {"author": {"date": "2026-01-03T00:00:00Z"}}}

    def route(url: str, params: dict) -> _Resp:
        assert url.endswith("/pulls/7/commits")
        return _Resp(data=[late] * 100 if params["page"] == 1 else [early])

    calls = _fake_client(monkeypatch, route)
    assert await GitHubProvider("t").get_pr_first_commit_at(_pr()) == pytest.approx(1767398400.0)
    assert len(calls) == 2


# ── API ──


def test_hotspots_route(store: IndexStore, monkeypatch: pytest.MonkeyPatch) -> None:
    from mira.dashboard.routers import delivery

    _seed_hotspot(store, "src/hot.py", 4)

    @contextmanager
    def _open(owner: str, repo: str):
        assert (owner, repo) == ("acme", "app")
        yield store

    monkeypatch.setattr(delivery, "_open_store", _open)
    resp = delivery.repo_hotspots("acme", "app", days=30, limit=10)
    assert resp.enabled is True
    assert resp.window_days == 30
    assert resp.files[0]["path"] == "src/hot.py"
    assert resp.directories[0]["path"] == "src"

    cfg = MiraConfig()
    cfg.analytics.hotspots.enabled = False
    monkeypatch.setattr(delivery, "load_config", lambda: cfg)
    assert delivery.repo_hotspots("acme", "app").enabled is False
