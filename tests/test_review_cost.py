"""What a review spends: concurrency slots, reasoning per hop, cache prefixes, rounds."""

from __future__ import annotations

import asyncio
import json
import time
from functools import partial
from itertools import count
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from mira.config import LLMConfig, MiraConfig
from mira.core import multipass
from mira.core.engine import ReviewEngine, _patches_read
from mira.core.passes import agentic_review_loop
from mira.core.patch_id import diff_patch_id, file_patch_ids
from mira.exceptions import LLMError, NonRetriableLLMError
from mira.llm import accounting, concurrency, costs
from mira.llm import provider_profiles as profiles
from mira.llm.base import _quota_exhausted, lower_effort, reasoning_override
from mira.llm.provider import LLMProvider
from mira.models import ReviewComment, ReviewResult, Severity

_TIER_LIMIT = {
    "error": {
        "code": "tier_limit_exceeded",
        "message": "[code=tier_limit_exceeded] membership concurrency limit exceeded: "
        "21/20 resets 2026-11-14T14:31:00Z",
    }
}
_HELPER = {
    "type": "function",
    "function": {"name": "read_file", "parameters": {"type": "object", "properties": {}}},
}
_TOOL = {
    "type": "function",
    "function": {
        "name": "submit_findings",
        "parameters": {
            "type": "object",
            "properties": {"summary": {"type": "string"}},
            "required": ["summary"],
        },
    },
}
_urls = count()


@pytest.fixture(autouse=True)
def _keys(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    monkeypatch.setenv("OPENDESIGN_API_KEY", "test-key")
    concurrency.reset()


def _config(**fields) -> LLMConfig:
    # A URL of its own per test: refusals are remembered process-wide, per endpoint.
    fields.setdefault("base_url", f"https://llm{next(_urls)}.example.com/v1")
    fields.setdefault("model", "m")
    return LLMConfig(**fields)


def _client_factory(handler):
    return partial(httpx.AsyncClient, transport=httpx.MockTransport(handler))


def _answer(content: str = "ok", **message) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "choices": [{"message": {"role": "assistant", "content": content, **message}}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 2},
        },
    )


def _tool_answer(name: str, arguments: dict) -> httpx.Response:
    return _answer(
        "",
        tool_calls=[
            {
                "id": "c1",
                "type": "function",
                "function": {"name": name, "arguments": json.dumps(arguments)},
            }
        ],
    )


# ─────────────────────────────────────────────────────────── concurrency


def test_open_design_slots_come_from_the_plan() -> None:
    profile = profiles.get("open-design")
    assert profile is not None
    assert concurrency.limit_for(SimpleNamespace(plan="plus"), profile) == (20, 2)
    assert concurrency.limit_for(SimpleNamespace(plan="go"), profile) == (10, 2)
    assert concurrency.limit_for(SimpleNamespace(plan="max"), profile) == (50, 2)
    # No plan named: the smallest plan's limit, the safe guess.
    assert concurrency.limit_for(SimpleNamespace(plan=""), profile) == (10, 2)
    # A stored endpoint's own figure wins over its plan; the config over both.
    stored = {**profile, "plan": "plus", "endpoint_max_concurrency": 12}
    assert concurrency.limit_for(SimpleNamespace(plan=""), stored) == (12, 2)
    explicit = SimpleNamespace(plan="plus", max_concurrent_requests=4)
    assert concurrency.limit_for(explicit, profile) == (4, 1)
    # An endpoint nobody described has no limit, as before.
    assert concurrency.limit_for(SimpleNamespace(plan=""), dict(profiles.DEFAULT_PROFILE)) is None


def test_a_concurrency_refusal_is_not_a_quota() -> None:
    for status in (400, 429):
        resp = httpx.Response(status, json=_TIER_LIMIT)
        assert concurrency.is_concurrency_refusal(status, resp.text)
        assert not _quota_exhausted(resp)
    spent = httpx.Response(429, json={"error": {"message": "monthly quota exhausted"}})
    assert _quota_exhausted(spent)
    assert not concurrency.is_concurrency_refusal(429, spent.text)


def test_the_wait_honours_a_near_reset_time() -> None:
    text = json.dumps(_TIER_LIMIT)
    now = time.mktime(time.strptime("2026-11-14T14:30:50", "%Y-%m-%dT%H:%M:%S")) - time.timezone
    assert 10 <= concurrency.refusal_wait(text, 1, now=now) <= 11
    # A reset far off (or past) falls back to the short backoff.
    assert concurrency.refusal_wait(text, 1, now=now - 3600) <= 3
    assert concurrency.refusal_wait("busy", 5) <= 31


