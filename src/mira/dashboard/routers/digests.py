"""Dashboard routes for digests and release notes.

Reading stored digests is open to any signed-in user, the same reach as the
activity feed: a digest says which pull requests merged, which every user of
the dashboard can already see. Generating one is admin-only — it spends model
calls and, with ``deliver``, posts to every subscribed webhook — and so are
release notes, which are generated on request. Release notes are a ``GET`` so
an admin's read-only API token can fetch them from a release pipeline; they
change nothing.

Owner and repo are checked against the registry before any provider call, so
a request can only ever read a repository Mira was installed on.
"""

from __future__ import annotations

import logging
import time
from datetime import UTC, datetime
from typing import Literal

from fastapi import HTTPException, Request
from pydantic import BaseModel, Field

from mira.config import load_config
from mira.dashboard import api as _api
from mira.dashboard.api import _require_admin, router

logger = logging.getLogger(__name__)

_MAX_PAGE = 200

Platform = Literal["github", "gitlab", "forgejo"]


class DigestPage(BaseModel):
    digests: list[dict]
    total: int
    limit: int
    offset: int


class DigestGenerate(BaseModel):
    platform: Platform = "github"
    owner: str = Field(min_length=1, max_length=500)
    repo: str = Field(min_length=1, max_length=200)
    days: int = Field(default=7, ge=1, le=90)
    deliver: bool = False


class ReleaseNotesResponse(BaseModel):
    markdown: str
    notes: dict


def _require_repo(platform: str, owner: str, repo: str) -> None:
    if _api._app_db is None:
        raise HTTPException(503, "The repository registry is unavailable")
    if not _api._app_db.get_repo(owner, repo, platform=platform):
        raise HTTPException(404, "Repository not found")


async def _provider(platform: str, owner: str, repo: str):  # type: ignore[no-untyped-def]
    from mira.digests.runtime import auths_from_env, provider_factory

    try:
        return await provider_factory(auths_from_env())(platform, owner, repo)
    except Exception as exc:
        logger.warning("No provider for %s/%s: %s", owner, repo, exc)
        raise HTTPException(400, f"No {platform} credentials can read {owner}/{repo}") from exc


@router.get("/api/digests", response_model=DigestPage)
def list_digests(
    kind: str = "",
    platform: str = "",
    owner: str = "",
    repo: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> DigestPage:
    limit = max(1, min(limit, _MAX_PAGE))
    offset = max(0, offset)
    rows, total = _api._app_db.list_digests(
        kind=kind, platform=platform, owner=owner, repo=repo, limit=limit, offset=offset
    )
    return DigestPage(digests=rows, total=total, limit=limit, offset=offset)


@router.get("/api/digests/{digest_id}")
def get_digest(digest_id: int) -> dict:
    found = _api._app_db.get_digest(digest_id)
    if found is None:
        raise HTTPException(404, "Digest not found")
    return found


@router.post("/api/digests/generate")
async def generate_digest(body: DigestGenerate, request: Request) -> dict:
    """Build a digest of the trailing ``days`` now, store it, optionally deliver it."""
    _require_admin(request)
    _require_repo(body.platform, body.owner, body.repo)
    from mira.digests.runtime import digest_llm
    from mira.digests.service import (
        DigestTarget,
        build_digest,
        index_context,
        publish_digest,
    )

    config = load_config()
    cfg = config.digests
    provider = await _provider(body.platform, body.owner, body.repo)
    until = time.time()
    try:
        digest = await build_digest(
            [
                DigestTarget(
                    provider=provider,
                    platform=body.platform,
                    owner=body.owner,
                    repo=body.repo,
                    context=index_context(body.platform, body.owner, body.repo),
                )
            ],
            since=until - body.days * 86400,
            until=until,
            cfg=cfg,
            llm=digest_llm(config) if cfg.use_llm else None,
        )
    except Exception as exc:
        logger.warning("Digest for %s/%s failed: %s", body.owner, body.repo, exc)
        raise HTTPException(502, f"Could not read {body.owner}/{body.repo}: {exc}") from exc
    digest_id = await publish_digest(digest, cfg=cfg, db=_api._app_db, deliver=body.deliver)
    found = _api._app_db.get_digest(digest_id) if digest_id else None
    return found or {"id": 0, "data": digest.to_dict()}


def _parse_date(value: str) -> float:
    try:
        parsed = datetime.fromisoformat(value.strip())
    except ValueError as exc:
        raise HTTPException(422, f"Not a date: {value!r} (use YYYY-MM-DD)") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.timestamp()


@router.get("/api/release-notes", response_model=ReleaseNotesResponse)
async def release_notes(
    request: Request,
    owner: str,
    repo: str,
    platform: Platform = "github",
    from_ref: str = "",
    to_ref: str = "",
    since: str = "",
    llm: bool = True,
) -> ReleaseNotesResponse:
    """Markdown release notes for ``from_ref..to_ref`` or since a date."""
    _require_admin(request)
    if bool(from_ref) == bool(since):
        raise HTTPException(422, "Give either from_ref or since")
    _require_repo(platform, owner, repo)
    from mira.digests.release_notes import render_markdown
    from mira.digests.runtime import digest_llm
    from mira.digests.service import build_release_notes_for

    config = load_config()
    provider = await _provider(platform, owner, repo)
    try:
        notes = await build_release_notes_for(
            provider,
            platform=platform,
            owner=owner,
            repo=repo,
            cfg=config.digests,
            from_ref=from_ref,
            to_ref=to_ref,
            since=_parse_date(since) if since else 0.0,
            llm=digest_llm(config) if llm and config.digests.use_llm else None,
        )
    except HTTPException:
        raise
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    except Exception as exc:
        logger.warning("Release notes for %s/%s failed: %s", owner, repo, exc)
        raise HTTPException(502, f"Could not read {owner}/{repo}: {exc}") from exc
    return ReleaseNotesResponse(markdown=render_markdown(notes), notes=notes.to_dict())
