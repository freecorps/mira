"""Phase 4 — persistence, idempotency, and SQLite/Postgres parity.

The Postgres half runs the store's real SQL against a psycopg stand-in backed
by SQLite (the same trick `test_pg_store.py` uses), so both backends here are
exercising the *same* statements from `mira.gate.persistence` rather than two
hand-written copies that happen to agree today.
"""

from __future__ import annotations

import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path
from threading import Barrier

import pytest

from mira.feedback.models import ReviewFinding
from mira.gate.models import (
    CIState,
    GateDecision,
    GateInputs,
    Reason,
    RiskFactor,
    decision_key,
    delivery_key,
    override_key,
)
from mira.index import pg_store
from mira.index.pg_store import _PG_SCHEMA, PgIndexStore
from mira.index.store import IndexStore

# ── A psycopg stand-in, so the Postgres SQL is really executed ───────────────


class _FakeCursor:
    def __init__(self, conn: sqlite3.Connection):
        self._cur = conn.cursor()

    def execute(self, sql, params=()):
        self._cur.execute(sql.replace("%s", "?").replace(" FOR UPDATE", ""), params)
        return self

    def executemany(self, sql, seq_of_params):
        self._cur.executemany(sql.replace("%s", "?"), seq_of_params)
        return self

    @property
    def rowcount(self):
        return self._cur.rowcount

    def fetchone(self):
        return self._cur.fetchone()

    def fetchall(self):
        return self._cur.fetchall()

    def close(self):
        self._cur.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


class _FakeConn:
    def __init__(self):
        self._conn = sqlite3.connect(":memory:", check_same_thread=False)
        schema = _PG_SCHEMA.replace("BIGSERIAL PRIMARY KEY", "INTEGER PRIMARY KEY")
        schema = schema.replace("SERIAL PRIMARY KEY", "INTEGER PRIMARY KEY")
        self._conn.executescript(schema)

    def cursor(self):
        return _FakeCursor(self._conn)

    @contextmanager
    def transaction(self):
        self._conn.execute("BEGIN")
        try:
            yield self
        except Exception:
            self._conn.rollback()
            raise
        else:
            self._conn.commit()

    def commit(self):
        self._conn.commit()

    def close(self):
        self._conn.close()


@pytest.fixture
def sqlite_store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("MIRA_INDEX_DIR", str(tmp_path))
    monkeypatch.delenv("DATABASE_URL", raising=False)
    store = IndexStore.open("acme", "app")
    yield store
    store.close()


@pytest.fixture
def postgres_store(monkeypatch: pytest.MonkeyPatch):
    conn = _FakeConn()
    monkeypatch.setattr(pg_store, "_pg_conn", conn, raising=False)
    monkeypatch.setattr(pg_store, "_schema_initialized", True, raising=False)
    monkeypatch.setattr(pg_store, "_get_conn", lambda url, **_kwargs: conn)
    monkeypatch.setattr(pg_store, "_new_pg_conn", lambda url: conn)
    store = PgIndexStore("acme", "app", "postgresql://test")
    yield store
    conn.close()


@pytest.fixture(params=["sqlite", "postgres"])
def store(request, sqlite_store, postgres_store):
    """Both backends, so every assertion below is a parity assertion."""
    return sqlite_store if request.param == "sqlite" else postgres_store