@pytest.mark.asyncio
async def test_requests_never_exceed_the_endpoints_slots() -> None:
    in_flight = peak = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal in_flight, peak
        in_flight += 1
        peak = max(peak, in_flight)
        await asyncio.sleep(0.02)
        in_flight -= 1
        return _answer()

    llm = LLMProvider(_config(max_concurrent_requests=3))
    with patch("mira.llm.provider.httpx.AsyncClient", side_effect=_client_factory(handler)):
        await asyncio.gather(*(llm.complete([{"role": "user", "content": "x"}]) for _ in range(9)))
    assert peak == 3


@pytest.mark.asyncio
async def test_background_work_leaves_headroom_for_chat() -> None:
    limiter = concurrency.EndpointLimiter(4, headroom=1)
    for _ in range(3):
        await limiter.acquire(background=True)
    waiting = asyncio.create_task(limiter.acquire(background=True))
    await asyncio.sleep(0)
    assert not waiting.done()
    # A chat reply gets the slot background work may not take…
    await asyncio.wait_for(limiter.acquire(background=False), 1)
    assert limiter.in_use == 4
    limiter.release()  # the chat reply ends: background is still at its ceiling
    await asyncio.sleep(0)
    assert not waiting.done()
    limiter.release()  # …and the queued background call gets the next one.
    await asyncio.wait_for(waiting, 1)
    assert limiter.in_use == 3


@pytest.mark.asyncio
async def test_a_cancelled_wait_gives_its_slot_back() -> None:
    limiter = concurrency.EndpointLimiter(1)
    await limiter.acquire(background=False)
    waiting = asyncio.create_task(limiter.acquire(background=False))
    await asyncio.sleep(0)
    waiting.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiting
    limiter.release()
    assert limiter.in_use == 0 and limiter.waiting == 0


@pytest.mark.asyncio
async def test_a_tier_limit_refusal_waits_and_goes_again(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(concurrency, "refusal_wait", lambda text, attempt, now=None: 0.0)
    replies = [httpx.Response(400, json=_TIER_LIMIT), httpx.Response(400, json=_TIER_LIMIT)]

    async def handler(request: httpx.Request) -> httpx.Response:
        return replies.pop(0) if replies else _answer("done")

    llm = LLMProvider(_config(max_concurrent_requests=5))
    with patch("mira.llm.provider.httpx.AsyncClient", side_effect=_client_factory(handler)):
        assert await llm.complete([{"role": "user", "content": "x"}], json_mode=False) == "done"
    # The refusal lowered the ceiling for a while: someone else holds slots.
    key = llm._endpoint_key()
    assert concurrency.limiter(key, 5, 1).ceiling(background=False) < 5


@pytest.mark.asyncio
async def test_a_refusal_past_the_wait_is_a_transient_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json=_TIER_LIMIT)

    llm = LLMProvider(_config(concurrency_wait_seconds=0, max_retries=1))
    with (
        patch("mira.llm.provider.httpx.AsyncClient", side_effect=_client_factory(handler)),
        pytest.raises(LLMError) as raised,
    ):
        await llm._call_llm("m", [{"role": "user", "content": "x"}], json_mode=False)
    assert not isinstance(raised.value, NonRetriableLLMError)
    assert raised.value.concurrency_limited


