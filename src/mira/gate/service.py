"""Running the gate: gather, decide, persist, deliver.

The order is load-bearing and never varies.

1. **Resolve the policy.** If the gate is off for this repository, stop here.
   Nothing is fetched, nothing is written but a `skipped` row — an install that
   never turned the gate on must not pay for it, and a repository that opted
   out must not have its pull-request data copied into a decision.
2. **Gather inputs**, under a wall-clock budget. Anything that raises, and any
   budget overrun, produces an ``error`` decision.
3. **Decide**, purely, in :mod:`mira.gate.decide`.
4. **Persist** before acting. A decision that was delivered but never recorded
   is an approval nobody can audit; a decision recorded but not yet delivered
   is a retry.
5. **Deliver**, claiming each side effect first so a redelivered webhook or a
   second worker cannot approve twice.

The state only becomes ``approved`` in step 5, after the platform confirms it.
Every earlier step's failure path leaves it at something else, which is what
"fail closed" means here: not a handler that catches the bad case, but a value
that was never set to the good one.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any

from mira.config import MiraConfig, load_config
from mira.gate import capabilities as caps
from mira.gate.codeowners import CodeownersFile
from mira.gate.codeowners import parse as parse_codeowners
from mira.gate.decide import decide
from mira.gate.eligibility import classify_paths
from mira.gate.explain import (
    admin_explanation,
    one_line,
    public_explanation,
    status_conclusion,
    status_title,
)
from mira.gate.models import (
    RETRYABLE_DELIVERY_STATES,
    STATUS_CONTEXT,
    CIState,
    GateDecision,
    GateInputs,
    Reason,
    ReasonCode,
    decision_key,
    delivery_key,
    override_key,
)
from mira.gate.policy import EffectivePolicy, resolve_policy
from mira.models import FileChangeStat

logger = logging.getLogger(__name__)

# Marker for the gate's PR comment, so updates land in place instead of
# stacking a new comment on every push.
COMMENT_MARKER = "<!-- mira:merge-gate -->"


class GateUnavailable(Exception):
    """An input the decision depends on could not be read.

    Raised rather than returning a partial :class:`GateInputs`, so there is no
    code path where a missing fact silently reads as a benign one.
    """


@dataclass
class ReviewSignal:
    """What the review pass knows, passed in rather than re-fetched.

    The gate usually runs right after a review that has already parsed the
    diff, counted the findings and knows whether every file was covered.
    Re-deriving that would cost a second diff fetch per PR for nothing.
    """

    # Every file the PR touches, with its own line counts — not the review's
    # filtered list. A file Mira chose not to review can still be protected.
    changes: list[FileChangeStat] | None = None
    # Finding counts the caller already has. The store is consulted regardless
    # and the larger of the two wins — a dry-run review never persists its
    # findings, and taking the max can only make the gate stricter.
    open_blockers: int = 0
    open_warnings: int = 0
    open_security: int = 0
    open_findings: int = 0
    worst_severity: str = ""
    review_complete: bool = True
    skipped_paths: list[str] | None = None
    review_failed: str = ""
    review_id: int = 0


# Severity ordering, for reconciling the store's worst finding with the
# caller's. Not `Severity.from_str`, which resolves an unknown value to
# `suggestion` — here an unrecognised severity must not silently outrank or
# understate a real one, so it is simply ranked lowest and ignored.
_SEVERITY_RANK = {"blocker": 4, "warning": 3, "suggestion": 2, "nitpick": 1}


def _worst_severity(*candidates: str) -> str:
    """The most severe of several reported worst-severities."""
    best, best_rank = "", 0
    for candidate in candidates:
        rank = _SEVERITY_RANK.get((candidate or "").lower(), 0)
        if rank > best_rank:
            best, best_rank = candidate, rank
    return best


def _open_store(owner: str, repo: str, platform: str) -> Any:
    from mira.index.store import IndexStore

    return IndexStore.open(owner, repo, platform=platform)


def _index_ready(owner: str, repo: str, platform: str) -> bool:
    """Whether the repository index is complete enough to have informed the review.

    A registry that cannot be reached returns False. "We could not check" and
    "the index is missing" lead to the same conservative place, and the gate
    has no business inventing a third.
    """
    try:
        from mira.dashboard.api import _app_db

        if _app_db is None:  # pragma: no cover - unconfigured installs
            return False
        record = _app_db.get_repo(owner, repo, platform=platform)
    except Exception as exc:  # noqa: BLE001
        logger.debug("Gate could not read repo index status for %s/%s: %s", owner, repo, exc)
        return False
    return bool(record and record.status == "ready")


async def _codeowners_for(provider: Any, pr_info: Any, policy: EffectivePolicy) -> CodeownersFile:
    """Read and parse CODEOWNERS, or say why it could not be read.

    Only called when the integration is enabled. A read failure becomes
    ``unreadable`` rather than propagating, because the *policy* decides what
    an unreadable ownership map means — and in ``block`` mode it means no
    automatic approval, which is a decision, not an error.
    """
    if policy.codeowners == "off":
        return CodeownersFile(status="not_checked")
    getter = getattr(provider, "get_codeowners", None)
    if getter is None:
        return CodeownersFile(status="unreadable", error="provider cannot read CODEOWNERS")
    try:
        path, content = await getter(pr_info)
    except Exception as exc:  # noqa: BLE001 - conservative, not fatal
        return CodeownersFile(status="unreadable", error=str(exc))
    if not content:
        return CodeownersFile(status="absent", path=path)
    return parse_codeowners(content, source_path=path)


async def gather_inputs(
    provider: Any,
    pr_info: Any,
    policy: EffectivePolicy,
    *,
    bot_name: str = "",
    signal: ReviewSignal | None = None,
    capabilities: caps.GateCapabilities | None = None,
) -> GateInputs:
    """Collect every fact the decision needs. Raises on anything unreadable."""
    signal = signal or ReviewSignal()
    capability = capabilities or caps.for_provider(provider)

    labels: list[str] = []
    if capability.can_read_labels:
        labels = list(await provider.get_pr_labels(pr_info))
    elif policy.blocked_labels or policy.required_labels:
        # The policy asks a question this provider cannot answer, and guessing
        # the answer is exactly the failure the gate exists to prevent.
        raise GateUnavailable(
            f"{capability.provider} cannot read pull-request labels, which this policy requires"
        )

    association = "UNKNOWN"
    if capability.can_read_association:
        association = str(await provider.get_author_association(pr_info) or "UNKNOWN").upper()

    ci = CIState(state="unknown")
    if capability.can_read_ci:
        ci = await provider.get_ci_state(pr_info)

    if not capability.can_read_review_states:
        # An empty mapping would read as "no human has objected", which is the
        # one thing the gate must never assume on a provider's behalf.
        raise GateUnavailable(f"{capability.provider} cannot report human review states")
    human_states = dict(await provider.get_review_states(pr_info) or {})

    changes = (
        list(signal.changes)
        if signal.changes is not None
        else list(await provider.get_pr_change_stats(pr_info))
    )
    changed_paths = [change.path for change in changes]
    added = sum(change.added_lines for change in changes)
    deleted = sum(change.deleted_lines for change in changes)

    generated, protected = classify_paths(changed_paths, policy)
    generated_set = set(generated)
    generated_lines = sum(
        change.added_lines + change.deleted_lines
        for change in changes
        if change.path in generated_set
    )

    codeowners = await _codeowners_for(provider, pr_info, policy)
    owned = codeowners.owned_paths(changed_paths) if codeowners.status == "ok" else []

    owner = pr_info.owner
    repo = pr_info.repo
    platform = getattr(pr_info, "platform", "github")

    store = _open_store(owner, repo, platform)
    try:
        counts = store.gate_finding_counts(pr_info.number)
        # Read from the same store handle rather than through
        # `checks.service.latest_verdict`, which would open a second one: this
        # runs inside the gate's wall-clock budget, and on SQLite opening an
        # index store is a file open plus a schema pass.
        check_run = store.latest_check_run(
            pr_number=pr_info.number, head_sha=getattr(pr_info, "head_sha", "") or ""
        )
    finally:
        store.close()
    blockers = max(counts["blockers"], signal.open_blockers)
    warnings = max(counts["warnings"], signal.open_warnings)
    security = max(counts["security"], signal.open_security)
    open_findings = max(counts["open"], signal.open_findings)
    worst = _worst_severity(counts["worst"], signal.worst_severity)

    return GateInputs(
        platform=platform,
        owner=owner,
        repo=repo,
        pr_number=pr_info.number,
        pr_url=pr_info.url,
        pr_author=getattr(pr_info, "author", "") or "",
        base_branch=pr_info.base_branch,
        head_branch=pr_info.head_branch,
        head_sha=getattr(pr_info, "head_sha", "") or "",
        base_sha=getattr(pr_info, "base_sha", "") or "",
        draft=bool(getattr(pr_info, "draft", False)),
        labels=labels,
        author_association=association,
        changed_paths=list(changed_paths),
        changed_files=len(changed_paths),
        added_lines=int(added),
        deleted_lines=int(deleted),
        generated_paths=generated,
        generated_lines=generated_lines,
        protected_matches=protected,
        codeowner_matches=owned,
        codeowners_status=codeowners.status,
        ci=ci,
        open_blockers=int(blockers),
        open_warnings=int(warnings),
        open_security=int(security),
        open_findings=int(open_findings),
        worst_severity=worst,
        review_complete=signal.review_complete,
        review_skipped_paths=list(signal.skipped_paths or []),
        review_failed=signal.review_failed,
        # A run against an older commit is not evidence about this one, and
        # `latest_check_run` is asked for this head sha, so a pull request that
        # was pushed to since its last check run reports `not_run` — which the
        # gate ignores rather than treating as a pass.
        checks_verdict=check_run.verdict if check_run else "not_run",
        checks_blocking=(
            sorted(result.check_id for result in check_run.blocking_results) if check_run else []
        ),
        index_ready=_index_ready(owner, repo, platform),
        human_states=human_states,
        bot_login=bot_name,
        review_id=signal.review_id,
    )


def _error_decision(
    pr_info: Any,
    policy: EffectivePolicy,
    message: str,
    *,
    code: str = ReasonCode.EVALUATION_ERROR,
) -> GateDecision:
    """A decision that records the failure and approves nothing.

    Given its own inputs snapshot rather than a partial one, so an audit can
    never mistake "these are the facts we decided on" for "these are the facts
    we managed to fetch before it broke".
    """
    inputs = GateInputs(
        platform=getattr(pr_info, "platform", "github"),
        owner=pr_info.owner,
        repo=pr_info.repo,
        pr_number=pr_info.number,
        pr_url=pr_info.url,
        pr_author=getattr(pr_info, "author", "") or "",
        base_branch=getattr(pr_info, "base_branch", ""),
        head_sha=getattr(pr_info, "head_sha", "") or "",
    )
    return GateDecision(
        decision_key=decision_key(
            platform=inputs.platform,
            owner=inputs.owner,
            repo=inputs.repo,
            pr_number=inputs.pr_number,
            head_sha=inputs.head_sha,
            policy_version=policy.version,
            mode=policy.mode,
            inputs_digest=f"error:{code}",
        ),
        state="error",
        mode=policy.mode,  # type: ignore[arg-type]
        policy_version=policy.version,
        inputs=inputs,
        reasons=[Reason(code, message)],
        delivery_state="skipped",
        error=message,
    )


async def evaluate(
    provider: Any,
    pr_info: Any,
    *,
    config: MiraConfig | None = None,
    bot_name: str = "",
    signal: ReviewSignal | None = None,
    deliver_side_effects: bool = True,
) -> GateDecision:
    """Evaluate one pull request and record the decision. Never raises."""
    config = config or load_config()
    policy = resolve_policy(config.gate, pr_info.owner, pr_info.repo)

    if not policy.active:
        decision = decide(
            GateInputs(
                platform=getattr(pr_info, "platform", "github"),
                owner=pr_info.owner,
                repo=pr_info.repo,
                pr_number=pr_info.number,
                pr_url=pr_info.url,
                head_sha=getattr(pr_info, "head_sha", "") or "",
            ),
            policy,
        )
        _persist(decision)
        return decision

    capability = caps.for_provider(provider)
    try:
        inputs = await asyncio.wait_for(
            gather_inputs(
                provider,
                pr_info,
                policy,
                bot_name=bot_name,
                signal=signal,
                capabilities=capability,
            ),
            timeout=policy.timeout_seconds,
        )
    except TimeoutError:
        decision = _error_decision(
            pr_info,
            policy,
            f"Gate evaluation exceeded its {policy.timeout_seconds:g}s budget",
            code=ReasonCode.EVALUATION_TIMEOUT,
        )
        _persist(decision)
        logger.warning("Merge gate timed out on %s", pr_info.url)
        return decision
    except Exception as exc:  # noqa: BLE001 - every failure is a non-approval
        decision = _error_decision(pr_info, policy, f"{type(exc).__name__}: {exc}")
        _persist(decision)
        logger.warning("Merge gate could not evaluate %s: %s", pr_info.url, exc)
        return decision

    decision = decide(inputs, policy, capabilities=capability)
    stored, created = _persist(decision)
    logger.info("Merge gate on %s: %s", pr_info.url, one_line(stored))

    if deliver_side_effects and _worth_retrying(stored):
        stored = await deliver(provider, pr_info, stored, policy)
    return stored


# A channel that has refused this many times is not going to be talked round by
# another webhook. The retry exists for a transient 5xx, not for a missing
# scope — and an unbounded retry is one self-triggering event away from a loop.
_MAX_DELIVERY_ATTEMPTS = 5


def _worth_retrying(decision: GateDecision) -> bool:
    """Whether there is still something to send, and still a point in trying."""
    if decision.delivery_state not in RETRYABLE_DELIVERY_STATES:
        return False
    if decision.delivery_attempts >= _MAX_DELIVERY_ATTEMPTS:
        logger.info(
            "Merge gate giving up on delivery for %s after %d attempts (%s)",
            decision.inputs.pr_url,
            decision.delivery_attempts,
            decision.error or decision.delivery_state,
        )
        return False
    return True


def _persist(decision: GateDecision) -> tuple[GateDecision, bool]:
    """Write the decision, tolerating a store that is unavailable.

    A decision that cannot be recorded still has to be *returned* — the caller
    may be about to act on it — but it must not be acted on as if it were
    auditable, so the delivery state is forced to skipped.
    """
    inputs = decision.inputs
    try:
        store = _open_store(inputs.owner, inputs.repo, inputs.platform)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Merge gate could not open its store for %s: %s", inputs.pr_url, exc)
        decision.delivery_state = "skipped"
        decision.error = decision.error or f"decision not recorded: {exc}"
        return decision, False
    try:
        return store.record_gate_decision(decision)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Merge gate could not record a decision for %s: %s", inputs.pr_url, exc)
        decision.delivery_state = "skipped"
        decision.error = decision.error or f"decision not recorded: {exc}"
        return decision, False
    finally:
        store.close()


async def deliver(
    provider: Any,
    pr_info: Any,
    decision: GateDecision,
    policy: EffectivePolicy,
) -> GateDecision:
    """Perform the platform-side effects this decision calls for.

    Each irreversible action is claimed first. The claim is what makes a
    redelivered webhook, a retried background task and a second worker safe:
    the winner acts, the losers do nothing, and a *failed* attempt is
    reclaimable so a transient error still gets a second chance.
    """
    inputs = decision.inputs
    capability = caps.for_provider(provider)
    store = _open_store(inputs.owner, inputs.repo, inputs.platform)
    try:
        # Whether a review event — the delivery that actually matters — was
        # attempted. If one was, its outcome owns `delivery_state`: an
        # announcement that went out fine must not overwrite a failed approval
        # with "delivered", which would read as an approval that never happened.
        attempted_review_event = False
        if decision.request_changes:
            attempted_review_event = True
            await _deliver_review_event(provider, pr_info, decision, store, "REQUEST_CHANGES")
        elif decision.state == "would_approve" and policy.enforcing and capability.can_approve:
            attempted_review_event = True
            await _deliver_review_event(provider, pr_info, decision, store, "APPROVE")

        # The announcement channels only report whether they got through; this
        # function decides what the row says, so that adding or removing one
        # cannot leave the bookkeeping to whichever happens to run last.
        outcomes: list[tuple[bool, str, str]] = []
        if policy.publish_status and capability.can_publish_status:
            outcomes.append(await _publish_status(provider, pr_info, decision))
        if policy.comment:
            outcomes.append(await _publish_comment(provider, pr_info, decision))

        if outcomes and not attempted_review_event:
            # Three outcomes, not two. "Did the explanation reach the pull
            # request?" and "is there still something to send?" are different
            # questions, and collapsing them either hides a missing status
            # check behind a clean row or re-sends one that published fine.
            succeeded = sum(1 for ok, _ref, _error in outcomes if ok)
            if succeeded == len(outcomes):
                state = "delivered"
            elif succeeded:
                state = "partial"
            else:
                state = "failed"
            store.update_gate_decision_delivery(
                decision.decision_key,
                delivery_state=state,
                # An empty reference leaves the stored one alone: a comment has
                # none to give, so a retry where only the comment succeeds must
                # not erase the check-run id the status channel recorded.
                delivery_ref=next((ref for ok, ref, _e in outcomes if ok and ref), ""),
                error="; ".join(error for ok, _ref, error in outcomes if not ok),
                bump_attempts=True,
            )
            decision.delivery_state = state

        refreshed = store.get_gate_decision(decision.decision_key)
        return refreshed or decision
    finally:
        store.close()


async def _deliver_review_event(
    provider: Any,
    pr_info: Any,
    decision: GateDecision,
    store: Any,
    event: str,
) -> None:
    """Submit APPROVE / REQUEST_CHANGES exactly once per PR head commit."""
    inputs = decision.inputs
    kind = "approval" if event == "APPROVE" else "request_changes"
    key = delivery_key(
        platform=inputs.platform,
        owner=inputs.owner,
        repo=inputs.repo,
        pr_number=inputs.pr_number,
        head_sha=inputs.head_sha,
        kind=kind,
    )
    if not store.claim_gate_delivery(
        delivery_key=key,
        decision_key=decision.decision_key,
        platform=inputs.platform,
        owner=inputs.owner,
        repo=inputs.repo,
        pr_number=inputs.pr_number,
        head_sha=inputs.head_sha,
        kind=kind,
    ):
        # Someone else owns this side effect. Adopt whatever they achieved with
        # it, so this decision settles instead of looking un-attempted and
        # re-entering `deliver()` on every later webhook.
        existing = store.get_gate_delivery(key) or {}
        existing_state = str(existing.get("state") or "in_flight")
        logger.info(
            "Merge gate skipping a duplicate %s on %s (delivery already %s)",
            event,
            inputs.pr_url,
            existing_state,
        )
        approved = event == "APPROVE" and existing_state == "delivered"
        store.update_gate_decision_delivery(
            decision.decision_key,
            delivery_state=existing_state,
            delivery_ref=str(existing.get("ref", "")),
            error=str(existing.get("error", "")),
            state="approved" if approved else None,
        )
        decision.delivery_state = existing_state
        if approved:
            # Another worker already approved this exact commit. Saying so here
            # keeps the two decision rows from disagreeing about the same PR.
            decision.state = "approved"
        return

    body = public_explanation(decision)
    try:
        submitted = bool(await provider.submit_verdict(pr_info, event, body))
    except Exception as exc:  # noqa: BLE001 - a failed delivery is not an approval
        store.finish_gate_delivery(key, state="failed", error=str(exc))
        store.update_gate_decision_delivery(
            decision.decision_key,
            delivery_state="failed",
            error=str(exc),
            bump_attempts=True,
        )
        decision.delivery_state = "failed"
        logger.warning("Merge gate could not submit %s on %s: %s", event, inputs.pr_url, exc)
        return

    if not submitted:
        # The platform refused — self-approval, missing permission, a tier that
        # does not have approvals. The decision stays advisory and says so, in
        # the stored row as well as in memory: a decision whose reasons predate
        # its own delivery cannot explain itself to whoever reads it later.
        decision.reasons.append(
            Reason(
                ReasonCode.APPROVAL_REFUSED,
                "The platform refused to record the review event",
                "info",
            )
        )
        store.finish_gate_delivery(key, state="failed", error="platform refused the review event")
        store.update_gate_decision_delivery(
            decision.decision_key,
            delivery_state="failed",
            error="platform refused the review event",
            bump_attempts=True,
            reasons=decision.reasons,
        )
        decision.delivery_state = "failed"
        return

    store.finish_gate_delivery(key, state="delivered")
    if event == "APPROVE":
        store.update_gate_decision_delivery(
            decision.decision_key,
            delivery_state="delivered",
            state="approved",
            bump_attempts=True,
        )
        decision.state = "approved"
    else:
        store.update_gate_decision_delivery(
            decision.decision_key, delivery_state="delivered", bump_attempts=True
        )
    decision.delivery_state = "delivered"


async def _publish_status(
    provider: Any, pr_info: Any, decision: GateDecision
) -> tuple[bool, str, str]:
    """Publish the explanation as a check run / commit status.

    Returns ``(published, reference, error)``. Not claimed: a status is an
    update to a named artifact, so re-sending one replaces it. A failure here
    never changes what the decision *is* — it is how the decision is announced.
    """
    try:
        ref = await provider.publish_gate_status(
            pr_info,
            context=STATUS_CONTEXT,
            conclusion=status_conclusion(decision),
            title=status_title(decision),
            summary=public_explanation(decision),
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("Merge gate could not publish its status on %s: %s", pr_info.url, exc)
        return False, "", str(exc)
    return True, str(ref or ""), ""


async def _publish_comment(
    provider: Any, pr_info: Any, decision: GateDecision
) -> tuple[bool, str, str]:
    """Post or update the gate's PR comment, in place.

    Returns ``(posted, reference, error)``. On a provider with no status check
    this is the only place the explanation reaches the pull request, so whether
    it got through is worth recording.

    The reference is always empty. This comment is addressed by its marker
    rather than by an id — that is what lets an update land in place — and
    ``post_comment`` does not return one anyway, so reporting an id on updates
    and nothing on creations would only make the column arbitrary.
    """
    body = f"{COMMENT_MARKER}\n{public_explanation(decision)}"
    try:
        existing = await provider.find_bot_comment(pr_info, COMMENT_MARKER)
        if existing is not None:
            await provider.update_comment(pr_info, existing, body)
        else:
            await provider.post_comment(pr_info, body)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Merge gate could not post its comment on %s: %s", pr_info.url, exc)
        return False, "", str(exc)
    return True, "", ""


# ─────────────────────────────────────────────────────────────── overrides ──


class OverrideDenied(Exception):
    """An override that policy or a veto does not permit."""


@dataclass
class OverrideResult:
    decision: GateDecision
    override: dict[str, Any]
    created: bool


def apply_override(
    *,
    owner: str,
    repo: str,
    platform: str,
    decision_id: int,
    actor: str,
    reason: str,
    new_state: str,
    config: MiraConfig | None = None,
    nonce: str = "",
) -> OverrideResult:
    """Move a recorded decision by hand, with the trail that makes it auditable.

    An override changes *Mira's record*. It never submits or retracts a review
    event on the platform. That boundary is the point: if an override could
    reach through to an approval, "who may administer Mira" and "who may
    approve this pull request" would collapse into one permission, and the
    platform's own review would stop being the thing that gates the merge.

    Authorization has already been checked by the caller — this function
    enforces the *policy* limits, which are a separate question from who the
    actor is:

    - Overrides can be disabled entirely for the deployment.
    - Forcing an approval is its own opt-in, distinct from revoking one.
      Revoking is always available; that asymmetry is deliberate.
    - No override can approve past a hard veto. A protected path, an open
      blocker or a human's requested changes are not opinions the gate formed
      and an admin can wave off — they are the reasons this phase exists.
    """
    config = config or load_config()
    policy = resolve_policy(config.gate, owner, repo)
    if not policy.allow_overrides:
        raise OverrideDenied("Overrides are disabled for this deployment")
    if new_state not in {"approved", "not_approved"}:
        raise OverrideDenied("An override can only set 'approved' or 'not_approved'")
    if not reason.strip():
        raise OverrideDenied("An override must record a reason")

    store = _open_store(owner, repo, platform)
    try:
        decision = store.get_gate_decision_by_id(decision_id)
        if decision is None:
            raise OverrideDenied("No such gate decision")
        if new_state == "approved":
            if not policy.allow_approval_override:
                raise OverrideDenied(
                    "Forcing an approval is disabled (gate.allow_approval_override)"
                )
            vetoes = decision.hard_vetoes
            if vetoes:
                raise OverrideDenied(
                    "This decision cannot be overridden into an approval: "
                    + "; ".join(veto.message for veto in vetoes)
                )
        key = override_key(
            decision_key_value=decision.decision_key,
            actor=actor,
            new_state=new_state,
            nonce=nonce,
        )
        override, created = store.record_gate_override(
            override_key=key,
            decision=decision,
            actor=actor,
            reason=reason.strip(),
            new_state=new_state,
            detail={
                "previous_delivery_state": decision.delivery_state,
                "previous_reasons": [item.as_dict() for item in decision.reasons],
                "policy_version": decision.policy_version,
            },
        )
        refreshed = store.get_gate_decision(decision.decision_key) or decision
        return OverrideResult(decision=refreshed, override=override or {}, created=created)
    finally:
        store.close()


def explain(decision: GateDecision, *, admin: bool = False) -> str:
    """The rendered explanation for a stored decision."""
    return admin_explanation(decision) if admin else public_explanation(decision)
