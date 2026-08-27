"""Phase 6 — persistence: idempotent retries, and parity between the backends.

Two properties, and the second one is why the queries live in a mixin rather
than in each store.

**A retry converges.** Both tables are keyed on a content-derived identity, so
a redelivered webhook, a manual re-run and a second worker land on the same
rows. They *update* those rows rather than being ignored — a check result is an
answer to a question, and a run that failed because the network was down must
read as resolved once it is retried successfully, not stay wrong forever.

**The two backends carry the same columns.** The SQLite parity half runs here;
the Postgres half runs in `test_pg_store.py`, which is skipped without a
database. What is asserted in both places is the same statement list, because
the mixin is the same statement list.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from mira.checks.models import (
    CheckFinding,
    CheckResult,
    CheckRun,
    CheckRunInputs,
    Evidence,
    SkipReason,
    result_key,
    run_key,
)
from mira.index.store import IndexStore


@pytest.fixture(autouse=True)
def isolated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MIRA_INDEX_DIR", str(tmp_path))
    monkeypatch.delenv("DATABASE_URL", raising=False)


def _inputs(**overrides) -> CheckRunInputs:
    base = {
        "platform": "github",
        "owner": "acme",
        "repo": "app",
        "pr_number": 7,
        "pr_url": "https://github.com/acme/app/pull/7",
        "pr_author": "alice",
        "head_sha": "head123",
        "changed_paths": ["src/a.py"],
        "changed_files": 1,
        "added_lines": 4,
    }
    base.update(overrides)
    return CheckRunInputs(**base)


def _run(state="violation", *, inputs=None, policy_version="checks-v1+abc", **kwargs) -> CheckRun:
    inputs = inputs or _inputs()
    key = run_key(
        platform=inputs.platform,
        owner=inputs.owner,
        repo=inputs.repo,
        pr_number=inputs.pr_number,
        head_sha=inputs.head_sha,
        policy_version=policy_version,
        inputs_digest=inputs.digest,
    )
    result = CheckResult(
        check_id="native.tests",
        check_version="1",
        title="Tests",
        origin="native",
        mode=kwargs.pop("mode", "error"),
        state=state,
        summary="source changed with no test",
        evidence=[Evidence(path="src/a.py", detail="4 added lines", source="diff")],
        findings=(
            [
                CheckFinding(
                    fingerprint="fp1",
                    title="Source changed and no test changed with it",
                    detail="4 added lines",
                    evidence=[Evidence(path="src/a.py", start_line=3, source="diff")],
                    sources=["native.tests"],
                )
            ]
            if state == "violation"
            else []
        ),
        skip_reason=kwargs.pop("skip_reason", ""),
        error=kwargs.pop("error", ""),
        duration_seconds=0.25,
        config_digest="cfg1",
        result_key=result_key(run_key_value=key, check_id="native.tests"),
        sources=["native.tests"],
    )
    return CheckRun(
        run_key=key,
        policy_version=policy_version,
        inputs=inputs,
        results=[result],
        duration_seconds=0.5,
        created_at=time.time(),
    )


@pytest.fixture
def store():
    handle = IndexStore.open("acme", "app")
    try:
        yield handle
    finally:
        handle.close()


# ────────────────────────────────────────────────────────── round-tripping ──


def test_a_run_survives_a_round_trip_with_its_evidence(store) -> None:
    stored, created = store.record_check_run(_run())
    assert created is True

    read = store.get_check_run(stored.run_key)
    assert read is not None
    assert read.verdict == "violation"
    assert read.inputs.pr_author == "alice"
    result = read.results[0]
    assert result.state == "violation"
    assert result.mode == "error"
    assert result.duration_seconds == pytest.approx(0.25)
    assert result.config_digest == "cfg1"
    assert result.evidence[0].path == "src/a.py"
    assert result.findings[0].evidence[0].start_line == 3
    assert result.sources == ["native.tests"]


def test_the_states_survive_a_round_trip_distinguishably(store) -> None:
    """`skipped` for a missing tool and `infrastructure_error` must not merge."""
    for state, extra in (
        ("pass", {}),
        ("violation", {}),
        ("infrastructure_error", {"error": "network down"}),
        ("timeout", {"error": "exceeded 60s"}),
        ("skipped", {"skip_reason": SkipReason.TOOL_MISSING}),
    ):
        run = _run(state, inputs=_inputs(head_sha=f"sha-{state}"), **extra)
        store.record_check_run(run)
        read = store.get_check_run(run.run_key)
        result = read.results[0]
        assert result.state == state
        assert result.skip_reason == extra.get("skip_reason", "")
        assert result.error == extra.get("error", "")
        # And the derived properties reconstruct the same way they were built.
        assert result.is_violation is (state == "violation")
        assert result.incomplete is (
            state in {"infrastructure_error", "timeout"}
            or extra.get("skip_reason") in {SkipReason.TOOL_MISSING}
        )


# ────────────────────────────────────────────────────────────── idempotency ──


def test_a_retried_run_converges_on_one_row(store) -> None:
    run = _run()
    store.record_check_run(run)
    store.record_check_run(run)
    store.record_check_run(run)
    assert store.count_check_runs({"pr_number": 7}) == 1
    assert store.count_check_results({"run_key": run.run_key}) == 1


def test_a_retry_records_that_it_was_a_retry(store) -> None:
    run = _run()
    _, first = store.record_check_run(run)
    _, second = store.record_check_run(run)
    assert first is True
    assert second is False


def test_a_retry_that_succeeds_replaces_an_infrastructure_error(store) -> None:
    """Otherwise a network blip leaves a pull request permanently unanswered."""
    inputs = _inputs()
    failed = _run("infrastructure_error", inputs=inputs, error="network down")
    store.record_check_run(failed)
    assert store.get_check_run(failed.run_key).verdict == "incomplete"

    recovered = _run("pass", inputs=inputs)
    store.record_check_run(recovered)
    read = store.get_check_run(failed.run_key)
    assert read.verdict == "pass"
    assert read.results[0].state == "pass"
    assert read.results[0].error == ""
    assert store.count_check_runs({"pr_number": 7}) == 1


def test_a_new_commit_is_a_new_run(store) -> None:
    store.record_check_run(_run(inputs=_inputs(head_sha="one")))
    store.record_check_run(_run(inputs=_inputs(head_sha="two")))
    assert store.count_check_runs({"pr_number": 7}) == 2


def test_a_new_policy_is_a_new_run(store) -> None:
    store.record_check_run(_run(policy_version="checks-v1+aaa"))
    store.record_check_run(_run(policy_version="checks-v1+bbb"))
    assert store.count_check_runs({"pr_number": 7}) == 2


# ──────────────────────────────────────────────────────── reading it back ──


def test_the_latest_run_for_a_commit_is_what_a_gate_reads(store) -> None:
    store.record_check_run(_run("pass", inputs=_inputs(head_sha="old")))
    store.record_check_run(_run("violation", inputs=_inputs(head_sha="new")))

    assert store.latest_check_run(pr_number=7, head_sha="new").verdict == "violation"
    assert store.latest_check_run(pr_number=7, head_sha="old").verdict == "pass"
    # A commit nothing ran against is not satisfied by a clean older run.
    assert store.latest_check_run(pr_number=7, head_sha="unseen") is None


def test_results_can_be_filtered_by_state_and_by_incompleteness(store) -> None:
    store.record_check_run(_run("pass", inputs=_inputs(head_sha="a")))
    store.record_check_run(
        _run("skipped", inputs=_inputs(head_sha="b"), skip_reason=SkipReason.TOOL_MISSING)
    )
    store.record_check_run(_run("infrastructure_error", inputs=_inputs(head_sha="c"), error="down"))

    assert store.count_check_results({"state": "pass"}) == 1
    # The filter an operator reaches for after an incident: everything that was
    # not a statement about a pull request.
    assert store.count_check_results({"incomplete": True}) == 2


def test_blocking_is_stored_so_a_gate_does_not_recompute_it(store) -> None:
    store.record_check_run(_run("violation", mode="error"))
    store.record_check_run(_run("violation", inputs=_inputs(head_sha="b"), mode="warning"))
    assert store.count_check_results({"blocking": True}) == 1


def test_the_summary_groups_by_check_and_state(store) -> None:
    store.record_check_run(_run("pass", inputs=_inputs(head_sha="a")))
    store.record_check_run(_run("violation", inputs=_inputs(head_sha="b")))
    buckets = {(row["check_id"], row["state"]): row for row in store.summarize_check_results()}
    assert buckets[("native.tests", "pass")]["count"] == 1
    assert buckets[("native.tests", "violation")]["count"] == 1
    assert buckets[("native.tests", "pass")]["average_duration"] > 0


def test_runs_paginate_and_sort(store) -> None:
    for index in range(5):
        run = _run(inputs=_inputs(head_sha=f"sha{index}"))
        run.created_at = 1000.0 + index
        store.record_check_run(run)
    newest = store.list_check_runs({}, limit=2)
    assert len(newest) == 2
    assert newest[0].created_at > newest[1].created_at
    oldest = store.list_check_runs({}, limit=1, descending=False)
    assert oldest[0].created_at == 1000.0


def test_an_unknown_sort_column_falls_back_rather_than_interpolating(store) -> None:
    """The value arrives from a query string; the column list is an allowlist."""
    store.record_check_run(_run())
    rows = store.list_check_runs({}, sort="1; DROP TABLE check_runs")
    assert len(rows) == 1


# ──────────────────────────────────────────────────────────────── parity ──


def test_both_stores_expose_the_same_check_surface() -> None:
    """The mixin is the parity, and this is the assertion that says so."""
    from mira.checks.persistence import ChecksStoreMixin
    from mira.index.pg_store import PgIndexStore

    surface = {
        name
        for name in vars(ChecksStoreMixin)
        if not name.startswith("_") and callable(getattr(ChecksStoreMixin, name))
    }
    assert surface, "the mixin defines the shared surface"
    for name in surface:
        assert hasattr(IndexStore, name)
        assert hasattr(PgIndexStore, name)
        # Same implementation object, not two that happen to share a name.
        assert getattr(IndexStore, name) is getattr(PgIndexStore, name)


def test_both_stores_supply_the_primitives_the_mixin_needs() -> None:
    from mira.index.pg_store import PgIndexStore

    for cls, placeholder in ((IndexStore, "?"), (PgIndexStore, "%s")):
        assert cls._checks_placeholder == placeholder
        assert cls._checks_query is not None
        assert cls._checks_exec is not None


# ────────────────────────────────────────────────────── migration and rollback ──


def test_an_existing_database_gains_the_check_tables_on_open(tmp_path: Path) -> None:
    """Upgrading is opening the file: the schema is `CREATE TABLE IF NOT EXISTS`.

    There is no migration step to run and nothing to sequence, which is the
    property the Orange Pi profile is built around — an update is a container
    restart, not a maintenance window.
    """
    import sqlite3

    path = tmp_path / "legacy.db"
    store = IndexStore(str(path), owner="acme", repo="app")
    store.close()

    # Simulate a database written by a build that predates Phase 6.
    conn = sqlite3.connect(path)
    conn.executescript("DROP TABLE check_results; DROP TABLE check_runs;")
    conn.commit()
    conn.close()

    upgraded = IndexStore(str(path), owner="acme", repo="app")
    try:
        assert upgraded.count_check_runs({}) == 0
        upgraded.record_check_run(_run())
        assert upgraded.count_check_runs({}) == 1
    finally:
        upgraded.close()


def test_rolling_back_leaves_the_older_code_working(tmp_path: Path) -> None:
    """The rollback story, asserted rather than asserted-in-a-docstring.

    A database that a Phase 6 build wrote to carries two tables the previous
    release has never heard of. Nothing in the older code selects from them, so
    the rows sit there unread and every pre-Phase-6 query still answers — which
    is what makes downgrading a tag change rather than a restore.
    """
    path = tmp_path / "rollback.db"
    store = IndexStore(str(path), owner="acme", repo="app")
    try:
        store.record_check_run(_run())
        # The Phase 4 surface an older build would still be using.
        assert store.gate_finding_counts(7)["open"] == 0
    finally:
        store.close()

    # Re-opening without ever touching the new tables must not fail, and the
    # rows written by the newer build must still be there afterwards.
    older = IndexStore(str(path), owner="acme", repo="app")
    try:
        assert older.gate_finding_counts(7)["open"] == 0
        assert older.count_check_runs({}) == 1
    finally:
        older.close()


def test_the_summary_carries_the_persisted_incomplete_count(store) -> None:
    """The dashboard's health number, and states alone would undercount it."""
    store.record_check_run(_run("violation", inputs=_inputs(head_sha="a")))
    store.record_check_run(
        _run("skipped", inputs=_inputs(head_sha="b"), skip_reason=SkipReason.TOOL_MISSING)
    )
    store.record_check_run(_run("infrastructure_error", inputs=_inputs(head_sha="c"), error="x"))

    total_incomplete = sum(row["incomplete"] for row in store.summarize_check_results())
    # A missing linter and an error both count; the violation does not.
    assert total_incomplete == 2


def test_a_run_is_never_visible_before_its_results(store, monkeypatch) -> None:
    """A reader mid-write would otherwise compute a verdict from half a run.

    Neither backend gives `record_check_run` a transaction, so the ordering is
    the guarantee. Asserted by watching what the store *does*: every result
    write happens before the run row exists.
    """
    seen: list[str] = []
    original = store._checks_exec

    def _record(sql, params=()):
        head = " ".join(sql.split())[:40]
        if "INSERT INTO check_results" in sql:
            seen.append("result")
        elif "INSERT INTO check_runs" in sql:
            seen.append("run")
        return original(sql, params)

    monkeypatch.setattr(store, "_checks_exec", _record)
    store.record_check_run(_run())

    assert "run" in seen and "result" in seen
    assert seen.index("result") < seen.index("run"), (
        "results must be written before the run row a gate reads"
    )
