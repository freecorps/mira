"""Phase 7C — persistence: one run per question, and history that cannot double-count.

Two failure modes this guards against. A retried run that stacks a second row
would make "how often is Dana suggested" a measure of webhook redelivery. And a
contribution recorded twice — once from a merge Mira watched, once from the
commit history it later fetched — would put whoever pushes most at the top of
every ranking.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from mira.index.store import IndexStore
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

NOW = 1_800_000_000.0


@pytest.fixture(autouse=True)
def isolated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MIRA_INDEX_DIR", str(tmp_path))
    monkeypatch.delenv("DATABASE_URL", raising=False)


@pytest.fixture
def store() -> IndexStore:
    handle = IndexStore.open("acme", "app")
    yield handle
    handle.close()


def _candidate(identity: str, score: float = 3.0) -> ReviewerCandidate:
    return ReviewerCandidate(
        identity=identity,
        score=score,
        contributions=[
            SignalContribution(
                kind="codeowners",
                raw=1,
                weight=3.0,
                score=score,
                evidence=[Evidence(path="src/app.py", line=2, source="codeowners")],
            )
        ],
    )


def _run(*, candidates: list[ReviewerCandidate] | None = None, **overrides: object) -> TriageRun:
    inputs = TriageInputs(
        platform="github",
        owner="acme",
        repo="app",
        pr_number=7,
        pr_url="https://github.com/acme/app/pull/7",
        pr_author="kit",
        base_sha="base111",
        head_sha="head222",
        changed_paths=["src/app.py"],
    )
    return TriageRun(
        run_key=run_key(
            platform="github",
            owner="acme",
            repo="app",
            pr_number=7,
            head_sha=str(overrides.get("head_sha", "head222")),
            policy_version="triage-v1+abc",
            inputs_digest=inputs.digest,
        ),
        policy_version="triage-v1+abc",
        inputs=inputs,
        classification=Classification(size="s", changed_files=1, changed_lines=2, kinds=["code"]),
        candidates=candidates if candidates is not None else [_candidate("dana")],
        signals=[SignalReport(kind="codeowners", status="available", candidates=1)],
        excluded=[Exclusion(identity="kit", reason="author")],
        notes=["review load could not be read"],
    )


def test_a_run_survives_the_round_trip_intact(store: IndexStore) -> None:
    stored, created = store.record_triage_run(_run())
    assert created is True
    assert stored.status == "ok"
    assert stored.suggested == ["dana"]
    assert stored.classification.size == "s"
    assert stored.excluded[0].reason == "author"
    assert stored.notes == ["review load could not be read"]
    assert stored.candidates[0].contributions[0].evidence[0].line == 2


def test_the_same_question_asked_twice_is_one_row(store: IndexStore) -> None:
    store.record_triage_run(_run())
    stored, created = store.record_triage_run(_run())
    assert created is False
    assert stored.attempts == 2
    assert store.count_triage_runs({}) == 1


def test_a_rerun_that_finds_somebody_else_does_not_keep_the_old_name(
    store: IndexStore,
) -> None:
    """A stale candidate row would be counted as a suggestion this run never made."""
    store.record_triage_run(_run(candidates=[_candidate("dana"), _candidate("sam", 2.0)]))
    store.record_triage_run(_run(candidates=[_candidate("dana")]))
    summary = store.summarize_triage_candidates({})
    assert [row["identity"] for row in summary] == ["dana"]


def test_a_run_can_be_found_by_the_person_it_named(store: IndexStore) -> None:
    store.record_triage_run(_run())
    assert store.count_triage_runs({"identity": "dana"}) == 1
    # Through the candidate table, not a substring of the JSON blob: being told
    # you were suggested when you were not is how a feature gets switched off.
    assert store.count_triage_runs({"identity": "dan"}) == 0


def test_the_newest_run_for_a_commit_is_the_one_that_answers(store: IndexStore) -> None:
    store.record_triage_run(_run())
    assert store.latest_triage_run(pr_number=7, head_sha="head222") is not None
    # A suggestion computed against an older commit is not evidence about this
    # one: it was computed from a different set of files.
    assert store.latest_triage_run(pr_number=7, head_sha="other") is None


def test_filters_narrow_by_status_and_time(store: IndexStore) -> None:
    store.record_triage_run(_run())
    assert store.count_triage_runs({"status": "ok"}) == 1
    assert store.count_triage_runs({"status": "unavailable"}) == 0
    assert store.count_triage_runs({"pr_author": "kit"}) == 1
    assert store.count_triage_runs({"since": NOW * 2}) == 0


def test_a_degraded_run_can_be_singled_out(store: IndexStore) -> None:
    degraded = _run()
    degraded.signals = [SignalReport(kind="codeowners", status="unavailable", detail="502")]
    store.record_triage_run(degraded)
    assert store.count_triage_runs({"degraded": True}) == 1


def test_recording_the_same_contribution_twice_counts_once(store: IndexStore) -> None:
    rows = [
        {
            "platform": "github",
            "path": "src/app.py",
            "identity": "dana",
            "role": "authored",
            "source": "commit",
            "reference": "abc1234",
            "event_at": NOW,
        }
    ]
    assert store.record_path_contributions(rows) == 1
    assert store.record_path_contributions(rows) == 0
    assert len(store.path_contributions(["src/app.py"], since=0)) == 1


def test_the_same_person_can_author_and_review_the_same_file(store: IndexStore) -> None:
    store.record_path_contributions(
        [
            {
                "platform": "github",
                "path": "src/app.py",
                "identity": "dana",
                "role": "authored",
                "source": "pull_request",
                "reference": "3",
                "event_at": NOW,
            },
            {
                "platform": "github",
                "path": "src/app.py",
                "identity": "dana",
                "role": "reviewed",
                "source": "review",
                "reference": "4",
                "event_at": NOW,
            },
        ]
    )
    roles = {row["role"] for row in store.path_contributions(["src/app.py"], since=0)}
    assert roles == {"authored", "reviewed"}


def test_identities_are_stored_in_one_spelling(store: IndexStore) -> None:
    store.record_path_contributions(
        [
            {
                "platform": "github",
                "path": "src/app.py",
                "identity": "Dana",
                "role": "authored",
                "source": "commit",
                "reference": "abc",
                "event_at": NOW,
            }
        ]
    )
    assert store.path_contributions(["src/app.py"], since=0)[0]["identity"] == "dana"


def test_a_fetch_marker_separates_asked_from_never_asked(store: IndexStore) -> None:
    assert store.path_fetch_times(["a.py"]) == {}
    store.mark_path_fetched(["a.py"], platform="github", entries=0, at=NOW)
    assert store.path_fetch_times(["a.py"]) == {"a.py": NOW}
    # Re-marking updates rather than duplicating.
    store.mark_path_fetched(["a.py"], platform="github", entries=3, at=NOW + 10)
    assert store.path_fetch_times(["a.py"]) == {"a.py": NOW + 10}


def test_contributions_outside_the_window_are_not_returned(store: IndexStore) -> None:
    store.record_path_contributions(
        [
            {
                "platform": "github",
                "path": "src/app.py",
                "identity": "dana",
                "role": "authored",
                "source": "commit",
                "reference": "old",
                "event_at": NOW - 500 * 86_400,
            }
        ]
    )
    assert store.path_contributions(["src/app.py"], since=NOW - 180 * 86_400) == []


def test_a_row_without_a_path_or_a_name_is_not_recorded(store: IndexStore) -> None:
    written = store.record_path_contributions(
        [
            {"platform": "github", "path": "", "identity": "dana", "role": "authored"},
            {"platform": "github", "path": "a.py", "identity": "", "role": "authored"},
        ]
    )
    assert written == 0


def test_a_person_is_findable_however_codeowners_spelled_them(store: IndexStore) -> None:
    """`@Dana` in CODEOWNERS and `dana` on the platform are one person.

    The candidate column is what "was I suggested?" is answered from, so it
    holds the comparison spelling; the stored candidate keeps the display one.
    """
    store.record_triage_run(_run(candidates=[_candidate("Dana")]))
    assert store.count_triage_runs({"identity": "dana"}) == 1
    assert store.list_triage_runs({})[0].candidates[0].identity == "Dana"
    assert store.summarize_triage_candidates({})[0]["identity"] == "dana"
