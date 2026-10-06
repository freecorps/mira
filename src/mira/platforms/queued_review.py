"""Running one queued review request: the part every platform shares.

The queue (``mira.core.review_queue``) decides *when* a request runs; this
decides *what* running it means, through the provider abstraction only:

1. Read the pull request. If its head moved since the request was queued — a
   push whose webhook was lost in a restart, or a Re-run pressed on an old
   commit — close the old head's check as superseded and review the new one.
2. For a `synchronize`, compare the pull request's own diff with the one the
   last published verdict was decided on. Same patch id: the push was a
   rebase that changed nothing of this pull request's, so the verdict is
   republished on the new head and no model is called.
3. Otherwise, review.
"""

from __future__ import annotations

import dataclasses
import logging
from typing import Any

from mira.config import MiraConfig, load_config
from mira.core.commit_status import (
    STATUS_CONTEXT,
    CommitStatus,
    carried_over_status,
    superseded_status,
)
from mira.core.patch_id import diff_patch_id
from mira.core.review_queue import RunResult
from mira.dashboard.db import ReviewRequest
from mira.models import PRInfo

logger = logging.getLogger(__name__)


def pr_info_for(request: ReviewRequest) -> PRInfo:
    """Enough of a pull request to publish a status against the request's head."""
    return PRInfo(
        title=request.pr_title,
        description="",
        base_branch=request.base_ref,
        head_branch=request.head_ref,
        url=request.pr_url,
        number=request.pr_number,
        owner=request.owner,
        repo=request.repo,
        head_sha=request.head_sha,
        platform=request.platform,
    )


async def publish_status(provider: Any, pr_info: PRInfo, status: CommitStatus) -> bool:
    """Publish `mira/review` on ``pr_info.head_sha``. Never raises."""
    publish = getattr(provider, "publish_review_status", None)
    if not callable(publish):
        return True
    try:
        await publish(
            pr_info,
            context=STATUS_CONTEXT,
            state=status.state,
            title=status.title,
            summary=status.summary,
        )
    except Exception as exc:  # noqa: BLE001 - announcing is never fatal
        logger.warning("Could not publish %r on %s: %s", status.title, pr_info.url, exc)
        return False
    return True


async def carry_over_unchanged_rebase(
    provider: Any,
    pr_info: PRInfo,
    *,
    config: MiraConfig,
    db: Any,
) -> tuple[bool, bool]:
    """Republish the last verdict when the pull request's own diff is unchanged.

    Returns ``(carried, settled)``: whether the review can be skipped, and
    whether the carried-over check reached the platform.
    """
    previous = db.get_pr_review_verdict(
        pr_info.owner, pr_info.repo, pr_info.number, platform=pr_info.platform
    )
    if (
        previous is None
        or not previous.patch_id
        or not previous.state
        or not pr_info.head_sha
        or previous.head_sha == pr_info.head_sha
    ):
        return False, False
    diff_text = await provider.get_pr_diff(pr_info)
    if diff_patch_id(diff_text) != previous.patch_id:
        return False, False

    settled = True
    if config.review.status.enabled:
        status = carried_over_status(
            previous.state, previous.title, previous.summary, previous.head_sha
        )
        settled = await publish_status(provider, pr_info, status)
    db.set_last_reviewed_sha(
        pr_info.owner, pr_info.repo, pr_info.number, pr_info.head_sha, platform=pr_info.platform
    )
    db.set_pr_review_verdict(dataclasses.replace(previous, head_sha=pr_info.head_sha))
    logger.info(
        "%s: rebased without changes since %s; previous review carried over",
        pr_info.url,
        previous.head_sha[:12],
    )
    return True, settled


async def execute_review_request(
    provider: Any,
    request: ReviewRequest,
    *,
    bot_name: str,
    bot_identity: str | None = None,
    db: Any = None,
    config: MiraConfig | None = None,
) -> RunResult:
    """Run one queued request to its end. Raises only before the review starts."""
    from mira.platforms.handlers import run_gate_evaluation, run_pr_review

    if db is None:
        from mira.dashboard.api import _app_db as db
    config = config or load_config()
    status_on = bool(config.review.status.enabled)

    pr_info = await provider.get_pr_info(request.pr_url)
    if pr_info.head_sha and request.head_sha and pr_info.head_sha != request.head_sha:
        if status_on and request.check_state in ("queued", "running"):
            restarted = request.attempts > 1 or request.reason == "recovered"
            await publish_status(
                provider,
                dataclasses.replace(pr_info, head_sha=request.head_sha),
                superseded_status(restarted=restarted),
            )
        logger.info(
            "%s moved from %s to %s while queued; reviewing the new head",
            pr_info.url,
            request.head_sha[:12],
            pr_info.head_sha[:12],
        )
    if pr_info.head_sha and pr_info.head_sha != request.head_sha:
        db.update_review_request(request.id, head_sha=pr_info.head_sha)
        request.head_sha = pr_info.head_sha

    if (
        request.reason == "synchronize"
        and not request.full_review
        and config.review.queue.carry_over_unchanged_rebase
    ):
        carried, settled = await carry_over_unchanged_rebase(
            provider, pr_info, config=config, db=db
        )
        if carried:
            # The gate's decision belongs to a head, and this head has none
            # yet. Re-deciding it costs no model call.
            try:
                await run_gate_evaluation(
                    provider,
                    request.owner,
                    request.repo,
                    request.pr_number,
                    pr_info.url,
                    bot_name,
                    platform=request.platform,
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning("Gate re-evaluation after carry-over failed: %s", exc)
            return RunResult("carried_over", status_settled=settled)

    state: dict[str, Any] = {}
    try:
        ran = await run_pr_review(
            provider,
            request.owner,
            request.repo,
            request.pr_number,
            pr_info.url or request.pr_url,
            request.is_private,
            bot_name,
            platform=request.platform,
            pr_title=request.pr_title or pr_info.title,
            bot_identity=bot_identity,
            full_review=request.full_review,
            review_state=state,
        )
    except Exception as exc:
        logger.exception("Review of %s failed", pr_info.url)
        from mira.outbound_webhooks import REVIEW_FAILED, dispatch_event

        await dispatch_event(
            REVIEW_FAILED,
            {
                "repo": f"{request.owner}/{request.repo}",
                "pr_url": pr_info.url,
                "error": str(exc),
            },
        )
        return RunResult("failed", status_settled=bool(state.get("status_settled")) or not status_on)
    if not ran:
        return RunResult("busy", status_settled=False)
    return RunResult("reviewed", status_settled=bool(state.get("status_settled")) or not status_on)
