"""Phase 5 — the durable queue, on both backends.

The queue is a table, which means its correctness is entirely a property of a
handful of SQL statements. These exercise those statements directly: leases,
crashes, retries, dead-lettering, cancellation, and the claim under contention.

The Postgres half runs the store's real SQL against a SQLite stand-in — the
same technique `test_pg_store.py` uses — so the parity assertions compare
*behaviour*, not two hand-written expectations that happen to agree.
"""

from __future__ import annotations

import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path

import pytest

from mira.autofix.models import (
    AutofixAttempt,
    AutofixJob,
    Reason,
    ReasonCode,
    ValidationResult,
    job_key,
)
from mira.index import pg_store
from mira.index.pg_store import _PG_SCHEMA, PgIndexStore
from mira.index.store import IndexStore


@pytest.fixture(autouse=True)
def isolated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MIRA_INDEX_DIR", str(tmp_path))
    monkeypatch.delenv("DATABASE_URL", raising=False)


def _job(*, finding: str = "f1", head: str = "sha1", **overrides) -> AutofixJob:
    base = {
        "job_key": job_key(
            platform="github",
            owner="acme",
            repo="app",
            pr_number=7,
            head_sha=head,
            finding_id=finding,
            mode="branch_pr",
        ),
        "owner": "acme",
        "repo": "app",
        "pr_number": 7,
        "pr_url": "https://github.com/acme/app/pull/7",
        "head_sha": head,
        "finding_id": finding,
        "finding_title": "A finding",
        "requested_by": "alice",
        "max_attempts": 2,
        "available_at": time.time() - 1,
    }
    base.update(overrides)
    return AutofixJob(**base)


# ── the SQLite backend ───────────────────────────────────────────────────────


@pytest.fixture
def store() -> IndexStore:
    handle = IndexStore.open("acme", "app")
    yield handle
    handle.close()


def test_enqueue_is_idempotent_on_the_job_key(store: IndexStore) -> None:
    first, created_one = store.enqueue_autofix_job(_job())
    second, created_two = store.enqueue_autofix_job(_job())
    assert created_one is True
    assert created_two is False
    assert first.id == second.id
    assert store.count_autofix_jobs({}) == 1


def test_a_claim_is_exclusive(store: IndexStore) -> None:
    store.enqueue_autofix_job(_job())
    first = store.claim_autofix_job(worker="w1", lease_seconds=60)
    second = store.claim_autofix_job(worker="w2", lease_seconds=60)
    assert first is not None
    assert first.lease_owner == "w1"
    assert first.state == "running"
    assert second is None


def test_a_crashed_worker_releases_its_job(store: IndexStore) -> None:
    """The whole point of a lease: nothing has to notice the crash."""
    store.enqueue_autofix_job(_job(max_attempts=5))
    leased = store.claim_autofix_job(worker="doomed", lease_seconds=60)
    assert leased is not None

    # The worker dies. No handler runs, no cleanup happens; the deadline simply
    # passes.
    store._autofix_exec(
        "UPDATE autofix_jobs SET lease_expires_at = ? WHERE job_key = ?",
        (time.time() - 1, leased.job_key),
    )

    reclaimed = store.claim_autofix_job(worker="survivor", lease_seconds=60)
    assert reclaimed is not None
    assert reclaimed.lease_owner == "survivor"
    assert reclaimed.attempts == 2  # the crashed attempt still counts


def test_reaping_shows_an_abandoned_job_as_waiting(store: IndexStore) -> None:
    store.enqueue_autofix_job(_job(max_attempts=5))
    leased = store.claim_autofix_job(worker="doomed", lease_seconds=60)
    store._autofix_exec(
        "UPDATE autofix_jobs SET lease_expires_at = ? WHERE job_key = ?",
        (time.time() - 1, leased.job_key),
    )
    assert store.reap_expired_autofix_leases() == 1
    assert store.get_autofix_job(leased.job_key).state == "queued"
    assert store.get_autofix_job(leased.job_key).lease_owner == ""


