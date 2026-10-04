"""Records for backtests and escaped bugs.

Plain dataclasses with ``to_dict`` so the same object feeds the JSON report,
the Markdown report, the database row and the dashboard API.
"""

from __future__ import annotations

import hashlib
from dataclasses import asdict, dataclass, field
from typing import Any

# Ground-truth signal kinds. Positive ones are evidence that a real problem sat
# on those lines; negative ones are evidence that a finding there was wrong.
SIGNAL_HUMAN_COMMENT = "human_comment"
SIGNAL_FIX_COMMIT = "fix_commit"
SIGNAL_REVERT = "revert"
SIGNAL_FEEDBACK_POSITIVE = "feedback_positive"
SIGNAL_FEEDBACK_NEGATIVE = "feedback_negative"

LABEL_TP = "tp"
LABEL_FP = "fp"
LABEL_UNLABELLED = "unlabelled"


@dataclass
class Signal:
    """One piece of after-the-fact evidence about lines of a pull request."""

    kind: str
    path: str
    start: int
    end: int
    positive: bool = True
    ref: str = ""
    detail: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ScoredFinding:
    """A finding the replayed review produced, and what the evidence says about it."""

    path: str
    line: int
    end_line: int
    severity: str
    category: str
    title: str
    confidence: float = 0.0
    label: str = LABEL_UNLABELLED
    matched: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Score:
    """Precision/recall for one pull request, or summed over many.

    ``precision`` is strict: true positives over *every* finding, so a finding
    nothing can vouch for counts against it. ``labelled_precision`` only
    counts findings the evidence spoke about either way. The truth is between
    them, and the report shows both rather than pick one.
    """

    findings: int = 0
    tp: int = 0
    fp: int = 0
    unlabelled: int = 0
    positives: int = 0
    positives_caught: int = 0

    @property
    def has_ground_truth(self) -> bool:
        return bool(self.positives or self.tp or self.fp)

    @property
    def precision(self) -> float | None:
        if not self.has_ground_truth or not self.findings:
            return None
        return self.tp / self.findings

    @property
    def labelled_precision(self) -> float | None:
        decided = self.tp + self.fp
        return self.tp / decided if decided else None

    @property
    def recall(self) -> float | None:
        return self.positives_caught / self.positives if self.positives else None

    def add(self, other: Score) -> None:
        self.findings += other.findings
        self.tp += other.tp
        self.fp += other.fp
        self.unlabelled += other.unlabelled
        self.positives += other.positives
        self.positives_caught += other.positives_caught

    def to_dict(self) -> dict[str, Any]:
        return {
            "findings": self.findings,
            "tp": self.tp,
            "fp": self.fp,
            "unlabelled": self.unlabelled,
            "positives": self.positives,
            "positives_caught": self.positives_caught,
            "has_ground_truth": self.has_ground_truth,
            "precision": self.precision,
            "labelled_precision": self.labelled_precision,
            "recall": self.recall,
        }


@dataclass
class PRBacktest:
    """One configuration's replay of one pull request."""

    variant: str
    pr_number: int
    pr_title: str = ""
    pr_url: str = ""
    merged_at: float = 0.0
    findings: list[ScoredFinding] = field(default_factory=list)
    signals: list[Signal] = field(default_factory=list)
    score: Score = field(default_factory=Score)
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cost_usd: float = 0.0
    latency_ms: int = 0
    error: str = ""
    # Writes the read-only provider swallowed. Always 0 in a healthy run; a
    # non-zero value is a bug worth reporting, not a thing that happened.
    blocked_writes: int = 0
    skipped_reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "variant": self.variant,
            "pr_number": self.pr_number,
            "pr_title": self.pr_title,
            "pr_url": self.pr_url,
            "merged_at": self.merged_at,
            "findings": [f.to_dict() for f in self.findings],
            "signals": [s.to_dict() for s in self.signals],
            "score": self.score.to_dict(),
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "cost_usd": round(self.cost_usd, 6),
            "latency_ms": self.latency_ms,
            "error": self.error,
            "blocked_writes": self.blocked_writes,
            "skipped_reason": self.skipped_reason,
        }


