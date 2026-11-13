"""Peer recall: collecting other review bots' threads, reporting, and positive learning."""

from __future__ import annotations

import dataclasses
import json
import time
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from click.testing import CliRunner
from fastapi import HTTPException

from mira.config import MiraConfig
from mira.feedback.evaluation import origin_for_rule
from mira.feedback.models import LearningCandidate, ReviewFinding
from mira.feedback.retrieval import (
    AUTO_ACTOR,
    POSITIVE_SIGNAL,
    is_positive_rule,
    render_rule,
    retrieve_rules,
    split_rules,
)
from mira.models import PeerReviewSnapshot, PeerReviewThread, PeerThreadComment, PRInfo
from mira.providers.github import GitHubProvider
from mira.quality import history, peer_runtime
from mira.quality.config_models import PeerRecallConfig
from mira.quality.models import (
    ADDRESSED_OUTDATED,
    ADDRESSED_REPLY,
    ADDRESSED_RESOLVED,
    LEARN_ACTIVE,
    LEARN_COLLECTING,
    LEARN_SKIPPED,
)
from mira.quality.peer_learning import (
    enforce_positive_budget,
    learn_from_misses,
    normalize_pattern,
)
from mira.quality.peers import (
    DISMISSED,
    NOT_MERGED,
    UNVERIFIED,
    assess_addressed,
    collect_repository,
    peer_category,
    peer_title,
)
from mira.quality.store import QualityStore, open_quality_store
from tests.quality_support import DAY, FakeHistoryProvider, file_diff

NOW = time.time()

CODERABBIT_BODY = """_⚠️ Potential issue_ | _🟠 Major_

**Retry path never resets `status`, leaving the job stuck in RUNNING.**

When `attempt()` raises, the except branch returns early...

<!-- fingerprinting:phantom:triton -->
"""

CURSOR_BODY = """### Bug: Cache not invalidated after config reload

<!-- **High Severity** -->

The cache keeps serving the old value after `reload()` is called.
"""


# ─────────────────────────────────────────────────────────────── helpers ──


def _finding(pr: int, path: str, line: int) -> ReviewFinding:
    fid = f"f-{pr}-{path}-{line}"
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


def _reviewed(pr: int, findings: list[tuple[str, int]] = ()) -> None:
    with open_quality_store("acme", "app") as store:
        store.index_store.record_review(
            pr_number=pr,
            pr_title=f"PR {pr}",
            pr_url="",
            comments_posted=len(findings),
            blockers=0,
            warnings=len(findings),
            reviewed_paths="[]",
            created_at=NOW - 2 * DAY,
        )
        for path, line in findings:
            store.index_store.save_review_finding(_finding(pr, path, line))


def _thread(
    tid: str,
    *,
    author: str = "cursor",
    path: str = "app/jobs.py",
    line: int = 40,
    body: str = CURSOR_BODY,
    resolved: bool = False,
    outdated: bool = False,
    replies: list[tuple[str, str]] = (),
    commit: str = "c1",
) -> PeerReviewThread:
    return PeerReviewThread(
        thread_id=tid,
        author=author,
        path=path,
        line=0 if outdated else line,
        original_line=line,
        body=body,
        url=f"https://github.com/acme/app/pull/1#discussion_{tid}",
        is_resolved=resolved,
        is_outdated=outdated,
        original_commit=commit,
        created_at=NOW - 3 * DAY,
        replies=[PeerThreadComment(author=a, body=b) for a, b in replies],
    )


def _snapshot(number: int, threads: list[PeerReviewThread], state: str = "merged"):
    return PeerReviewSnapshot(
        number=number,
        state=state,
        url=f"https://github.com/acme/app/pull/{number}",
        head_sha=f"head{number}",
        merged_at=NOW - DAY if state == "merged" else 0.0,
        closed_at=NOW - DAY,
        viewer_login="freecorps-review[bot]",
        threads=threads,
    )


