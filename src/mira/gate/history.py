"""Reading gate decisions back, across whichever store backs the install.

Postgres keeps every repository in one table, so a filter and a ``LIMIT``
answer any question directly. SQLite keeps a file per repository, so the same
question means visiting each file and merging. Both paths page inside the
store and only ever hold one page in memory — a decision history is exactly the
kind of table that grows without anyone noticing, and the Orange Pi profile
cannot afford to materialise it.

The platform-resolution and repository-walking primitives are shared with the
Phase 3 analytics rather than re-implemented: they solve the same problem (the
same repository name can exist on three platforms and its rows live under a
namespaced owner), and two implementations would eventually disagree.
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
from mira.gate.models import GateDecision

logger = logging.getLogger(__name__)

__all__ = [
    "PlatformResolutionError",
    "get_decision",
    "list_decisions",
    "list_overrides",
    "platform_for",
    "summarize",
]

# Rows pulled per round trip while merging a repository's decisions. A batch
# size, not a ceiling — the walk keeps paging until the repository is done.
_MERGE_PAGE_SIZE = 200

# Backstop on one repository's walk, so a pathological history cannot spin
# forever. Crossing it is logged, never silent.
_MERGE_MAX_ROWS = 20_000


def _sort_key(decision: GateDecision, sort: str) -> Any:
    if sort == "risk_score":
        return decision.risk_score
    if sort == "pr_number":
        return decision.inputs.pr_number
    if sort == "state":
        return decision.state
    return decision.created_at


def list_decisions(
    *,
    filters: dict[str, Any] | None = None,
    limit: int = 50,
    offset: int = 0,
    sort: str = "created_at",
    descending: bool = True,
) -> tuple[list[GateDecision], int]:
    """One page of decisions plus the total that matched."""
    active = dict(filters or {})
    owner = str(active.get("owner") or "")
    repo = str(active.get("repo") or "")

    if _postgres_url():
        # One table for the whole install: the org-wide handle sees everything
        # and the filters do the scoping.
        with open_analytics_store("", "") as store:
            rows = store.list_gate_decisions(
                active, limit=limit, offset=offset, sort=sort, descending=descending
            )
            total = store.count_gate_decisions(active)
        return rows, total

    merged: list[GateDecision] = []
    total = 0
    for platform, db_owner, db_repo in _repo_targets(owner, repo):
        scoped = {**active, "platform": active.get("platform") or platform}
        # The store's own file already scopes owner/repo; passing them again
        # would filter on the namespaced spelling and match nothing.
        scoped.pop("owner", None)
        scoped.pop("repo", None)
        with open_analytics_store(db_owner, db_repo, platform=platform) as store:
            total += store.count_gate_decisions(scoped)
            merged.extend(_walk_repo(store, scoped, sort, descending))
    merged.sort(key=lambda row: _sort_key(row, sort), reverse=descending)
    return merged[offset : offset + limit], total


def _walk_repo(
    store: Any, filters: dict[str, Any], sort: str, descending: bool
) -> list[GateDecision]:
    """Every matching decision in one repository, paged rather than slurped."""
    out: list[GateDecision] = []
    page_offset = 0
    while page_offset < _MERGE_MAX_ROWS:
        page = store.list_gate_decisions(
            filters,
            limit=_MERGE_PAGE_SIZE,
            offset=page_offset,
            sort=sort,
            descending=descending,
        )
        if not page:
            return out
        out.extend(page)
        if len(page) < _MERGE_PAGE_SIZE:
            return out
        page_offset += _MERGE_PAGE_SIZE
    logger.warning(
        "Gate history walk hit the %s-row backstop; the page may be incomplete",
        _MERGE_MAX_ROWS,
    )
    return out


def summarize(*, filters: dict[str, Any] | None = None) -> list[dict]:
    """Counts by state and mode — the numbers a shadow rollout is run for."""
    active = dict(filters or {})
    owner = str(active.get("owner") or "")
    repo = str(active.get("repo") or "")

    if _postgres_url():
        with open_analytics_store("", "") as store:
            return store.summarize_gate_decisions(active)

    buckets: dict[tuple[str, str], dict[str, Any]] = {}
    for platform, db_owner, db_repo in _repo_targets(owner, repo):
        scoped = {**active, "platform": active.get("platform") or platform}
        scoped.pop("owner", None)
        scoped.pop("repo", None)
        with open_analytics_store(db_owner, db_repo, platform=platform) as store:
            for row in store.summarize_gate_decisions(scoped):
                key = (row["state"], row["mode"])
                bucket = buckets.setdefault(
                    key,
                    {
                        "state": row["state"],
                        "mode": row["mode"],
                        "count": 0,
                        "approved": 0,
                        "_risk_total": 0.0,
                    },
                )
                bucket["count"] += row["count"]
                bucket["approved"] += row["approved"]
                bucket["_risk_total"] += row["average_risk"] * row["count"]
    out = []
    for bucket in buckets.values():
        count = bucket["count"] or 1
        out.append(
            {
                "state": bucket["state"],
                "mode": bucket["mode"],
                "count": bucket["count"],
                "approved": bucket["approved"],
                "average_risk": round(bucket["_risk_total"] / count, 2),
            }
        )
    out.sort(key=lambda row: (row["state"], row["mode"]))
    return out


def get_decision(owner: str, repo: str, decision_id: int) -> GateDecision | None:
    platform = platform_for(owner, repo)
    with open_analytics_store(owner, repo, platform=platform) as store:
        return store.get_gate_decision_by_id(decision_id)


def list_overrides(
    owner: str, repo: str, *, decision_id: int = 0, limit: int = 100, offset: int = 0
) -> list[dict[str, Any]]:
    platform = platform_for(owner, repo)
    with open_analytics_store(owner, repo, platform=platform) as store:
        return store.list_gate_overrides(decision_id=decision_id, limit=limit, offset=offset)
