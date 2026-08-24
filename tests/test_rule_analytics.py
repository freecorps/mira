"""Phase 3 — continuous evaluation and analytics.

The acceptance criterion these tests defend: you can pick a rule, see where it
ran and what came back, every number reduces to the events behind it, and no
absence of feedback is ever converted into approval.
"""

from __future__ import annotations

import csv
import io
import json
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier
from types import SimpleNamespace

import pytest

from mira.config import LearningConfig
from mira.feedback import analytics
from mira.feedback.evaluation import (
    RuleAnalyticsRow,
    RuleEvaluation,
    RuleOutcomeCounts,
    detect_regression,
    evaluation_key,
    is_addressed,
    origin_for_rule,
    outcome_for_kinds,
)
from mira.feedback.exposure import (
    ExposedRule,
    ReviewScope,
    build_rule_evaluations,
    exposed_rules_from_rows,
    record_review_exposures,
)
from mira.feedback.models import FeedbackEventV2, ReviewFinding
from mira.index.store import IndexStore


@pytest.fixture
def isolated_index(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("MIRA_INDEX_DIR", str(tmp_path))
    monkeypatch.delenv("DATABASE_URL", raising=False)
    return tmp_path


@pytest.fixture
def store(isolated_index: Path) -> IndexStore:
    store = IndexStore.open("acme", "app")
    yield store
    store.close()


def _finding(
    store: IndexStore,
    finding_id: str,
    *,
    path: str = "src/a.py",
    category: str = "security",
    title: str = "Unsafe call",
    state: str = "open",
    created_at: float = 0.0,
) -> ReviewFinding:
    finding = ReviewFinding(
        id=finding_id,
        fingerprint=f"fp-{finding_id}",
        review_id=0,
        platform="github",
        owner="acme",
        repo="app",
        pr_number=7,
        pr_url="https://github.com/acme/app/pull/7",
        base_sha="base",
        head_sha="head",
        path=path,
        start_line=10,
        end_line=10,
        symbol="",
        category=category,
        severity="warning",
        confidence=0.9,
        title=title,
        body="body",
        suggestion="",
        detector="main",
        prompt_model="model",
        state=state,
        created_at=created_at or time.time(),
    )
    store.save_review_finding(finding)
    if state != "open":
        store.update_review_finding_state(finding_id, state)
    return finding


def _evaluation(finding_id: str | None, *, rule_id: int = 1, **overrides) -> RuleEvaluation:
    defaults = {
        "rule_id": rule_id,
        "rule_version": 1,
        "rule_origin": "learned",
        "scope_type": "repo",
        "scope_value": "",
        "category": "security",
        "decision": "instruction",
        "platform": "github",
        "owner": "acme",
        "repo": "app",
        "pr_number": 7,
        "pr_author": "alice",
        "head_sha": "head",
    }
    defaults.update(overrides)
    return RuleEvaluation(
        evaluation_key=evaluation_key(
            platform=defaults["platform"],
            owner=defaults["owner"],
            repo=defaults["repo"],
            pr_number=defaults["pr_number"],
            head_sha=defaults["head_sha"],
            rule_id=defaults["rule_id"],
            rule_version=defaults["rule_version"],
            decision=defaults["decision"],
            finding_id=finding_id,
        ),
        finding_id=finding_id,
        **defaults,
    )


def _feedback(
    store: IndexStore, finding_id: str, kind: str, *, actor: str = "bob", source: str = ""
) -> None:
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
            source_event_id=source or f"{kind}:{finding_id}:{actor}",
            head_sha="head",
            thread_state="",
            provenance_complete=True,
        )
    )


# --------------------------------------------------------------- idempotency


def test_retry_does_not_duplicate_evaluation(store: IndexStore) -> None:
    _finding(store, "f1")
    evaluation = _evaluation("f1")

    assert store.record_rule_evaluations([evaluation]) == 1
    assert store.record_rule_evaluations([evaluation]) == 0
    assert store.record_rule_evaluations([evaluation, evaluation]) == 0

    assert store.count_rule_evaluations({"rule_id": 1}) == 1


def test_evaluation_key_ignores_review_id(store: IndexStore) -> None:
    """A retried round gets a new review row but is still the same exposure."""
    _finding(store, "f1")
    first = _evaluation("f1")
    first.review_id = 11
    retry = _evaluation("f1")
    retry.review_id = 22

    assert first.evaluation_key == retry.evaluation_key
    store.record_rule_evaluations([first])
    assert store.record_rule_evaluations([retry]) == 0
    assert store.count_rule_evaluations({"rule_id": 1}) == 1


def test_evaluation_key_separates_distinct_exposures() -> None:
    base = {
        "platform": "github",
        "owner": "acme",
        "repo": "app",
        "pr_number": 7,
        "head_sha": "head",
        "rule_id": 1,
        "rule_version": 1,
        "decision": "instruction",
        "finding_id": "f1",
    }
    baseline = evaluation_key(**base)
    for field, value in (
        ("head_sha", "other"),
        ("rule_id", 2),
        ("rule_version", 2),
        ("decision", "suppress"),
        ("finding_id", "f2"),
        ("pr_number", 8),
    ):
        assert evaluation_key(**{**base, field: value}) != baseline


def test_link_rule_evaluations_does_not_repoint_history(store: IndexStore) -> None:
    _finding(store, "f1")
    evaluation = _evaluation("f1")
    store.record_rule_evaluations([evaluation])

    store.link_rule_evaluations([evaluation.evaluation_key], 101)
    store.link_rule_evaluations([evaluation.evaluation_key], 202)

    rows = store.list_rule_evaluations({"rule_id": 1})
    assert rows[0]["review_id"] == 101


# --------------------------------------------------------------- concurrency


def test_concurrent_writers_record_one_evaluation(isolated_index: Path) -> None:
    setup = IndexStore.open("acme", "app")
    _finding(setup, "f1")
    setup.close()
    barrier = Barrier(4)

    def write() -> int:
        store = IndexStore.open("acme", "app")
        try:
            barrier.wait()
            return store.record_rule_evaluations([_evaluation("f1")])
        finally:
            store.close()

    with ThreadPoolExecutor(max_workers=4) as pool:
        created = list(pool.map(lambda _: write(), range(4)))

    assert sum(created) == 1
    store = IndexStore.open("acme", "app")
    try:
        assert store.count_rule_evaluations({"rule_id": 1}) == 1
    finally:
        store.close()


def test_concurrent_distinct_evaluations_all_land(isolated_index: Path) -> None:
    setup = IndexStore.open("acme", "app")
    for index in range(6):
        _finding(setup, f"f{index}")
    setup.close()
    barrier = Barrier(6)

    def write(index: int) -> int:
        store = IndexStore.open("acme", "app")
        try:
            barrier.wait()
            return store.record_rule_evaluations([_evaluation(f"f{index}")])
        finally:
            store.close()

    with ThreadPoolExecutor(max_workers=6) as pool:
        created = list(pool.map(write, range(6)))

    assert sum(created) == 6
    store = IndexStore.open("acme", "app")
    try:
        assert store.count_rule_evaluations({"rule_id": 1}) == 6
    finally:
        store.close()


# ------------------------------------------------- silence stays neutral


