"""Escaped bugs: reverts and hotfixes linked back to pull requests Mira reviewed."""

from __future__ import annotations

import json
import time
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import HTTPException

from mira.config import LearningConfig, MiraConfig
from mira.feedback.models import ReviewFinding
from mira.models import BlameRange, CommitInfo, IssueInfo, PRInfo
from mira.providers.forgejo import ForgejoProvider
from mira.providers.github import GitHubProvider
from mira.providers.gitlab import GitLabProvider
from mira.quality import history
from mira.quality.backtest import repo_scope
from mira.quality.config_models import EscapedBugsConfig
from mira.quality.escaped import (
    MISSED_FEEDBACK_KIND,
    classify_commit,
    classify_pull_request,
    process_merged_pull_request,
    process_pushed_commits,
    scan_history,
)
from mira.quality.models import (
    KIND_HOTFIX,
    KIND_REVERT,
    LINK_BLAME,
    LINK_DIFF_OVERLAP,
    LINK_REVERT_BRANCH,
    LINK_REVERT_SHA,
    EscapedBug,
)
from mira.quality.store import QualityStore, QualityStoreUnavailable, open_quality_store
from mira.quality.webhooks import push_commits
from tests.quality_support import DAY, FakeHistoryProvider, file_diff, merged_pr

NOW = time.time()
SCOPE = repo_scope("acme", "app", "github")


def _config(**kw: Any) -> EscapedBugsConfig:
    return EscapedBugsConfig(enabled=True, **kw)


def _mira_config(**kw: Any) -> MiraConfig:
    config = MiraConfig()
    config.escaped_bugs = _config(**kw)
    return config


def _finding(pr: int, path: str, line: int, fid: str = "") -> ReviewFinding:
    fid = fid or f"f-{pr}-{path}-{line}"
    return ReviewFinding(
        id=fid,
        fingerprint=fid,
        review_id=1,
        platform="github",
        owner="acme",
        repo="app",
        pr_number=pr,
        pr_url="",
        base_sha="",
        head_sha="",
        path=path,
        start_line=line,
        end_line=line,
        symbol="",
        category="bug",
        severity="warning",
        confidence=0.9,
        title="t",
        body="b",
        suggestion="",
        detector="main",
        prompt_model="",
    )


def _reviewed(pr: int, *, paths: list[str] | None = None, findings: list[tuple[str, int]] = ()):
    with open_quality_store("acme", "app") as store:
        store.index_store.record_review(
            pr_number=pr,
            pr_title=f"PR {pr}",
            pr_url="",
            comments_posted=len(findings),
            blockers=0,
            warnings=len(findings),
            reviewed_paths=json.dumps(paths or []),
            created_at=NOW - 5 * DAY,
        )
        for path, line in findings:
            store.index_store.save_review_finding(_finding(pr, path, line))


# ──────────────────────────────────────────────────────────── classifying ──


async def test_revert_is_recognised_by_title_body_and_branch() -> None:
    provider = FakeHistoryProvider()
    by_body = merged_pr(9, title='Revert "Add cache"', body="This reverts commit ABCDEF1234.")
    event = await classify_pull_request(provider, SCOPE, by_body, _config())
    assert event is not None and event.kind == KIND_REVERT
    assert event.reverted_shas == ["abcdef1234"]

    by_branch = merged_pr(10, title="Undo", head_branch="revert-5-add-cache")
    event = await classify_pull_request(provider, SCOPE, by_branch, _config())
    assert event is not None and event.reverted_prs == [5]


async def test_hotfix_is_recognised_by_label_branch_or_bug_issue() -> None:
    provider = FakeHistoryProvider(
        issues={
            40: IssueInfo(number=40, title="", body="", state="closed", url="", labels=["Bug"]),
            41: IssueInfo(number=41, title="", body="", state="closed", url="", labels=["docs"]),
        }
    )
    labelled = merged_pr(1, labels=["hotfix"])
    branch = merged_pr(2, head_branch="hotfix/login")
    fixes_bug = merged_pr(3, body="Fixes #40")
    fixes_docs = merged_pr(4, body="Fixes #41")
    plain = merged_pr(5, title="Add feature")
    assert (await classify_pull_request(provider, SCOPE, labelled, _config())).kind == KIND_HOTFIX
    assert (await classify_pull_request(provider, SCOPE, branch, _config())).kind == KIND_HOTFIX
    event = await classify_pull_request(provider, SCOPE, fixes_bug, _config())
    assert event is not None and "fixes-bug:#40" in event.reasons
    assert await classify_pull_request(provider, SCOPE, fixes_docs, _config()) is None
    assert await classify_pull_request(provider, SCOPE, plain, _config()) is None
    assert (
        await classify_pull_request(provider, SCOPE, labelled, _config(detect_hotfixes=False))
        is None
    )


