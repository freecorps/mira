"""Phase 2: explainable candidates, governance, scoped retrieval, and YAML."""

from __future__ import annotations

import time
from pathlib import Path
from unittest.mock import patch

import pytest

from mira.config import LearningConfig
from mira.feedback.deduplication import find_equivalent_candidate, rule_similarity
from mira.feedback.lifecycle import (
    approve_candidate,
    update_candidate,
    version_rule,
)
from mira.feedback.models import FeedbackEventV2, LearningCandidate, ReviewFinding
from mira.feedback.retrieval import retrieve_rules
from mira.feedback.serialization import export_rules_yaml, import_rules_yaml
from mira.feedback.service import create_learning_candidate_for_feedback
from mira.feedback.synthesis import synthesize_candidate
from mira.index.store import IndexStore
from mira.models import PRInfo


def _finding(*, head_sha: str = "head123", symbol: str = "") -> ReviewFinding:
    return ReviewFinding(
        id="00000000-0000-4000-8000-000000000042",
        fingerprint="fingerprint",
        review_id=1,
        platform="github",
        owner="acme",
        repo="app",
        pr_number=7,
        pr_url="https://github.com/acme/app/pull/7",
        base_sha="base123",
        head_sha=head_sha,
        path="src/auth/session.py",
        start_line=42,
        end_line=42,
        symbol=symbol,
        category="security",
        severity="warning",
        confidence=0.9,
        title="Session token is not rotated",
        body="Rotate the token after every request.",
        suggestion="",
        detector="main",
        prompt_model="test-model",
    )


def _feedback(store: IndexStore, finding: ReviewFinding, source: str) -> FeedbackEventV2:
    event, created = store.record_feedback_v2(
        FeedbackEventV2(
            id=0,
            finding_id=finding.id,
            kind="reply_disagree",
            actor="alice",
            actor_role="OWNER",
            raw_text="Tokens rotate only when a session is renewed.",
            rationale="The finding assumes per-request rotation, which is not our protocol.",
            platform="github",
            source_event_id=source,
            head_sha=finding.head_sha,
            thread_state="open",
            provenance_complete=False,
        )
    )
    assert created
    return event


@pytest.fixture
def learning_store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> IndexStore:
    monkeypatch.setenv("MIRA_INDEX_DIR", str(tmp_path))
    store = IndexStore.open("acme", "app")
    yield store
    store.close()


def test_disagreement_creates_pending_candidate_and_deduplicates(
    learning_store: IndexStore,
) -> None:
    finding = _finding()
    learning_store.save_review_finding(finding)
    proposal = {
        "rule": "Do not require session-token rotation on every request.",
        "rationale": "This repository rotates tokens only at session renewal.",
        "scope_type": "repo",
        "scope_value": "acme/app",
        "confidence": 0.91,
    }

    first, created = synthesize_candidate(
        learning_store,
        finding,
        _feedback(learning_store, finding, "comment:1"),
        proposal=proposal,
    )
    assert created and first is not None
    assert first.status == "pending"
    # One example cannot justify repository scope, so the model is clamped.
    assert (first.scope_type, first.scope_value) == ("path", finding.path)
    assert learning_store.list_active_learned_rules() == []

    second, created = synthesize_candidate(
        learning_store,
        finding,
        _feedback(learning_store, finding, "comment:2"),
        proposal=proposal,
    )
    assert not created and second is not None
    assert second.id == first.id
    assert second.evidence_count == 2


def test_incomplete_provenance_never_becomes_candidate(learning_store: IndexStore) -> None:
    finding = _finding(head_sha="")
    learning_store.save_review_finding(finding)
    event = _feedback(learning_store, finding, "comment:incomplete")
    assert not event.provenance_complete
    candidate, created = synthesize_candidate(learning_store, finding, event)
    assert candidate is None and not created


