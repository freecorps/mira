"""The staged review pipeline: context first, at most two thinking calls, never partial."""

from __future__ import annotations

import asyncio
import json
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from mira.config import MiraConfig
from mira.core.engine import ReviewEngine
from mira.core.staged_review import (
    PartOutcome,
    ReviewBudget,
    StagedSettings,
    build_context_pack,
    hedged,
    related_tests,
    review_part,
    run_lookups,
    within,
)
from mira.llm import concurrency
from mira.llm.agentic_tools import AgenticToolExecutor
from mira.llm.tool_schemas import SUBMIT_REVIEW_TOOL, SUBMIT_STAGED_REVIEW_TOOL

SETTINGS = StagedSettings(call_timeout=5.0, degraded_seconds=2.0, followup_max_lookups=4)
# The opt-in second thinking call (`review.followup_call`).
FOLLOWUP = StagedSettings(
    call_timeout=5.0, degraded_seconds=2.0, followup_max_lookups=4, followup_call=True
)
MESSAGES = [
    {"role": "system", "content": "You are a reviewer."},
    {"role": "user", "content": "### `src/a.py`\n+x = 1"},
]


def review_json(*titles: str, need: list | None = None) -> str:
    body: dict = {
        "comments": [
            {
                "path": "src/a.py",
                "line": 1,
                "severity": "warning",
                "category": "bug",
                "title": title,
                "body": f"{title}, shown by the diff.",
                "confidence": 0.9,
                "existing_code": "x = 1",
            }
            for title in titles
        ],
        "summary": "checked",
    }
    if need is not None:
        body["need"] = need
    return json.dumps(body)


class FakeExecutor:
    def __init__(self, delay: float = 0.0) -> None:
        self.calls: list[tuple[str, dict]] = []
        self.delay = delay
        self.active = 0
        self.peak = 0

    async def execute(self, name: str, args: dict) -> str:
        self.calls.append((name, args))
        self.active += 1
        self.peak = max(self.peak, self.active)
        try:
            await asyncio.sleep(self.delay)
        finally:
            self.active -= 1
        return f"result of {name} {args}"


# ── review_part ──


async def test_one_thinking_call_when_the_context_is_enough():
    llm = MagicMock()
    llm.complete_with_tools = AsyncMock(return_value=review_json("Off by one", need=[]))
    outcome = PartOutcome()
    raw = await review_part(
        llm,
        MESSAGES,
        deadline_at=time.monotonic() + 30,
        settings=SETTINGS,
        executor=FakeExecutor(),
        outcome=outcome,
    )
    assert json.loads(raw)["comments"][0]["title"] == "Off by one"
    assert "need" not in json.loads(raw)
    assert outcome.thinking_calls == 1 and not outcome.degraded
    _messages, kwargs = llm.complete_with_tools.call_args.args, llm.complete_with_tools.call_args
    assert kwargs.kwargs["tools"] == [SUBMIT_STAGED_REVIEW_TOOL]
    system = llm.complete_with_tools.call_args.args[0][0]["content"]
    assert "## Context and lookups" in system and "`need`" in system


async def test_requested_lookups_run_together_and_one_follow_up_revises():
    need = [
        {"tool": "find_usages", "symbol": "load", "reason": "callers pass None?"},
        {"tool": "read_file", "path": "src/b.py", "reason": "what b expects"},
        {"tool": "find_usages", "symbol": "load", "reason": "duplicate"},
        {"tool": "made_up", "reason": "ignored"},
    ]
    llm = MagicMock()
    llm.complete_with_tools = AsyncMock(
        side_effect=[review_json("Maybe", need=need), review_json("Confirmed", "New one")]
    )
    executor = FakeExecutor(delay=0.05)
    outcome = PartOutcome()
    raw = await review_part(
        llm,
        MESSAGES,
        deadline_at=time.monotonic() + 120,
        settings=FOLLOWUP,
        executor=executor,
        outcome=outcome,
    )
    assert [c["title"] for c in json.loads(raw)["comments"]] == ["Confirmed", "New one"]
    assert outcome.thinking_calls == 2 and outcome.followup == "used"
    # Deduplicated, unknown tools dropped, run side by side.
    assert [name for name, _ in executor.calls] == ["find_usages", "read_file"]
    assert executor.peak == 2
    first, second = (c.args[0] for c in llm.complete_with_tools.call_args_list)
    # The follow-up repeats the first prompt and adds to its end: one cache prefix.
    assert second[-1]["content"].startswith(MESSAGES[-1]["content"])
    assert "## The lookups you asked for" in second[-1]["content"]
    assert "result of find_usages" in second[-1]["content"]
    assert llm.complete_with_tools.call_args_list[1].kwargs["tools"] == [SUBMIT_REVIEW_TOOL]
    assert first[0]["content"].startswith(MESSAGES[0]["content"])


