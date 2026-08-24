"""Vocabulary and records for the merge gate.

Everything the gate decides is expressed with the codes in this module, so a
decision is a value the dashboard, the tests and the audit trail can all read
without re-deriving anything. Nothing here talks to a provider, a store or an
LLM — it is the shape of a decision, not how one is reached.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from typing import Any, Literal

# ── States ───────────────────────────────────────────────────────────────────
#
# `skipped`        The gate had no business deciding: it is off for this repo,
#                  the PR is out of scope, or the diff carries nothing it can
#                  reason about. No opinion is recorded as an opinion.
# `not_approved`   The gate looked and decided against approving. Always the
#                  answer when anything is uncertain, incomplete or protected.
# `would_approve`  The gate would have approved but did not act: shadow mode,
#                  or a provider that cannot record an approval. Never rendered
#                  or reported as an approval.
# `approved`       A real approval was delivered to the platform.
# `error`          Evaluation or delivery failed. Fails closed by construction:
#                  `error` is not an approval and never becomes one.
GateState = Literal["approved", "would_approve", "not_approved", "skipped", "error"]

GATE_STATES: tuple[GateState, ...] = (
    "approved",
    "would_approve",
    "not_approved",
    "skipped",
    "error",
)

# States that mean "no approval happened". Used everywhere a caller is about to
# treat a decision as permission to merge.
NON_APPROVING_STATES: frozenset[str] = frozenset(
    {"would_approve", "not_approved", "skipped", "error"}
)

GateMode = Literal["off", "shadow", "enforce"]

GATE_MODES: tuple[GateMode, ...] = ("off", "shadow", "enforce")

# Delivery of the platform-side artifact (approval, request-changes, status
# check). Tracked apart from the decision so a retry can be told from a rerun.
DELIVERY_STATES: tuple[str, ...] = (
    "not_attempted",
    "pending",
    "in_flight",
    "delivered",
    "failed",
    "skipped",
)

RISK_BANDS: tuple[str, ...] = ("low", "medium", "high")


class ReasonCode:
    """Stable identifiers for why the gate landed where it did.

    Strings, not an enum, because they are persisted in decision rows and read
    back by older code after an upgrade. Adding a code is safe; renaming one
    rewrites history, so codes are append-only.
    """

    # Not applicable — the gate has no business deciding.
    GATE_OFF = "gate_off"
    KILL_SWITCH = "kill_switch"
    REPO_DISABLED = "repo_disabled"
    PR_DRAFT = "pr_draft"
    BASE_BRANCH_OUT_OF_SCOPE = "base_branch_out_of_scope"
    AUTHOR_NOT_IN_ALLOWLIST = "author_not_in_allowlist"
    MISSING_REQUIRED_LABEL = "missing_required_label"
    GENERATED_ONLY_DIFF = "generated_only_diff"
    SELF_AUTHORED = "self_authored"
    HUMAN_ALREADY_APPROVED = "human_already_approved"
    NO_REVIEW_RECORDED = "no_review_recorded"

    # Disqualifying — the gate looked and said no.
    BLOCKED_LABEL = "blocked_label"
    BLOCKED_BASE_BRANCH = "blocked_base_branch"
    AUTHOR_BLOCKED = "author_blocked"
    AUTHOR_ASSOCIATION_INSUFFICIENT = "author_association_insufficient"
    AUTHOR_ASSOCIATION_UNKNOWN = "author_association_unknown"
    PR_TOO_MANY_FILES = "pr_too_many_files"
    PR_TOO_MANY_LINES = "pr_too_many_lines"
    PROTECTED_PATH = "protected_path"
    CODEOWNERS_PATH = "codeowners_path"
    CODEOWNERS_UNREADABLE = "codeowners_unreadable"
    CI_PENDING = "ci_pending"
    CI_FAILING = "ci_failing"
    CI_UNKNOWN = "ci_unknown"
    REVIEW_INCOMPLETE = "review_incomplete"
    REVIEW_FAILED = "review_failed"
    INDEX_NOT_READY = "index_not_ready"
    OPEN_BLOCKER = "open_blocker"
    SEVERITY_ABOVE_CEILING = "severity_above_ceiling"
    HUMAN_CHANGES_REQUESTED = "human_changes_requested"
    RISK_ABOVE_THRESHOLD = "risk_above_threshold"
    LLM_FAILURE = "llm_failure"

    # Outcome and delivery.
    ELIGIBLE = "eligible"
    SHADOW_MODE = "shadow_mode"
    PROVIDER_CANNOT_APPROVE = "provider_cannot_approve"
    PROVIDER_CANNOT_REQUEST_CHANGES = "provider_cannot_request_changes"
    APPROVAL_DELIVERED = "approval_delivered"
    APPROVAL_REFUSED = "approval_refused"
    REQUEST_CHANGES_DELIVERED = "request_changes_delivered"
    EVALUATION_ERROR = "evaluation_error"
    EVALUATION_TIMEOUT = "evaluation_timeout"
    OVERRIDE_APPLIED = "override_applied"


# Reasons that may never be overridden into an approval. A human admin can
# always revoke an approval; forcing one past a protected path or an open
# blocker is the exact failure this phase exists to prevent.
HARD_VETO_CODES: frozenset[str] = frozenset(
    {
        ReasonCode.PROTECTED_PATH,
        ReasonCode.CODEOWNERS_PATH,
        ReasonCode.CODEOWNERS_UNREADABLE,
        ReasonCode.OPEN_BLOCKER,
        ReasonCode.HUMAN_CHANGES_REQUESTED,
        ReasonCode.REVIEW_FAILED,
        ReasonCode.LLM_FAILURE,
        ReasonCode.EVALUATION_ERROR,
        ReasonCode.EVALUATION_TIMEOUT,
    }
)


@dataclass(frozen=True)
class Reason:
    """One reason, in the gate's own words.

    `detail` is rendered to humans and may quote repository data (a path, a
    label, a branch). It is never interpreted: nothing downstream parses a
    reason back into a decision.
    """

    code: str
    message: str
    # "skip" — out of scope; "block" — disqualifying; "info" — context only.
    kind: str = "block"

    def as_dict(self) -> dict[str, str]:
        return {"code": self.code, "message": self.message, "kind": self.kind}


@dataclass(frozen=True)
class RiskFactor:
    """A single scored contribution to the risk total.

    Points are integers so the same inputs produce a byte-identical score on
    every machine — a float sum would let two replicas disagree in the last
    digit and turn an audit into an argument.
    """

    code: str
    label: str
    points: int
    detail: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "label": self.label,
            "points": self.points,
            "detail": self.detail,
        }


@dataclass
class CIState:
    """What the platform says about this head commit's checks."""

    # "success" | "failure" | "pending" | "none" | "unknown"
    state: str = "unknown"
    total: int = 0
    failing: list[str] = field(default_factory=list)
    pending: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "state": self.state,
            "total": self.total,
            "failing": sorted(self.failing)[:20],
            "pending": sorted(self.pending)[:20],
        }


