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

    @property
    def rowcount(self):
        # psycopg exposes it and some store primitives read it back to tell an
        # insert from a no-op; sqlite3 exposes the same attribute.
        return self._cur.rowcount

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
    monkeypatch.setattr(pg_store, "_get_conn", lambda url, **_kwargs: conn)
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


def _pg_finding(store, finding_id, *, path="src/a.py", category="security", state="open") -> None:
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


def _pg_evaluation(store, finding_id, *, rule_id=1, decision="instruction"):  # noqa: ANN201
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


def _pg_feedback(store, finding_id, kind, actor="bob") -> None:
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


def _seed_evaluation_fixture(store) -> None:
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


def test_postgres_org_wide_handle_reads_every_repo(fake_conn):
    """An empty owner/repo is the deliberate org-wide handle.

    Regression guard: the audit listing pinned `owner=''`/`repo=''` literally,
    so an org-wide request matched only rows with empty scope — i.e. nothing.
    """
    a = PgIndexStore("acme", "widgets", "postgresql://fake")
    b = PgIndexStore("acme", "gadgets", "postgresql://fake")
    a.record_learning_audit_event(event_type="regression_dismissed", rule_id=1)
    b.record_learning_audit_event(event_type="regression_accepted", rule_id=2)

    org_wide = PgIndexStore("", "", "postgresql://fake")
    assert {event["repo"] for event in org_wide.list_learning_audit_events()} == {
        "widgets",
        "gadgets",
    }
    # An owner-scoped handle still spans that owner's repositories.
    owner_wide = PgIndexStore("acme", "", "postgresql://fake")
    assert len(owner_wide.list_learning_audit_events()) == 2
    # And a repo-scoped handle stays scoped.
    assert len(a.list_learning_audit_events()) == 1


def test_postgres_org_wide_aggregate_keeps_rule_metadata(fake_conn):
    """Org-wide rows must still carry rule text, status and activation date.

    Regression guard: metadata was fetched with the store's own owner/repo,
    which is empty on the org-wide handle, so every rule rendered blank.
    """
    repo_store = PgIndexStore("acme", "widgets", "postgresql://fake")
    rule = repo_store.create_learned_rule("Never log credentials.", "security", created_by="admin")
    _pg_finding(repo_store, "f1")
    repo_store.record_rule_evaluations([_pg_evaluation(repo_store, "f1", rule_id=rule.id)])

    org_wide = PgIndexStore("", "", "postgresql://fake")
    rows = org_wide.aggregate_rule_analytics()
    assert len(rows) == 1
    assert rows[0].rule_text == "Never log credentials."
    assert rows[0].status == "approved"
    assert rows[0].active is True
    assert rows[0].owner == "acme"
    assert rows[0].repo == "widgets"


# ── Phase 6 pre-merge checks ────────────────────────────────────────────────
#
# The queries live in `ChecksStoreMixin` and are shared verbatim with SQLite,
# so what is worth asserting here is the half that is *not* shared: the
# Postgres schema carries the same columns, the upsert clause both engines
# spell identically really does update, and one table holding every repository
# stays scoped to the one that asked.


def _check_run(pr_number=7, *, head_sha="head123", state="violation", owner="acme", repo="widgets"):
    from mira.checks.models import (
        CheckFinding,
        CheckResult,
        CheckRun,
        CheckRunInputs,
        Evidence,
        result_key,
        run_key,
    )

    inputs = CheckRunInputs(
        platform="github",
        owner=owner,
        repo=repo,
        pr_number=pr_number,
        pr_url=f"https://github.com/{owner}/{repo}/pull/{pr_number}",
        pr_author="alice",
        head_sha=head_sha,
        changed_paths=["src/a.py"],
        changed_files=1,
        added_lines=3,
    )
    key = run_key(
        platform="github",
        owner=owner,
        repo=repo,
        pr_number=pr_number,
        head_sha=head_sha,
        policy_version="checks-v1+abc",
        inputs_digest=inputs.digest,
    )
    result = CheckResult(
        check_id="native.tests",
        title="Tests",
        origin="native",
        mode="error",
        state=state,
        summary="source changed with no test",
        evidence=[Evidence(path="src/a.py", detail="3 added lines", source="diff")],
        findings=(
            [
                CheckFinding(
                    fingerprint="fp1",
                    title="Source changed and no test changed with it",
                    evidence=[Evidence(path="src/a.py", start_line=3, source="diff")],
                    sources=["native.tests"],
                )
            ]
            if state == "violation"
            else []
        ),
        duration_seconds=0.25,
        config_digest="cfg1",
        result_key=result_key(run_key_value=key, check_id="native.tests"),
        sources=["native.tests"],
    )
    return CheckRun(
        run_key=key,
        policy_version="checks-v1+abc",
        inputs=inputs,
        results=[result],
        duration_seconds=0.5,
        created_at=time.time(),
    )


