"""Backtests: replaying Mira over merged pull requests, safely and within budget."""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from click.testing import CliRunner

from mira.config import MiraConfig
from mira.llm.provider import LLMProvider
from mira.models import (
    CommitInfo,
    HumanReviewComment,
    PRInfo,
    ReviewComment,
    ReviewResult,
    Severity,
)
from mira.quality.backtest import (
    BacktestRunner,
    Variant,
    estimate_review_cost,
    select_prs,
)
from mira.quality.config_models import BacktestConfig
from mira.quality.ground_truth import classify_commit_message, fix_commit_signals
from mira.quality.lines import changed_lines, ranges_overlap, to_ranges
from mira.quality.models import (
    LABEL_FP,
    LABEL_TP,
    LABEL_UNLABELLED,
    SIGNAL_FEEDBACK_NEGATIVE,
    SIGNAL_FIX_COMMIT,
    SIGNAL_HUMAN_COMMENT,
    Signal,
)
from mira.quality.readonly import ReadOnlyProvider, WriteBlocked, blocked_base_methods
from mira.quality.report import render_json, render_markdown
from mira.quality.scoring import score_findings
from mira.quality.store import open_quality_store
from tests.quality_support import DAY, FakeHistoryProvider, file_diff, merged_pr

MERGED_AT = 1_780_000_000.0


def _pr_info() -> PRInfo:
    return PRInfo(
        title="t",
        description="",
        base_branch="main",
        head_branch="f",
        url="https://github.com/acme/app/pull/1",
        number=1,
        owner="acme",
        repo="app",
    )


def _comment(path: str, line: int, title: str = "issue") -> ReviewComment:
    return ReviewComment(
        path=path,
        line=line,
        end_line=None,
        severity=Severity.WARNING,
        category="bug",
        title=title,
        body="b",
        confidence=0.9,
    )


# ───────────────────────────────────────────────────── read-only provider ──


async def test_every_base_write_is_blocked_and_never_reaches_the_provider() -> None:
    inner = MagicMock()
    guard = ReadOnlyProvider(inner)
    blocked = blocked_base_methods()
    assert {"post_review", "post_comment", "submit_verdict", "commit_files"} <= set(blocked)
    for name in blocked:
        result = getattr(guard, name)(_pr_info(), "x")
        if hasattr(result, "__await__"):
            await result
    assert not inner.method_calls, "a blocked write reached the wrapped provider"
    assert {c.method for c in guard.blocked_calls} == set(blocked)
    with pytest.raises(WriteBlocked):
        guard.assert_no_writes()


async def test_reads_pass_through_and_unknown_methods_fail_closed() -> None:
    inner = FakeHistoryProvider(diffs={1: "DIFF"})
    inner.some_new_write = AsyncMock()  # type: ignore[attr-defined]
    guard = ReadOnlyProvider(inner)
    assert await guard.get_pr_diff(_pr_info()) == "DIFF"
    await guard.some_new_write(_pr_info())  # type: ignore[attr-defined]
    inner.some_new_write.assert_not_called()  # type: ignore[attr-defined]
    assert [c.method for c in guard.blocked_calls] == ["some_new_write"]
    assert await guard.post_review(_pr_info(), ReviewResult()) == []
    assert inner.writes == []


# ─────────────────────────────────────────────────────────── line helpers ──


def test_changed_lines_reports_both_sides_and_insertion_points() -> None:
    diff = (
        "diff --git a/a.py b/a.py\n--- a/a.py\n+++ b/a.py\n"
        "@@ -10,4 +10,5 @@\n ctx\n-old\n+new\n ctx2\n+inserted\n ctx3\n"
    )
    lines = changed_lines(diff)["a.py"]
    assert lines.removed >= {11}
    assert lines.added == {11, 13}
    # The pure insertion after `ctx2` (old line 12) points at old lines 12/13.
    assert {12, 13} <= lines.removed


