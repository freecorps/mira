"""PgIndexStore tests against a SQLite-backed psycopg stand-in.

The real ``_PG_SCHEMA`` happens to parse under SQLite, so these tests run the
store's actual SQL (with ``%s`` swapped for ``?``) against an in-memory
database — real behavior coverage without a Postgres server.
"""

from __future__ import annotations

import json
import sqlite3
import time
from contextlib import contextmanager

import pytest

from mira.feedback.lifecycle import approve_candidate, version_rule
from mira.feedback.models import LearningCandidate
from mira.index import pg_store
from mira.index.pg_store import _PG_SCHEMA, PgIndexStore
from mira.index.store import IndexStore
from mira.models import PRFingerprint


class _FakeCursor:
    def __init__(self, conn: sqlite3.Connection):
        self._cur = conn.cursor()

    def execute(self, sql, params=()):
        sqlite_sql = sql.replace("%s", "?").replace(" FOR UPDATE", "")
        self._cur.execute(sqlite_sql, params)
        return self

    def executemany(self, sql, seq_of_params):
        self._cur.executemany(sql.replace("%s", "?"), seq_of_params)
        return self

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
        self._conn = sqlite3.connect(":memory:")
        # SERIAL isn't a rowid alias in SQLite — ids would insert as NULL.
        sqlite_schema = _PG_SCHEMA.replace("BIGSERIAL PRIMARY KEY", "INTEGER PRIMARY KEY")
        sqlite_schema = sqlite_schema.replace("SERIAL PRIMARY KEY", "INTEGER PRIMARY KEY")
        self._conn.executescript(sqlite_schema)
        self._conn.executescript(
            "CREATE UNIQUE INDEX idx_pg_learned_rules_candidate_once "
            "ON learned_rules(origin_candidate_id) "
            "WHERE origin_candidate_id IS NOT NULL AND status = 'approved';"
            "CREATE UNIQUE INDEX idx_pg_learned_rules_successor_once "
            "ON learned_rules(supersedes_rule_id) "
            "WHERE supersedes_rule_id IS NOT NULL AND status = 'approved';"
        )

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

    def rollback(self):
        self._conn.rollback()

    def close(self):
        # The production atomic path owns and closes a dedicated connection.
        # This in-memory stand-in is shared by the parity fixture, so closing
        # it is intentionally a no-op.
        pass


@pytest.fixture
def fake_conn(monkeypatch):
    conn = _FakeConn()
    monkeypatch.setattr(pg_store, "_get_conn", lambda url: conn)
    monkeypatch.setattr(pg_store, "_new_pg_conn", lambda url: conn)
    return conn


@pytest.fixture
def store(fake_conn):
    return PgIndexStore("acme", "widgets", "postgresql://fake")


def _fp(number, *, head_sha="sha", updated_at=0.0, paths=None, symbols=None):
    return PRFingerprint(
        pr_number=number,
        head_sha=head_sha,
        title=f"PR {number}",
        body="",
        paths=paths or [],
        symbols=symbols or [],
        updated_at=updated_at,
    )


def test_fingerprint_upsert_and_list(store):
    store.upsert_pr_fingerprint(_fp(7, paths=["a.py", "b.py"], symbols=["foo"]))

    rows = store.list_pr_fingerprints()
    assert len(rows) == 1
    got = rows[0]
    assert got.pr_number == 7
    assert got.title == "PR 7"
    assert got.paths == ["a.py", "b.py"]
    assert got.symbols == ["foo"]
    assert got.updated_at > 0


def test_fingerprint_upsert_replaces_on_conflict(store):
    store.upsert_pr_fingerprint(_fp(7, head_sha="old", paths=["a.py"]))
    store.upsert_pr_fingerprint(_fp(7, head_sha="new", paths=["a.py", "c.py"]))

    rows = store.list_pr_fingerprints()
    assert len(rows) == 1
    assert rows[0].head_sha == "new"
    assert rows[0].paths == ["a.py", "c.py"]