def test_postgres_check_run_round_trips_with_its_evidence(store):
    run = _check_run()
    stored, created = store.record_check_run(run)
    assert created is True
    assert stored.verdict == "violation"

    read = store.get_check_run(run.run_key)
    result = read.results[0]
    assert result.state == "violation"
    assert result.mode == "error"
    assert result.config_digest == "cfg1"
    assert result.evidence[0].path == "src/a.py"
    assert result.findings[0].evidence[0].start_line == 3


def test_postgres_check_retry_is_idempotent_and_refreshes(store):
    failed = _check_run(state="infrastructure_error")
    store.record_check_run(failed)
    recovered = _check_run(state="pass")
    _, created = store.record_check_run(recovered)

    assert created is False
    assert store.count_check_runs({"pr_number": 7}) == 1
    assert store.get_check_run(failed.run_key).verdict == "pass"


def test_postgres_check_rows_are_scoped_by_repository(fake_conn):
    """One table holds every repository, so an unscoped read would leak."""
    a = PgIndexStore("acme", "alpha", "postgresql://fake")
    b = PgIndexStore("acme", "beta", "postgresql://fake")
    a.record_check_run(_check_run(pr_number=1, repo="alpha"))
    b.record_check_run(_check_run(pr_number=2, repo="beta"))

    assert a.count_check_runs({}) == 1
    assert b.count_check_runs({}) == 1
    assert a.list_check_runs({})[0].inputs.pr_number == 1
    # The org-wide handle is the deliberate exception.
    assert PgIndexStore("", "", "postgresql://fake").count_check_runs({}) == 2


def test_postgres_and_sqlite_agree_on_a_check_run(store, tmp_path, monkeypatch):
    """Identical inputs must produce identical rows on both backends."""
    monkeypatch.setenv("MIRA_INDEX_DIR", str(tmp_path))
    run = _check_run()
    store.record_check_run(run)
    pg_result = store.get_check_run(run.run_key).results[0].as_dict()

    sqlite_store = IndexStore.open("acme", "widgets")
    try:
        sqlite_store.record_check_run(_check_run())
        sqlite_result = sqlite_store.get_check_run(run.run_key).results[0].as_dict()
    finally:
        sqlite_store.close()

    for field in ("state", "mode", "origin", "summary", "config_digest", "findings", "evidence"):
        assert pg_result[field] == sqlite_result[field]


def test_postgres_check_writes_commit_together(store):
    """A dedicated connection, so another writer cannot commit half a run.

    Deferring commits on the *shared* handle would leave this run's rows to be
    committed by whatever wrote next on it — which is the failure the
    transaction exists to prevent, wearing a different hat.
    """
    store.record_check_run(_check_run(state="pass"))
    original = store._checks_exec
    # A flag rather than `monkeypatch.undo()`: undoing would also revert the
    # fake connection this store is built on, and the read below would try to
    # reach a real database.
    failing = True

    def _fail_on_the_run_row(sql, params=()):
        if failing and "INSERT INTO check_runs" in sql:
            raise RuntimeError("the connection went away")
        return original(sql, params)

    store._checks_exec = _fail_on_the_run_row  # type: ignore[method-assign]
    with pytest.raises(RuntimeError):
        store.record_check_run(_check_run(state="violation"))

    failing = False
    read = store.get_check_run(_check_run().run_key)
    assert read.results[0].state == "pass"
    assert read.verdict == "pass"


def test_postgres_check_atomic_uses_a_dedicated_connection(store, monkeypatch):
    """Asserted directly, because the shared handle is the thing to avoid."""
    used: list[str] = []
    original = pg_store.PgIndexStore._transaction_cursor

    def _record(self):
        used.append("dedicated")
        return original(self)

    monkeypatch.setattr(pg_store.PgIndexStore, "_transaction_cursor", _record)
    store.record_check_run(_check_run())
    assert used == ["dedicated"]


# ── Phase 7C triage ─────────────────────────────────────────────────────────
#
# The same parity questions the checks asked, about a table that names people:
# does a run round-trip, does a retry converge on one row, is a read scoped to
# one repository, and does a run become visible all at once or not at all.


def _triage_run(pr_number=7, *, head_sha="head222", owner="acme", repo="widgets", identity="dana"):
    from mira.triage.models import (
        Classification,
        Evidence,
        Exclusion,
        ReviewerCandidate,
        SignalContribution,
        SignalReport,
        TriageInputs,
        TriageRun,
        run_key,
    )

    inputs = TriageInputs(
        platform="github",
        owner=owner,
        repo=repo,
        pr_number=pr_number,
        pr_url=f"https://github.com/{owner}/{repo}/pull/{pr_number}",
        pr_author="kit",
        base_sha="base111",
        head_sha=head_sha,
        ownership_ref="base111",
        changed_paths=["src/app.py"],
        changed_files=1,
    )
    return TriageRun(
        run_key=run_key(
            platform="github",
            owner=owner,
            repo=repo,
            pr_number=pr_number,
            head_sha=head_sha,
            policy_version="triage-v1+abc",
            inputs_digest=inputs.digest,
        ),
        policy_version="triage-v1+abc",
        inputs=inputs,
        classification=Classification(size="s", changed_files=1, changed_lines=4, kinds=["code"]),
        candidates=[
            ReviewerCandidate(
                identity=identity,
                score=3.0,
                contributions=[
                    SignalContribution(
                        kind="codeowners",
                        raw=1,
                        weight=3.0,
                        score=3.0,
                        evidence=[Evidence(path="src/app.py", line=2, source="codeowners")],
                    )
                ],
            )
        ],
        signals=[SignalReport(kind="codeowners", status="available", candidates=1)],
        excluded=[Exclusion(identity="kit", reason="author")],
        notes=["review load could not be read"],
        created_at=time.time(),
    )