def test_ranges_and_overlap() -> None:
    assert to_ranges({5, 1, 2, 3, 9}) == [(1, 3), (5, 5), (9, 9)]
    assert ranges_overlap(10, 10, 12, 14, tolerance=2)
    assert not ranges_overlap(10, 10, 14, 15, tolerance=2)


def test_changed_lines_is_empty_for_garbage() -> None:
    assert changed_lines("not a diff") == {}
    assert changed_lines("") == {}


def test_commit_messages_are_classified() -> None:
    assert classify_commit_message('Revert "add cache"') == "revert"
    assert classify_commit_message("x\n\nThis reverts commit abcdef1.") == "revert"
    assert classify_commit_message("fix: null deref in parser") == "fix_commit"
    assert classify_commit_message("Add a feature") == ""


# ──────────────────────────────────────────────────────────────── scoring ──


def test_scoring_labels_and_recall() -> None:
    signals = [
        Signal(kind=SIGNAL_HUMAN_COMMENT, path="a.py", start=10, end=10),
        Signal(kind=SIGNAL_FIX_COMMIT, path="a.py", start=50, end=52),
        Signal(kind=SIGNAL_FEEDBACK_NEGATIVE, path="b.py", start=5, end=5, positive=False),
    ]
    comments = [_comment("a.py", 11), _comment("b.py", 5), _comment("c.py", 1)]
    scored, score = score_findings(comments, signals, tolerance=2)
    assert [f.label for f in scored] == [LABEL_TP, LABEL_FP, LABEL_UNLABELLED]
    assert (score.tp, score.fp, score.unlabelled) == (1, 1, 1)
    assert score.positives == 2 and score.positives_caught == 1
    assert score.recall == pytest.approx(0.5)
    assert score.precision == pytest.approx(1 / 3)
    assert score.labelled_precision == pytest.approx(0.5)


def test_no_ground_truth_reports_no_precision() -> None:
    _scored, score = score_findings([_comment("a.py", 1)], [], tolerance=0)
    assert score.precision is None and score.recall is None


# ──────────────────────────────────────────────────────── selection/plan ──


async def test_select_prs_by_number_reports_unmerged_ones() -> None:
    provider = FakeHistoryProvider(merged={3: merged_pr(3, merged_at=MERGED_AT)})
    prs, notes = await select_prs(provider, _pr_info(), numbers=[3, 4], limit=10)
    assert [p.number for p in prs] == [3]
    assert "#4" in notes[0]


def test_estimate_scales_with_diff_size_and_model_price() -> None:
    config = MiraConfig()
    small = estimate_review_cost(1_000, config)
    big = estimate_review_cost(100_000, config)
    assert 0 < small < big


# ───────────────────────────────────────────── end-to-end with the engine ──


def _mock_llm(sample_llm_response_text: str) -> Any:
    llm = MagicMock(spec=LLMProvider)
    llm.walkthrough = AsyncMock(
        return_value=json.dumps({"summary": "s", "change_groups": [], "sequence_diagram": None})
    )
    llm.review = AsyncMock(return_value=sample_llm_response_text)
    llm.complete = AsyncMock(return_value=sample_llm_response_text)
    llm.count_tokens = MagicMock(return_value=100)
    llm.usage = {"prompt_tokens": 1000, "completion_tokens": 200, "total_tokens": 1200}
    llm.config = SimpleNamespace(model="anthropic/claude-sonnet-4-6")
    return llm


def _history(sample_diff_text: str) -> FakeHistoryProvider:
    fix_sha = "f1x0000000000000000000000000000000000000"
    return FakeHistoryProvider(
        merged={7: merged_pr(7, title="Add utils", merged_at=MERGED_AT)},
        diffs={7: sample_diff_text},
        human_comments={
            7: [
                HumanReviewComment(
                    path="src/utils.py", line=16, body="eval on file input is unsafe", author="dana"
                ),
                HumanReviewComment(path="src/utils.py", line=3, body="LGTM", author="eve"),
            ]
        },
        path_commits={
            "src/utils.py": [
                CommitInfo(sha=fix_sha, message="fix: drop default API key", date=MERGED_AT + DAY),
                CommitInfo(sha="feat1", message="add more helpers", date=MERGED_AT + DAY),
            ]
        },
        commit_diffs={
            fix_sha: file_diff(
                "src/utils.py",
                21,
                ['    return os.environ.get("API_KEY", "sk-default-key-12345")'],
                21,
                ['    return os.environ.get("API_KEY", "")'],
            )
        },
    )


