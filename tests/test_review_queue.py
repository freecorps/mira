"""The durable review queue: limits, stacks, restarts, coalescing, carry-over."""

from __future__ import annotations

import asyncio
import random
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import BackgroundTasks

from mira.config import MiraConfig, ReviewConfig, ReviewQueueConfig, ReviewStatusConfig
from mira.core import review_queue
from mira.core.commit_status import CARRIED_OVER_NOTE, STATUS_CONTEXT, CommitStatus
from mira.core.patch_id import diff_patch_id
from mira.core.review_queue import ReviewQueue, RunResult, order_for_dispatch, stack_depths
from mira.dashboard.db import AppDatabase, PRReviewVerdict, ReviewRequest
from mira.models import PRInfo
from mira.platforms.github.webhook import dispatch_github_event
from mira.platforms.queued_review import execute_review_request

OWNER = "freecorps"
REPO = "groceryops"
BOT = "mira-bot"


@pytest.fixture
def db(tmp_path: Path) -> AppDatabase:
    return AppDatabase(f"sqlite:///{tmp_path / 'queue.db'}", admin_password="pw")


class Clock:
    def __init__(self) -> None:
        self.now = 1_000_000.0

    def __call__(self) -> float:
        return self.now


class FakeGitHub:
    """A review platform whose check runs live in a dict, like GitHub's.

    ``checks[(pr, sha)]`` is the latest ``(state, title)`` of `mira/review` on
    that commit. A run publishes "Reviewing…", waits for ``release`` (or
    ``delay``), then publishes its result — the engine's two publishes.
    """

    def __init__(
        self,
        checks: dict[tuple[int, str], tuple[str, str]] | None = None,
        *,
        delay: float = 0.01,
        block: bool = False,
        orphans: list[ReviewRequest] | None = None,
    ) -> None:
        self.checks = checks if checks is not None else {}
        self.delay = delay
        self.release = asyncio.Event()
        if not block:
            self.release.set()
        self.orphans = orphans
        self.running = 0
        self.max_running = 0
        self.started: list[tuple[int, str]] = []
        self.completed: list[tuple[int, str]] = []

    async def run(self, request: ReviewRequest) -> RunResult:
        self.running += 1
        self.max_running = max(self.max_running, self.running)
        self.started.append((request.pr_number, request.head_sha))
        self.checks[(request.pr_number, request.head_sha)] = ("pending", "Reviewing…")
        try:
            await self.release.wait()
            await asyncio.sleep(self.delay)
        finally:
            self.running -= 1
        self.checks[(request.pr_number, request.head_sha)] = ("success", "No findings")
        self.completed.append((request.pr_number, request.head_sha))
        return RunResult("reviewed", status_settled=True)

    async def publish(self, request: ReviewRequest, status: CommitStatus) -> bool:
        self.checks[(request.pr_number, request.head_sha)] = (status.state, status.title)
        return True

    async def find_orphans(self) -> list[ReviewRequest]:
        return list(self.orphans or [])


def _config(**queue: Any) -> MiraConfig:
    settings = {"max_concurrent_reviews": 2, "settle_seconds": 0.0, "reconcile_on_boot": True}
    settings.update(queue)
    return MiraConfig(review=ReviewConfig(queue=ReviewQueueConfig(**settings)))


def _queue(db: AppDatabase, platform: FakeGitHub, clock: Clock, **queue: Any) -> ReviewQueue:
    config = _config(**queue)
    return ReviewQueue(db, {"github": platform}, config=lambda: config, clock=clock)


def _request(number: int, head_sha: str, **fields: Any) -> ReviewRequest:
    values: dict[str, Any] = {
        "owner": OWNER,
        "repo": REPO,
        "installation_id": 1,
        "pr_url": f"https://github.com/{OWNER}/{REPO}/pull/{number}",
        "reason": "synchronize",
    }
    values.update(fields)
    return ReviewRequest(pr_number=number, head_sha=head_sha, **values)


async def _tick(queue: ReviewQueue) -> None:
    """One tick, and a turn of the loop for the reviews it started."""
    await queue.tick()
    for _ in range(3):
        await asyncio.sleep(0)


