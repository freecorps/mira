"""Concurrency regressions for governed learning state transitions."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier

import pytest

from mira.feedback.lifecycle import approve_candidate, version_rule
from mira.feedback.models import LearningCandidate
from mira.index.store import IndexStore


@pytest.fixture
def isolated_index(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MIRA_INDEX_DIR", str(tmp_path))
    monkeypatch.delenv("DATABASE_URL", raising=False)
    initial = IndexStore.open("acme", "app")
    initial.close()


def _candidate(evidence_id: int) -> LearningCandidate:
    return LearningCandidate(
        id=0,
        semantic_fingerprint="same-candidate",
        rule_text="Do not flag generated fixtures.",
        rationale="Fixtures are synthetic.",
        scope_type="path",
        scope_value="tests/fixtures/**",
        category="security",
        language="python",
        confidence=0.9,
        status="pending",
        synthesizer_version="test",
        evidence_ids_json=f"[{evidence_id}]",
    )


def test_candidate_upsert_merges_concurrent_evidence(isolated_index: None) -> None:
    barrier = Barrier(2)

    def upsert(evidence_id: int) -> int:
        store = IndexStore.open("acme", "app")
        try:
            barrier.wait()
            candidate, _created = store.upsert_learning_candidate(_candidate(evidence_id))
            return candidate.id
        finally:
            store.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        ids = list(pool.map(upsert, (1, 2)))

    store = IndexStore.open("acme", "app")
    try:
        candidates = store.list_learning_candidates()
        assert len(candidates) == 1
        assert candidates[0].evidence_count == 2
        assert ids == [candidates[0].id, candidates[0].id]
    finally:
        store.close()


def test_candidate_approval_creates_one_rule_under_concurrency(isolated_index: None) -> None:
    store = IndexStore.open("acme", "app")
    candidate, _ = store.upsert_learning_candidate(_candidate(1))
    store.close()
    barrier = Barrier(2)

    def approve() -> int:
        worker_store = IndexStore.open("acme", "app")
        try:
            barrier.wait()
            return approve_candidate(worker_store, candidate.id, actor="admin").id
        finally:
            worker_store.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        ids = list(pool.map(lambda _value: approve(), range(2)))

    store = IndexStore.open("acme", "app")
    try:
        rules = [
            rule
            for rule in store.list_learned_rules()
            if rule.origin_candidate_id == candidate.id and rule.status == "approved"
        ]
        assert len(rules) == 1
        assert ids == [rules[0].id, rules[0].id]
        assert store.get_learning_candidate(candidate.id).status == "approved"
    finally:
        store.close()


def test_rule_versioning_creates_one_successor_under_concurrency(isolated_index: None) -> None:
    store = IndexStore.open("acme", "app")
    original = store.create_learned_rule(
        "Ignore style in generated code.",
        "style",
        scope_type="path",
        scope_value="generated/**",
        source_signal="manual",
    )
    store.close()
    barrier = Barrier(2)

    def version() -> int:
        worker_store = IndexStore.open("acme", "app")
        try:
            barrier.wait()
            return version_rule(
                worker_store,
                original.id,
                rule_text="Do not report style in generated code.",
                category="style",
                scope_type="path",
                scope_value="generated/**",
                actor="admin",
            ).id
        finally:
            worker_store.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        ids = list(pool.map(lambda _value: version(), range(2)))

    store = IndexStore.open("acme", "app")
    try:
        successors = [
            rule
            for rule in store.list_learned_rules()
            if rule.supersedes_rule_id == original.id and rule.status == "approved"
        ]
        prior = store.get_learned_rule(original.id)
        assert len(successors) == 1
        assert ids == [successors[0].id, successors[0].id]
        assert prior is not None and prior.status == "superseded" and not prior.active
    finally:
        store.close()