class PeerProvider(FakeHistoryProvider):
    def __init__(self, snapshots: dict[int, PeerReviewSnapshot], compare: dict | None = None):
        super().__init__()
        self.snapshots = snapshots
        self.compare = compare or {}
        self.compares: list[tuple[str, str]] = []

    async def get_peer_review_threads(self, pr_info: PRInfo, logins: list[str]) -> Any:
        self.reads.append(f"peer:{pr_info.number}")
        snapshot = self.snapshots.get(pr_info.number)
        if snapshot is None:
            return None
        # As GitHub's does: only threads one of ``logins`` opened.
        wanted = {login.removesuffix("[bot]") for login in logins}
        return dataclasses.replace(
            snapshot, threads=[t for t in snapshot.threads if t.author in wanted]
        )

    async def get_compare_diff(self, pr_info: PRInfo, base: str, head: str) -> str:
        self.compares.append((base, head))
        return self.compare.get((base, head), "")


def _config(**peer: Any) -> MiraConfig:
    config = MiraConfig()
    config.peer_recall = PeerRecallConfig(**peer)
    return config


def _llm(*payloads: dict) -> AsyncMock:
    llm = AsyncMock()
    llm.complete_with_tools = AsyncMock(side_effect=[json.dumps(p) for p in payloads])
    return llm


_PATTERN = {
    "verdict": "pattern",
    "pattern": "Look for status fields set on success but never reset when a retry exits early.",
    "same_as": 0,
    "category": "bug",
    "scope_type": "repo",
    "confidence": 0.85,
    "reason": "Recurring lifecycle bug.",
}


# ─────────────────────────────────────────────────────────── reading text ──


def test_titles_skip_badges_and_prefer_the_bold_line() -> None:
    assert peer_title(CODERABBIT_BODY) == (
        "Retry path never resets `status`, leaving the job stuck in RUNNING."
    )
    assert peer_title(CURSOR_BODY) == "Cache not invalidated after config reload"
    assert peer_title("Possible None dereference of user here") == (
        "Possible None dereference of user here"
    )


def test_categories_come_from_the_title_before_the_body() -> None:
    assert peer_category("Retry path never resets status", "") == "lifecycle"
    assert peer_category("Cache not invalidated after reload", "") == "lifecycle"
    assert peer_category("SQL injection in search", "") == "security"
    assert peer_category("Race between two workers", "") == "concurrency"
    # A None in a code sample in the body does not decide it when the title can.
    assert peer_category("Off-by-one in pagination", "x = None") == "logic"
    assert peer_category("Rename for clarity", "") == "other"


# ─────────────────────────────────────────────────────────── addressing ──


def test_addressed_rule_is_conservative() -> None:
    merged = _snapshot(1, [])
    closed = _snapshot(1, [], state="closed")

    # Unmerged: nothing shipped, so nothing was addressed.
    assert assess_addressed(_thread("t", resolved=True), closed, region_changed=True) == (
        False,
        NOT_MERGED,
    )
    # A reply claiming the fix counts, unless it is Mira's own.
    fixed = _thread("t", replies=[("dana", "Fixed in abc1234")])
    assert assess_addressed(fixed, merged, region_changed=None) == (True, ADDRESSED_REPLY)
    mine = _thread("t", replies=[("freecorps-review[bot]", "fixed")])
    assert assess_addressed(mine, merged, region_changed=None) == (False, "")
    # Resolved or outdated count only when the code near the line changed.
    assert assess_addressed(_thread("t", resolved=True), merged, region_changed=True) == (
        True,
        ADDRESSED_RESOLVED,
    )
    assert assess_addressed(_thread("t", resolved=True), merged, region_changed=False) == (
        False,
        "",
    )
    assert assess_addressed(_thread("t", outdated=True), merged, region_changed=True) == (
        True,
        ADDRESSED_OUTDATED,
    )
    assert assess_addressed(_thread("t", resolved=True), merged, region_changed=None) == (
        False,
        UNVERIFIED,
    )
    # Open and untouched: not addressed.
    assert assess_addressed(_thread("t"), merged, region_changed=True) == (False, "")
    # Any dismissal vetoes, even next to a fix claim and a changed region.
    dismissed = _thread(
        "t", resolved=True, replies=[("dana", "fixed the typo; the rest is intentional")]
    )
    assert assess_addressed(dismissed, merged, region_changed=True) == (False, DISMISSED)
    assert assess_addressed(
        _thread("t", resolved=True, replies=[("dana", "Isso é falso positivo")]),
        merged,
        region_changed=True,
    ) == (False, DISMISSED)