def test_pushed_commits_are_classified_from_their_message() -> None:
    revert = CommitInfo(
        sha="r1", message='Revert "x"\n\nThis reverts commit 0123abcd.', parents=["p"]
    )
    hotfix = CommitInfo(sha="h1", message="hotfix: guard null user", parents=["p"])
    feature = CommitInfo(sha="f1", message="add things", parents=["p"])
    assert classify_commit(revert, _config()).reverted_shas == ["0123abcd"]
    assert classify_commit(hotfix, _config()).kind == KIND_HOTFIX
    assert classify_commit(feature, _config()) is None


def test_config_scope() -> None:
    assert not EscapedBugsConfig().tracks("acme", "app")  # disabled by default
    assert _config().tracks("acme", "app")
    assert _config(repos=["acme/app"]).tracks("ACME", "app")
    assert not _config(repos=["acme/other"]).tracks("acme", "app")


# ─────────────────────────────────────────────────────────────── linking ──


def _revert_provider() -> FakeHistoryProvider:
    return FakeHistoryProvider(
        merged={
            5: merged_pr(5, title="Add cache", merged_at=NOW - 3 * DAY),
            9: merged_pr(
                9,
                title='Revert "Add cache"',
                body="This reverts commit abc1234.",
                merged_at=NOW,
            ),
        },
        diffs={9: file_diff("a.py", 10, ["c1", "c2", "c3", "c4", "c5"], 10, [])},
        prs_for_commit={"abc1234": [5]},
    )


async def test_revert_links_to_the_reverted_pr_and_sees_mira_flagged_it() -> None:
    _reviewed(5, findings=[("a.py", 12)])
    provider = _revert_provider()
    bugs = await process_merged_pull_request(
        provider, await provider.get_pr_info("x/9"), config=_mira_config()
    )
    [bug] = bugs
    assert (bug.kind, bug.original_pr_number, bug.path) == (KIND_REVERT, 5, "a.py")
    assert bug.link_method == LINK_REVERT_SHA
    assert bug.flagged and bug.finding_ids == ["f-5-a.py-12"]
    assert (bug.line_start, bug.line_end) == (10, 14)
    assert bug.learning_candidate_id == 0  # caught, so nothing to learn
    assert provider.writes == []

    # A redelivered webhook records nothing new.
    again = await process_merged_pull_request(
        provider, await provider.get_pr_info("x/9"), config=_mira_config()
    )
    assert again == []
    with open_quality_store("acme", "app") as store:
        assert store.escaped_bug_counts() == {"caught": 1, "missed": 0, "total": 1}


async def test_revert_branch_links_without_a_sha() -> None:
    _reviewed(5, paths=["a.py"])
    provider = _revert_provider()
    provider.merged[9].body = ""
    provider.merged[9].head_branch = "revert-5-add-cache"
    [bug] = await process_merged_pull_request(
        provider, await provider.get_pr_info("x/9"), config=_mira_config(feed_learning=False)
    )
    assert bug.link_method == LINK_REVERT_BRANCH and not bug.flagged


async def test_a_pr_mira_never_reviewed_is_not_an_escape_of_mira() -> None:
    provider = _revert_provider()
    assert (
        await process_merged_pull_request(
            provider, await provider.get_pr_info("x/9"), config=_mira_config()
        )
        == []
    )


async def test_disabled_tracking_makes_no_requests() -> None:
    provider = _revert_provider()
    provider.get_landed_pull_request = AsyncMock()  # type: ignore[method-assign]
    pr_info = await provider.get_pr_info("x/9")
    assert await process_merged_pull_request(provider, pr_info, config=MiraConfig()) == []
    provider.get_landed_pull_request.assert_not_called()


