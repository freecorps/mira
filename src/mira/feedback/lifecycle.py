"""Governance and versioning for candidates and active learned rules."""

from __future__ import annotations

from typing import Any

from mira.config import LearningConfig
from mira.feedback.deduplication import semantic_fingerprint
from mira.feedback.models import LearningCandidate

_SCOPES = {"symbol", "path", "language", "category", "repo", "org"}


def evidence_required(scope_type: str, config: LearningConfig | None = None) -> int:
    config = config or LearningConfig()
    return {
        "symbol": config.min_evidence_path,
        "path": config.min_evidence_path,
        "language": config.min_evidence_language,
        "category": config.min_evidence_language,
        "repo": config.min_evidence_repo,
        "org": config.min_evidence_org,
    }.get(scope_type, config.min_evidence_repo)


def validate_rule_scope(scope_type: str, scope_value: str) -> None:
    if scope_type not in _SCOPES:
        raise ValueError(f"Unsupported scope_type: {scope_type}")
    if not scope_value.strip():
        raise ValueError("scope_value is required")


def validate_rule_text(rule_text: str) -> None:
    if not rule_text.strip():
        raise ValueError("rule_text is required")


def update_candidate(
    store: Any,
    candidate_id: int,
    *,
    rule_text: str,
    rationale: str,
    scope_type: str,
    scope_value: str,
    category: str,
    language: str = "",
    config: LearningConfig | None = None,
) -> LearningCandidate:
    candidate = store.get_learning_candidate(candidate_id)
    if candidate is None:
        raise LookupError("Learning candidate not found")
    if candidate.status not in {"collecting", "pending"}:
        raise ValueError("Only collecting or pending candidates can be edited")
    validate_rule_text(rule_text)
    validate_rule_scope(scope_type, scope_value)
    if candidate.evidence_count < evidence_required(scope_type, config):
        raise ValueError(
            f"Scope '{scope_type}' requires at least "
            f"{evidence_required(scope_type, config)} evidence events"
        )
    fingerprint = semantic_fingerprint(rule_text, category)
    duplicate = next(
        (
            current
            for current in store.list_learning_candidates(limit=2000)
            if current.id != candidate_id
            and current.semantic_fingerprint == fingerprint
            and current.scope_type == scope_type
            and current.scope_value == scope_value.strip()
            and current.status not in {"rejected", "superseded"}
        ),
        None,
    )
    if duplicate is not None:
        raise ValueError(f"Equivalent candidate already exists as #{duplicate.id}")
    store.update_learning_candidate(
        candidate_id,
        rule_text=rule_text.strip(),
        rationale=rationale.strip(),
        scope_type=scope_type,
        scope_value=scope_value.strip(),
        category=category.strip() or "other",
        language=language.strip(),
        semantic_fingerprint=fingerprint,
    )
    updated = store.get_learning_candidate(candidate_id)
    if updated is None:  # Defensive: a store must not lose a row during update.
        raise LookupError("Learning candidate not found after update")
    return updated


def approve_candidate(
    store: Any,
    candidate_id: int,
    actor: str = "",
    config: LearningConfig | None = None,
) -> Any:
    candidate = store.get_learning_candidate(candidate_id)
    if candidate is None:
        raise LookupError("Learning candidate not found")
    rules = [
        rule
        for rule in store.list_learned_rules()
        if rule.origin_candidate_id == candidate.id and rule.status == "approved"
    ]
    if rules:
        # This also repairs an interrupted approval where rule creation
        # committed but the candidate status update did not.
        if candidate.status != "approved":
            store.set_learning_candidate_status(candidate.id, "approved")
        return rules[0]
    if candidate.status == "approved":
        raise ValueError("Approved candidate has no active rule")
    if candidate.status not in {"collecting", "pending"}:
        raise ValueError(f"Cannot approve candidate in state '{candidate.status}'")
    validate_rule_text(candidate.rule_text)
    validate_rule_scope(candidate.scope_type, candidate.scope_value)
    required = evidence_required(candidate.scope_type, config)
    if candidate.evidence_count < required:
        raise ValueError(
            f"Scope '{candidate.scope_type}' requires at least {required} evidence events"
        )
    rule = store.create_learned_rule(
        rule_text=candidate.rule_text,
        category=candidate.category,
        path_pattern=candidate.scope_value if candidate.scope_type == "path" else "",
        source_signal="feedback_v2",
        status="approved",
        active=True,
        created_by=actor,
        version=1,
        scope_type=candidate.scope_type,
        scope_value=candidate.scope_value,
        origin_candidate_id=candidate.id,
        rationale=candidate.rationale,
        evidence_count=candidate.evidence_count,
        semantic_fingerprint=candidate.semantic_fingerprint,
    )
    store.set_learning_candidate_status(candidate.id, "approved")
    return rule


def reject_candidate(store: Any, candidate_id: int) -> None:
    candidate = store.get_learning_candidate(candidate_id)
    if candidate is None:
        raise LookupError("Learning candidate not found")
    if candidate.status == "approved":
        raise ValueError("Approved candidates cannot be rejected; disable their rule instead")
    store.set_learning_candidate_status(candidate_id, "rejected")


def version_rule(
    store: Any,
    rule_id: int,
    *,
    rule_text: str,
    category: str,
    scope_type: str,
    scope_value: str,
    rationale: str = "",
    actor: str = "",
    config: LearningConfig | None = None,
) -> Any:
    previous = store.get_learned_rule(rule_id)
    if previous is None:
        raise LookupError("Learning not found")
    if previous.status != "approved":
        raise ValueError("Only approved rules can be versioned")
    validate_rule_text(rule_text)
    validate_rule_scope(scope_type, scope_value)
    if previous.source_signal != "manual" and previous.evidence_count < evidence_required(
        scope_type, config
    ):
        raise ValueError(
            f"Scope '{scope_type}' requires at least "
            f"{evidence_required(scope_type, config)} evidence events"
        )
    interrupted = next(
        (
            rule
            for rule in store.list_learned_rules()
            if rule.supersedes_rule_id == previous.id and rule.status == "approved"
        ),
        None,
    )
    if interrupted is not None:
        store.supersede_learned_rule(previous.id, interrupted.id)
        return interrupted
    replacement = store.create_learned_rule(
        rule_text=rule_text,
        category=category,
        path_pattern=scope_value if scope_type == "path" else "",
        source_signal=previous.source_signal,
        status="approved",
        active=True,
        created_by=actor or previous.created_by,
        version=previous.version + 1,
        scope_type=scope_type,
        scope_value=scope_value,
        origin_candidate_id=previous.origin_candidate_id,
        rationale=rationale or previous.rationale,
        evidence_count=previous.evidence_count,
        supersedes_rule_id=previous.id,
        semantic_fingerprint=semantic_fingerprint(rule_text, category),
    )
    store.supersede_learned_rule(previous.id, replacement.id)
    return replacement