async def test_end_to_end_replay_scores_and_never_writes(
    sample_diff_text: str, sample_llm_response_text: str
) -> None:
    provider = _history(sample_diff_text)
    llm = _mock_llm(sample_llm_response_text)
    with open_quality_store("acme", "app") as store:
        runner = BacktestRunner(
            provider,
            [Variant("A", "default", MiraConfig())],
            owner="acme",
            repo="app",
            platform="github",
            limits=BacktestConfig(max_estimated_cost_usd=10.0),
            llm_factory=lambda _config: (llm, llm, llm),
            quality_store=store,
        )
        plan = await runner.plan(numbers=[7])
        assert plan.estimated_cost_usd > 0 and not runner.over_budget(plan)
        run = await runner.run(plan)

        assert provider.writes == []
        [result] = run.results
        assert result.error == "", result.error
        assert result.blocked_writes == 0
        kinds = {s.kind for s in result.signals}
        assert kinds == {SIGNAL_HUMAN_COMMENT, SIGNAL_FIX_COMMIT}  # LGTM is not evidence
        labels = {(f.line, f.label) for f in result.findings}
        assert (16, LABEL_TP) in labels
        assert (21, LABEL_TP) in labels
        assert result.score.positives_caught == result.score.positives == 2
        assert result.prompt_tokens == 1000 and result.cost_usd > 0
        assert run.status == "completed"

        stored = store.get_backtest_run(run.id)
        assert stored is not None and stored["status"] == "completed"
        assert stored["results"][0]["score"]["tp"] == result.score.tp
        assert store.list_backtest_runs()[0]["id"] == run.id

    markdown = render_markdown(run)
    assert "Mira backtest — acme/app" in markdown and "#7" in markdown
    assert json.loads(render_json(run))["results"][0]["pr_number"] == 7


# ───────────────────────────────────────────────── A/B, budget, failures ──


class _FakeEngine:
    def __init__(self, comments: list[ReviewComment]) -> None:
        self.comments = comments

    async def review_diff(self, diff_text: str, **kwargs: Any) -> ReviewResult:
        return ReviewResult(comments=list(self.comments))


def _clients(prompt: int = 10_000, completion: int = 1_000) -> tuple[Any, Any, Any]:
    client = SimpleNamespace(
        usage={"prompt_tokens": prompt, "completion_tokens": completion},
        config=SimpleNamespace(model="anthropic/claude-sonnet-4-6"),
    )
    return client, client, client


def _ab_provider() -> FakeHistoryProvider:
    diff = file_diff("a.py", 1, [], 1, [f"line {i}" for i in range(1, 30)])
    return FakeHistoryProvider(
        merged={n: merged_pr(n, merged_at=MERGED_AT + n) for n in (1, 2)},
        diffs={1: diff, 2: diff},
        human_comments={
            n: [HumanReviewComment(path="a.py", line=10, body="off by one here", author="x")]
            for n in (1, 2)
        },
    )


