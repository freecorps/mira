"""Risk-oriented merge gate — Phase 4.

The review verdict answers "is this code good?". The gate answers a different,
narrower question: *may Mira put its name on merging this?* The two are kept
apart on purpose. A quality score is an opinion that can be wrong and cost
nothing; an approval is an action that can satisfy a branch-protection rule and
let code land unread.

Every path through this package resolves doubt the same way: no approval. A
missing input, a timed-out provider call, an unparseable CODEOWNERS file, a
pending CI run, an open blocker, a protected path — all of them decide against
approving, and all of them say which one it was.
"""

from mira.gate.models import (
    DELIVERY_STATES,
    GATE_MODES,
    GATE_STATES,
    GateDecision,
    GateInputs,
    GateMode,
    GateState,
    Reason,
    ReasonCode,
    RiskFactor,
    decision_key,
    override_key,
)

__all__ = [
    "DELIVERY_STATES",
    "GATE_MODES",
    "GATE_STATES",
    "GateDecision",
    "GateInputs",
    "GateMode",
    "GateState",
    "Reason",
    "ReasonCode",
    "RiskFactor",
    "decision_key",
    "override_key",
]
