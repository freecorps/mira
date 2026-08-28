"""Recording what Mira watched happen, so it can rank on it later.

One event feeds this: a pull request merging. At that moment Mira knows the
files it changed, who wrote it and who reviewed it, all from a webhook the
platform signed — which makes it the only identity evidence available on every
platform, and better evidence than a commit's author fields on any of them.

Three decisions are worth stating.

**Nothing is recorded for a repository where triage is off.** Who worked on
which file is data about people, and collecting it "in case it is useful later"
is exactly the habit that makes an install untrustworthy. Turning triage on
starts the collection; a repository that never turns it on never has the rows.

**It is recorded at merge, not at review.** An abandoned pull request is not
evidence that anybody knows the file, and a review left on a change that was
then thrown away is a weaker signal than the ranking would imply.

**A failure here is silent and total.** This runs after a merge, on a path that
has already done everything that matters. Nothing about the merge, the learning
pass or the review depends on it, so it logs and returns rather than raising.
"""

from __future__ import annotations

import contextlib
import logging
import time
from typing import Any

from mira.checks.native import paths as pathkind

logger = logging.getLogger(__name__)

# Files per merged pull request that leave rows. A 900-file refactor tells you
# almost nothing about who owns any one of those files, and would write 900
# rows per person to say it.
MAX_PATHS_PER_EVENT = 200

# Review states that count as having reviewed. A dismissed or pending review is
# not a review anybody did.
_REVIEW_STATES = frozenset({"approved", "changes_requested", "commented"})


def _paths_from(changes: list[Any]) -> list[str]:
    """The paths worth attributing: real files, capped, in a stable order."""
    paths = [
        change.path
        for change in changes
        if getattr(change, "path", "") and not pathkind.is_generated(change.path)
    ]
    paths.sort()
    return paths[:MAX_PATHS_PER_EVENT]


def rows_for(
    *,
    platform: str,
    paths: list[str],
    author: str,
    reviewers: dict[str, str],
    pr_number: int,
    pr_url: str,
    event_at: float,
) -> list[dict[str, Any]]:
    """Turn one merged pull request into contribution rows.

    Pure, so the shape of what gets written is testable without a provider, a
    store or a webhook. The author is recorded once per path as an authorship;
    each reviewer once per path as a review. Somebody who both wrote and
    reviewed — which happens, on a pull request opened by a bot and finished by
    a human — legitimately gets both.
    """
    rows: list[dict[str, Any]] = []
    author = (author or "").strip().lower()
    for path in paths:
        if author:
            rows.append(
                {
                    "platform": platform,
                    "path": path,
                    "identity": author,
                    "role": "authored",
                    "source": "pull_request",
                    "reference": str(pr_number),
                    "url": pr_url,
                    "event_at": event_at,
                }
            )
        for login, state in sorted(reviewers.items()):
            identity = (login or "").strip().lower()
            if not identity or identity == author:
                continue
            if (state or "").strip().lower() not in _REVIEW_STATES:
                continue
            rows.append(
                {
                    "platform": platform,
                    "path": path,
                    "identity": identity,
                    "role": "reviewed",
                    "source": "review",
                    "reference": str(pr_number),
                    "url": pr_url,
                    "event_at": event_at,
                }
            )
    return rows


async def record_merged_pull_request(
    provider: Any,
    pr_info: Any,
    *,
    config: Any = None,
    store: Any = None,
    now: float = 0.0,
) -> int:
    """Record one merged pull request as path history. Returns rows written."""
    from mira.config import load_config
    from mira.triage.policy import resolve_policy

    config = config or load_config()
    policy = resolve_policy(config.triage, pr_info.owner, pr_info.repo)
    if not policy.active:
        return 0

    platform = str(getattr(pr_info, "platform", "github") or "github")
    try:
        changes = list(await provider.get_pr_change_stats(pr_info))
    except Exception as exc:  # noqa: BLE001 - nothing downstream depends on this
        logger.debug("Triage could not read changed files for %s: %s", pr_info.url, exc)
        return 0
    paths = _paths_from(changes)
    if not paths:
        return 0

    reviewers: dict[str, str] = {}
    try:
        reviewers = dict(await provider.get_review_states(pr_info))
    except Exception as exc:  # noqa: BLE001 - authorship still gets recorded
        logger.debug("Triage could not read review states for %s: %s", pr_info.url, exc)

    rows = rows_for(
        platform=platform,
        paths=paths,
        author=str(getattr(pr_info, "author", "") or ""),
        reviewers=reviewers,
        pr_number=int(getattr(pr_info, "number", 0) or 0),
        pr_url=str(getattr(pr_info, "url", "") or ""),
        # Recorded as "now" because this runs on the merge event itself. A
        # provider-reported merge timestamp would be better and not every
        # provider has one; being wrong by the length of a webhook queue does
        # not change a ranking measured in days.
        event_at=now or time.time(),
    )
    if not rows:
        return 0

    owned = store is None
    if store is None:
        from mira.index.store import IndexStore

        try:
            store = IndexStore.open(pr_info.owner, pr_info.repo, platform=platform)
        except Exception as exc:  # noqa: BLE001
            logger.debug("Triage could not open a store for %s: %s", pr_info.url, exc)
            return 0
    try:
        written = int(store.record_path_contributions(rows))
    except Exception as exc:  # noqa: BLE001
        logger.debug("Triage could not record path history for %s: %s", pr_info.url, exc)
        return 0
    finally:
        if owned:
            # Closing is best effort: the rows are already written, and a
            # handle that will not close is not a reason to fail a caller.
            with contextlib.suppress(Exception):
                store.close()
    logger.debug("Triage recorded %d path contribution(s) for %s", written, pr_info.url)
    return written