# ────────────────────────────────────────────────────────────── collect ──


def _change_near(line: int, path: str = "app/jobs.py") -> str:
    return file_diff(path, line - 1, ["old"], line - 1, ["new"])


async def test_collection_records_overlap_and_addressed_idempotently() -> None:
    _reviewed(1, findings=[("app/jobs.py", 50)])  # within 15 lines of the thread at 40
    _reviewed(2)
    _reviewed(3)
    provider = PeerProvider(
        {
            1: _snapshot(1, [_thread("a", resolved=True)]),
            2: _snapshot(
                2,
                [
                    _thread("b", author="coderabbitai", body=CODERABBIT_BODY, outdated=True),
                    _thread("c", author="someone-else", resolved=True),
                    _thread("d", author="codeant-ai", path="app/x.py", resolved=True),
                ],
            ),
            3: _snapshot(3, [_thread("e")], state="open"),
        },
        compare={
            ("c1", "head1"): _change_near(40),
            ("c1", "head2"): _change_near(41),
        },
    )
    settings = PeerRecallConfig()
    with open_quality_store("acme", "app") as store:
        result = await collect_repository(provider, "acme", "app", store, settings, now=NOW)
        assert result.prs_examined == 2 and result.prs_skipped_open == 1
        assert result.threads == 3  # "someone-else" is not a configured peer bot
        rows = {row.thread_id: row for row in store.list_peer_threads(limit=50)}
    assert set(rows) == {"a", "b", "d"}
    a, b, d = rows["a"], rows["b"], rows["d"]
    assert a.bot == "cursor[bot]" and a.mira_flagged and a.addressed
    assert a.finding_ids == ["f-1-app/jobs.py-50"]
    assert b.bot == "coderabbitai[bot]" and not b.mira_flagged
    assert (b.addressed, b.addressed_reason) == (True, ADDRESSED_OUTDATED)
    assert b.missed_and_addressed and b.category == "lifecycle"
    assert b.line == 40 and b.original_commit == "c1" and b.head_sha == "head2"
    # app/x.py did not change after the comment: resolved alone is not addressed.
    assert not d.addressed and not d.mira_flagged_file
    # One compare per (commit, head), never one per thread.
    assert provider.compares.count(("c1", "head2")) == 1

    # A second run skips what it read after closing, and still waits on the open one.
    with open_quality_store("acme", "app") as store:
        again = await collect_repository(provider, "acme", "app", store, settings, now=NOW)
        assert again.prs_examined == 0 and again.prs_skipped_open == 1
        rescan = await collect_repository(
            provider, "acme", "app", store, settings, rescan=True, now=NOW
        )
        assert rescan.prs_examined == 2
        assert len(store.list_peer_threads(limit=50)) == 3


async def test_the_read_cap_skips_past_what_was_already_read() -> None:
    for number in (1, 2, 3):
        _reviewed(number)
    provider = PeerProvider({n: _snapshot(n, [_thread(f"t{n}")]) for n in (1, 2, 3)})
    capped = PeerRecallConfig(max_prs_per_run=2)
    with open_quality_store("acme", "app") as store:
        first = await collect_repository(provider, "acme", "app", store, capped, now=NOW)
        second = await collect_repository(provider, "acme", "app", store, capped, now=NOW)
        assert (first.prs_examined, second.prs_examined) == (2, 1)
        assert len(store.list_peer_threads()) == 3


async def test_a_rescan_keeps_learning_state() -> None:
    _reviewed(2)
    provider = PeerProvider(
        {2: _snapshot(2, [_thread("b", outdated=True)])},
        compare={("c1", "head2"): _change_near(40)},
    )
    settings = PeerRecallConfig()
    with open_quality_store("acme", "app") as store:
        await collect_repository(provider, "acme", "app", store, settings, now=NOW)
        [row] = store.list_peer_threads()
        store.set_peer_learning(row.id, LEARN_SKIPPED)
        await collect_repository(provider, "acme", "app", store, settings, rescan=True, now=NOW)
        assert store.get_peer_thread(row.id).learning_state == LEARN_SKIPPED