async def test_ab_comparison_reports_both_configurations() -> None:
    config_a, config_b = MiraConfig(), MiraConfig()
    config_b.filter.confidence_threshold = 0.9

    def engine_factory(config: Any, clients: Any, provider: Any) -> Any:
        assert isinstance(provider, ReadOnlyProvider)
        if config is config_b:
            return _FakeEngine([_comment("a.py", 10)])
        return _FakeEngine([_comment("a.py", 10), _comment("a.py", 25)])

    runner = BacktestRunner(
        _ab_provider(),
        [Variant("A", "a.yaml", config_a), Variant("B", "b.yaml", config_b)],
        owner="acme",
        repo="app",
        platform="github",
        limits=BacktestConfig(max_estimated_cost_usd=100.0, store_results=False),
        llm_factory=lambda _c: _clients(),
        engine_factory=engine_factory,
    )
    run = await runner.run(await runner.plan(limit=5))
    summaries = {s.variant: s for s in run.summaries()}
    assert summaries["A"].score.findings == 4 and summaries["B"].score.findings == 2
    assert summaries["A"].score.recall == summaries["B"].score.recall == 1.0
    assert summaries["B"].score.precision == 1.0
    assert summaries["A"].score.precision == pytest.approx(0.5)
    markdown = render_markdown(run)
    assert "A/B comparison" in markdown and "+50 pts" in markdown


async def test_spend_limit_stops_new_reviews() -> None:
    reviewed: list[int] = []

    def engine_factory(config: Any, clients: Any, provider: Any) -> Any:
        engine = _FakeEngine([])
        original = engine.review_diff

        async def review_diff(diff_text: str, **kwargs: Any) -> ReviewResult:
            reviewed.append(kwargs["repo_scope"].number)
            return await original(diff_text, **kwargs)

        engine.review_diff = review_diff  # type: ignore[method-assign]
        return engine

    runner = BacktestRunner(
        _ab_provider(),
        [Variant("A", "default", MiraConfig())],
        owner="acme",
        repo="app",
        platform="github",
        # One review at these token counts costs far more than a cent.
        limits=BacktestConfig(max_estimated_cost_usd=0.01, max_concurrency=1, store_results=False),
        llm_factory=lambda _c: _clients(prompt=1_000_000),
        engine_factory=engine_factory,
    )
    plan = await runner.plan(limit=5)
    run = await runner.run(plan)
    assert len(reviewed) == 1
    assert run.status == "stopped"
    assert [r.skipped_reason for r in run.results].count("spend limit reached") == 1


async def test_over_budget_plan_is_detected() -> None:
    runner = BacktestRunner(
        _ab_provider(),
        [Variant("A", "default", MiraConfig())],
        owner="acme",
        repo="app",
        platform="github",
        limits=BacktestConfig(max_estimated_cost_usd=0.0),
    )
    assert runner.over_budget(await runner.plan(limit=5))


async def test_a_failing_review_is_recorded_not_raised() -> None:
    def engine_factory(config: Any, clients: Any, provider: Any) -> Any:
        engine = MagicMock()
        engine.review_diff = AsyncMock(side_effect=RuntimeError("model down"))
        return engine

    runner = BacktestRunner(
        _ab_provider(),
        [Variant("A", "default", MiraConfig())],
        owner="acme",
        repo="app",
        platform="github",
        limits=BacktestConfig(store_results=False),
        llm_factory=lambda _c: _clients(prompt=0, completion=0),
        engine_factory=engine_factory,
    )
    run = await runner.run(await runner.plan(limit=5))
    assert all("model down" in r.error for r in run.results)
    assert run.summaries()[0].errors == 2


