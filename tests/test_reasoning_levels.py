"""Reasoning per call: honoured levels, mechanical stages, the length runaway."""

from __future__ import annotations

import json
from functools import partial
from itertools import count
from unittest.mock import patch

import httpx
import pytest
from pydantic import BaseModel, ValidationError

from mira.config import LLMConfig
from mira.index import indexer
from mira.llm import accounting, concurrency, models_dev
from mira.llm import reasoning as rules
from mira.llm.base import lower_effort, reasoning_override
from mira.llm.provider import LLMProvider

_urls = count()
_OPEN_DESIGN = {"reasoning_levels": {"_comment": "measured", "deepseek-*": ["none", "high"]}}


@pytest.fixture(autouse=True)
def _keys(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    monkeypatch.setenv("OPENDESIGN_API_KEY", "test-key")
    concurrency.reset()


def _open_design(**fields) -> LLMConfig:
    fields.setdefault("model", "deepseek-v4.1-flash-eco")
    fields.setdefault("json_schema_mode", False)  # the replies below are tool calls
    return LLMConfig(provider="open-design", **fields)


def _generic(**fields) -> LLMConfig:
    # A URL of its own per test: refusals are remembered process-wide, per endpoint.
    fields.setdefault("base_url", f"https://llm{next(_urls)}.example.com/v1")
    fields.setdefault("model", "m")
    fields.setdefault("json_schema_mode", False)
    return LLMConfig(**fields)


class _Verdict(BaseModel):
    correct: bool


def _reply(message: dict, finish_reason: str = "stop", **usage) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "choices": [
                {"message": {"role": "assistant", **message}, "finish_reason": finish_reason}
            ],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5, **usage},
        },
    )


def _verdict() -> httpx.Response:
    return _reply(
        {
            "content": "",
            "tool_calls": [
                {
                    "id": "c1",
                    "type": "function",
                    "function": {"name": "submit_verdict", "arguments": '{"correct": true}'},
                }
            ],
        }
    )


def _record(bodies: list[dict], replies: list[httpx.Response] | None = None):
    async def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content))
        return replies.pop(0) if replies else _verdict()

    return patch(
        "mira.llm.provider.httpx.AsyncClient",
        side_effect=partial(httpx.AsyncClient, transport=httpx.MockTransport(handler)),
    )


async def _ask(llm: LLMProvider) -> _Verdict:
    return await llm.generate_object(
        [{"role": "user", "content": "x"}], _Verdict, name="submit_verdict"
    )


def _effort(body: dict) -> str | None:
    nested = body.get("reasoning")
    return nested.get("effort") if isinstance(nested, dict) else body.get("reasoning_effort")


# ───────────────────────────────────────────────────────────── the rules


def test_none_is_the_lowest_level_an_override_can_ask_for() -> None:
    assert lower_effort("high", "none") == "none"
    assert lower_effort("low", "none") == "none"
    assert lower_effort("none", "low") == "none"
    assert lower_effort(None, "none") is None


def test_a_lowered_level_goes_down_to_an_honoured_one() -> None:
    honoured = ("none", "high")
    assert rules.resolve("high", "low", honoured=honoured) == "none"
    assert rules.resolve("high", "minimal", honoured=honoured) == "none"
    assert rules.resolve("high", "none", honoured=honoured) == "none"


def test_a_configured_level_is_never_turned_into_no_thinking() -> None:
    honoured = ("none", "high")
    assert rules.resolve("high", None, honoured=honoured) == "high"
    # "low" does what "high" does on this endpoint: say so, never "none".
    assert rules.resolve("low", None, honoured=honoured) == "high"
    assert rules.resolve("max", None, honoured=honoured) == "high"
    # An override above the configured level leaves the configured one.
    assert rules.resolve("low", "medium", honoured=honoured) == "high"


def test_no_level_configured_becomes_none_only_where_none_is_known() -> None:
    assert rules.resolve(None, "none", honoured=("none", "high")) == "none"
    assert rules.resolve("off", "none", known=("none", "low", "high")) == "none"
    assert rules.resolve(None, "none") is None
    assert rules.resolve(None, "none", known=("low", "high")) is None
    # Any other override never turns thinking on.
    assert rules.resolve(None, "low", honoured=("none", "high")) is None


def test_an_undescribed_endpoint_gets_low_for_none() -> None:
    assert rules.resolve("high", "none") == "low"
    assert rules.resolve("high", "low") == "low"
    # models.dev knows the model: snapped to its levels later, by the provider.
    assert rules.resolve("high", "none", known=("low", "high")) == "none"