def _hotfix_provider(*, blame: bool) -> FakeHistoryProvider:
    return FakeHistoryProvider(
        merged={
            6: merged_pr(6, title="Rework totals", merged_at=NOW - 10 * DAY),
            20: merged_pr(20, title="Fix totals", labels=["hotfix"], merged_at=NOW),
        },
        diffs={
            20: file_diff("b.py", 30, ["total = a - b", "return total"], 30, ["total = a + b"]),
            6: file_diff("b.py", 27, [], 27, [f"x{i}" for i in range(7)]),
        },
        blame=(
            {
                "b.py": [
                    BlameRange(start=1, end=29, sha="ancient"),
                    BlameRange(start=30, end=35, sha="c6"),
                ]
            }
            if blame
            else None
        ),
        prs_for_commit={"c6": [6], "ancient": [1]},
    )


async def test_hotfix_is_linked_by_blame_and_a_miss_feeds_learning() -> None:
    _reviewed(6, findings=[("b.py", 80)])
    provider = _hotfix_provider(blame=True)
    [bug] = await process_merged_pull_request(
        provider, await provider.get_pr_info("x/20"), config=_mira_config()
    )
    assert (bug.kind, bug.original_pr_number, bug.link_method) == (KIND_HOTFIX, 6, LINK_BLAME)
    assert not bug.flagged and bug.flagged_file
    assert bug.learning_candidate_id > 0

    with open_quality_store("acme", "app") as store:
        index = store.index_store
        candidate = index.get_learning_candidate(bug.learning_candidate_id)
        assert candidate.status == "pending"
        assert candidate.scope_type == "path" and candidate.scope_value == "b.py"
        example = json.loads(candidate.positive_examples_json)[0]
        assert example["original_pr"] == 6 and example["escaped_bug_id"] == bug.id
        events = [e for e in index.list_feedback_v2(limit=50) if e.kind == MISSED_FEEDBACK_KIND]
        assert len(events) == 1 and events[0].source_event_id == f"escaped:{bug.id}"
        assert store.get_escaped_bug(bug.id).learning_candidate_id == bug.learning_candidate_id


async def test_learning_feed_respects_the_learning_switches() -> None:
    _reviewed(6, findings=[("b.py", 80)])
    provider = _hotfix_provider(blame=True)
    config = _mira_config()
    config.learning = LearningConfig(learning_synthesis=False)
    [bug] = await process_merged_pull_request(
        provider, await provider.get_pr_info("x/20"), config=config
    )
    assert bug.learning_candidate_id == 0


async def test_hotfix_falls_back_to_diff_overlap_without_blame() -> None:
    _reviewed(6, paths=["b.py"], findings=[("b.py", 31)])
    provider = _hotfix_provider(blame=False)
    [bug] = await process_merged_pull_request(
        provider, await provider.get_pr_info("x/20"), config=_mira_config()
    )
    assert bug.link_method == LINK_DIFF_OVERLAP and bug.original_pr_number == 6
    assert bug.flagged


async def test_originals_outside_the_window_are_ignored() -> None:
    _reviewed(6, findings=[("b.py", 80)])
    provider = _hotfix_provider(blame=True)
    provider.merged[6].merged_at = NOW - 400 * DAY
    assert (
        await process_merged_pull_request(
            provider, await provider.get_pr_info("x/20"), config=_mira_config(window_days=90)
        )
        == []
    )


async def test_a_revert_pushed_to_the_default_branch_is_tracked() -> None:
    _reviewed(5, findings=[("a.py", 40)])
    provider = _revert_provider()
    provider.commits["r1"] = CommitInfo(
        sha="r1", message='Revert "Add cache"\n\nThis reverts commit abc1234.', parents=["p0"]
    )
    provider.commit_diffs["r1"] = file_diff("a.py", 10, ["c1", "c2"], 10, [])
    payload = {
        "commits": [
            {"id": "r1", "message": 'Revert "Add cache"\n\nThis reverts commit abc1234.'},
            {"id": "n1", "message": "docs"},
        ]
    }
    bugs = await process_pushed_commits(
        provider, "acme", "app", push_commits(payload), config=_mira_config(feed_learning=False)
    )
    [bug] = bugs
    assert bug.fix_ref == "commit:r1" and bug.original_pr_number == 5 and not bug.flagged