async def test_by_default_lookups_go_to_verification_not_a_second_call():
    need = [
        {"tool": "find_usages", "symbol": "load", "reason": "callers pass None?"},
        {"tool": "read_file", "path": "src/b.py", "reason": "what b expects"},
    ]
    llm = MagicMock()
    llm.complete_with_tools = AsyncMock(return_value=review_json("Maybe", need=need))
    executor = FakeExecutor()
    outcome = PartOutcome()
    raw = await review_part(
        llm,
        MESSAGES,
        deadline_at=time.monotonic() + 120,
        settings=SETTINGS,
        executor=executor,
        outcome=outcome,
    )
    assert llm.complete_with_tools.await_count == 1 and outcome.thinking_calls == 1
    assert outcome.followup == "evidence"
    assert [label for label, _ in outcome.evidence] == [
        "find_usages(symbol='load')",
        "read_file(path='src/b.py')",
    ]
    assert [c["title"] for c in json.loads(raw)["comments"]] == ["Maybe"]
    system = llm.complete_with_tools.call_args.args[0][0]["content"]
    assert "before your findings are verified" in system


async def test_never_more_than_two_thinking_calls():
    need = [{"tool": "find_definition", "symbol": "load", "reason": "what it returns"}]
    llm = MagicMock()
    # The follow-up asks for more lookups again: they are not run.
    llm.complete_with_tools = AsyncMock(
        side_effect=[review_json("A", need=need), review_json("B", need=need), "never"]
    )
    executor = FakeExecutor()
    outcome = PartOutcome()
    raw = await review_part(
        llm,
        MESSAGES,
        deadline_at=time.monotonic() + 120,
        settings=FOLLOWUP,
        executor=executor,
        outcome=outcome,
    )
    assert llm.complete_with_tools.await_count == 2
    assert outcome.thinking_calls == 2
    assert len(executor.calls) == 1
    assert "need" not in json.loads(raw)


async def test_follow_up_failure_keeps_the_first_reading():
    need = [{"tool": "grep_repo", "pattern": "load\\(", "reason": "callers"}]
    llm = MagicMock()
    llm.complete_with_tools = AsyncMock(
        side_effect=[review_json("First", need=need), RuntimeError("boom")]
    )
    outcome = PartOutcome()
    raw = await review_part(
        llm,
        MESSAGES,
        deadline_at=time.monotonic() + 120,
        settings=FOLLOWUP,
        executor=FakeExecutor(),
        outcome=outcome,
    )
    assert [c["title"] for c in json.loads(raw)["comments"]] == ["First"]
    assert outcome.followup == "kept_first" and not outcome.degraded


async def test_no_follow_up_without_time_for_it():
    need = [{"tool": "grep_repo", "pattern": "x", "reason": "r"}]
    llm = MagicMock()
    llm.complete_with_tools = AsyncMock(return_value=review_json("First", need=need))
    executor = FakeExecutor()
    outcome = PartOutcome()
    await review_part(
        llm,
        MESSAGES,
        deadline_at=time.monotonic() + 8,
        settings=FOLLOWUP,
        executor=executor,
        outcome=outcome,
    )
    assert outcome.followup == "skipped" and executor.calls == []
    assert llm.complete_with_tools.await_count == 1


