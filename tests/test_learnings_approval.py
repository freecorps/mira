"""Synthesized learnings land in a pending queue and only feed reviews once an
admin approves them. Admins can also CRUD rules directly."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import HTTPException

from mira.dashboard import api
from mira.dashboard.db import AppDatabase, User
from mira.dashboard.routers import rules
from mira.feedback.models import LearningCandidate
from mira.index.store import IndexStore


@pytest.fixture
def patched_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AppDatabase:
    monkeypatch.setenv("MIRA_INDEX_DIR", str(tmp_path))
    db = AppDatabase(url="", admin_password="admin")
    monkeypatch.setattr(api, "_app_db", db)
    return db


class _Req:
    """Minimal stand-in for a Starlette Request carrying request.state.user."""

    def __init__(self, is_admin: bool, username: str = "u"):
        self.state = type("S", (), {"user": User(id=1, username=username, is_admin=is_admin)})()


def test_synthesized_rules_are_pending(patched_db: AppDatabase):
    patched_db.register_repo("acme", "web")
    store = IndexStore.open("acme", "web")
    # upsert is the synthesis path — should default to pending.
    rule = store.upsert_learned_rule(
        rule_text="Don't flag missing docstrings on helpers",
        source_signal="reject_pattern",
        category="style",
        path_pattern="",
        sample_count=3,
    )
    assert rule.status == "pending"
    # Pending rules must NOT feed reviews.
    assert store.list_active_learned_rules() == []
    store.close()


def test_approve_makes_rule_active(patched_db: AppDatabase):
    patched_db.register_repo("acme", "web")
    store = IndexStore.open("acme", "web")
    rule = store.upsert_learned_rule("r", "reject_pattern", "style", "", 3)
    store.close()

    rules.approve_learned_rule("acme", "web", rule.id, _Req(is_admin=True))

    store = IndexStore.open("acme", "web")
    active = store.list_active_learned_rules()
    assert [r.id for r in active] == [rule.id]
    store.close()


def test_reject_keeps_rule_out(patched_db: AppDatabase):
    patched_db.register_repo("acme", "web")
    store = IndexStore.open("acme", "web")
    rule = store.upsert_learned_rule("r", "reject_pattern", "style", "", 3)
    store.close()

    rules.reject_learned_rule("acme", "web", rule.id, _Req(is_admin=True))

    store = IndexStore.open("acme", "web")
    assert store.list_active_learned_rules() == []
    assert store.get_learned_rule(rule.id).status == "rejected"
    store.close()


def test_non_admin_cannot_approve(patched_db: AppDatabase):
    patched_db.register_repo("acme", "web")
    store = IndexStore.open("acme", "web")
    rule = store.upsert_learned_rule("r", "reject_pattern", "style", "", 3)
    store.close()
    with pytest.raises(HTTPException) as exc:
        rules.approve_learned_rule("acme", "web", rule.id, _Req(is_admin=False))
    assert exc.value.status_code == 403


def test_admin_crud(patched_db: AppDatabase):
    patched_db.register_repo("acme", "web")
    # Create → approved + active immediately.
    created = rules.create_learned_rule(
        "acme",
        "web",
        api.LearnedRuleInput(rule_text="No nits in tests", category="style", path_pattern="tests/"),
        _Req(is_admin=True),
    )
    assert created.status == "approved" and created.active

    store = IndexStore.open("acme", "web")
    assert any(r.rule_text == "No nits in tests" for r in store.list_active_learned_rules())
    store.close()

    # Update.
    updated = rules.update_learned_rule(
        "acme",
        "web",
        created.id,
        api.LearnedRuleInput(rule_text="Updated", category="style", path_pattern="tests/"),
        _Req(is_admin=True),
    )
    # Editing an approved rule creates a new version and supersedes the old.
    replacement_id = updated["rule_id"]
    assert replacement_id != created.id
    store = IndexStore.open("acme", "web")
    prior = store.get_learned_rule(created.id)
    replacement = store.get_learned_rule(replacement_id)
    assert prior.status == "superseded" and not prior.active
    assert replacement.rule_text == "Updated" and replacement.version == 2
    store.close()

    # Disable → drops the replacement out of the active set.
    rules.set_learned_rule_active(
        "acme",
        "web",
        replacement_id,
        api.LearnedRuleActiveInput(active=False),
        _Req(is_admin=True),
    )
    store = IndexStore.open("acme", "web")
    assert all(r.id != replacement_id for r in store.list_active_learned_rules())
    store.close()

    # Delete.
    rules.delete_learned_rule("acme", "web", replacement_id, _Req(is_admin=True))
    store = IndexStore.open("acme", "web")
    assert store.get_learned_rule(replacement_id) is None
    store.close()


def test_non_admin_create_is_pending(patched_db: AppDatabase):
    patched_db.register_repo("acme", "web")
    created = rules.create_learned_rule(
        "acme",
        "web",
        api.LearnedRuleInput(rule_text="Be nice", category="style"),
        _Req(is_admin=False, username="junior"),
    )
    assert created.status == "pending"
    assert created.created_by == "junior"


def test_admin_create_is_approved(patched_db: AppDatabase):
    patched_db.register_repo("acme", "web")
    created = rules.create_learned_rule(
        "acme",
        "web",
        api.LearnedRuleInput(rule_text="Be safe", category="security"),
        _Req(is_admin=True, username="boss"),
    )
    assert created.status == "approved"


def test_creator_can_edit_own_pending(patched_db: AppDatabase):
    patched_db.register_repo("acme", "web")
    created = rules.create_learned_rule(
        "acme",
        "web",
        api.LearnedRuleInput(rule_text="original", category="style"),
        _Req(is_admin=False, username="junior"),
    )
    rules.update_learned_rule(
        "acme",
        "web",
        created.id,
        api.LearnedRuleInput(rule_text="edited", category="style"),
        _Req(is_admin=False, username="junior"),
    )
    store = IndexStore.open("acme", "web")
    assert store.get_learned_rule(created.id).rule_text == "edited"
    store.close()


def test_other_non_admin_cannot_edit(patched_db: AppDatabase):
    patched_db.register_repo("acme", "web")
    created = rules.create_learned_rule(
        "acme",
        "web",
        api.LearnedRuleInput(rule_text="original", category="style"),
        _Req(is_admin=False, username="junior"),
    )
    with pytest.raises(HTTPException) as exc:
        rules.update_learned_rule(
            "acme",
            "web",
            created.id,
            api.LearnedRuleInput(rule_text="hijacked", category="style"),
            _Req(is_admin=False, username="someone-else"),
        )
    assert exc.value.status_code == 403


def test_creator_cannot_edit_once_approved(patched_db: AppDatabase):
    patched_db.register_repo("acme", "web")
    created = rules.create_learned_rule(
        "acme",
        "web",
        api.LearnedRuleInput(rule_text="original", category="style"),
        _Req(is_admin=False, username="junior"),
    )
    rules.approve_learned_rule("acme", "web", created.id, _Req(is_admin=True))
    with pytest.raises(HTTPException) as exc:
        rules.update_learned_rule(
            "acme",
            "web",
            created.id,
            api.LearnedRuleInput(rule_text="edited", category="style"),
            _Req(is_admin=False, username="junior"),
        )
    assert exc.value.status_code == 403


def test_other_non_admin_cannot_read_pending(patched_db: AppDatabase):
    patched_db.register_repo("acme", "web")
    created = rules.create_learned_rule(
        "acme",
        "web",
        api.LearnedRuleInput(rule_text="original", category="style"),
        _Req(is_admin=False, username="junior"),
    )
    with pytest.raises(HTTPException) as exc:
        rules.get_learned_rule_detail(
            "acme", "web", created.id, _Req(is_admin=False, username="someone-else")
        )
    assert exc.value.status_code == 403
    # Creator and admin can still read it.
    assert (
        rules.get_learned_rule_detail(
            "acme", "web", created.id, _Req(is_admin=False, username="junior")
        ).id
        == created.id
    )
    assert (
        rules.get_learned_rule_detail(
            "acme", "web", created.id, _Req(is_admin=True, username="boss")
        ).id
        == created.id
    )
    # Once approved, anyone authenticated can read it.
    rules.approve_learned_rule("acme", "web", created.id, _Req(is_admin=True))
    assert (
        rules.get_learned_rule_detail(
            "acme", "web", created.id, _Req(is_admin=False, username="someone-else")
        ).status
        == "approved"
    )


def test_admin_can_edit_anyones_rule(patched_db: AppDatabase):
    patched_db.register_repo("acme", "web")
    created = rules.create_learned_rule(
        "acme",
        "web",
        api.LearnedRuleInput(rule_text="original", category="style"),
        _Req(is_admin=False, username="junior"),
    )
    rules.update_learned_rule(
        "acme",
        "web",
        created.id,
        api.LearnedRuleInput(rule_text="admin edited", category="style"),
        _Req(is_admin=True, username="boss"),
    )
    store = IndexStore.open("acme", "web")
    assert store.get_learned_rule(created.id).rule_text == "admin edited"
    store.close()


def test_candidate_dashboard_flow_exposes_evidence_and_requires_approval(
    patched_db: AppDatabase,
):
    patched_db.register_repo("acme", "web")
    store = IndexStore.open("acme", "web")
    candidate, _ = store.upsert_learning_candidate(
        LearningCandidate(
            id=0,
            semantic_fingerprint="candidate-dashboard",
            rule_text="Do not flag fake fixture secrets.",
            rationale="The value only appears in test fixtures.",
            scope_type="path",
            scope_value="tests/fixtures/**",
            category="security",
            language="python",
            confidence=0.94,
            status="pending",
            synthesizer_version="test",
            evidence_ids_json="[41]",
            negative_examples_json=(
                '[{"feedback_id":41,"finding_id":"finding-1",'
                '"human_feedback":"this is a fake fixture"}]'
            ),
        )
    )
    store.close()

    detail = rules.get_learning_candidate("acme", "web", candidate.id)
    assert detail.evidence_count == 1
    assert detail.negative_examples[0]["finding_id"] == "finding-1"

    updated = rules.update_learning_candidate(
        "acme",
        "web",
        candidate.id,
        api.LearningCandidateInput(
            rule_text="Never flag known fake fixture secrets.",
            rationale="Maintainers confirmed these values are synthetic.",
            scope_type="path",
            scope_value="tests/fixtures/**",
            category="security",
            language="python",
        ),
        _Req(is_admin=True, username="boss"),
    )
    assert updated.rule_text.startswith("Never flag")

    approved = rules.approve_learning_candidate(
        "acme", "web", candidate.id, _Req(is_admin=True, username="boss")
    )
    store = IndexStore.open("acme", "web")
    try:
        active = store.get_learned_rule(approved["rule_id"])
        assert active is not None and active.created_by == "boss"
        assert active.origin_candidate_id == candidate.id
    finally:
        store.close()