def test_a_live_lease_is_not_reaped(store: IndexStore) -> None:
    store.enqueue_autofix_job(_job())
    store.claim_autofix_job(worker="busy", lease_seconds=600)
    assert store.reap_expired_autofix_leases() == 0


def test_renewing_needs_the_lease_you_hold(store: IndexStore) -> None:
    store.enqueue_autofix_job(_job())
    leased = store.claim_autofix_job(worker="w1", lease_seconds=5)
    assert store.renew_autofix_lease(leased.job_key, worker="w1", lease_seconds=60) is True
    assert store.renew_autofix_lease(leased.job_key, worker="w2", lease_seconds=60) is False


def test_releasing_does_not_consume_an_attempt(store: IndexStore) -> None:
    store.enqueue_autofix_job(_job())
    leased = store.claim_autofix_job(worker="w1", lease_seconds=60)
    store.release_autofix_lease(leased.job_key, worker="w1")
    again = store.get_autofix_job(leased.job_key)
    assert again.state == "queued"
    assert again.attempts == 1
    assert store.claim_autofix_job(worker="w2", lease_seconds=60) is not None


def test_attempts_run_out_and_the_job_stops_being_claimable(store: IndexStore) -> None:
    store.enqueue_autofix_job(_job(max_attempts=2))
    key = _job().job_key
    for _ in range(2):
        leased = store.claim_autofix_job(worker="w1", lease_seconds=60)
        assert leased is not None
        store.update_autofix_job(key, state="failed", available_at=0, clear_lease=True)
    assert store.claim_autofix_job(worker="w1", lease_seconds=60) is None


def test_dead_lettering_parks_the_job_with_its_reason(store: IndexStore) -> None:
    store.enqueue_autofix_job(_job())
    key = _job().job_key
    parked = store.dead_letter_autofix_job(
        key,
        reasons=[Reason(ReasonCode.ATTEMPT_LIMIT, "gave up after 2 attempts")],
        error="gave up",
    )
    assert parked.state == "dead_letter"
    assert parked.reason_codes() == [ReasonCode.ATTEMPT_LIMIT]
    assert parked.terminal
    assert store.claim_autofix_job(worker="w1", lease_seconds=60) is None


def test_cancelling_a_running_job_takes_its_lease_away(store: IndexStore) -> None:
    store.enqueue_autofix_job(_job())
    leased = store.claim_autofix_job(worker="w1", lease_seconds=600)
    cancelled = store.cancel_autofix_job(leased.job_key, actor="root", reason="incident")
    assert cancelled.state == "cancelled"
    assert cancelled.cancelled_by == "root"
    # The worker discovers it on its next heartbeat, and cannot renew.
    assert store.renew_autofix_lease(leased.job_key, worker="w1", lease_seconds=60) is False


def test_cancelling_never_rewrites_a_finished_job(store: IndexStore) -> None:
    """A cancellation racing a worker's last write must not lose the result."""
    store.enqueue_autofix_job(_job())
    key = _job().job_key
    store.update_autofix_job(key, state="opened", child_pr_url="https://example/1")
    cancelled = store.cancel_autofix_job(key, actor="root", reason="too late")
    assert cancelled.state == "opened"
    assert cancelled.child_pr_url == "https://example/1"


def test_a_cancelled_job_is_never_claimed_again(store: IndexStore) -> None:
    store.enqueue_autofix_job(_job())
    store.cancel_autofix_job(_job().job_key, actor="root", reason="stop")
    assert store.claim_autofix_job(worker="w1", lease_seconds=60) is None


def test_the_active_count_backs_the_concurrency_ceiling(store: IndexStore) -> None:
    store.enqueue_autofix_job(_job(finding="f1", head="a"))
    store.enqueue_autofix_job(_job(finding="f2", head="a"))
    assert store.count_active_autofix_jobs(owner="acme", repo="app") == 2
    store.update_autofix_job(_job(finding="f1", head="a").job_key, state="opened")
    assert store.count_active_autofix_jobs(owner="acme", repo="app") == 1