def test_honoured_levels_match_model_globs_and_skip_comments() -> None:
    assert rules.honoured_levels(_OPEN_DESIGN, "deepseek-v4.1-flash") == ("none", "high")
    assert rules.honoured_levels(_OPEN_DESIGN, "deepseek/deepseek-v4.1-flash") == (
        "none",
        "high",
    )
    assert rules.honoured_levels(_OPEN_DESIGN, "kimi-k2.7") is None
    assert rules.honoured_levels({"reasoning_levels": ["high", "none", "bogus"]}, "x") == (
        "none",
        "high",
    )
    assert rules.honoured_levels({}, "x") is None


def test_only_mechanical_stages_lower_the_level() -> None:
    assert rules.stage_level("file_summaries", "none") == "none"
    assert rules.stage_level("walkthrough", "low") == "low"
    assert rules.stage_level("walkthrough", "inherit") is None
    for judgement in ("pass", "system", "security", "critique", "threads", ""):
        assert rules.stage_level(judgement, "none") is None


def test_mechanical_reasoning_is_validated() -> None:
    assert LLMConfig().mechanical_reasoning == "none"
    assert LLMConfig(mechanical_reasoning="inherit").mechanical_reasoning == "inherit"
    with pytest.raises(ValidationError):
        LLMConfig(mechanical_reasoning="nothing")


# ──────────────────────────────────────────────────────────── on the wire


@pytest.mark.asyncio
async def test_a_mechanical_call_on_open_design_turns_thinking_off() -> None:
    bodies: list[dict] = []
    llm = LLMProvider(_open_design(usage_purpose="indexing"))
    with _record(bodies), accounting.stage("file_summaries"):
        await _ask(llm)
    assert bodies[0]["reasoning_effort"] == "none"
    # Nothing thinks at "none": the call keeps its temperature.
    assert "temperature" in bodies[0]


@pytest.mark.asyncio
async def test_a_judgement_call_keeps_the_endpoint_default_or_its_level() -> None:
    bodies: list[dict] = []
    with _record(bodies):
        with accounting.stage("critique"):
            await _ask(LLMProvider(_open_design(usage_purpose="indexing")))
        with accounting.stage("pass"):
            await _ask(LLMProvider(_open_design(reasoning_effort="high")))
            await _ask(LLMProvider(_open_design(reasoning_effort="low")))
    assert "reasoning_effort" not in bodies[0]
    assert bodies[1]["reasoning_effort"] == "high"
    assert bodies[2]["reasoning_effort"] == "high"


@pytest.mark.asyncio
async def test_a_lookup_hop_on_open_design_asks_for_none() -> None:
    bodies: list[dict] = []
    llm = LLMProvider(_open_design(reasoning_effort="high"))
    with _record(bodies), reasoning_override("low"):
        await _ask(llm)
    assert bodies[0]["reasoning_effort"] == "none"


@pytest.mark.asyncio
async def test_a_walkthrough_on_the_review_client_drops_its_thinking() -> None:
    bodies: list[dict] = []
    llm = LLMProvider(_open_design(reasoning_effort="high"))
    with _record(bodies), accounting.stage("walkthrough"):
        await _ask(llm)
    with _record(bodies), accounting.stage("walkthrough"):
        await _ask(
            LLMProvider(_open_design(reasoning_effort="high", mechanical_reasoning="inherit"))
        )
    assert [b["reasoning_effort"] for b in bodies] == ["none", "high"]


@pytest.mark.asyncio
async def test_other_endpoints_keep_what_they_sent() -> None:
    bodies: list[dict] = []
    with _record(bodies), accounting.stage("file_summaries"):
        await _ask(LLMProvider(_generic()))
        await _ask(LLMProvider(_generic(reasoning_effort="high")))
    # No level configured and nothing says "none" works: nothing is sent.
    assert "reasoning_effort" not in bodies[0]
    # A configured level is lowered as far as nearly every API goes.
    assert bodies[1]["reasoning_effort"] == "low"


@pytest.mark.asyncio
async def test_openrouter_sends_none_nested_where_models_dev_lists_it() -> None:
    models_dev.load(
        {
            "openrouter": {
                "api": "https://openrouter.ai/api/v1",
                "models": {
                    "deepseek/deepseek-v4.1-flash": {
                        "reasoning_options": [
                            {"type": "effort", "values": ["none", "low", "high", "max"]}
                        ]
                    }
                },
            }
        }
    )
    bodies: list[dict] = []
    config = LLMConfig(
        provider="openrouter", model="deepseek/deepseek-v4.1-flash", json_schema_mode=False
    )
    with _record(bodies), accounting.stage("overlaps"):
        await _ask(LLMProvider(config))
    assert bodies[0]["reasoning"] == {"effort": "none"}
    assert "reasoning_effort" not in bodies[0]