@dataclass
class VariantSummary:
    """Totals for one configuration across a run."""

    variant: str
    config_label: str = ""
    prs: int = 0
    prs_with_ground_truth: int = 0
    errors: int = 0
    score: Score = field(default_factory=Score)
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cost_usd: float = 0.0
    latency_ms_total: int = 0

    @property
    def mean_latency_ms(self) -> int:
        ran = self.prs - self.errors
        return int(self.latency_ms_total / ran) if ran > 0 else 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "variant": self.variant,
            "config_label": self.config_label,
            "prs": self.prs,
            "prs_with_ground_truth": self.prs_with_ground_truth,
            "errors": self.errors,
            "score": self.score.to_dict(),
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "cost_usd": round(self.cost_usd, 6),
            "mean_latency_ms": self.mean_latency_ms,
        }


@dataclass
class BacktestRun:
    """A whole ``mira backtest`` invocation."""

    id: str
    platform: str
    owner: str
    repo: str
    status: str = "running"  # running | completed | stopped | failed
    created_at: float = 0.0
    finished_at: float = 0.0
    # variant name ("A", "B") → a label for its configuration (usually a path).
    variants: dict[str, str] = field(default_factory=dict)
    params: dict[str, Any] = field(default_factory=dict)
    estimated_cost_usd: float = 0.0
    results: list[PRBacktest] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def summaries(self) -> list[VariantSummary]:
        out: dict[str, VariantSummary] = {
            name: VariantSummary(variant=name, config_label=label)
            for name, label in self.variants.items()
        }
        for result in self.results:
            summary = out.setdefault(result.variant, VariantSummary(variant=result.variant))
            summary.prs += 1
            if result.error:
                summary.errors += 1
                continue
            if result.score.has_ground_truth:
                summary.prs_with_ground_truth += 1
            summary.score.add(result.score)
            summary.prompt_tokens += result.prompt_tokens
            summary.completion_tokens += result.completion_tokens
            summary.cost_usd += result.cost_usd
            summary.latency_ms_total += result.latency_ms
        return list(out.values())

    @property
    def total_cost_usd(self) -> float:
        return sum(r.cost_usd for r in self.results)

    def to_dict(self, *, include_results: bool = True) -> dict[str, Any]:
        data: dict[str, Any] = {
            "id": self.id,
            "platform": self.platform,
            "owner": self.owner,
            "repo": self.repo,
            "status": self.status,
            "created_at": self.created_at,
            "finished_at": self.finished_at,
            "variants": dict(self.variants),
            "params": dict(self.params),
            "estimated_cost_usd": round(self.estimated_cost_usd, 6),
            "total_cost_usd": round(self.total_cost_usd, 6),
            "summaries": [s.to_dict() for s in self.summaries()],
            "notes": list(self.notes),
        }
        if include_results:
            data["results"] = [r.to_dict() for r in self.results]
        return data


# Escaped-bug kinds and how each was linked to its original pull request.
KIND_REVERT = "revert"
KIND_HOTFIX = "hotfix"

LINK_REVERT_SHA = "revert_sha"
LINK_REVERT_BRANCH = "revert_branch"
LINK_BLAME = "blame"
LINK_DIFF_OVERLAP = "diff_overlap"


def escaped_bug_id(platform: str, owner: str, repo: str, fix_ref: str, original_pr: int, path: str):
    """Deterministic identity, so a redelivered webhook or a re-scan is a no-op."""
    raw = "\x1f".join([platform, owner, repo, fix_ref, str(original_pr), path])
    return hashlib.sha256(raw.encode()).hexdigest()[:32]


@dataclass
class EscapedBug:
    """A defect that shipped through a pull request Mira reviewed.

    ``flagged`` answers the question the whole feature exists for: did Mira
    say anything on these lines when it reviewed the original pull request?
    ``flagged_file`` is the weaker "anything in the same file", kept so a
    reader can see near misses without them inflating recall.
    """

    id: str
    platform: str
    owner: str
    repo: str
    kind: str
    fix_ref: str
    original_pr_number: int
    path: str
    line_start: int = 0
    line_end: int = 0
    fix_pr_number: int = 0
    fix_sha: str = ""
    fix_title: str = ""
    fix_url: str = ""
    original_pr_url: str = ""
    original_merged_at: float = 0.0
    flagged: bool = False
    flagged_file: bool = False
    finding_ids: list[str] = field(default_factory=list)
    link_method: str = ""
    detected_at: float = 0.0
    learning_candidate_id: int = 0
    detail: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
