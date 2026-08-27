"""Reading autofix jobs back, across whichever store backs the install.

Postgres keeps every repository in one table, so a filter and a ``LIMIT``
answer any question directly. SQLite keeps a file per repository, so the same
question means visiting each file and merging. Both paths page inside the store
and only ever hold one page in memory — a job history is exactly the kind of
table that grows without anyone noticing, and the small-board profile cannot
afford to materialise it.

The platform-resolution and repository-walking primitives are shared with the
Phase 3 analytics and the Phase 4 gate history rather than re-implemented: they
solve the same problem (the same repository name can exist on three platforms
and its rows live under a namespaced owner), and a third implementation would
eventually disagree with the other two.
"""

from __future__ import annotations

import logging
from typing import Any

from mira.autofix.models import AutofixAttempt, AutofixJob
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
    "get_job",
    "get_job_by_key",
    "list_attempts",
    "list_jobs",
    "platform_for",
    "summarize",
]

# Rows pulled per round trip while merging a repository's jobs. A batch size,
# not a ceiling — the walk keeps paging until the repository is done.
_MERGE_PAGE_SIZE = 200

# Backstop on one repository's walk, so a pathological history cannot spin
# forever. Crossing it is logged, never silent.
_MERGE_MAX_ROWS = 20_000


def _sort_key(job: AutofixJob, sort: str) -> Any:
    if sort == "updated_at":
        return job.updated_at
    if sort == "pr_number":
        return job.pr_number
    if sort == "state":
        return job.state
    if sort == "attempts":
        return job.attempts
    return job.created_at


def list_jobs(
    *,
    filters: dict[str, Any] | None = None,
    limit: int = 50,
    offset: int = 0,
    sort: str = "created_at",
    descending: bool = True,
) -> tuple[list[AutofixJob], int]:
    """One page of jobs plus the total that matched."""
    active = dict(filters or {})
    owner = str(active.get("owner") or "")
    repo = str(active.get("repo") or "")

    if _postgres_url():
        # One table for the whole install: the org-wide handle sees everything
        # and the filters do the scoping.
        with open_analytics_store("", "") as store:
            rows = store.list_autofix_jobs(
                active, limit=limit, offset=offset, sort=sort, descending=descending
            )
            total = store.count_autofix_jobs(active)
        return rows, total

    merged: list[AutofixJob] = []
    total = 0
    for platform, db_owner, db_repo in _repo_targets(owner, repo):
        scoped = {**active, "platform": active.get("platform") or platform}
        # The store's own file already scopes owner/repo; passing them again
        # would filter on the namespaced spelling and match nothing.
        scoped.pop("owner", None)
        scoped.pop("repo", None)
        with open_analytics_store(db_owner, db_repo, platform=platform) as store:
            total += store.count_autofix_jobs(scoped)
            merged.extend(_walk_repo(store, scoped, sort, descending))
    merged.sort(key=lambda row: _sort_key(row, sort), reverse=descending)
    return merged[offset : offset + limit], total


def _walk_repo(
    store: Any, filters: dict[str, Any], sort: str, descending: bool
) -> list[AutofixJob]:
    """Every matching job in one repository, paged rather than slurped."""
    out: list[AutofixJob] = []
    page_offset = 0
    while page_offset < _MERGE_MAX_ROWS:
        page = store.list_autofix_jobs(
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
        "Autofix history walk hit the %s-row backstop; the page may be incomplete",
        _MERGE_MAX_ROWS,
    )
    return out


def summarize(*, filters: dict[str, Any] | None = None) -> list[dict]:
    """Counts by state and mode — the numbers a rollout is watched through."""
    active = dict(filters or {})
    owner = str(active.get("owner") or "")
    repo = str(active.get("repo") or "")

    if _postgres_url():
        with open_analytics_store("", "") as store:
            return store.summarize_autofix_jobs(active)

    buckets: dict[tuple[str, str], dict[str, Any]] = {}
    for platform, db_owner, db_repo in _repo_targets(owner, repo):
        scoped = {**active, "platform": active.get("platform") or platform}
        scoped.pop("owner", None)
        scoped.pop("repo", None)
        with open_analytics_store(db_owner, db_repo, platform=platform) as store:
            for row in store.summarize_autofix_jobs(scoped):
                key = (row["state"], row["mode"])
                bucket = buckets.setdefault(
                    key,
                    {
                        "state": row["state"],
                        "mode": row["mode"],
                        "count": 0,
                        "attempts": 0,
                        "opened": 0,
                    },
                )
                bucket["count"] += row["count"]
                bucket["attempts"] += row["attempts"]
                bucket["opened"] += row["opened"]
    out = list(buckets.values())
    out.sort(key=lambda row: (row["state"], row["mode"]))
    return out


def get_job(owner: str, repo: str, job_id: int) -> AutofixJob | None:
    platform = platform_for(owner, repo)
    with open_analytics_store(owner, repo, platform=platform) as store:
        return store.get_autofix_job_by_id(job_id)


def get_job_by_key(owner: str, repo: str, job_key: str) -> AutofixJob | None:
    platform = platform_for(owner, repo)
    with open_analytics_store(owner, repo, platform=platform) as store:
        return store.get_autofix_job(job_key)


def list_attempts(
    owner: str, repo: str, *, job_key: str = "", limit: int = 100, offset: int = 0
) -> list[AutofixAttempt]:
    platform = platform_for(owner, repo)
    with open_analytics_store(owner, repo, platform=platform) as store:
        return store.list_autofix_attempts(job_key=job_key, limit=limit, offset=offset)