def _decision(key: str = "", **overrides) -> GateDecision:
    inputs = GateInputs(
        platform="github",
        owner="acme",
        repo="app",
        pr_number=int(overrides.pop("pr_number", 7)),
        pr_url="https://github.com/acme/app/pull/7",
        pr_author="alice",
        base_branch="main",
        head_sha=str(overrides.pop("head_sha", "abc123")),
        labels=["ready"],
        changed_paths=["src/a.py"],
        changed_files=1,
        added_lines=10,
        ci=CIState(state="success", total=2),
    )
    decision = GateDecision(
        decision_key=key
        or decision_key(
            platform="github",
            owner="acme",
            repo="app",
            pr_number=inputs.pr_number,
            head_sha=inputs.head_sha,
            policy_version="gate-v1+deadbeef",
            mode="shadow",
            inputs_digest=inputs.digest,
        ),
        state=str(overrides.pop("state", "would_approve")),
        mode=str(overrides.pop("mode", "shadow")),
        risk_score=int(overrides.pop("risk_score", 12)),
        risk_band=str(overrides.pop("risk_band", "low")),
        policy_version="gate-v1+deadbeef",
        inputs=inputs,
        reasons=[Reason("eligible", "Eligible with risk score 12", "info")],
        factors=[RiskFactor("size_files", "Files changed", 3, "1 reviewable file")],
        capabilities={"provider": "github", "can_approve": True},
        delivery_state=str(overrides.pop("delivery_state", "pending")),
    )
    for name, value in overrides.items():
        setattr(decision, name, value)
    return decision


# ────────────────────────────────────────────────────────────── round trip ──


def test_a_decision_survives_a_round_trip_intact(store) -> None:
    stored, created = store.record_gate_decision(_decision())
    assert created is True
    assert stored.id > 0
    assert stored.state == "would_approve"
    assert stored.risk_score == 12
    assert stored.reasons[0].code == "eligible"
    assert stored.factors[0].points == 3
    assert stored.capabilities["provider"] == "github"
    # The inputs are the audit record: the decision has to be re-checkable.
    assert stored.inputs.ci.state == "success"
    assert stored.inputs.labels == ["ready"]
    assert stored.inputs.changed_paths == ["src/a.py"]


def test_recording_the_same_decision_twice_is_one_row(store) -> None:
    first, created_first = store.record_gate_decision(_decision())
    second, created_second = store.record_gate_decision(_decision())
    assert created_first is True
    assert created_second is False
    assert first.id == second.id
    assert store.count_gate_decisions() == 1


def test_a_re_evaluation_never_erases_an_override(store) -> None:
    stored, _ = store.record_gate_decision(_decision())
    store.record_gate_override(
        override_key="o1",
        decision=stored,
        actor="admin",
        reason="Released by hand",
        new_state="not_approved",
    )
    # The webhook fires again with the identical facts.
    store.record_gate_decision(_decision())
    assert store.get_gate_decision(stored.decision_key).state == "not_approved"


def test_filtering_and_paging_agree_with_the_count(store) -> None:
    for number in range(1, 6):
        store.record_gate_decision(
            _decision(pr_number=number, state="not_approved" if number % 2 else "would_approve")
        )
    assert store.count_gate_decisions() == 5
    assert store.count_gate_decisions({"state": "would_approve"}) == 2
    page = store.list_gate_decisions({"state": "would_approve"}, limit=1, offset=0)
    assert len(page) == 1
    assert page[0].state == "would_approve"
    assert len(store.list_gate_decisions(limit=2, offset=4)) == 1


def test_the_summary_buckets_by_state_and_mode(store) -> None:
    store.record_gate_decision(_decision(pr_number=1, state="would_approve"))
    store.record_gate_decision(_decision(pr_number=2, state="not_approved"))
    store.record_gate_decision(_decision(pr_number=3, state="approved"))
    buckets = {(row["state"], row["mode"]): row for row in store.summarize_gate_decisions()}
    assert buckets[("would_approve", "shadow")]["count"] == 1
    assert buckets[("approved", "shadow")]["approved"] == 1
    assert sum(row["count"] for row in buckets.values()) == 3


# ────────────────────────────────────────────────────── delivery claiming ──


def _claim(store, key: str, kind: str = "approval") -> bool:
    return store.claim_gate_delivery(
        delivery_key=key,
        decision_key="dk",
        platform="github",
        owner="acme",
        repo="app",
        pr_number=7,
        head_sha="abc123",
        kind=kind,
    )