def test_unobserved_is_not_positive(store: IndexStore) -> None:
    _finding(store, "f1")
    store.record_rule_evaluations([_evaluation("f1")])
    # Exactly what run_pr_merged_learning writes when a PR is merged with no
    # reaction and an unresolved thread.
    _feedback(store, "f1", "unobserved", actor="merger")

    row = store.aggregate_rule_analytics({"rule_id": 1})[0]
    counts = row.counts
    assert counts.unobserved == 1
    assert counts.positive == 0
    assert counts.observed == 0
    assert counts.acceptance_rate is None
    assert counts.addressed == 0
    assert counts.addressed_rate == 0.0


def test_unobserved_cannot_raise_acceptance_rate(store: IndexStore) -> None:
    """Adding silence to a mixed record must not move the score at all."""
    for index, kind in ((1, "thumbs_up"), (2, "thumbs_down")):
        _finding(store, f"f{index}")
        store.record_rule_evaluations([_evaluation(f"f{index}")])
        _feedback(store, f"f{index}", kind)

    before = store.aggregate_rule_analytics({"rule_id": 1})[0].counts.acceptance_rate

    for index in range(3, 9):
        _finding(store, f"f{index}")
        store.record_rule_evaluations([_evaluation(f"f{index}")])
        _feedback(store, f"f{index}", "unobserved", actor="merger")

    after = store.aggregate_rule_analytics({"rule_id": 1})[0]
    assert after.counts.acceptance_rate == before == 0.5
    assert after.counts.unobserved == 6
    # Silence does lower the addressed rate, which is correct: it is evidence
    # of absence of resolution, not evidence of resolution.
    assert after.counts.addressed == 0


def test_merge_without_feedback_is_not_acceptance(store: IndexStore) -> None:
    """A silent merge marks the finding outdated; that is not addressed."""
    _finding(store, "f1", state="outdated")
    store.record_rule_evaluations([_evaluation("f1")])
    _feedback(store, "f1", "unobserved", actor="merger")

    detail = store.list_rule_evaluations({"rule_id": 1})[0]
    assert detail["finding_state"] == "outdated"
    assert detail["outcome"] == "unobserved"
    assert detail["addressed"] is False

    counts = store.aggregate_rule_analytics({"rule_id": 1})[0].counts
    assert counts.positive == 0
    assert counts.addressed == 0


def test_resolved_thread_before_merge_is_addressed(store: IndexStore) -> None:
    """The one merge-time signal that *is* evidence: a resolved thread."""
    _finding(store, "f1", state="fixed")
    store.record_rule_evaluations([_evaluation("f1")])
    _feedback(store, "f1", "fixed", actor="merger")

    counts = store.aggregate_rule_analytics({"rule_id": 1})[0].counts
    assert counts.addressed == 1
    assert counts.positive == 1
    assert counts.addressed_rate == 1.0


def test_outdated_state_alone_is_never_addressed() -> None:
    assert is_addressed([], "outdated") is False
    assert is_addressed(["unobserved"], "outdated") is False
    assert is_addressed(["fixed"], "open") is True
    assert is_addressed([], "fixed") is True


def test_outcome_precedence_puts_dissent_first() -> None:
    assert outcome_for_kinds(["thumbs_up", "reply_disagree"]) == "negative"
    assert outcome_for_kinds(["thumbs_up", "reply_question"]) == "positive"
    assert outcome_for_kinds(["reply_question"]) == "neutral"
    assert outcome_for_kinds([]) == "unobserved"
    assert outcome_for_kinds(["unobserved"]) == "unobserved"
    # An unrecognized signal must not be read as approval.
    assert outcome_for_kinds(["something_new"]) == "unobserved"


# ------------------------------------------ aggregates match their evidence


@pytest.mark.parametrize("outcome", ["positive", "negative", "neutral", "unobserved"])
def test_aggregate_matches_drilldown(store: IndexStore, outcome: str) -> None:
    """Every bucket count equals the number of rows the drill-down returns."""
    mix = {
        "a": ["thumbs_up"],
        "b": ["thumbs_down"],
        "c": ["reply_question"],
        "d": ["unobserved"],
        "e": ["thumbs_up"],
        "f": [],
    }
    for finding_id, finding_kinds in mix.items():
        _finding(store, finding_id)
        store.record_rule_evaluations([_evaluation(finding_id)])
        for kind in finding_kinds:
            _feedback(store, finding_id, kind)

    counts = store.aggregate_rule_analytics({"rule_id": 1})[0].counts
    aggregate = getattr(counts, outcome)
    detailed = store.list_rule_evaluations({"rule_id": 1}, outcome=outcome, limit=100)

    assert (
        aggregate == len(detailed) == store.count_rule_evaluations({"rule_id": 1}, outcome=outcome)
    )
    assert {row["outcome"] for row in detailed} == {outcome}


def test_buckets_sum_to_findings(store: IndexStore) -> None:
    for index, kind in enumerate(["thumbs_up", "thumbs_down", "reply_question", "unobserved"]):
        _finding(store, f"f{index}")
        store.record_rule_evaluations([_evaluation(f"f{index}")])
        _feedback(store, f"f{index}", kind)
    # A review-scoped exposure counts toward exposures but has no outcome.
    store.record_rule_evaluations([_evaluation(None)])

    counts = store.aggregate_rule_analytics({"rule_id": 1})[0].counts
    assert counts.positive + counts.negative + counts.neutral + counts.unobserved == counts.findings
    assert counts.findings == 4
    assert counts.review_exposures == 1
    assert counts.exposures == 5


def test_reaction_and_reply_counters(store: IndexStore) -> None:
    _finding(store, "f1")
    store.record_rule_evaluations([_evaluation("f1")])
    _feedback(store, "f1", "thumbs_up", actor="a")
    _feedback(store, "f1", "thumbs_up", actor="b")
    _feedback(store, "f1", "thumbs_down", actor="c")
    _finding(store, "f2")
    store.record_rule_evaluations([_evaluation("f2")])
    _feedback(store, "f2", "reply_agree", actor="d")
    _feedback(store, "f2", "reply_disagree", actor="e")

    counts = store.aggregate_rule_analytics({"rule_id": 1})[0].counts
    assert counts.thumbs_up == 2
    assert counts.thumbs_down == 1
    assert counts.reply_agree == 1
    assert counts.reply_disagree == 1
    # Both findings carry a negative signal, so both land in `negative`.
    assert counts.negative == 2


def test_repeated_false_positives_count_only_repeats(store: IndexStore) -> None:
    for index in range(3):
        _finding(store, f"dup{index}", path="src/a.py", title="Unsafe call")
        store.record_rule_evaluations([_evaluation(f"dup{index}")])
        _feedback(store, f"dup{index}", "thumbs_down", actor=f"user{index}")
    _finding(store, "solo", path="src/b.py", title="Different complaint")
    store.record_rule_evaluations([_evaluation("solo")])
    _feedback(store, "solo", "thumbs_down")

    counts = store.aggregate_rule_analytics({"rule_id": 1})[0].counts
    # Three equivalent complaints = two repeats; the unique one is not a repeat.
    assert counts.repeated_false_positives == 2
    assert counts.negative == 4


# ------------------------------------------------------------- aggregations


