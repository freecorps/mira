"""``@mira fix`` — the platform-neutral entry point.

The webhook layers for GitHub, GitLab and Forgejo all funnel here rather than
each growing their own copy of the rules. What differs between them is how a
comment arrives and how a reply goes out; what must not differ is who may ask,
what gets resolved, and what the refusal says.

Two things this module is careful about.

**The finding is resolved from durable provenance, never from where the comment
sits.** A reply to a review comment carries the hidden ``mira:finding:<id>``
marker in the comment it replies to, and that marker is the handle. Path and
line are not: they move under a rebase, and a fix attached to whatever drifted
into a line number is the exact failure this phase must not have.

**The reply always says what happened.** Accepted, refused, or partly both — a
request that produces silence is worse than one that is turned down, because
nobody can tell it apart from a broken webhook.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from mira.autofix.models import Reason, ReasonCode
from mira.autofix.service import FixRequest, RequestOutcome, request_fix
from mira.autofix.worker import AutofixWorker
from mira.config import MiraConfig, load_config
from mira.feedback.provenance import parse_finding_id

logger = logging.getLogger(__name__)

# The verb, and the words that mean "and the rest of them too". Matched against
# the text following the mention, so `@mira fix all` and `@mira fix everything`
# both land on the batch path and `@mira fixture` lands on neither.
FIX_KEYWORDS = frozenset({"fix"})
FIX_ALL_WORDS = frozenset({"all", "everything", "them", "these"})

# `@mira fix --on-branch` asks for a commit onto the pull request's own branch.
# A modifier on the same request rather than a separate verb — and recognising
# it does not make it permitted: policy still has to allow it, and the refusal
# names the setting when it does not.
#
# Keyed without leading dashes, and matched that way, so `--on-branch`,
# `-on-branch` and a plain `on-branch` all mean the same thing. A maintainer
# typing a command into a comment box should not have to remember how many
# hyphens it wants.
_MODE_FLAGS = {
    "on-branch": "pr_branch",
    "in-place": "pr_branch",
    "handoff": "handoff",
}

_WORD = re.compile(r"[a-z0-9\-]+")


def parse_fix_command(text: str) -> tuple[str, str] | None:
    """``(kind, mode)`` for a fix command, or None if this is not one.

    Reads only the words Mira defined. Everything else in the comment is
    ignored — there is no path here from arbitrary comment text to an argument,
    a command, or a file.
    """
    words = _WORD.findall((text or "").lower())
    if not words or words[0] not in FIX_KEYWORDS:
        return None
    mode = "branch_pr"
    kind = "single"
    for word in words[1:]:
        flag = _MODE_FLAGS.get(word.strip("-"))
        if flag:
            mode = flag
        elif word in FIX_ALL_WORDS:
            kind = "all"
    return kind, mode


def _mode_label(mode: str) -> str:
    return {
        "branch_pr": "a separate branch and a stacked pull request",
        "pr_branch": "a commit on this pull request's branch",
        "handoff": "a handoff to an external agent",
    }.get(mode, mode)


def render_reply(outcome: RequestOutcome, *, actor: str, kind: str) -> str:
    """The comment Mira posts back. Always says something."""
    lines = [f"> @{actor} asked Mira to `fix{' all' if kind == 'all' else ''}`.", ""]

    if outcome.accepted:
        plural = "" if len(outcome.accepted) == 1 else "s"
        lines.append(
            f"Queued **{len(outcome.accepted)}** fix{plural} — "
            f"{_mode_label(outcome.mode)}. "
            "Nothing is written until the patch has been generated and validated."
        )
        lines.append("")
        lines.append("| Finding | Job |")
        lines.append("|---|---|")
        for job in outcome.accepted:
            title = (job.finding_title or job.finding_id)[:80].replace("|", "\\|")
            lines.append(f"| {title} | `{job.job_key[:12]}` |")
        lines.append("")

    if outcome.skipped:
        lines.append(f"**Not attempted ({len(outcome.skipped)}):**")
        lines.append("")
        for finding_id, reason in outcome.skipped[:20]:
            lines.append(f"- `{finding_id[:12]}` — {reason.message}")
        if len(outcome.skipped) > 20:
            lines.append(f"- … and {len(outcome.skipped) - 20} more")
        lines.append("")

    refusals = [reason for reason in outcome.reasons if reason.kind != "info"]
    notes = [reason for reason in outcome.reasons if reason.kind == "info"]
    if refusals:
        lines.append("**Mira did not start this:**")
        lines.append("")
        lines.extend(f"- {reason.message}" for reason in refusals)
        lines.append("")
    if notes:
        lines.extend(f"> {reason.message}" for reason in notes)
        lines.append("")

    if not outcome.accepted and not refusals and not outcome.skipped:
        lines.append("There was nothing to fix.")

    return "\n".join(lines).strip()


async def handle_fix_command(
    provider: Any,
    pr_info: Any,
    *,
    actor: str,
    kind: str,
    mode: str = "branch_pr",
    original_body: str = "",
    finding_id: str = "",
    config: MiraConfig | None = None,
    reply: Any = None,
) -> RequestOutcome:
    """Accept or refuse one fix request and answer on the pull request.

    ``reply`` is an awaitable callable taking the rendered body — the webhook
    layer supplies it so a reply to a review comment threads under that comment
    instead of landing as a new top-level one. Absent, the reply goes out as a
    top-level comment.
    """
    config = config or load_config()
    resolved = finding_id or (parse_finding_id(original_body) or "")
    outcome = await request_fix(
        provider,
        pr_info,
        FixRequest(actor=actor, kind=kind, finding_id=resolved, mode=mode),
        config=config,
    )
    if kind == "single" and not resolved and not outcome.reasons:
        outcome.reasons.append(
            Reason(
                ReasonCode.FINDING_NOT_FOUND,
                "Reply to one of Mira's review comments with `fix`, or use "
                "`fix all` on the pull request itself",
            )
        )

    body = render_reply(outcome, actor=actor, kind=kind)
    try:
        if reply is not None:
            await reply(body)
        else:
            await provider.post_comment(pr_info, body)
    except Exception as exc:  # noqa: BLE001 - a silent reply is not a reason to lose the jobs
        logger.warning("Could not answer the fix request on %s: %s", pr_info.url, exc)

    if outcome.accepted:
        _nudge_inline_worker(config)
    return outcome


def _nudge_inline_worker(config: MiraConfig) -> None:
    """Run one poll now, so an inline worker answers promptly.

    Only a nudge: the loop in :class:`~mira.autofix.worker.AutofixWorker` is
    what actually guarantees the job runs, and the job is already durable
    whether this fires or not. Without it, a request made just after a poll
    would wait a whole interval for no reason.
    """
    if not config.autofix.inline_worker:
        return
    import asyncio

    from mira.autofix.runtime import inline_worker

    worker: AutofixWorker | None = inline_worker()
    if worker is None:
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:  # pragma: no cover - no loop outside the server
        return
    task = loop.create_task(worker.poll_once(config=config))
    task.add_done_callback(
        lambda done: (
            logger.debug("Inline autofix poll failed: %s", done.exception())
            if not done.cancelled() and done.exception()
            else None
        )
    )