def test_only_one_caller_can_claim_a_delivery(store) -> None:
    key = delivery_key(
        platform="github", owner="acme", repo="app", pr_number=7, head_sha="abc123", kind="approval"
    )
    assert _claim(store, key) is True
    assert _claim(store, key) is False
    store.finish_gate_delivery(key, state="delivered", ref="42")
    # A delivered approval is never re-claimed, however many webhooks arrive.
    assert _claim(store, key) is False
    assert store.get_gate_delivery(key)["state"] == "delivered"


def test_a_failed_delivery_is_reclaimable(store) -> None:
    key = "retryable"
    assert _claim(store, key) is True
    store.finish_gate_delivery(key, state="failed", error="502 from the platform")
    assert _claim(store, key) is True
    assert store.get_gate_delivery(key)["attempts"] == 2


def test_two_decisions_over_one_commit_share_one_approval_claim(store) -> None:
    """CI going green makes a new decision, not a second approval."""
    first = delivery_key(
        platform="github", owner="acme", repo="app", pr_number=7, head_sha="abc123", kind="approval"
    )
    second = delivery_key(
        platform="github", owner="acme", repo="app", pr_number=7, head_sha="abc123", kind="approval"
    )
    assert first == second
    assert _claim(store, first) is True
    assert _claim(store, second) is False


def test_approval_and_request_changes_are_separate_claims(store) -> None:
    approval = delivery_key(
        platform="github", owner="acme", repo="app", pr_number=7, head_sha="abc", kind="approval"
    )
    changes = delivery_key(
        platform="github",
        owner="acme",
        repo="app",
        pr_number=7,
        head_sha="abc",
        kind="request_changes",
    )
    assert approval != changes
    assert _claim(store, approval, "approval") is True
    assert _claim(store, changes, "request_changes") is True


def _race(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, work, workers: int = 4) -> list:
    """Run `work(store)` on N threads that all start at the same instant.

    The schema is created once up front: eight connections racing to run
    `CREATE TABLE IF NOT EXISTS` would contend on the file before the code
    under test ever ran, and a lock held there proves nothing about the claim.
    """
    monkeypatch.setenv("MIRA_INDEX_DIR", str(tmp_path))
    monkeypatch.delenv("DATABASE_URL", raising=False)
    IndexStore.open("acme", "app").close()
    barrier = Barrier(workers)

    def attempt(_: int):
        store = IndexStore.open("acme", "app")
        try:
            barrier.wait(timeout=30)
            for _attempt in range(10):
                try:
                    return work(store)
                except sqlite3.OperationalError:
                    # WAL write contention, not a second winner.
                    time.sleep(0.05)
            raise AssertionError("the write never got through")
        finally:
            store.close()

    with ThreadPoolExecutor(max_workers=workers) as pool:
        return list(pool.map(attempt, range(workers)))