def test_fingerprint_upsert_prunes_stale_rows(store):
    now = time.time()
    store.upsert_pr_fingerprint(_fp(1, updated_at=now - IndexStore._FINGERPRINT_TTL - 3600))
    store.upsert_pr_fingerprint(_fp(2, updated_at=now - 60))
    store.upsert_pr_fingerprint(_fp(3))

    numbers = {fp.pr_number for fp in store.list_pr_fingerprints()}
    assert numbers == {2, 3}


def test_add_and_list_review_comments(store):
    store.add_review_comments(
        1,
        42,
        "https://github.com/acme/widgets/pull/42",
        [
            {"path": "a.py", "line": 3, "severity": "warning", "title": "t1", "body": "b1"},
            {"path": "b.py", "line": 9, "severity": "blocker", "title": "t2", "body": "b2"},
        ],
    )

    rows = store.list_review_comments(42)
    assert [(r.path, r.line, r.severity) for r in rows] == [
        ("a.py", 3, "warning"),
        ("b.py", 9, "blocker"),
    ]


def test_record_and_list_replies(store):
    row = store.record_reply(
        42,
        "https://github.com/acme/widgets/pull/42",
        author="alice",
        body="looks fixed",
        comment_path="a.py",
        comment_line=3,
    )
    assert row.id > 0

    rows = store.list_replies(42)
    assert len(rows) == 1
    assert rows[0].author == "alice"
    assert rows[0].body == "looks fixed"


def test_fingerprints_scoped_by_repo(fake_conn):
    a = PgIndexStore("acme", "widgets", "postgresql://fake")
    b = PgIndexStore("acme", "gadgets", "postgresql://fake")
    now = time.time()

    # A stale row in repo B must survive repo A's prune-on-write.
    b.upsert_pr_fingerprint(_fp(1, updated_at=now - IndexStore._FINGERPRINT_TTL - 3600))
    a.upsert_pr_fingerprint(_fp(1, updated_at=now))

    assert [fp.pr_number for fp in a.list_pr_fingerprints()] == [1]
    assert [fp.pr_number for fp in b.list_pr_fingerprints()] == [1]
    assert b.list_pr_fingerprints()[0].updated_at < now - IndexStore._FINGERPRINT_TTL


def test_governed_learning_candidate_parity(store):
    candidate = LearningCandidate(
        id=0,
        semantic_fingerprint="semantic-1",
        rule_text="Do not flag generated fixtures.",
        rationale="A maintainer identified the fixture as synthetic.",
        scope_type="path",
        scope_value="tests/fixtures/**",
        category="security",
        language="python",
        confidence=0.9,
        status="pending",
        synthesizer_version="test",
        evidence_ids_json="[1]",
        negative_examples_json='[{"feedback_id": 1}]',
    )
    first, created = store.upsert_learning_candidate(candidate)
    assert created and first.evidence_count == 1

    candidate.evidence_ids_json = "[2]"
    candidate.negative_examples_json = '[{"feedback_id": 2}]'
    merged, created = store.upsert_learning_candidate(candidate)
    assert not created
    assert json.loads(merged.evidence_ids_json) == [1, 2]

    rule = approve_candidate(store, merged.id, actor="admin")
    assert rule.origin_candidate_id == merged.id
    assert store.get_learning_candidate(merged.id).status == "approved"
    assert [item.id for item in store.list_active_learned_rules()] == [rule.id]


def test_governed_learning_rule_version_parity(store):
    original = store.create_learned_rule(
        "Ignore style in generated code.",
        "style",
        path_pattern="generated/**",
        scope_type="path",
        scope_value="generated/**",
        created_by="admin",
    )
    replacement = version_rule(
        store,
        original.id,
        rule_text="Do not report style issues in generated code.",
        category="style",
        scope_type="path",
        scope_value="generated/**",
        actor="admin",
    )

    assert replacement.version == 2
    assert replacement.supersedes_rule_id == original.id
    assert store.get_learned_rule(original.id).status == "superseded"