async def test_a_thinking_call_that_times_out_is_read_once_without_thinking():
    async def slow(*_a, **_kw):
        await asyncio.sleep(10)

    llm = MagicMock()
    llm.complete_with_tools = AsyncMock(side_effect=slow)
    degraded = MagicMock()
    degraded.review = AsyncMock(return_value=review_json("Seen without thinking"))
    outcome = PartOutcome()
    settings = StagedSettings(call_timeout=0.2, degraded_seconds=2.0, followup_max_lookups=4)
    # The thinking call is not even started under the floor; lower it for the test.
    import mira.core.staged_review as module

    original = module._MIN_THINKING_SECONDS
    module._MIN_THINKING_SECONDS = 0.1
    try:
        raw = await review_part(
            llm,
            MESSAGES,
            deadline_at=time.monotonic() + 60,
            settings=settings,
            executor=FakeExecutor(),
            degraded_llm=degraded,
            outcome=outcome,
        )
    finally:
        module._MIN_THINKING_SECONDS = original
    assert json.loads(raw)["comments"][0]["title"] == "Seen without thinking"
    assert outcome.degraded and outcome.thinking_calls == 1
    assert llm.complete_with_tools.await_count == 1  # never re-run from scratch
    # The degraded read gets the plain prompt (no lookup instructions, no `need`).
    assert degraded.review.call_args.args[0] == MESSAGES


async def test_past_the_deadline_the_part_is_still_read_without_thinking():
    llm = MagicMock()
    llm.complete_with_tools = AsyncMock(return_value=review_json("never"))
    degraded = MagicMock()
    degraded.review = AsyncMock(return_value=review_json("Read late"))
    outcome = PartOutcome()
    raw = await review_part(
        llm,
        MESSAGES,
        deadline_at=time.monotonic() - 5,
        settings=SETTINGS,
        degraded_llm=degraded,
        outcome=outcome,
    )
    assert json.loads(raw)["comments"][0]["title"] == "Read late"
    assert outcome.thinking_calls == 0 and outcome.degraded
    llm.complete_with_tools.assert_not_awaited()


@pytest.mark.parametrize("answer", ["", "not json at all", RuntimeError("length")])
async def test_an_unusable_or_failed_answer_degrades(answer):
    llm = MagicMock()
    llm.complete_with_tools = AsyncMock(
        side_effect=answer if isinstance(answer, Exception) else [answer]
    )
    degraded = MagicMock()
    degraded.review = AsyncMock(return_value=review_json("Recovered"))
    outcome = PartOutcome()
    raw = await review_part(
        llm,
        MESSAGES,
        deadline_at=time.monotonic() + 60,
        settings=SETTINGS,
        degraded_llm=degraded,
        outcome=outcome,
    )
    assert json.loads(raw)["comments"][0]["title"] == "Recovered"
    assert outcome.degraded and outcome.errors


async def test_a_failed_degraded_read_raises():
    llm = MagicMock()
    llm.complete_with_tools = AsyncMock(side_effect=RuntimeError("offline"))
    degraded = MagicMock()
    degraded.review = AsyncMock(side_effect=RuntimeError("still offline"))
    with pytest.raises(RuntimeError, match="still offline"):
        await review_part(
            llm,
            MESSAGES,
            deadline_at=time.monotonic() + 60,
            settings=SETTINGS,
            degraded_llm=degraded,
        )


async def test_without_an_executor_there_is_no_need_field():
    llm = MagicMock()
    llm.complete_with_tools = AsyncMock(return_value=review_json())
    await review_part(llm, MESSAGES, deadline_at=None, settings=SETTINGS)
    assert llm.complete_with_tools.call_args.kwargs["tools"] == [SUBMIT_REVIEW_TOOL]
    assert "## Context and lookups" not in llm.complete_with_tools.call_args.args[0][0]["content"]


async def test_lookups_still_running_are_left_out():
    executor = FakeExecutor(delay=5)
    results = await run_lookups(
        executor, [{"tool": "find_usages", "symbol": "x", "reason": "r"}], 4, seconds=0.05
    )
    assert results == [("find_usages(symbol='x')", "[this lookup did not finish in time]")]


# ── hedging ──


async def test_a_silent_call_is_hedged_and_the_first_answer_wins():
    calls = 0
    cancelled = asyncio.Event()

    async def call() -> str:
        nonlocal calls
        calls += 1
        if calls == 1:
            try:
                await asyncio.sleep(10)
            except asyncio.CancelledError:
                cancelled.set()
                raise
        return "second"

    marks: list[bool] = []
    assert await hedged(call, 0.05, on_hedge=lambda: marks.append(True)) == "second"
    assert calls == 2 and marks == [True]
    await asyncio.wait_for(cancelled.wait(), 1)


async def test_a_call_that_has_started_streaming_is_not_hedged():
    calls = 0

    async def call() -> str:
        nonlocal calls
        calls += 1
        concurrency.first_response()  # the stream has begun
        await asyncio.sleep(0.15)
        return "first"

    assert await hedged(call, 0.05) == "first"
    assert calls == 1