def test_summary_dimensions(store: IndexStore) -> None:
    _finding(store, "f1", category="security")
    _finding(store, "f2", category="style")
    store.record_rule_evaluations(
        [
            _evaluation("f1", category="security", pr_author="alice"),
            _evaluation("f2", rule_id=2, category="style", pr_author="bob"),
        ]
    )
    _feedback(store, "f1", "thumbs_up")
    _feedback(store, "f2", "thumbs_down")

    by_category = {b["bucket"]: b for b in store.rule_analytics_summary(dimension="category")}
    assert by_category["security"]["positive"] == 1
    assert by_category["style"]["negative"] == 1

    by_author = {b["bucket"]: b for b in store.rule_analytics_summary(dimension="author")}
    assert by_author["alice"]["exposures"] == 1
    assert by_author["bob"]["exposures"] == 1

    by_repo = {b["bucket"]: b for b in store.rule_analytics_summary(dimension="repo")}
    assert by_repo["acme/app"]["exposures"] == 2


def test_summary_rejects_unknown_dimension(store: IndexStore) -> None:
    with pytest.raises(ValueError, match="unsupported summary dimension"):
        store.rule_analytics_summary(dimension="drop table")


def test_period_filters_are_applied(store: IndexStore) -> None:
    now = time.time()
    _finding(store, "old")
    _finding(store, "new")
    old = _evaluation("old")
    old.created_at = now - 100_000
    new = _evaluation("new")
    new.created_at = now
    store.record_rule_evaluations([old, new])

    recent = store.aggregate_rule_analytics({"rule_id": 1, "since": now - 1000})
    assert recent[0].counts.exposures == 1
    assert store.aggregate_rule_analytics({"rule_id": 1})[0].counts.exposures == 2


def test_pagination_is_stable(store: IndexStore) -> None:
    for index in range(10):
        _finding(store, f"f{index}")
        store.record_rule_evaluations([_evaluation(f"f{index}")])

    first = store.list_rule_evaluations({"rule_id": 1}, limit=4, offset=0)
    second = store.list_rule_evaluations({"rule_id": 1}, limit=4, offset=4)
    third = store.list_rule_evaluations({"rule_id": 1}, limit=4, offset=8)

    ids = [row["id"] for row in first + second + third]
    assert len(ids) == 10
    assert len(set(ids)) == 10


# -------------------------------------------------------- regression advice


def _analytics_row(*, exposures: int, positive: int, negative: int, origin="learned"):
    return RuleAnalyticsRow(
        rule_id=1,
        owner="acme",
        repo="app",
        origin=origin,
        counts=RuleOutcomeCounts(
            exposures=exposures,
            findings=positive + negative,
            positive=positive,
            negative=negative,
        ),
    )


def test_regression_requires_minimum_exposures() -> None:
    row = _analytics_row(exposures=5, positive=0, negative=5)
    assert (
        detect_regression(
            row, min_exposures=20, negative_rate_threshold=0.5, disable_rate_threshold=0.8
        )
        is None
    )
    row = _analytics_row(exposures=20, positive=0, negative=20)
    suggestion = detect_regression(
        row, min_exposures=20, negative_rate_threshold=0.5, disable_rate_threshold=0.8
    )
    assert suggestion is not None
    assert suggestion.action == "disable"
    assert suggestion.min_exposures == 20


def test_regression_escalates_by_negative_rate() -> None:
    row = _analytics_row(exposures=30, positive=12, negative=18)
    suggestion = detect_regression(
        row, min_exposures=20, negative_rate_threshold=0.5, disable_rate_threshold=0.8
    )
    assert suggestion is not None and suggestion.action == "downgrade"


def test_regression_ignores_silence() -> None:
    """Many exposures with no decisive feedback is not a regression."""
    row = RuleAnalyticsRow(
        rule_id=1,
        owner="acme",
        repo="app",
        counts=RuleOutcomeCounts(exposures=100, findings=100, unobserved=100),
    )
    assert (
        detect_regression(
            row, min_exposures=20, negative_rate_threshold=0.5, disable_rate_threshold=0.8
        )
        is None
    )


def test_regression_suggestions_skip_manual_rules(store: IndexStore) -> None:
    rule = store.create_learned_rule(
        "Never log credentials.", "security", created_by="admin", source_signal="manual"
    )
    for index in range(25):
        _finding(store, f"f{index}")
        store.record_rule_evaluations(
            [_evaluation(f"f{index}", rule_id=rule.id, rule_origin="manual")]
        )
        _feedback(store, f"f{index}", "thumbs_down", actor=f"user{index}")

    config = LearningConfig(min_exposures_for_regression=5)
    assert analytics.regression_suggestions(config=config) == []


def test_regression_suggestions_flag_learned_rules(store: IndexStore) -> None:
    rule = store.create_learned_rule(
        "Avoid broad excepts.", "style", source_signal="reject_pattern"
    )
    for index in range(25):
        _finding(store, f"f{index}")
        store.record_rule_evaluations([_evaluation(f"f{index}", rule_id=rule.id)])
        _feedback(store, f"f{index}", "thumbs_down", actor=f"user{index}")

    config = LearningConfig(min_exposures_for_regression=5)
    suggestions = analytics.regression_suggestions(config=config)
    assert [s.rule_id for s in suggestions] == [rule.id]
    assert suggestions[0].action == "disable"


def test_suggestions_never_disable_the_rule(store: IndexStore) -> None:
    """Phase 3 advises; it must not act."""
    rule = store.create_learned_rule(
        "Avoid broad excepts.", "style", source_signal="reject_pattern"
    )
    for index in range(25):
        _finding(store, f"f{index}")
        store.record_rule_evaluations([_evaluation(f"f{index}", rule_id=rule.id)])
        _feedback(store, f"f{index}", "thumbs_down", actor=f"user{index}")

    analytics.regression_suggestions(config=LearningConfig(min_exposures_for_regression=5))

    refreshed = IndexStore.open("acme", "app")
    try:
        stored = refreshed.get_learned_rule(rule.id)
        assert stored.active is True
        assert stored.status == "approved"
        assert stored.disabled_at is None
    finally:
        refreshed.close()


def test_manual_and_learned_rules_stay_distinguishable(store: IndexStore) -> None:
    # `create_learned_rule` is the admin-authored constructor, so it defaults
    # to the manual signal; a synthesized rule carries its detector signal.
    manual = store.create_learned_rule("Manual.", "style", created_by="admin")
    learned = store.create_learned_rule(
        "Learned.", "style", source_signal="reject_pattern", created_by=""
    )
    assert origin_for_rule(manual) == "manual"
    assert origin_for_rule(learned) == "learned"


# --------------------------------------------------------- period comparison


def test_activation_comparison_splits_on_effective_from(store: IndexStore) -> None:
    now = time.time()
    pivot = now - 5 * 86400
    rule = store.create_learned_rule("Avoid broad excepts.", "security")
    store._conn.execute(
        "UPDATE learned_rules SET effective_from = ? WHERE id = ?", (pivot, rule.id)
    )
    store._conn.commit()

    _finding(store, "before1", created_at=pivot - 86400)
    _feedback(store, "before1", "thumbs_down")
    _finding(store, "after1", created_at=pivot + 86400)
    _feedback(store, "after1", "thumbs_up")

    result = analytics.compare_activation_periods(
        owner="acme", repo="app", rule_id=rule.id, window_days=30
    )
    assert result["activated_at"] == pytest.approx(pivot)
    assert result["before"]["negative"] == 1
    assert result["after"]["positive"] == 1
    assert result["delta"]["negative"] == -1
    # The 30-day window has not elapsed yet, so the comparison is incomplete.
    assert result["comparable"] is False
    assert "accumulating" in result["reason"]


