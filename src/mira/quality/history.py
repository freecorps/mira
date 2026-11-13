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


# ------------------------------------------------------------ peer recall


def peer_threads(
    *,
    owner: str = "",
    repo: str = "",
    bot: str = "",
    category: str = "",
    flagged: bool | None = None,
    addressed: bool | None = None,
    since: float = 0.0,
    limit: int = 100_000,
) -> list[Any]:
    """Peer-thread rows across every store, newest first."""
    rows: list[Any] = []
    for platform, db_owner, db_repo in _targets(owner, repo):
        with open_quality_store(_public_owner(db_owner), db_repo, platform) as store:
            for row in store.list_peer_threads(
                bot=bot,
                category=category,
                flagged=flagged,
                addressed=addressed,
                since=since,
                limit=limit,
            ):
                row.owner = _public_owner(row.owner) or _public_owner(db_owner)
                row.repo = row.repo or db_repo
                rows.append(row)
    if owner:
        rows = [r for r in rows if r.owner == owner]
    if repo:
        rows = [r for r in rows if r.repo == repo]
    rows.sort(key=lambda r: (r.pr_closed_at, r.pr_number), reverse=True)
    return rows[:limit]


def _peer_counts(rows: list[Any]) -> dict[str, Any]:
    """Threads, overlap and recall for one group of peer threads.

    ``recall`` is over *addressed* threads only — the ones the author fixed,
    so the ones that pointed at something real. ``overlap`` is over every
    thread, noise included, and is shown for context.
    """
    threads = len(rows)
    flagged = sum(1 for r in rows if r.mira_flagged)
    addressed = sum(1 for r in rows if r.addressed)
    caught = sum(1 for r in rows if r.addressed and r.mira_flagged)
    return {
        "threads": threads,
        "flagged": flagged,
        "addressed": addressed,
        "addressed_flagged": caught,
        "missed_addressed": addressed - caught,
        "overlap": flagged / threads if threads else None,
        "recall": caught / addressed if addressed else None,
    }


def _week_start(epoch: float) -> str:
    from datetime import UTC, datetime, timedelta

    day = datetime.fromtimestamp(epoch or 0, tz=UTC).date()
    return (day - timedelta(days=day.weekday())).isoformat()


def summarize_peer_rows(rows: list[Any]) -> dict[str, Any]:
    """Recall against each peer bot: overall, per repository, per category, per week."""

    def grouped(key: Any) -> dict[Any, list[Any]]:
        groups: dict[Any, list[Any]] = {}
        for row in rows:
            groups.setdefault(key(row), []).append(row)
        return groups

    bots = [{"bot": bot, **_peer_counts(group)} for bot, group in grouped(lambda r: r.bot).items()]
    bots.sort(key=lambda r: (-r["threads"], r["bot"]))
    repos = [
        {"platform": platform, "owner": owner, "repo": repo, "bot": bot, **_peer_counts(group)}
        for (platform, owner, repo, bot), group in grouped(
            lambda r: (r.platform, r.owner, r.repo, r.bot)
        ).items()
    ]
    repos.sort(key=lambda r: (r["owner"], r["repo"], -r["threads"], r["bot"]))
    categories = [
        {"category": category, **_peer_counts(group)}
        for category, group in grouped(lambda r: r.category).items()
    ]
    categories.sort(key=lambda r: (-r["missed_addressed"], -r["threads"], r["category"]))
    weeks = [
        {"week": week, "bot": bot, **_peer_counts(group)}
        for (week, bot), group in grouped(lambda r: (_week_start(r.pr_closed_at), r.bot)).items()
    ]
    weeks.sort(key=lambda r: (r["week"], r["bot"]))
    learning: dict[str, int] = {}
    for row in rows:
        if row.addressed and not row.mira_flagged:
            state = row.learning_state or "pending"
            learning[state] = learning.get(state, 0) + 1
    return {
        "totals": _peer_counts(rows),
        "bots": bots,
        "repos": repos,
        "categories": categories,
        "weeks": weeks,
        "learning": learning,
    }


def peer_recall_summary(
    *, owner: str = "", repo: str = "", bot: str = "", since: float = 0.0
) -> dict[str, Any]:
    return summarize_peer_rows(peer_threads(owner=owner, repo=repo, bot=bot, since=since))


def list_missed_peer_threads(
    *,
    owner: str = "",
    repo: str = "",
    bot: str = "",
    category: str = "",
    since: float = 0.0,
    limit: int = 50,
    offset: int = 0,
) -> list[dict[str, Any]]:
    """Missed-and-addressed threads, newest first: what Mira should have said."""
    rows = peer_threads(
        owner=owner,
        repo=repo,
        bot=bot,
        category=category,
        flagged=False,
        addressed=True,
        since=since,
        limit=limit + offset,
    )
    return [row.to_dict() for row in rows[offset : offset + limit]]