def test_model_cannot_broaden_or_invent_initial_path_and_symbol_scopes(
    learning_store: IndexStore,
) -> None:
    finding = _finding(symbol="refresh_session")
    learning_store.save_review_finding(finding)
    path_candidate, _ = synthesize_candidate(
        learning_store,
        finding,
        _feedback(learning_store, finding, "comment:broad-path"),
        proposal={
            "rule": "Do not require per-request token rotation.",
            "scope_type": "path",
            "scope_value": "src/**",
        },
    )
    symbol_candidate, _ = synthesize_candidate(
        learning_store,
        finding,
        _feedback(learning_store, finding, "comment:invented-symbol"),
        proposal={
            "rule": "Keep the session renewal behavior.",
            "scope_type": "symbol",
            "scope_value": "invented_symbol",
        },
    )
    assert path_candidate is not None
    assert (path_candidate.scope_type, path_candidate.scope_value) == (
        "path",
        finding.path,
    )
    assert symbol_candidate is not None
    assert (symbol_candidate.scope_type, symbol_candidate.scope_value) == (
        "symbol",
        finding.symbol,
    )


def test_scope_widening_requires_evidence(learning_store: IndexStore) -> None:
    finding = _finding()
    learning_store.save_review_finding(finding)
    candidate = None
    for index in range(3):
        candidate, _ = synthesize_candidate(
            learning_store,
            finding,
            _feedback(learning_store, finding, f"comment:{index}"),
            proposal={"rule": "Do not require token rotation on every request."},
        )
    assert candidate is not None and candidate.evidence_count == 3
    widened = update_candidate(
        learning_store,
        candidate.id,
        rule_text=candidate.rule_text,
        rationale=candidate.rationale,
        scope_type="language",
        scope_value="python",
        category=candidate.category,
        language="python",
    )
    assert widened.scope_type == "language"


def test_approval_and_retrieval_are_scoped_and_manual_first(
    learning_store: IndexStore,
) -> None:
    finding = _finding()
    learning_store.save_review_finding(finding)
    candidate, _ = synthesize_candidate(
        learning_store,
        finding,
        _feedback(learning_store, finding, "comment:approval"),
        proposal={"rule": "Do not require token rotation on every request."},
    )
    assert candidate is not None
    approved = approve_candidate(learning_store, candidate.id, actor="admin")
    assert approved.origin_candidate_id == candidate.id

    manual = learning_store.create_learned_rule(
        "Always verify authentication boundaries.",
        "security",
        source_signal="manual",
        scope_type="repo",
        scope_value="acme/app",
        created_by="admin",
    )
    relevant = retrieve_rules(
        learning_store,
        paths=[finding.path],
        languages=["python"],
    )
    assert [rule.id for rule in relevant[:2]] == [manual.id, approved.id]
    assert approved.id not in {
        rule.id
        for rule in retrieve_rules(
            learning_store,
            paths=["src/payments.py"],
            languages=["python"],
        )
    }

    symbol_rule = learning_store.create_learned_rule(
        "Keep compatibility behavior inside refresh_session.",
        "correctness",
        source_signal="manual",
        scope_type="symbol",
        scope_value="refresh_session",
    )
    wrong_repo = learning_store.create_learned_rule(
        "This belongs to another repository.",
        "other",
        source_signal="manual",
        scope_type="repo",
        scope_value="acme/other",
    )
    matched_ids = {
        rule.id
        for rule in retrieve_rules(
            learning_store,
            paths=[finding.path],
            languages=["python"],
            symbols=["refresh_session"],
        )
    }
    assert symbol_rule.id in matched_ids
    assert wrong_repo.id not in matched_ids
    assert symbol_rule.id not in {
        rule.id
        for rule in retrieve_rules(
            learning_store,
            paths=[finding.path],
            languages=["python"],
            symbols=["other_symbol"],
        )
    }


def test_org_rule_is_retrieved_from_sibling_sqlite_repository(
    learning_store: IndexStore,
) -> None:
    sibling = IndexStore.open("acme", "shared-policy")
    outsider = IndexStore.open("other-org", "shared-policy")
    try:
        org_rule = sibling.create_learned_rule(
            "Never log authentication credentials.",
            "security",
            source_signal="manual",
            scope_type="org",
            scope_value="acme",
        )
        outsider.create_learned_rule(
            "Use the other organization's convention.",
            "style",
            source_signal="manual",
            scope_type="org",
            scope_value="other-org",
        )
        now = time.time() + 1
        outsider._conn.executemany(
            "INSERT INTO learned_rules "
            "(rule_text, source_signal, category, active, status, scope_type, "
            "scope_value, created_at, updated_at) "
            "VALUES (?, 'manual', 'style', 1, 'approved', 'repo', ?, ?, ?)",
            [
                (f"Unrelated rule {index}", f"other-org/repo-{index}", now, now)
                for index in range(2100)
            ],
        )
        outsider._conn.commit()
    finally:
        sibling.close()
        outsider.close()

    rules = retrieve_rules(
        learning_store,
        paths=["src/auth/session.py"],
        languages=["python"],
    )
    assert org_rule.rule_text in {rule.rule_text for rule in rules}
    assert "Use the other organization's convention." not in {rule.rule_text for rule in rules}