async def test_other_platforms_and_providers_without_threads_are_skipped() -> None:
    _reviewed(1)
    settings = PeerRecallConfig()
    with open_quality_store("acme", "app") as store:
        gitlab = await collect_repository(
            PeerProvider({}), "acme", "app", store, settings, platform="gitlab"
        )
        assert gitlab.unsupported
        unsupported = await collect_repository(
            FakeHistoryProvider(), "acme", "app", store, settings
        )
        assert unsupported.unsupported and unsupported.prs_examined == 0


async def test_github_reads_peer_threads_across_pages() -> None:
    def _node(tid: str, login: str, replies: list[str] = ()) -> dict:
        first = {
            "author": {"login": login},
            "body": "**Bug here**",
            "url": f"u-{tid}",
            "path": "a.py",
            "line": None,
            "originalLine": 7,
            "createdAt": "2026-10-01T00:00:00Z",
            "originalCommit": {"oid": "sha1"},
        }
        rest = [{"author": {"login": "dana"}, "body": text} for text in replies]
        return {
            "id": tid,
            "isResolved": True,
            "isOutdated": True,
            "resolvedBy": {"login": "dana"},
            "comments": {"nodes": [first, *rest]},
        }

    pr = {
        "number": 5,
        "url": "pr-url",
        "title": "T",
        "state": "MERGED",
        "mergedAt": "2026-10-02T00:00:00Z",
        "closedAt": "2026-10-02T00:00:00Z",
        "headRefOid": "head",
    }
    pages = [
        {
            "viewer": {"login": "mira[bot]"},
            "repository": {
                "pullRequest": {
                    **pr,
                    "reviewThreads": {
                        "pageInfo": {"hasNextPage": True, "endCursor": "x"},
                        "nodes": [_node("t1", "cursor", ["done"]), _node("t2", "human")],
                    },
                }
            },
        },
        {
            "viewer": {"login": "mira[bot]"},
            "repository": {
                "pullRequest": {
                    **pr,
                    "reviewThreads": {
                        "pageInfo": {"hasNextPage": False, "endCursor": None},
                        "nodes": [_node("t3", "coderabbitai")],
                    },
                }
            },
        },
    ]
    provider = GitHubProvider("token")
    with patch.object(GitHubProvider, "_graphql_request", AsyncMock(side_effect=pages)) as call:
        snapshot = await provider.get_peer_review_threads(
            PRInfo(
                title="",
                description="",
                base_branch="",
                head_branch="",
                url="",
                number=5,
                owner="acme",
                repo="app",
            ),
            ["cursor[bot]", "coderabbitai[bot]"],
        )
    assert call.await_count == 2
    assert snapshot.state == "merged" and snapshot.head_sha == "head"
    assert snapshot.viewer_login == "mira[bot]" and snapshot.merged_at > 0
    assert [t.thread_id for t in snapshot.threads] == ["t1", "t3"]
    first = snapshot.threads[0]
    assert (first.line, first.original_line, first.original_commit) == (0, 7, "sha1")
    assert first.is_resolved and first.is_outdated and first.resolved_by == "dana"
    assert [r.body for r in first.replies] == ["done"]


# ──────────────────────────────────────────────────────────────── storage ──


def test_peer_rows_round_trip_on_postgres_too(monkeypatch: pytest.MonkeyPatch) -> None:
    from mira.index import pg_store
    from mira.index.pg_store import PgIndexStore
    from mira.quality.models import PeerThread
    from tests.test_gate_store import _FakeConn

    conn = _FakeConn()
    monkeypatch.setattr(pg_store, "_pg_conn", conn, raising=False)
    monkeypatch.setattr(pg_store, "_schema_initialized", True, raising=False)
    monkeypatch.setattr(pg_store, "_get_conn", lambda url, **_kwargs: conn)
    monkeypatch.setattr(pg_store, "_new_pg_conn", lambda url: conn)
    store = QualityStore(PgIndexStore("acme", "app", "postgresql://test"))
    assert not store.unavailable_reason
    row = PeerThread(
        id="r1",
        platform="github",
        owner="acme",
        repo="app",
        pr_number=4,
        bot="cursor[bot]",
        path="a.py",
        line=3,
        addressed=True,
        finding_ids=["x"],
        pr_closed_at=NOW,
    )
    store.upsert_peer_thread(row)
    row.title = "updated"
    row.learning_state = "should not overwrite"
    store.upsert_peer_thread(row)
    [back] = store.list_peer_threads(addressed=True)
    assert back.title == "updated" and back.learning_state == "" and back.finding_ids == ["x"]
    store.set_peer_learning("r1", LEARN_ACTIVE, 9)
    assert store.get_peer_thread("r1").learning_candidate_id == 9
    store.record_peer_scan("github", 4, pr_state="merged", threads=1)
    store.record_peer_scan("github", 4, pr_state="merged", threads=1)
    assert store.peer_scanned("github", 4) and not store.peer_scanned("github", 5)
    conn.close()


