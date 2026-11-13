"""Governance and versioning for candidates and active learned rules."""

from __future__ import annotations

from typing import Any

from mira.config import LearningConfig
from mira.feedback.deduplication import semantic_fingerprint
from mira.feedback.models import LearningCandidate

_SCOPES = {"symbol", "path", "language", "repo", "org"}


def evidence_required(scope_type: str, config: LearningConfig | None = None) -> int:
    config = config or LearningConfig()
    return {
        "symbol": config.min_evidence_path,
        "path": config.min_evidence_path,
        "language": config.min_evidence_language,
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
    *,
    min_evidence: int | None = None,
) -> Any:
    """Approve a candidate into an active rule.

    ``min_evidence`` overrides the per-scope minimum. Peer-recall learning
    uses it: its evidence unit is a distinct pull request where a peer bot's
    finding was fixed, which is stronger than one disagreeing reply, so it
    sets its own floor (``peer_recall.min_support``) instead of borrowing the
    five replies a repository-wide "do not report" rule needs.

    A candidate from a positive synthesiser becomes a positive rule
    (:data:`~mira.feedback.retrieval.POSITIVE_SIGNAL`) whoever approves it.
    """
    from mira.feedback.retrieval import POSITIVE_SIGNAL, is_positive_candidate

    candidate = store.get_learning_candidate(candidate_id)
    if candidate is None:
        raise LookupError("Learning candidate not found")
    validate_rule_text(candidate.rule_text)
    validate_rule_scope(candidate.scope_type, candidate.scope_value)
    required = (
        evidence_required(candidate.scope_type, config) if min_evidence is None else min_evidence
    )
    if candidate.evidence_count < required:
        raise ValueError(
            f"Scope '{candidate.scope_type}' requires at least {required} evidence events"
        )
    if is_positive_candidate(candidate):
        return store.approve_learning_candidate_atomic(
            candidate_id,
            actor=actor,
            min_evidence=required,
            source_signal=POSITIVE_SIGNAL,
        )
    return store.approve_learning_candidate_atomic(
        candidate_id,
        actor=actor,
        min_evidence=required,
    )


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
    from mira.feedback.retrieval import is_positive_rule

    validate_rule_text(rule_text)
    validate_rule_scope(scope_type, scope_value)
    # A positive rule was accepted on its own floor (distinct pull requests,
    # see ``approve_candidate``); rewording it in place must not then demand
    # the per-scope minimum. Widening its scope still does — and a scope is
    # its value as much as its type: `path a.py` to `path *.py` is a widening.
    kept_scope = (
        is_positive_rule(previous)
        and scope_type == previous.scope_type
        and scope_value.strip() == (previous.scope_value or "").strip()
    )
    if (
        previous.source_signal != "manual"
        and not kept_scope
        and previous.evidence_count < evidence_required(scope_type, config)
    ):
        raise ValueError(
            f"Scope '{scope_type}' requires at least "
            f"{evidence_required(scope_type, config)} evidence events"
        )
    return store.version_learned_rule_atomic(
        rule_id,
        rule_text=rule_text,
        category=category,
        scope_type=scope_type,
        scope_value=scope_value,
        rationale=rationale,
        actor=actor,
        semantic_fingerprint=semantic_fingerprint(rule_text, category),
    )