async def test_history_scan_records_and_counts() -> None:
    _reviewed(5, findings=[("a.py", 12)])
    _reviewed(6, findings=[("b.py", 80)])
    provider = _revert_provider()
    hot = _hotfix_provider(blame=True)
    provider.merged.update(hot.merged)
    provider.diffs.update(hot.diffs)
    provider.blame = hot.blame
    provider.prs_for_commit.update(hot.prs_for_commit)

    result = await scan_history(
        provider, "acme", "app", config=_mira_config(feed_learning=False), limit=50
    )
    assert result.examined == 4 and result.fix_events == 2
    assert {b.original_pr_number for b in result.recorded} == {5, 6}

    summary = history.recall_summary()
    assert summary["totals"] == {"caught": 1, "missed": 1, "total": 2, "recall": 0.5}
    assert summary["repos"][0]["owner"] == "acme"
    listed = history.list_escaped_bugs(flagged=False)
    assert [b["original_pr_number"] for b in listed] == [6]


# ───────────────────────────────────────────────────────────── dashboard ──


def _admin(is_admin: bool = True) -> Any:
    user = SimpleNamespace(id=1, username="admin", is_admin=is_admin)
    return SimpleNamespace(state=SimpleNamespace(user=user))


async def test_quality_routes_serve_admins_only() -> None:
    from mira.dashboard.routers import quality as routes

    _reviewed(5, findings=[("a.py", 12)])
    provider = _revert_provider()
    await process_merged_pull_request(
        provider, await provider.get_pr_info("x/9"), config=_mira_config()
    )
    summary = routes.quality_summary(_admin())
    assert summary.totals["caught"] == 1
    page = routes.list_escaped_bugs(_admin(), flagged="yes")
    assert page.bugs[0]["original_pr_number"] == 5
    assert routes.list_backtests(_admin()).runs == []
    with pytest.raises(HTTPException) as exc:
        routes.quality_summary(_admin(is_admin=False))
    assert exc.value.status_code == 403
    with pytest.raises(HTTPException):
        routes.list_escaped_bugs(_admin(), flagged="maybe")
    with pytest.raises(HTTPException):
        routes.get_backtest(_admin(), "acme", "app", "../etc")


# ─────────────────────────────────────────────────── provider adapters ──


class _Resp:
    def __init__(self, status: int = 200, payload: Any = None, text: str = "") -> None:
        self.status_code = status
        self._payload = payload
        self.text = text

    def json(self) -> Any:
        return self._payload


def _pr() -> PRInfo:
    return repo_scope("acme", "app", "github")


async def test_github_lists_merged_prs_and_maps_commits() -> None:
    pulls = [
        {
            "number": 3,
            "title": "Fix",
            "html_url": "u3",
            "merged_at": "2026-08-02T00:00:00Z",
            "updated_at": "2026-08-02T00:00:00Z",
            "merge_commit_sha": "m3",
            "labels": [{"name": "hotfix"}],
            "base": {"ref": "main", "sha": "b3"},
            "head": {"ref": "hotfix/x", "sha": "h3"},
            "user": {"login": "dana"},
        },
        {"number": 4, "title": "closed", "merged_at": None, "updated_at": "2026-08-01T00:00:00Z"},
    ]
    calls: list[str] = []

    class _Client:
        async def __aenter__(self) -> Any:
            return self

        async def __aexit__(self, *a: Any) -> None:
            return None

        async def get(self, url: str, **kwargs: Any) -> _Resp:
            calls.append(url)
            if url.endswith("/pulls"):
                return _Resp(200, pulls)
            if "/commits/abc/pulls" in url:
                return _Resp(200, [{"number": 3, "merged_at": "x"}, {"number": 8}])
            return _Resp(404)

    provider = GitHubProvider("token")
    with patch("httpx.AsyncClient", lambda *a, **k: _Client()):
        merged = await provider.list_landed_pull_requests(_pr(), since=0.0, limit=5, max_files=0)
        prs = await provider.get_prs_for_commit(_pr(), "abc")
    assert [m.number for m in merged] == [3]
    assert merged[0].labels == ["hotfix"] and merged[0].merged_at > 0
    assert merged[0].merge_commit_sha == "m3"
    assert (merged[0].head_branch, merged[0].head_sha, merged[0].base_sha) == (
        "hotfix/x",
        "h3",
        "b3",
    )
    assert prs == [3]