# ───────────────────────────────────────────────────────────── reporting ──


async def _collected_fixture() -> None:
    _reviewed(1, findings=[("app/jobs.py", 45)])
    _reviewed(2)
    provider = PeerProvider(
        {
            1: _snapshot(1, [_thread("a", outdated=True), _thread("x", author="coderabbitai")]),
            2: _snapshot(2, [_thread("b", outdated=True, body=CODERABBIT_BODY)]),
        },
        compare={("c1", "head1"): _change_near(40), ("c1", "head2"): _change_near(40)},
    )
    with open_quality_store("acme", "app") as store:
        await collect_repository(provider, "acme", "app", store, PeerRecallConfig(), now=NOW)


async def test_summary_counts_recall_over_addressed_threads() -> None:
    await _collected_fixture()
    summary = history.peer_recall_summary()
    totals = summary["totals"]
    assert totals["threads"] == 3 and totals["addressed"] == 2
    assert totals["addressed_flagged"] == 1 and totals["recall"] == 0.5
    assert totals["missed_addressed"] == 1
    cursor = next(b for b in summary["bots"] if b["bot"] == "cursor[bot]")
    assert cursor["threads"] == 2 and cursor["recall"] == 0.5
    assert summary["categories"][0]["missed_addressed"] == 1
    assert summary["weeks"] and summary["learning"] == {"pending": 1}
    [missed] = history.list_missed_peer_threads()
    assert missed["pr_number"] == 2 and missed["missed_and_addressed"]
    assert history.list_missed_peer_threads(bot="coderabbitai[bot]") == []


async def test_cli_report_prints_the_same_numbers() -> None:
    from mira.cli import main

    await _collected_fixture()
    result = CliRunner().invoke(main, ["peer-recall", "report"])
    assert result.exit_code == 0, result.output
    assert "Recall on addressed peer findings: 1 / 2 (50%)" in result.output
    assert "cursor[bot]" in result.output and "acme/app#2" in result.output
    as_json = CliRunner().invoke(main, ["peer-recall", "report", "--output", "json"])
    assert json.loads(as_json.output)["totals"]["recall"] == 0.5


def _admin(is_admin: bool = True) -> Any:
    user = SimpleNamespace(id=1, username="admin", is_admin=is_admin)
    return SimpleNamespace(state=SimpleNamespace(user=user))


async def test_dashboard_routes_serve_admins_only() -> None:
    from mira.dashboard.routers import quality as routes

    await _collected_fixture()
    summary = routes.peer_recall_summary(_admin())
    assert summary.totals["recall"] == 0.5 and summary.enabled
    page = routes.peer_recall_missed(_admin(), category="lifecycle")
    assert [t["pr_number"] for t in page.threads] == [2]
    with pytest.raises(HTTPException) as exc:
        routes.peer_recall_summary(_admin(is_admin=False))
    assert exc.value.status_code == 403
    with pytest.raises(HTTPException):
        routes.peer_recall_missed(_admin(), owner="../x")
    with pytest.raises(HTTPException) as exc:
        await routes.peer_recall_run(routes.PeerRecallRun(owner="acme"), _admin())
    assert exc.value.status_code == 400


# ─────────────────────────────────────────────────────── positive learning ──


