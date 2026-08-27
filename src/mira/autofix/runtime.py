"""The inline worker's handle, and how the server starts and stops it.

Kept apart from :mod:`mira.autofix.worker` so that the worker itself stays a
plain object with no global state — which is what lets a test build one, drive
it a poll at a time, and throw it away. This module holds the single process-
wide instance the FastAPI lifespan owns, and nothing else.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import Any

from mira.autofix.worker import AutofixWorker
from mira.config import MiraConfig, load_config

logger = logging.getLogger(__name__)

_worker: AutofixWorker | None = None
_task: asyncio.Task | None = None


def inline_worker() -> AutofixWorker | None:
    """The running in-process worker, or None when there is not one."""
    return _worker


def provider_factory(auths: dict[str, Any]) -> Any:
    """Build the per-job provider callable from whatever auth the server has.

    A callable rather than a provider instance, because an installation token
    is short-lived: a worker that held one at startup would be holding an
    expired one by the time a fix was requested.
    """

    async def _factory(job: Any) -> Any:
        from mira.providers import create_provider

        platform = (getattr(job, "platform", "") or "github").lower()
        auth = auths.get(platform)
        if auth is None:
            raise RuntimeError(f"No {platform} credentials are configured for autofix")
        token = await _token_for(auth, job)
        return create_provider(platform, token)

    return _factory


def _installation_id(job: Any) -> int:
    """The installation that owns this repository, from the registry.

    GitHub mints tokens per installation, and a worker holds no webhook payload
    to read one from. Looking it up keeps the credential scoped to the one
    install that asked — an app-level credential would let a fix on one
    customer's repository be signed with authority over every other one.
    """
    from mira.dashboard.api import _app_db

    if _app_db is None:  # pragma: no cover - only unconfigured installs
        return 0
    record = _app_db.get_repo(job.owner, job.repo, platform=job.platform or "github")
    return int(getattr(record, "installation_id", 0) or 0)


async def _token_for(auth: Any, job: Any) -> str:
    """A token scoped as narrowly as the platform allows.

    On GitHub that is an installation token for the one installation that owns
    the repository. On the token-based platforms it is the configured project
    or instance token, which is as narrow as those APIs get — and is why the
    documentation asks for one scoped to the repositories autofix may touch.
    """
    scope: str | int | None = None
    if (job.platform or "github").lower() == "github":
        scope = _installation_id(job)
        if not scope:
            raise RuntimeError(
                f"No GitHub installation is recorded for {job.owner}/{job.repo}; "
                "autofix will not mint an app-wide token to work around it"
            )
    getter = getattr(auth, "get_token", None)
    if not callable(getter):
        raise RuntimeError("No usable credential for autofix on this platform")
    result = getter(scope)
    return str(await result if asyncio.iscoroutine(result) else result)


def start(auths: dict[str, Any], *, config: MiraConfig | None = None) -> AutofixWorker | None:
    """Start the in-process worker if this deployment wants one.

    Returns the worker so a caller can stop it, or None when autofix is off,
    the kill switch is on, or the deployment runs the worker elsewhere. Starting
    nothing is the default: `autofix.mode` is `off` out of the box, and a queue
    with nothing in it still costs a poll.
    """
    global _worker, _task
    config = config or load_config()
    if config.autofix.mode == "off" or config.autofix.kill_switch:
        return None
    if not config.autofix.inline_worker:
        logger.info("Autofix inline worker disabled; run `mira autofix-worker` separately")
        return None
    if _worker is not None:
        return _worker
    _worker = AutofixWorker(provider_factory=provider_factory(auths))
    _task = asyncio.create_task(_worker.run_forever())
    _task.add_done_callback(
        lambda done: (
            logger.warning("Autofix worker stopped: %s", done.exception())
            if not done.cancelled() and done.exception()
            else None
        )
    )
    return _worker


async def stop() -> None:
    """Stop the in-process worker, if one is running.

    A job the worker was holding is not lost: its lease simply stops being
    renewed and the next worker to poll — this process after a restart, or
    another one — takes it back.
    """
    global _worker, _task
    if _worker is not None:
        _worker.stop()
    if _task is not None and not _task.done():
        _task.cancel()
        # Shutdown is best-effort: a worker that raises on its way out must not
        # stop the server from stopping. Whatever job it held stays leased and
        # is reclaimed by the next poll after the restart.
        #
        # `CancelledError` is listed explicitly because it is a BaseException
        # since 3.8, so suppressing `Exception` alone lets the cancellation we
        # just requested propagate out of our own shutdown.
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await _task
    _worker = None
    _task = None
