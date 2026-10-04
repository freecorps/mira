"""What a merge records for the delivery analytics.

Runs in the background after a merge, on every platform, and never raises:
nothing about a merge depends on the heatmap or a DORA number.

Hotspots
    The first merge Mira sees for a repository pulls a bounded slice of the
    default branch's commit history (``analytics.hotspots.history_max_commits``)
    so the heatmap starts populated. Every later merge records its own file
    changes. The backfill already contains the merge that triggered it, so that
    merge is not recorded twice.

DORA
    The merged PR's row in ``pull_requests`` gets its base and default branch,
    labels and first-commit time — the fields lead time and change failure
    rate need. Platforms whose webhooks do not maintain that table (GitLab,
    Forgejo) get the row created here. In ``releases`` mode the platform's
    recent releases are synced as deployments.
"""

from __future__ import annotations

import logging
import time
from typing import Any

logger = logging.getLogger(__name__)

DAY = 86400.0


def _churn_rows(platform: str, reference: str, source: str, files: Any, at: float) -> list[dict]:
    return [
        {
            "platform": platform,
            "path": f.path,
            "reference": reference,
            "source": source,
            "additions": int(getattr(f, "added_lines", 0) or 0),
            "deletions": int(getattr(f, "deleted_lines", 0) or 0),
            "event_at": at,
        }
        for f in files or []
        if getattr(f, "path", "")
    ]


async def record_merge_churn(
    provider: Any,
    pr_info: Any,
    *,
    config: Any = None,
    store: Any = None,
    now: float = 0.0,
) -> int:
    """Record file churn for a merged PR (or backfill history). Returns rows written."""
    from mira.config import load_config

    config = config or load_config()
    cfg = config.analytics.hotspots
    if not cfg.enabled:
        return 0
    platform = str(getattr(pr_info, "platform", "github") or "github")
    now = now or time.time()

    owned = store is None
    if store is None:
        from mira.index.store import IndexStore

        store = IndexStore.open(pr_info.owner, pr_info.repo, platform=platform)
    try:
        written = 0
        if cfg.history_max_commits > 0 and not store.churn_history_fetched_at(platform):
            try:
                commits = await provider.get_commit_churn(
                    pr_info,
                    since=now - cfg.window_days * DAY,
                    max_commits=cfg.history_max_commits,
                )
            except Exception as exc:  # noqa: BLE001
                logger.debug("Churn history backfill failed for %s: %s", pr_info.url, exc)
                commits = None
            if commits is not None:
                for c in commits:
                    written += store.record_file_churn(
                        _churn_rows(platform, f"commit:{c.sha}", "commit", c.files, c.at or now)
                    )
                store.mark_churn_history_fetched(platform, commits=len(commits), at=now)
                if commits:
                    # The history already includes this merge.
                    return written
        try:
            changes = await provider.get_pr_change_stats(pr_info)
        except Exception as exc:  # noqa: BLE001
            logger.debug("Could not read changed files for %s: %s", pr_info.url, exc)
            return written
        written += store.record_file_churn(
            _churn_rows(platform, f"pr:{pr_info.number}", "pull_request", changes, now)
        )
        return written
    finally:
        if owned:
            store.close()


async def record_merge_delivery(
    provider: Any,
    pr_info: Any,
    *,
    config: Any = None,
    app_db: Any = None,
    now: float = 0.0,
) -> None:
    """Enrich the merged PR's ``pull_requests`` row and sync releases."""
    from mira.config import load_config

    config = config or load_config()
    cfg = config.analytics.dora
    if not cfg.enabled:
        return
    if app_db is None:
        from mira.dashboard.api import _app_db

        app_db = _app_db
    if app_db is None:
        return
    now = now or time.time()

    async def _try(coro: Any, default: Any) -> Any:
        try:
            return await coro
        except Exception as exc:  # noqa: BLE001
            logger.debug("Delivery enrichment read failed for %s: %s", pr_info.url, exc)
            return default

    first_commit = await _try(provider.get_pr_first_commit_at(pr_info), 0.0)
    default_branch = await _try(provider.get_default_branch(pr_info), "")
    labels = await _try(provider.get_pr_labels(pr_info), None)
    platform = str(getattr(pr_info, "platform", "github") or "github")
    merged_at = 0.0
    if platform != "github":
        # The platform's own merge time: the webhook may arrive late or be
        # redelivered, so "now" is only the fallback.
        read_landed_at = getattr(provider, "get_pr_landed_at", None)
        if read_landed_at is not None:
            merged_at = float(await _try(read_landed_at(pr_info), 0.0) or 0.0)
        merged_at = merged_at or now
    app_db.upsert_pull_request(
        pr_info.owner,
        pr_info.repo,
        int(pr_info.number),
        author=str(getattr(pr_info, "author", "") or ""),
        title=str(getattr(pr_info, "title", "") or ""),
        url=str(getattr(pr_info, "url", "") or ""),
        state="merged",
        updated_at=now,
        # GitHub's pull_request webhook already recorded the exact merge time;
        # elsewhere the platform's merged_at, else the merge event's clock.
        merged_at=merged_at,
        closed_at=merged_at,
        base_branch=str(getattr(pr_info, "base_branch", "") or ""),
        default_branch=str(default_branch or ""),
        labels=list(labels) if labels is not None else None,
        first_commit_at=float(first_commit or 0.0),
    )

    if cfg.deployment_source == "releases":
        releases = await _try(provider.list_deployment_releases(pr_info, since=now - 90 * DAY), [])
        for rel in releases:
            app_db.record_deployment(
                pr_info.owner, pr_info.repo, rel.tag, rel.at, kind=rel.kind, url=rel.url
            )


async def record_merge_analytics(provider: Any, pr_info: Any, *, config: Any = None) -> None:
    """Both of the above, each isolated from the other's failure."""
    try:
        await record_merge_churn(provider, pr_info, config=config)
    except Exception as exc:  # noqa: BLE001
        logger.debug("Hotspot churn not recorded for %s: %s", getattr(pr_info, "url", ""), exc)
    try:
        await record_merge_delivery(provider, pr_info, config=config)
    except Exception as exc:  # noqa: BLE001
        logger.debug("DORA enrichment not recorded for %s: %s", getattr(pr_info, "url", ""), exc)
