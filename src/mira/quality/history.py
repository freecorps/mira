"""Reading backtests and escaped bugs back, across whichever store backs the install.

The same split the gate history makes: Postgres holds every repository in one
table, so the org-wide handle answers directly; SQLite keeps a file per
repository, so the answer is a walk over the files. The walk reuses the Phase 3
analytics primitives rather than re-implementing repository discovery.
"""

from __future__ import annotations

from typing import Any

from mira.feedback.analytics import _postgres_url, _repo_targets
from mira.quality.store import open_quality_store


def _targets(owner: str, repo: str) -> list[tuple[str, str, str]]:
    if _postgres_url():
        return [("github", "", "")]
    return _repo_targets(owner, repo)


def _public_owner(db_owner: str) -> str:
    return db_owner.split("/", 1)[1] if db_owner.startswith("_") else db_owner


def recall_summary(*, owner: str = "", repo: str = "", since: float = 0.0) -> dict[str, Any]:
    """Caught vs escaped, per repository and in total."""
    repos: list[dict[str, Any]] = []
    totals = {"caught": 0, "missed": 0, "total": 0}
    if _postgres_url():
        # One table: group by repository in a single pass over the org-wide handle.
        with open_quality_store(owner, repo) as store:
            bugs = store.list_escaped_bugs(since=since, limit=100_000)
        grouped: dict[tuple[str, str, str], dict[tuple[str, int], bool]] = {}
        for bug in bugs:
            key = (bug.platform, bug.owner, bug.repo)
            incident = (bug.fix_ref, bug.original_pr_number)
            seen = grouped.setdefault(key, {})
            seen[incident] = seen.get(incident, False) or bug.flagged
        for (platform, db_owner, db_repo), incidents in grouped.items():
            caught = sum(1 for flagged in incidents.values() if flagged)
            repos.append(
                _row(platform, _public_owner(db_owner), db_repo, caught, len(incidents) - caught)
            )
    else:
        for platform, db_owner, db_repo in _targets(owner, repo):
            with open_quality_store(_public_owner(db_owner), db_repo, platform) as store:
                counts = store.escaped_bug_counts(since=since)
            if counts["total"]:
                repos.append(
                    _row(
                        platform,
                        _public_owner(db_owner),
                        db_repo,
                        counts["caught"],
                        counts["missed"],
                    )
                )
    for row in repos:
        totals["caught"] += row["caught"]
        totals["missed"] += row["missed"]
        totals["total"] += row["total"]
    repos.sort(key=lambda r: (-r["total"], r["owner"], r["repo"]))
    return {
        "repos": repos,
        "totals": {**totals, "recall": _recall(totals["caught"], totals["total"])},
    }


def _recall(caught: int, total: int) -> float | None:
    return caught / total if total else None


def _row(platform: str, owner: str, repo: str, caught: int, missed: int) -> dict[str, Any]:
    total = caught + missed
    return {
        "platform": platform,
        "owner": owner,
        "repo": repo,
        "caught": caught,
        "missed": missed,
        "total": total,
        "recall": _recall(caught, total),
    }


def list_escaped_bugs(
    *,
    owner: str = "",
    repo: str = "",
    flagged: bool | None = None,
    since: float = 0.0,
    limit: int = 50,
    offset: int = 0,
) -> list[dict[str, Any]]:
    """Newest first, across repositories."""
    rows: list[dict[str, Any]] = []
    for platform, db_owner, db_repo in _targets(owner, repo):
        with open_quality_store(_public_owner(db_owner), db_repo, platform) as store:
            for bug in store.list_escaped_bugs(
                flagged=flagged, since=since, limit=limit + offset, offset=0
            ):
                item = bug.to_dict()
                item["owner"] = _public_owner(bug.owner) or _public_owner(db_owner)
                item["repo"] = bug.repo or db_repo
                rows.append(item)
    if owner:
        rows = [r for r in rows if r["owner"] == owner]
    if repo:
        rows = [r for r in rows if r["repo"] == repo]
    rows.sort(key=lambda r: r["detected_at"], reverse=True)
    return rows[offset : offset + limit]


def list_backtest_runs(
    *, owner: str = "", repo: str = "", limit: int = 50, offset: int = 0
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for platform, db_owner, db_repo in _targets(owner, repo):
        with open_quality_store(_public_owner(db_owner), db_repo, platform) as store:
            for run in store.list_backtest_runs(limit=limit + offset):
                run["owner"] = _public_owner(run["owner"]) or _public_owner(db_owner)
                run["repo"] = run["repo"] or db_repo
                rows.append(run)
    if owner:
        rows = [r for r in rows if r["owner"] == owner]
    if repo:
        rows = [r for r in rows if r["repo"] == repo]
    rows.sort(key=lambda r: r["created_at"], reverse=True)
    return rows[offset : offset + limit]


def get_backtest_run(owner: str, repo: str, run_id: str, platform: str = "github") -> Any:
    with open_quality_store(owner, repo, platform) as store:
        run = store.get_backtest_run(run_id)
    if run is not None:
        run["owner"] = _public_owner(run["owner"]) or owner
        run["repo"] = run["repo"] or repo
    return run