def test_activation_comparison_reports_complete_windows(store: IndexStore) -> None:
    now = time.time()
    pivot = now - 60 * 86400
    rule = store.create_learned_rule("Avoid broad excepts.", "security")
    store._conn.execute(
        "UPDATE learned_rules SET effective_from = ? WHERE id = ?", (pivot, rule.id)
    )
    store._conn.commit()
    _finding(store, "after1", created_at=pivot + 86400)

    result = analytics.compare_activation_periods(
        owner="acme", repo="app", rule_id=rule.id, window_days=30
    )
    assert result["comparable"] is True
    assert result["after"]["findings"] == 1


def test_activation_comparison_without_timestamp_is_not_comparable(store: IndexStore) -> None:
    rule = store.create_learned_rule("Avoid broad excepts.", "security")
    store._conn.execute(
        "UPDATE learned_rules SET effective_from = 0, created_at = 0 WHERE id = ?", (rule.id,)
    )
    store._conn.commit()

    result = analytics.compare_activation_periods(owner="acme", repo="app", rule_id=rule.id)
    assert result["comparable"] is False
    assert result["before"] is None


def test_path_scope_comparison_uses_like_translation(store: IndexStore) -> None:
    now = time.time()
    pivot = now - 60 * 86400
    rule = store.create_learned_rule(
        "Skip generated code.",
        "style",
        path_pattern="generated/**",
        scope_type="path",
        scope_value="generated/**",
    )
    store._conn.execute(
        "UPDATE learned_rules SET effective_from = ? WHERE id = ?", (pivot, rule.id)
    )
    store._conn.commit()

    _finding(store, "in", path="generated/api.py", category="style", created_at=pivot + 100)
    _finding(store, "out", path="src/api.py", category="style", created_at=pivot + 100)

    result = analytics.compare_activation_periods(owner="acme", repo="app", rule_id=rule.id)
    assert result["after"]["findings"] == 1


def test_glob_to_like_escapes_wildcards() -> None:
    from mira.feedback.evaluation_sql import glob_to_like

    assert glob_to_like("generated/**") == "generated/%"
    assert glob_to_like("src/*.py") == "src/%.py"
    # A literal % in a path must not become a wildcard.
    assert glob_to_like("reports/100%/*") == "reports/100\\%/%"


# ------------------------------------------------------------------- export


def test_export_json_round_trips(store: IndexStore) -> None:
    _finding(store, "f1")
    store.record_rule_evaluations([_evaluation("f1")])
    _feedback(store, "f1", "thumbs_up")

    body, media_type = analytics.export_rule_analytics(fmt="json")
    assert media_type == "application/json"
    payload = json.loads(body)
    assert payload["rules"][0]["rule_id"] == 1
    assert payload["rules"][0]["positive"] == 1


def test_export_csv_has_stable_header_and_values(store: IndexStore) -> None:
    _finding(store, "f1")
    store.record_rule_evaluations([_evaluation("f1")])
    _feedback(store, "f1", "thumbs_down")

    body, media_type = analytics.export_rule_analytics(fmt="csv")
    assert media_type == "text/csv"
    rows = list(csv.DictReader(io.StringIO(body)))
    assert len(rows) == 1
    assert rows[0]["rule_id"] == "1"
    assert rows[0]["negative"] == "1"
    assert rows[0]["owner"] == "acme"
    # None renders as empty, not the string "None".
    assert rows[0]["addressed_rate"] == "0.0"


def test_export_evaluations_carries_the_evidence(store: IndexStore) -> None:
    _finding(store, "f1", title="Unsafe call")
    store.record_rule_evaluations([_evaluation("f1")])
    _feedback(store, "f1", "thumbs_down")

    body, _media = analytics.export_rule_evaluations(
        owner="acme", repo="app", filters={"rule_id": 1}, fmt="csv"
    )
    rows = list(csv.DictReader(io.StringIO(body)))
    assert rows[0]["finding_title"] == "Unsafe call"
    assert rows[0]["outcome"] == "negative"
    assert rows[0]["addressed"] == "false"


def test_export_csv_renders_none_as_empty(store: IndexStore) -> None:
    _finding(store, "f1")
    store.record_rule_evaluations([_evaluation("f1")])

    body, _media = analytics.export_rule_analytics(fmt="csv")
    row = next(csv.DictReader(io.StringIO(body)))
    assert row["acceptance_rate"] == ""


# ------------------------------------------------------------- audit events


def test_audit_events_are_recorded_and_listed(store: IndexStore) -> None:
    analytics.record_audit_event(
        owner="acme",
        repo="app",
        event_type="regression_dismissed",
        rule_id=3,
        actor="admin",
        summary="dismissed",
        detail={"note": "expected during migration"},
    )
    events = analytics.list_audit_events(owner="acme", repo="app")
    assert len(events) == 1
    assert events[0]["event_type"] == "regression_dismissed"
    assert events[0]["actor"] == "admin"
    assert json.loads(events[0]["detail_json"])["note"] == "expected during migration"


def test_audit_events_filter_by_rule(store: IndexStore) -> None:
    for rule_id in (1, 2):
        analytics.record_audit_event(
            owner="acme", repo="app", event_type="regression_accepted", rule_id=rule_id
        )
    assert len(analytics.list_audit_events(owner="acme", repo="app", rule_id=2)) == 1


# ---------------------------------------------------------------- exposures


def test_exposure_builds_review_and_finding_rows() -> None:
    exposed = [
        ExposedRule(
            rule_id=1,
            version=2,
            origin="learned",
            scope_type="path",
            scope_value="src/**",
            category="security",
            rule_text="Never log credentials.",
        )
    ]
    findings = [
        SimpleNamespace(finding_id="in", path="src/a.py", category="security"),
        SimpleNamespace(finding_id="wrong-path", path="docs/a.md", category="security"),
        SimpleNamespace(finding_id="wrong-category", path="src/b.py", category="style"),
        SimpleNamespace(finding_id="", path="src/c.py", category="security"),
    ]
    evaluations = build_rule_evaluations(
        exposed,
        platform="github",
        owner="acme",
        repo="app",
        pr_number=7,
        pr_author="alice",
        head_sha="head",
        findings=findings,
    )

    linked = [e.finding_id for e in evaluations]
    assert linked.count(None) == 1  # the review-scoped exposure
    assert "in" in linked
    assert "wrong-path" not in linked
    assert "wrong-category" not in linked
    assert len(evaluations) == 2
    assert all(e.rule_version == 2 for e in evaluations)


def test_repo_scope_covers_every_path_in_category() -> None:
    exposed = [
        ExposedRule(
            rule_id=1,
            version=1,
            origin="learned",
            scope_type="repo",
            scope_value="",
            category="",
            rule_text="Be careful.",
        )
    ]
    findings = [
        SimpleNamespace(finding_id="a", path="src/a.py", category="security"),
        SimpleNamespace(finding_id="b", path="docs/b.md", category="style"),
    ]
    evaluations = build_rule_evaluations(
        exposed,
        platform="github",
        owner="acme",
        repo="app",
        pr_number=7,
        pr_author="alice",
        head_sha="head",
        findings=findings,
    )
    assert {e.finding_id for e in evaluations} == {None, "a", "b"}


def test_exposed_rules_snapshot_records_origin_and_version(store: IndexStore) -> None:
    manual = store.create_learned_rule("Manual.", "style", created_by="admin")
    exposed = exposed_rules_from_rows([manual])
    assert exposed[0].origin == "manual"
    assert exposed[0].rule_id == manual.id
    assert exposed[0].decision == "instruction"