async def test_a_hedge_waits_for_the_other_call_when_one_fails():
    calls = 0

    async def call() -> str:
        nonlocal calls
        calls += 1
        if calls == 1:
            await asyncio.sleep(0.1)
            raise RuntimeError("first broke")
        await asyncio.sleep(0.2)
        return "second"

    assert await hedged(call, 0.05) == "second"


# ── budget ──


def test_budget_deadlines_derive_from_what_is_left():
    budget = ReviewBudget(started=100.0, total=300.0)
    assert budget.deadline(0.7) == pytest.approx(310.0)
    assert budget.left(0.9, now=200.0) == pytest.approx(170.0)
    unbounded = ReviewBudget(started=100.0, total=0)
    assert unbounded.deadline(0.7) is None and unbounded.left(0.7) is None


async def test_a_side_stage_never_fails_or_holds_the_review():
    async def slow() -> list:
        await asyncio.sleep(10)
        return ["late"]

    async def broken() -> list:
        raise RuntimeError("broke")

    audit: list = []
    started = time.monotonic()
    assert await within(slow(), 0.05, [], "Slow pass", audit) == []
    assert time.monotonic() - started < 2
    assert await within(broken(), 5, [], "Broken pass", audit) == []
    assert [e["what"] for e in audit] == ["Slow pass", "Broken pass"]


# ── the context pack ──


class SnapshotSource:
    def __init__(self, files: dict[str, str]) -> None:
        self.files = files

    def loaded(self):
        return SimpleNamespace(files=self.files, partial=False)

    async def fetch(self, path: str) -> str | None:
        return self.files.get(path)

    async def tree(self) -> list[str]:
        return list(self.files)


def _diff_file(path: str, start: int, added: list[str]):
    from mira.core.diff_parser import parse_diff

    body = "".join(f"+{line}\n" for line in added)
    text = (
        f"diff --git a/{path} b/{path}\n--- a/{path}\n+++ b/{path}\n"
        f"@@ -{start},0 +{start},{len(added)} @@\n{body}"
    )
    return parse_diff(text).files[0]


REPO = {
    "src/store.py": (
        "def load_config(path):\n    data = read_text(path)\n    return parse_settings(data)\n"
    ),
    "src/text.py": "def read_text(path):\n    with open(path) as fh:\n        return fh.read()\n",
    "src/settings.py": "def parse_settings(data):\n    return dict(line.split('=') for line in data)\n",
    "src/app.py": "from store import load_config\n\ndef main():\n    cfg = load_config('a.ini')\n",
    "tests/test_store.py": "def test_load():\n    assert load_config('x')\n",
}


async def test_the_pack_lists_callers_callees_and_related_tests():
    files = [_diff_file("src/store.py", 2, ["    data = read_text(path)"])]
    source = SnapshotSource(REPO)
    executor = AgenticToolExecutor(source_fetcher=source, repo_tree=list(REPO))
    pack = await build_context_pack(files, executor, char_budget=20_000, seconds=10)
    assert "## Code around this change" in pack.text
    assert "Callers of `load_config`" in pack.text
    assert "`src/app.py:4` in `main`" in pack.text
    assert "`read_text` — `src/text.py`" in pack.text
    assert "tests/test_store.py" in pack.text
    assert pack.symbols == ["load_config"] and "read_text" in pack.callees
    assert pack.tests == ["tests/test_store.py"]


async def test_the_pack_holds_to_its_budget_and_never_raises():
    files = [_diff_file("src/store.py", 2, ["    data = read_text(path)"])]
    executor = AgenticToolExecutor(source_fetcher=SnapshotSource(REPO), repo_tree=list(REPO))
    pack = await build_context_pack(files, executor, char_budget=900, seconds=10)
    assert len(pack.text) <= 900

    broken = MagicMock()
    broken._graph.side_effect = RuntimeError("no graph")
    empty = await build_context_pack(files, broken, char_budget=5000, seconds=1)
    assert empty.text == ""


def test_related_tests_skip_the_prs_own_files():
    files = [_diff_file("src/store.py", 1, ["x = 1"]), _diff_file("tests/test_store.py", 1, ["y"])]
    tree = [
        "tests/test_store.py",
        "tests/store_test.go",
        "src/store.spec.ts",
        "tests/test_other.py",
        "tests/fixtures/store.py",
        "tests/test_store_backup.py",
    ]
    assert related_tests(files, tree) == ["src/store.spec.ts", "tests/store_test.go"]


