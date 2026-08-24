"""Application service for resolving and recording feedback provenance."""

from __future__ import annotations

import json
import os
from typing import Any
from urllib.parse import urlencode

from mira.config import LearningConfig, load_config
from mira.feedback.models import FeedbackEventV2, ReviewFinding
from mira.feedback.provenance import (
    finding_fingerprint,
    legacy_finding_id,
    parse_finding_id,
)
from mira.index.store import IndexStore
from mira.providers.formatting import parse_bot_comment_metadata

DISAGREEMENT_ACK = "Entendi o feedback; registrei como falso positivo."


def resolve_finding(
    store: Any,
    pr_info: Any,
    *,
    original_body: str = "",
    platform_comment_id: str | int = "",
    platform_thread_id: str = "",
    path: str = "",
    line: int = 0,
    platform: str = "github",
) -> ReviewFinding | None:
    """Resolve a finding by marker, remote IDs, or legacy location metadata."""
    embedded_id = parse_finding_id(original_body)
    finding = store.get_review_finding(embedded_id) if embedded_id else None
    if finding is None and platform_comment_id:
        finding = store.find_review_finding(platform_comment_id=platform_comment_id)
    if finding is None and platform_thread_id:
        finding = store.find_review_finding(platform_thread_id=platform_thread_id)
    if finding is None and pr_info.number and path:
        finding = store.find_review_finding(
            pr_number=pr_info.number,
            path=path,
            line=line,
        )

    if finding is None:
        # Old Mira comments have no hidden marker. Reconstruct a deliberately
        # incomplete finding so feedback is retained but cannot feed rules.
        metadata = parse_bot_comment_metadata(original_body)
        if not metadata["category"]:
            return None
        finding_id = embedded_id or legacy_finding_id(
            platform,
            pr_info.owner,
            pr_info.repo,
            pr_info.number,
            platform_comment_id,
            path,
            line,
        )
        finding = ReviewFinding(
            id=finding_id,
            fingerprint=finding_fingerprint(
                owner=pr_info.owner,
                repo=pr_info.repo,
                pr_number=pr_info.number,
                base_sha=getattr(pr_info, "base_sha", ""),
                head_sha=getattr(pr_info, "head_sha", ""),
                path=path,
                symbol="",
                category=metadata["category"],
                detector="legacy-webhook",
                problem=original_body,
            ),
            review_id=0,
            platform=platform,
            owner=pr_info.owner,
            repo=pr_info.repo,
            pr_number=pr_info.number,
            pr_url=pr_info.url,
            base_sha=getattr(pr_info, "base_sha", ""),
            head_sha=getattr(pr_info, "head_sha", ""),
            path=path,
            start_line=line,
            end_line=line,
            symbol="",
            category=metadata["category"],
            severity=metadata["severity"],
            confidence=0.0,
            title=metadata["title"],
            body=original_body,
            suggestion="",
            detector="legacy-webhook",
            prompt_model="",
            platform_comment_id=str(platform_comment_id or ""),
            platform_thread_id=platform_thread_id,
        )
        store.save_review_finding(finding)

    store.update_review_finding_posted(
        finding.id,
        platform_comment_id=platform_comment_id,
        platform_thread_id=platform_thread_id,
    )
    return store.get_review_finding(finding.id) or finding


def record_finding_feedback(
    pr_info: Any,
    *,
    kind: str,
    source_event_id: str,
    actor: str,
    actor_role: str = "",
    raw_text: str = "",
    rationale: str = "",
    original_body: str = "",
    platform_comment_id: str | int = "",
    platform_thread_id: str = "",
    path: str = "",
    line: int = 0,
    thread_state: str = "",
    platform: str = "github",
    audit: dict[str, Any] | None = None,
) -> tuple[ReviewFinding | None, FeedbackEventV2 | None, bool]:
    """Resolve provenance and atomically deduplicate an incoming signal."""
    store = IndexStore.open(pr_info.owner, pr_info.repo, platform=platform)
    try:
        finding = resolve_finding(
            store,
            pr_info,
            original_body=original_body,
            platform_comment_id=platform_comment_id,
            platform_thread_id=platform_thread_id,
            path=path,
            line=line,
            platform=platform,
        )
        event, created = store.record_feedback_v2(
            FeedbackEventV2(
                id=0,
                finding_id=finding.id if finding else None,
                kind=kind,
                actor=actor,
                actor_role=actor_role,
                raw_text=raw_text,
                rationale=rationale,
                platform=platform,
                source_event_id=source_event_id,
                head_sha=finding.head_sha if finding else getattr(pr_info, "head_sha", ""),
                thread_state=thread_state,
                provenance_complete=False,
                audit_json=json.dumps(audit or {}, sort_keys=True),
            )
        )
        return finding, event, created
    finally:
        store.close()


def set_finding_state(pr_info: Any, finding_id: str, state: str, platform: str) -> None:
    store = IndexStore.open(pr_info.owner, pr_info.repo, platform=platform)
    try:
        store.update_review_finding_state(finding_id, state)
    finally:
        store.close()


def create_learning_candidate_for_feedback(
    pr_info: Any,
    finding: ReviewFinding | None,
    event: FeedbackEventV2 | None,
    *,
    proposal: dict[str, Any] | None = None,
    platform: str = "github",
    config: LearningConfig | None = None,
) -> tuple[Any | None, bool]:
    """Synthesize a governed candidate after the feedback event is durable."""
    from mira.feedback.lifecycle import approve_candidate, evidence_required
    from mira.feedback.synthesis import synthesize_candidate

    learning_config = config if isinstance(config, LearningConfig) else load_config().learning
    store = IndexStore.open(pr_info.owner, pr_info.repo, platform=platform)
    try:
        candidate, created = synthesize_candidate(
            store,
            finding,
            event,
            proposal=proposal,
            config=learning_config,
        )
        if (
            candidate is not None
            and learning_config.learning_auto_apply
            and candidate.confidence >= 0.95
            and candidate.evidence_count >= evidence_required(candidate.scope_type, learning_config)
        ):
            approve_candidate(
                store,
                candidate.id,
                actor="mira-auto-apply",
                config=learning_config,
            )
            candidate = store.get_learning_candidate(candidate.id)
        return candidate, created
    finally:
        store.close()


def feedback_ack(candidate: Any | None, owner: str, repo: str) -> str:
    """Keep the Phase 1 acknowledgement and add an auditable candidate link."""
    if candidate is None:
        return DISAGREEMENT_ACK
    base = os.environ.get("MIRA_DASHBOARD_URL", "").rstrip("/")
    status = getattr(candidate, "status", "pending")
    tab = status if status in {"approved", "rejected"} else "pending"
    label = f"candidato #{candidate.id}"
    if base:
        query = urlencode(
            {
                "tab": tab,
                "candidate": candidate.id,
                "owner": owner,
                "repo": repo,
            }
        )
        url = f"{base}/learnings?{query}"
        label = f"[{label}]({url})"
    if status == "approved":
        return f"{DISAGREEMENT_ACK}\n\nAssociei esta evidência ao {label}, que já foi aprovado."
    if status == "rejected":
        return (
            f"{DISAGREEMENT_ACK}\n\nAssociei esta evidência ao {label}; "
            "ele continua rejeitado e inativo."
        )
    return (
        f"{DISAGREEMENT_ACK}\n\nRegistrei o {label} para revisão; "
        "ele não afeta reviews até ser aprovado."
    )