def test_the_ceiling_is_enforced_by_the_insert_not_by_the_caller(store: IndexStore) -> None:
    """Count-then-insert leaves a window several `await` points wide.

    Two `fix all` requests arriving together would each read the same free
    capacity and each fill it, so the ceiling travels with the statement that
    fills it rather than with a number somebody read earlier.
    """
    first, created = store.enqueue_autofix_job(_job(finding="f1", head="a"), max_active=2)
    assert created and first.id
    second, created = store.enqueue_autofix_job(_job(finding="f2", head="a"), max_active=2)
    assert created and second.id

    third, created = store.enqueue_autofix_job(_job(finding="f3", head="a"), max_active=2)
    assert created is False
    # `id == 0` is the "no room" signal: nothing was persisted, as opposed to a
    # duplicate key where the existing row comes back.
    assert third.id == 0
    assert store.get_autofix_job(third.job_key) is None
    assert store.count_active_autofix_jobs(owner="acme", repo="app") == 2


def test_room_frees_up_when_a_job_finishes(store: IndexStore) -> None:
    store.enqueue_autofix_job(_job(finding="f1", head="a"), max_active=1)
    _blocked, created = store.enqueue_autofix_job(_job(finding="f2", head="a"), max_active=1)
    assert created is False
    store.update_autofix_job(_job(finding="f1", head="a").job_key, state="opened")
    stored, created = store.enqueue_autofix_job(_job(finding="f2", head="a"), max_active=1)
    assert created and stored.id


def test_a_duplicate_key_still_returns_the_existing_row_under_a_ceiling(
    store: IndexStore,
) -> None:
    """ "Already queued" and "no room" are different answers to the same call."""
    store.enqueue_autofix_job(_job(finding="f1", head="a"), max_active=5)
    stored, created = store.enqueue_autofix_job(_job(finding="f1", head="a"), max_active=5)
    assert created is False
    assert stored.id and stored.job_key == _job(finding="f1", head="a").job_key


def test_zero_means_no_ceiling(store: IndexStore) -> None:
    for index in range(4):
        _stored, created = store.enqueue_autofix_job(_job(finding=f"f{index}", head="a"))
        assert created
    assert store.count_active_autofix_jobs(owner="acme", repo="app") == 4


def test_a_ci_retry_grants_the_attempt_it_needs(store: IndexStore) -> None:
    """Requeuing without raising the ceiling would strand the job forever."""
    store.enqueue_autofix_job(_job(max_attempts=1))
    key = _job().job_key
    store.claim_autofix_job(worker="w1", lease_seconds=60)
    store.update_autofix_job(key, state="opened", clear_lease=True)
    store.update_autofix_job(
        key, state="queued", bump_ci_attempts=True, extra_attempts=1, available_at=0
    )
    again = store.claim_autofix_job(worker="w1", lease_seconds=60)
    assert again is not None
    assert again.ci_attempts == 1
    assert again.attempts == 2  # the earlier attempt is still on the record


def test_attempts_are_appended_not_replaced(store: IndexStore) -> None:
    store.enqueue_autofix_job(_job())
    key = _job().job_key
    for index, phase in enumerate(("generate", "apply", "validate")):
        store.record_autofix_attempt(
            AutofixAttempt(job_key=key, attempt=1, phase=phase, outcome="ok", created_at=index + 1)
        )
    rows = store.list_autofix_attempts(job_key=key)
    assert [row.phase for row in rows] == ["generate", "apply", "validate"]


def test_findings_are_listed_open_first_and_never_closed_ones(store: IndexStore) -> None:
    from mira.feedback.models import ReviewFinding

    for index, state in enumerate(("open", "resolved", "outdated")):
        store.save_review_finding(
            ReviewFinding(
                id=f"f{index}",
                fingerprint=f"fp{index}",
                review_id=1,
                platform="github",
                owner="acme",
                repo="app",
                pr_number=7,
                pr_url="u",
                base_sha="b",
                head_sha="h",
                path="a.py",
                start_line=1,
                end_line=1,
                symbol="",
                category="bug",
                severity="warning",
                confidence=0.8,
                title=f"F{index}",
                body="",
                suggestion="",
                detector="llm",
                prompt_model="m",
                state=state,
            )
        )
    ids = {finding.id for finding in store.list_review_findings(pr_number=7)}
    # `outdated` counts as open: it only means the diff moved past the line.
    assert ids == {"f0", "f2"}