# ── the engine ──


def _engine_config(**review) -> MiraConfig:
    cfg = MiraConfig()
    cfg.review.walkthrough = False
    cfg.review.security_pass = False
    cfg.review.dependency_overlap = False
    cfg.review.self_critique = False
    cfg.review.include_summary = False
    cfg.review.code_context = False
    for key, value in review.items():
        setattr(cfg.review, key, value)
    return cfg


def _diff(paths: list[str], lines: int = 3) -> str:
    out = []
    for path in paths:
        body = "".join(f"+value_{i} = {i}\n" for i in range(lines))
        out.append(
            f"diff --git a/{path} b/{path}\n--- a/{path}\n+++ b/{path}\n"
            f"@@ -0,0 +1,{lines} @@\n{body}"
        )
    return "".join(out)


def _model(staged_answer) -> MagicMock:
    model = MagicMock()
    model.count_tokens.side_effect = lambda text: (len(text) + 3) // 4
    model.complete_with_tools = AsyncMock(side_effect=staged_answer)
    model.review = AsyncMock(return_value=json.dumps({"comments": [], "summary": "degraded"}))
    model.usage = {}
    return model


async def test_a_part_out_of_time_is_degraded_not_dropped(monkeypatch):
    """No partial review: the part that cannot think in time is read without it."""
    import mira.core.engine as module
    import mira.core.staged_review as staged
    from mira.models import ReviewChunk

    monkeypatch.setattr(staged, "_MIN_THINKING_SECONDS", 0.1)
    monkeypatch.setattr(
        module, "chunk_files", lambda files, **kw: [ReviewChunk([f]) for f in files]
    )

    async def answer(messages, tools, **_kw):
        if "### `src/slow.py`" in messages[-1]["content"]:
            await asyncio.sleep(30)
        return json.dumps({"comments": [], "summary": "thought"})

    model = _model(answer)
    config = _engine_config(call_timeout_seconds=10, degraded_call_seconds=10)
    config.review.time_budget_seconds = 0
    config.review.call_timeout_seconds = 10
    # The thinking call's own bound is what cuts it here.
    monkeypatch.setattr(
        staged.StagedSettings,
        "from_config",
        classmethod(
            lambda cls, review, streams: cls(call_timeout=0.3, degraded_seconds=5, hedge_after=0)
        ),
    )
    result = await ReviewEngine(config=config, llm=model).review_diff(
        _diff(["src/fast.py", "src/slow.py"])
    )
    assert result.skipped_paths == []
    assert sorted(result.reviewed_paths) == ["src/fast.py", "src/slow.py"]
    assert result.reviewed_files == 2
    parts = [e for e in result.audit if e["stage"] == "staged_part"]
    assert sorted(e["degraded"] for e in parts) == [False, True]
    assert not any(e["stage"] in ("chunk_failed", "chunk_retry") for e in result.audit)
    # The slow part was asked once with thinking, then once without: never re-run.
    slow_calls = [
        c
        for c in model.complete_with_tools.call_args_list
        if "### `src/slow.py`" in c.args[0][-1]["content"]
    ]
    assert len(slow_calls) == 1
    assert model.review.await_count == 1


async def test_a_timed_out_agentic_part_is_not_run_again():
    """The fall-through: a part that timed out was re-run after the others finished."""
    calls = 0

    async def review(messages, temperature=None):
        nonlocal calls
        calls += 1
        await asyncio.sleep(5)
        return json.dumps({"comments": []})

    model = MagicMock()
    model.count_tokens.side_effect = lambda text: (len(text) + 3) // 4
    model.review = AsyncMock(side_effect=review)
    model.usage = {}
    config = _engine_config(pipeline="agentic", part_deadline_seconds=1, chunk_retries=1)
    config.review.system_pass = False
    with pytest.raises(TimeoutError):
        await ReviewEngine(config=config, llm=model).review_diff(_diff(["src/a.py"]))
    assert calls == 1


