"""Rule evaluation vocabulary, outcome classification and regression checks.

Phase 3 answers one question: *did a learned rule make reviews better?* To
answer it auditably every number the dashboard shows has to be reducible to the
individual feedback events that produced it. That only holds if the aggregate
query and the drill-down query classify outcomes with the *same* predicate, so
the classification lives here once, as data, and both SQL paths are generated
from it.

The other invariant is that silence is never a compliment. A merged PR whose
threads nobody touched produces ``unobserved`` events (see
``run_pr_merged_learning``); those keep an evaluation in the ``unobserved``
bucket, which is excluded from the acceptance numerator *and* denominator so it
can never lift a rule's score.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field

# The decision a rule produced for a review or a finding.
#   instruction - the rule text was injected into the review prompt.
#   suppress    - the rule caused a finding to be withheld.
#   boost       - the rule raised a finding's priority/confidence.
DECISIONS = ("instruction", "suppress", "boost")

# Where the rule came from. Kept distinct so a human-authored rule is never
# scored, downgraded or suggested for removal as if Mira had invented it.
ORIGINS = ("manual", "learned")

OUTCOMES = ("positive", "negative", "neutral", "unobserved")

# Feedback kinds, bucketed. Anything unknown falls through to `unobserved`,
# which is the only safe default: a signal we cannot interpret must not count
# as approval.
POSITIVE_KINDS = ("thumbs_up", "reply_agree", "fixed", "resolved")
NEGATIVE_KINDS = ("thumbs_down", "reply_disagree", "dismissed", "rejected")
NEUTRAL_KINDS = ("reply_question", "reply_other", "reopened")

# `addressed` needs concrete evidence that the finding was actually dealt with:
# an explicitly resolved thread (`fixed`/`resolved`), which Mira records only
# when the platform reports the thread resolved - never a bare merge.
#
# `outdated` is deliberately absent. It only means the diff moved past the
# commented line, which is exactly what a silent merge looks like; counting it
# would smuggle merge-as-acceptance back in through the diff.
ADDRESSED_KINDS = ("fixed", "resolved")
ADDRESSED_FINDING_STATES = ("fixed", "resolved")

# Explicitly recorded absence of a signal. Named so the dashboard can show
# "we looked and nobody said anything" apart from "we never looked".
UNOBSERVED_KINDS = ("unobserved",)


@dataclass
class RuleEvaluation:
    """One rule exposure, and the decision it produced, in one review."""

    id: int = 0
    evaluation_key: str = ""
    review_id: int = 0
    rule_id: int = 0
    rule_version: int = 1
    rule_origin: str = "learned"
    scope_type: str = "repo"
    scope_value: str = ""
    category: str = ""
    decision: str = "instruction"
    finding_id: str | None = None
    platform: str = "github"
    owner: str = ""
    repo: str = ""
    pr_number: int = 0
    pr_author: str = ""
    head_sha: str = ""
    detail_json: str = "{}"
    created_at: float = 0.0


@dataclass
class RuleOutcomeCounts:
    """Outcome breakdown for a set of evaluations, plus the derived rates."""

    # Every recorded exposure of the rule: one row per finding it was attached
    # to, plus one review-scoped row per review it was injected into. A rule
    # that correctly kept a review quiet still accrues exposures.
    exposures: int = 0
    # Exposures not tied to a finding. Outcome buckets below deliberately
    # ignore these - there is no finding to have an outcome about - so
    # positive+negative+neutral+unobserved sums to `findings`, not `exposures`.
    review_exposures: int = 0
    findings: int = 0
    positive: int = 0
    negative: int = 0
    neutral: int = 0
    unobserved: int = 0
    addressed: int = 0
    thumbs_up: int = 0
    thumbs_down: int = 0
    reply_agree: int = 0
    reply_disagree: int = 0
    repeated_false_positives: int = 0

    @property
    def observed(self) -> int:
        """Evaluations carrying a real signal. `unobserved` is not one."""
        return self.positive + self.negative + self.neutral

    @property
    def acceptance_rate(self) -> float | None:
        """positive / (positive + negative), or None when nobody has spoken.

        Neutral and unobserved are excluded from both sides, so an unanswered
        finding can neither raise nor lower the rate - it simply does not
        participate.
        """
        decisive = self.positive + self.negative
        return self.positive / decisive if decisive else None

    @property
    def addressed_rate(self) -> float | None:
        """Share of findings with concrete evidence they were resolved.

        The denominator is every finding the rule touched, so an unaddressed
        finding lowers the rate; it can never raise it.
        """
        return self.addressed / self.findings if self.findings else None

    @property
    def negative_rate(self) -> float | None:
        decisive = self.positive + self.negative
        return self.negative / decisive if decisive else None

    def as_dict(self) -> dict:
        return {
            "exposures": self.exposures,
            "review_exposures": self.review_exposures,
            "findings": self.findings,
            "observed": self.observed,
            "positive": self.positive,
            "negative": self.negative,
            "neutral": self.neutral,
            "unobserved": self.unobserved,
            "addressed": self.addressed,
            "thumbs_up": self.thumbs_up,
            "thumbs_down": self.thumbs_down,
            "reply_agree": self.reply_agree,
            "reply_disagree": self.reply_disagree,
            "repeated_false_positives": self.repeated_false_positives,
            "acceptance_rate": self.acceptance_rate,
            "addressed_rate": self.addressed_rate,
            "negative_rate": self.negative_rate,
        }


@dataclass
class RuleAnalyticsRow:
    """A rule plus its aggregated outcomes, as the dashboard lists it."""

    rule_id: int
    owner: str
    repo: str
    platform: str = "github"
    rule_text: str = ""
    category: str = ""
    scope_type: str = "repo"
    scope_value: str = ""
    origin: str = "learned"
    version: int = 1
    status: str = "approved"
    active: bool = True
    effective_from: float = 0.0
    disabled_at: float | None = None
    counts: RuleOutcomeCounts = field(default_factory=RuleOutcomeCounts)
    first_exposure_at: float = 0.0
    last_exposure_at: float = 0.0

    def as_dict(self) -> dict:
        return {
            "rule_id": self.rule_id,
            "owner": self.owner,
            "repo": self.repo,
            "platform": self.platform,
            "rule_text": self.rule_text,
            "category": self.category,
            "scope_type": self.scope_type,
            "scope_value": self.scope_value,
            "origin": self.origin,
            "version": self.version,
            "status": self.status,
            "active": self.active,
            "effective_from": self.effective_from,
            "disabled_at": self.disabled_at,
            "first_exposure_at": self.first_exposure_at,
            "last_exposure_at": self.last_exposure_at,
            **self.counts.as_dict(),
        }


@dataclass
class RegressionSuggestion:
    """A non-binding proposal to downgrade or disable a regressing rule."""

    rule_id: int
    owner: str
    repo: str
    action: str  # "downgrade" | "disable"
    reason: str
    exposures: int
    negative_rate: float
    addressed_rate: float | None
    min_exposures: int

    def as_dict(self) -> dict:
        return {
            "rule_id": self.rule_id,
            "owner": self.owner,
            "repo": self.repo,
            "action": self.action,
            "reason": self.reason,
            "exposures": self.exposures,
            "negative_rate": self.negative_rate,
            "addressed_rate": self.addressed_rate,
            "min_exposures": self.min_exposures,
        }


def classify_kind(kind: str) -> str:
    """Bucket a feedback kind. Unknown kinds are treated as no signal."""
    if kind in NEGATIVE_KINDS:
        return "negative"
    if kind in POSITIVE_KINDS:
        return "positive"
    if kind in NEUTRAL_KINDS:
        return "neutral"
    return "unobserved"


def outcome_for_kinds(kinds: list[str], finding_state: str = "") -> str:
    """Reduce every signal on one finding to a single outcome.

    Precedence is negative > positive > neutral > unobserved: one person
    pushing back outweighs a later thumbs-up from someone else, because a
    disputed rule is exactly what we most need to surface.
    """
    buckets = {classify_kind(kind) for kind in kinds}
    if "negative" in buckets:
        return "negative"
    if "positive" in buckets:
        return "positive"
    if finding_state in ADDRESSED_FINDING_STATES:
        return "positive"
    if "neutral" in buckets:
        return "neutral"
    return "unobserved"


def is_addressed(kinds: list[str], finding_state: str = "") -> bool:
    """True only with concrete resolution evidence - never from a merge."""
    if any(kind in ADDRESSED_KINDS for kind in kinds):
        return True
    return finding_state in ADDRESSED_FINDING_STATES


def evaluation_key(
    *,
    platform: str,
    owner: str,
    repo: str,
    pr_number: int,
    head_sha: str,
    rule_id: int,
    rule_version: int,
    decision: str,
    finding_id: str | None,
) -> str:
    """Deterministic identity for one evaluation.

    Two runs of the same review round over the same head SHA produce the same
    key, so a retried or re-delivered review cannot inflate a rule's exposure
    count. ``review_id`` is deliberately excluded: a retry allocates a fresh
    review row but is still the same exposure.
    """
    raw = "|".join(
        [
            platform,
            owner,
            repo,
            str(pr_number),
            head_sha,
            str(rule_id),
            str(rule_version),
            decision,
            finding_id or "",
        ]
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def origin_for_rule(rule: object) -> str:
    """Manual rules stay distinguishable from anything Mira synthesized."""
    signal = str(getattr(rule, "source_signal", "") or "")
    created_by = str(getattr(rule, "created_by", "") or "")
    if signal == "manual" or (created_by and created_by != "mira-auto-apply"):
        return "manual"
    return "learned"


def _sql_list(values: tuple[str, ...]) -> str:
    """Render a tuple as a SQL literal list of quoted strings.

    The values are module constants, never user input, so inlining them keeps
    the generated CASE expressions readable and parameter-free.
    """
    return ", ".join("'" + value.replace("'", "''") + "'" for value in values)


def signal_flags_sql(kind_column: str = "kind") -> str:
    """MAX(CASE ...) flags that reduce a finding's events to booleans."""
    return (
        f"MAX(CASE WHEN {kind_column} IN ({_sql_list(NEGATIVE_KINDS)}) THEN 1 ELSE 0 END) "
        "AS has_negative, "
        f"MAX(CASE WHEN {kind_column} IN ({_sql_list(POSITIVE_KINDS)}) THEN 1 ELSE 0 END) "
        "AS has_positive, "
        f"MAX(CASE WHEN {kind_column} IN ({_sql_list(NEUTRAL_KINDS)}) THEN 1 ELSE 0 END) "
        "AS has_neutral, "
        f"MAX(CASE WHEN {kind_column} IN ({_sql_list(ADDRESSED_KINDS)}) THEN 1 ELSE 0 END) "
        "AS has_addressed, "
        f"SUM(CASE WHEN {kind_column} = 'thumbs_up' THEN 1 ELSE 0 END) AS n_thumbs_up, "
        f"SUM(CASE WHEN {kind_column} = 'thumbs_down' THEN 1 ELSE 0 END) AS n_thumbs_down, "
        f"SUM(CASE WHEN {kind_column} = 'reply_agree' THEN 1 ELSE 0 END) AS n_reply_agree, "
        f"SUM(CASE WHEN {kind_column} = 'reply_disagree' THEN 1 ELSE 0 END) AS n_reply_disagree"
    )