async def _two_misses() -> None:
    """Two missed-and-addressed threads on two pull requests, and one more on the first."""
    _reviewed(1)
    _reviewed(2)
    provider = PeerProvider(
        {
            1: _snapshot(
                1,
                [
                    _thread("a", outdated=True),
                    _thread("a2", author="coderabbitai", outdated=True, body=CODERABBIT_BODY),
                ],
            ),
            2: _snapshot(2, [_thread("b", outdated=True, body=CODERABBIT_BODY)]),
        },
        compare={("c1", "head1"): _change_near(40), ("c1", "head2"): _change_near(40)},
    )
    with open_quality_store("acme", "app") as store:
        await collect_repository(provider, "acme", "app", store, PeerRecallConfig(), now=NOW)


async def test_two_independent_misses_activate_a_positive_rule() -> None:
    await _two_misses()
    provider = PeerProvider(
        {}, compare={("c1", "head1"): _change_near(40), ("c1", "head2"): _change_near(40)}
    )
    config = _config()
    # Threads come newest first: #2's, then #1's two. The second and third
    # name the first's candidate (id 1) as the same pattern.
    llm = _llm(_PATTERN, {**_PATTERN, "same_as": 1}, {**_PATTERN, "same_as": 1})
    with open_quality_store("acme", "app") as store:
        result = await learn_from_misses(provider, "acme", "app", store, llm, config)
        assert result.examined == 3 and len(result.activated) == 1
        rules = store.index_store.list_learned_rules("approved")
        [rule] = [r for r in rules if r.active]
        assert rule.source_signal == POSITIVE_SIGNAL and rule.created_by == AUTO_ACTOR
        assert rule.rule_text.startswith("Look for status fields")
        assert (rule.scope_type, rule.scope_value) == ("repo", "acme/app")
        [candidate] = store.index_store.list_learning_candidates()
        # Two threads on #1 (two bots, one bug) count as one pull request.
        assert candidate.evidence_count == 2
        assert len(json.loads(candidate.positive_examples_json)) == 3
        states = {row.thread_id: row.learning_state for row in store.list_peer_threads()}
        assert states == {"b": LEARN_COLLECTING, "a": LEARN_ACTIVE, "a2": LEARN_ACTIVE}
        # Nothing is learned twice from the same thread.
        again = await learn_from_misses(provider, "acme", "app", store, _llm(), config)
        assert again.examined == 0

    prompt = llm.complete_with_tools.await_args_list[0].kwargs["messages"][0]["content"]
    # The peer bot's text sits inside an untrusted block; the diff does too.
    opened = prompt.index("<<<MIRA-UNTRUSTED-COMMENT>>>")
    assert (
        opened
        < prompt.index("Retry path never resets")
        < prompt.index("<<<END-MIRA-UNTRUSTED-COMMENT>>>")
    )
    assert "<<<MIRA-UNTRUSTED-DIFF>>>" in prompt
    later = llm.complete_with_tools.await_args_list[1].kwargs["messages"][0]["content"]
    assert "[1] (bug) Look for status fields" in later


async def test_weak_or_negative_patterns_are_not_learned() -> None:
    await _two_misses()
    llm = _llm(
        {**_PATTERN, "confidence": 0.4},
        {**_PATTERN, "verdict": "not_generalizable", "pattern": ""},
        {**_PATTERN, "pattern": "Do not report missing resets."},
    )
    with open_quality_store("acme", "app") as store:
        result = await learn_from_misses(
            provider := PeerProvider({}), "acme", "app", store, llm, _config()
        )
        assert result.skipped == 3 and not result.activated
        assert store.index_store.list_learning_candidates() == []
    assert provider.writes == []


async def test_learning_is_gated_and_respects_auto_accept() -> None:
    await _two_misses()
    off = _config(positive_learning=False)
    with open_quality_store("acme", "app") as store:
        assert (
            await learn_from_misses(PeerProvider({}), "acme", "app", store, _llm(), off)
        ).examined == 0

    manual = _config()
    manual.learning.auto_accept = False
    llm = _llm(_PATTERN, {**_PATTERN, "same_as": 1}, {**_PATTERN, "same_as": 1})
    with open_quality_store("acme", "app") as store:
        result = await learn_from_misses(PeerProvider({}), "acme", "app", store, llm, manual)
        assert not result.activated
        [candidate] = store.index_store.list_learning_candidates()
        assert candidate.status == "pending"
        assert store.index_store.list_learned_rules("approved") == []


