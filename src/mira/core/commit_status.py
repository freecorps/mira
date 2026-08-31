"""The commit status that says whether Mira has finished reviewing.

Everything Mira publishes today only exists once it has something to say. The
walkthrough placeholder goes up early, but it is a *comment*, and the box a
reviewer actually looks at before merging is the checks list. Until this
module, that box said nothing about Mira at all — which on a slow review is
indistinguishable from "the bot is not installed here", and after a crash is
indistinguishable from "there was nothing to flag".

So the review publishes its own status: ``in progress`` when it starts, and
one terminal state when it stops. Three rules hold it together.

*The status is about the review, not about the merge.* The merge gate has its
own context and its own decision; this one reports what the review found and
how much of the diff it got through. Two names, two claims, and neither
inherits the other's authority.

*Mira's failures are never red.* A timeout, a rate limit or a model outage
publishes ``neutral`` naming the failure. Red on a pull request is read as a
statement about the change, and a status that goes red when the API is having
a bad afternoon is a status people learn to scroll past — which costs exactly
the signal this module exists to add.

*The name is a constant.* Providers filter Mira's own contexts out of the CI
they read back; a configurable name is one the exclusion list cannot know, and
the loop that follows — Mira reads its own red status as a failing build,
reports CI as failing, publishes red, repeats — is documented in
``mira.checks.models.mira_status_contexts`` because the gate hit it first.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from mira.config import MiraConfig
from mira.exceptions import MiraError
from mira.models import PRInfo, ReviewResult, Severity

logger = logging.getLogger(__name__)

# The check-run / commit-status name. Stable, and excluded from the CI Mira
# reads back — see the module docstring.
STATUS_CONTEXT = "mira/review"

PENDING = "pending"
SUCCESS = "success"
FAILURE = "failure"
NEUTRAL = "neutral"


@dataclass(frozen=True)
class CommitStatus:
    """One publishable state: what to show, and what it says."""

    state: str
    title: str
    summary: str


def _plural(count: int, word: str) -> str:
    return f"{count} {word}{'' if count == 1 else 's'}"


def _counts(result: ReviewResult) -> dict[Severity, int]:
    counts: dict[Severity, int] = {}
    for comment in result.comments:
        counts[comment.severity] = counts.get(comment.severity, 0) + 1
    return counts


def _findings_line(result: ReviewResult) -> str:
    """ "1 blocker, 2 warnings", in severity order, or "no findings"."""
    counts = _counts(result)
    parts = [
        _plural(counts[severity], severity.name.lower())
        for severity in (Severity.BLOCKER, Severity.WARNING, Severity.SUGGESTION, Severity.NITPICK)
        if counts.get(severity)
    ]
    return ", ".join(parts) if parts else "no findings"


def _coverage_line(result: ReviewResult) -> str:
    """What was left unread, when anything was.

    Worth a sentence of its own: a green status on a pull request whose largest
    file was skipped for size is true about what Mira read and misleading about
    what it means, and the difference is invisible unless it is written down.
    """
    if not result.skipped_paths:
        return ""
    total = len(result.reviewed_paths) + len(result.skipped_paths)
    return (
        f"{len(result.reviewed_paths)} of {total} changed files were reviewed; "
        f"{_plural(len(result.skipped_paths), 'file')} did not fit this pass "
        f"(comment `review-rest` to cover them)."
    )


def pending_status() -> CommitStatus:
    return CommitStatus(
        state=PENDING,
        title="Reviewing…",
        summary="Mira is reviewing this pull request.",
    )


def finished_status(result: ReviewResult, config: MiraConfig) -> CommitStatus:
    """The terminal state for a review that ran to completion.

    ``skipped_reason`` — every file excluded by config, or every file over the
    size limit — is ``neutral`` and not ``success``: Mira did not review this
    pull request, and green is a claim that it did.
    """
    if result.skipped_reason:
        return CommitStatus(
            state=NEUTRAL,
            title=f"Not reviewed — {result.skipped_reason}",
            summary=(
                f"Mira did not review this pull request: {result.skipped_reason}\n\n"
                "This is a statement about what Mira looked at, not about the change."
            ),
        )

    cfg = config.review.status
    counts = _counts(result)
    blockers = counts.get(Severity.BLOCKER, 0)
    ceiling = Severity.from_str(config.review.verdict.approve_max_severity)
    worst = max((comment.severity for comment in result.comments), default=None)

    failing = False
    if cfg.fail_on == "blocker":
        failing = blockers > 0
    elif cfg.fail_on == "above_ceiling":
        failing = worst is not None and worst > ceiling

    findings = _findings_line(result)
    reviewed = _plural(result.reviewed_files, "file")
    summary = f"Mira reviewed {reviewed} and posted {findings}."
    coverage = _coverage_line(result)
    if coverage:
        summary = f"{summary}\n\n{coverage}"

    if failing:
        return CommitStatus(
            state=FAILURE,
            title=findings.capitalize(),
            summary=summary,
        )
    return CommitStatus(
        state=SUCCESS,
        title="No findings" if not result.comments else findings.capitalize(),
        summary=summary,
    )


def failed_status(exc: BaseException) -> CommitStatus:
    """The terminal state for a review that did not finish. Never red.

    The message is the same user-safe one the failure comment uses: a raw
    exception string can carry a URL with a token in it, and this one is
    published to a commit rather than to a log.
    """
    message = exc.safe_message if isinstance(exc, MiraError) else type(exc).__name__
    return CommitStatus(
        state=NEUTRAL,
        title="Mira could not finish this review",
        summary=(
            f"The review stopped before it finished: {message}\n\n"
            "This says nothing about the pull request — it is a Mira failure. "
            "Push again or comment `review` to retry."
        ),
    )


class ReviewStatusReporter:
    """Publishes the review status, and swallows everything that goes wrong.

    A status is an announcement about work, never the work. Every method here
    returns ``None`` and logs rather than raising, because a review that landed
    its comments and then failed to colour a box is a successful review, and a
    provider that refuses the status must not be able to undo it.

    ``published`` records whether the platform actually took one — an operator
    reading "status: never published" in the log has the one fact that
    separates a missing capability from a missing token scope.
    """

    def __init__(self, provider: Any, config: MiraConfig, *, dry_run: bool = False) -> None:
        self._provider = provider
        self._config = config
        self._dry_run = dry_run
        self.published = False
        # Set once a terminal state has gone out, so a failure raised *after*
        # the review finished cannot overwrite the result with "could not
        # finish". The gate, the checks and triage all run after the status is
        # published, and a triage crash is not a review failure.
        self.settled = False

    @property
    def active(self) -> bool:
        return bool(
            self._config.review.status.enabled and self._provider is not None and not self._dry_run
        )

    async def _publish(self, pr_info: PRInfo, status: CommitStatus) -> None:
        publish = getattr(self._provider, "publish_review_status", None)
        if not callable(publish):
            return
        try:
            reference = await publish(
                pr_info,
                context=STATUS_CONTEXT,
                state=status.state,
                title=status.title,
                summary=status.summary,
            )
        except Exception as exc:  # noqa: BLE001 - announcing is never fatal
            logger.warning("Could not publish the review status on %s: %s", pr_info.url, exc)
            return
        if not reference:
            # The provider declined rather than failed — GitLab, deliberately.
            logger.debug("No review status published on %s: unsupported", pr_info.url)
            return
        self.published = True

    async def start(self, pr_info: PRInfo) -> None:
        if not self.active or not self._config.review.status.pending:
            return
        await self._publish(pr_info, pending_status())

    async def finish(self, pr_info: PRInfo, result: ReviewResult) -> None:
        if not self.active:
            return
        await self._publish(pr_info, finished_status(result, self._config))
        self.settled = True

    async def failed(self, pr_info: PRInfo | None, exc: BaseException) -> None:
        """Report a review that stopped early.

        Does nothing once a terminal state has gone out, and nothing when the
        failure happened before there was a pull request to publish against —
        in both cases there is no status of Mira's left hanging.
        """
        if not self.active or self.settled or pr_info is None:
            return
        await self._publish(pr_info, failed_status(exc))
        self.settled = True