def test_opposite_polarity_rules_are_never_deduplicated() -> None:
    affirmative = LearningCandidate(
        id=1,
        semantic_fingerprint="affirmative",
        rule_text="Report SQL injection issues.",
        rationale="Security findings should be reported.",
        scope_type="repo",
        scope_value="acme/app",
        category="security",
        language="",
        confidence=0.9,
        status="pending",
        synthesizer_version="test",
    )
    assert rule_similarity(affirmative.rule_text, "Do not report SQL injection issues.") == 0.0
    assert (
        find_equivalent_candidate(
            [affirmative],
            rule_text="Do not report SQL injection issues.",
            category="security",
        )
        is None
    )


def test_legacy_category_scope_fails_closed(learning_store: IndexStore) -> None:
    legacy = learning_store.create_learned_rule(
        "Do not report category-wide style findings.",
        "style",
        scope_type="category",
        scope_value="style",
    )

    rules = retrieve_rules(
        learning_store,
        paths=["src/app.py"],
        languages=["python"],
    )

    assert legacy.id not in {rule.id for rule in rules}


def test_scopes_and_pending_effective_time_survive_sqlite_reopen(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MIRA_INDEX_DIR", str(tmp_path))
    store = IndexStore.open("acme", "app")
    org_rule = store.create_learned_rule(
        "Never log credentials.",
        "security",
        scope_type="org",
        scope_value="acme",
    )
    pending = store.create_learned_rule(
        "Proposed repository convention.",
        "style",
        status="pending",
        scope_type="repo",
        scope_value="acme/app",
    )
    store.close()

    reopened = IndexStore.open("acme", "app")
    try:
        persisted = reopened.get_learned_rule(org_rule.id)
        persisted_pending = reopened.get_learned_rule(pending.id)
        assert persisted is not None and persisted.scope_type == "org"
        assert persisted.scope_value == "acme"
        assert persisted_pending is not None and persisted_pending.effective_from == 0
    finally:
        reopened.close()


def test_editing_approved_rule_creates_version_and_supersedes(
    learning_store: IndexStore,
) -> None:
    original = learning_store.create_learned_rule(
        "Avoid style nits in generated files.",
        "style",
        path_pattern="generated/**",
        scope_type="path",
        scope_value="generated/**",
    )
    replacement = version_rule(
        learning_store,
        original.id,
        rule_text="Never report style nits in generated files.",
        category="style",
        scope_type="path",
        scope_value="generated/**",
        actor="admin",
    )
    prior = learning_store.get_learned_rule(original.id)
    assert prior is not None
    assert prior.status == "superseded" and not prior.active
    assert replacement.version == 2
    assert replacement.supersedes_rule_id == original.id


def test_feedback_rule_cannot_be_versioned_to_broader_scope_without_evidence(
    learning_store: IndexStore,
) -> None:
    finding = _finding()
    learning_store.save_review_finding(finding)
    candidate, _ = synthesize_candidate(
        learning_store,
        finding,
        _feedback(learning_store, finding, "comment:version-scope"),
    )
    assert candidate is not None
    rule = approve_candidate(learning_store, candidate.id, actor="admin")

    with pytest.raises(ValueError, match="requires at least 5"):
        version_rule(
            learning_store,
            rule.id,
            rule_text=rule.rule_text,
            category=rule.category,
            scope_type="repo",
            scope_value="acme/app",
            actor="admin",
        )


def test_approval_recovers_after_candidate_status_update_is_interrupted(
    learning_store: IndexStore,
) -> None:
    finding = _finding()
    learning_store.save_review_finding(finding)
    candidate, _ = synthesize_candidate(
        learning_store,
        finding,
        _feedback(learning_store, finding, "comment:approval-recovery"),
    )
    assert candidate is not None
    first_rule = approve_candidate(learning_store, candidate.id, actor="admin")
    learning_store.set_learning_candidate_status(candidate.id, "pending")

    recovered_rule = approve_candidate(learning_store, candidate.id, actor="admin")

    assert recovered_rule.id == first_rule.id
    assert len(learning_store.list_active_learned_rules()) == 1
    assert learning_store.get_learning_candidate(candidate.id).status == "approved"


def test_yaml_round_trip_preserves_scope_and_keeps_rules_explicit(
    learning_store: IndexStore,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    learning_store.create_learned_rule(
        "Do not flag fixtures as production secrets.",
        "security",
        path_pattern="tests/fixtures/**",
        scope_type="path",
        scope_value="tests/fixtures/**",
        rationale="Fixtures contain deliberately fake values.",
    )
    raw = export_rules_yaml(learning_store, "acme", "app")
    learning_store.close()

    target = tmp_path / "imported"
    monkeypatch.setenv("MIRA_INDEX_DIR", str(target))
    imported_store = IndexStore.open("acme", "app")
    try:
        imported = import_rules_yaml(imported_store, raw, actor="yaml-admin")
        assert len(imported) == 1
        assert imported[0].scope_value == "tests/fixtures/**"
        assert imported[0].created_by == "yaml-admin"
        assert import_rules_yaml(imported_store, raw, actor="yaml-admin") == []
    finally:
        imported_store.close()


def test_yaml_import_validates_every_entry_before_writing(learning_store: IndexStore) -> None:
    raw = """
version: 1
rules:
  - rule: Keep API errors structured.
    category: correctness
    scope: {type: repo, value: acme/app}
  - rule: This entry has no safe scope.
    category: style
    scope: {type: path, value: ''}
"""
    with pytest.raises(ValueError, match="scope_value"):
        import_rules_yaml(learning_store, raw)
    assert learning_store.list_learned_rules() == []


def test_synthesis_feature_flag_can_disable_candidates(learning_store: IndexStore) -> None:
    finding = _finding()
    learning_store.save_review_finding(finding)
    candidate, created = synthesize_candidate(
        learning_store,
        finding,
        _feedback(learning_store, finding, "comment:disabled"),
        config=LearningConfig(learning_synthesis=False),
    )
    assert candidate is None and not created

    candidate, created = synthesize_candidate(
        learning_store,
        finding,
        _feedback(learning_store, finding, "comment:feedback-v2-disabled"),
        config=LearningConfig(feedback_v2=False),
    )
    assert candidate is None and not created


def test_candidate_synthesis_failure_is_best_effort(learning_store: IndexStore) -> None:
    finding = _finding()
    learning_store.save_review_finding(finding)
    event = _feedback(learning_store, finding, "comment:synthesis-failure")
    pr_info = PRInfo(
        title="PR",
        description="",
        base_branch="main",
        head_branch="feature",
        url=finding.pr_url,
        number=finding.pr_number,
        owner=finding.owner,
        repo=finding.repo,
        base_sha=finding.base_sha,
        head_sha=finding.head_sha,
    )

    with patch(
        "mira.feedback.synthesis.synthesize_candidate",
        side_effect=RuntimeError("synthesizer unavailable"),
    ):
        candidate, created = create_learning_candidate_for_feedback(pr_info, finding, event)

    assert candidate is None and not created


def test_auto_apply_requires_explicit_opt_in_and_high_confidence(
    learning_store: IndexStore,
) -> None:
    finding = _finding()
    learning_store.save_review_finding(finding)
    event = _feedback(learning_store, finding, "comment:auto-apply")
    pr_info = PRInfo(
        title="PR",
        description="",
        base_branch="main",
        head_branch="feature",
        url=finding.pr_url,
        number=finding.pr_number,
        owner=finding.owner,
        repo=finding.repo,
        base_sha=finding.base_sha,
        head_sha=finding.head_sha,
    )

    candidate, created = create_learning_candidate_for_feedback(
        pr_info,
        finding,
        event,
        proposal={
            "rule": "Do not require token rotation on every request.",
            "scope_type": "path",
            "scope_value": finding.path,
            "confidence": 0.99,
        },
        config=LearningConfig(learning_auto_apply=True),
    )

    assert created and candidate is not None and candidate.status == "approved"
    assert len(learning_store.list_active_learned_rules()) == 1