def test_record_review_exposures_never_raises() -> None:
    class Broken:
        def record_rule_evaluations(self, evaluations):
            raise RuntimeError("db is on fire")

    exposed = [
        ExposedRule(
            rule_id=1,
            version=1,
            origin="learned",
            scope_type="repo",
            scope_value="",
            category="",
            rule_text="x",
        )
    ]
    assert (
        record_review_exposures(
            Broken(),
            exposed,
            platform="github",
            owner="acme",
            repo="app",
            pr_number=1,
            pr_author="a",
            head_sha="h",
            findings=[],
        )
        == 0
    )


# ----------------------------------------------------------- old-db upgrade


_PRE_PHASE3_SCHEMA = """
CREATE TABLE review_findings (
    id TEXT PRIMARY KEY,
    fingerprint TEXT NOT NULL,
    review_id INTEGER NOT NULL DEFAULT 0,
    platform TEXT NOT NULL DEFAULT 'github',
    owner TEXT NOT NULL DEFAULT '',
    repo TEXT NOT NULL DEFAULT '',
    pr_number INTEGER NOT NULL DEFAULT 0,
    pr_url TEXT NOT NULL DEFAULT '',
    base_sha TEXT NOT NULL DEFAULT '',
    head_sha TEXT NOT NULL DEFAULT '',
    path TEXT NOT NULL DEFAULT '',
    start_line INTEGER NOT NULL DEFAULT 0,
    end_line INTEGER NOT NULL DEFAULT 0,
    symbol TEXT NOT NULL DEFAULT '',
    category TEXT NOT NULL DEFAULT '',
    severity TEXT NOT NULL DEFAULT '',
    confidence REAL NOT NULL DEFAULT 0,
    title TEXT NOT NULL DEFAULT '',
    body TEXT NOT NULL DEFAULT '',
    suggestion TEXT NOT NULL DEFAULT '',
    detector TEXT NOT NULL DEFAULT '',
    prompt_model TEXT NOT NULL DEFAULT '',
    platform_comment_id TEXT NOT NULL DEFAULT '',
    platform_thread_id TEXT NOT NULL DEFAULT '',
    state TEXT NOT NULL DEFAULT 'open',
    created_at REAL NOT NULL DEFAULT 0,
    updated_at REAL NOT NULL DEFAULT 0
);
CREATE TABLE learned_rules (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    rule_text TEXT NOT NULL DEFAULT '',
    source_signal TEXT NOT NULL DEFAULT '',
    category TEXT NOT NULL DEFAULT '',
    path_pattern TEXT NOT NULL DEFAULT '',
    sample_count INTEGER NOT NULL DEFAULT 0,
    active INTEGER NOT NULL DEFAULT 1,
    created_at REAL NOT NULL DEFAULT 0,
    updated_at REAL NOT NULL DEFAULT 0
);
"""


def test_opens_and_upgrades_a_pre_phase3_database(isolated_index: Path) -> None:
    """A database from before Phase 3 gains the new tables and keeps its data."""
    db_dir = isolated_index / "acme"
    db_dir.mkdir(parents=True, exist_ok=True)
    db_path = db_dir / "legacy.db"
    conn = sqlite3.connect(db_path)
    conn.executescript(_PRE_PHASE3_SCHEMA)
    conn.execute(
        "INSERT INTO review_findings (id, fingerprint, category, path, state, head_sha) "
        "VALUES ('legacy-finding', 'fp', 'security', 'src/a.py', 'open', 'head')"
    )
    conn.execute(
        "INSERT INTO learned_rules (rule_text, source_signal, category, sample_count, active) "
        "VALUES ('Legacy rule.', 'reject_pattern', 'security', 4, 1)"
    )
    conn.commit()
    conn.close()

    store = IndexStore.open("acme", "legacy")
    try:
        # Pre-existing rows survive the upgrade.
        assert store.get_review_finding("legacy-finding") is not None
        rules = store.list_learned_rules()
        assert [r.rule_text for r in rules] == ["Legacy rule."]
        # And the Phase 3 tables now exist and work.
        evaluation = _evaluation("legacy-finding", rule_id=rules[0].id, repo="legacy")
        assert store.record_rule_evaluations([evaluation]) == 1
        assert store.record_rule_evaluations([evaluation]) == 0
        store.record_learning_audit_event(event_type="upgrade_check", rule_id=rules[0].id)
        assert len(store.list_learning_audit_events()) == 1
        assert store.aggregate_rule_analytics()[0].counts.exposures == 1
    finally:
        store.close()


def test_upgraded_database_keeps_working_after_reopen(isolated_index: Path) -> None:
    """Re-running the schema pass on an already-upgraded DB is a no-op."""
    first = IndexStore.open("acme", "app")
    _finding(first, "f1")
    first.record_rule_evaluations([_evaluation("f1")])
    first.close()

    second = IndexStore.open("acme", "app")
    try:
        assert second.count_rule_evaluations({"rule_id": 1}) == 1
        assert second.record_rule_evaluations([_evaluation("f1")]) == 0
    finally:
        second.close()


# ------------------------------------------------------------ kill switch


def test_analytics_flag_defaults_on_and_can_be_disabled() -> None:
    assert LearningConfig().evaluation_analytics is True
    assert LearningConfig(evaluation_analytics=False).evaluation_analytics is False


def test_disabled_analytics_skips_recording(monkeypatch: pytest.MonkeyPatch) -> None:
    """The flag is checked before the store is ever touched."""
    from mira.config import MiraConfig

    config = MiraConfig()
    config.learning.evaluation_analytics = False
    assert config.learning.evaluation_analytics is False
    # The engine guards both the snapshot and the write on this flag, so an
    # empty snapshot is all a disabled install can produce.
    assert (
        record_review_exposures(
            None,
            [],
            platform="github",
            owner="acme",
            repo="app",
            pr_number=1,
            pr_author="a",
            head_sha="h",
            findings=[],
        )
        == 0
    )


# --------------------------------------------- review-scoped rows are labelled


def test_review_scoped_exposure_is_not_unobserved(store: IndexStore) -> None:
    """A rule that produced nothing must not read as a rule nobody answered."""
    _finding(store, "f1")
    store.record_rule_evaluations([_evaluation("f1"), _evaluation(None)])
    _feedback(store, "f1", "unobserved", actor="merger")

    rows = store.list_rule_evaluations({"rule_id": 1}, limit=100)
    by_finding = {row["finding_id"]: row["outcome"] for row in rows}
    assert by_finding["f1"] == "unobserved"
    assert by_finding[None] == "not_applicable"

    counts = store.aggregate_rule_analytics({"rule_id": 1})[0].counts
    assert counts.unobserved == 1
    assert counts.review_exposures == 1


def test_every_bucket_equals_its_drilldown(store: IndexStore) -> None:
    """The audit guarantee, stated once over a mixed fixture."""
    plan = {
        "up": "thumbs_up",
        "down": "thumbs_down",
        "q": "reply_question",
        "silent": "unobserved",
    }
    for finding_id, kind in plan.items():
        _finding(store, finding_id)
        store.record_rule_evaluations([_evaluation(finding_id)])
        _feedback(store, finding_id, kind)
    store.record_rule_evaluations([_evaluation(None)])

    counts = store.aggregate_rule_analytics({"rule_id": 1})[0].counts
    total = 0
    for outcome in ("positive", "negative", "neutral", "unobserved"):
        rows = store.list_rule_evaluations({"rule_id": 1}, outcome=outcome, limit=100)
        assert getattr(counts, outcome) == len(rows)
        total += len(rows)
    assert total == counts.findings
    not_applicable = store.list_rule_evaluations(
        {"rule_id": 1}, outcome="not_applicable", limit=100
    )
    assert len(not_applicable) == counts.review_exposures
    assert total + len(not_applicable) == counts.exposures