def test_patterns_read_as_instructions_to_look() -> None:
    assert normalize_pattern("caches not invalidated on reload") == (
        "Look for caches not invalidated on reload."
    )
    assert normalize_pattern("Check whether a retry resets state.") == (
        "Check whether a retry resets state."
    )
    assert normalize_pattern("Never flag retries") == ""
    assert normalize_pattern("") == ""


def test_positive_budget_disables_the_stalest() -> None:
    with open_quality_store("acme", "app") as store:
        index = store.index_store
        ids = []
        for n in range(3):
            stored, _ = index.upsert_learning_candidate(
                LearningCandidate(
                    id=0,
                    semantic_fingerprint=f"fp{n}",
                    rule_text=f"Look for thing {n}.",
                    rationale="r",
                    scope_type="repo",
                    scope_value="acme/app",
                    category="bug",
                    language="",
                    confidence=0.9,
                    status="collecting",
                    synthesizer_version="peer-recall-v1",
                    evidence_ids_json=json.dumps(["p1", "p2"]),
                )
            )
            from mira.feedback.lifecycle import approve_candidate

            ids.append(approve_candidate(index, stored.id, actor=AUTO_ACTOR, min_evidence=2).id)
        assert enforce_positive_budget(index, 2) == [ids[0]]
        assert enforce_positive_budget(index, 2) == []


def test_rewording_a_positive_rule_keeps_its_own_evidence_floor() -> None:
    from mira.feedback.lifecycle import version_rule

    rule = _rule(POSITIVE_SIGNAL, "Look for thing.")
    calls: list[dict] = []
    store = SimpleNamespace(
        get_learned_rule=lambda _id: rule,
        version_learned_rule_atomic=lambda rule_id, **kw: calls.append(kw) or rule,
    )
    # Two pull requests of evidence: rewording in place is fine…
    version_rule(
        store,
        1,
        rule_text="Look for thing, reworded.",
        category="bug",
        scope_type="repo",
        scope_value="acme/app",
    )
    assert calls and calls[0]["rule_text"] == "Look for thing, reworded."
    # …widening it to the organisation still needs the org minimum.
    with pytest.raises(ValueError):
        version_rule(
            store,
            1,
            rule_text="Look for thing.",
            category="bug",
            scope_type="org",
            scope_value="acme",
        )
    # A negative rule with the same evidence cannot be reworded at repo scope.
    negative = _rule("feedback_v2", "Do not flag x.")
    store.get_learned_rule = lambda _id: negative
    with pytest.raises(ValueError):
        version_rule(
            store,
            1,
            rule_text="Do not flag y.",
            category="bug",
            scope_type="repo",
            scope_value="acme/app",
        )


# ───────────────────────────────────────────── retrieval, prompt, analytics ──


def _rule(signal: str, text: str, created_by: str = AUTO_ACTOR) -> Any:
    return SimpleNamespace(
        id=1,
        rule_text=text,
        source_signal=signal,
        scope_type="repo",
        scope_value="acme/app",
        version=1,
        created_by=created_by,
        evidence_count=2,
        sample_count=2,
        updated_at=NOW,
        effective_from=NOW,
    )


def test_positive_rules_render_as_checks_not_preferences() -> None:
    from mira.llm.prompts.review import build_review_prompt

    positive = _rule(POSITIVE_SIGNAL, "Look for stale caches.")
    negative = _rule("feedback_v2", "Do not flag generated files.")
    preferences, checks = split_rules([positive, negative])
    assert preferences == [negative] and checks == [positive] and is_positive_rule(positive)
    messages = build_review_prompt(
        files=[],
        config=MiraConfig(),
        learned_rules=[render_rule(negative)],
        learned_checks=[render_rule(positive)],
    )
    text = "\n".join(m["content"] for m in messages)
    prefs = text.index("## Team Preferences")
    also = text.index("## Also Check For")
    assert text.index("Do not flag generated files.") < also
    assert also < text.index("Look for stale caches.")
    assert prefs < also
    # No positive rules, no section.
    plain = build_review_prompt(files=[], config=MiraConfig(), learned_rules=["x"])
    assert "Also Check For" not in "\n".join(m["content"] for m in plain)


