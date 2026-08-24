"""Turn provenance-complete human feedback into governed rule candidates."""

from __future__ import annotations

import json
import math
from pathlib import PurePosixPath
from typing import Any

from mira.config import LearningConfig
from mira.feedback.deduplication import find_equivalent_candidate, semantic_fingerprint
from mira.feedback.models import FeedbackEventV2, LearningCandidate, ReviewFinding

SYNTHESIZER_VERSION = "phase2-v1"
NEGATIVE_KINDS = {"thumbs_down", "reply_disagree", "dismissed"}

_LANGUAGES = {
    ".c": "c",
    ".cc": "cpp",
    ".cpp": "cpp",
    ".cs": "csharp",
    ".go": "go",
    ".java": "java",
    ".js": "javascript",
    ".jsx": "javascript",
    ".kt": "kotlin",
    ".php": "php",
    ".py": "python",
    ".rb": "ruby",
    ".rs": "rust",
    ".swift": "swift",
    ".ts": "typescript",
    ".tsx": "typescript",
}


def language_for_path(path: str) -> str:
    return _LANGUAGES.get(PurePosixPath(path).suffix.lower(), "")


def _json_list(raw: str) -> list[Any]:
    try:
        value = json.loads(raw or "[]")
    except (TypeError, json.JSONDecodeError):
        return []
    return value if isinstance(value, list) else []


def _safe_scope(
    finding: ReviewFinding,
    requested_type: str,
    requested_value: str,
    evidence_count: int,
    config: LearningConfig,
) -> tuple[str, str]:
    """Clamp a model proposal to the narrowest scope justified by evidence."""
    language = language_for_path(finding.path)
    minimums = {
        "path": config.min_evidence_path,
        "symbol": config.min_evidence_path,
        "language": config.min_evidence_language,
        "repo": config.min_evidence_repo,
        "org": config.min_evidence_org,
    }
    requested_type = requested_type if requested_type in minimums else "path"
    if evidence_count < minimums[requested_type]:
        requested_type = "symbol" if finding.symbol else "path"
        requested_value = finding.symbol or finding.path
    elif requested_type == "symbol":
        # Never trust a model-proposed symbol name over finding provenance.
        requested_value = finding.symbol
        if not finding.symbol:
            requested_type, requested_value = "path", finding.path
    elif requested_type == "path":
        # A glob proposed from one example would silently broaden the rule.
        # Initial candidates always start at the exact originating path.
        requested_value = finding.path
    elif requested_type == "language":
        requested_value = requested_value or language
        if not requested_value:
            requested_type, requested_value = "path", finding.path
    elif requested_type == "repo":
        requested_value = requested_value or f"{finding.owner}/{finding.repo}"
    elif requested_type == "org":
        requested_value = requested_value or finding.owner
    return requested_type, requested_value


def _fallback_rule(finding: ReviewFinding, event: FeedbackEventV2) -> str:
    context = event.raw_text.strip() or event.rationale.strip()
    suffix = f" Human context: {context}" if context else ""
    return (
        f"Do not report '{finding.title}' as a {finding.category} issue in this scope "
        f"unless the code provides new, explicit evidence.{suffix}"
    )


def synthesize_candidate(
    store: Any,
    finding: ReviewFinding | None,
    event: FeedbackEventV2 | None,
    *,
    proposal: dict[str, Any] | None = None,
    config: LearningConfig | None = None,
) -> tuple[LearningCandidate | None, bool]:
    """Create or enrich a pending candidate from one negative feedback event."""
    config = config or LearningConfig()
    if (
        not config.feedback_v2
        or not config.learning_synthesis
        or finding is None
        or event is None
        or not event.provenance_complete
        or event.kind not in NEGATIVE_KINDS
    ):
        return None, False

    proposal = proposal or {}
    rule_text = str(proposal.get("rule") or "").strip() or _fallback_rule(finding, event)
    rationale = str(proposal.get("rationale") or event.rationale or "").strip()
    if not rationale:
        rationale = "The reviewer explicitly disagreed with the originating finding."
    category = finding.category or "other"

    candidates = store.list_learning_candidates(limit=2000)
    equivalent = find_equivalent_candidate(
        candidates,
        rule_text=rule_text,
        category=category,
    )
    prior_evidence = _json_list(equivalent.evidence_ids_json) if equivalent else []
    evidence_ids = [*prior_evidence, event.id]
    evidence_count = len({str(item) for item in evidence_ids})

    requested_type = str(proposal.get("scope_type") or "path").lower()
    requested_value = str(proposal.get("scope_value") or "")
    scope_type, scope_value = _safe_scope(
        finding,
        requested_type,
        requested_value,
        evidence_count,
        config,
    )
    fingerprint = (
        equivalent.semantic_fingerprint if equivalent else semantic_fingerprint(rule_text, category)
    )
    negative_example = {
        "feedback_id": event.id,
        "finding_id": finding.id,
        "path": finding.path,
        "line": finding.start_line,
        "head_sha": finding.head_sha,
        "finding": f"{finding.title}: {finding.body}",
        "human_feedback": event.raw_text,
    }
    raw_confidence = proposal.get("confidence", 0.75)
    try:
        confidence = float(0.75 if raw_confidence is None else raw_confidence)
    except (TypeError, ValueError):
        confidence = 0.5
    if not math.isfinite(confidence):
        confidence = 0.5
    confidence = max(0.0, min(confidence, 1.0))
    candidate = LearningCandidate(
        id=0,
        semantic_fingerprint=fingerprint,
        rule_text=rule_text,
        rationale=rationale,
        scope_type=scope_type,
        scope_value=scope_value,
        category=category,
        language=language_for_path(finding.path),
        confidence=confidence,
        status="pending",
        synthesizer_version=SYNTHESIZER_VERSION,
        evidence_ids_json=json.dumps([event.id]),
        positive_examples_json="[]",
        negative_examples_json=json.dumps([negative_example], sort_keys=True),
        source_finding_id=finding.id,
        source_feedback_id=event.id,
    )

    # Equivalent candidates retain their identity and governance state. Scope
    # widening is deliberately an explicit dashboard action in this release.
    if equivalent:
        candidate.scope_type = equivalent.scope_type
        candidate.scope_value = equivalent.scope_value
    return store.upsert_learning_candidate(candidate)
