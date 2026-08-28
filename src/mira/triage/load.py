"""How much review is already waiting on each person.

A dampener, never a filter. The most qualified reviewer is still the most
qualified reviewer when they are busy — what changes is that they stop being
the *only* name on the list, which is the whole point: a suggestion feature
that routes every pull request to the same two people has made the bottleneck
worse and called it automation.

Load is counted install-wide rather than per repository, because a person's
capacity is not per repository. It counts pull requests that are open and where
they have been asked and have not yet answered; a review they have already left
is not work waiting on them.

An unreadable load table does not invalidate anything. Every score is simply
computed without the dampener, and the run records that it was — because a
ranking that silently stopped balancing looks exactly like a ranking that had
nothing to balance.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class LoadOutcome:
    counts: dict[str, int] = field(default_factory=dict)
    available: bool = True
    detail: str = ""


def _normalize(identity: str) -> str:
    return (identity or "").strip().lstrip("@").lower()


def from_rows(
    rows: list[dict[str, Any]], *, exclude_pr: tuple[str, str, int] | None = None
) -> dict[str, int]:
    """Count open, unanswered review requests per reviewer.

    ``exclude_pr`` drops the pull request being triaged. Counting it would
    penalise somebody for the very review this run is about to suggest they
    do, which would make the suggestion less likely the second time it ran.
    """
    counts: dict[str, int] = {}
    for row in rows:
        if float(row.get("responded_at") or 0.0) > 0:
            continue
        reviewer = _normalize(str(row.get("reviewer") or ""))
        if not reviewer:
            continue
        if exclude_pr is not None:
            owner, repo, number = exclude_pr
            if (
                str(row.get("owner") or "").lower() == owner.lower()
                and str(row.get("repo") or "").lower() == repo.lower()
                and int(row.get("number") or 0) == int(number)
            ):
                continue
        counts[reviewer] = counts.get(reviewer, 0) + 1
    return counts


def current(*, owner: str = "", repo: str = "", pr_number: int = 0) -> LoadOutcome:
    """Open review load per person, or an outcome saying it could not be read."""
    try:
        from mira.dashboard.api import _app_db
    except Exception as exc:  # noqa: BLE001 - only an unconfigured install
        return LoadOutcome(available=False, detail=f"the review database is unavailable: {exc}")

    if _app_db is None:
        return LoadOutcome(available=False, detail="no review database is configured")
    try:
        rows = _app_db.get_open_pr_reviewers()
    except Exception as exc:  # noqa: BLE001
        logger.debug("Triage could not read reviewer load: %s", exc)
        return LoadOutcome(available=False, detail=f"the review load could not be read: {exc}")

    exclude = (owner, repo, pr_number) if owner and repo and pr_number else None
    return LoadOutcome(counts=from_rows(rows, exclude_pr=exclude))