async def test_a_failed_agentic_part_is_not_counted_unread_after_a_retry_lands(monkeypatch):
    import mira.core.engine as module
    from mira.models import ReviewChunk

    monkeypatch.setattr(
        module, "chunk_files", lambda files, **kw: [ReviewChunk([f]) for f in files]
    )
    attempts = 0

    async def review(messages, temperature=None):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("transient")
        return json.dumps({"comments": []})

    model = MagicMock()
    model.count_tokens.side_effect = lambda text: (len(text) + 3) // 4
    model.review = AsyncMock(side_effect=review)
    model.usage = {}
    config = _engine_config(pipeline="agentic", chunk_retries=1)
    result = await ReviewEngine(config=config, llm=model).review_diff(_diff(["src/a.py"]))
    assert attempts == 2
    assert result.skipped_paths == [] and result.reviewed_files == 1


async def test_the_whole_pr_pass_is_one_bounded_call_only_with_several_parts(monkeypatch):
    import mira.core.engine as module
    from mira.models import ReviewChunk

    def is_system(messages):
        return "## Changed files" in messages[-1]["content"]

    async def answer(messages, tools, **_kw):
        return json.dumps({"comments": [], "summary": "ok"})

    # One part: no system pass at all.
    model = _model(answer)
    config = _engine_config(system_pass=True, multipass_min_changed_lines=0)
    result = await ReviewEngine(config=config, llm=model).review_diff(_diff(["src/a.py"]))
    assert not any(e["stage"] == "system_pass" for e in result.audit)

    # Two parts: one plain call (never the agentic loop), within the budget.
    monkeypatch.setattr(
        module, "chunk_files", lambda files, **kw: [ReviewChunk([f]) for f in files]
    )
    loop = AsyncMock(side_effect=AssertionError("no agentic loop under staged"))
    monkeypatch.setattr(module, "agentic_review_loop", loop)
    model = _model(answer)
    system_calls = []

    async def review(messages, temperature=None):
        if is_system(messages):
            system_calls.append(messages)
        return json.dumps({"comments": []})

    model.review = AsyncMock(side_effect=review)
    result = await ReviewEngine(config=config, llm=model).review_diff(
        _diff(["src/a.py", "src/b.py"])
    )
    assert len(system_calls) == 1
    assert any(e["stage"] == "system_pass" and e["status"] == "ok" for e in result.audit)
    loop.assert_not_awaited()


async def test_a_broken_security_pass_does_not_fail_a_staged_review(monkeypatch):
    import mira.core.engine as module

    async def broken(*_a, **_kw):
        raise RuntimeError("security pass broke")

    monkeypatch.setattr(module, "security_review_pass", broken)

    async def answer(messages, tools, **_kw):
        return json.dumps({"comments": [], "summary": "ok"})

    config = _engine_config(security_pass=True)
    result = await ReviewEngine(config=config, llm=_model(answer)).review_diff(_diff(["src/a.py"]))
    assert result.reviewed_files == 1
    assert any(
        e["stage"] == "stage_left_out" and e["what"] == "Security pass" for e in result.audit
    )


async def test_verification_grades_large_sets_in_parallel_batches(monkeypatch):
    import mira.core.engine as module

    batches: list[int] = []

    async def critique(llm, comments, **_kw):
        batches.append(len(comments))
        await asyncio.sleep(0.01)
        return comments

    monkeypatch.setattr(module, "self_critique", critique)
    titles = [
        "Cache never invalidated", "Token leaks into logs", "Lock held across await",
        "Queue drained twice", "Parser drops trailing field", "Socket left open",
        "Retry loops forever", "Header casing ignored", "Index off by one",
        "Timezone assumed UTC", "Division by zero possible", "Permission check skipped",
        "Migration not reversible", "Stale closure captured", "Unbounded recursion",
    ]  # fmt: skip

    async def answer(messages, tools, **_kw):
        comments = [
            {
                "path": "src/a.py",
                "line": i + 1,
                "severity": "warning",
                "category": "bug",
                "title": title,
                "body": f"{title}.",
                "confidence": 0.9,
                "existing_code": f"value_{i} = {i}",
            }
            for i, title in enumerate(titles)
        ]
        return json.dumps({"comments": comments, "summary": "ok"})

    config = _engine_config(self_critique=True, critique_batch_size=6)
    config.filter.max_comments = 50
    await ReviewEngine(config=config, llm=_model(answer)).review_diff(_diff(["src/a.py"], 30))
    assert len(batches) >= 2 and all(n <= 6 for n in batches)


def test_defaults_are_the_staged_single_pass_pipeline():
    review = MiraConfig().review
    assert review.pipeline == "staged" and review.passes == 1
    assert review.time_budget_seconds == 300
    assert review.followup_max_lookups > 0 and review.followup_call is False


