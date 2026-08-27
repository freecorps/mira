"""Running the checks: schedule, bound, deduplicate, decide.

The scheduler is the only place in this package that knows about time,
concurrency and modes, and that is on purpose. A check is a coroutine that
answers a question; everything about *how many run at once*, *how long one may
take*, and *what its answer means for a merge* is decided here, so that a check
cannot get any of it wrong on its own behalf.

Four properties this module is responsible for.

**Independence.** Every check gets the same context and none of them sees
another's result. A check cannot read, influence or short-circuit another, so a
run is a set of independent answers rather than a pipeline where an early
failure quietly changes a later verdict.

**Bounds.** Concurrency is capped — two at a time by default, because the
reference deployment is a four-core board that is also serving webhooks. Each
check has its own wall-clock ceiling, and the run has one. A check that
overruns is ``timeout``; a check that never started because the run's budget
was spent is ``skipped`` with the budget named. Neither is a pass, and neither
is a violation.

**Reproducibility.** Checks run against one immutable context built before any
of them starts, in a deterministic order, and the identity of every result
includes the check's version and the digest of the configuration it ran under.
Two runs over the same commit under the same policy produce the same rows —
which is what makes the run key idempotent rather than merely convenient.

**Honesty about what a result means.** A check that raised produces an
``infrastructure_error`` with the exception on it, never a violation. A check
that reported a violation with no evidence is downgraded to ``skipped`` right
here, because a violation nobody can look up is a guess, and the framework's
one promise is that it does not make those.
"""

from __future__ import annotations

import asyncio
import logging
import time

from mira.checks.context import CheckContext, CheckOutcome
from mira.checks.dedupe import deduplicate
from mira.checks.models import (
    CheckFinding,
    CheckResult,
    CheckRun,
    CheckRunInputs,
    SkipReason,
    result_key,
    run_key,
)
from mira.checks.policy import EffectiveChecksPolicy
from mira.checks.registry import CheckSpec, specs_for

logger = logging.getLogger(__name__)


def _skipped(
    spec: CheckSpec, policy: EffectiveChecksPolicy, summary: str, reason: str
) -> CheckResult:
    return CheckResult(
        check_id=spec.check_id,
        check_version=spec.version,
        title=spec.title,
        origin=spec.origin,
        mode=policy.mode_for(spec.check_id),  # type: ignore[arg-type]
        state="skipped",
        summary=summary,
        skip_reason=reason,
        config_digest=policy.config_digest_for(spec.check_id),
        sources=[spec.check_id],
    )


# Findings kept from one check. Every producer already caps itself, and this
# is the cap that does not depend on all of them remembering to: a result row
# is an audit record, not a second copy of the diff, and a check that returned
# four hundred findings has found one pattern.
MAX_FINDINGS_PER_CHECK = 25


def _trim(findings: list[CheckFinding], limit: int) -> list[CheckFinding]:
    kept = findings[:MAX_FINDINGS_PER_CHECK]
    for finding in kept:
        finding.evidence = finding.evidence[:limit]
    return kept


def _result_from(
    spec: CheckSpec,
    policy: EffectiveChecksPolicy,
    outcome: CheckOutcome,
    duration: float,
) -> CheckResult:
    """Turn a check's answer into a persisted result, enforcing the one rule.

    The rule: a violation must carry evidence. A check that reported one
    without any is recorded as ``skipped`` with ``no_evidence`` — which still
    counts as unanswered, so a blocking check cannot be satisfied by making an
    unsupported claim. The check's own words are kept so the operator can see
    what it tried to say.
    """
    mode = policy.mode_for(spec.check_id)
    limit = policy.max_evidence_per_check
    state = outcome.state
    skip_reason = outcome.skip_reason
    summary = outcome.summary

    if state == "violation":
        has_evidence = any(finding.evidence for finding in outcome.findings) or bool(
            outcome.evidence
        )
        if not outcome.findings or not has_evidence:
            state = "skipped"
            skip_reason = SkipReason.NO_EVIDENCE
            summary = (
                "This check reported a problem and produced no evidence for it, so "
                "Mira is not recording it against this pull request. "
                f"It said: {outcome.summary}"
            )
            outcome.findings = []

    return CheckResult(
        check_id=spec.check_id,
        check_version=spec.version,
        title=spec.title,
        origin=spec.origin,
        mode=mode,  # type: ignore[arg-type]
        state=state,
        summary=summary[:2_000],
        evidence=list(outcome.evidence)[:limit],
        findings=_trim(list(outcome.findings), limit),
        skip_reason=skip_reason,
        error=outcome.error[:2_000],
        duration_seconds=round(duration, 4),
        config_digest=policy.config_digest_for(spec.check_id),
        sources=[spec.check_id],
    )