def test_previous_release_can_still_read_an_upgraded_database(isolated_index: Path) -> None:
    """Rollback: the prior release must keep working on an upgraded database.

    Phase 3 only adds tables — it alters no existing column and drops nothing —
    so the previous release, which has never heard of `rule_evaluations`, must
    read and write the tables it does know about exactly as before.
    """
    store = IndexStore.open("acme", "app")
    try:
        _finding(store, "f1")
        store.record_rule_evaluations([_evaluation("f1")])
        _feedback(store, "f1", "thumbs_up")
        rule = store.create_learned_rule("Never log credentials.", "security")
        db_path = store._db_path
    finally:
        store.close()

    # The exact column lists the pre-Phase-3 release selects. A dropped or
    # renamed column would raise OperationalError here.
    conn = sqlite3.connect(db_path)
    try:
        assert conn.execute(
            "SELECT id, rule_text, source_signal, category, path_pattern, sample_count, "
            "active, status, created_by, version, scope_type, scope_value, "
            "origin_candidate_id, rationale, evidence_count, effective_from, disabled_at, "
            "supersedes_rule_id, semantic_fingerprint, created_at, updated_at "
            "FROM learned_rules WHERE id = ?",
            (rule.id,),
        ).fetchone()
        assert conn.execute(
            "SELECT id, fingerprint, review_id, platform, owner, repo, pr_number, pr_url, "
            "base_sha, head_sha, path, start_line, end_line, symbol, category, severity, "
            "confidence, title, body, suggestion, detector, prompt_model, "
            "platform_comment_id, platform_thread_id, state, created_at, updated_at "
            "FROM review_findings"
        ).fetchall()
        assert conn.execute(
            "SELECT id, finding_id, kind, actor, actor_role, raw_text, rationale, platform, "
            "source_event_id, head_sha, thread_state, provenance_complete, audit_json, "
            "created_at FROM feedback_events_v2"
        ).fetchall()
        # A prior release still writes through the old paths unchanged.
        conn.execute(
            "INSERT INTO learned_rules (rule_text, source_signal, category, sample_count, "
            "active, created_at, updated_at) VALUES ('Old release rule.', 'manual', "
            "'style', 1, 1, 0, 0)"
        )
        conn.commit()
    finally:
        conn.close()

    # And the new release still reads what the old one wrote.
    reopened = IndexStore.open("acme", "app")
    try:
        assert "Old release rule." in {r.rule_text for r in reopened.list_learned_rules()}
        assert reopened.count_rule_evaluations({"rule_id": 1}) == 1
    finally:
        reopened.close()


# ------------------------------------------------------- platform resolution


def test_platform_resolution_finds_non_github_repos(isolated_index: Path) -> None:
    """A GitLab repo must resolve to its own store, not a GitHub-shaped guess.

    `IndexStore.open` namespaces non-GitHub owners, so guessing "github" would
    point analytics at a store that has no rows.
    """
    store = IndexStore.open("acme", "app", platform="gitlab")
    try:
        _finding(store, "f1")
        store.record_rule_evaluations([_evaluation("f1")])
    finally:
        store.close()

    assert analytics._platform_for("acme", "app") == "gitlab"
    rows, total = analytics.list_rule_analytics()
    assert total == 1
    evaluations, count = analytics.list_rule_evaluations(owner="acme", repo="app")
    assert count == 1
    assert evaluations[0]["finding_id"] == "f1"
    assert rows[0].counts.exposures == 1


