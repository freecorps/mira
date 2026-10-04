"""Dashboard routes for review-quality measurement: backtests and escaped bugs.

Read-only. Backtests are started from the CLI (they spend model budget and
take minutes, neither of which belongs behind a button), and escaped bugs are
recorded by the merge/push webhooks or ``mira escaped-bugs scan``. These routes
only show what was recorded.

Admin-only, like the gate and triage history: the rows quote pull request
titles and file paths across every repository in the install.
"""

from __future__ import annotations

import logging
import re

from fastapi import HTTPException, Request
from pydantic import BaseModel

from mira.dashboard.api import _require_admin, router
from mira.feedback.analytics import PlatformResolutionError

logger = logging.getLogger(__name__)

_MAX_PAGE = 200
_UNSAFE_SEGMENT = re.compile(r"[/\x00]")
_RUN_ID = re.compile(r"^[A-Za-z0-9_.:-]{1,80}$")


class QualitySummaryResponse(BaseModel):
    repos: list[dict]
    totals: dict


class EscapedBugPage(BaseModel):
    bugs: list[dict]
    limit: int
    offset: int


class BacktestRunPage(BaseModel):
    runs: list[dict]
    limit: int
    offset: int


class BacktestRunDetail(BaseModel):
    run: dict


def _safe(value: str, label: str) -> str:
    if value and (value in {".", ".."} or _UNSAFE_SEGMENT.search(value)):
        raise HTTPException(status_code=400, detail=f"Invalid {label}")
    return value


def _page(limit: int, offset: int) -> tuple[int, int]:
    return max(1, min(limit, _MAX_PAGE)), max(0, offset)


@router.get("/api/quality/summary", response_model=QualitySummaryResponse)
def quality_summary(
    request: Request, owner: str = "", repo: str = "", since: float = 0.0
) -> QualitySummaryResponse:
    """Real-world recall: escaped bugs Mira had flagged vs ones it missed."""
    _require_admin(request)
    from mira.quality import history

    try:
        data = history.recall_summary(
            owner=_safe(owner, "owner"), repo=_safe(repo, "repo"), since=since
        )
    except PlatformResolutionError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return QualitySummaryResponse(**data)


@router.get("/api/quality/escaped-bugs", response_model=EscapedBugPage)
def list_escaped_bugs(
    request: Request,
    owner: str = "",
    repo: str = "",
    flagged: str = "",
    since: float = 0.0,
    limit: int = 50,
    offset: int = 0,
) -> EscapedBugPage:
    """Recorded escaped bugs, newest first. ``flagged=yes|no`` filters."""
    _require_admin(request)
    from mira.quality import history

    if flagged not in {"", "yes", "no"}:
        raise HTTPException(status_code=400, detail="flagged must be 'yes' or 'no'")
    limit, offset = _page(limit, offset)
    bugs = history.list_escaped_bugs(
        owner=_safe(owner, "owner"),
        repo=_safe(repo, "repo"),
        flagged=None if not flagged else flagged == "yes",
        since=since,
        limit=limit,
        offset=offset,
    )
    return EscapedBugPage(bugs=bugs, limit=limit, offset=offset)


@router.get("/api/quality/backtests", response_model=BacktestRunPage)
def list_backtests(
    request: Request, owner: str = "", repo: str = "", limit: int = 50, offset: int = 0
) -> BacktestRunPage:
    """Stored backtest runs with their per-configuration summaries."""
    _require_admin(request)
    from mira.quality import history

    limit, offset = _page(limit, offset)
    runs = history.list_backtest_runs(
        owner=_safe(owner, "owner"), repo=_safe(repo, "repo"), limit=limit, offset=offset
    )
    return BacktestRunPage(runs=runs, limit=limit, offset=offset)


@router.get("/api/quality/backtests/{owner}/{repo}/{run_id}", response_model=BacktestRunDetail)
def get_backtest(
    request: Request, owner: str, repo: str, run_id: str, platform: str = "github"
) -> BacktestRunDetail:
    """One run with every per-PR result."""
    _require_admin(request)
    from mira.quality import history

    if not owner or not repo or not _RUN_ID.match(run_id):
        raise HTTPException(status_code=400, detail="Invalid run reference")
    if platform not in {"github", "gitlab", "forgejo"}:
        raise HTTPException(status_code=400, detail="Invalid platform")
    run = history.get_backtest_run(_safe(owner, "owner"), _safe(repo, "repo"), run_id, platform)
    if run is None:
        raise HTTPException(status_code=404, detail="Backtest run not found")
    return BacktestRunDetail(run=run)
