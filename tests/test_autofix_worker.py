"""Phase 5 — the worker loop, the CI retry, and the administrative controls.

These cover the parts that only exist because the work is durable: a worker
that dies, a worker that is told to stop, a CI run that rejects a published fix,
and an admin who cancels something mid-flight.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from mira.autofix import ci as autofix_ci
from mira.autofix import runtime as autofix_runtime
from mira.autofix import worker as worker_module
from mira.autofix.models import AutofixJob, ReasonCode, job_key
from mira.autofix.worker import _REPO_SCAN_LIMIT, AutofixWorker, cancel_job, retry_after_ci
from mira.config import AutofixConfig, MiraConfig
from mira.gate.models import CIState
from mira.index.store import IndexStore


@pytest.fixture(autouse=True)
def isolated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MIRA_INDEX_DIR", str(tmp_path))
    monkeypatch.delenv("DATABASE_URL", raising=False)
    IndexStore.open("acme", "app").close()
    # The worker walks the repository registry on SQLite; pin it to one repo so
    # these tests do not depend on what else the dashboard happens to know.
    monkeypatch.setattr(worker_module, "_targets", lambda: [("github", "acme", "app")])


def _config(**autofix: Any) -> MiraConfig:
    settings = {"mode": "on", "worker_poll_seconds": 0.05, "lease_seconds": 5}
    settings.update(autofix)
    return MiraConfig(autofix=AutofixConfig(**settings))


def _job(**overrides: Any) -> AutofixJob:
    base = {
        "job_key": job_key(
            platform="github",
            owner="acme",
            repo="app",
            pr_number=7,
            head_sha="sha1",
            finding_id=overrides.pop("finding_id", "f1"),
            mode="branch_pr",
        ),
        "owner": "acme",
        "repo": "app",
        "pr_number": 7,
        "pr_url": "https://github.com/acme/app/pull/7",
        "head_sha": "sha1",
        "finding_id": "f1",
        "requested_by": "alice",
        "max_attempts": 2,
        "max_ci_attempts": 1,
        "available_at": time.time() - 1,
    }
    base.update(overrides)
    return AutofixJob(**base)


def _enqueue(job: AutofixJob) -> None:
    store = IndexStore.open("acme", "app")
    try:
        store.enqueue_autofix_job(job)
    finally:
        store.close()


def _get(key: str) -> AutofixJob | None:
    store = IndexStore.open("acme", "app")
    try:
        return store.get_autofix_job(key)
    finally:
        store.close()


# ── the loop ─────────────────────────────────────────────────────────────────


async def test_a_poll_runs_one_job_and_reports_that_it_did(monkeypatch) -> None:
    _enqueue(_job())
    ran: list[str] = []

    async def fake_run(provider, job, **kwargs):  # noqa: ANN001
        ran.append(job.job_key)
        return SimpleNamespace(job=job)

    monkeypatch.setattr(worker_module, "run_job", fake_run)
    worker = AutofixWorker(provider_factory=lambda job: object())
    assert await worker.poll_once(config=_config()) is True
    assert len(ran) == 1
    assert await worker.poll_once(config=_config()) is False


async def test_the_kill_switch_stops_work_that_is_already_queued(monkeypatch) -> None:
    """A switch flipped during an incident must not be drained around."""
    _enqueue(_job())
    monkeypatch.setattr(
        worker_module, "run_job", lambda *a, **k: pytest.fail("should not have run")
    )
    worker = AutofixWorker(provider_factory=lambda job: object())
    assert await worker.poll_once(config=_config(kill_switch=True)) is False
    assert _get(_job().job_key).state == "queued"


async def test_a_job_whose_credentials_cannot_be_minted_keeps_its_attempts(monkeypatch) -> None:
    _enqueue(_job(max_attempts=2))

    def broken_factory(job):  # noqa: ANN001
        raise RuntimeError("no installation token")

    monkeypatch.setattr(
        worker_module, "run_job", lambda *a, **k: pytest.fail("should not have run")
    )
    worker = AutofixWorker(provider_factory=broken_factory)
    await worker.poll_once(config=_config())
    job = _get(_job().job_key)
    assert job.state == "queued"
    assert job.lease_owner == ""


async def test_a_crashing_job_does_not_take_the_loop_down(monkeypatch) -> None:
    _enqueue(_job())

    async def exploding(provider, job, **kwargs):  # noqa: ANN001
        raise RuntimeError("boom")

    monkeypatch.setattr(worker_module, "run_job", exploding)
    worker = AutofixWorker(provider_factory=lambda job: object())
    # `poll_once` swallows it; the loop is expected to come back around.
    assert await worker.poll_once(config=_config()) is False


async def test_run_forever_stops_when_asked(monkeypatch) -> None:
    monkeypatch.setattr(worker_module, "_targets", list)
    worker = AutofixWorker(provider_factory=lambda job: object(), config=_config())
    task = asyncio.create_task(worker.run_forever())
    await asyncio.sleep(0.1)
    worker.stop()
    await asyncio.wait_for(task, timeout=2)
    assert task.done()


def test_two_workers_have_different_identities() -> None:
    assert worker_module.worker_id() != worker_module.worker_id()


# ── the CI retry loop ────────────────────────────────────────────────────────


class CIProvider:
    def __init__(self, state: CIState) -> None:
        self._state = state
        self.reads = 0

    async def get_ci_state(self, pr_info: Any) -> CIState:
        self.reads += 1
        return self._state


def _published(**overrides: Any) -> AutofixJob:
    job = _job(**overrides)
    store = IndexStore.open("acme", "app")
    try:
        store.enqueue_autofix_job(job)
        return store.update_autofix_job(
            job.job_key,
            state="opened",
            branch_name="mira/fix/pr-7/abc",
            commit_sha="c0ffee",
            child_pr_url="https://github.com/acme/app/pull/900",
            child_pr_number=900,
        )
    finally:
        store.close()


async def test_a_red_ci_run_requeues_the_fix_once() -> None:
    job = _published()
    provider = CIProvider(CIState(state="failure", total=3, failing=["pytest"]))
    updated = await autofix_ci.recheck_job(provider, job, config=_config())
    assert updated.state == "queued"
    assert updated.ci_attempts == 1
    assert ReasonCode.CI_RETRY in updated.reason_codes()


async def test_the_ci_retry_budget_is_respected() -> None:
    job = _published()
    provider = CIProvider(CIState(state="failure", total=1, failing=["pytest"]))
    first = await autofix_ci.recheck_job(provider, job, config=_config())
    assert first.ci_attempts == 1

    # Pretend the regenerated fix published again, and CI is red a second time.
    store = IndexStore.open("acme", "app")
    try:
        again = store.update_autofix_job(first.job_key, state="opened")
    finally:
        store.close()
    second = await autofix_ci.recheck_job(provider, again, config=_config())
    assert second.state == "opened"  # not re-queued
    assert second.ci_attempts == 1
    assert ReasonCode.CI_RETRY_LIMIT in second.reason_codes()


async def test_a_green_ci_run_changes_nothing() -> None:
    job = _published()
    provider = CIProvider(CIState(state="success", total=3))
    updated = await autofix_ci.recheck_job(provider, job, config=_config())
    assert updated.state == "opened"
    assert updated.ci_attempts == 0


async def test_a_pending_ci_run_is_not_a_failure() -> None:
    job = _published()
    provider = CIProvider(CIState(state="pending", total=3, pending=["pytest"]))
    assert (await autofix_ci.recheck_job(provider, job, config=_config())).state == "opened"


async def test_an_unreadable_ci_state_never_regenerates_a_good_fix() -> None:
    job = _published()

    class Broken:
        async def get_ci_state(self, pr_info: Any) -> CIState:
            raise RuntimeError("the checks API is down")

    assert (await autofix_ci.recheck_job(Broken(), job, config=_config())).state == "opened"


def test_the_ci_summary_carries_names_and_not_log_bodies() -> None:
    state = CIState(state="failure", total=4, failing=["pytest", "ruff"])
    summary = autofix_ci._summarize(state)
    assert "pytest" in summary and "ruff" in summary
    assert "\n" not in summary.strip()


async def test_the_sweep_finds_published_fixes_whose_ci_went_red() -> None:
    _published()
    provider = CIProvider(CIState(state="failure", total=1, failing=["pytest"]))
    store = IndexStore.open("acme", "app")
    try:
        requeued = await autofix_ci.sweep(
            provider_factory=lambda job: provider, store=store, config=_config()
        )
    finally:
        store.close()
    assert requeued == 1


async def test_the_sweep_skips_a_repository_that_stopped_allowing_writes() -> None:
    _published()
    provider = CIProvider(CIState(state="failure", total=1, failing=["pytest"]))
    store = IndexStore.open("acme", "app")
    try:
        requeued = await autofix_ci.sweep(
            provider_factory=lambda job: provider,
            store=store,
            config=_config(mode="suggest"),
        )
    finally:
        store.close()
    assert requeued == 0
    assert provider.reads == 0


async def test_a_requeued_fix_tells_the_next_attempt_what_ci_said() -> None:
    job = _published()
    await retry_after_ci(job, ci_summary="pytest failed: 3 tests", config=_config())
    store = IndexStore.open("acme", "app")
    try:
        attempts = store.list_autofix_attempts(job_key=job.job_key)
    finally:
        store.close()
    assert [attempt.phase for attempt in attempts] == ["ci_retry"]
    assert "pytest failed" in attempts[0].detail


# ── administrative cancellation ──────────────────────────────────────────────


def test_an_admin_can_cancel_a_queued_job() -> None:
    _enqueue(_job())
    cancelled = cancel_job(
        owner="acme",
        repo="app",
        platform="github",
        job_key=_job().job_key,
        actor="root",
        reason="wrong finding",
    )
    assert cancelled.state == "cancelled"
    assert cancelled.cancelled_by == "root"
    assert cancelled.error == "wrong finding"


def test_cancelling_does_not_reach_through_to_the_platform() -> None:
    """A published fix stays published; cancel is not a deletion primitive."""
    _published()
    cancelled = cancel_job(
        owner="acme",
        repo="app",
        platform="github",
        job_key=_job().job_key,
        actor="root",
        reason="never mind",
    )
    assert cancelled.state == "opened"
    assert cancelled.child_pr_url.endswith("/900")


# ── the inline worker's lifecycle ────────────────────────────────────────────


def test_the_inline_worker_does_not_start_when_autofix_is_off() -> None:
    assert autofix_runtime.start({}, config=MiraConfig()) is None
    assert autofix_runtime.inline_worker() is None


def test_the_inline_worker_does_not_start_under_the_kill_switch() -> None:
    assert autofix_runtime.start({}, config=_config(kill_switch=True)) is None


def test_the_inline_worker_does_not_start_when_the_deployment_runs_its_own() -> None:
    assert autofix_runtime.start({}, config=_config(inline_worker=False)) is None


async def test_the_inline_worker_starts_and_stops(monkeypatch) -> None:
    monkeypatch.setattr(worker_module, "_targets", list)
    worker = autofix_runtime.start({}, config=_config())
    try:
        assert worker is not None
        assert autofix_runtime.inline_worker() is worker
    finally:
        await autofix_runtime.stop()
    assert autofix_runtime.inline_worker() is None


async def test_the_worker_asks_for_a_token_scoped_to_the_installation(monkeypatch) -> None:
    """An app-wide credential would be authority over every other customer."""
    asked: list[Any] = []

    class Auth:
        async def get_token(self, scope=None):  # noqa: ANN001
            asked.append(scope)
            return "installation-token"

    monkeypatch.setattr(autofix_runtime, "_installation_id", lambda job: 4242)
    factory = autofix_runtime.provider_factory({"github": Auth()})
    monkeypatch.setattr("mira.providers.create_provider", lambda platform, token: (platform, token))
    got = await factory(_job())
    assert got == ("github", "installation-token")
    assert asked == [4242]


async def test_the_worker_refuses_to_run_without_an_installation(monkeypatch) -> None:
    class Auth:
        async def get_token(self, scope=None):  # noqa: ANN001
            return "app-wide"

    monkeypatch.setattr(autofix_runtime, "_installation_id", lambda job: 0)
    factory = autofix_runtime.provider_factory({"github": Auth()})
    with pytest.raises(RuntimeError, match="installation"):
        await factory(_job())


# ── the review's findings, pinned ────────────────────────────────────────────


def test_the_repository_scan_rotates_instead_of_starving_the_tail(monkeypatch) -> None:
    """A fixed prefix would be a starvation bug rather than a cap: every
    repository past the limit would be polled never, not late."""
    every = [("github", "acme", f"repo{index}") for index in range(_REPO_SCAN_LIMIT + 25)]
    monkeypatch.setattr(worker_module, "_targets", lambda: every)
    worker = AutofixWorker(provider_factory=lambda job: object())

    seen: set[tuple[str, str, str]] = set()
    for _ in range(4):
        window = worker._scan_window()
        assert len(window) == _REPO_SCAN_LIMIT
        seen.update(window)
    assert seen == set(every)


def test_a_small_install_is_scanned_whole_every_poll(monkeypatch) -> None:
    every = [("github", "acme", f"repo{index}") for index in range(3)]
    monkeypatch.setattr(worker_module, "_targets", lambda: every)
    worker = AutofixWorker(provider_factory=lambda job: object())
    assert worker._scan_window() == every
    assert worker._scan_window() == every


async def test_the_ci_sweep_clock_is_per_queue_not_per_worker(monkeypatch) -> None:
    """One clock would let whichever repository is polled first spend the whole
    interval, and the poll order is stable — so the same ones would never be
    swept."""
    swept: list[str] = []

    async def fake_sweep(*, provider_factory, store, config):  # noqa: ANN001
        swept.append(getattr(store, "_repo", "?"))
        return 0

    monkeypatch.setattr("mira.autofix.ci.sweep", fake_sweep)
    worker = AutofixWorker(provider_factory=lambda job: object())
    store = IndexStore.open("acme", "app")
    try:
        await worker._maybe_sweep_ci(store, _config(), target="github:acme/one")
        await worker._maybe_sweep_ci(store, _config(), target="github:acme/two")
        # …and neither is swept again inside the interval.
        await worker._maybe_sweep_ci(store, _config(), target="github:acme/one")
    finally:
        store.close()
    assert len(swept) == 2


async def test_cancelling_mid_flight_stops_the_job_before_it_writes(monkeypatch) -> None:
    """The finding that mattered: a heartbeat that only *returned* left the job
    running to completion, so a cancelled fix could still open a pull request."""
    _enqueue(_job(max_attempts=1))
    started = asyncio.Event()
    finished = asyncio.Event()

    async def slow_run(provider, job, **kwargs):  # noqa: ANN001
        started.set()
        try:
            await asyncio.sleep(30)
        finally:
            finished.set()
        return SimpleNamespace(job=job)

    monkeypatch.setattr(worker_module, "run_job", slow_run)
    # A lease short enough that the heartbeat fires promptly.
    config = _config(lease_seconds=3)
    worker = AutofixWorker(provider_factory=lambda job: object())
    poll = asyncio.create_task(worker.poll_once(config=config))
    await asyncio.wait_for(started.wait(), timeout=5)

    cancel_job(
        owner="acme",
        repo="app",
        platform="github",
        job_key=_job().job_key,
        actor="root",
        reason="stop it now",
    )
    await asyncio.wait_for(poll, timeout=15)

    assert finished.is_set()  # the coroutine really was stopped
    job = _get(_job().job_key)
    assert job.state == "cancelled"
    assert job.cancelled_by == "root"
