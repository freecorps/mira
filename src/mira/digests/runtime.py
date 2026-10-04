"""The digest scheduler inside ``mira serve``, and the credentials it reads with.

One small loop: every few minutes, load the configuration and, when
``digests.enabled`` is on, run :func:`mira.digests.service.run_scheduled`. The
loop starts with the server whether or not digests are enabled, because the
setting can be changed from the dashboard without a restart and an idle tick
costs one configuration load. Which period to build, and whether it has
already been built, is decided by the stored schedule boundary — not by the
loop's own clock — so a restart neither skips nor repeats a week.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
from typing import Any

from mira.config import MiraConfig, load_config

logger = logging.getLogger(__name__)

_TICK_SECONDS = float(os.environ.get("MIRA_DIGEST_TICK_SECONDS", "300") or 300)
# Startup does its own work (backfills, discovery); the first tick waits.
_FIRST_TICK_DELAY = float(os.environ.get("MIRA_DIGEST_FIRST_TICK_SECONDS", "60") or 60)

_task: asyncio.Task | None = None


def auths_from_env() -> dict[str, Any]:
    """Platform credentials from the same environment ``mira serve`` reads."""
    auths: dict[str, Any] = {}
    app_id = os.environ.get("MIRA_GITHUB_APP_ID", "")
    private_key = os.environ.get("MIRA_GITHUB_PRIVATE_KEY", "")
    if app_id and private_key:
        from mira.platforms.github.auth import GitHubAppAuth

        auths["github"] = GitHubAppAuth(app_id=app_id, private_key=private_key)
    gitlab_token = os.environ.get("MIRA_GITLAB_TOKEN", "")
    if gitlab_token:
        from mira.platforms.gitlab.auth import GitLabTokenAuth

        auths["gitlab"] = GitLabTokenAuth(
            gitlab_token, os.environ.get("MIRA_GITLAB_BASE_URL") or "https://gitlab.com/api/v4"
        )
    forgejo_token = os.environ.get("MIRA_FORGEJO_TOKEN", "")
    if forgejo_token:
        from mira.platforms.forgejo.auth import ForgejoTokenAuth

        auths["forgejo"] = ForgejoTokenAuth(
            forgejo_token,
            os.environ.get("MIRA_FORGEJO_BASE_URL") or "https://codeberg.org/api/v1",
        )
    return auths


def provider_factory(auths: dict[str, Any]) -> Any:
    """``await factory(platform, owner, repo)`` → a provider for that repository.

    A token per call rather than one held by the loop: a GitHub installation
    token lasts an hour, and the loop lives for weeks. On GitHub the token is
    scoped to the installation that owns the repository; ``GITHUB_TOKEN`` is the
    fallback for an install that runs on a personal token instead of an App.
    """

    async def _factory(platform: str, owner: str, repo: str) -> Any:
        from mira.providers import create_provider

        platform = (platform or "github").lower()
        auth = auths.get(platform)
        if platform == "github":
            token = ""
            if auth is not None:
                from mira.dashboard.api import _app_db

                record = _app_db.get_repo(owner, repo, platform="github") if _app_db else None
                installation = int(getattr(record, "installation_id", 0) or 0)
                if installation:
                    token = str(await auth.get_token(installation))
            token = token or os.environ.get("GITHUB_TOKEN", "")
            if not token:
                raise RuntimeError(f"No GitHub credential can read {owner}/{repo}")
            return create_provider("github", token)
        if auth is None:
            raise RuntimeError(f"No {platform} credentials are configured")
        return create_provider(platform, str(await auth.get_token(None)))

    return _factory


def digest_llm(config: MiraConfig) -> Any:
    """The indexing-tier model: summarising is what that tier is for."""
    from mira.dashboard.models_config import llm_config_for
    from mira.llm import create_llm

    return create_llm(llm_config_for("indexing", config.llm))


async def tick(provider_for: Any, *, config: MiraConfig | None = None) -> list[int]:
    """One pass. Never raises."""
    from mira.digests.service import run_scheduled

    try:
        config = config or load_config()
        if not config.digests.enabled:
            return []
        from mira.dashboard.api import _app_db

        llm = digest_llm(config) if config.digests.use_llm else None
        return await run_scheduled(config, db=_app_db, provider_for=provider_for, llm=llm)
    except Exception as exc:  # noqa: BLE001 - the loop must keep running
        logger.warning("Digest scheduler tick failed: %s", exc)
        return []


async def run_forever(auths: dict[str, Any]) -> None:
    provider_for = provider_factory(auths)
    await asyncio.sleep(_FIRST_TICK_DELAY)
    while True:
        await tick(provider_for)
        await asyncio.sleep(_TICK_SECONDS)


def start(auths: dict[str, Any]) -> asyncio.Task | None:
    """Start the loop once per process. Returns the task."""
    global _task
    if _task is not None and not _task.done():
        return _task
    _task = asyncio.create_task(run_forever(auths))
    _task.add_done_callback(
        lambda done: (
            logger.warning("Digest scheduler stopped: %s", done.exception())
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
