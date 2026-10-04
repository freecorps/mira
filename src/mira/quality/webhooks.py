"""Webhook glue for escaped-bug tracking, shared by the three platforms.

The platform webhook modules decide *whether* an event is a merge or a push to
the default branch; everything after that is the same, and lives here.
Every function is a background task: it never raises, and it checks
``escaped_bugs`` before minting a token.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any

logger = logging.getLogger(__name__)

TokenGetter = Callable[[], Awaitable[str]]


def tracked(owner: str, repo: str) -> bool:
    """Cheap pre-check, so a disabled install schedules nothing at all."""
    try:
        from mira.config import load_config

        return load_config().escaped_bugs.tracks(owner, repo)
    except Exception:  # noqa: BLE001
        return False


def push_commits(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """``[{id, message}]`` from a push payload; the three platforms agree on these keys."""
    out = []
    for commit in payload.get("commits") or []:
        sha = str(commit.get("id") or commit.get("sha") or "")
        if sha:
            out.append({"id": sha, "message": str(commit.get("message") or "")})
    return out


async def on_push(
    platform: str, owner: str, repo: str, commits: list[dict[str, Any]], get_token: TokenGetter
) -> None:
    if not commits or not tracked(owner, repo):
        return
    try:
        from mira.platforms.handlers import run_escaped_bug_push
        from mira.providers import create_provider

        provider = create_provider(platform, await get_token())
        await run_escaped_bug_push(provider, owner, repo, commits, platform=platform)
    except Exception as exc:  # noqa: BLE001
        logger.debug("Escaped-bug push handling failed for %s/%s: %s", owner, repo, exc)