# ── the Postgres backend, and parity ─────────────────────────────────────────


class _FakeCursor:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self._cur = conn.cursor()

    def execute(self, sql, params=()):  # noqa: ANN001
        # SQLite has neither `FOR UPDATE` nor `SKIP LOCKED`; dropping them is
        # exactly what the SQLite backend does for real, so the stand-in stays
        # honest about what it is testing.
        sqlite_sql = (
            sql.replace("%s", "?").replace(" FOR UPDATE SKIP LOCKED", "").replace(" FOR UPDATE", "")
        )
        self._cur.execute(sqlite_sql, params)
        return self

    def fetchone(self):
        return self._cur.fetchone()

    def fetchall(self):
        return self._cur.fetchall()

    @property
    def rowcount(self) -> int:
        return self._cur.rowcount

    def close(self) -> None:
        self._cur.close()

    def __enter__(self):
        return self

    def __exit__(self, *args) -> None:
        self.close()


class _FakeConn:
    def __init__(self) -> None:
        self._conn = sqlite3.connect(":memory:")
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

    def commit(self) -> None:
        self._conn.commit()

    def rollback(self) -> None:
        self._conn.rollback()

    def close(self) -> None:
        pass


@pytest.fixture
def pg(monkeypatch: pytest.MonkeyPatch) -> PgIndexStore:
    conn = _FakeConn()
    monkeypatch.setattr(pg_store, "_get_conn", lambda url: conn)
    monkeypatch.setattr(pg_store, "_new_pg_conn", lambda url: conn)
    return PgIndexStore("acme", "app", "postgresql://fake")


@pytest.mark.parametrize("backend", ["sqlite", "postgres"])
def test_the_queue_behaves_identically_on_both_backends(
    backend: str, store: IndexStore, pg: PgIndexStore
) -> None:
    """One script, run against both stores, asserting the same outcomes."""
    handle = store if backend == "sqlite" else pg

    stored, created = handle.enqueue_autofix_job(_job(max_attempts=2))
    assert created is True
    assert stored.state == "queued"

    _again, created_again = handle.enqueue_autofix_job(_job(max_attempts=2))
    assert created_again is False

    # The conditional insert is hand-written SQL that differs per backend, so
    # it is exercised on both rather than only on the one the tests default to.
    blocked, created_blocked = handle.enqueue_autofix_job(
        _job(finding="ceiling", max_attempts=2), max_active=1
    )
    assert created_blocked is False and blocked.id == 0
    admitted, created_admitted = handle.enqueue_autofix_job(
        _job(finding="ceiling", max_attempts=2), max_active=5
    )
    assert created_admitted is True and admitted.id
    handle.update_autofix_job(admitted.job_key, state="opened")

    leased = handle.claim_autofix_job(worker="w1", lease_seconds=60)
    assert leased is not None and leased.lease_owner == "w1" and leased.attempts == 1
    assert handle.claim_autofix_job(worker="w2", lease_seconds=60) is None

    assert handle.renew_autofix_lease(leased.job_key, worker="w1", lease_seconds=60) is True
    assert handle.renew_autofix_lease(leased.job_key, worker="w2", lease_seconds=60) is False

    handle._autofix_exec(
        handle._ph("UPDATE autofix_jobs SET lease_expires_at = ? WHERE job_key = ?"),
        (time.time() - 1, leased.job_key),
    )
    reclaimed = handle.claim_autofix_job(worker="w2", lease_seconds=60)
    assert reclaimed is not None and reclaimed.attempts == 2

    handle.record_autofix_attempt(
        AutofixAttempt(job_key=leased.job_key, attempt=2, phase="validate", outcome="failed")
    )
    assert len(handle.list_autofix_attempts(job_key=leased.job_key)) == 1

    parked = handle.dead_letter_autofix_job(
        leased.job_key, reasons=[Reason(ReasonCode.ATTEMPT_LIMIT, "out of tries")], error="no"
    )
    assert parked.state == "dead_letter"
    assert parked.reason_codes() == [ReasonCode.ATTEMPT_LIMIT]
    assert handle.claim_autofix_job(worker="w3", lease_seconds=60) is None

    assert handle.count_autofix_jobs({"state": "dead_letter"}) == 1
    summary = handle.summarize_autofix_jobs({})
    assert summary and summary[0]["state"] == "dead_letter"