def test_concurrent_workers_never_both_claim_an_approval(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The real race: two webhook deliveries landing on two threads at once."""
    key = delivery_key(
        platform="github", owner="acme", repo="app", pr_number=7, head_sha="abc", kind="approval"
    )
    results = _race(tmp_path, monkeypatch, lambda store: _claim(store, key))
    assert sum(1 for claimed in results if claimed) == 1


def test_concurrent_recording_of_one_decision_yields_one_row(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    created = _race(tmp_path, monkeypatch, lambda store: store.record_gate_decision(_decision())[1])
    assert sum(1 for was_new in created if was_new) == 1
    store = IndexStore.open("acme", "app")
    try:
        assert store.count_gate_decisions() == 1
    finally:
        store.close()


# ───────────────────────────────────────────────────────────────── overrides ──


def test_an_override_records_actor_reason_and_both_states(store) -> None:
    stored, _ = store.record_gate_decision(_decision())
    override, created = store.record_gate_override(
        override_key="o1",
        decision=stored,
        actor="admin",
        reason="Released manually after a hotfix review",
        new_state="not_approved",
        detail={"ticket": "OPS-1"},
    )
    assert created is True
    assert override["actor"] == "admin"
    assert override["reason"] == "Released manually after a hotfix review"
    assert override["previous_state"] == "would_approve"
    assert override["new_state"] == "not_approved"
    assert override["previous_risk"] == 12
    assert override["detail"]["ticket"] == "OPS-1"
    moved = store.get_gate_decision(stored.decision_key)
    assert moved.state == "not_approved"
    # A state without an actor beside it is a state nobody can account for.
    assert moved.overridden_by == "admin"


def test_a_retried_override_request_records_one_override(store) -> None:
    stored, _ = store.record_gate_decision(_decision())
    key = override_key(
        decision_key_value=stored.decision_key, actor="admin", new_state="not_approved"
    )
    _, first = store.record_gate_override(
        override_key=key,
        decision=stored,
        actor="admin",
        reason="r",
        new_state="not_approved",
    )
    _, second = store.record_gate_override(
        override_key=key,
        decision=stored,
        actor="admin",
        reason="r",
        new_state="not_approved",
    )
    assert (first, second) == (True, False)
    assert len(store.list_gate_overrides(decision_id=stored.id)) == 1


def test_the_override_trail_is_ordered_and_complete(store) -> None:
    stored, _ = store.record_gate_decision(_decision())
    store.record_gate_override(
        override_key="o1", decision=stored, actor="admin", reason="revoke", new_state="not_approved"
    )
    current = store.get_gate_decision(stored.decision_key)
    store.record_gate_override(
        override_key="o2", decision=current, actor="root", reason="restore", new_state="approved"
    )
    trail = store.list_gate_overrides(decision_id=stored.id)
    assert [item["new_state"] for item in trail] == ["approved", "not_approved"]
    assert trail[0]["previous_state"] == "not_approved"


# ─────────────────────────────────────────────────────────── finding counts ──


def _finding(
    store,
    finding_id: str,
    severity: str,
    state: str,
    category: str = "logic",
    pr_number: int = 7,
) -> None:
    store.save_review_finding(
        ReviewFinding(
            id=finding_id,
            fingerprint=f"fp-{finding_id}",
            review_id=0,
            platform="github",
            owner="acme",
            repo="app",
            pr_number=pr_number,
            pr_url="https://github.com/acme/app/pull/7",
            base_sha="base",
            head_sha="head",
            path="src/a.py",
            start_line=1,
            end_line=1,
            symbol="",
            category=category,
            severity=severity,
            confidence=0.9,
            title=f"finding {finding_id}",
            body="",
            suggestion="",
            detector="llm",
            prompt_model="test",
            state=state,
        )
    )


def test_open_findings_are_counted_conservatively(store) -> None:
    _finding(store, "f1", "blocker", "open")
    _finding(store, "f2", "warning", "open", category="security")
    _finding(store, "f3", "blocker", "fixed")
    _finding(store, "f4", "suggestion", "dismissed")
    # `outdated` only means the diff moved past the comment — which is exactly
    # what an unaddressed blocker looks like after a rebase.
    _finding(store, "f5", "blocker", "outdated")
    counts = store.gate_finding_counts(7)
    assert counts["blockers"] == 2
    assert counts["warnings"] == 1
    assert counts["security"] == 1
    assert counts["open"] == 3
    assert counts["worst"] == "blocker"


def test_findings_from_another_pr_do_not_leak_in(store) -> None:
    _finding(store, "f1", "blocker", "open")
    _finding(store, "other", "blocker", "open", pr_number=99)
    assert store.gate_finding_counts(7)["blockers"] == 1


def test_postgres_reads_are_scoped_to_their_repository(postgres_store, monkeypatch) -> None:
    """One table holds every repo, so an unscoped read would cross repos."""
    postgres_store.record_gate_decision(_decision())
    other = PgIndexStore("acme", "other", "postgresql://test")
    other_decision = _decision(key="other-key")
    other_decision.inputs.repo = "other"
    other.record_gate_decision(other_decision)
    assert postgres_store.count_gate_decisions() == 1
    assert other.count_gate_decisions() == 1
    org_wide = PgIndexStore("", "", "postgresql://test")
    assert org_wide.count_gate_decisions() == 2
