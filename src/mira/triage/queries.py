"""Reading triage runs back, across whichever store backs the install.

The same two shapes every history reader in this codebase has: Postgres keeps
one table for the install, so a filter and a ``LIMIT`` answer directly; SQLite
keeps a file per repository, so the same question means visiting each file and
merging. The platform-resolution and repository-walking primitives are the
Phase 3 analytics ones, shared rather than re-implemented — a third copy would
eventually disagree with the other two about which repository a row belongs to.
"""

from __future__ import annotations

import logging
from typing import Any

from mira.feedback.analytics import (
    PlatformResolutionError,
    _postgres_url,
    _repo_targets,
    open_analytics_store,
)
from mira.feedback.analytics import (
    _platform_for as platform_for,
)
from mira.triage.models import TriageRun

logger = logging.getLogger(__name__)

__all__ = [
    "PlatformResolutionError",
    "get_run",
    "list_runs",
    "platform_for",
    "summarize_candidates",
]

_MERGE_PAGE_SIZE = 200
_MERGE_MAX_ROWS = 20_000


def _sort_key(run: TriageRun, sort: str) -> Any:
    if sort == "pr_number":
        return run.inputs.pr_number
    if sort == "status":
        return run.status
    if sort == "duration_seconds":
        return run.duration_seconds
    return run.created_at


def _walk(store: Any, filters: dict[str, Any], **kwargs: Any) -> list[TriageRun]:
    out: list[TriageRun] = []
    offset = 0
    while offset < _MERGE_MAX_ROWS:
        page = store.list_triage_runs(filters, limit=_MERGE_PAGE_SIZE, offset=offset, **kwargs)
        if not page:
            return out
        out.extend(page)
        if len(page) < _MERGE_PAGE_SIZE:
            return out
        offset += _MERGE_PAGE_SIZE
    logger.warning(
        "Triage history walk hit the %s-row backstop; the page may be incomplete",
        _MERGE_MAX_ROWS,
    )
    return out


def list_runs(
    *,
    filters: dict[str, Any] | None = None,
    limit: int = 50,
    offset: int = 0,
    sort: str = "created_at",
    descending: bool = True,
) -> tuple[list[TriageRun], int]:
    """One page of runs plus the total that matched."""
    active = dict(filters or {})
    owner = str(active.get("owner") or "")
    repo = str(active.get("repo") or "")

    if _postgres_url():
        with open_analytics_store("", "") as store:
            rows = store.list_triage_runs(
                active, limit=limit, offset=offset, sort=sort, descending=descending
            )
            total = store.count_triage_runs(active)
        return rows, total

    merged: list[TriageRun] = []
    total = 0
    for platform, db_owner, db_repo in _repo_targets(owner, repo):
        scoped = {**active, "platform": active.get("platform") or platform}
        # The SQLite file is already scoped to one repository; passing the
        # owner again would filter on the namespaced spelling and match
        # nothing.
        scoped.pop("owner", None)
        scoped.pop("repo", None)
        with open_analytics_store(db_owner, db_repo, platform=platform) as store:
            total += store.count_triage_runs(scoped)
            merged.extend(_walk(store, scoped, sort=sort, descending=descending))
    merged.sort(key=lambda row: _sort_key(row, sort), reverse=descending)
    return merged[offset : offset + limit], total


def summarize_candidates(*, filters: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """How often each identity has been suggested, and how highly."""
    active = dict(filters or {})
    owner = str(active.get("owner") or "")
    repo = str(active.get("repo") or "")

    if _postgres_url():
        with open_analytics_store("", "") as store:
            return store.summarize_triage_candidates(active)

    buckets: dict[tuple[str, str], dict[str, Any]] = {}
    for platform, db_owner, db_repo in _repo_targets(owner, repo):
        scoped = {**active, "platform": active.get("platform") or platform}
        scoped.pop("owner", None)
        scoped.pop("repo", None)
        with open_analytics_store(db_owner, db_repo, platform=platform) as store:
            for row in store.summarize_triage_candidates(scoped):
                key = (row["identity"], row["kind"])
                bucket = buckets.setdefault(
                    key,
                    {
                        "identity": row["identity"],
                        "kind": row["kind"],
                        "count": 0,
                        "_rank_total": 0.0,
                        "_score_total": 0.0,
                    },
                )
                bucket["count"] += row["count"]
                bucket["_rank_total"] += row["average_rank"] * row["count"]
                bucket["_score_total"] += row["average_score"] * row["count"]
    out = []
    for bucket in buckets.values():
        count = bucket["count"] or 1
        out.append(
            {
                "identity": bucket["identity"],
                "kind": bucket["kind"],
                "count": bucket["count"],
                "average_rank": round(bucket["_rank_total"] / count, 3),
                "average_score": round(bucket["_score_total"] / count, 3),
            }
        )
    out.sort(key=lambda row: (-row["count"], row["identity"]))
    return out


def get_run(owner: str, repo: str, run_id: int) -> TriageRun | None:
    platform = platform_for(owner, repo)
    with open_analytics_store(owner, repo, platform=platform) as store:
        return store.get_triage_run_by_id(run_id)