async def test_github_blame_ranges_come_from_graphql() -> None:
    provider = GitHubProvider("token")
    data = {
        "repository": {
            "object": {
                "blame": {
                    "ranges": [
                        {"startingLine": 1, "endingLine": 4, "commit": {"oid": "aaa"}},
                        {"startingLine": 5, "endingLine": 9, "commit": {"oid": "bbb"}},
                    ]
                }
            }
        }
    }
    with patch.object(GitHubProvider, "_graphql_request", AsyncMock(return_value=data)):
        ranges = await provider.get_blame(_pr(), "a.py", "base")
    assert [(r.start, r.end, r.sha) for r in ranges] == [(1, 4, "aaa"), (5, 9, "bbb")]


async def test_gitlab_blame_groups_become_line_ranges() -> None:
    provider = GitLabProvider("token")
    groups = [
        {"commit": {"id": "aaa"}, "lines": ["1", "2", "3"]},
        {"commit": {"id": "bbb"}, "lines": ["4"]},
    ]
    with patch.object(GitLabProvider, "_request", AsyncMock(return_value=_Resp(200, groups))):
        ranges = await provider.get_blame(_pr(), "a.py", "base")
    assert [(r.start, r.end, r.sha) for r in ranges] == [(1, 3, "aaa"), (4, 4, "bbb")]


async def test_gitlab_reads_one_landed_mr_and_maps_commits() -> None:
    provider = GitLabProvider("token")
    mr = {
        "iid": 7,
        "title": "Revert x",
        "web_url": "u",
        "state": "merged",
        "merged_at": "2026-08-02T00:00:00Z",
        "labels": ["bug"],
        "diff_refs": {"base_sha": "b"},
        "sha": "h",
        "source_branch": "s",
        "target_branch": "main",
        "squash_commit_sha": "sq",
    }

    async def _request(self: Any, method: str, url: str, **kw: Any) -> _Resp:
        if url.endswith("/merge_requests"):
            return _Resp(200, [mr])
        if url.endswith("/merge_requests/7"):
            return _Resp(200, mr)
        if "/commits/abc/merge_requests" in url:
            return _Resp(200, [mr, {"iid": 8, "state": "opened"}])
        return _Resp(404)

    with patch.object(GitLabProvider, "_request", _request):
        one = await provider.get_landed_pull_request(_pr(), 7)
        prs = await provider.get_prs_for_commit(_pr(), "abc")
    assert one is not None and one.labels == ["bug"] and one.merge_commit_sha == "sq"
    assert one.head_branch == "s" and one.base_sha == "b"
    assert prs == [7]


async def test_forgejo_maps_a_commit_to_its_pr_and_cannot_blame() -> None:
    provider = ForgejoProvider("token")

    async def _request(self: Any, method: str, url: str, **kw: Any) -> _Resp:
        if url.endswith("/commits/abc/pull"):
            return _Resp(200, {"number": 12, "merged": True})
        if url.endswith("/commits"):
            return _Resp(
                200,
                [
                    {
                        "sha": "s1",
                        "commit": {"message": "fix", "committer": {"date": "2026-08-02T00:00:00Z"}},
                    },
                    {
                        "sha": "s0",
                        "commit": {"message": "old", "committer": {"date": "2020-01-01T00:00:00Z"}},
                    },
                ],
            )
        return _Resp(404)

    with patch.object(ForgejoProvider, "_request", _request):
        assert await provider.get_prs_for_commit(_pr(), "abc") == [12]
        assert await provider.get_prs_for_commit(_pr(), "zzz") == []
        recent = await provider.list_path_commits(_pr(), "a.py", since=1_700_000_000)
    assert [c.sha for c in recent] == ["s1"]
    with pytest.raises(NotImplementedError):
        await provider.get_blame(_pr(), "a.py", "main")


# ─────────────────────────────────────────────────────────────── webhooks ──


class _Tasks:
    def __init__(self) -> None:
        self.tasks: list[tuple[Any, tuple]] = []

    def add_task(self, fn: Any, *args: Any, **kwargs: Any) -> None:
        self.tasks.append((fn, args))


