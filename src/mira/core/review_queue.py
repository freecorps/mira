"""The durable review queue: every automatic review goes through here.

Before this, a webhook handed its review straight to FastAPI's
``BackgroundTasks``. That has three properties a burst of pushes finds at once:
no limit on how many reviews run together, nothing that survives the process,
and nothing that settles a check the process died in the middle of. A restacked
chain of fifteen pull requests delivered fifteen `synchronize` events in one
second, fifteen reviews started, the process was killed, the webhooks that
arrived during the restart were lost, and the checks of the reviews it was
running stayed "Reviewing…" for good.

So a review request is now a row, written before the webhook is answered (see
``AppDatabase.enqueue_review_request``), and one worker per process runs the
rows:

* **At most N at a time**, globally and per installation. The rest wait with a
  `Queued` check, so a pull request still says Mira has seen it.
* **Coalesced per pull request.** A newer push supersedes the older head's
  request — cancelling its review if one is running — and the older head's
  check is closed as neutral, "superseded".
* **Ordered from the base of a stack up.** Requests for one repository that
  arrive together are one batch; the batch waits for the burst to go quiet and
  then runs in stack order, where a pull request whose base branch is another
  queued request's head comes after it.
* **Recovered on boot.** A `running` row belongs to a process that is gone: it
  is queued again (and given up on, with a neutral check, after
  ``max_attempts``). Rows whose check was left queued or in progress are
  settled, and a platform that can look (GitHub) is asked for `mira/review`
  checks left in progress that no row knows about.

The queue knows nothing about any platform: each one supplies a
:class:`ReviewPlatform` that runs a request and publishes a status for it.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections import Counter
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any, Protocol

from mira.config import MiraConfig, load_config
from mira.core.commit_status import (
    NEUTRAL,
    CommitStatus,
    interrupted_status,
    queued_status,
    superseded_status,
)
from mira.dashboard.db import REVIEW_REQUEST_ACTIVE, ReviewRequest

logger = logging.getLogger(__name__)

# How long a request waits after the review it would have run was found
# already running in this process (a `review-rest`, say) before trying again.
_BUSY_RETRY_SECONDS = 30.0
# Publishing a settling status is retried on later ticks this many times, with
# a growing delay, before the row is let go — a token without `checks:write`
# would otherwise be asked again every second for ever.
_SETTLE_ATTEMPTS = 5
_CONFIG_TTL_SECONDS = 15.0
_ANNOUNCE_CONCURRENCY = 4
_ANNOUNCE_RETRY_SECONDS = 60.0
_PRUNE_EVERY_SECONDS = 3600.0


@dataclass
class RunResult:
    """What running a request came to.

    ``outcome`` is ``reviewed``, ``carried_over``, ``failed``, or ``busy`` (the
    pull request was already being reviewed in this process; try later).
    ``status_settled`` says whether the request's `mira/review` check reached a
    terminal state — when it did not, the queue settles it.
    """

    outcome: str
    status_settled: bool = True


class ReviewPlatform(Protocol):
    async def run(self, request: ReviewRequest) -> RunResult: ...

    async def publish(self, request: ReviewRequest, status: CommitStatus) -> bool: ...


def _installation_key(request: ReviewRequest) -> tuple[Any, ...]:
    if request.installation_id:
        return (request.platform, request.installation_id)
    return request.repo_key


def stack_depths(rows: Iterable[ReviewRequest]) -> dict[int, int]:
    """How far up its stack each request sits: 0 at the base.

    A request's parent is the request in the same repository whose head branch
    is its base branch. Only the rows given are considered, so a chain whose
    middle was not pushed splits into two stacks — which is still base-first
    within each.
    """
    rows = list(rows)
    by_head = {(r.repo_key, r.head_ref): r for r in rows if r.head_ref}
    depths: dict[int, int] = {}

    def depth(row: ReviewRequest) -> int:
        seen: set[int] = set()
        count = 0
        current = row
        while True:
            if current.id in depths:
                count += depths[current.id]
                break
            seen.add(current.id)
            parent = by_head.get((current.repo_key, current.base_ref)) if current.base_ref else None
            if parent is None or parent.id in seen or parent.pr_number == current.pr_number:
                break
            count += 1
            current = parent
        return count

    for row in rows:
        depths[row.id] = depth(row)
    return depths


def order_for_dispatch(
    eligible: Iterable[ReviewRequest], active: Iterable[ReviewRequest]
) -> list[ReviewRequest]:
    """The order eligible requests start in.

    Batches first-come first-served, and within a batch the stack from its base
    up, then arrival order. ``active`` is every queued and running request, so a
    running parent still counts towards its children's depth.
    """
    active = list(active)
    depths = stack_depths(active)
    batch_start: dict[int, float] = {}
    for row in active:
        key = row.batch_id or row.id
        batch_start[key] = min(batch_start.get(key, row.created_at), row.created_at)
    return sorted(
        eligible,
        key=lambda r: (
            batch_start.get(r.batch_id or r.id, r.created_at),
            r.repo_key,
            depths.get(r.id, 0),
            r.created_at,
            r.id,
        ),
    )


def _could_not_finish(row: ReviewRequest) -> CommitStatus:
    return CommitStatus(
        state=NEUTRAL,
        title="Mira could not finish this review",
        summary=(
            "The review stopped before it finished.\n\n"
            "This says nothing about the pull request — it is a Mira failure. "
            "Push again, comment `review`, or press Re-run to retry."
        ),
    )


class ReviewQueue:
    """Runs persisted review requests, a bounded number at a time.

    ``db`` is the application database; ``platforms`` maps a platform name to
    its :class:`ReviewPlatform`. Requests for any other platform are refused by
    :meth:`accepts`, and their webhook layers keep reviewing directly.
    """

    def __init__(
        self,
        db: Any,
        platforms: dict[str, ReviewPlatform],
        *,
        config: Callable[[], MiraConfig] | None = None,
        clock: Callable[[], float] = time.time,
        poll_interval: float = 1.0,
    ) -> None:
        self._db = db
        self._platforms = dict(platforms)
        self._config_loader = config or load_config
        self._config: MiraConfig | None = None
        self._config_at = 0.0
        self._clock = clock
        self._poll_interval = poll_interval
        self._tasks: dict[int, asyncio.Task[None]] = {}
        self._running: dict[int, ReviewRequest] = {}
        self._superseding: set[int] = set()
        self._settle_failures: dict[int, tuple[int, float]] = {}
        # Requests whose `Queued` status failed to publish, and when to try again.
        self._announce_failed: dict[int, float] = {}
        self._wake = asyncio.Event()
        self._stopped = False
        self._last_prune = 0.0
        self._orphan_task: asyncio.Task[int] | None = None

    # ── Submitting ──

    def accepts(self, platform: str) -> bool:
        return platform in self._platforms

    def config(self) -> MiraConfig:
        now = time.monotonic()
        if self._config is None or now - self._config_at > _CONFIG_TTL_SECONDS:
            self._config = self._config_loader()
            self._config_at = now
        return self._config

    def enqueue(self, request: ReviewRequest) -> tuple[ReviewRequest, bool]:
        """Persist a request and coalesce it. Synchronous: it is the durable step.

        Returns the row and whether it is new. A running review this request
        supersedes is cancelled here; its check is settled by the worker once
        the cancellation has landed, so the two cannot race on the same check.
        """
        q = self.config().review.queue
        row, superseded, created = self._db.enqueue_review_request(
            request,
            settle_seconds=q.settle_seconds,
            max_settle_seconds=max(q.settle_seconds * 6, 30.0),
            now=self._clock(),
        )
        for old in superseded:
            logger.info(
                "Review of %s/%s#%s at %s superseded by %s",
                old.owner,
                old.repo,
                old.pr_number,
                old.head_sha[:12] or "?",
                row.head_sha[:12] or "a newer request",
            )
            task = self._tasks.get(old.id)
            if task is not None and not task.done():
                self._superseding.add(old.id)
                task.cancel()
        if created:
            logger.info(
                "Review of %s/%s#%s queued (%s, head %s)",
                row.owner,
                row.repo,
                row.pr_number,
                row.reason or "request",
                row.head_sha[:12] or "current",
            )
        self.wake()
        return row, created

    def wake(self) -> None:
        self._wake.set()

    # ── Running ──

    async def run_forever(self) -> None:
        try:
            await self.recover()
        except Exception:  # noqa: BLE001 - the queue must run even if recovery could not
            logger.exception("Review queue recovery failed; running the queue without it")
        while not self._stopped:
            self._wake.clear()
            try:
                await self.tick()
            except Exception:  # noqa: BLE001 - one bad tick must not stop the queue
                logger.exception("Review queue tick failed")
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._wake.wait(), timeout=self._poll_interval)

    async def recover(self) -> None:
        """Pick up where the last process left off. Called once, before the first tick."""
        q = self.config().review.queue
        requeued, exhausted = self._db.requeue_interrupted_review_requests(
            max_attempts=q.max_attempts, now=self._clock()
        )
        waiting = self._db.list_review_requests(("queued",))
        if requeued or exhausted or waiting:
            logger.info(
                "Review queue recovered: %d interrupted review(s) queued again, %d given up "
                "on, %d waiting",
                len(requeued),
                len(exhausted),
                len(waiting) - len(requeued),
            )
        for row in exhausted:
            logger.warning(
                "Review of %s/%s#%s was interrupted %d time(s); not starting it again",
                row.owner,
                row.repo,
                row.pr_number,
                row.attempts,
            )
        if q.reconcile_on_boot and any(
            callable(getattr(p, "find_orphans", None)) for p in self._platforms.values()
        ):
            self._orphan_task = asyncio.create_task(self.reconcile_orphans())

    async def reconcile_orphans(self) -> int:
        """Queue a review for every `mira/review` check left in progress by nobody.

        The rows cover what this version of Mira started; this covers the rest
        — checks from before the queue existed, or from a database that was
        lost. Returns how many were queued.
        """
        queued = 0
        for name, platform in self._platforms.items():
            finder = getattr(platform, "find_orphans", None)
            if not callable(finder):
                continue
            try:
                orphans = await finder()
            except Exception as exc:  # noqa: BLE001 - a scan that fails costs only the scan
                logger.warning("Could not look for unfinished %s review checks: %s", name, exc)
                continue
            known = {r.pr_key for r in self._db.list_review_requests(REVIEW_REQUEST_ACTIVE)}
            for request in orphans:
                if request.pr_key in known:
                    continue
                _row, created = self.enqueue(request)
                if created:
                    known.add(request.pr_key)
                    queued += 1
        if queued:
            logger.info("Queued %d review(s) for checks left in progress", queued)
        return queued

    async def tick(self) -> None:
        """One pass: settle what finished, start what may start, announce the rest."""
        config = self.config()
        q = config.review.queue
        now = self._clock()
        await self._settle(config, now)
        active = self._db.list_review_requests(REVIEW_REQUEST_ACTIVE)
        queued = [r for r in active if r.state == "queued"]
        started = self._dispatch(
            queued, active, now, q.max_concurrent_reviews, q.max_concurrent_per_installation
        )
        await self._announce([r for r in queued if r.id not in started], active, config)
        if now - self._last_prune > _PRUNE_EVERY_SECONDS:
            self._last_prune = now
            with contextlib.suppress(Exception):
                self._db.prune_review_requests()

    def _dispatch(
        self,
        queued: list[ReviewRequest],
        active: list[ReviewRequest],
        now: float,
        limit: int,
        per_installation: int,
    ) -> set[int]:
        started: set[int] = set()
        if len(self._tasks) >= limit:
            return started
        running_prs = {r.pr_key for r in self._running.values()}
        per_install = Counter(_installation_key(r) for r in self._running.values())
        eligible = [
            r
            for r in queued
            if r.available_at <= now and r.pr_key not in running_prs and self.accepts(r.platform)
        ]
        for row in order_for_dispatch(eligible, active):
            if len(self._tasks) >= limit:
                break
            key = _installation_key(row)
            if per_installation and per_install[key] >= per_installation:
                continue
            if row.pr_key in running_prs or not self._db.claim_review_request(row.id, now=now):
                continue
            row.state = "running"
            row.attempts += 1
            row.check_state = "running"
            self._running[row.id] = row
            self._tasks[row.id] = asyncio.create_task(self._run(row))
            running_prs.add(row.pr_key)
            per_install[key] += 1
            started.add(row.id)
        return started

    async def _run(self, request: ReviewRequest) -> None:
        platform = self._platforms[request.platform]
        logger.info(
            "Review of %s/%s#%s started (%d running)",
            request.owner,
            request.repo,
            request.pr_number,
            len(self._tasks),
        )
        try:
            try:
                result = await platform.run(request)
            except asyncio.CancelledError:
                if request.id in self._superseding:
                    # The row is already `superseded`; its check, left in
                    # progress by the review that was just stopped, is settled
                    # on the next tick.
                    return
                raise
            except Exception:  # noqa: BLE001 - recorded on the row, settled below
                logger.exception(
                    "Queued review of %s/%s#%s failed",
                    request.owner,
                    request.repo,
                    request.pr_number,
                )
                result = RunResult("failed", status_settled=False)
            if result.outcome == "busy":
                self._db.update_review_request(
                    request.id,
                    expected_state="running",
                    state="queued",
                    attempts=max(request.attempts - 1, 0),
                    available_at=self._clock() + _BUSY_RETRY_SECONDS,
                )
                return
            self._db.update_review_request(
                request.id,
                expected_state="running",
                state="failed" if result.outcome == "failed" else "done",
                outcome=result.outcome,
                check_state="settled" if result.status_settled else request.check_state,
            )
        finally:
            self._tasks.pop(request.id, None)
            self._running.pop(request.id, None)
            self._superseding.discard(request.id)
            self.wake()

    # ── Statuses ──

    def _status_enabled(self, config: MiraConfig) -> bool:
        return bool(config.review.status.enabled)

    async def _announce(
        self, waiting: list[ReviewRequest], active: list[ReviewRequest], config: MiraConfig
    ) -> None:
        """Publish `Queued` on every waiting request that has no check yet."""
        if not self._status_enabled(config) or not config.review.status.pending:
            return
        order = order_for_dispatch([r for r in active if r.state == "queued"], active)
        position = {r.id: i for i, r in enumerate(order)}
        running = len(self._tasks)
        # A restack announces a dozen pull requests at once; a few in flight
        # keeps that to seconds without bursting the platform's rate limit.
        gate = asyncio.Semaphore(_ANNOUNCE_CONCURRENCY)
        now = self._clock()

        async def _one(row: ReviewRequest) -> None:
            async with gate:
                ok = await self._publish(row, queued_status(running + position.get(row.id, 0)))
            if not ok:
                self._announce_failed[row.id] = now + _ANNOUNCE_RETRY_SECONDS
                return
            self._announce_failed.pop(row.id, None)
            # Recorded whatever the row's state is now: it may have been
            # superseded while the status was in flight, and then this is the
            # check the settle pass has to close.
            self._db.update_review_request(row.id, check_state="queued")

        await asyncio.gather(
            *(
                _one(row)
                for row in waiting
                if not row.check_state and self._announce_failed.get(row.id, 0.0) <= now
            )
        )

    async def _settle(self, config: MiraConfig, now: float) -> None:
        """Close every check a finished request left queued or in progress."""
        rows = [
            r
            for r in self._db.list_review_requests(("superseded", "failed", "done"))
            if r.check_state in ("queued", "running") and r.id not in self._tasks
        ]
        for row in rows:
            failures, retry_at = self._settle_failures.get(row.id, (0, 0.0))
            if retry_at > now:
                continue
            ok = True
            if self._status_enabled(config):
                ok = await self._publish(row, self._settling_status(row))
            if not ok:
                failures += 1
                if failures < _SETTLE_ATTEMPTS:
                    self._settle_failures[row.id] = (failures, now + 2.0**failures)
                    continue
                logger.warning(
                    "Giving up settling the review check of %s/%s#%s at %s",
                    row.owner,
                    row.repo,
                    row.pr_number,
                    row.head_sha[:12],
                )
            self._settle_failures.pop(row.id, None)
            self._db.update_review_request(row.id, check_state="settled")

    def _settling_status(self, row: ReviewRequest) -> CommitStatus:
        if row.state == "superseded":
            return superseded_status()
        if row.state == "failed" and row.outcome == "interrupted":
            return interrupted_status(row.attempts)
        if row.state == "done":
            # The review finished but its own terminal state did not reach the
            # platform. Its verdict was recorded; republish that.
            verdict = None
            with contextlib.suppress(Exception):
                verdict = self._db.get_pr_review_verdict(
                    row.owner, row.repo, row.pr_number, platform=row.platform
                )
            if verdict is not None and verdict.head_sha == row.head_sha and verdict.state:
                return CommitStatus(verdict.state, verdict.title, verdict.summary)
        return _could_not_finish(row)

    async def _publish(self, row: ReviewRequest, status: CommitStatus) -> bool:
        platform = self._platforms.get(row.platform)
        if platform is None:
            return False
        try:
            return bool(await platform.publish(row, status))
        except Exception as exc:  # noqa: BLE001 - a status is an announcement
            logger.warning(
                "Could not publish %r on %s/%s#%s: %s",
                status.title,
                row.owner,
                row.repo,
                row.pr_number,
                exc,
            )
            return False

    # ── Introspection and shutdown ──

    def metrics(self) -> dict[str, Any]:
        q = self.config().review.queue
        try:
            queued = len(self._db.list_review_requests(("queued",)))
        except Exception:  # noqa: BLE001
            queued = -1
        return {
            "reviews_running": len(self._tasks),
            "reviews_queued": queued,
            "max_concurrent_reviews": q.max_concurrent_reviews,
        }

    async def idle(self) -> None:
        """Wait until no review task is running. For tests and shutdown."""
        while self._tasks:
            await asyncio.gather(*list(self._tasks.values()), return_exceptions=True)

    async def stop(self) -> None:
        """Stop the reviews this process is running and hand them back to the queue.

        A deploy is not a crash: the rows go back to `queued` without the start
        counting towards ``max_attempts``, and the next process picks them up.
        """
        self._stopped = True
        self.wake()
        if self._orphan_task is not None and not self._orphan_task.done():
            self._orphan_task.cancel()
        running = list(self._running.values())
        for task in list(self._tasks.values()):
            task.cancel()
        with contextlib.suppress(Exception):
            await asyncio.gather(*list(self._tasks.values()), return_exceptions=True)
        for row in running:
            with contextlib.suppress(Exception):
                self._db.update_review_request(
                    row.id,
                    expected_state="running",
                    state="queued",
                    attempts=max(row.attempts - 1, 0),
                    available_at=self._clock(),
                )


# ── The process-wide instance the server lifespan owns ──

_queue: ReviewQueue | None = None
_task: asyncio.Task[None] | None = None


def current() -> ReviewQueue | None:
    return _queue


def start(db: Any, platforms: dict[str, ReviewPlatform]) -> ReviewQueue | None:
    """Start the worker for the platforms given. None when there are none."""
    global _queue, _task
    if not platforms:
        return None
    if _queue is not None:
        return _queue
    _queue = ReviewQueue(db, platforms)
    _task = asyncio.create_task(_queue.run_forever())
    _task.add_done_callback(
        lambda done: (
            logger.error("Review queue stopped: %s", done.exception())
            if not done.cancelled() and done.exception()
            else None
        )
    )
    return _queue


async def stop() -> None:
    global _queue, _task
    queue, task = _queue, _task
    _queue = None
    _task = None
    if queue is not None:
        await queue.stop()
    if task is not None and not task.done():
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await task


def submit(request: ReviewRequest) -> ReviewRequest | None:
    """Queue a review if this process runs a queue for its platform.

    None means "not queued": no queue is running (the CLI, a test app without
    a lifespan), the platform has no runner, or the database refused the
    write. The caller then reviews the old way rather than dropping the event.
    """
    queue = _queue
    if queue is None or not queue.accepts(request.platform):
        return None
    try:
        row, _created = queue.enqueue(request)
    except Exception:  # noqa: BLE001 - falling back beats losing the event
        logger.exception(
            "Could not queue the review of %s/%s#%s",
            request.owner,
            request.repo,
            request.pr_number,
        )
        return None
    return row


def metrics() -> dict[str, Any]:
    queue = _queue
    if queue is None:
        return {"reviews_running": 0, "reviews_queued": 0, "queue": "not running"}
    return queue.metrics()
