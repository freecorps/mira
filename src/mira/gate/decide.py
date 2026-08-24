"""The decision itself: inputs plus policy in, a :class:`GateDecision` out.

Pure. No I/O, no clock, no randomness — hand it the same inputs and policy and
it returns the same decision, which is what makes a stored decision auditable
years later and what lets the tests cover the matrix exhaustively without a
platform anywhere near them.

The ordering is the safety property. Scope first (is this ours to judge), then
disqualifications, then risk, and only then — after everything that could say
no has spoken — the question of whether we may act. There is no path from
``error`` or from a hard veto to ``approved``; the function cannot express one.
"""

from __future__ import annotations

from mira.gate.capabilities import NO_CAPABILITIES, GateCapabilities
from mira.gate.eligibility import blocking_reasons, scope_reasons
from mira.gate.models import (
    GateDecision,
    GateInputs,
    Reason,
    ReasonCode,
    decision_key,
    risk_band,
)
from mira.gate.policy import EffectivePolicy
from mira.gate.risk import score as score_risk


def decide(
    inputs: GateInputs,
    policy: EffectivePolicy,
    *,
    capabilities: GateCapabilities = NO_CAPABILITIES,
) -> GateDecision:
    """Evaluate one PR against one policy."""
    key = decision_key(
        platform=inputs.platform,
        owner=inputs.owner,
        repo=inputs.repo,
        pr_number=inputs.pr_number,
        head_sha=inputs.head_sha,
        policy_version=policy.version,
        mode=policy.mode,
        inputs_digest=inputs.digest,
    )
    base = GateDecision(
        decision_key=key,
        mode=policy.mode,  # type: ignore[arg-type]
        policy_version=policy.version,
        inputs=inputs,
        capabilities=capabilities.as_dict(),
    )

    # ── Out of scope ─────────────────────────────────────────────────────
    scope = scope_reasons(inputs, policy)
    if scope:
        base.state = "skipped"
        base.reasons = scope
        base.delivery_state = "skipped"
        return base

    # Risk is computed for every in-scope PR, including ones already
    # disqualified: a shadow rollout needs the score of the PRs it refused as
    # much as the score of the ones it would have approved, or the threshold
    # can only ever be tuned from half the data.
    total, factors = score_risk(inputs, policy.weights)
    base.risk_score = total
    base.factors = factors
    base.risk_band = risk_band(total, medium_at=policy.risk_medium_at, high_at=policy.risk_high_at)

    # ── Disqualified ─────────────────────────────────────────────────────
    blocked = blocking_reasons(inputs, policy)
    if total > policy.risk_threshold:
        blocked.append(
            Reason(
                ReasonCode.RISK_ABOVE_THRESHOLD,
                f"Risk score {total} is above the approval threshold of {policy.risk_threshold}",
            )
        )

    if blocked:
        base.state = "not_approved"
        base.reasons = blocked
        base.request_changes = _wants_request_changes(inputs, policy, capabilities, blocked)
        base.delivery_state = "pending" if _has_delivery(policy, capabilities, base) else "skipped"
        if base.request_changes and not capabilities.can_request_changes:
            base.reasons.append(
                Reason(
                    ReasonCode.PROVIDER_CANNOT_REQUEST_CHANGES,
                    f"{capabilities.provider} cannot record a blocking review event; "
                    "the findings stay as review comments",
                    "info",
                )
            )
            base.request_changes = False
        return base

    # ── Would approve ────────────────────────────────────────────────────
    reasons = [
        Reason(
            ReasonCode.ELIGIBLE,
            f"Eligible with risk score {total} (threshold {policy.risk_threshold})",
            "info",
        )
    ]

    if not policy.enforcing:
        reasons.append(
            Reason(
                ReasonCode.SHADOW_MODE,
                "The gate is in shadow mode, so nothing was submitted to the platform",
                "info",
            )
        )
        base.state = "would_approve"
        base.reasons = reasons
        base.delivery_state = "pending" if _has_delivery(policy, capabilities, base) else "skipped"
        return base

    if not capabilities.can_approve:
        reasons.append(
            Reason(
                ReasonCode.PROVIDER_CANNOT_APPROVE,
                f"{capabilities.provider} cannot record an approval, so the decision "
                "stays advisory",
                "info",
            )
        )
        base.state = "would_approve"
        base.reasons = reasons
        base.delivery_state = "pending" if _has_delivery(policy, capabilities, base) else "skipped"
        return base

    # Still `would_approve` here: the state only becomes `approved` once the
    # platform has confirmed the approval. The service flips it after delivery,
    # and a delivery that fails leaves this value untouched — which is exactly
    # the fail-closed behaviour, expressed as a default rather than a handler.
    base.state = "would_approve"
    base.reasons = reasons
    base.delivery_state = "pending"
    return base


def _wants_request_changes(
    inputs: GateInputs,
    policy: EffectivePolicy,
    capabilities: GateCapabilities,
    reasons: list[Reason],
) -> bool:
    """Whether to submit REQUEST_CHANGES alongside a `not_approved`.

    Only for open blockers, only in enforce mode, only when configured, and
    never when a human has already reviewed. Mira's job is to say what it
    found; superseding or standing in for a human's review event is not part of
    it, in either direction — a REQUEST_CHANGES over someone's APPROVE is just
    as much of an overwrite as an APPROVE over their CHANGES_REQUESTED.
    """
    if not policy.request_changes_on_blockers or not policy.enforcing:
        return False
    if not capabilities.can_request_changes:
        return False
    if not any(reason.code == ReasonCode.OPEN_BLOCKER for reason in reasons):
        return False
    human_reviewed = any(
        state in {"APPROVED", "CHANGES_REQUESTED"}
        for login, state in (inputs.human_states or {}).items()
        if not login.endswith("[bot]")
    )
    return not human_reviewed


def _has_delivery(
    policy: EffectivePolicy, capabilities: GateCapabilities, decision: GateDecision
) -> bool:
    """Whether anything is left to send to the platform for this decision."""
    if decision.request_changes:
        return True
    if policy.publish_status and capabilities.can_publish_status:
        return True
    return bool(policy.comment)