@pytest.mark.parametrize("backend", ["sqlite", "postgres"])
def test_a_stored_job_round_trips_identically(
    backend: str, store: IndexStore, pg: PgIndexStore
) -> None:
    handle = store if backend == "sqlite" else pg
    handle.enqueue_autofix_job(_job())
    key = _job().job_key
    handle.update_autofix_job(
        key,
        state="opened",
        branch_name="mira/fix/pr-7/abc",
        commit_sha="deadbeef",
        child_pr_url="https://example/900",
        child_pr_number=900,
        model="test-model",
        patch_digest="digest",
        diff="diff --git a/x b/x\n",
        validation=ValidationResult(checks=[], executed=True),
        reasons=[Reason(ReasonCode.PR_OPENED, "opened", "info")],
    )
    got = handle.get_autofix_job(key)
    assert got.state == "opened"
    assert got.branch_name == "mira/fix/pr-7/abc"
    assert got.child_pr_number == 900
    assert got.validation.executed is True
    assert got.reason_codes() == [ReasonCode.PR_OPENED]
    assert got.as_dict()["terminal"] is True


def test_postgres_scopes_reads_to_one_repository(pg: PgIndexStore, monkeypatch) -> None:
    """One table for the install means an unscoped read is a data leak."""
    pg.enqueue_autofix_job(_job())
    other = PgIndexStore("other", "repo", "postgresql://fake")
    assert other.count_autofix_jobs({}) == 0
    assert other.get_autofix_job(_job().job_key) is None
    # …and the org-wide handle deliberately sees everything.
    org = PgIndexStore("", "", "postgresql://fake")
    assert org.count_autofix_jobs({}) == 1


def test_postgres_scoping_keeps_a_worker_off_another_repos_queue(pg: PgIndexStore) -> None:
    pg.enqueue_autofix_job(_job())
    other = PgIndexStore("other", "repo", "postgresql://fake")
    assert other.claim_autofix_job(worker="w1", lease_seconds=60) is None
    assert pg.claim_autofix_job(worker="w1", lease_seconds=60) is not None


# ── upgrade and rollback ─────────────────────────────────────────────────────


def _db_path(owner: str, repo: str) -> str:
    handle = IndexStore.open(owner, repo)
    try:
        return handle._db_path
    finally:
        handle.close()


def test_an_existing_database_picks_the_tables_up_with_no_migration_step() -> None:
    """What the ARM64 job checks at the container level, checked here at the
    schema level: a database created before Phase 5 opens and works."""
    path = _db_path("acme", "legacy")

    # Drop the Phase 5 tables, to stand in for a database written by older code.
    conn = sqlite3.connect(path)
    try:
        conn.executescript("DROP TABLE autofix_jobs; DROP TABLE autofix_attempts;")
        conn.commit()
    finally:
        conn.close()

    reopened = IndexStore.open("acme", "legacy")
    try:
        stored, created = reopened.enqueue_autofix_job(_job())
        assert created is True
        assert stored.state == "queued"
    finally:
        reopened.close()