@pytest.mark.asyncio
async def test_a_tier_limit_refusal_is_not_taken_for_a_json_schema_refusal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Before: the 400 sent the call down the tool → JSON-mode ladder, for good."""
    monkeypatch.setattr(concurrency, "refusal_wait", lambda text, attempt, now=None: 0.0)
    bodies: list[dict] = []
    replies = [httpx.Response(400, json=_TIER_LIMIT)]

    async def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content))
        return replies.pop(0) if replies else _answer(json.dumps({"summary": "fine"}))

    llm = LLMProvider(_config())
    with patch("mira.llm.provider.httpx.AsyncClient", side_effect=_client_factory(handler)):
        raw = await llm.complete_with_tools([{"role": "user", "content": "x"}], [_TOOL])
    assert json.loads(raw) == {"summary": "fine"}
    assert all(b["response_format"]["type"] == "json_schema" for b in bodies)
    assert llm._takes_json_schema("m")


@pytest.mark.asyncio
async def test_a_preset_without_json_schema_goes_straight_to_tools() -> None:
    bodies: list[dict] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content))
        data = {
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": "",
                        "tool_calls": [
                            {
                                "id": "c",
                                "type": "function",
                                "function": {
                                    "name": "submit_findings",
                                    "arguments": json.dumps({"summary": "fine"}),
                                },
                            }
                        ],
                    }
                }
            ]
        }
        body = "".join(
            f"data: {json.dumps({'choices': [{'delta': d}]})}\n\n"
            for d in ({"tool_calls": data["choices"][0]["message"]["tool_calls"]},)
        )
        return httpx.Response(
            200, content=body + "data: [DONE]\n\n", headers={"content-type": "text/event-stream"}
        )

    llm = LLMProvider(
        LLMConfig(provider="open-design", model="deepseek-v4.1-flash-eco", plan="plus")
    )
    assert llm._json_schema_for([_TOOL]) is not None  # OpenDesign takes the format
    llm.profile = {**llm.profile, "json_schema": False}
    with patch("mira.llm.provider.httpx.AsyncClient", side_effect=_client_factory(handler)):
        raw = await llm.complete_with_tools([{"role": "user", "content": "x"}], [_TOOL])
    assert json.loads(raw) == {"summary": "fine"}
    assert len(bodies) == 1
    assert "tools" in bodies[0] and "response_format" not in bodies[0]


# ──────────────────────────────────────────────────── reasoning per hop


def test_an_override_only_ever_lowers_the_level() -> None:
    assert lower_effort("high", "low") == "low"
    assert lower_effort("low", "high") == "low"
    assert lower_effort(None, "low") is None
    assert lower_effort("off", "low") == "off"
    assert lower_effort("high", "inherit") == "high"
    assert lower_effort("high", "bogus") == "high"


class _Executor:
    tools = [_HELPER]
    call_log: list = []

    async def execute(self, name: str, args: dict) -> str:
        return "def f(): pass"


def _submit(comments: list | None = None) -> dict:
    arguments = json.dumps({"comments": comments or [], "summary": "ok"})
    return {
        "role": "assistant",
        "content": "",
        "tool_calls": [
            {
                "id": "s",
                "type": "function",
                "function": {"name": "submit_review", "arguments": arguments},
            }
        ],
    }


def _lookup() -> dict:
    return {
        "role": "assistant",
        "content": "",
        "tool_calls": [
            {"id": "r", "type": "function", "function": {"name": "read_file", "arguments": "{}"}}
        ],
    }


@pytest.mark.asyncio
async def test_lookup_hops_think_less_and_the_submitting_hop_thinks_fully() -> None:
    from mira.llm.base import current_effort_override

    levels: list[str | None] = []
    script = [_lookup(), _submit(), _submit()]

    async def complete_agentic(messages, tools, **kwargs):
        levels.append(current_effort_override())
        return script.pop(0)

    llm = SimpleNamespace(complete_agentic=complete_agentic)
    raw = await agentic_review_loop(
        llm,
        [{"role": "system", "content": "s"}, {"role": "user", "content": "u"}],
        _Executor(),
        lookup_reasoning_effort="low",
    )
    assert json.loads(raw)["summary"] == "ok"
    # Hop 1 looked something up at the lookup level; hop 2 tried to submit at
    # it and was asked again at the configured level, whose answer stands.
    assert levels == ["low", "low", None]


@pytest.mark.asyncio
async def test_the_lookup_level_reaches_the_wire_and_a_refusal_restores_the_configured_one() -> (
    None
):
    sent: list[str | None] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        sent.append(body.get("reasoning_effort"))
        if body.get("reasoning_effort") == "low":
            return httpx.Response(400, json={"error": {"message": "unsupported reasoning_effort"}})
        return _answer("", tool_calls=_lookup()["tool_calls"])

    llm = LLMProvider(_config(reasoning_effort="high"))
    with (
        patch("mira.llm.provider.httpx.AsyncClient", side_effect=_client_factory(handler)),
        reasoning_override("low"),
    ):
        await llm.complete_agentic([{"role": "user", "content": "x"}], [_HELPER])
        await llm.complete_agentic([{"role": "user", "content": "x"}], [_HELPER])
    # Refused once, asked again at the configured level, and not asked low again.
    assert sent == ["low", "high", "high"]


@pytest.mark.asyncio
async def test_a_loop_out_of_time_sends_no_more_calls() -> None:
    calls = 0

    async def complete_agentic(messages, tools, **kwargs):
        nonlocal calls
        calls += 1
        return _lookup()

    llm = SimpleNamespace(complete_agentic=complete_agentic)
    with pytest.raises(TimeoutError):
        await agentic_review_loop(
            llm,
            [{"role": "user", "content": "u"}],
            _Executor(),
            stop_by=time.monotonic() - 1,
        )
    assert calls == 1  # the first hop, and nothing once the time was gone


# ─────────────────────────────────────────────────────── shuffled passes


def _file(path: str, starts=(1, 40)):
    from mira.models import FileChangeType, FileDiff, HunkInfo

    hunks = [
        HunkInfo(
            source_start=s,
            source_length=1,
            target_start=s,
            target_length=2,
            content=f"@@ -{s},1 +{s},2 @@\n+x\n",
        )
        for s in starts
    ]
    return FileDiff(path=path, change_type=FileChangeType.MODIFIED, hunks=hunks, added_lines=2)


def test_later_passes_read_in_another_order_from_the_same_prompt() -> None:
    files = [_file(f"src/{n}.py") for n in "abcd"]
    assert multipass.reading_order_note(files, 1, 0) == ""
    note = multipass.reading_order_note(files, multipass.pass_seed("k", 1), 1)
    assert "## Reading order for this pass" in note and "last hunk" in note
    listed = [line.split("`")[1] for line in note.splitlines() if line[:1].isdigit()]
    assert sorted(listed) == [f.path for f in files]
    messages = [{"role": "system", "content": "s"}, {"role": "user", "content": "diff"}]
    out = multipass.with_reading_order(messages, note)
    assert out[0] == messages[0] and out[1]["content"] == "diff" + note
    assert messages[1]["content"] == "diff"  # the shared prompt is not touched


@pytest.mark.asyncio
async def test_later_passes_wait_for_the_first_reply_to_start() -> None:
    events: list[str] = []

    async def one(index: int) -> int:
        events.append(f"start {index}")
        if index == 0:
            await asyncio.sleep(0.05)
            concurrency.first_response()  # what the HTTP layer says on the first byte
            await asyncio.sleep(0.05)
        events.append(f"end {index}")
        return index

    run = await multipass.run_passes(one, 3, stagger_seconds=5)
    assert [o.result for o in run.landed] == [0, 1, 2]
    assert events[0] == "start 0"
    assert events.index("start 1") > 0 and events.index("start 1") < events.index("end 0")


def _comment(path: str, line: int, severity: Severity, title: str) -> ReviewComment:
    return ReviewComment(
        path=path,
        line=line,
        end_line=None,
        body="b",
        severity=severity,
        category="bug",
        title=title,
        confidence=0.9,
    )


def test_a_single_pass_warning_can_still_be_proven() -> None:
    a = _comment("x.py", 3, Severity.WARNING, "Counter never reset")
    b = _comment("x.py", 9, Severity.NITPICK, "Rename this")
    old = multipass.vote([[a, b], []], 2)
    assert old.kept == []
    new = multipass.vote([[a, b], []], 2, Severity.WARNING)
    assert [c.title for c in new.kept] == ["Counter never reset"]
    assert new.kept[0].require_proven


# ────────────────────────────────────────────────────── follow-up rounds

_DIFF = (
    "diff --git a/a.py b/a.py\n--- a/a.py\n+++ b/a.py\n@@ -1 +1 @@\n-x\n+y\n"
    "diff --git a/b.py b/b.py\n--- a/b.py\n+++ b/b.py\n@@ -1 +1 @@\n-p\n+q\n"
)


def test_file_patch_ids_ignore_where_a_change_applies() -> None:
    moved = _DIFF.replace("@@ -1 +1 @@\n-x", "@@ -40 +40 @@\n-x")
    assert file_patch_ids(moved) == file_patch_ids(_DIFF)
    edited = _DIFF.replace("+q", "+r")
    changed = {p for p, pid in file_patch_ids(edited).items() if file_patch_ids(_DIFF)[p] != pid}
    assert changed == {"b.py"}
    assert diff_patch_id(_DIFF)  # the whole-diff id is still there


def test_unread_files_keep_the_patch_the_last_review_read() -> None:
    previous = {"a.py": "old-a", "b.py": "old-b"}
    result = ReviewResult(summary="", skipped_paths=["b.py"])
    recorded = _patches_read(_DIFF, previous, result)
    assert recorded["a.py"] == file_patch_ids(_DIFF)["a.py"]
    assert recorded["b.py"] == "old-b"


def _pr_provider(compare: object) -> MagicMock:
    from mira.models import PRInfo

    provider = MagicMock()
    provider.get_pr_info = AsyncMock(
        return_value=PRInfo(
            title="t",
            description="",
            base_branch="main",
            head_branch="f",
            url="https://github.com/o/r/pull/1",
            number=1,
            owner="o",
            repo="r",
            head_sha="NEW",
        )
    )
    provider.get_pr_diff = AsyncMock(return_value=_DIFF.replace("+q", "+r"))
    if isinstance(compare, Exception):
        provider.get_compare_diff = AsyncMock(side_effect=compare)
    else:
        provider.get_compare_diff = AsyncMock(return_value=compare)
    provider.get_all_bot_threads = AsyncMock(return_value=[])
    provider.get_unresolved_bot_threads = AsyncMock(return_value=[])
    provider.find_bot_comment = AsyncMock(return_value=None)
    provider.post_comment = AsyncMock()
    provider.update_comment = AsyncMock()
    provider.resolve_outdated_review_threads = AsyncMock(return_value=0)
    return provider


@pytest.mark.parametrize(
    "compare",
    [
        # A force-push: the compare diff is the whole pull request again.
        _DIFF.replace("+q", "+r"),
        # The old head is gone and the compare fails outright.
        RuntimeError("No common ancestor"),
    ],
)
@pytest.mark.asyncio
async def test_a_rewritten_branch_reviews_only_the_files_whose_change_moved(
    monkeypatch: pytest.MonkeyPatch, compare: object
) -> None:
    db = MagicMock()
    db.get_last_reviewed_sha = MagicMock(return_value="OLD")
    db.get_reviewed_patches = MagicMock(return_value=file_patch_ids(_DIFF))
    db.get_repo = MagicMock(return_value=None)
    monkeypatch.setattr("mira.dashboard.api._app_db", db)
    seen: dict = {}

    async def fake_internal(self, diff_text, **kwargs):
        seen["diff"] = diff_text
        return ReviewResult(comments=[], summary="")

    monkeypatch.setattr(ReviewEngine, "_review_diff_internal", fake_internal)
    engine = ReviewEngine(
        config=MiraConfig(), llm=AsyncMock(), provider=_pr_provider(compare), bot_name="mira"
    )
    await engine.review_pr("https://github.com/o/r/pull/1")
    assert "b/b.py" in seen["diff"] and "b/a.py" not in seen["diff"]
    recorded = db.set_reviewed_patches.call_args.args[3]
    assert recorded == file_patch_ids(_DIFF.replace("+q", "+r"))


@pytest.mark.asyncio
async def test_a_part_waiting_for_its_slot_keeps_its_deadline() -> None:
    """The part clock starts when the part does, not while it queues behind others."""
    from tests.test_multipass import diff, engine_config, llm, response

    async def review(messages, temperature=None):
        await asyncio.sleep(0.6)
        return response()

    model = llm(review)
    config = engine_config(
        passes=1,
        system_pass=False,
        agent_max_files=1,
        max_concurrent_chunks=1,
        part_deadline_seconds=1,
        chunk_retries=0,
    )
    result = await ReviewEngine(config=config, llm=model).review_diff(
        diff(["src/a.py", "src/b.py"], lines=3)
    )
    assert model.review.await_count == 2
    assert not any(e.get("stage") == "chunk_failed" for e in result.audit)


# ─────────────────────────────────────────────────────────── the ledger


def test_calls_are_tagged_with_their_stage(monkeypatch: pytest.MonkeyPatch) -> None:
    rows: list[dict] = []
    monkeypatch.setattr("mira.llm.ledger.submit", rows.append)
    with accounting.stage("critique"):
        accounting.record(
            accounting.CallRecord(
                created_at=1.0,
                endpoint="e",
                provider="p",
                model="m",
                purpose="review",
                prompt_tokens=1,
                cached_tokens=0,
                completion_tokens=1,
                reasoning_tokens=0,
                cost_usd=0.0,
                credits=0.0,
                priced=True,
            )
        )
    assert rows[0]["stage"] == "critique"


def test_the_cost_report_splits_by_purpose_and_stage(tmp_path, monkeypatch) -> None:
    from mira.dashboard.db import AppDatabase

    monkeypatch.setenv("MIRA_INDEX_DIR", str(tmp_path))
    db = AppDatabase(url="", admin_password="admin")
    now = time.time()
    base = {
        "created_at": now,
        "endpoint": "config",
        "provider": "",
        "model": "m",
        "cached_tokens": 0,
        "reasoning_tokens": 0,
        "credits": 0.0,
        "priced": 1,
    }
    db.record_llm_usage(
        [
            {
                **base,
                "purpose": "review",
                "stage": "pass",
                "prompt_tokens": 100,
                "cached_tokens": 80,
                "completion_tokens": 10,
                "cost_usd": 0.3,
            },
            {
                **base,
                "purpose": "review",
                "stage": "critique",
                "prompt_tokens": 10,
                "completion_tokens": 1,
                "cost_usd": 0.1,
            },
            {
                **base,
                "purpose": "indexing",
                "stage": "",
                "prompt_tokens": 5,
                "completion_tokens": 1,
                "cost_usd": 0.1,
            },
        ]
    )
    report = costs.report(db, days=1, now=now + 1)
    stages = {(s["purpose"], s["stage"]): s for s in report["by_stage"]}
    assert stages[("review", "pass")]["cache_hit_rate"] == 0.8
    assert stages[("review", "pass")]["cost_share"] == pytest.approx(0.6)
    assert ("indexing", "indexing") in stages  # no stage: named by its purpose
    purposes = {p["purpose"]: p for p in report["by_purpose"]}
    assert purposes["review"]["calls"] == 2 and purposes["review"]["cost_usd"] == pytest.approx(0.4)


@pytest.mark.asyncio
async def test_a_pass_left_out_at_the_soft_deadline_stops_calling_the_model() -> None:
    calls = {"slow": 0}

    async def slow_hop(messages, tools, **kwargs):
        calls["slow"] += 1
        await asyncio.sleep(0.05)
        return _lookup()

    async def quick_hop(messages, tools, **kwargs):
        return _submit()

    async def one(index: int) -> str:
        hop = quick_hop if index == 0 else slow_hop
        return await agentic_review_loop(
            SimpleNamespace(complete_agentic=hop),
            [{"role": "user", "content": "u"}],
            _Executor(),
            max_hops=50,
        )

    run = await multipass.run_passes(one, 2, soft_deadline=time.monotonic() + 0.2)
    assert [o.status for o in run.outcomes] == ["ok", "late"]
    made = calls["slow"]
    await asyncio.sleep(0.3)
    assert calls["slow"] == made  # nothing after the cut


def test_a_stored_endpoint_without_a_plan_does_not_take_the_config_plan() -> None:
    from types import SimpleNamespace

    from mira.llm import concurrency

    profile = {
        "max_concurrency": 10,
        "concurrency_headroom": 0,
        "plans": {"plus": {"concurrency": 20}},
        "plan": "",
        "endpoint_max_concurrency": 0,
    }
    config = SimpleNamespace(plan="plus", max_concurrent_requests=0)
    assert concurrency.limit_for(config, profile) == (10, 0)
    assert concurrency.limit_for(config, {**profile, "plan": "plus"}) == (20, 0)


@pytest.mark.asyncio
async def test_a_resubmission_at_the_configured_level_counts_toward_the_hop_cap() -> None:
    from unittest.mock import AsyncMock

    submit = {
        "content": "",
        "tool_calls": [
            {
                "id": "s1",
                "type": "function",
                "function": {"name": "submit_review", "arguments": '{"comments": []}'},
            }
        ],
    }
    lookup = {
        "content": "",
        "tool_calls": [
            {"id": "c1", "type": "function", "function": {"name": "read_file", "arguments": "{}"}}
        ],
    }
    llm = AsyncMock()
    # Hop 1 submits at the lookup level; asked again, the model looks something up.
    llm.complete_agentic = AsyncMock(side_effect=[submit, lookup, submit, submit])

    class _Executor:
        tools = [{"type": "function", "function": {"name": "read_file", "parameters": {}}}]

        async def execute(self, name, args):
            return "x"

    await agentic_review_loop(
        llm,
        [{"role": "system", "content": "s"}, {"role": "user", "content": "d"}],
        _Executor(),
        max_hops=2,
        lookup_reasoning_effort="low",
    )
    # Two model calls spent the cap of two: no third hop.
    assert llm.complete_agentic.await_count == 2