def outcome_case_sql(
    *,
    negative: str = "s.has_negative",
    positive: str = "s.has_positive",
    neutral: str = "s.has_neutral",
    state: str = "f.state",
) -> str:
    """The single source of truth for outcome in SQL.

    Mirrors ``outcome_for_kinds`` exactly. Both the aggregate and the
    drill-down query use this, which is what makes "the number equals the
    events behind it" true by construction rather than by convention.
    """
    addressed_states = _sql_list(ADDRESSED_FINDING_STATES)
    return (
        f"CASE WHEN COALESCE({negative}, 0) = 1 THEN 'negative' "
        f"WHEN COALESCE({positive}, 0) = 1 THEN 'positive' "
        f"WHEN COALESCE({state}, '') IN ({addressed_states}) THEN 'positive' "
        f"WHEN COALESCE({neutral}, 0) = 1 THEN 'neutral' "
        "ELSE 'unobserved' END"
    )


def addressed_case_sql(addressed: str = "s.has_addressed", state: str = "f.state") -> str:
    """Mirrors ``is_addressed``: thread evidence or an explicit fixed state."""
    addressed_states = _sql_list(ADDRESSED_FINDING_STATES)
    return (
        f"CASE WHEN COALESCE({addressed}, 0) = 1 THEN 1 "
        f"WHEN COALESCE({state}, '') IN ({addressed_states}) THEN 1 ELSE 0 END"
    )


def detect_regression(
    row: RuleAnalyticsRow,
    *,
    min_exposures: int,
    negative_rate_threshold: float,
    disable_rate_threshold: float,
) -> RegressionSuggestion | None:
    """Suggest a downgrade or disable - never perform one.

    Phase 3 only advises. Acting on the advice stays an explicit admin
    decision, recorded in the audit log.
    """
    counts = row.counts
    if counts.exposures < min_exposures:
        return None
    negative_rate = counts.negative_rate
    # No decisive feedback at all means no evidence of regression. Silence is
    # not a complaint any more than it is a compliment.
    if negative_rate is None or negative_rate < negative_rate_threshold:
        return None
    action = "disable" if negative_rate >= disable_rate_threshold else "downgrade"
    reason = (
        f"{counts.negative} of {counts.positive + counts.negative} decisive signals were "
        f"negative across {counts.exposures} exposures"
    )
    return RegressionSuggestion(
        rule_id=row.rule_id,
        owner=row.owner,
        repo=row.repo,
        action=action,
        reason=reason,
        exposures=counts.exposures,
        negative_rate=negative_rate,
        addressed_rate=counts.addressed_rate,
        min_exposures=min_exposures,
    )
