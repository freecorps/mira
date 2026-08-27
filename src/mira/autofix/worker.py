"""The thing that actually runs the queue.

One loop: take a lease, run the job, release. It runs inside the web process by
default, because the deployment this project targets is one container on a
small board and a second process to supervise is a second thing to go wrong. A
larger install sets ``autofix.inline_worker: false`` and runs
``mira autofix-worker`` next to the web process — the loop is the same code
either way, and the only difference is which process owns it.

Correctness under crash comes from the lease, not from the loop. A worker that
is killed mid-job leaves a row in `running` with a deadline that then passes,
and the next poll takes it back. Nothing has to notice the crash, because the
deadline expiring *is* noticing it — and no cleanup handler has to run, which
is what makes `SIGKILL` and a power cut behave the same as a clean stop.

Where the queue lives depends on the backend. Postgres keeps one table for the
install, so one worker serves every repository. SQLite keeps a file per
repository, so the loop walks the registered repositories and polls each — the
same walk the Phase 3 analytics and the Phase 4 history already do, reused
rather than re-implemented.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import socket
import time
import uuid
from typing import Any

from mira.autofix.models import AutofixJob, Reason, ReasonCode
from mira.autofix.policy import resolve_policy
from mira.autofix.service import run_job
from mira.config import MiraConfig, load_config

logger = logging.getLogger(__name__)

# How long a worker sleeps when it found nothing. Short enough that a fix
# requested on a pull request feels prompt, long enough that an idle install on
# a small board is not spinning on the database.
_IDLE_SLEEP_FLOOR = 1.0

# Repositories visited per poll on the SQLite backend. A cap rather than a
# page: the loop comes back around, and an install with 400 repositories should
# not open 400 database files before running the first job it found.
_REPO_SCAN_LIMIT = 200

# How often the worker asks CI what it thought of the fixes it published.
# Minutes rather than seconds: a build takes minutes, and asking faster only
# spends API quota discovering that it is still running.
_CI_SWEEP_SECONDS = 180.0


def worker_id() -> str:
    """A name that identifies this process in a lease, and stays put.

    Host and pid so an operator can find the process, plus a random suffix so
    two workers on one host with recycled pids cannot claim each other's
    leases.
    """
    return f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:8]}"


def _postgres() -> bool:
    from mira.feedback.analytics import _postgres_url

    return bool(_postgres_url())


def _targets() -> list[tuple[str, str, str]]:
    """``(platform, owner, repo)`` for every queue this worker could poll."""
    if _postgres():
        # One table for the install; the org-wide handle sees all of it.
        return [("", "", "")]
    from mira.feedback.analytics import _repo_targets

    try:
        return list(_repo_targets("", ""))
    except Exception as exc:  # noqa: BLE001 - an unreachable registry is a quiet poll
        logger.debug("Autofix worker could not list repositories: %s", exc)
        return []


def _open(platform: str, owner: str, repo: str) -> Any:
    from mira.index.store import IndexStore

    return IndexStore.open(owner, repo, platform=platform or "github")


class AutofixWorker:
    """Polls for leasable jobs and runs them, one at a time.

    One at a time deliberately. Concurrency here would multiply model spend and
    platform writes on a machine that has one core, and the ceiling that
    matters — how many fixes are in flight for a repository — is enforced when
    the request is accepted, where it can be explained to the person who asked.
    """

    def __init__(
        self,
        *,
        provider_factory: Any,
        config: MiraConfig | None = None,
        identity: str = "",
    ) -> None:
        # A callable rather than a provider: a token is per-installation and
        # short-lived, so the worker asks for one per job instead of holding
        # one that will have expired by the time a job arrives.
        self._provider_factory = provider_factory
        self._config = config
        self._identity = identity or worker_id()
        self._stopped = asyncio.Event()
        # Keyed by target, not one clock for the worker. A single timestamp
        # would let the first repository in the list consume the interval and
        # leave every later one unswept — and the list order is stable, so the
        # same repositories would lose every time.
        self._last_ci_sweep: dict[str, float] = {}
        # Where the next SQLite scan starts. See `_scan_window`.
        self._scan_offset = 0
        self.jobs_run = 0

    @property
    def identity(self) -> str:
        return self._identity

    def stop(self) -> None:
        self._stopped.set()

    async def run_forever(self) -> None:
        """Poll until stopped. Never raises out of the loop.

        A crash inside one job must not take the queue down: the next poll has
        to happen, or a single malformed row becomes an outage.
        """
        logger.info("Autofix worker %s started", self._identity)
        while not self._stopped.is_set():
            config = self._config or load_config()
            policy = resolve_policy(config.autofix)
            try:
                ran = await self.poll_once(config=config)
            except Exception as exc:  # noqa: BLE001
                logger.exception("Autofix worker poll failed: %s", exc)
                ran = False
            delay = 0.0 if ran else max(_IDLE_SLEEP_FLOOR, policy.worker_poll_seconds)
            if delay:
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(self._stopped.wait(), timeout=delay)
        logger.info("Autofix worker %s stopped", self._identity)

    def _scan_window(self) -> list[tuple[str, str, str]]:
        """The repositories to visit this poll, rotating across polls.

        A fixed prefix would be a starvation bug rather than a cap: an install
        with more than `_REPO_SCAN_LIMIT` repositories would poll the same first
        slice forever and never claim a job in any of the others. Advancing the
        offset each poll means every repository comes round, and the cap only
        decides how long that takes.
        """
        every = _targets()
        if len(every) <= _REPO_SCAN_LIMIT:
            self._scan_offset = 0
            return every
        start = self._scan_offset % len(every)
        window = (every + every)[start : start + _REPO_SCAN_LIMIT]
        self._scan_offset = (start + _REPO_SCAN_LIMIT) % len(every)
        return window

    async def poll_once(self, *, config: MiraConfig | None = None) -> bool:
        """Run at most one job. Returns whether it found one."""
        config = config or self._config or load_config()
        policy = resolve_policy(config.autofix)
        if config.autofix.kill_switch:
            # The kill switch stops work from *running*, not just from being
            # requested. An operator who flips it during an incident means "no
            # more writes", and a queue that drains itself afterwards would
            # make that switch a suggestion.
            return False
        for platform, owner, repo in self._scan_window():
            store = None
            try:
                store = _open(platform, owner, repo)
                store.reap_expired_autofix_leases()
                job = store.claim_autofix_job(
                    worker=self._identity, lease_seconds=policy.lease_seconds
                )
                if job is None:
                    await self._maybe_sweep_ci(store, config, target=f"{platform}:{owner}/{repo}")
                    continue
                await self._run(job, store=store, config=config)
                self.jobs_run += 1
                return True
            except Exception as exc:  # noqa: BLE001 - one bad repo is not the queue
                logger.warning("Autofix worker failed on %s/%s: %s", owner, repo, exc)
            finally:
                if store is not None:
                    with contextlib.suppress(Exception):
                        store.close()
        return False

    async def _maybe_sweep_ci(self, store: Any, config: MiraConfig, *, target: str) -> None:
        """Ask CI about one queue's published fixes, at most every few minutes.

        Runs only when there was no job to claim there, so a busy queue never
        delays real work to go looking for something to retry. The interval is
        deliberately much longer than the poll interval: a build takes minutes,
        and asking faster only spends API quota discovering it is still running.

        The interval is tracked *per queue*. One clock for the worker would let
        whichever repository is polled first spend the whole interval, and the
        poll order is stable — so the same repositories would never be swept.
        """
        now = time.time()
        if now - self._last_ci_sweep.get(target, 0.0) < _CI_SWEEP_SECONDS:
            return
        self._last_ci_sweep[target] = now
        from mira.autofix.ci import sweep

        try:
            requeued = await sweep(
                provider_factory=self._provider_factory, store=store, config=config
            )
        except Exception as exc:  # noqa: BLE001 - the sweep is opportunistic
            logger.debug("Autofix CI sweep failed: %s", exc)
            return
        if requeued:
            logger.info("Autofix re-queued %d fix(es) after a red CI run", requeued)

    async def _run(self, job: AutofixJob, *, store: Any, config: MiraConfig) -> None:
        """Run one leased job, keeping the lease alive while it runs."""
        policy = resolve_policy(config.autofix, job.owner, job.repo)
        try:
            provider = await self._provider(job)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Autofix could not authenticate for %s: %s", job.pr_url, exc)
            # Hand the job back rather than consuming an attempt: a token that
            # could not be minted is an infrastructure problem, and burning the
            # retry budget on it would dead-letter work that is perfectly fine.
            store.release_autofix_lease(job.job_key, worker=self._identity)
            return

        running = asyncio.create_task(run_job(provider, job, config=config, store=store))
        heartbeat = asyncio.create_task(self._heartbeat(job, store, policy.lease_seconds, running))
        try:
            await running
        except asyncio.CancelledError:
            # The heartbeat stopped it: an admin cancelled, or another worker
            # took the lease. Either way this process no longer owns the job,
            # and `run_job` re-reads the row before it writes anything, so the
            # worst case is a model call nobody wanted rather than a change.
            logger.info("Autofix job %s was stopped mid-flight", job.job_key)
        finally:
            heartbeat.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await heartbeat

    async def _heartbeat(
        self, job: AutofixJob, store: Any, lease_seconds: float, running: asyncio.Task
    ) -> None:
        """Extend the lease while the job is genuinely still running.

        Renewed at a third of the lease, so two missed renewals still leave the
        job leased. A worker that hangs stops renewing and the job is taken
        back; a worker that is merely slow keeps it.

        Losing the lease **stops the work**. Cancellation clears the lease, and
        a heartbeat that merely returned would leave the job running to
        completion — which is how a cancelled job used to be able to open a
        pull request anyway.
        """
        interval = max(1.0, lease_seconds / 3)
        while True:
            await asyncio.sleep(interval)
            try:
                if not store.renew_autofix_lease(
                    job.job_key, worker=self._identity, lease_seconds=lease_seconds
                ):
                    logger.info(
                        "Autofix worker %s lost the lease on %s; stopping the job",
                        self._identity,
                        job.job_key,
                    )
                    running.cancel()
                    return
            except Exception as exc:  # noqa: BLE001 - a failed renewal is not fatal
                logger.debug("Lease renewal failed for %s: %s", job.job_key, exc)
                return

    async def _provider(self, job: AutofixJob) -> Any:
        result = self._provider_factory(job)
        if asyncio.iscoroutine(result):
            return await result
        return result


def cancel_job(
    *, owner: str, repo: str, platform: str, job_key: str, actor: str, reason: str
) -> AutofixJob | None:
    """Stop a job by hand. Administrative, and always available.

    Cancellation reaches a running job the same way a crash does: the state
    changes and the lease is cleared, so the worker's next lease renewal fails
    and it stops. It does not reach through to the platform — a job that has
    already opened a pull request stays `opened`, because closing somebody's
    pull request is not what "cancel" means.
    """
    store = _open(platform, owner, repo)
    try:
        return store.cancel_autofix_job(job_key, actor=actor, reason=reason)
    finally:
        store.close()


async def retry_after_ci(
    job: AutofixJob,
    *,
    ci_summary: str,
    config: MiraConfig | None = None,
) -> AutofixJob:
    """Re-queue a published fix whose own CI went red, up to the limit.

    Bounded conservatively and bounded *durably*: the count lives on the job
    row, so a restart does not reset it and a redelivered webhook does not
    advance it twice. When the budget is spent the job stays where it is, with
    a reason saying so — an unbounded loop between a model and a CI run is the
    failure mode this limit exists for, and it is expensive in exactly the way
    nobody notices until the invoice.
    """
    config = config or load_config()
    policy = resolve_policy(config.autofix, job.owner, job.repo)
    store = _open(job.platform, job.owner, job.repo)
    try:
        if job.ci_attempts >= job.max_ci_attempts:
            return (
                store.update_autofix_job(
                    job.job_key,
                    reasons=[
                        *job.reasons,
                        Reason(
                            ReasonCode.CI_RETRY_LIMIT,
                            f"CI rejected this fix and the retry budget "
                            f"({job.max_ci_attempts}) is spent; a human should look",
                        ),
                    ],
                )
                or job
            )
        from mira.autofix.models import AutofixAttempt

        store.record_autofix_attempt(
            AutofixAttempt(
                job_id=job.id,
                job_key=job.job_key,
                attempt=job.attempts,
                phase="ci_retry",
                outcome="requeued",
                diff=job.diff,
                detail=ci_summary[:4_000],
            )
        )
        return (
            store.update_autofix_job(
                job.job_key,
                state="queued",
                bump_ci_attempts=True,
                # A CI retry is a new piece of work, not another go at the one
                # that already succeeded. Without this the job would sit in
                # `queued` forever: the claim query refuses a row whose attempts
                # have run out, and a published fix has spent at least one.
                extra_attempts=1,
                available_at=time.time() + policy.retry_backoff_seconds,
                clear_lease=True,
                reasons=[
                    Reason(
                        ReasonCode.CI_RETRY,
                        f"CI failed on the fix; regenerating "
                        f"(attempt {job.ci_attempts + 1} of {job.max_ci_attempts})",
                        "info",
                    )
                ],
            )
            or job
        )
    finally:
        store.close()
