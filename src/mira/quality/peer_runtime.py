"""Running peer recall: one repository, every repository, and the scheduler.

The scheduler follows the digest scheduler's shape: a loop inside ``mira
serve`` that wakes every few minutes, reloads the configuration, and runs only
when a schedule boundary (daily, or weekly on Monday, at ``peer_recall.hour``
UTC) has passed that no process has claimed yet. The claim is a
compare-and-set on the application settings, taken before the work, so two
replicas or a restart mid-run cannot both run one boundary; a run where every
repository failed hands the claim back for the next tick.

The run itself only reads from GitHub. What it may change is Mira's own
learned rules, and only through :mod:`mira.quality.peer_learning`.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import time
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

logger = logging.getLogger(__name__)

LAST_BOUNDARY_KEY = "peer_recall:last_boundary"
LAST_RUN_KEY = "peer_recall:last_run"

_TICK_SECONDS = float(os.environ.get("MIRA_PEER_RECALL_TICK_SECONDS", "300") or 300)
_FIRST_TICK_DELAY = float(os.environ.get("MIRA_PEER_RECALL_FIRST_TICK_SECONDS", "120") or 120)

ProviderFor = Callable[[str, str, str], Awaitable[Any]]

_task: asyncio.Task | None = None
# One run at a time per process, scheduled or on demand: two would read the
# same pull requests and race to learn from the same rows.
_running = asyncio.Lock()


@dataclass
class RepoRun:
    platform: str
    owner: str
    repo: str
    prs_examined: int = 0
    threads: int = 0
    missed_and_addressed: int = 0
    learned: int = 0
    rules_activated: list[int] = field(default_factory=list)
    error: str = ""
    skipped: str = ""


@dataclass
class RunReport:
    started_at: float = 0.0
    finished_at: float = 0.0
    trigger: str = "schedule"
    repos: list[RepoRun] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# ----------------------------------------------------------------- schedule


def latest_boundary(now: datetime, schedule: str, hour: int) -> datetime:
    """The most recent scheduled instant at or before ``now``."""
    now = now.astimezone(UTC) if now.tzinfo else now.replace(tzinfo=UTC)
    today = now.replace(hour=hour, minute=0, second=0, microsecond=0)
    if schedule == "weekly":
        candidate = today - timedelta(days=now.weekday())
        return candidate if candidate <= now else candidate - timedelta(days=7)
    return today if today <= now else today - timedelta(days=1)


def _claim(db: Any, boundary_ts: float) -> tuple[bool, str | None]:
    current = db.get_setting(LAST_BOUNDARY_KEY)
    try:
        if current is not None and float(current) >= boundary_ts:
            return False, current
    except ValueError:
        pass
    return bool(db.compare_and_set_setting(LAST_BOUNDARY_KEY, current, repr(boundary_ts))), current


def _release(db: Any, boundary_ts: float, previous: str | None) -> None:
    try:
        db.compare_and_set_setting(LAST_BOUNDARY_KEY, repr(boundary_ts), previous or "0")
    except Exception as exc:  # noqa: BLE001 - the claim stands; the period is skipped
        logger.warning("Could not release the peer-recall boundary: %s", exc)


# ------------------------------------------------------------------ running


def repositories(config: Any, db: Any) -> list[tuple[str, str, str]]:
    """``(platform, owner, repo)`` for every GitHub repository a run covers."""
    settings = config.peer_recall
    if settings.repos:
        out = []
        for entry in settings.repos:
            owner, _, repo = entry.rpartition("/")
            if owner and repo:
                out.append(("github", owner, repo))
        return out
    if db is None:
        return []
    return [
        ("github", record.owner, record.repo)
        for record in db.list_repos()
        if (record.platform or "github") == "github" and settings.covers(record.owner, record.repo)
    ]


def review_llm(config: Any) -> Any:
    """The review-tier model: a learned check is read by every later review."""
    from mira.dashboard.models_config import llm_config_for
    from mira.llm import create_llm

    return create_llm(llm_config_for("review", config.llm))


async def run_repository(
    provider: Any,
    owner: str,
    repo: str,
    *,
    config: Any,
    llm: Any = None,
    platform: str = "github",
    rescan: bool = False,
    learn: bool = True,
) -> RepoRun:
    """Collect one repository's peer threads, then learn from its misses."""
    from mira.quality.peer_learning import learn_from_misses
    from mira.quality.peers import collect_repository
    from mira.quality.readonly import ReadOnlyProvider
    from mira.quality.store import open_quality_store

    outcome = RepoRun(platform=platform, owner=owner, repo=repo)
    if platform != "github":
        outcome.skipped = "peer recall reads GitHub review threads only"
        return outcome
    reader = ReadOnlyProvider(provider)
    with open_quality_store(owner, repo, platform) as store:
        collected = await collect_repository(
            reader, owner, repo, store, config.peer_recall, platform=platform, rescan=rescan
        )
        outcome.prs_examined = collected.prs_examined
        outcome.threads = collected.threads
        outcome.missed_and_addressed = collected.missed_and_addressed
        if collected.unsupported:
            outcome.skipped = "the provider cannot list review threads"
            return outcome
        if collected.errors and not collected.prs_examined:
            outcome.error = collected.errors[0]
        if learn:
            learned = await learn_from_misses(
                reader, owner, repo, store, llm, config, platform=platform
            )
            outcome.learned = learned.examined
            outcome.rules_activated = list(learned.activated)
    return outcome