def _engine_with_lookups(confidence: float, verdict: str):
    """A one-part staged review whose draft asked for a lookup, and a critic giving ``verdict``."""
    from mira.models import PRInfo
    from mira.platforms.fetch import RepoSnapshot

    critic_prompts: list[str] = []

    async def answer(messages, tools, **_kw):
        name = tools[0]["function"]["name"]
        if name == "submit_critique":
            critic_prompts.append(messages[0]["content"])
            return json.dumps({"verdicts": [{"index": 0, "evidence": verdict, "reason": "r"}]})
        body = json.loads(review_json("Caller passes None"))
        body["comments"][0]["confidence"] = confidence
        body["comments"][0]["existing_code"] = "value_0 = 0"
        body["need"] = [{"tool": "find_usages", "symbol": "load", "reason": "None?"}]
        return json.dumps(body)

    model = _model(answer)
    config = _engine_config(self_critique=True, code_context=True)
    source = "".join(f"value_{i} = {i}\n" for i in range(3))
    provider = MagicMock()
    provider.get_repo_snapshot = AsyncMock(
        return_value=RepoSnapshot(files={"src/a.py": source}, paths={"src/a.py"})
    )
    provider.get_file_content = AsyncMock(return_value="")
    provider.get_repo_tree = AsyncMock(return_value=["src/a.py"])
    provider.get_file_history = AsyncMock(return_value={})
    scope = PRInfo("t", "", "main", "feature", "https://github.com/o/r/pull/1", 1, "o", "r")
    scope.head_sha = "abc123"
    return ReviewEngine(config=config, llm=model, provider=provider), scope, critic_prompts


@pytest.mark.parametrize(("verdict", "posted"), [("proven", True), ("plausible", False)])
async def test_an_unsure_finding_waits_for_its_lookups_and_is_posted_only_if_proven(
    monkeypatch, verdict, posted
):
    executor = FakeExecutor()
    executor.tools = []
    monkeypatch.setattr("mira.llm.agentic_tools.AgenticToolExecutor", lambda **kw: executor)
    engine, scope, critic_prompts = _engine_with_lookups(0.4, verdict)
    result = await engine.review_diff(_diff(["src/a.py"]), repo_scope=scope)
    titles = [c.title for c in result.comments]
    assert (titles == ["Caller passes None"]) is posted
    assert ("find_usages", {"symbol": "load"}) in executor.calls
    assert critic_prompts and "## Lookups the reviewer asked for" in critic_prompts[0]
    assert "result of find_usages" in critic_prompts[0]
    assert "The reviewer was unsure of this one" in critic_prompts[0]
    if posted:
        assert result.comments[0].confidence == pytest.approx(0.7)
    else:
        # Graded only plausible: the critic drops it, since only proof could carry it.
        assert any(
            e.get("stage") == "self_critique" and e.get("title") == "Caller passes None"
            for e in result.audit
        )


async def test_a_part_is_verified_as_soon_as_it_lands(monkeypatch):
    """The fast part's findings are graded while the slow part is still thinking."""
    import mira.core.engine as module
    from mira.models import ReviewChunk

    monkeypatch.setattr(
        module, "chunk_files", lambda files, **kw: [ReviewChunk([f]) for f in files]
    )
    slow_done = asyncio.Event()
    graded_before_slow: list[bool] = []

    async def critique(llm, comments, **_kw):
        graded_before_slow.append(not slow_done.is_set())
        return comments

    monkeypatch.setattr(module, "self_critique", critique)

    async def answer(messages, tools, **_kw):
        content = messages[-1]["content"]
        path = "src/slow.py" if "### `src/slow.py`" in content else "src/fast.py"
        if path == "src/slow.py":
            await asyncio.sleep(0.3)
            slow_done.set()
        body = json.loads(review_json(f"Problem in {path}"))
        body["comments"][0]["path"] = path
        body["comments"][0]["existing_code"] = "value_0 = 0"
        return json.dumps(body)

    config = _engine_config(self_critique=True)
    result = await ReviewEngine(config=config, llm=_model(answer)).review_diff(
        _diff(["src/fast.py", "src/slow.py"])
    )
    assert sorted(c.path for c in result.comments) == ["src/fast.py", "src/slow.py"]
    # One grading per part, the fast one before the slow part finished; none after.
    assert graded_before_slow == [True, False]