async def _run_one(
    spec: CheckSpec,
    ctx: CheckContext,
    policy: EffectiveChecksPolicy,
    semaphore: asyncio.Semaphore,
    deadline: float,
) -> CheckResult:
    """Run one check under its own ceiling. Never raises."""
    mode = policy.mode_for(spec.check_id)
    if mode == "off":
        # Recorded rather than omitted: "this check is off" and "this check
        # does not exist in this version" are different facts, and a dashboard
        # that cannot tell them apart cannot be trusted about coverage.
        return _skipped(
            spec, policy, "This check is switched off for this repository.", SkipReason.DISABLED
        )

    remaining = deadline - time.monotonic()
    if remaining <= 0:
        return _skipped(
            spec,
            policy,
            f"The run's {policy.total_timeout_seconds:g}s budget was spent before this "
            "check started, so it did not run and says nothing about this change.",
            SkipReason.BUDGET_EXHAUSTED,
        )

    async with semaphore:
        # The clock is read again here rather than above, because this check
        # may have queued behind others: a ceiling measured before waiting
        # would hand a late check the budget of an early one and blow the run's
        # total.
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return _skipped(
                spec,
                policy,
                f"The run's {policy.total_timeout_seconds:g}s budget was spent while this "
                "check was queued, so it did not run.",
                SkipReason.BUDGET_EXHAUSTED,
            )
        budget = min(policy.check_timeout_seconds, remaining)
        started = time.monotonic()
        try:
            outcome = await asyncio.wait_for(spec.run(ctx), timeout=budget)
        except TimeoutError:
            return CheckResult(
                check_id=spec.check_id,
                check_version=spec.version,
                title=spec.title,
                origin=spec.origin,
                mode=mode,  # type: ignore[arg-type]
                state="timeout",
                summary=(
                    f"This check was still running after {budget:g}s and was stopped, so "
                    "it reached no conclusion about this change."
                ),
                error=f"exceeded its {budget:g}s budget",
                duration_seconds=round(time.monotonic() - started, 4),
                config_digest=policy.config_digest_for(spec.check_id),
                sources=[spec.check_id],
            )
        except asyncio.CancelledError:
            # The whole run is being torn down. Propagating is correct: a
            # cancelled check has not produced a result and inventing one would
            # record an answer nobody computed.
            raise
        except Exception as exc:  # noqa: BLE001 - a broken check is never a violation
            logger.warning("Check %s failed on %s: %s", spec.check_id, ctx.pr_url, exc)
            outcome = CheckOutcome.failed(
                error=f"{type(exc).__name__}: {exc}",
                summary=(
                    "This check raised while running, so it says nothing about this "
                    "change. This is a Mira problem, not a problem with the change."
                ),
            )

    return _result_from(spec, policy, outcome, time.monotonic() - started)


async def run_checks(ctx: CheckContext, inputs: CheckRunInputs) -> CheckRun:
    """Run every check this policy carries, and return the recorded run.

    Does not persist and does not touch a platform: this is the pure half, and
    :mod:`mira.checks.service` is the half with side effects. Splitting them
    means a test can drive a whole run with no store, no provider and no
    network, which is most of what makes the state matrix testable at all.
    """
    policy = ctx.policy
    started = time.monotonic()
    deadline = started + policy.total_timeout_seconds
    ctx.deadline = deadline

    specs = specs_for(policy)
    semaphore = asyncio.Semaphore(max(1, policy.max_concurrency))

    gathered = await asyncio.gather(
        *(_run_one(spec, ctx, policy, semaphore, deadline) for spec in specs)
    )
    results = deduplicate(list(gathered))

    key = run_key(
        platform=inputs.platform,
        owner=inputs.owner,
        repo=inputs.repo,
        pr_number=inputs.pr_number,
        head_sha=inputs.head_sha,
        policy_version=policy.version,
        inputs_digest=inputs.digest,
    )
    for result in results:
        result.result_key = result_key(run_key_value=key, check_id=result.check_id)

    run = CheckRun(
        run_key=key,
        policy_version=policy.version,
        inputs=inputs,
        results=results,
        duration_seconds=round(time.monotonic() - started, 4),
    )
    logger.info(
        "Pre-merge checks on %s: %s (%s)",
        inputs.pr_url or f"{inputs.owner}/{inputs.repo}#{inputs.pr_number}",
        run.verdict,
        ", ".join(f"{state}={count}" for state, count in sorted(run.counts().items()) if count),
    )
    return run