async def _drain(queue: ReviewQueue, db: AppDatabase, *, rounds: int = 2000) -> None:
    """Tick until nothing is queued, running or left to settle."""
    for _ in range(rounds):
        await queue.tick()
        await asyncio.sleep(0.002)
        unsettled = [
            r
            for r in db.list_review_requests(("superseded", "failed", "done"))
            if r.check_state in ("queued", "running")
        ]
        if not queue._tasks and not db.list_review_requests(("queued", "running")) and not unsettled:
            return
    raise AssertionError("the queue never drained")


def _pending(checks: dict[tuple[int, str], tuple[str, str]]) -> dict:
    return {key: value for key, value in checks.items() if value[0] in ("queued", "pending")}


# ─────────────────────────────────────────────── a restacked chain of 15 ──


def _stack_payload(number: int, index: int, sha: str) -> dict[str, Any]:
    return {
        "action": "synchronize",
        "installation": {"id": 1},
        "sender": {"login": "alice"},
        "pull_request": {
            "number": number,
            "title": f"Step {index}",
            "body": "",
            "labels": [],
            "user": {"login": "alice"},
            "head": {"ref": f"stack/{index}", "sha": sha},
            "base": {"ref": "main" if index == 0 else f"stack/{index - 1}"},
        },
        "repository": {"owner": {"login": OWNER}, "name": REPO, "private": True},
    }