@dataclass
class GateInputs:
    """Everything the decision was made from, captured before deciding.

    Persisted verbatim with the decision. An audit that cannot see the inputs
    can only confirm the arithmetic, not the judgement.
    """

    platform: str = "github"
    owner: str = ""
    repo: str = ""
    pr_number: int = 0
    pr_url: str = ""
    pr_author: str = ""
    base_branch: str = ""
    head_branch: str = ""
    head_sha: str = ""
    base_sha: str = ""
    draft: bool = False
    labels: list[str] = field(default_factory=list)
    author_association: str = "unknown"
    changed_paths: list[str] = field(default_factory=list)
    changed_files: int = 0
    added_lines: int = 0
    deleted_lines: int = 0
    generated_paths: list[str] = field(default_factory=list)
    protected_matches: list[str] = field(default_factory=list)
    codeowner_matches: list[str] = field(default_factory=list)
    # ok | unreadable | not_checked | absent
    codeowners_status: str = "not_checked"
    ci: CIState = field(default_factory=CIState)
    # Open findings, bucketed. Recorded on the inputs rather than passed to
    # `decide()`, so a gate woken by a finished CI run scores the same pull
    # request the same way a gate woken by the review does.
    open_blockers: int = 0
    open_warnings: int = 0
    open_security: int = 0
    open_findings: int = 0
    worst_severity: str = ""
    review_complete: bool = True
    review_skipped_paths: list[str] = field(default_factory=list)
    review_failed: str = ""
    index_ready: bool = True
    human_states: dict[str, str] = field(default_factory=dict)
    bot_login: str = ""
    review_id: int = 0

    def as_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["ci"] = self.ci.as_dict()
        # Path lists are unbounded in principle; a decision row is an audit
        # record, not a second copy of the diff.
        for key in ("changed_paths", "generated_paths", "protected_matches", "codeowner_matches"):
            data[key] = sorted(data[key])[:200]
        data["labels"] = sorted(data["labels"])[:100]
        data["review_skipped_paths"] = sorted(data["review_skipped_paths"])[:100]
        return data

    @property
    def open_suggestions(self) -> int:
        """Everything open that is neither a blocker nor a warning."""
        return max(0, self.open_findings - self.open_blockers - self.open_warnings)

    @property
    def digest(self) -> str:
        """Content hash of the facts a decision was made from.

        Part of the decision key, so re-evaluating a PR whose CI has since gone
        green records a *new* decision instead of silently reusing the one made
        while it was still pending — while a webhook redelivered with the same
        facts converges on the row that already exists.
        """
        payload = self.as_dict()
        # `review_id` links a decision to the review row that triggered it. A
        # retried review gets a new id for the same facts, and that must not
        # look like a different world to decide about.
        payload.pop("review_id", None)
        return hashlib.sha256(
            json.dumps(payload, sort_keys=True, default=str).encode("utf-8")
        ).hexdigest()[:16]