def test_postgres_org_rules_apply_across_repositories(fake_conn):
    source = PgIndexStore("acme", "policy", "postgresql://fake")
    target = PgIndexStore("acme", "widgets", "postgresql://fake")
    outsider = PgIndexStore("other", "policy", "postgresql://fake")
    org_rule = source.create_learned_rule(
        "Never log credentials.",
        "security",
        scope_type="org",
        scope_value="acme",
    )
    outsider.create_learned_rule(
        "Use another organization's convention.",
        "style",
        scope_type="org",
        scope_value="other",
    )

    assert org_rule.rule_text in {rule.rule_text for rule in target.list_active_learned_rules()}
    assert "Use another organization's convention." not in {
        rule.rule_text for rule in target.list_active_learned_rules()
    }


# ─────────────────────────── Phase 3 evaluation analytics ───────────────────


def _pg_finding(store, finding_id, *, path="src/a.py", category="security", state="open"):
    from mira.feedback.models import ReviewFinding

    store.save_review_finding(
        ReviewFinding(
            id=finding_id,
            fingerprint=f"fp-{finding_id}",
            review_id=0,
            platform="github",
            owner=store._owner,
            repo=store._repo,
            pr_number=7,
            pr_url="https://example.test/pull/7",
            base_sha="base",
            head_sha="head",
            path=path,
            start_line=10,
            end_line=10,
            symbol="",
            category=category,
            severity="warning",
            confidence=0.9,
            title="Unsafe call",
            body="body",
            suggestion="",
            detector="main",
            prompt_model="model",
            state=state,
        )
    )
    if state != "open":
        store.update_review_finding_state(finding_id, state)


def _pg_evaluation(store, finding_id, *, rule_id=1, decision="instruction"):
    from mira.feedback.evaluation import RuleEvaluation, evaluation_key

    return RuleEvaluation(
        evaluation_key=evaluation_key(
            platform="github",
            owner=store._owner,
            repo=store._repo,
            pr_number=7,
            head_sha="head",
            rule_id=rule_id,
            rule_version=1,
            decision=decision,
            finding_id=finding_id,
        ),
        rule_id=rule_id,
        rule_version=1,
        rule_origin="learned",
        category="security",
        decision=decision,
        finding_id=finding_id,
        platform="github",
        owner=store._owner,
        repo=store._repo,
        pr_number=7,
        pr_author="alice",
        head_sha="head",
    )


def _pg_feedback(store, finding_id, kind, actor="bob"):
    from mira.feedback.models import FeedbackEventV2

    store.record_feedback_v2(
        FeedbackEventV2(
            id=0,
            finding_id=finding_id,
            kind=kind,
            actor=actor,
            actor_role="",
            raw_text="",
            rationale="",
            platform="github",
            source_event_id=f"{kind}:{finding_id}:{actor}",
            head_sha="head",
            thread_state="",
            provenance_complete=True,
        )
    )


def _seed_evaluation_fixture(store):
    """The same mix of signals on both backends, so the numbers must match."""
    plan = {
        "up": "thumbs_up",
        "down": "thumbs_down",
        "question": "reply_question",
        "silent": "unobserved",
        "agree": "reply_agree",
    }
    for finding_id, kind in plan.items():
        _pg_finding(store, finding_id)
        store.record_rule_evaluations([_pg_evaluation(store, finding_id)])
        _pg_feedback(store, finding_id, kind)
    _pg_finding(store, "resolved", state="fixed")
    store.record_rule_evaluations([_pg_evaluation(store, "resolved")])
    _pg_feedback(store, "resolved", "fixed", actor="merger")
    # A review-scoped exposure with no finding attached.
    store.record_rule_evaluations([_pg_evaluation(store, None)])