def test_older_code_can_still_read_a_database_this_version_wrote() -> None:
    """Rollback: the tables are additive, so a build that does not know them
    ignores them and everything it *does* know still answers."""
    handle = IndexStore.open("acme", "app")
    try:
        handle.enqueue_autofix_job(_job())
    finally:
        handle.close()

    conn = sqlite3.connect(_db_path("acme", "app"))
    try:
        names = {
            row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        assert {"review_findings", "gate_decisions", "files"} <= names
        assert conn.execute("SELECT COUNT(*) FROM review_findings").fetchone()[0] == 0
    finally:
        conn.close()


# ── the namespaced-owner trap ────────────────────────────────────────────────


@pytest.fixture
def pg_gitlab(monkeypatch: pytest.MonkeyPatch) -> PgIndexStore:
    """A Postgres store opened the way `IndexStore.open` opens a GitLab repo.

    Non-GitHub owners are namespaced as ``_{platform}/{owner}``, and *that*
    spelling is what the store scopes its reads on — while a job carries the
    plain one.
    """
    conn = _FakeConn()
    monkeypatch.setattr(pg_store, "_get_conn", lambda url: conn)
    monkeypatch.setattr(pg_store, "_new_pg_conn", lambda url: conn)
    return PgIndexStore("_gitlab/acme", "app", "postgresql://fake")


def test_a_gitlab_job_is_visible_to_the_store_that_wrote_it(pg_gitlab: PgIndexStore) -> None:
    """The store's owner has to win over the job's, or the row is invisible.

    Written with the plain owner and read back through the namespaced scope,
    the job would be enqueued and then never found — no idempotency, and no
    worker would ever claim it. On Postgres that means autofix silently does
    nothing on every GitLab and Forgejo repository.
    """
    job = _job()
    job.platform = "gitlab"
    job.owner = "acme"  # plain, as `_build_job` sets it from the pull request

    stored, created = pg_gitlab.enqueue_autofix_job(job)
    assert created is True
    assert pg_gitlab.get_autofix_job(job.job_key) is not None

    _again, created_again = pg_gitlab.enqueue_autofix_job(job)
    assert created_again is False  # idempotent, not a second row
    assert pg_gitlab.count_autofix_jobs({}) == 1

    claimed = pg_gitlab.claim_autofix_job(worker="w1", lease_seconds=60)
    assert claimed is not None
    assert claimed.job_key == job.job_key


def test_a_namespaced_store_still_cannot_see_another_repo(
    pg_gitlab: PgIndexStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    job = _job()
    job.platform = "gitlab"
    job.owner = "acme"
    pg_gitlab.enqueue_autofix_job(job)

    other = PgIndexStore("_gitlab/other", "app", "postgresql://fake")
    assert other.get_autofix_job(job.job_key) is None
    assert other.claim_autofix_job(worker="w2", lease_seconds=60) is None


def test_the_gate_stores_a_gitlab_decision_the_same_way(pg_gitlab: PgIndexStore) -> None:
    """The same trap, in the Phase 4 tables. Fixed in both mixins together."""
    from mira.gate.models import GateDecision, GateInputs

    decision = GateDecision(
        decision_key="d1",
        inputs=GateInputs(platform="gitlab", owner="acme", repo="app", pr_number=7),
    )
    stored, created = pg_gitlab.record_gate_decision(decision)
    assert created is True
    assert pg_gitlab.get_gate_decision("d1") is not None
    _again, created_again = pg_gitlab.record_gate_decision(decision)
    assert created_again is False


def test_cancelled_is_final_in_sql_not_by_convention(store: IndexStore) -> None:
    """A worker that was already running will still try to record its progress,
    and a read-then-write in the worker cannot win that race. An update that
    matches no row can."""
    store.enqueue_autofix_job(_job())
    key = _job().job_key
    store.claim_autofix_job(worker="w1", lease_seconds=600)
    store.cancel_autofix_job(key, actor="root", reason="stop")

    for state in ("validating", "publishing", "opened", "failed", "queued"):
        store.update_autofix_job(key, state=state, branch_name="mira/fix/pr-7/abc")
        assert store.get_autofix_job(key).state == "cancelled"
    assert store.get_autofix_job(key).branch_name == ""

    # Dead-lettering cannot overwrite it either.
    store.dead_letter_autofix_job(key, reasons=[], error="gave up")
    assert store.get_autofix_job(key).state == "cancelled"


def test_a_ci_retry_still_moves_an_opened_job(store: IndexStore) -> None:
    """The invariant is `cancelled`, not "terminal": a published fix whose CI
    went red is deliberately moved back to the queue."""
    store.enqueue_autofix_job(_job(max_attempts=1))
    key = _job().job_key
    store.update_autofix_job(key, state="opened")
    store.update_autofix_job(key, state="queued", extra_attempts=1, available_at=0)
    assert store.get_autofix_job(key).state == "queued"