def test_platform_resolution_uses_the_registry_on_postgres(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """On Postgres the owner column is namespaced, so the platform must be
    resolved from the repo registry rather than assumed."""
    monkeypatch.setenv("DATABASE_URL", "postgresql://example/db")

    class _Db:
        def get_repo_any_platform(self, owner: str, repo: str):
            return [SimpleNamespace(platform="forgejo")]

    monkeypatch.setattr("mira.dashboard.api._app_db", _Db())
    assert analytics._platform_for("acme", "app") == "forgejo"


def test_registry_failure_is_reported_not_defaulted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unreachable registry must not become an empty analytics result.

    Defaulting to GitHub would query the unnamespaced owner while a
    GitLab/Forgejo repo's rows live under `_{platform}/{owner}`, handing back a
    confident-looking empty history. "We could not look" is not "there is
    nothing" — the same distinction the outcome model makes about silence.
    """
    monkeypatch.setenv("DATABASE_URL", "postgresql://example/db")

    class _Broken:
        def get_repo_any_platform(self, owner: str, repo: str):
            raise RuntimeError("registry down")

    monkeypatch.setattr("mira.dashboard.api._app_db", _Broken())
    with pytest.raises(analytics.PlatformResolutionError):
        analytics._platform_for("acme", "app")


def test_unregistered_repo_still_resolves(monkeypatch: pytest.MonkeyPatch) -> None:
    """An empty lookup is not a failure.

    It means the repo is unregistered, or `owner` is already an
    `_{platform}/{owner}` value taken from an aggregate row. Both resolve
    correctly through the GitHub path, which passes the owner through unchanged.
    """
    monkeypatch.setenv("DATABASE_URL", "postgresql://example/db")

    class _Empty:
        def get_repo_any_platform(self, owner: str, repo: str):
            return []

    monkeypatch.setattr("mira.dashboard.api._app_db", _Empty())
    assert analytics._platform_for("_gitlab/acme", "app") == "github"


def test_registry_failure_surfaces_as_service_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The API says unavailable rather than returning an empty page."""
    from fastapi import HTTPException

    import mira.dashboard.routers.analytics as routes

    monkeypatch.setenv("DATABASE_URL", "postgresql://example/db")

    class _Broken:
        def get_repo_any_platform(self, owner: str, repo: str):
            raise RuntimeError("registry down")

    monkeypatch.setattr("mira.dashboard.api._app_db", _Broken())
    admin = SimpleNamespace(
        state=SimpleNamespace(user=SimpleNamespace(is_admin=True, username="root"))
    )
    with pytest.raises(HTTPException) as exc:
        routes.list_rule_evaluations(admin, "acme", "app", 1)
    assert exc.value.status_code == 503


def test_summary_omits_metrics_it_does_not_compute(store: IndexStore) -> None:
    """Repeat detection needs per-rule grouping a bucket doesn't have."""
    _finding(store, "f1")
    store.record_rule_evaluations([_evaluation("f1")])

    bucket = store.rule_analytics_summary(dimension="category")[0]
    assert "repeated_false_positives" not in bucket
    assert bucket["exposures"] == 1


# ------------------------------------------------------------ review findings


def test_csv_export_neutralizes_spreadsheet_formulas(store: IndexStore) -> None:
    """Exported cells come from pull requests, so they can be hostile."""
    _finding(store, "f1", path="=cmd|'/c calc'!A1", title="@SUM(1+1)")
    store.record_rule_evaluations([_evaluation("f1", pr_author="+evil")])

    body, _media = analytics.export_rule_evaluations(
        owner="acme", repo="app", filters={"rule_id": 1}, fmt="csv"
    )
    row = next(csv.DictReader(io.StringIO(body)))
    assert row["finding_title"].startswith("'@")
    assert row["finding_path"].startswith("'=")
    assert row["pr_author"].startswith("'+")
    # Harmless values are untouched.
    assert row["outcome"] == "unobserved"


def test_json_export_is_not_formula_escaped(store: IndexStore) -> None:
    """The apostrophe is a spreadsheet workaround; JSON must stay faithful."""
    _finding(store, "f1", title="=DANGER")
    store.record_rule_evaluations([_evaluation("f1")])

    body, _media = analytics.export_rule_evaluations(
        owner="acme", repo="app", filters={"rule_id": 1}, fmt="json"
    )
    assert json.loads(body)["evaluations"][0]["finding_title"] == "=DANGER"


def test_rule_pagination_covers_every_repository_page(isolated_index: Path) -> None:
    """Merged pagination must not stop at one page per repository.

    Regression guard for a per-repo cap that both dropped later pages and
    reported a total smaller than reality.
    """
    from mira.feedback import analytics as analytics_module

    monkey_cap = 3
    original = analytics_module._MERGE_PAGE_SIZE
    analytics_module._MERGE_PAGE_SIZE = monkey_cap
    try:
        store = IndexStore.open("acme", "app")
        try:
            for rule_id in range(1, 11):
                _finding(store, f"f{rule_id}")
                store.record_rule_evaluations([_evaluation(f"f{rule_id}", rule_id=rule_id)])
        finally:
            store.close()

        rows, total = analytics_module.list_rule_analytics(limit=100)
        assert total == 10
        assert {row.rule_id for row in rows} == set(range(1, 11))

        # Later pages are reachable and disjoint.
        first, _ = analytics_module.list_rule_analytics(limit=4, offset=0)
        second, _ = analytics_module.list_rule_analytics(limit=4, offset=4)
        third, _ = analytics_module.list_rule_analytics(limit=4, offset=8)
        seen = [row.rule_id for row in first + second + third]
        assert len(seen) == 10
        assert len(set(seen)) == 10
    finally:
        analytics_module._MERGE_PAGE_SIZE = original


def test_summary_limit_is_global_not_per_repository(isolated_index: Path) -> None:
    """A bucket ranked second everywhere can still be the global winner.

    Applying the caller's limit inside each repository would drop it from every
    result set and lose it entirely.
    """
    # In each repo `runner-up` is second locally, but it wins overall.
    layout = {
        "one": {"winner-a": 5, "runner-up": 4},
        "two": {"winner-b": 5, "runner-up": 4},
    }
    for repo, categories in layout.items():
        store = IndexStore.open("acme", repo)
        try:
            for category, count in categories.items():
                for index in range(count):
                    finding_id = f"{category}-{index}"
                    _finding(store, finding_id, category=category)
                    store.record_rule_evaluations(
                        [
                            _evaluation(
                                finding_id,
                                rule_id=1,
                                category=category,
                                repo=repo,
                                head_sha=f"{repo}-{category}-{index}",
                            )
                        ]
                    )
        finally:
            store.close()

    top = analytics.summarize(dimension="category", limit=1)
    assert [bucket["bucket"] for bucket in top] == ["runner-up"]
    assert top[0]["exposures"] == 8


def test_audit_listing_spans_repositories(isolated_index: Path) -> None:
    for repo in ("one", "two"):
        analytics.record_audit_event(
            owner="acme", repo=repo, event_type="regression_dismissed", rule_id=1
        )

    org_wide = analytics.list_audit_events()
    assert {event["repo"] for event in org_wide} == {"one", "two"}
    assert len(analytics.list_audit_events(owner="acme", repo="one")) == 1


# ------------------------------------------------------- scope attribution


def _exposed(scope_type: str, scope_value: str, *, category: str = "") -> ExposedRule:
    return ExposedRule(
        rule_id=1,
        version=1,
        origin="learned",
        scope_type=scope_type,
        scope_value=scope_value,
        category=category,
        rule_text="Rule text.",
    )


def _build(exposed, findings, scope=None):
    return build_rule_evaluations(
        [exposed],
        platform="github",
        owner="acme",
        repo="app",
        pr_number=7,
        pr_author="alice",
        head_sha="head",
        findings=findings,
        scope=scope,
    )


def test_symbol_scope_only_attributes_files_carrying_the_symbol(store: IndexStore) -> None:
    """Retrieval matches if *any* file has the symbol; attribution must not.

    Otherwise a symbol-scoped rule collects feedback from every unrelated
    finding in the review and its score stops meaning anything.
    """
    findings = [
        SimpleNamespace(finding_id="here", path="src/auth.py", category="security"),
        SimpleNamespace(finding_id="elsewhere", path="src/views.py", category="security"),
    ]
    scope = ReviewScope(symbols={"src/auth.py": {"validate_token"}, "src/views.py": {"render"}})

    linked = {e.finding_id for e in _build(_exposed("symbol", "validate_token"), findings, scope)}
    assert linked == {None, "here"}


def test_language_scope_only_attributes_files_in_that_language() -> None:
    findings = [
        SimpleNamespace(finding_id="py", path="a.py", category="style"),
        SimpleNamespace(finding_id="ts", path="a.ts", category="style"),
    ]
    scope = ReviewScope(languages={"a.py": "Python", "a.ts": "TypeScript"})

    linked = {e.finding_id for e in _build(_exposed("language", "python"), findings, scope)}
    assert linked == {None, "py"}


def test_language_and_symbol_scopes_fail_closed_without_metadata() -> None:
    """A missing lookup must not become a link.

    The review-scoped row still records that the rule ran, so failing closed
    loses an attribution but never the exposure; failing open would silently
    corrupt the score.
    """
    findings = [SimpleNamespace(finding_id="a", path="a.py", category="style")]

    for scope_type, value in (("symbol", "validate_token"), ("language", "python")):
        linked = {e.finding_id for e in _build(_exposed(scope_type, value), findings, None)}
        assert linked == {None}, scope_type


def test_repo_scope_still_covers_the_whole_review() -> None:
    findings = [
        SimpleNamespace(finding_id="a", path="a.py", category="style"),
        SimpleNamespace(finding_id="b", path="b.ts", category="style"),
    ]
    linked = {e.finding_id for e in _build(_exposed("repo", ""), findings, ReviewScope())}
    assert linked == {None, "a", "b"}


def test_summary_walks_high_cardinality_dimensions(isolated_index: Path) -> None:
    """`author` has no small domain, so a fixed per-repo cap loses buckets.

    Each repository here has many one-off authors that outrank `shared`
    locally, but `shared` appears in both and wins globally. A capped
    single-page read would drop it from both result sets.
    """
    from mira.feedback import analytics as analytics_module

    original = analytics_module._SUMMARY_PAGE_SIZE
    analytics_module._SUMMARY_PAGE_SIZE = 2
    try:
        for repo in ("one", "two"):
            store = IndexStore.open("acme", repo)
            try:
                for index in range(5):
                    finding_id = f"solo-{index}"
                    _finding(store, finding_id)
                    store.record_rule_evaluations(
                        [
                            _evaluation(
                                finding_id,
                                repo=repo,
                                pr_author=f"solo-{repo}-{index}",
                                head_sha=f"{repo}-solo-{index}",
                            )
                        ]
                    )
                # `shared` gets a single exposure per repo — last locally,
                # but present in both.
                _finding(store, "shared")
                store.record_rule_evaluations(
                    [
                        _evaluation(
                            "shared",
                            repo=repo,
                            pr_author="shared",
                            head_sha=f"{repo}-shared",
                        )
                    ]
                )
            finally:
                store.close()

        buckets = {b["bucket"]: b for b in analytics_module.summarize(dimension="author")}
        assert "shared" in buckets
        assert buckets["shared"]["exposures"] == 2
        # And the whole domain survived the walk: 10 solos plus shared.
        assert len(buckets) == 11
    finally:
        analytics_module._SUMMARY_PAGE_SIZE = original


def test_summary_paging_is_stable(store: IndexStore) -> None:
    """Offset paging needs a deterministic order, including on ties."""
    for index in range(6):
        _finding(store, f"f{index}")
        store.record_rule_evaluations([_evaluation(f"f{index}", pr_author=f"user{index}")])

    walked: list[str] = []
    for offset in (0, 2, 4):
        page = store.rule_analytics_summary(dimension="author", limit=2, offset=offset)
        walked.extend(bucket["bucket"] for bucket in page)

    assert len(walked) == 6
    assert len(set(walked)) == 6


# ------------------------------------------------ review-round 3 regressions


def test_engine_clears_exposures_between_reviews() -> None:
    """A reused engine must not attribute one PR's rules to another.

    The evaluation key is built from the *new* review's identity, so a stale
    snapshot would record as a perfectly legitimate-looking exposure.
    """
    from mira.core.engine import ReviewEngine

    engine = ReviewEngine.__new__(ReviewEngine)
    engine._exposed_rules = [
        ExposedRule(
            rule_id=99,
            version=1,
            origin="learned",
            scope_type="repo",
            scope_value="",
            category="",
            rule_text="Stale.",
        )
    ]
    engine._review_scope = ReviewScope(languages={"a.py": "Python"})

    engine._reset_exposures()

    assert engine._exposed_rules == []
    assert engine._review_scope.languages == {}


def test_path_scope_matching_is_case_insensitive(store: IndexStore) -> None:
    """SQLite folds LIKE case and Postgres does not; both must agree."""
    now = time.time()
    pivot = now - 60 * 86400
    rule = store.create_learned_rule(
        "Skip generated code.",
        "style",
        path_pattern="Generated/**",
        scope_type="path",
        scope_value="Generated/**",
    )
    store._conn.execute(
        "UPDATE learned_rules SET effective_from = ? WHERE id = ?", (pivot, rule.id)
    )
    store._conn.commit()
    _finding(store, "lower", path="generated/api.py", category="style", created_at=pivot + 100)

    result = analytics.compare_activation_periods(owner="acme", repo="app", rule_id=rule.id)
    assert result["after"]["findings"] == 1


def test_sqlite_platform_resolution_prefers_github(isolated_index: Path) -> None:
    """`_iter_repo_dbs` visits `_gitlab/` first; ranking must still pick GitHub."""
    for platform in ("gitlab", "github"):
        store = IndexStore.open("acme", "app", platform=platform)
        store.close()

    assert analytics._platform_for("acme", "app") == "github"


def test_regression_needs_decisive_signals_not_just_exposures() -> None:
    """Exposures alone are not evidence of a regression.

    A rule can clear the exposure floor on review-scoped rows and then reach a
    100% negative rate from one thumbs-down; that must not read as "disable".
    """
    row = RuleAnalyticsRow(
        rule_id=1,
        owner="acme",
        repo="app",
        counts=RuleOutcomeCounts(exposures=30, review_exposures=29, findings=1, negative=1),
    )
    assert (
        detect_regression(
            row,
            min_exposures=20,
            negative_rate_threshold=0.5,
            disable_rate_threshold=0.8,
            min_decisive=5,
        )
        is None
    )
    # With enough people actually objecting, it is flagged.
    row.counts.negative = 6
    row.counts.findings = 6
    assert (
        detect_regression(
            row,
            min_exposures=20,
            negative_rate_threshold=0.5,
            disable_rate_threshold=0.8,
            min_decisive=5,
        )
        is not None
    )


def test_analytics_routes_reject_path_traversal() -> None:
    """owner/repo reach a filesystem path, so they are validated as segments."""
    from fastapi import HTTPException

    import mira.dashboard.routers.analytics as routes

    admin = SimpleNamespace(
        state=SimpleNamespace(user=SimpleNamespace(is_admin=True, username="root"))
    )
    for owner, repo in (("..", "app"), ("acme", ".."), ("a/b", "app"), ("acme", "a/b")):
        with pytest.raises(HTTPException) as exc:
            routes.rule_analytics_detail(admin, owner, repo, 1)
        assert exc.value.status_code == 400, (owner, repo)


def test_namespaced_owner_is_accepted() -> None:
    """The stores emit `_{platform}/{owner}` themselves, so it must pass."""
    from mira.dashboard.routers.analytics import _NAMESPACED_OWNER, _public_owner

    assert _NAMESPACED_OWNER.match("_gitlab/acme")
    assert _public_owner("_gitlab/acme") == "acme"
    # But it cannot be used to smuggle a traversal through.
    assert not _NAMESPACED_OWNER.match("_gitlab/../etc")


def test_unregistered_repo_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    from fastapi import HTTPException

    import mira.dashboard.routers.analytics as routes

    class _Empty:
        def get_repo_any_platform(self, owner: str, repo: str):
            return []

    monkeypatch.setattr("mira.dashboard.api._app_db", _Empty())
    admin = SimpleNamespace(
        state=SimpleNamespace(user=SimpleNamespace(is_admin=True, username="root"))
    )
    with pytest.raises(HTTPException) as exc:
        routes.rule_analytics_detail(admin, "acme", "app", 1)
    assert exc.value.status_code == 404


def test_partial_repo_filters_are_allowed(store: IndexStore, monkeypatch) -> None:
    """`owner=` alone and `repo=` alone are supported filters, not bad input.

    Regression guard: the traversal check initially required both segments and
    rejected every owner-only or repo-only query with a 400.
    """
    import mira.dashboard.routers.analytics as routes

    _finding(store, "f1")
    store.record_rule_evaluations([_evaluation("f1")])

    class _Registry:
        def get_repo_any_platform(self, owner: str, repo: str):
            return [SimpleNamespace(platform="github")]

    monkeypatch.setattr("mira.dashboard.api._app_db", _Registry())
    admin = SimpleNamespace(
        state=SimpleNamespace(user=SimpleNamespace(is_admin=True, username="root"))
    )

    assert routes.list_rule_analytics(admin, owner="acme").total == 1
    assert routes.list_rule_analytics(admin, repo="app").total == 1
    assert routes.list_rule_analytics(admin, owner="acme", repo="app").total == 1
    assert routes.analytics_summary(admin, owner="acme").buckets
    assert routes.list_regressions(admin, owner="acme").min_exposures > 0
    assert routes.list_audit_events(admin, owner="acme").events == []


def test_partial_repo_filters_still_reject_traversal(monkeypatch) -> None:
    from fastapi import HTTPException

    import mira.dashboard.routers.analytics as routes

    admin = SimpleNamespace(
        state=SimpleNamespace(user=SimpleNamespace(is_admin=True, username="root"))
    )
    for owner, repo in (("..", ""), ("", ".."), ("a/b", ""), ("", "a/b")):
        with pytest.raises(HTTPException) as exc:
            routes.list_rule_analytics(admin, owner=owner, repo=repo)
        assert exc.value.status_code == 400, (owner, repo)
