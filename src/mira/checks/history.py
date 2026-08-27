"""Reading check runs back, across whichever store backs the install.

Postgres keeps every repository in one table, so a filter and a ``LIMIT``
answer any question directly. SQLite keeps a file per repository, so the same
question means visiting each file and merging. Both paths page inside the store
and only ever hold one page in memory — a check history grows by one row per
check per push, which is the fastest-growing table this codebase has, and the
Orange Pi profile cannot afford to materialise it.

The platform-resolution and repository-walking primitives are shared with the
Phase 3 analytics and the Phase 4 gate history rather than re-implemented: they
solve the same problem (the same repository name can exist on three platforms
and its rows live under a namespaced owner), and a third implementation would
eventually disagree with the other two.
"""

from __future__ import annotations

import logging
from typing import Any

from mira.checks.models import CheckResult, CheckRun
from mira.feedback.analytics import (
    PlatformResolutionError,
    _postgres_url,
    _repo_targets,
    open_analytics_store,
)
from mira.feedback.analytics import (
    _platform_for as platform_for,
)

logger = logging.getLogger(__name__)

__all__ = [
    "PlatformResolutionError",
    "get_run",
    "list_results",
    "list_runs",
    "platform_for",
    "summarize",
]

# Rows pulled per round trip while merging a repository's runs. A batch size,
# not a ceiling — the walk keeps paging until the repository is done.
_MERGE_PAGE_SIZE = 200

# Backstop on one repository's walk, so a pathological history cannot spin
# forever. Crossing it is logged, never silent.
_MERGE_MAX_ROWS = 20_000


def _run_sort_key(run: CheckRun, sort: str) -> Any:
    if sort == "pr_number":
        return run.inputs.pr_number
    if sort == "verdict":
        return run.verdict
    if sort == "duration_seconds":
        return run.duration_seconds
    return run.created_at


def _result_sort_key(result: CheckResult, sort: str) -> Any:
    if sort == "check_id":
        return result.check_id
    if sort == "state":
        return result.state
    if sort == "duration_seconds":
        return result.duration_seconds
    return result.created_at


def _walk(store: Any, method: str, filters: dict[str, Any], **kwargs: Any) -> list[Any]:
    """Every matching row in one repository, paged rather than slurped."""
    out: list[Any] = []
    page_offset = 0
    reader = getattr(store, method)
    while page_offset < _MERGE_MAX_ROWS:
        page = reader(filters, limit=_MERGE_PAGE_SIZE, offset=page_offset, **kwargs)
        if not page:
            return out
        out.extend(page)
        if len(page) < _MERGE_PAGE_SIZE:
            return out
        page_offset += _MERGE_PAGE_SIZE
    logger.warning(
        "Check history walk hit the %s-row backstop; the page may be incomplete",
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
    with_results: bool = False,
) -> tuple[list[CheckRun], int]:
    """One page of runs plus the total that matched."""
    active = dict(filters or {})
    owner = str(active.get("owner") or "")
    repo = str(active.get("repo") or "")

    if _postgres_url():
        with open_analytics_store("", "") as store:
            rows = store.list_check_runs(
                active,
                limit=limit,
                offset=offset,
                sort=sort,
                descending=descending,
                with_results=with_results,
            )
            total = store.count_check_runs(active)
        return rows, total

    merged: list[CheckRun] = []
    total = 0
    for platform, db_owner, db_repo in _repo_targets(owner, repo):
        scoped = {**active, "platform": active.get("platform") or platform}
        # The store's own file already scopes owner/repo; passing them again
        # would filter on the namespaced spelling and match nothing.
        scoped.pop("owner", None)
        scoped.pop("repo", None)
        with open_analytics_store(db_owner, db_repo, platform=platform) as store:
            total += store.count_check_runs(scoped)
            merged.extend(
                _walk(
                    store,
                    "list_check_runs",
                    scoped,
                    sort=sort,
                    descending=descending,
                    with_results=with_results,
                )
            )
    merged.sort(key=lambda row: _run_sort_key(row, sort), reverse=descending)
    return merged[offset : offset + limit], total


def list_results(
    *,
    filters: dict[str, Any] | None = None,
    limit: int = 100,
    offset: int = 0,
    sort: str = "created_at",
    descending: bool = True,
) -> tuple[list[CheckResult], int]:
    """One page of individual results plus the total that matched.

    The history view a team actually uses: "show me every time
    ``native.tests`` objected", or "every result this month that was an
    infrastructure error rather than a finding".
    """
    active = dict(filters or {})
    owner = str(active.get("owner") or "")
    repo = str(active.get("repo") or "")

    if _postgres_url():
        with open_analytics_store("", "") as store:
            rows = store.list_check_results(
                active, limit=limit, offset=offset, sort=sort, descending=descending
            )
            total = store.count_check_results(active)
        return rows, total

    merged: list[CheckResult] = []
    total = 0
    for platform, db_owner, db_repo in _repo_targets(owner, repo):
        scoped = {**active, "platform": active.get("platform") or platform}
        scoped.pop("owner", None)
        scoped.pop("repo", None)
        with open_analytics_store(db_owner, db_repo, platform=platform) as store:
            total += store.count_check_results(scoped)
            merged.extend(
                _walk(store, "list_check_results", scoped, sort=sort, descending=descending)
            )
    merged.sort(key=lambda row: _result_sort_key(row, sort), reverse=descending)
    return merged[offset : offset + limit], total


def summarize(*, filters: dict[str, Any] | None = None) -> list[dict]:
    """Counts by check and state — which check is noisy, which cannot run."""
    active = dict(filters or {})
    owner = str(active.get("owner") or "")
    repo = str(active.get("repo") or "")

    if _postgres_url():
        with open_analytics_store("", "") as store:
            return store.summarize_check_results(active)

    buckets: dict[tuple[str, str, str, str], dict[str, Any]] = {}
    for platform, db_owner, db_repo in _repo_targets(owner, repo):
        scoped = {**active, "platform": active.get("platform") or platform}
        scoped.pop("owner", None)
        scoped.pop("repo", None)
        with open_analytics_store(db_owner, db_repo, platform=platform) as store:
            for row in store.summarize_check_results(scoped):
                key = (row["check_id"], row["origin"], row["state"], row["mode"])
                bucket = buckets.setdefault(
                    key,
                    {
                        "check_id": row["check_id"],
                        "origin": row["origin"],
                        "state": row["state"],
                        "mode": row["mode"],
                        "count": 0,
                        "_duration_total": 0.0,
                    },
                )
                bucket["count"] += row["count"]
                bucket["_duration_total"] += row["average_duration"] * row["count"]
    out = []
    for bucket in buckets.values():
        count = bucket["count"] or 1
        out.append(
            {
                "check_id": bucket["check_id"],
                "origin": bucket["origin"],
                "state": bucket["state"],
                "mode": bucket["mode"],
                "count": bucket["count"],
                "average_duration": round(bucket["_duration_total"] / count, 4),
            }
        )
    out.sort(key=lambda row: (row["check_id"], row["state"]))
    return out


def get_run(owner: str, repo: str, run_id: int) -> CheckRun | None:
    platform = platform_for(owner, repo)
    with open_analytics_store(owner, repo, platform=platform) as store:
        return store.get_check_run_by_id(run_id)