def test_retrieval_expires_automatic_positive_rules_like_any_other() -> None:
    stale = _rule(POSITIVE_SIGNAL, "Look for stale caches.")
    stale.updated_at = NOW - 90 * DAY
    store = SimpleNamespace(_owner="acme", _repo="app", list_active_learned_rules=lambda: [stale])
    assert retrieve_rules(store, paths=["a.py"], languages=[], auto_rule_ttl_days=60) == []
    assert retrieve_rules(store, paths=["a.py"], languages=[], auto_rule_ttl_days=None) == [stale]


def test_automatic_rules_are_scored_as_learned_not_manual() -> None:
    assert origin_for_rule(_rule(POSITIVE_SIGNAL, "x")) == "learned"
    assert origin_for_rule(_rule("feedback_v2", "x")) == "learned"
    assert origin_for_rule(_rule("feedback_v2", "x", created_by="alice")) == "manual"
    assert origin_for_rule(_rule("manual", "x", created_by="")) == "manual"


def test_approving_an_escaped_bug_candidate_makes_a_positive_rule() -> None:
    from mira.feedback.lifecycle import approve_candidate

    with open_quality_store("acme", "app") as store:
        stored, _ = store.index_store.upsert_learning_candidate(
            LearningCandidate(
                id=0,
                semantic_fingerprint="esc",
                rule_text="Review changes to a.py with extra care.",
                rationale="r",
                scope_type="path",
                scope_value="a.py",
                category="bug",
                language="python",
                confidence=0.5,
                status="pending",
                synthesizer_version="escaped-bug-v1",
                evidence_ids_json="[1]",
            )
        )
        rule = approve_candidate(store.index_store, stored.id, actor="admin")
        assert rule.source_signal == POSITIVE_SIGNAL


# ───────────────────────────────────────────────────────────── scheduling ──


def test_boundaries_are_daily_or_weekly_at_the_hour() -> None:
    now = datetime(2026, 11, 13, 3, 0, tzinfo=UTC)  # a Friday, before 04:00
    assert peer_runtime.latest_boundary(now, "daily", 4) == datetime(2026, 11, 12, 4, tzinfo=UTC)
    assert peer_runtime.latest_boundary(now, "weekly", 4) == datetime(2026, 11, 9, 4, tzinfo=UTC)


class _Settings:
    def __init__(self) -> None:
        self.values: dict[str, str] = {}

    def get_setting(self, key: str) -> str | None:
        return self.values.get(key)

    def set_setting(self, key: str, value: str) -> None:
        self.values[key] = value

    def compare_and_set_setting(self, key: str, expected: str | None, value: str) -> bool:
        if self.values.get(key) != expected:
            return False
        self.values[key] = value
        return True

    def list_repos(self) -> list[Any]:
        return [
            SimpleNamespace(platform="github", owner="acme", repo="app"),
            SimpleNamespace(platform="gitlab", owner="acme", repo="gl"),
        ]


async def test_a_boundary_runs_once_and_a_failed_run_is_retried() -> None:
    _reviewed(2)
    db = _Settings()
    config = _config(positive_learning=False)
    provider = PeerProvider({2: _snapshot(2, [_thread("b", outdated=True)])})

    async def provider_for(platform: str, owner: str, repo: str) -> Any:
        return provider

    now = datetime(2026, 11, 13, 5, 0, tzinfo=UTC)
    report = await peer_runtime.tick(provider_for, config=config, db=db, now=now)
    assert [(r.owner, r.repo, r.prs_examined) for r in report.repos] == [("acme", "app", 1)]
    assert peer_runtime.last_run(db)["repos"][0]["threads"] == 1
    assert await peer_runtime.tick(provider_for, config=config, db=db, now=now) is None

    async def broken(platform: str, owner: str, repo: str) -> Any:
        raise RuntimeError("no credentials")

    later = datetime(2026, 11, 14, 5, 0, tzinfo=UTC)
    failed = await peer_runtime.tick(broken, config=config, db=db, now=later)
    assert failed.repos[0].error == "no credentials"
    # Handed back: the next tick tries the same boundary again.
    assert await peer_runtime.tick(provider_for, config=config, db=db, now=later) is not None

    disabled = _config(enabled=False)
    assert await peer_runtime.tick(provider_for, config=disabled, db=_Settings(), now=now) is None