@dataclass
class GateDecision:
    """One evaluation of one PR at one head commit, under one policy."""

    decision_key: str = ""
    state: GateState = "skipped"
    mode: GateMode = "off"
    risk_score: int = 0
    risk_band: str = "low"
    policy_version: str = ""
    reasons: list[Reason] = field(default_factory=list)
    factors: list[RiskFactor] = field(default_factory=list)
    inputs: GateInputs = field(default_factory=GateInputs)
    capabilities: dict[str, Any] = field(default_factory=dict)
    # Set when the gate wants a REQUEST_CHANGES alongside the decision.
    request_changes: bool = False
    delivery_state: str = "not_attempted"
    delivery_ref: str = ""
    delivery_attempts: int = 0
    error: str = ""
    # Set when an admin moved this decision by hand. The full trail lives in
    # `gate_overrides`; this is here so a list view never shows a state without
    # showing that a person put it there.
    overridden_by: str = ""
    id: int = 0
    created_at: float = 0.0
    updated_at: float = 0.0

    @property
    def approved(self) -> bool:
        """True only for a delivered, real approval."""
        return self.state == "approved"

    @property
    def hard_vetoes(self) -> list[Reason]:
        return [r for r in self.reasons if r.code in HARD_VETO_CODES]

    def reason_codes(self) -> list[str]:
        return [r.code for r in self.reasons]

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "decision_key": self.decision_key,
            "state": self.state,
            "mode": self.mode,
            "risk_score": self.risk_score,
            "risk_band": self.risk_band,
            "policy_version": self.policy_version,
            "reasons": [r.as_dict() for r in self.reasons],
            "factors": [f.as_dict() for f in self.factors],
            "inputs": self.inputs.as_dict(),
            "capabilities": dict(self.capabilities),
            "request_changes": self.request_changes,
            "delivery_state": self.delivery_state,
            "delivery_ref": self.delivery_ref,
            "delivery_attempts": self.delivery_attempts,
            "error": self.error,
            "overridden_by": self.overridden_by,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "platform": self.inputs.platform,
            "owner": self.inputs.owner,
            "repo": self.inputs.repo,
            "pr_number": self.inputs.pr_number,
            "pr_url": self.inputs.pr_url,
            "pr_author": self.inputs.pr_author,
            "head_sha": self.inputs.head_sha,
        }


def _digest(parts: list[str]) -> str:
    return hashlib.sha256("\x1f".join(parts).encode("utf-8")).hexdigest()


def decision_key(
    *,
    platform: str,
    owner: str,
    repo: str,
    pr_number: int,
    head_sha: str,
    policy_version: str,
    mode: str,
    inputs_digest: str = "",
) -> str:
    """Identity of one gate evaluation, for idempotent persistence.

    Deliberately *excludes* the trigger. The same head commit evaluated from a
    review, a redelivered webhook and a CI-completion event, over the same
    facts and the same policy, is one decision — which is what makes a retry
    converge on the existing row instead of stacking duplicates.

    It *includes* the policy version and the inputs digest, because a decision
    reached under a different policy, or over a CI run that has since finished,
    is a genuinely different decision and must not inherit the earlier verdict.
    """
    return _digest(
        [
            platform,
            owner,
            repo,
            str(pr_number),
            head_sha,
            policy_version,
            mode,
            inputs_digest,
        ]
    )


def delivery_key(
    *, platform: str, owner: str, repo: str, pr_number: int, head_sha: str, kind: str
) -> str:
    """Identity of one *side effect* on the platform.

    Coarser than the decision key on purpose: it is scoped to the pull request
    and head commit, not to the evaluation. Two decisions over the same commit
    (CI went green between them) must still produce at most one approval, so
    the claim they compete for has to be the same claim.
    """
    return _digest([platform, owner, repo, str(pr_number), head_sha, kind])


# Side effects the gate can have on a platform. Only `approval` and
# `request_changes` are claimed: they are actions that must happen at most
# once. A status check and a PR comment are updates to a named artifact and are
# idempotent by construction, so re-sending one is harmless.
DELIVERY_KINDS: tuple[str, ...] = ("approval", "request_changes", "status", "comment")
CLAIMED_DELIVERY_KINDS: frozenset[str] = frozenset({"approval", "request_changes"})


def override_key(*, decision_key_value: str, actor: str, new_state: str, nonce: str = "") -> str:
    """Identity of one administrative override.

    A retried override request with the same actor, target and intent collapses
    onto one audit row instead of manufacturing a second one; `nonce` lets a
    caller record a genuinely repeated action (revoke, re-approve, revoke).
    """
    return _digest([decision_key_value, actor.lower(), new_state, nonce])


def risk_band(score: int, *, medium_at: int, high_at: int) -> str:
    """Bucket a score for display. Bands never decide anything on their own."""
    if score >= high_at:
        return "high"
    if score >= medium_at:
        return "medium"
    return "low"


def dumps(value: Any) -> str:
    """Stable JSON for persisted columns, so two runs produce equal bytes."""
    return json.dumps(value, sort_keys=True, default=str)


def loads(value: str, fallback: Any) -> Any:
    """Parse a persisted JSON column, tolerating rows written by older code."""
    if not value:
        return fallback
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return fallback