def test_rule_evaluation_retry_is_idempotent_on_postgres(store):
    _pg_finding(store, "f1")
    evaluation = _pg_evaluation(store, "f1")

    assert store.record_rule_evaluations([evaluation]) == 1
    assert store.record_rule_evaluations([evaluation]) == 0
    assert store.count_rule_evaluations({"rule_id": 1}) == 1


def test_rule_analytics_parity_between_backends(store, tmp_path, monkeypatch):
    """Identical inputs must produce identical aggregates on both backends."""
    monkeypatch.setenv("MIRA_INDEX_DIR", str(tmp_path))
    monkeypatch.delenv("DATABASE_URL", raising=False)

    sqlite_store = IndexStore.open("acme", "widgets")
    try:
        _seed_evaluation_fixture(sqlite_store)
        sqlite_counts = sqlite_store.aggregate_rule_analytics({"rule_id": 1})[0].counts.as_dict()
        sqlite_details = sqlite_store.list_rule_evaluations({"rule_id": 1}, limit=100)
    finally:
        sqlite_store.close()

    _seed_evaluation_fixture(store)
    pg_counts = store.aggregate_rule_analytics({"rule_id": 1})[0].counts.as_dict()
    pg_details = store.list_rule_evaluations({"rule_id": 1}, limit=100)

    assert pg_counts == sqlite_counts
    assert sorted((r["finding_id"] or "", r["outcome"]) for r in pg_details) == sorted(
        (r["finding_id"] or "", r["outcome"]) for r in sqlite_details
    )
    # And the shared semantics actually held, rather than both being empty.
    assert sqlite_counts["exposures"] == 7
    assert sqlite_counts["findings"] == 6
    assert sqlite_counts["positive"] == 3  # thumbs_up, reply_agree, fixed
    assert sqlite_counts["negative"] == 1
    assert sqlite_counts["neutral"] == 1
    assert sqlite_counts["unobserved"] == 1
    assert sqlite_counts["addressed"] == 1


def test_postgres_summary_and_evidence_agree(store):
    _seed_evaluation_fixture(store)

    counts = store.aggregate_rule_analytics({"rule_id": 1})[0].counts
    for outcome in ("positive", "negative", "neutral", "unobserved"):
        rows = store.list_rule_evaluations({"rule_id": 1}, outcome=outcome, limit=100)
        assert getattr(counts, outcome) == len(rows)

    buckets = {b["bucket"]: b for b in store.rule_analytics_summary(dimension="author")}
    assert buckets["alice"]["exposures"] == 7


def test_postgres_unobserved_is_never_positive(store):
    _pg_finding(store, "f1", state="outdated")
    store.record_rule_evaluations([_pg_evaluation(store, "f1")])
    _pg_feedback(store, "f1", "unobserved", actor="merger")

    counts = store.aggregate_rule_analytics({"rule_id": 1})[0].counts
    assert counts.positive == 0
    assert counts.addressed == 0
    assert counts.acceptance_rate is None


def test_postgres_audit_events_scoped_by_repo(fake_conn):
    a = PgIndexStore("acme", "widgets", "postgresql://fake")
    b = PgIndexStore("acme", "gadgets", "postgresql://fake")
    a.record_learning_audit_event(event_type="regression_dismissed", rule_id=1, actor="admin")

    assert len(a.list_learning_audit_events()) == 1
    assert b.list_learning_audit_events() == []


def test_postgres_rule_evaluations_scoped_by_repo(fake_conn):
    a = PgIndexStore("acme", "widgets", "postgresql://fake")
    b = PgIndexStore("acme", "gadgets", "postgresql://fake")
    _pg_finding(a, "f1")
    a.record_rule_evaluations([_pg_evaluation(a, "f1")])

    assert a.count_rule_evaluations({"rule_id": 1}) == 1
    assert b.count_rule_evaluations({"rule_id": 1}) == 0
