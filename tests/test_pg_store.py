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