async def test_stored_feedback_is_ground_truth() -> None:
    from mira.feedback.models import FeedbackEventV2, ReviewFinding

    provider = _ab_provider()
    with open_quality_store("acme", "app") as store:
        index = store.index_store
        for fid, line, kind in (("f-up", 10, "thumbs_up"), ("f-down", 20, "thumbs_down")):
            index.save_review_finding(
                ReviewFinding(
                    id=fid,
                    fingerprint=fid,
                    review_id=1,
                    platform="github",
                    owner="acme",
                    repo="app",
                    pr_number=1,
                    pr_url="",
                    base_sha="",
                    head_sha="",
                    path="a.py",
                    start_line=line,
                    end_line=line,
                    symbol="",
                    category="bug",
                    severity="warning",
                    confidence=0.9,
                    title="t",
                    body="b",
                    suggestion="",
                    detector="main",
                    prompt_model="",
                )
            )
            index.record_feedback_v2(
                FeedbackEventV2(
                    id=0,
                    finding_id=fid,
                    kind=kind,
                    actor="dana",
                    actor_role="",
                    raw_text="",
                    rationale="",
                    platform="github",
                    source_event_id=f"src-{fid}",
                    head_sha="",
                    thread_state="",
                    provenance_complete=False,
                )
            )
        runner = BacktestRunner(
            provider,
            [Variant("A", "default", MiraConfig())],
            owner="acme",
            repo="app",
            platform="github",
            limits=BacktestConfig(store_results=False),
            llm_factory=lambda _c: _clients(prompt=0, completion=0),
            engine_factory=lambda *_a: _FakeEngine([_comment("a.py", 20)]),
            quality_store=store,
        )
        run = await runner.run(await runner.plan(numbers=[1]))
    [result] = run.results
    kinds = {s.kind for s in result.signals}
    assert "feedback_positive" in kinds and SIGNAL_FEEDBACK_NEGATIVE in kinds
    assert result.findings[0].label == LABEL_FP


# ──────────────────────────────────────────────────────────────────── CLI ──


def test_cli_prints_a_plan_and_runs_nothing_without_confirm(monkeypatch: Any) -> None:
    from mira.cli import main

    provider = _ab_provider()
    monkeypatch.setattr("mira.quality.cli._provider", lambda platform, token: provider)

    def _explode(*_a: Any, **_k: Any) -> Any:
        raise AssertionError("no model may be built without --confirm")

    monkeypatch.setattr("mira.quality.backtest.default_llm_factory", _explode)
    result = CliRunner().invoke(
        main, ["backtest", "--repo", "acme/app", "--prs", "1,2", "--token", "t", "--no-save"]
    )
    assert result.exit_code == 0, result.output
    assert "Backtest plan: 2 pull request(s)" in result.output
    assert "--confirm" in result.output
    assert provider.writes == []


def test_cli_refuses_a_plan_over_the_cost_limit(monkeypatch: Any) -> None:
    from mira.cli import main

    monkeypatch.setattr("mira.quality.cli._provider", lambda platform, token: _ab_provider())
    result = CliRunner().invoke(
        main,
        [
            "backtest",
            "--repo",
            "acme/app",
            "--prs",
            "1",
            "--token",
            "t",
            "--max-cost",
            "0",
            "--confirm",
            "--no-save",
        ],
    )
    assert result.exit_code != 0
    assert "exceeds" in result.output


def test_backtest_config_is_bounded() -> None:
    with pytest.raises(ValueError):
        BacktestConfig(max_prs=0)
    assert MiraConfig().backtest.max_prs == 25


# ──────────────────────────────────────────────── review-bot regressions ──


async def _fix_signals(provider: FakeHistoryProvider, merged: Any, diff: str) -> list[Signal]:
    return await fix_commit_signals(
        provider,
        _pr_info(),
        merged,
        diff,
        window_days=30,
        max_files=5,
        max_commits=10,
        notes=[],
        line_tolerance=3,
    )


async def test_fix_commit_far_from_the_prs_lines_is_not_evidence() -> None:
    pr_diff = file_diff("a.py", 9, [], 10, ["x1", "x2", "x3"])  # adds lines 10-12
    provider = FakeHistoryProvider(
        path_commits={
            "a.py": [
                CommitInfo(sha="near", message="fix: off by one", date=MERGED_AT + DAY),
                CommitInfo(sha="far", message="fix: unrelated typo", date=MERGED_AT + DAY),
            ]
        },
        commit_diffs={
            "near": file_diff("a.py", 11, ["x2"], 11, ["y2"]),
            "far": file_diff("a.py", 200, ["old"], 200, ["new"]),
        },
    )
    merged = merged_pr(7, merged_at=MERGED_AT)
    signals = await _fix_signals(provider, merged, pr_diff)
    assert {s.ref for s in signals} == {"near"}
    assert all(s.start >= 7 and s.end <= 15 for s in signals)


