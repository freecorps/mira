"""Domain models for provenance-complete review feedback."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class ReviewFinding:
    """A stable, persisted issue found during a review pass."""

    id: str
    fingerprint: str
    review_id: int
    platform: str
    owner: str
    repo: str
    pr_number: int
    pr_url: str
    base_sha: str
    head_sha: str
    path: str
    start_line: int
    end_line: int
    symbol: str
    category: str
    severity: str
    confidence: float
    title: str
    body: str
    suggestion: str
    detector: str
    prompt_model: str
    platform_comment_id: str = ""
    platform_thread_id: str = ""
    state: str = "open"
    created_at: float = 0.0
    updated_at: float = 0.0


@dataclass
class FeedbackEventV2:
    """An idempotent feedback signal tied to its originating finding."""

    id: int
    finding_id: str | None
    kind: str
    actor: str
    actor_role: str
    raw_text: str
    rationale: str
    platform: str
    source_event_id: str
    head_sha: str
    thread_state: str
    provenance_complete: bool
    audit_json: str = ""
    created_at: float = 0.0


@dataclass
class LearningCandidate:
    """An explainable rule proposal that is not active until governed."""

    id: int
    semantic_fingerprint: str
    rule_text: str
    rationale: str
    scope_type: str
    scope_value: str
    category: str
    language: str
    confidence: float
    status: str
    synthesizer_version: str
    evidence_ids_json: str = "[]"
    positive_examples_json: str = "[]"
    negative_examples_json: str = "[]"
    source_finding_id: str | None = None
    source_feedback_id: int | None = None
    superseded_by_id: int | None = None
    cost_tokens: int = 0
    created_at: float = 0.0
    updated_at: float = 0.0

    @property
    def evidence_count(self) -> int:
        """Return the number of distinct evidence IDs without exposing JSON details."""
        import json

        try:
            values = json.loads(self.evidence_ids_json or "[]")
        except (TypeError, json.JSONDecodeError):
            return 0
        return len(values) if isinstance(values, list) else 0