def test_postgres_triage_run_round_trips_with_its_evidence(store):
    run = _triage_run()
    stored, created = store.record_triage_run(run)
    assert created is True
    assert stored.status == "ok"
    assert stored.suggested == ["dana"]
    assert stored.candidates[0].contributions[0].evidence[0].line == 2
    assert stored.notes == ["review load could not be read"]
    assert stored.excluded[0].reason == "author"


def test_postgres_triage_retry_is_idempotent(store):
    store.record_triage_run(_triage_run())
    stored, created = store.record_triage_run(_triage_run())
    assert created is False
    assert stored.attempts == 2
    assert store.count_triage_runs({"pr_number": 7}) == 1


def test_postgres_triage_rows_are_scoped_by_repository(fake_conn):
    """One table holds every repository, and these rows name people."""
    a = PgIndexStore("acme", "alpha", "postgresql://fake")
    b = PgIndexStore("acme", "beta", "postgresql://fake")
    a.record_triage_run(_triage_run(pr_number=1, repo="alpha"))
    b.record_triage_run(_triage_run(pr_number=2, repo="beta", identity="sam"))

    assert a.count_triage_runs({}) == 1
    assert b.count_triage_runs({}) == 1
    assert a.list_triage_runs({})[0].suggested == ["dana"]
    assert a.summarize_triage_candidates({}) == [
        {"identity": "dana", "kind": "user", "count": 1, "average_rank": 1.0, "average_score": 3.0}
    ]
    # The org-wide handle is the deliberate exception.
    assert PgIndexStore("", "", "postgresql://fake").count_triage_runs({}) == 2


def test_postgres_path_contributions_are_scoped_and_idempotent(fake_conn):
    a = PgIndexStore("acme", "alpha", "postgresql://fake")
    b = PgIndexStore("acme", "beta", "postgresql://fake")
    row = {
        "platform": "github",
        "path": "src/app.py",
        "identity": "dana",
        "role": "authored",
        "source": "commit",
        "reference": "abc",
        "event_at": 1.0,
    }
    assert a.record_path_contributions([row]) == 1
    assert a.record_path_contributions([row]) == 0
    assert len(a.path_contributions(["src/app.py"], since=0)) == 1
    assert b.path_contributions(["src/app.py"], since=0) == []


def test_postgres_and_sqlite_agree_on_a_triage_run(store, tmp_path, monkeypatch):
    """Identical inputs must produce identical rows on both backends."""
    monkeypatch.setenv("MIRA_INDEX_DIR", str(tmp_path))
    run = _triage_run()
    store.record_triage_run(run)
    pg_row = store.get_triage_run(run.run_key).as_dict()

    sqlite_store = IndexStore.open("acme", "widgets")
    try:
        sqlite_store.record_triage_run(_triage_run())
        sqlite_row = sqlite_store.get_triage_run(run.run_key).as_dict()
    finally:
        sqlite_store.close()

    for field in ("status", "degraded", "candidates", "signals", "excluded", "classification"):
        assert pg_row[field] == sqlite_row[field]


def test_postgres_triage_writes_commit_together(store):
    """A run and its candidates become visible at once, or not at all.

    A reader that found the run row without its candidates would be reading a
    suggestion with somebody missing from it.
    """
    store.record_triage_run(_triage_run())
    original = store._triage_exec
    failing = True

    def _fail_on_the_run_row(sql, params=()):
        if failing and "INSERT INTO triage_runs" in sql:
            raise RuntimeError("the connection went away")
        return original(sql, params)

    store._triage_exec = _fail_on_the_run_row  # type: ignore[method-assign]
    with pytest.raises(RuntimeError):
        store.record_triage_run(_triage_run(identity="sam"))

    failing = False
    read = store.get_triage_run(_triage_run().run_key)
    assert read.suggested == ["dana"]


def test_postgres_triage_atomic_uses_a_dedicated_connection(store, monkeypatch):
    used: list[str] = []
    original = pg_store.PgIndexStore._transaction_cursor

    def _record(self):
        used.append("dedicated")
        return original(self)

    monkeypatch.setattr(pg_store.PgIndexStore, "_transaction_cursor", _record)
    store.record_triage_run(_triage_run())
    assert used == ["dedicated"]