async def test_the_prs_own_commit_is_skipped_when_either_sha_is_abbreviated() -> None:
    pr_diff = file_diff("a.py", 9, [], 10, ["x1"])
    provider = FakeHistoryProvider(
        path_commits={
            "a.py": [
                CommitInfo(sha="abcdef1", message="fix: merge #7", date=MERGED_AT + 1),
                CommitInfo(sha="", message="fix: no sha", date=MERGED_AT + 1),
            ]
        },
        commit_diffs={
            "abcdef1": file_diff("a.py", 10, ["x1"], 10, ["x1!"]),
            "": file_diff("a.py", 10, ["x1"], 10, ["x1?"]),
        },
    )
    merged = merged_pr(7, merged_at=MERGED_AT, merge_commit_sha="abcdef1234567890", head_sha="")
    signals = await _fix_signals(provider, merged, pr_diff)
    assert {s.ref for s in signals} == {""}  # the own commit is skipped; an empty sha is not


async def test_blocked_writes_return_values_of_their_declared_type() -> None:
    import inspect as _inspect

    from mira.providers.base import BaseProvider

    guard = ReadOnlyProvider(MagicMock())
    assert await guard.commit_files(_pr_info(), {}) == ""
    assert await guard.create_pull_request(_pr_info()) == (0, "")
    for name in blocked_base_methods():
        declared = _inspect.signature(getattr(BaseProvider, name)).return_annotation
        if declared in (None, "None"):
            continue
        result = getattr(guard, name)(_pr_info())
        if hasattr(result, "__await__"):
            result = await result
        assert result is not None, f"blocked {name}() returns None but promises {declared}"


async def test_unknown_members_mirror_sync_and_async_and_missing_ones_raise() -> None:
    class Inner(FakeHistoryProvider):
        def sync_write(self, value: int) -> int:
            self.writes.append("sync_write")
            return value

        async def async_write(self) -> None:
            self.writes.append("async_write")

    inner = Inner()
    guard = ReadOnlyProvider(inner)
    result = guard.sync_write(1)  # type: ignore[attr-defined]
    assert not _is_awaitable(result)
    await guard.async_write()  # type: ignore[attr-defined]
    assert [c.method for c in guard.blocked_calls] == ["sync_write", "async_write"]
    assert inner.writes == []
    assert not hasattr(guard, "no_such_method")


def _is_awaitable(value: Any) -> bool:
    return hasattr(value, "__await__")


async def test_concurrent_workers_reserve_spend_before_reviewing() -> None:
    import asyncio

    started: list[int] = []

    def engine_factory(config: Any, clients: Any, provider: Any) -> Any:
        engine = _FakeEngine([])

        async def review_diff(diff_text: str, **kwargs: Any) -> ReviewResult:
            started.append(kwargs["repo_scope"].number)
            await asyncio.sleep(0.01)  # both workers are in flight at once
            return ReviewResult(comments=[])

        engine.review_diff = review_diff  # type: ignore[method-assign]
        return engine

    runner = BacktestRunner(
        _ab_provider(),
        [Variant("A", "default", MiraConfig())],
        owner="acme",
        repo="app",
        platform="github",
        limits=BacktestConfig(max_concurrency=2, store_results=False),
        llm_factory=lambda _c: _clients(prompt=1_000_000),
        engine_factory=engine_factory,
    )
    plan = await runner.plan(limit=5)
    one_review = plan.prs[0].estimated_cost_usd["A"]
    assert one_review > 0
    # Room for one review's estimate, not two.
    runner.limits = BacktestConfig(
        max_concurrency=2, store_results=False, max_estimated_cost_usd=one_review * 0.9
    )
    run = await runner.run(plan)
    assert len(started) == 1
    assert run.status == "stopped"
