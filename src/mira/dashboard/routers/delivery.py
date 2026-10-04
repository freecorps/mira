"""Dashboard routes for delivery analytics: hotspots and DORA / cycle time.

The hotspot heatmap is per-repository browsing data — file paths and counts,
the same thing the Files tab shows — so it follows the other repository
routes and is open to any signed-in user. DORA sits next to the rest of the
review-health page and, like it, is admin-only: it is about how a team works.
"""

from __future__ import annotations

from typing import Any

from fastapi import HTTPException, Request
from pydantic import BaseModel

from mira.analytics.dora import compute_dora
from mira.analytics.hotspots import compute_hotspots
from mira.config import load_config
from mira.dashboard import api as _api
from mira.dashboard.api import _open_store, _require_admin, router

DAY = 86400.0

# How far before the comparison window delivery rows are read, so a revert in
# the window can find the PR it reverted (MTTR).
_REVERT_LOOKBACK_DAYS = 90


class HotspotResponse(BaseModel):
    enabled: bool
    window_days: int
    total_files: int = 0
    max_score: float = 0.0
    files: list[dict[str, Any]] = []
    directories: list[dict[str, Any]] = []
    sources: dict[str, bool] = {}


class DoraResponse(BaseModel):
    enabled: bool
    days: int
    deployment_source: str
    current: dict[str, Any] | None = None
    previous: dict[str, Any] | None = None
    series: list[dict[str, Any]] = []
    bucket_secs: float = DAY
    failures: list[dict[str, Any]] = []
    repos: list[str] = []


@router.get("/api/repos/{owner}/{repo}/hotspots", response_model=HotspotResponse)
def repo_hotspots(owner: str, repo: str, days: int = 0, limit: int = 100) -> HotspotResponse:
    """Change-frequency hotspots: churn × complexity × finding density."""
    cfg = load_config().analytics.hotspots
    window = max(7, min(days or cfg.window_days, 730))
    limit = max(1, min(limit, 500))
    if not cfg.enabled:
        return HotspotResponse(enabled=False, window_days=window)
    with _open_store(owner, repo) as store:
        report = compute_hotspots(store, window_days=window, limit=limit)
    data = report.to_dict()
    return HotspotResponse(enabled=True, **data)


@router.get("/api/review-insights/dora", response_model=DoraResponse)
def review_dora(request: Request, days: int = 30, repo: str = "") -> DoraResponse:
    """DORA metrics + cycle-time breakdown for the last ``days`` vs the
    ``days`` before. ``repo`` narrows to one ``owner/repo``. Admin."""
    _require_admin(request)
    cfg = load_config().analytics.dora
    days = max(1, min(days, 365))
    if not cfg.enabled:
        return DoraResponse(enabled=False, days=days, deployment_source=cfg.deployment_source)
    owner = name = ""
    if repo:
        if repo.count("/") < 1:
            raise HTTPException(status_code=400, detail="repo must be owner/repo")
        owner, name = repo.rsplit("/", 1)
    now = _api._now()
    since = now - (2 * days + _REVERT_LOOKBACK_DAYS) * DAY
    db = _api._app_db
    rows = db.get_delivery_rows(since, owner=owner, repo=name)
    deployments = (
        db.get_deployments(now - 2 * days * DAY, owner=owner, repo=name)
        if cfg.deployment_source == "releases"
        else []
    )
    report = compute_dora(
        rows,
        deployments,
        days=days,
        deployment_source=cfg.deployment_source,
        title_prefixes=cfg.failure_title_prefixes,
        failure_labels=cfg.failure_labels,
        now=now,
    )
    # Repositories with merge data, for the panel's selector — from the
    # unfiltered read so picking one does not empty the list.
    all_rows = rows if not repo else db.get_delivery_rows(since)
    repos = sorted({f"{r['owner']}/{r['repo']}" for r in all_rows})
    data = report.to_dict()
    return DoraResponse(enabled=True, repos=repos, **data)