async def run_all(
    config: Any,
    *,
    db: Any,
    provider_for: ProviderFor,
    llm: Any = None,
    trigger: str = "schedule",
    only: tuple[str, str] | None = None,
) -> RunReport:
    """Every covered repository (or ``only`` one), one after another. Never raises."""
    from mira.quality.peer_learning import enabled as learning_enabled

    report = RunReport(started_at=time.time(), trigger=trigger)
    async with _running:
        if llm is None and learning_enabled(config):
            try:
                llm = review_llm(config)
            except Exception as exc:  # noqa: BLE001 - collect without learning
                logger.warning("Peer recall: no review model, learning skipped: %s", exc)
        targets = repositories(config, db)
        if only is not None:
            targets = [("github", only[0], only[1])]
        for platform, owner, repo in targets:
            try:
                provider = await provider_for(platform, owner, repo)
                report.repos.append(
                    await run_repository(
                        provider, owner, repo, config=config, llm=llm, platform=platform
                    )
                )
            except Exception as exc:  # noqa: BLE001 - one repository must not stop the rest
                logger.warning("Peer recall failed for %s/%s: %s", owner, repo, exc)
                report.repos.append(
                    RepoRun(platform=platform, owner=owner, repo=repo, error=str(exc)[:300])
                )
    report.finished_at = time.time()
    if db is not None:
        try:
            db.set_setting(LAST_RUN_KEY, json.dumps(report.to_dict(), default=str))
        except Exception as exc:  # noqa: BLE001
            logger.debug("Could not store the peer-recall run report: %s", exc)
    return report


def last_run(db: Any) -> dict[str, Any] | None:
    if db is None:
        return None
    raw = db.get_setting(LAST_RUN_KEY)
    try:
        value = json.loads(raw) if raw else None
    except (TypeError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def is_running() -> bool:
    return _running.locked()


async def tick(
    provider_for: ProviderFor, *, config: Any = None, db: Any = None, now: datetime | None = None
) -> RunReport | None:
    """One scheduler pass. Never raises."""
    try:
        if config is None:
            from mira.config import load_config

            config = load_config()
        settings = config.peer_recall
        if not settings.enabled:
            return None
        if db is None:
            from mira.dashboard.api import _app_db

            db = _app_db
        if db is None:
            return None
        boundary = latest_boundary(
            now or datetime.now(tz=UTC), settings.schedule, settings.hour
        ).timestamp()
        claimed, previous = _claim(db, boundary)
        if not claimed:
            return None
        try:
            report = await run_all(config, db=db, provider_for=provider_for)
        except BaseException:
            _release(db, boundary, previous)
            raise
        if report.repos and all(run.error for run in report.repos):
            logger.warning("Every peer-recall repository failed; the run will be retried")
            _release(db, boundary, previous)
        return report
    except Exception as exc:  # noqa: BLE001 - the loop must keep running
        logger.warning("Peer-recall scheduler tick failed: %s", exc)
        return None


async def run_forever(auths: dict[str, Any]) -> None:
    from mira.digests.runtime import provider_factory

    provider_for = provider_factory(auths)
    await asyncio.sleep(_FIRST_TICK_DELAY)
    while True:
        await tick(provider_for)
        await asyncio.sleep(_TICK_SECONDS)


def start(auths: dict[str, Any]) -> asyncio.Task | None:
    """Start the loop once per process. Nothing to do without GitHub credentials."""
    global _task
    if "github" not in auths and not os.environ.get("GITHUB_TOKEN"):
        return None
    if _task is not None and not _task.done():
        return _task
    _task = asyncio.create_task(run_forever(auths))
    _task.add_done_callback(
        lambda done: (
            logger.warning("Peer-recall scheduler stopped: %s", done.exception())
            if not done.cancelled() and done.exception()
            else None
        )
    )
    return _task


async def stop() -> None:
    global _task
    if _task is not None and not _task.done():
        _task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await _task
    _task = None
