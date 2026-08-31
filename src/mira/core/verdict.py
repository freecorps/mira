"""Turn review findings into a platform review verdict.

The engine already knows whether a PR is safe — it counts blockers, clamps the
walkthrough confidence to "Do not merge", and verifies on later passes that the
findings were fixed. What it never did was *say so* in the only place the merge
box reads: the review event. This module maps a finished ``ReviewResult`` onto
``APPROVE`` / ``REQUEST_CHANGES``, and the engine submits it.

``review.verdict.mode`` decides how far this goes, and the two directions are
not symmetric. ``approve`` is the default: an approval adds a signal that a
human can ignore, dismiss or override, and it is the thing a reviewer wants
back from a bot that read the whole diff and found nothing. ``request_changes``
stays opt-in, because it *removes* the ability to merge until somebody
dismisses it.

Two conditions gate an approval — nothing above ``approve_max_severity``, and a
walkthrough confidence of at least ``approve_min_confidence`` — and every other
guard below resolves doubt to "say nothing". Silence is recoverable; a wrong
verdict is someone merging on Mira's word.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from mira.config import MiraConfig
from mira.models import PRInfo, ReviewResult, Severity

logger = logging.getLogger(__name__)

APPROVE = "APPROVE"
REQUEST_CHANGES = "REQUEST_CHANGES"


@dataclass
class Verdict:
    """A review event to submit, plus why we landed on it (for the log)."""

    event: str
    body: str
    reason: str


def _plural(n: int, word: str) -> str:
    return f"{n} {word}{'s' if n != 1 else ''}"


def decide_verdict(
    result: ReviewResult,
    config: MiraConfig,
    pr_info: PRInfo,
    bot_name: str,
    human_states: dict[str, str] | None = None,
) -> Verdict | None:
    """Decide the review event for a finished review, or ``None`` to stay quiet.

    ``human_states`` maps reviewer login → latest review state, used only to
    keep Mira from approving over a human who asked for changes.
    """
    cfg = config.review.verdict
    if cfg.mode == "off":
        return None

    ceiling = Severity.from_str(cfg.approve_max_severity)
    blockers = sum(1 for c in result.comments if c.severity == Severity.BLOCKER)
    warnings = sum(1 for c in result.comments if c.severity == Severity.WARNING)
    worst = max((c.severity for c in result.comments), default=None)

    if worst is not None and worst > ceiling:
        if cfg.mode != "request_changes":
            # Opted into approvals only — findings are already inline, and a
            # REQUEST_CHANGES the user didn't ask for would block their merge.
            return None
        counts = []
        if blockers:
            counts.append(_plural(blockers, "blocker"))
        if warnings:
            counts.append(_plural(warnings, "warning"))
        summary = " and ".join(counts) if counts else "findings"
        return Verdict(
            event=REQUEST_CHANGES,
            body=(
                f"🛑 **Mira requested changes** — found {summary} that should be "
                f"resolved before merge. Details are in the inline comments.\n\n"
                f"Push a fix and Mira will re-check on the next pass; once the "
                f"findings are verified as addressed this review is superseded "
                f"automatically."
            ),
            reason=f"worst severity {worst.name} exceeds ceiling {ceiling.name}",
        )

    # ── Approve path. Every guard below is a reason to stay silent instead. ──

    if result.skipped_reason:
        return None

    if cfg.require_all_files_reviewed and result.skipped_paths:
        # The diff blew past max_diff_size and files were dropped by priority.
        # Approving a PR Mira only half-read is the worst failure mode here.
        return None

    if bot_name:
        authored_by_self = pr_info.author.lower() in {
            bot_name.lower(),
            f"{bot_name}[bot]".lower(),
        }
        if authored_by_self:
            return None

    score = getattr(getattr(result.walkthrough, "confidence_score", None), "score", None)
    if cfg.approve_min_confidence and score is not None and score < cfg.approve_min_confidence:
        # The walkthrough's own merge-readiness score, after the engine has
        # clamped it against the findings. It answers a question the severity
        # ceiling does not: the ceiling asks whether Mira found a problem, this
        # asks whether it believes it understood the change well enough for
        # "nothing found" to mean anything. A 40-file refactor the model scored
        # 2/5 is not an approval whatever the comment list looks like.
        logger.info(
            "Skipping approval — confidence %s is below the floor of %s",
            score,
            cfg.approve_min_confidence,
        )
        return None

    blocking_humans = sorted(
        login
        for login, state in (human_states or {}).items()
        if state == "CHANGES_REQUESTED" and not login.endswith("[bot]")
    )
    if blocking_humans:
        # A human asked for changes. Mira's opinion doesn't get to overwrite it.
        logger.info("Skipping approval — changes requested by %s", ", ".join(blocking_humans))
        return None

    remaining = len(result.comments)
    detail = (
        f" {_plural(remaining, 'non-blocking note')} left inline."
        if remaining
        else " No issues found."
    )
    confidence = f" Merge-readiness confidence: {score}/5." if score is not None else ""
    return Verdict(
        event=APPROVE,
        body=(
            f"✅ **Mira approved** — reviewed "
            f"{_plural(result.reviewed_files, 'file')} and found nothing above "
            f"`{ceiling.name.lower()}` severity.{detail}{confidence}"
        ),
        reason=(
            f"worst severity {worst.name if worst else 'none'} within ceiling {ceiling.name}"
            + (f", confidence {score}" if score is not None else "")
        ),
    )