@pytest.mark.asyncio
async def test_a_refused_mechanical_level_is_asked_again_at_the_configured_one() -> None:
    bodies: list[dict] = []
    refused = httpx.Response(400, json={"error": {"message": "unsupported reasoning_effort"}})
    llm = LLMProvider(_generic(reasoning_effort="high"))
    with _record(bodies, [refused, _verdict(), _verdict()]), accounting.stage("digest"):
        await _ask(llm)
        await _ask(llm)
    assert [_effort(b) for b in bodies] == ["low", "high", "high"]


# ───────────────────────────────────────────────────────── length runaway


def _runaway() -> httpx.Response:
    return _reply(
        {"content": "", "reasoning_content": "thinking " * 50},
        finish_reason="length",
        completion_tokens_details={"reasoning_tokens": 8000},
    )


@pytest.mark.asyncio
async def test_a_reply_that_was_all_thinking_is_asked_once_more_with_it_off() -> None:
    bodies: list[dict] = []
    llm = LLMProvider(_open_design(reasoning_effort="high", tool_call_retries=0))
    with _record(bodies, [_runaway(), _verdict()]), accounting.stage("pass"):
        result = await _ask(llm)
    assert result.correct is True
    assert [b["reasoning_effort"] for b in bodies] == ["high", "none"]
    # More thinking room is not what fixes it: the budget stays where it was.
    assert bodies[0].get("max_tokens") == bodies[1].get("max_tokens")


@pytest.mark.asyncio
async def test_a_runaway_under_json_schema_is_turned_off_the_same_way() -> None:
    bodies: list[dict] = []
    llm = LLMProvider(
        _open_design(reasoning_effort="high", tool_call_retries=0, json_schema_mode=True)
    )
    answer = _reply({"content": '{"correct": false}'})
    with _record(bodies, [_runaway(), answer]), accounting.stage("pass"):
        result = await _ask(llm)
    assert result.correct is False
    assert [b["reasoning_effort"] for b in bodies] == ["high", "none"]
    assert all(b["response_format"]["type"] == "json_schema" for b in bodies)


@pytest.mark.asyncio
async def test_a_model_that_thinks_by_default_is_turned_off_too() -> None:
    bodies: list[dict] = []
    llm = LLMProvider(_open_design(tool_call_retries=0))
    with _record(bodies, [_runaway(), _verdict()]), accounting.stage("critique"):
        await _ask(llm)
    assert [b.get("reasoning_effort") for b in bodies] == [None, "none"]


@pytest.mark.asyncio
async def test_a_runaway_with_no_way_to_stop_thinking_takes_the_old_road() -> None:
    bodies: list[dict] = []
    llm = LLMProvider(_generic(tool_call_retries=1, json_mode_fallback=False))
    with _record(bodies, [_runaway(), _verdict()]):
        await _ask(llm)
    # Nothing says "none" works here and no level is configured: the re-roll
    # is the truncation one, with a bigger budget and no level.
    assert [b.get("reasoning_effort") for b in bodies] == [None, None]
    assert bodies[1]["max_tokens"] > bodies[0]["max_tokens"]


# ─────────────────────────────────────────────────────────── measurement


@pytest.mark.asyncio
async def test_an_untagged_structured_call_is_recorded_under_its_tool(monkeypatch) -> None:
    rows: list[dict] = []
    monkeypatch.setattr("mira.llm.ledger.submit", rows.append)
    with _record([]):
        await _ask(LLMProvider(_open_design()))
        with accounting.stage("file_summaries"):
            await _ask(LLMProvider(_open_design()))
    assert [r["stage"] for r in rows] == ["submit_verdict", "file_summaries"]


def test_indexing_takes_half_the_background_lane_of_a_large_plan() -> None:
    class _Client:
        def __init__(self, config: LLMConfig) -> None:
            self.config = config

    assert indexer._llm_concurrency(_Client(_open_design(plan="go"))) == 8
    assert indexer._llm_concurrency(_Client(_open_design(plan="max"))) == 24
    assert indexer._llm_concurrency(_Client(_generic())) == 8
    assert indexer._llm_concurrency(object()) == 8
