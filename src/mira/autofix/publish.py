"""Putting a validated patch on the platform, or refusing to.

This is the only module in the phase that writes. Everything upstream of it
produces values; everything here produces side effects, and each one is
guarded, idempotent, and reversible by a human with one click.

Four rules, in the order they are enforced:

1. **Never the default branch.** The target is read from the provider and
   compared before a branch is created, before a commit is made and before a
   pull request is opened. A provider that cannot name its default branch does
   not get to the first of those — that is why `can_read_default_branch` is
   part of `can_publish`.
2. **Never a force push.** There is no force parameter on any provider method
   this module calls, and no method that takes one. A branch that already
   exists at a different commit is a conflict Mira reports, not a history it
   rewrites.
3. **Everything is idempotent.** A retry re-derives the same branch name,
   finds the branch it made last time, finds the pull request it opened last
   time, and adopts them. Two attempts produce one branch, one commit per
   distinct content, and one pull request.
4. **Never a merge.** Mira opens the change; a human merges it. There is no
   merge call here and no capability that would enable one.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from mira.autofix import capabilities as caps
from mira.autofix.models import (
    AutofixJob,
    FixPatch,
    Reason,
    ReasonCode,
    branch_name,
)
from mira.autofix.policy import EffectivePolicy
from mira.autofix.redact import redact

logger = logging.getLogger(__name__)

# Marker on the pull request body, so a retry can recognise the pull request it
# opened even if somebody renamed the branch or edited the title.
PR_MARKER = "<!-- mira:autofix:{job_key} -->"

# Trailer on every commit. A human reading `git log` should be able to tell a
# generated commit from a typed one without opening the pull request.
COMMIT_TRAILER = "Generated-by: Mira autofix\nMira-Finding: {finding_id}\nMira-Job: {job_key}"


class PublishRefused(Exception):
    """A write that must not happen, with the reason it must not."""

    def __init__(self, reason: Reason) -> None:
        super().__init__(reason.message)
        self.reason = reason


def _refuse(code: str, message: str) -> PublishRefused:
    return PublishRefused(Reason(code, message))


@dataclass
class PublishResult:
    """What ended up on the platform."""

    branch: str = ""
    commit_sha: str = ""
    pr_url: str = ""
    pr_number: int = 0
    reused: bool = False
    reasons: list[Reason] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.reasons is None:
            self.reasons = []


def commit_message(job: AutofixJob, patch: FixPatch) -> str:
    """Subject, body and trailers for the fix commit.

    The subject comes from the model and is therefore redacted and truncated;
    the trailers come from Mira and are the part anything automated should
    read.
    """
    subject = (redact(patch.summary).strip().splitlines() or ["Apply Mira's suggested fix"])[0]
    subject = subject[:72] or "Apply Mira's suggested fix"
    body = redact(patch.rationale).strip()
    trailer = COMMIT_TRAILER.format(finding_id=job.finding_id, job_key=job.job_key)
    parts = [subject]
    if body:
        parts.append(body[:2_000])
    parts.append(f"Refs: {job.pr_url}" if job.pr_url else "")
    parts.append(trailer)
    return "\n\n".join(part for part in parts if part)


def pull_request_body(job: AutofixJob, patch: FixPatch) -> str:
    """The stacked pull request's description: what, why, and from where.

    Everything a reviewer needs to judge the change without leaving the page —
    the originating finding, the model that wrote it, the validation that ran,
    and the fact that a machine wrote it.
    """
    lines = [
        PR_MARKER.format(job_key=job.job_key),
        "## Mira autofix",
        "",
        f"This change was generated to resolve a finding on {job.pr_url or 'the pull request'}.",
        "",
        "| | |",
        "|---|---|",
        f"| Finding | `{job.finding_id}` |",
        f"| Title | {redact(job.finding_title)[:200] or '—'} |",
        f"| Requested by | @{job.requested_by} |",
        f"| Model | `{job.model or patch.model or 'unknown'}` |",
        f"| Prompt | `{patch.prompt_digest or '—'}` |",
        f"| Patch | `{patch.digest}` ({patch.changed_files} file(s), "
        f"+{patch.added_lines}/-{patch.deleted_lines}) |",
        f"| Policy | `{job.policy_version}` |",
        "",
    ]
    if patch.rationale:
        lines.extend(["### Why", "", redact(patch.rationale)[:2_000], ""])

    checks = job.validation.checks
    if checks:
        lines.extend(["### Validation", "", "| Check | Result | Detail |", "|---|---|---|"])
        for check in checks:
            detail = redact(check.detail).replace("|", "\\|").replace("\n", " ")[:200]
            lines.append(f"| {check.name} | {check.outcome} | {detail or '—'} |")
        lines.append("")
    else:
        lines.extend(["### Validation", "", "No validation ran for this patch.", ""])

    lines.extend(
        [
            "### Before you merge",
            "",
            "A model wrote this. Read the diff. Mira will not merge it, will not "
            "approve it, and will not push to it again unless somebody asks.",
        ]
    )
    return "\n".join(lines)


async def _default_branch(provider: Any, pr_info: Any) -> str:
    getter = getattr(provider, "get_default_branch", None)
    if not callable(getter):
        raise _refuse(
            ReasonCode.PROVIDER_CANNOT_WRITE,
            "This provider cannot name the repository's default branch, "
            "so it cannot be kept away from it",
        )
    try:
        name = str(await getter(pr_info) or "").strip()
    except Exception as exc:  # noqa: BLE001
        raise _refuse(
            ReasonCode.PROVIDER_CANNOT_WRITE,
            f"The default branch could not be read: {exc}",
        ) from exc
    if not name:
        raise _refuse(
            ReasonCode.PROVIDER_CANNOT_WRITE,
            "The platform reported no default branch, so no write is safe here",
        )
    return name


def _assert_not_default(target: str, default: str) -> None:
    """The guarantee, spelled out once and called from every write path."""
    if not target:
        raise _refuse(ReasonCode.PUBLISH_FAILED, "No target branch was resolved")
    normalized = target.removeprefix("refs/heads/")
    if normalized == default.removeprefix("refs/heads/"):
        raise _refuse(
            ReasonCode.DEFAULT_BRANCH_REFUSED,
            f"{target} is this repository's default branch; a fix is never written to it",
        )


async def publish(
    provider: Any,
    pr_info: Any,
    job: AutofixJob,
    patch: FixPatch,
    policy: EffectivePolicy,
    *,
    capabilities: caps.AutofixCapabilities | None = None,
) -> PublishResult:
    """Write the patch out in whichever mode the job was authorized for."""
    capability = capabilities or caps.for_provider(provider)
    default = await _default_branch(provider, pr_info)
    if job.mode == "pr_branch":
        return await _commit_to_pr_branch(provider, pr_info, job, patch, default, capability)
    return await _open_stacked_pr(provider, pr_info, job, patch, policy, default, capability)


async def _commit_to_pr_branch(
    provider: Any,
    pr_info: Any,
    job: AutofixJob,
    patch: FixPatch,
    default: str,
    capability: caps.AutofixCapabilities,
) -> PublishResult:
    """Commit onto the pull request's own branch. Opt-in, and narrowly.

    Two extra refusals live here and nowhere else. A pull request whose head
    branch *is* the default branch is the one shape where "commit to the PR
    branch" and "commit to the default branch" are the same act. And a pull
    request from a fork has a head branch in a repository Mira was never given
    permission to write to; a push that happened to succeed would be a
    cross-repository write nobody authorized.
    """
    target = job.head_branch or getattr(pr_info, "head_branch", "")
    _assert_not_default(target, default)

    is_fork = bool(getattr(pr_info, "head_is_fork", False))
    if not is_fork and hasattr(provider, "pr_head_is_fork"):
        try:
            is_fork = bool(await provider.pr_head_is_fork(pr_info))
        except Exception as exc:  # noqa: BLE001 - unknown is treated as a fork
            logger.warning("Could not tell whether %s comes from a fork: %s", pr_info.url, exc)
            is_fork = True
    if is_fork:
        raise _refuse(
            ReasonCode.FORK_HEAD_REFUSED,
            "This pull request's branch lives in a fork; Mira will not commit into it",
        )

    if not capability.can_push_to_pr_branch:
        raise _refuse(
            ReasonCode.PROVIDER_CANNOT_WRITE,
            f"{capability.provider} cannot commit onto an existing pull request's branch",
        )

    sha = await _commit_files(provider, pr_info, target, patch, commit_message(job, patch))
    return PublishResult(
        branch=target,
        commit_sha=sha,
        pr_url=job.pr_url,
        pr_number=job.pr_number,
        reasons=[
            Reason(
                ReasonCode.COMMIT_PUSHED,
                f"The fix was committed to {target} as {sha[:12] or 'a new commit'}",
                "info",
            )
        ],
    )


async def _open_stacked_pr(
    provider: Any,
    pr_info: Any,
    job: AutofixJob,
    patch: FixPatch,
    policy: EffectivePolicy,
    default: str,
    capability: caps.AutofixCapabilities,
) -> PublishResult:
    """Mira's own branch, Mira's own commit, Mira's own pull request.

    Stacked on the branch under review rather than on the default branch, so
    merging it lands the fix inside the pull request being reviewed instead of
    beside it — and so a reviewer sees the fix in the context that produced it.
    """
    if not capability.can_publish:
        raise _refuse(
            ReasonCode.PROVIDER_CANNOT_WRITE,
            f"{capability.provider} cannot create a branch and open a pull request",
        )

    base = job.head_branch or getattr(pr_info, "head_branch", "")
    if not base:
        raise _refuse(
            ReasonCode.PUBLISH_FAILED, "The pull request has no head branch to stack onto"
        )

    branch = job.branch_name or branch_name(
        prefix=policy.branch_prefix,
        pr_number=job.pr_number,
        finding_id=job.finding_id,
        request_kind=job.request_kind,
        title=job.finding_title,
    )
    # The branch Mira creates is checked; `base` deliberately is not. Opening a
    # pull request *against* a branch does not modify it, and a single-commit
    # pull request whose head branch is the default branch is a real shape a
    # reviewer can find themselves in. Targeting it is fine. Writing to it is
    # what is forbidden, and that is what the check above covers.
    _assert_not_default(branch, default)

    reasons: list[Reason] = []
    from_sha = job.head_sha or getattr(pr_info, "head_sha", "")
    created = await _ensure_branch(provider, pr_info, branch, from_sha)
    if not created:
        reasons.append(
            Reason(
                ReasonCode.REUSED_EXISTING,
                f"The branch {branch} already existed and was reused",
                "info",
            )
        )

    sha = await _commit_files(provider, pr_info, branch, patch, commit_message(job, patch))

    existing = await _find_open_pr(provider, pr_info, branch, capability)
    if existing:
        number, url = existing
        reasons.append(
            Reason(
                ReasonCode.REUSED_EXISTING,
                f"The pull request for {branch} was already open and was updated in place",
                "info",
            )
        )
        return PublishResult(
            branch=branch,
            commit_sha=sha,
            pr_url=url,
            pr_number=number,
            reused=True,
            reasons=reasons,
        )

    title = (
        f"fix: {redact(patch.summary).strip().splitlines()[0][:60]}"
        if patch.summary
        else f"fix: address Mira finding on #{job.pr_number}"
    )
    try:
        number, url = await provider.create_pull_request(
            pr_info,
            head=branch,
            base=base,
            title=title,
            body=pull_request_body(job, patch),
        )
    except Exception as exc:  # noqa: BLE001
        raise _refuse(
            ReasonCode.PUBLISH_FAILED, f"The pull request could not be opened: {exc}"
        ) from exc

    reasons.append(
        Reason(ReasonCode.PR_OPENED, f"Opened {url or f'#{number}'} from {branch}", "info")
    )
    return PublishResult(
        branch=branch,
        commit_sha=sha,
        pr_url=str(url or ""),
        pr_number=int(number or 0),
        reasons=reasons,
    )


async def _ensure_branch(provider: Any, pr_info: Any, branch: str, from_sha: str) -> bool:
    """Create the branch, or accept the one a previous attempt made.

    Returns True when this call created it. An existing branch is adopted
    rather than reset: resetting it is a force push under another name, and a
    branch that somebody has already reviewed or pushed to is not Mira's to
    discard.
    """
    try:
        existing = await provider.get_branch_head(pr_info, branch)
    except Exception as exc:  # noqa: BLE001
        raise _refuse(
            ReasonCode.PUBLISH_FAILED, f"The fix branch could not be inspected: {exc}"
        ) from exc
    if existing:
        return False
    try:
        await provider.create_branch(pr_info, branch, from_sha)
    except Exception as exc:  # noqa: BLE001
        # A concurrent worker may have created it between the two calls. That
        # is the idempotent outcome, not a failure — but only if it is really
        # there now.
        try:
            if await provider.get_branch_head(pr_info, branch):
                return False
        except Exception:  # noqa: BLE001 - fall through to the original error
            pass
        raise _refuse(
            ReasonCode.PUBLISH_FAILED, f"The fix branch could not be created: {exc}"
        ) from exc
    return True


async def _commit_files(
    provider: Any, pr_info: Any, branch: str, patch: FixPatch, message: str
) -> str:
    """Commit the patched files onto ``branch``, or nothing if they are already there.

    The "already there" check is what makes a retried publish idempotent: a
    worker that crashed after committing and before recording the sha comes
    back, sees the content it wanted, and does not commit it a second time.
    """
    try:
        unchanged = await provider.files_match(pr_info, branch, dict(patch.files))
    except Exception as exc:  # noqa: BLE001
        raise _refuse(
            ReasonCode.PUBLISH_FAILED, f"The fix branch could not be compared: {exc}"
        ) from exc
    if unchanged:
        try:
            return str(await provider.get_branch_head(pr_info, branch) or "")
        except Exception:  # noqa: BLE001 - a missing sha is cosmetic here
            return ""
    try:
        return str(await provider.commit_files(pr_info, branch, dict(patch.files), message) or "")
    except Exception as exc:  # noqa: BLE001
        raise _refuse(ReasonCode.PUBLISH_FAILED, f"The fix could not be committed: {exc}") from exc


async def _find_open_pr(
    provider: Any, pr_info: Any, branch: str, capability: caps.AutofixCapabilities
) -> tuple[int, str] | None:
    if not capability.can_find_pull_request:
        return None
    try:
        found = await provider.find_open_pull_request(pr_info, branch)
    except Exception as exc:  # noqa: BLE001 - not finding one is not a failure
        logger.warning("Could not look for an existing pull request from %s: %s", branch, exc)
        return None
    if not found:
        return None
    number, url = found
    return int(number or 0), str(url or "")
