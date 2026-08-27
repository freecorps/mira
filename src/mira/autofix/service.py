"""Accepting a fix request, and running one to completion.

The order is load-bearing and never varies.

1. **Resolve the policy.** If autofix is off for this repository, stop. Nothing
   is fetched, nothing is generated and nothing is written.
2. **Authorize the requester**, against the platform's own permission model,
   before a single token is spent and long before anything is written.
3. **Resolve the finding**, by ``finding_id`` and by nothing else.
4. **Enqueue** a durable job. The request is answered at this point; the work
   happens later, on a worker, and survives a restart in between.
5. **Generate**, in the worker: ask a model, parse structured output.
6. **Apply**, purely, in :mod:`mira.autofix.patch`. Path safety and every size
   limit live here, and nothing has been written yet.
7. **Validate**. Still nothing written.
8. **Publish** — the only step that writes, and the only step that can reach a
   branch.

Steps 5 to 8 are separate functions with separate failure modes on purpose. A
failure in any of 1 to 7 leaves the repository exactly as it was, because the
repository has not been touched; that is what makes "a failed fix cannot damage
anything" a property of the shape of the code rather than of a handler.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any

from mira.autofix import capabilities as caps
from mira.autofix import handoff as handoff_module
from mira.autofix.authorization import authorize_delivery, authorize_requester
from mira.autofix.generate import FixContext, GenerationFailed, generate_fix
from mira.autofix.models import (
    NON_RETRYABLE_CODES,
    AutofixAttempt,
    AutofixJob,
    FixPatch,
    Reason,
    ReasonCode,
    job_key,
    request_id,
)
from mira.autofix.patch import PatchRefused, apply_patch
from mira.autofix.policy import EffectivePolicy, resolve_policy
from mira.autofix.publish import PublishRefused, publish
from mira.autofix.validate import validate
from mira.config import MiraConfig, load_config
from mira.feedback.models import ReviewFinding

logger = logging.getLogger(__name__)

# Severity ordering for the `fix all` floor. Not `Severity.from_str`, which
# resolves an unknown value to `suggestion` — here an unrecognised severity
# must not silently clear a floor it was never measured against, so it is
# ranked lowest and excluded.
_SEVERITY_RANK = {"blocker": 4, "warning": 3, "suggestion": 2, "nitpick": 1}

# Finding states that mean somebody already dealt with it. Matches the merge
# gate's list deliberately: `outdated` is *not* here, because it only means the
# diff moved past the anchored line, which is what an unaddressed finding looks
# like after a rebase.
CLOSED_FINDING_STATES = frozenset({"fixed", "resolved", "dismissed"})


def _open_store(owner: str, repo: str, platform: str) -> Any:
    from mira.index.store import IndexStore

    return IndexStore.open(owner, repo, platform=platform)


@dataclass
class FixRequest:
    """One ``@mira fix`` or ``@mira fix all``, before anything is decided."""

    actor: str
    kind: str = "single"
    # Present for `fix`, empty for `fix all`. Resolved from the comment the
    # reply was made on, never from a path and a line.
    finding_id: str = ""
    mode: str = "branch_pr"


@dataclass
class RequestOutcome:
    """What came of a request: the jobs it made, and what it would not do."""

    accepted: list[AutofixJob] = field(default_factory=list)
    skipped: list[tuple[str, Reason]] = field(default_factory=list)
    reasons: list[Reason] = field(default_factory=list)
    request_id: str = ""
    policy: EffectivePolicy | None = None
    mode: str = ""

    @property
    def ok(self) -> bool:
        return bool(self.accepted)


def _rank(severity: str) -> int:
    return _SEVERITY_RANK.get((severity or "").lower(), 0)


def select_findings(
    findings: list[ReviewFinding], policy: EffectivePolicy
) -> tuple[list[ReviewFinding], list[tuple[str, Reason]]]:
    """Choose what ``fix all`` will actually attempt, and name what it will not.

    ``all`` never means all. It means "the most serious open findings, up to
    the configured ceiling" — and every finding that ceiling or the severity
    floor excluded is returned alongside, with the reason, so the reply on the
    pull request can list them. A limit that silently drops work is a limit
    that gets discovered in an incident.
    """
    floor = _rank(policy.min_severity_for_fix_all)
    eligible: list[ReviewFinding] = []
    skipped: list[tuple[str, Reason]] = []

    for finding in findings:
        if finding.state in CLOSED_FINDING_STATES:
            skipped.append(
                (
                    finding.id,
                    Reason(
                        ReasonCode.FINDING_NOT_OPEN,
                        f"already {finding.state}",
                        "info",
                    ),
                )
            )
            continue
        if _rank(finding.severity) < floor:
            skipped.append(
                (
                    finding.id,
                    Reason(
                        ReasonCode.REQUEST_LIMIT,
                        f"severity {finding.severity or 'unknown'} is below the "
                        f"{policy.min_severity_for_fix_all} floor for `fix all`",
                        "info",
                    ),
                )
            )
            continue
        eligible.append(finding)

    # Most serious first, then oldest first, so which findings a limit selects
    # is deterministic rather than a property of row order.
    eligible.sort(key=lambda item: (-_rank(item.severity), item.created_at, item.id))
    chosen = eligible[: policy.max_fixes_per_request]
    for finding in eligible[policy.max_fixes_per_request :]:
        skipped.append(
            (
                finding.id,
                Reason(
                    ReasonCode.REQUEST_LIMIT,
                    f"over the limit of {policy.max_fixes_per_request} fixes per request",
                    "info",
                ),
            )
        )
    return chosen, skipped


async def request_fix(
    provider: Any,
    pr_info: Any,
    request: FixRequest,
    *,
    config: MiraConfig | None = None,
) -> RequestOutcome:
    """Accept or refuse a fix request, and durably queue whatever it accepts.

    Never raises. Every refusal comes back as a reason the caller can render on
    the pull request, because a request that vanishes is worse than one that is
    turned down.
    """
    config = config or load_config()
    platform = getattr(pr_info, "platform", "github")
    policy = resolve_policy(config.autofix, pr_info.owner, pr_info.repo)
    outcome = RequestOutcome(policy=policy)

    if not policy.active:
        outcome.reasons.append(
            Reason(
                ReasonCode.KILL_SWITCH if config.autofix.kill_switch else ReasonCode.AUTOFIX_OFF,
                "Assisted correction is not enabled for this repository",
            )
        )
        return outcome

    capability = caps.for_provider(provider)
    mode, refusal = authorize_delivery(
        policy=policy, capabilities=capability, requested_mode=request.mode
    )
    if refusal is not None and not (
        policy.handoff.fallback_when_refused and policy.handoff_enabled
    ):
        outcome.reasons.append(refusal)
        return outcome
    if refusal is not None:
        mode = "handoff"
        outcome.reasons.append(
            Reason(
                ReasonCode.HANDED_OFF,
                f"{refusal.message}; handing the work to the "
                f"{policy.handoff.adapter} adapter instead",
                "info",
            )
        )
    outcome.mode = mode

    # Authorization before anything is read about the finding. An account that
    # may not ask for a fix should not be able to use `@mira fix` to discover
    # whether a finding id exists.
    authorization = await authorize_requester(
        provider, pr_info, actor=request.actor, policy=policy, capabilities=capability
    )
    if not authorization.allowed:
        outcome.reasons.extend(authorization.refusal)
        return outcome

    store = _open_store(pr_info.owner, pr_info.repo, platform)
    try:
        findings = _resolve_findings(store, pr_info, request, outcome)
        if not findings:
            return outcome

        if request.kind == "all":
            chosen, skipped = select_findings(findings, policy)
            outcome.skipped.extend(skipped)
            findings = chosen
            if not findings:
                outcome.reasons.append(
                    Reason(
                        ReasonCode.NOTHING_TO_FIX,
                        "No open finding on this pull request meets the bar for `fix all`",
                    )
                )
                return outcome

        active = store.count_active_autofix_jobs(owner=pr_info.owner, repo=pr_info.repo)
        room = max(0, policy.max_concurrent_jobs - active)
        if room <= 0:
            outcome.reasons.append(
                Reason(
                    ReasonCode.CONCURRENCY_LIMIT,
                    f"{active} fix job(s) are already in flight for this repository; "
                    f"the limit is {policy.max_concurrent_jobs}",
                )
            )
            return outcome
        for finding in findings[room:]:
            outcome.skipped.append(
                (
                    finding.id,
                    Reason(
                        ReasonCode.CONCURRENCY_LIMIT,
                        "over this repository's concurrent-job limit; ask again "
                        "once the queue drains",
                        "info",
                    ),
                )
            )
        findings = findings[:room]

        batch = request_id(
            platform=platform,
            owner=pr_info.owner,
            repo=pr_info.repo,
            pr_number=pr_info.number,
            head_sha=getattr(pr_info, "head_sha", "") or "",
        )
        outcome.request_id = batch
        for finding in findings:
            job = _build_job(
                pr_info,
                finding,
                request=request,
                mode=mode,
                policy=policy,
                platform=platform,
                actor=authorization.actor,
                batch=batch,
            )
            stored, created = store.enqueue_autofix_job(job)
            if not created and stored.terminal:
                outcome.skipped.append(
                    (
                        finding.id,
                        Reason(
                            ReasonCode.REUSED_EXISTING,
                            f"a fix for this finding at this commit already finished "
                            f"as {stored.state}",
                            "info",
                        ),
                    )
                )
                continue
            outcome.accepted.append(stored)
    finally:
        store.close()

    return outcome


def _resolve_findings(
    store: Any, pr_info: Any, request: FixRequest, outcome: RequestOutcome
) -> list[ReviewFinding]:
    """Findings by durable id — never by path, line or comment position.

    A finding id is the one handle that survives a force push, a rebase and a
    comment being edited. Resolving by path and line would attach a fix to
    whatever moved into that position, which is precisely the class of mistake
    that makes an autofix dangerous rather than merely wrong.
    """
    if request.kind == "single":
        if not request.finding_id:
            outcome.reasons.append(
                Reason(
                    ReasonCode.FINDING_NOT_FOUND,
                    "Reply to one of Mira's review comments with `fix`, or use "
                    "`fix all` on the pull request",
                )
            )
            return []
        finding = store.get_review_finding(request.finding_id)
        if finding is None:
            outcome.reasons.append(
                Reason(
                    ReasonCode.FINDING_NOT_FOUND,
                    "That finding is not one Mira recorded for this repository",
                )
            )
            return []
        if finding.pr_number != pr_info.number:
            outcome.reasons.append(
                Reason(
                    ReasonCode.FINDING_OTHER_PR,
                    f"That finding belongs to pull request #{finding.pr_number}",
                )
            )
            return []
        if finding.state in CLOSED_FINDING_STATES:
            outcome.reasons.append(
                Reason(ReasonCode.FINDING_NOT_OPEN, f"That finding is already {finding.state}")
            )
            return []
        return [finding]

    findings = store.list_review_findings(pr_number=pr_info.number)
    if not findings:
        outcome.reasons.append(
            Reason(ReasonCode.NOTHING_TO_FIX, "There are no open findings on this pull request")
        )
    return findings


def _build_job(
    pr_info: Any,
    finding: ReviewFinding,
    *,
    request: FixRequest,
    mode: str,
    policy: EffectivePolicy,
    platform: str,
    actor: str,
    batch: str,
) -> AutofixJob:
    head_sha = getattr(pr_info, "head_sha", "") or ""
    key = job_key(
        platform=platform,
        owner=pr_info.owner,
        repo=pr_info.repo,
        pr_number=pr_info.number,
        head_sha=head_sha,
        finding_id=finding.id,
        mode=mode,
    )
    return AutofixJob(
        job_key=key,
        state="queued",
        mode=mode,  # type: ignore[arg-type]
        request_kind=request.kind,  # type: ignore[arg-type]
        platform=platform,
        owner=pr_info.owner,
        repo=pr_info.repo,
        pr_number=pr_info.number,
        pr_url=pr_info.url,
        base_branch=getattr(pr_info, "base_branch", ""),
        head_branch=getattr(pr_info, "head_branch", ""),
        head_sha=head_sha,
        finding_id=finding.id,
        finding_title=finding.title,
        requested_by=actor,
        request_id=batch,
        policy_version=policy.version,
        max_attempts=policy.max_attempts,
        max_ci_attempts=policy.max_ci_retries,
        available_at=time.time(),
    )


# ───────────────────────────────────────────────────────────── running one ──


@dataclass
class RunResult:
    """The state one attempt left the job in."""

    job: AutofixJob
    reasons: list[Reason] = field(default_factory=list)
    patch: FixPatch | None = None

    @property
    def opened(self) -> bool:
        return self.job.state == "opened"


class _Recorder:
    """Writes attempts as they happen, so a crash still leaves a trail."""

    def __init__(self, store: Any, job: AutofixJob) -> None:
        self._store = store
        self._job = job
        self._started = time.monotonic()

    def record(
        self,
        phase: str,
        outcome: str,
        *,
        reasons: list[Reason] | None = None,
        patch: FixPatch | None = None,
        validation: Any = None,
        detail: str = "",
    ) -> None:
        elapsed = time.monotonic() - self._started
        self._started = time.monotonic()
        attempt = AutofixAttempt(
            job_id=self._job.id,
            job_key=self._job.job_key,
            attempt=self._job.attempts,
            phase=phase,
            outcome=outcome,
            model=(patch.model if patch else self._job.model),
            prompt_digest=(patch.prompt_digest if patch else ""),
            patch_digest=(patch.digest if patch else ""),
            diff=(patch.diff if patch else ""),
            reasons=list(reasons or []),
            validation=validation or self._job.validation,
            detail=detail[:4_000],
            duration_seconds=elapsed,
        )
        try:
            self._store.record_autofix_attempt(attempt)
        except Exception as exc:  # noqa: BLE001 - an audit gap must not kill the job
            logger.warning("Could not record an autofix attempt for %s: %s", self._job.job_key, exc)


async def run_job(
    provider: Any,
    job: AutofixJob,
    *,
    config: MiraConfig | None = None,
    llm: Any = None,
    store: Any = None,
) -> RunResult:
    """Run one leased job to a terminal or retryable state. Never raises.

    The caller owns the lease; this function owns the outcome. A refusal at any
    stage is recorded on the job with its reason, and whether it is retried is
    decided from that reason rather than from the exception type — a path
    traversal is not going to succeed on the second try, and burning attempts
    on it would only delay telling somebody.
    """
    config = config or load_config()
    policy = resolve_policy(config.autofix, job.owner, job.repo)
    owned_store = store is None
    store = store or _open_store(job.owner, job.repo, job.platform)
    recorder = _Recorder(store, job)

    try:
        if not policy.active:
            return _fail(
                store,
                job,
                recorder,
                "generate",
                [
                    Reason(
                        ReasonCode.AUTOFIX_OFF,
                        "Assisted correction was turned off before this job ran",
                    )
                ],
                policy,
            )
        try:
            result = await asyncio.wait_for(
                _run_phases(provider, job, policy, config, llm, store, recorder),
                timeout=policy.job_timeout_seconds,
            )
        except TimeoutError:
            return _fail(
                store,
                job,
                recorder,
                "generate",
                [
                    Reason(
                        ReasonCode.JOB_TIMEOUT,
                        f"The fix exceeded its {policy.job_timeout_seconds:g}s budget",
                    )
                ],
                policy,
            )
        return result
    except Exception as exc:  # noqa: BLE001 - an unexpected failure is still a failure
        logger.exception("Autofix job %s crashed", job.job_key)
        return _fail(
            store,
            job,
            recorder,
            "generate",
            [Reason(ReasonCode.MODEL_FAILURE, f"{type(exc).__name__}: {exc}")],
            policy,
        )
    finally:
        if owned_store:
            store.close()


async def _run_phases(
    provider: Any,
    job: AutofixJob,
    policy: EffectivePolicy,
    config: MiraConfig,
    llm: Any,
    store: Any,
    recorder: _Recorder,
) -> RunResult:
    pr_info = await provider.get_pr_info(job.pr_url)
    finding = store.get_review_finding(job.finding_id)
    if finding is None:
        return _fail(
            store,
            job,
            recorder,
            "generate",
            [Reason(ReasonCode.FINDING_NOT_FOUND, "The finding this job was for is gone")],
            policy,
        )

    if job.mode == "handoff":
        return await _run_handoff(provider, pr_info, job, finding, policy, store, recorder)

    # ── generate ────────────────────────────────────────────────────────
    llm = llm or _default_llm(config)
    context = await _build_context(provider, pr_info, job, finding, policy, store)
    try:
        generated = await generate_fix(llm, context, policy)
    except GenerationFailed as exc:
        return _fail(store, job, recorder, "generate", [exc.reason], policy)
    recorder.record("generate", "ok", detail=generated.summary)

    # ── apply ───────────────────────────────────────────────────────────
    changed_paths = {stat.path for stat in await provider.get_pr_change_stats(pr_info)}
    try:
        patch = apply_patch(
            generated.edits,
            sources=context.sources,
            policy=policy,
            changed_paths=changed_paths,
            summary=generated.summary,
            rationale=generated.rationale,
            model=generated.model,
            prompt_digest=generated.prompt_digest,
        )
    except PatchRefused as exc:
        recorder.record("apply", "refused", reasons=[exc.reason])
        return _fail(store, job, recorder, "apply", [exc.reason], policy, record=False)
    store.update_autofix_job(
        job.job_key,
        state="validating",
        model=patch.model,
        patch_digest=patch.digest,
        diff=patch.diff,
    )
    job.state = "validating"
    job.diff = patch.diff
    job.model = patch.model
    recorder.record("apply", "ok", patch=patch)

    # ── validate ────────────────────────────────────────────────────────
    validation = await validate(patch, policy)
    job.validation = validation
    store.update_autofix_job(job.job_key, validation=validation)
    recorder.record(
        "validate",
        "passed" if validation.ok else "failed",
        patch=patch,
        validation=validation,
        detail="; ".join(f"{c.name}: {c.outcome}" for c in validation.checks),
    )
    if not validation.ok:
        failures = ", ".join(check.name for check in validation.failures)
        return _fail(
            store,
            job,
            recorder,
            "validate",
            [
                Reason(
                    ReasonCode.VALIDATION_FAILED,
                    f"The patch did not survive validation ({failures})",
                )
            ],
            policy,
            record=False,
            patch=patch,
        )

    # ── publish ─────────────────────────────────────────────────────────
    #
    # The last read before the first write. Everything above this line is
    # reversible by doing nothing; everything below it puts something on a
    # platform, so the job's own row is consulted one final time. An admin who
    # cancelled while the model was thinking gets what they asked for, and the
    # `cancelled` state is not overwritten by a result nobody wants.
    stopped = _stopped_by_admin(store, job)
    if stopped is not None:
        recorder.record("publish", "cancelled", reasons=[stopped], patch=patch)
        logger.info("Autofix job %s was cancelled before it wrote anything", job.job_key)
        return RunResult(
            job=store.get_autofix_job(job.job_key) or job, reasons=[stopped], patch=patch
        )

    if not policy.writing:
        stored = store.update_autofix_job(
            job.job_key,
            state="opened",
            reasons=[
                Reason(
                    ReasonCode.SUGGEST_ONLY,
                    "autofix.mode is `suggest`: the patch was generated and validated, "
                    "and nothing was written",
                    "info",
                )
            ],
            clear_lease=True,
        )
        recorder.record("publish", "suggest_only", patch=patch, validation=validation)
        return RunResult(job=stored or job, patch=patch)

    store.update_autofix_job(job.job_key, state="publishing")
    job.state = "publishing"
    try:
        published = await publish(provider, pr_info, job, patch, policy)
    except PublishRefused as exc:
        recorder.record("publish", "refused", reasons=[exc.reason], patch=patch)
        return _fail(
            store, job, recorder, "publish", [exc.reason], policy, record=False, patch=patch
        )

    stored = store.update_autofix_job(
        job.job_key,
        state="opened",
        branch_name=published.branch,
        commit_sha=published.commit_sha,
        child_pr_url=published.pr_url,
        child_pr_number=published.pr_number,
        reasons=published.reasons,
        clear_lease=True,
        error="",
    )
    recorder.record(
        "publish", "opened", patch=patch, validation=validation, detail=published.pr_url
    )
    logger.info(
        "Autofix opened %s for finding %s on %s",
        published.pr_url or published.branch,
        job.finding_id,
        job.pr_url,
    )
    return RunResult(job=stored or job, reasons=published.reasons, patch=patch)


async def _run_handoff(
    provider: Any,
    pr_info: Any,
    job: AutofixJob,
    finding: ReviewFinding,
    policy: EffectivePolicy,
    store: Any,
    recorder: _Recorder,
) -> RunResult:
    context = handoff_module.HandoffContext(
        job=job,
        finding_title=finding.title,
        finding_body=finding.body,
        finding_path=finding.path,
        finding_line=finding.start_line,
        pr_url=job.pr_url,
        pr_title=getattr(pr_info, "title", ""),
        head_sha=job.head_sha,
        options=dict(policy.handoff.options),
        provider=provider,
        pr_info=pr_info,
    )
    result = await handoff_module.dispatch(policy.handoff.adapter, context)
    if not result.ok:
        return _fail(
            store,
            job,
            recorder,
            "handoff",
            [Reason(ReasonCode.PUBLISH_FAILED, result.detail or "The handoff failed")],
            policy,
        )
    reasons = [Reason(ReasonCode.HANDED_OFF, result.detail or "Handed off", "info")]
    stored = store.update_autofix_job(
        job.job_key,
        state="opened",
        handoff_ref=result.ref,
        reasons=reasons,
        clear_lease=True,
    )
    recorder.record("handoff", "ok", reasons=reasons, detail=result.ref)
    return RunResult(job=stored or job, reasons=reasons)


def _stopped_by_admin(store: Any, job: AutofixJob) -> Reason | None:
    """Whether this job stopped being ours while we were working on it.

    Cancellation clears the lease and moves the state, which the worker's
    heartbeat notices — but a heartbeat runs on a timer and a publish does not
    wait for one. Re-reading the row immediately before the first write is what
    turns "the worker will stop soon" into "nothing was written", which is the
    only version of cancellation worth having.

    A store that cannot answer is *not* treated as a cancellation: an unreadable
    row is an infrastructure problem, and refusing to publish a validated patch
    over one would turn a database blip into a lost fix.
    """
    try:
        current = store.get_autofix_job(job.job_key)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Could not re-read %s before publishing: %s", job.job_key, exc)
        return None
    if current is None or current.state != "cancelled":
        return None
    actor = current.cancelled_by or "an admin"
    return Reason(
        ReasonCode.CANCELLED_BY_ADMIN,
        f"@{actor} cancelled this job before anything was written",
        "info",
    )


def _fail(
    store: Any,
    job: AutofixJob,
    recorder: _Recorder,
    phase: str,
    reasons: list[Reason],
    policy: EffectivePolicy,
    *,
    record: bool = True,
    patch: FixPatch | None = None,
) -> RunResult:
    """Park or reschedule a failed attempt, and say which and why.

    Retryability is decided from the *reason*, not the phase or the exception.
    A model that returned nothing might return something next time; a protected
    path will still be protected, and spending two more attempts discovering
    that only delays telling somebody.
    """
    if record:
        recorder.record(phase, "failed", reasons=reasons, patch=patch)

    message = "; ".join(reason.message for reason in reasons)
    permanent = any(reason.code in NON_RETRYABLE_CODES for reason in reasons)
    exhausted = job.attempts >= job.max_attempts

    if permanent or exhausted:
        if exhausted and not permanent:
            reasons = [
                *reasons,
                Reason(
                    ReasonCode.ATTEMPT_LIMIT,
                    f"Gave up after {job.attempts} of {job.max_attempts} attempt(s)",
                ),
            ]
        stored = store.dead_letter_autofix_job(job.job_key, reasons=reasons, error=message)
        logger.info("Autofix job %s dead-lettered: %s", job.job_key, message)
        return RunResult(job=stored or job, reasons=reasons, patch=patch)

    stored = store.update_autofix_job(
        job.job_key,
        state="failed",
        reasons=reasons,
        error=message,
        available_at=time.time() + policy.retry_backoff_seconds,
        clear_lease=True,
    )
    logger.info(
        "Autofix job %s failed (attempt %d/%d), retrying: %s",
        job.job_key,
        job.attempts,
        job.max_attempts,
        message,
    )
    return RunResult(job=stored or job, reasons=reasons, patch=patch)


def _default_llm(config: MiraConfig) -> Any:
    from mira.dashboard.models_config import llm_config_for
    from mira.llm import create_llm

    return create_llm(llm_config_for("review", config.llm))


async def _build_context(
    provider: Any,
    pr_info: Any,
    job: AutofixJob,
    finding: ReviewFinding,
    policy: EffectivePolicy,
    store: Any,
) -> FixContext:
    """Gather what the generator may see. Editable files only.

    The file list is derived from the policy rather than from the finding, so
    the model is never shown a file it would then be refused permission to
    edit — an offer the pipeline is going to withdraw is worse than no offer.
    """
    from mira.autofix.patch import PatchRefused as _Refused
    from mira.autofix.patch import check_path

    changed = [stat.path for stat in await provider.get_pr_change_stats(pr_info)]
    changed_set = set(changed)
    candidates: list[str] = []
    for path in [finding.path, *changed]:
        if not path or path in candidates:
            continue
        try:
            check_path(path, policy=policy, changed_paths=changed_set, known=True)
        except _Refused:
            continue
        candidates.append(path)
        if len(candidates) >= max(policy.max_files, 1) + 2:
            break

    ref = job.head_sha or getattr(pr_info, "head_sha", "") or getattr(pr_info, "head_branch", "")
    sources: dict[str, str] = {}
    for path in candidates:
        try:
            content = await provider.get_file_content(pr_info, path, ref)
        except Exception as exc:  # noqa: BLE001 - a missing file is one fewer, not a crash
            logger.debug("Autofix could not read %s@%s: %s", path, ref, exc)
            continue
        if content:
            sources[path] = content

    previous = _previous_failures(store, job)
    return FixContext(
        finding_title=finding.title,
        finding_body=finding.body,
        finding_path=finding.path,
        finding_line=finding.start_line,
        finding_severity=finding.severity,
        finding_category=finding.category,
        finding_suggestion=finding.suggestion,
        pr_title=getattr(pr_info, "title", ""),
        sources=sources,
        diff=await _safe_diff(provider, pr_info),
        previous_failures=previous[0],
        previous_diff=previous[1],
        ci_summary=previous[2],
    )


async def _safe_diff(provider: Any, pr_info: Any) -> str:
    try:
        return str(await provider.get_pr_diff(pr_info) or "")
    except Exception as exc:  # noqa: BLE001 - the diff is context, not a requirement
        logger.debug("Autofix could not read the diff for %s: %s", pr_info.url, exc)
        return ""


def _previous_failures(store: Any, job: AutofixJob) -> tuple[list, str, str]:
    """What the last attempt was rejected for, so the next one can answer it."""
    try:
        attempts = store.list_autofix_attempts(job_key=job.job_key, limit=20)
    except Exception:  # noqa: BLE001 - missing history only costs context
        return [], "", ""
    for attempt in reversed(attempts):
        if attempt.phase == "validate" and attempt.outcome == "failed":
            return list(attempt.validation.failures), attempt.diff, ""
        if attempt.phase == "ci_retry":
            return [], attempt.diff, attempt.detail
    return [], "", ""