async def test_forgejo_merge_runs_escaped_bug_detection(monkeypatch: Any) -> None:
    """Forgejo's merge handler (shared with delivery analytics) runs detection too."""
    from mira.platforms import handlers
    from mira.platforms.forgejo import webhook as forgejo_webhook

    auth = SimpleNamespace(get_bot_identity=AsyncMock(return_value="mira"), get_token=AsyncMock())
    payload = {
        "action": "closed",
        "sender": {"login": "dana"},
        "repository": {"full_name": "acme/app"},
        "pull_request": {"number": 9, "merged": True, "html_url": "https://f/acme/app/pulls/9"},
    }
    tasks = _Tasks()
    status = await forgejo_webhook.dispatch_forgejo_event(
        "pull_request", payload, auth, "mira", tasks
    )
    assert status == "processing"
    assert any(fn is forgejo_webhook.handle_forgejo_merged for fn, _ in tasks.tasks)

    pr_info = SimpleNamespace(owner="acme", repo="app", url="u")
    provider = SimpleNamespace(get_pr_info=AsyncMock(return_value=pr_info))
    monkeypatch.setattr(forgejo_webhook, "create_provider", lambda *_a: provider)
    detect = AsyncMock()
    monkeypatch.setattr(handlers, "run_escaped_bug_detection", detect)
    await forgejo_webhook.handle_forgejo_merged(payload, auth)
    detect.assert_awaited_once_with(provider, pr_info, platform="forgejo")


async def test_github_default_branch_push_schedules_the_scan(monkeypatch: Any) -> None:
    from mira.platforms.github import webhook as github_webhook
    from mira.quality import webhooks as quality_hooks

    monkeypatch.setattr(quality_hooks, "tracked", lambda owner, repo: True)
    payload = {
        "ref": "refs/heads/main",
        "repository": {"name": "app", "owner": {"login": "acme"}, "default_branch": "main"},
        "sender": {"login": "dana"},
        "installation": {"id": 1},
        "commits": [{"id": "r1", "message": "Revert x"}],
    }
    tasks = _Tasks()
    await github_webhook.dispatch_github_event("push", payload, SimpleNamespace(), "mira", tasks)
    [(_fn, args)] = [t for t in tasks.tasks if t[0] is quality_hooks.on_push]
    assert args[:4] == ("github", "acme", "app", [{"id": "r1", "message": "Revert x"}])


# ──────────────────────────────────────────────── review-bot regressions ──


async def test_revert_links_only_files_each_original_changed() -> None:
    _reviewed(5, paths=["a.py"])
    _reviewed(7, paths=["c.py"])
    provider = FakeHistoryProvider(
        merged={
            5: merged_pr(5, merged_at=NOW - 3 * DAY),
            7: merged_pr(7, merged_at=NOW - 2 * DAY),
            9: merged_pr(
                9,
                title="Revert two changes",
                body="This reverts commit abc1234.\nThis reverts commit def5678.",
                merged_at=NOW,
            ),
        },
        diffs={
            9: file_diff("a.py", 10, ["a1", "a2"], 10, [])
            + file_diff("c.py", 4, ["c1"], 4, [])
            + file_diff("z.py", 1, ["z"], 1, ["zz"]),
            5: file_diff("a.py", 9, [], 10, ["a1", "a2"]),
            7: file_diff("c.py", 3, [], 4, ["c1"]),
        },
        prs_for_commit={"abc1234": [5], "def5678": [7]},
    )
    bugs = await process_merged_pull_request(
        provider, await provider.get_pr_info("x/9"), config=_mira_config(feed_learning=False)
    )
    assert sorted((b.original_pr_number, b.path) for b in bugs) == [(5, "a.py"), (7, "c.py")]


async def test_original_with_unknown_merge_time_is_not_recorded() -> None:
    _reviewed(6, findings=[("b.py", 80)])
    provider = _hotfix_provider(blame=True)
    del provider.merged[6]  # the original cannot be loaded, so no merge time
    assert (
        await process_merged_pull_request(
            provider, await provider.get_pr_info("x/20"), config=_mira_config()
        )
        == []
    )


async def test_blame_error_falls_back_to_diff_overlap() -> None:
    _reviewed(6, paths=["b.py"], findings=[("b.py", 31)])
    provider = _hotfix_provider(blame=True)
    provider.get_blame = AsyncMock(side_effect=RuntimeError("502"))  # type: ignore[method-assign]
    [bug] = await process_merged_pull_request(
        provider, await provider.get_pr_info("x/20"), config=_mira_config()
    )
    assert bug.link_method == LINK_DIFF_OVERLAP and bug.original_pr_number == 6