async def test_a_restacked_chain_of_fifteen_runs_two_at_a_time_from_the_base_up(
    db: AppDatabase, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The 2026-10-06 incident, replayed: fifteen `synchronize` webhooks for
    #802 → #816, force-pushed in the same second and delivered in no
    particular order. At most two reviews run at once, they start from the
    base of the stack, and every pull request ends with a finished check."""
    clock = Clock()
    platform = FakeGitHub()
    queue = _queue(db, platform, clock, settle_seconds=5.0)
    monkeypatch.setattr(review_queue, "_queue", queue)

    app_auth = MagicMock()
    app_auth.get_bot_identity = AsyncMock(return_value=BOT)
    numbers = list(range(802, 817))
    deliveries = list(enumerate(numbers))
    random.Random(1006).shuffle(deliveries)
    with patch("mira.platforms.github.webhook.load_config", return_value=MiraConfig()):
        for index, number in deliveries:
            status = await dispatch_github_event(
                "pull_request",
                _stack_payload(number, index, f"new{number}"),
                app_auth,
                BOT,
                BackgroundTasks(),
            )
            assert status == "queued"
            clock.now += 0.2

    # Persisted before the webhooks were answered, and nothing runs yet: the
    # batch waits for the burst to go quiet. Every PR shows it is in line.
    assert len(db.list_review_requests(("queued",))) == 15
    await _tick(queue)
    assert platform.started == []
    assert {platform.checks[(n, f"new{n}")] [0] for n in numbers} == {"queued"}
    assert {r.batch_id for r in db.list_review_requests(("queued",))} == {
        min(r.id for r in db.list_review_requests(("queued",)))
    }

    clock.now += 60
    await _drain(queue, db)

    assert platform.max_running == 2
    assert [number for number, _sha in platform.started] == numbers
    assert {platform.checks[(n, f"new{n}")] for n in numbers} == {("success", "No findings")}
    assert _pending(platform.checks) == {}
    assert {r.state for r in db.list_review_requests(("done",))} == {"done"}
    assert len(db.list_review_requests(("done",))) == 15


async def test_the_per_installation_limit_holds_one_installation_back(db: AppDatabase) -> None:
    clock = Clock()
    platform = FakeGitHub(delay=0.02)
    queue = _queue(db, platform, clock, max_concurrent_reviews=3, max_concurrent_per_installation=1)
    for number in (1, 2, 3):
        queue.enqueue(_request(number, f"a{number}"))
    queue.enqueue(_request(4, "b4", installation_id=2, owner="other"))
    await _tick(queue)
    assert sorted(platform.started) == [(1, "a1"), (4, "b4")]
    await _drain(queue, db)
    assert len(platform.completed) == 4


def test_stack_depth_follows_base_to_head_whatever_the_arrival_order() -> None:
    rows = [
        ReviewRequest(OWNER, REPO, 3, base_ref="b", head_ref="c", id=1, batch_id=1),
        ReviewRequest(OWNER, REPO, 1, base_ref="main", head_ref="a", id=2, batch_id=1),
        ReviewRequest(OWNER, REPO, 2, base_ref="a", head_ref="b", id=3, batch_id=1),
        ReviewRequest(OWNER, "elsewhere", 9, base_ref="a", head_ref="z", id=4, batch_id=4),
    ]
    assert stack_depths(rows) == {1: 2, 2: 0, 3: 1, 4: 0}
    assert [r.pr_number for r in order_for_dispatch(rows[:3], rows)] == [1, 2, 3]


def test_a_branch_cycle_does_not_hang_the_ordering() -> None:
    rows = [
        ReviewRequest(OWNER, REPO, 1, base_ref="b", head_ref="a", id=1),
        ReviewRequest(OWNER, REPO, 2, base_ref="a", head_ref="b", id=2),
    ]
    assert set(stack_depths(rows)) == {1, 2}


# ──────────────────────────────────────────────────────── a restart ──


async def test_a_restart_in_the_middle_of_the_queue_loses_nothing(db: AppDatabase) -> None:
    """The process is killed with two reviews running and two waiting. The
    next one resumes all four, gives up on a review that already took the
    process down three times, picks up a check nobody had a row for, and
    leaves no `mira/review` check queued or in progress."""
    clock = Clock()
    checks: dict[tuple[int, str], tuple[str, str]] = {}
    before = FakeGitHub(checks, block=True)
    first = _queue(db, before, clock)
    for number in (10, 11, 12, 13):
        first.enqueue(_request(number, f"h{number}"))
    await _tick(first)
    assert len(before.started) == 2
    assert sorted(v[0] for v in checks.values()) == ["pending", "pending", "queued", "queued"]

    # A review that has already been interrupted as many times as it may be.
    crashing, _, _ = db.enqueue_review_request(_request(20, "h20"))
    db.update_review_request(crashing.id, state="running", attempts=3, check_state="running")
    checks[(20, "h20")] = ("pending", "Reviewing…")
    # A check left in progress by a process that predates the queue.
    checks[(30, "h30")] = ("pending", "Reviewing…")
    orphan = _request(30, "h30", reason="recovered", check_state="running")

    # SIGKILL: the tasks die with the process and nothing gets to clean up.
    for task in list(first._tasks.values()):
        task.cancel()
    await asyncio.gather(*list(first._tasks.values()), return_exceptions=True)
    assert len(db.list_review_requests(("running",))) == 3

    after = FakeGitHub(checks, orphans=[orphan])
    second = _queue(db, after, clock)
    await second.recover()
    assert second._orphan_task is not None
    await second._orphan_task
    await _drain(second, db)

    assert sorted(after.completed) == [(10, "h10"), (11, "h11"), (12, "h12"), (13, "h13"), (30, "h30")]
    assert checks[(20, "h20")] == ("neutral", "Mira could not finish this review")
    assert _pending(checks) == {}
    assert all(state != "failure" for state, _title in checks.values())


async def test_a_graceful_stop_hands_running_reviews_back_without_counting_them(
    db: AppDatabase,
) -> None:
    clock = Clock()
    platform = FakeGitHub(block=True)
    queue = _queue(db, platform, clock)
    queue.enqueue(_request(1, "a"))
    await _tick(queue)
    assert platform.started == [(1, "a")]
    await queue.stop()
    (row,) = db.list_review_requests(("queued",))
    assert row.attempts == 0
    assert row.check_state == "running"


async def test_a_review_whose_head_moved_during_the_restart_is_superseded(
    db: AppDatabase,
) -> None:
    """The head was rewritten while Mira was down and the webhook for it was
    lost: the old head's check is closed as superseded, and the new head is
    the one reviewed."""
    row, _, _ = db.enqueue_review_request(_request(5, "old"))
    db.update_review_request(row.id, state="running", attempts=2, check_state="running")
    row = db.get_review_request(row.id)
    provider = MagicMock()
    provider.get_pr_info = AsyncMock(return_value=_pr_info(5, "new"))
    provider.publish_review_status = AsyncMock(return_value="1")
    with patch("mira.platforms.handlers.run_pr_review", AsyncMock(return_value=True)) as review:
        result = await execute_review_request(provider, row, bot_name=BOT, db=db, config=MiraConfig())
    published = provider.publish_review_status.call_args.kwargs
    target = provider.publish_review_status.call_args.args[0]
    assert target.head_sha == "old"
    assert published["state"] == "neutral"
    assert published["title"] == "Review interrupted by a restart; superseded"
    review.assert_awaited_once()
    assert result.outcome == "reviewed"
    assert db.get_review_request(row.id).head_sha == "new"


# ───────────────────────────────────────────────────────── coalescing ──


async def test_two_pushes_before_the_review_starts_run_one_review_on_the_newer_head(
    db: AppDatabase,
) -> None:
    clock = Clock()
    platform = FakeGitHub()
    queue = _queue(db, platform, clock, settle_seconds=5.0)
    queue.enqueue(_request(7, "first"))
    await _tick(queue)
    assert platform.checks[(7, "first")] == ("queued", "Queued")

    queue.enqueue(_request(7, "second"))
    clock.now += 60
    await _drain(queue, db)

    assert platform.started == [(7, "second")]
    assert platform.checks[(7, "first")] == ("neutral", "Superseded by a newer push")
    assert platform.checks[(7, "second")] == ("success", "No findings")


async def test_a_push_during_a_review_cancels_it_and_reviews_the_newer_head(
    db: AppDatabase,
) -> None:
    clock = Clock()
    platform = FakeGitHub(block=True)
    queue = _queue(db, platform, clock)
    queue.enqueue(_request(7, "first"))
    await _tick(queue)
    assert platform.started == [(7, "first")]

    queue.enqueue(_request(7, "second"))
    platform.release.set()
    await _drain(queue, db)

    assert platform.completed == [(7, "second")]
    assert platform.checks[(7, "first")] == ("neutral", "Superseded by a newer push")
    assert platform.checks[(7, "second")] == ("success", "No findings")
    assert _pending(platform.checks) == {}


async def test_a_redelivered_webhook_is_the_same_request(db: AppDatabase) -> None:
    clock = Clock()
    queue = _queue(db, FakeGitHub(), clock, settle_seconds=5.0)
    first, created = queue.enqueue(_request(7, "same"))
    again, created_again = queue.enqueue(_request(7, "same", reason="opened"))
    assert created and not created_again
    assert again.id == first.id
    # A comment names no head: it asks for whatever is already in line.
    _, created_comment = queue.enqueue(_request(7, "", reason="command"))
    assert not created_comment
    # A full review asks for more than the queued one and replaces it.
    full, created_full = queue.enqueue(_request(7, "same", reason="command", full_review=True))
    assert created_full and full.full_review
    assert [r.id for r in db.list_review_requests(("queued",))] == [full.id]


# ─────────────────────────────────────────────── rebase without changes ──

_DIFF = """diff --git a/app/cart.py b/app/cart.py
index 1111111..2222222 100644
--- a/app/cart.py
+++ b/app/cart.py
@@ -10,6 +10,7 @@ class Cart:
     def total(self):
-        return sum(i.price for i in self.items)
+        return sum(i.price * i.qty for i in self.items)
+
     def clear(self):
"""

# The same change after a rebase: new blob ids, the hunk forty lines lower,
# and upstream edits in its context.
_REBASED = """diff --git a/app/cart.py b/app/cart.py
index 3333333..4444444 100644
--- a/app/cart.py
+++ b/app/cart.py
@@ -50,6 +50,7 @@ class Cart:
     def total(self) -> int:
-        return sum(i.price for i in self.items)
+        return sum(i.price * i.qty for i in self.items)
+
     def clear(self) -> None:
"""

_CHANGED = _REBASED.replace("i.price * i.qty", "i.price * i.quantity")


def _pr_info(number: int, head_sha: str) -> PRInfo:
    return PRInfo(
        title="t",
        description="",
        base_branch="main",
        head_branch="feature",
        url=f"https://github.com/{OWNER}/{REPO}/pull/{number}",
        number=number,
        owner=OWNER,
        repo=REPO,
        head_sha=head_sha,
    )


def test_patch_id_ignores_where_a_change_applies_but_not_what_it_does() -> None:
    assert diff_patch_id(_DIFF) == diff_patch_id(_REBASED)
    assert diff_patch_id(_DIFF) != diff_patch_id(_CHANGED)
    assert diff_patch_id("") == ""


def _carry_over_setup(db: AppDatabase, diff_now: str) -> tuple[MagicMock, ReviewRequest]:
    db.set_pr_review_verdict(
        PRReviewVerdict(
            owner=OWNER,
            repo=REPO,
            pr_number=7,
            head_sha="before",
            patch_id=diff_patch_id(_DIFF),
            state="failure",
            title="1 blocker",
            summary="Mira reviewed 1 file and posted 1 blocker.",
        )
    )
    row, _, _ = db.enqueue_review_request(_request(7, "after"))
    db.claim_review_request(row.id)
    row = db.get_review_request(row.id)
    provider = MagicMock()
    provider.get_pr_info = AsyncMock(return_value=_pr_info(7, "after"))
    provider.get_pr_diff = AsyncMock(return_value=diff_now)
    provider.publish_review_status = AsyncMock(return_value="1")
    return provider, row


async def test_a_rebase_that_changes_nothing_carries_the_verdict_over_without_the_model(
    db: AppDatabase,
) -> None:
    provider, row = _carry_over_setup(db, _REBASED)
    with (
        patch("mira.platforms.handlers.run_pr_review", AsyncMock()) as review,
        patch("mira.platforms.handlers.run_gate_evaluation", AsyncMock()) as gate,
        patch("mira.platforms.handlers.create_llm") as create_llm,
    ):
        result = await execute_review_request(provider, row, bot_name=BOT, db=db, config=MiraConfig())

    assert result == RunResult("carried_over", status_settled=True)
    review.assert_not_awaited()
    create_llm.assert_not_called()
    gate.assert_awaited_once()
    published = provider.publish_review_status.call_args.kwargs
    assert published["context"] == STATUS_CONTEXT
    assert published["state"] == "failure"
    assert published["title"] == "1 blocker (carried over)"
    assert published["summary"].startswith(CARRIED_OVER_NOTE)
    assert provider.publish_review_status.call_args.args[0].head_sha == "after"
    assert db.get_last_reviewed_sha(OWNER, REPO, 7) == "after"
    carried = db.get_pr_review_verdict(OWNER, REPO, 7)
    assert carried.head_sha == "after"
    assert carried.title == "1 blocker"


async def test_a_rebase_that_changes_the_diff_is_reviewed(db: AppDatabase) -> None:
    provider, row = _carry_over_setup(db, _CHANGED)
    with patch("mira.platforms.handlers.run_pr_review", AsyncMock(return_value=True)) as review:
        result = await execute_review_request(provider, row, bot_name=BOT, db=db, config=MiraConfig())
    review.assert_awaited_once()
    assert result.outcome == "reviewed"


async def test_an_explicit_request_is_never_carried_over(db: AppDatabase) -> None:
    provider, row = _carry_over_setup(db, _REBASED)
    row.reason = "command"
    with patch("mira.platforms.handlers.run_pr_review", AsyncMock(return_value=True)) as review:
        await execute_review_request(provider, row, bot_name=BOT, db=db, config=MiraConfig())
    review.assert_awaited_once()
    provider.get_pr_diff.assert_not_awaited()


async def test_the_engine_records_the_verdict_it_published(
    db: AppDatabase, monkeypatch: pytest.MonkeyPatch
) -> None:
    from mira.core.commit_status import CommitStatus as Status
    from mira.core.engine import ReviewEngine

    monkeypatch.setattr("mira.dashboard.api._app_db", db)
    engine = ReviewEngine(config=MiraConfig(), llm=MagicMock(), provider=MagicMock())
    engine._status.last_status = Status("success", "No findings", "Mira reviewed 1 file.")
    engine._record_verdict(_pr_info(7, "sha7"), _DIFF)
    verdict = db.get_pr_review_verdict(OWNER, REPO, 7)
    assert (verdict.head_sha, verdict.state, verdict.patch_id) == (
        "sha7",
        "success",
        diff_patch_id(_DIFF),
    )

    # An incremental round with nothing new decided nothing.
    engine.last_review_had_changes = False
    engine._status.last_status = Status("success", "No findings", "")
    engine._record_verdict(_pr_info(7, "sha8"), _DIFF)
    assert db.get_pr_review_verdict(OWNER, REPO, 7).head_sha == "sha7"


# ───────────────────────────────────────────── Mira's failures stay neutral ──


async def test_a_review_that_fails_before_it_starts_settles_its_check_neutral(
    db: AppDatabase,
) -> None:
    class Broken(FakeGitHub):
        async def run(self, request: ReviewRequest) -> RunResult:
            raise RuntimeError("installation token refused")

    clock = Clock()
    platform = Broken()
    queue = _queue(db, platform, clock, settle_seconds=5.0)
    queue.enqueue(_request(3, "c"))
    await _tick(queue)
    clock.now += 60
    await _drain(queue, db)
    assert platform.checks[(3, "c")] == ("neutral", "Mira could not finish this review")
    assert db.list_review_requests(("failed",))[0].check_state == "settled"


async def test_nothing_is_published_with_the_status_off(db: AppDatabase) -> None:
    clock = Clock()
    platform = FakeGitHub()
    config = MiraConfig(
        review=ReviewConfig(
            status=ReviewStatusConfig(enabled=False),
            queue=ReviewQueueConfig(settle_seconds=5.0),
        )
    )
    queue = ReviewQueue(db, {"github": platform}, config=lambda: config, clock=clock)
    queue.enqueue(_request(1, "a"))
    queue.enqueue(_request(1, "b"))
    await _tick(queue)
    assert platform.checks == {}


# ────────────────────────────────────────────────────── webhook entry points ──


def _app_auth() -> MagicMock:
    auth = MagicMock()
    auth.get_bot_identity = AsyncMock(return_value=BOT)
    return auth


async def test_the_re_run_button_on_mira_review_queues_a_review(
    db: AppDatabase, monkeypatch: pytest.MonkeyPatch
) -> None:
    queue = _queue(db, FakeGitHub(), Clock())
    monkeypatch.setattr(review_queue, "_queue", queue)
    payload = {
        "action": "rerequested",
        "installation": {"id": 1},
        "sender": {"login": "alice"},
        "check_run": {
            "name": STATUS_CONTEXT,
            "head_sha": "abc",
            "pull_requests": [
                {"number": 9, "head": {"ref": "f", "sha": "abc"}, "base": {"ref": "main"}}
            ],
        },
        "repository": {"owner": {"login": OWNER}, "name": REPO},
    }
    with patch("mira.platforms.github.webhook.load_config", return_value=MiraConfig()):
        status = await dispatch_github_event("check_run", payload, _app_auth(), BOT, BackgroundTasks())
        other = dict(payload, check_run=dict(payload["check_run"], name="ci/tests"))
        ignored = await dispatch_github_event("check_run", other, _app_auth(), BOT, BackgroundTasks())
    assert (status, ignored) == ("queued", "ignored")
    (row,) = db.list_review_requests(("queued",))
    assert (row.pr_number, row.head_sha, row.reason) == (9, "abc", "rerun")


@pytest.mark.parametrize(("command", "full"), [("review", False), ("full review", True)])
async def test_a_review_comment_goes_through_the_queue(
    db: AppDatabase, monkeypatch: pytest.MonkeyPatch, command: str, full: bool
) -> None:
    queue = _queue(db, FakeGitHub(), Clock())
    monkeypatch.setattr(review_queue, "_queue", queue)
    payload = {
        "action": "created",
        "installation": {"id": 1},
        "comment": {"body": f"@{BOT} {command}", "user": {"login": "alice"}},
        "issue": {"number": 7, "title": "t", "pull_request": {"url": "x"}},
        "repository": {"owner": {"login": OWNER}, "name": REPO},
    }
    with patch("mira.platforms.github.webhook.load_config", return_value=MiraConfig()):
        status = await dispatch_github_event(
            "issue_comment", payload, _app_auth(), BOT, BackgroundTasks()
        )
    assert status == "queued"
    (row,) = db.list_review_requests(("queued",))
    assert (row.pr_number, row.reason, row.full_review, row.actor) == (7, "command", full, "alice")


async def test_without_a_queue_the_webhook_reviews_the_old_way(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(review_queue, "_queue", None)
    tasks = BackgroundTasks()
    with patch("mira.platforms.github.webhook.load_config", return_value=MiraConfig()):
        status = await dispatch_github_event(
            "pull_request", _stack_payload(1, 0, "s"), _app_auth(), BOT, tasks
        )
    assert status == "processing"
    assert any(t.func.__name__ == "handle_pull_request" for t in tasks.tasks)


async def test_review_health_reports_the_queue_and_memory(db: AppDatabase, monkeypatch) -> None:
    from httpx import ASGITransport, AsyncClient

    from mira.platforms.github.auth import GitHubAppAuth
    from mira.platforms.server import create_app

    queue = _queue(db, FakeGitHub(), Clock(), settle_seconds=5.0)
    queue.enqueue(_request(1, "a"))
    monkeypatch.setattr(review_queue, "_queue", queue)
    app = create_app(app_auth=GitHubAppAuth(app_id="1", private_key="k"), webhook_secret="s")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
        body = (await client.get("/health/reviews")).json()
    assert body["reviews_queued"] == 1
    assert body["reviews_running"] == 0
    assert body["max_concurrent_reviews"] == 2
    assert body["rss_bytes"] > 0
    assert "snapshot_bytes_held" in body


# ─────────────────────────────────────────────────────────── the store ──


def test_interrupted_requests_are_queued_again_until_they_run_out_of_attempts(
    db: AppDatabase,
) -> None:
    fresh, _, _ = db.enqueue_review_request(_request(1, "a"))
    worn, _, _ = db.enqueue_review_request(_request(2, "b"))
    db.claim_review_request(fresh.id)
    db.update_review_request(worn.id, state="running", attempts=3)
    requeued, exhausted = db.requeue_interrupted_review_requests(max_attempts=3)
    assert [r.id for r in requeued] == [fresh.id]
    assert [r.id for r in exhausted] == [worn.id]
    assert db.get_review_request(fresh.id).state == "queued"
    assert db.get_review_request(worn.id).outcome == "interrupted"


def test_a_burst_shares_a_batch_and_waits_for_the_last_request(db: AppDatabase) -> None:
    a, _, _ = db.enqueue_review_request(_request(1, "a"), settle_seconds=5, now=100.0)
    b, _, _ = db.enqueue_review_request(_request(2, "b"), settle_seconds=5, now=103.0)
    assert a.batch_id == b.batch_id == a.id
    assert db.get_review_request(a.id).available_at == 108.0
    # Never held longer than the cap from its own arrival.
    db.enqueue_review_request(_request(3, "c"), settle_seconds=5, max_settle_seconds=10, now=109.0)
    assert db.get_review_request(a.id).available_at == 110.0


async def test_a_cancelled_review_releases_its_slot_for_the_next_one() -> None:
    """A superseded review is cancelled. Had the in-memory "reviewing" slot
    stayed taken, the newer head's review would be skipped as already
    running — exactly the review the cancellation was for."""
    from mira.core.review_status import tracker
    from mira.platforms.handlers import run_pr_review

    engine = MagicMock()
    engine.review_pr = AsyncMock(side_effect=asyncio.CancelledError())
    engine.report_review_failure = AsyncMock()
    with (
        patch("mira.platforms.handlers.ReviewEngine", return_value=engine),
        patch("mira.platforms.handlers.create_llm"),
        patch("mira.platforms.handlers.load_config", return_value=MiraConfig()),
        patch("mira.dashboard.api._app_db"),
        pytest.raises(asyncio.CancelledError),
    ):
        await run_pr_review(AsyncMock(), "acme", "slot", 41, "u", False, "mira")
    engine.report_review_failure.assert_not_awaited()
    assert not any(j.repo == "acme/slot" and j.pr_number == 41 for j in tracker.get_active())