async def test_one_unreadable_pushed_commit_does_not_drop_the_push() -> None:
    _reviewed(5, findings=[("a.py", 40)])
    provider = _revert_provider()
    message = 'Revert "Add cache"\n\nThis reverts commit abc1234.'
    for sha in ("r0", "r1"):
        provider.commits[sha] = CommitInfo(sha=sha, message=message, parents=["p0"])
        provider.commit_diffs[sha] = file_diff("a.py", 10, ["c1", "c2"], 10, [])
    original = provider.get_prs_for_commit

    async def flaky(pr_info: PRInfo, sha: str) -> list[int]:
        if sha == "r0":
            raise RuntimeError("provider hiccup")
        return await original(pr_info, sha)

    provider.get_prs_for_commit = flaky  # type: ignore[method-assign]
    payload = {"commits": [{"id": "r0", "message": message}, {"id": "r1", "message": message}]}
    [bug] = await process_pushed_commits(
        provider, "acme", "app", push_commits(payload), config=_mira_config(feed_learning=False)
    )
    assert bug.fix_ref == "commit:r1"


def test_reviewed_prs_touching_reads_past_the_newest_500_reviews() -> None:
    with open_quality_store("acme", "app") as store:
        index = store.index_store
        common = {"pr_url": "", "comments_posted": 0, "blockers": 0, "warnings": 0}
        index.record_review(
            pr_number=1,
            pr_title="old",
            reviewed_paths=json.dumps(["target.py"]),
            created_at=NOW - 20 * DAY,
            **common,
        )
        for n in range(2, 603):
            index.record_review(
                pr_number=n,
                pr_title="busy",
                reviewed_paths=json.dumps(["other.py"]),
                created_at=NOW - DAY + n,
                **common,
            )
        assert store.reviewed_prs_touching("target.py", since=NOW - 30 * DAY) == [1]
        assert store.reviewed_prs_touching("target.py", since=NOW - 10 * DAY) == []


def test_backtest_result_rows_carry_the_run_repository() -> None:
    from mira.quality.models import PRBacktest

    with open_quality_store("acme", "app") as store:
        store.index_store._owner = ""  # a handle not pinned to a repository
        store.index_store._repo = ""
        store.save_backtest_result(
            "run-1", PRBacktest(variant="A", pr_number=3), owner="acme", repo="app"
        )
        rows = store._query("SELECT owner, repo FROM quality_backtest_results")
    assert [tuple(r) for r in rows] == [("acme", "app")]


class _NoDDLStore:
    """A handle that cannot create tables; ``tables_exist`` says whether they are there."""

    _owner = "acme"
    _repo = "app"

    def __init__(self, *, tables_exist: bool) -> None:
        self.tables_exist = tables_exist

    def _gate_query(self, sql: str, params: tuple = ()) -> list[tuple]:
        if not self.tables_exist:
            raise RuntimeError("no such table")
        return []

    def _gate_exec(self, sql: str, params: tuple = ()) -> int:
        if sql.lstrip().upper().startswith("CREATE"):
            raise RuntimeError("read-only database")
        return 1

    def _gate_scope(self) -> tuple[str, tuple]:
        return "", ()


def _bug() -> EscapedBug:
    return EscapedBug(
        id="x",
        platform="github",
        owner="acme",
        repo="app",
        kind=KIND_HOTFIX,
        fix_ref="pr:1",
        original_pr_number=2,
        path="a.py",
    )


def test_schema_failure_makes_writes_fail_loudly() -> None:
    broken = QualityStore(_NoDDLStore(tables_exist=False))
    assert "quality tables unavailable" in broken.unavailable_reason
    with pytest.raises(QualityStoreUnavailable):
        broken.record_escaped_bug(_bug())

    # A read-only handle over an existing schema stays usable.
    existing = QualityStore(_NoDDLStore(tables_exist=True))
    assert existing.unavailable_reason == ""
    assert existing.record_escaped_bug(_bug())


async def test_webhook_survives_an_unusable_quality_store() -> None:
    provider = _revert_provider()
    broken = QualityStore(_NoDDLStore(tables_exist=False))
    broken.was_reviewed = lambda _n: True  # type: ignore[method-assign]

    class _Ctx:
        def __enter__(self) -> Any:
            return broken

        def __exit__(self, *exc: Any) -> None:
            return None

    with patch("mira.quality.store.open_quality_store", lambda *a, **k: _Ctx()):
        bugs = await process_merged_pull_request(
            provider, await provider.get_pr_info("x/9"), config=_mira_config()
        )
    assert bugs == []
