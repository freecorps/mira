"""OpenDesign Cloud, and the token / cache / cost accounting it motivated.

Covers the gateway's quirks as Mira meets them (one turn split across
several choices, a 200 with no choices when thinking ate the budget, the
structured ``reasoning_details``, a spent quota), the endpoint-scoped model
registry, the per-call usage ledger in the application database, and the
allowance meter estimated from it for a subscription with no usage API.
"""

from __future__ import annotations

import json
import time
from datetime import UTC, datetime
from functools import partial
from pathlib import Path
from unittest.mock import patch

import httpx
import pytest

from mira.config import LLMConfig
from mira.dashboard.db import AppDatabase
from mira.exceptions import LLMError, NonRetriableLLMError
from mira.llm import (
    accounting,
    costs,
    endpoints,
    key_providers,
    ledger,
    registry,
)
from mira.llm import provider_profiles as profiles
from mira.llm.base import _carries_reasoning, _merge_choices
from mira.llm.provider import LLMProvider, _without_reasoning

OD_URL = "https://amr-link.open-design.ai/v1"
_TOOLS = [
    {
        "type": "function",
        "function": {"name": "read_file", "parameters": {"type": "object", "properties": {}}},
    }
]

# The shape OpenDesign returned live: the text in one choice, the call in the next.
SPLIT_REPLY = {
    "choices": [
        {
            "index": 0,
            "message": {"role": "assistant", "content": "I'll start by reading the file."},
        },
        {
            "index": 1,
            "finish_reason": "tool_calls",
            "message": {
                "role": "assistant",
                "reasoning_details": [{"type": "reasoning.summary", "summary": "read it"}],
                "tool_calls": [
                    {
                        "id": "call_1",
                        "type": "function",
                        "function": {"name": "read_file", "arguments": '{"path": "a.py"}'},
                    }
                ],
            },
        },
    ],
    "usage": {
        "prompt_tokens": 362,
        "prompt_tokens_details": {"cached_read_tokens": 256, "cached_tokens": 256},
        "completion_tokens": 46,
        "completion_tokens_details": {"reasoning_tokens": 12},
        "total_tokens": 408,
    },
}


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("OPENDESIGN_API_KEY", "sk-amr-test")
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    registry._live_limits.clear()
    # Calls made by one test must not land in the next test's database.
    ledger.flush()
    ledger.set_sink(_ListSink())
    yield
    ledger.flush()
    registry._live_limits.clear()
    ledger.set_sink(None)


@pytest.fixture
def db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AppDatabase:
    monkeypatch.setenv("MIRA_INDEX_DIR", str(tmp_path))
    database = AppDatabase(url="", admin_password="admin")
    monkeypatch.setattr("mira.dashboard.api._app_db", database)
    return database


def _client_factory(handler):
    return partial(httpx.AsyncClient, transport=httpx.MockTransport(handler))


def _od(model: str = "deepseek-v4.1-flash-eco", **kw) -> LLMProvider:
    return LLMProvider(
        LLMConfig(model=model, base_url=OD_URL, retry_min_wait=0, retry_max_wait=0, **kw)
    )


# ── The preset ───────────────────────────────────────────────────────


class TestPreset:
    def test_profile_matches_the_endpoint(self):
        p = profiles.resolve(OD_URL)
        assert p["name"] == "open-design"
        assert p["label"] == "OpenDesign Cloud"
        assert p["api_key_env"] == "OPENDESIGN_API_KEY"
        assert p["model_prefix"] == "strip"
        assert p["stream"] is True and p["echo_reasoning"] is True
        assert p["usage_format"] == "ledger"
        assert not p["usage_url"]

    def test_offered_as_a_preset_with_its_plans(self):
        option = next(o for o in endpoints.preset_options() if o["id"] == "open-design")
        assert option["base_url"] == OD_URL
        assert option["reports_usage"] is True
        assert option["usage_estimated"] is True
        assert [p["id"] for p in option["plans"]] == ["go", "plus", "pro", "max"]

    def test_recommends_eco(self):
        assert endpoints.recommended_model("open-design") == "deepseek-v4.1-flash-eco"

    def test_llm_provider_shortcut(self):
        config = LLMConfig(provider="open-design")
        assert config.base_url == OD_URL
        assert config.api_key_env == "OPENDESIGN_API_KEY"


# ── Gateway quirks ───────────────────────────────────────────────────


class TestSplitChoices:
    def test_merge_concatenates_content_and_tool_calls(self):
        merged = _merge_choices(SPLIT_REPLY["choices"])
        message = merged["message"]
        assert message["content"] == "I'll start by reading the file."
        assert message["tool_calls"][0]["function"]["name"] == "read_file"
        assert message["reasoning_details"][0]["type"] == "reasoning.summary"
        assert merged["finish_reason"] == "tool_calls"

    def test_one_choice_is_left_alone(self):
        only = {"message": {"content": "x"}, "finish_reason": "stop"}
        assert _merge_choices([only]) is only

    def test_length_wins_over_other_finish_reasons(self):
        merged = _merge_choices(
            [
                {"message": {"content": "a"}, "finish_reason": "stop"},
                {"message": {"content": "b"}, "finish_reason": "length"},
            ]
        )
        assert merged["finish_reason"] == "length"
        assert merged["message"]["content"] == "ab"

    @pytest.mark.asyncio
    async def test_the_agentic_hop_keeps_the_tool_call(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=SPLIT_REPLY)

        provider = _od()
        with patch("mira.llm.provider.httpx.AsyncClient", side_effect=_client_factory(handler)):
            message = await provider.complete_agentic(
                [{"role": "user", "content": "review"}], tools=_TOOLS
            )
        assert message["tool_calls"][0]["id"] == "call_1"
        assert message["content"].startswith("I'll start")

    @pytest.mark.asyncio
    async def test_a_forced_tool_call_is_found_in_the_second_choice(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=SPLIT_REPLY)

        tools = [
            {
                "type": "function",
                "function": {
                    "name": "read_file",
                    "parameters": {
                        "type": "object",
                        "properties": {"path": {"type": "string"}},
                        "required": ["path"],
                    },
                },
            }
        ]
        provider = _od(json_schema_mode=False)
        with patch("mira.llm.provider.httpx.AsyncClient", side_effect=_client_factory(handler)):
            arguments = await provider.complete_with_tools(
                [{"role": "user", "content": "x"}], tools=tools
            )
        assert json.loads(arguments) == {"path": "a.py"}


class TestEmptyChoices:
    def test_the_detail_says_the_tokens_went_to_thinking(self):
        data = {
            "choices": [],
            "usage": {
                "prompt_tokens": 20,
                "completion_tokens": 50,
                "completion_tokens_details": {"reasoning_tokens": 50},
            },
        }
        with pytest.raises(LLMError) as raised:
            LLMProvider._chat_message(data)
        assert "50 completion tokens were spent" in str(raised.value)
        assert "50 of them on reasoning" in str(raised.value)
        # Still the retriable kind: the same request with room may answer.
        assert not isinstance(raised.value, NonRetriableLLMError)

    def test_without_usage_it_reads_as_before(self):
        with pytest.raises(LLMError, match="no choices returned"):
            LLMProvider._chat_message({"choices": []})


class TestReasoningDetails:
    def test_counts_as_reasoning(self):
        assert _carries_reasoning({"reasoning_details": [{"type": "reasoning.encrypted"}]})
        assert not _carries_reasoning({"reasoning_details": []})

    def test_stripped_when_reasoning_is_not_echoed(self):
        message = {
            "role": "assistant",
            "content": "",
            "reasoning_details": [{"type": "reasoning.summary", "summary": "s"}],
            "reasoning": "r",
        }
        assert _without_reasoning(message) == {"role": "assistant", "content": ""}

    @pytest.mark.asyncio
    async def test_echoed_back_to_open_design(self):
        seen: list[dict] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(json.loads(request.content))
            return httpx.Response(200, json={"choices": [{"message": {"content": "done"}}]})

        messages = [
            {"role": "user", "content": "review"},
            {
                "role": "assistant",
                "content": "",
                "reasoning_details": [{"type": "reasoning.encrypted", "data": "x-0"}],
                "tool_calls": [],
            },
        ]
        with patch("mira.llm.provider.httpx.AsyncClient", side_effect=_client_factory(handler)):
            await _od().complete_agentic(messages, tools=_TOOLS)
            await LLMProvider(
                LLMConfig(model="m", base_url="https://llm.example/v1")
            ).complete_agentic(messages, tools=_TOOLS)
        assert "reasoning_details" in seen[0]["messages"][1]
        assert "reasoning_details" not in seen[1]["messages"][1]


class TestQuota:
    @pytest.mark.asyncio
    async def test_a_spent_quota_is_not_retried(self, caplog: pytest.LogCaptureFixture):
        calls = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            return httpx.Response(
                429, json={"error": {"code": "quota_exceeded", "message": "Weekly quota exhausted"}}
            )

        with (
            patch("mira.llm.provider.httpx.AsyncClient", side_effect=_client_factory(handler)),
            pytest.raises(LLMError),
        ):
            await _od(max_retries=3).complete([{"role": "user", "content": "x"}], json_mode=False)
        assert calls == 1
        assert "allowance looks spent" in caplog.text

    @pytest.mark.asyncio
    async def test_a_plain_rate_limit_is_still_retried(self):
        calls = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            if calls == 1:
                return httpx.Response(429, json={"error": {"message": "rate limit exceeded"}})
            return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

        with patch("mira.llm.provider.httpx.AsyncClient", side_effect=_client_factory(handler)):
            reply = await _od(max_retries=3).complete(
                [{"role": "user", "content": "x"}], json_mode=False
            )
        assert reply == "ok" and calls == 2

    @pytest.mark.parametrize(
        ("status", "body", "spent"),
        [
            (402, {"error": {"message": "Insufficient balance"}}, True),
            (403, {"error": {"message": "plan limit reached"}}, True),
            (403, {"error": {"message": "usage limit for this window"}}, True),
            (429, {"error": {"message": "credits exhausted"}}, True),
            (429, {"error": {"message": "Too many requests"}}, False),
            (403, {"error": {"message": "forbidden"}}, False),
            (500, {"error": {"message": "quota service down"}}, False),
        ],
    )
    def test_classification(self, status, body, spent):
        from mira.llm.base import _quota_exhausted

        assert _quota_exhausted(httpx.Response(status, json=body)) is spent


# ── Usage parsing ────────────────────────────────────────────────────


class TestUsageParsing:
    def test_open_design_shape(self):
        usage = accounting.from_chat(SPLIT_REPLY["usage"])
        assert (usage.prompt, usage.cached, usage.completion, usage.reasoning) == (
            362,
            256,
            46,
            12,
        )

    def test_deepseek_hit_tokens(self):
        usage = accounting.from_chat(
            {"prompt_tokens": 1000, "prompt_cache_hit_tokens": 900, "completion_tokens": 5}
        )
        assert usage.cached == 900

    def test_responses_api(self):
        usage = accounting.from_responses(
            {
                "input_tokens": 500,
                "input_tokens_details": {"cached_tokens": 400},
                "output_tokens": 9,
            }
        )
        assert (usage.prompt, usage.cached, usage.completion) == (500, 400, 9)

    def test_bedrock_adds_cache_reads_back(self):
        usage = accounting.from_bedrock(
            {"inputTokens": 10, "cacheReadInputTokens": 90, "outputTokens": 3}
        )
        assert (usage.prompt, usage.cached) == (100, 90)

    def test_garbage_is_no_usage(self):
        assert not accounting.from_chat("nope")
        assert not accounting.from_chat({"prompt_tokens": "x", "completion_tokens": None})
        # A cache count larger than the prompt is clamped, not trusted.
        assert accounting.from_chat({"prompt_tokens": 5, "prompt_cache_hit_tokens": 50}).cached == 5


# ── The registry ─────────────────────────────────────────────────────


class TestScopedRegistry:
    def test_same_id_on_two_endpoints(self):
        go = registry.get("deepseek-v4.1-flash")
        od = registry.get("deepseek-v4.1-flash", "open-design")
        assert go["endpoint"] == "opencode-go"
        assert od["endpoint"] == "open-design"
        assert od["credit_multiplier"] == 0.5
        assert "credit_multiplier" not in go
        # Asked for on Go, the plain entry: nothing changes for existing callers.
        assert registry.get("deepseek-v4.1-flash", "opencode-go") is go

    def test_an_id_that_exists_only_scoped_is_found_bare(self):
        entry = registry.get("deepseek-v4.1-flash-eco")
        assert entry is not None and entry["endpoint"] == "open-design"

    def test_entries_report_bare_ids(self):
        ids = [model_id for model_id, _ in registry.entries()]
        assert all(registry.SCOPE not in model_id for model_id in ids)
        assert ids.count("deepseek-v4.1-flash") == 2

    def test_catalogue_entries_use_the_bare_id(self):
        from mira.dashboard.model_catalog import build_options

        values = [o["value"] for o in build_options("open-design", None, "review")]
        assert values[0] == "deepseek-v4.1-flash-eco"
        assert "deepseek-v4.1-flash" in values
        assert not any(registry.SCOPE in v for v in values)

    def test_go_prices_corrected(self):
        entry = registry.get("deepseek-v4.1-flash")
        assert (entry["input_cost_per_1m"], entry["output_cost_per_1m"]) == (0.30, 1.20)
        assert entry["monthly_limit_usd"] == 100


class TestEstimates:
    PEAK = datetime(2026, 11, 13, 10, 0, tzinfo=UTC).timestamp()
    OFF_PEAK = datetime(2026, 11, 13, 18, 0, tzinfo=UTC).timestamp()

    def test_cached_tokens_bill_at_the_cache_rate_and_credits_apply_the_multiplier(self):
        estimate = registry.estimate(
            "deepseek-v4.1-flash-eco",
            "open-design",
            prompt_tokens=1_000_000,
            cached_tokens=800_000,
            completion_tokens=100_000,
            at=self.PEAK,
        )
        expected = 200_000 * 0.30 / 1e6 + 800_000 * 0.006 / 1e6 + 100_000 * 1.20 / 1e6
        assert estimate.priced
        assert estimate.cost_usd == pytest.approx(expected)
        assert estimate.credits == pytest.approx(expected * 0.27)

    def test_off_peak_rates(self):
        peak = registry.price("deepseek-v4.1-flash", "opencode-go", at=self.PEAK)
        off = registry.price("deepseek-v4.1-flash", "opencode-go", at=self.OFF_PEAK)
        assert (peak.input, peak.off_peak) == (0.30, False)
        assert (off.input, off.output, off.cache_read, off.off_peak) == (0.15, 0.60, 0.003, True)

    def test_window_wraps_midnight(self):
        at = datetime(2026, 11, 13, 0, 15, tzinfo=UTC).timestamp()
        assert registry.in_window("16:30-00:30", at)
        assert not registry.in_window("16:30-00:30", self.PEAK)

    def test_long_context_tier(self):
        short = registry.price("qwen3.7-plus", "opencode-go", at=self.PEAK, prompt_tokens=1000)
        long = registry.price("qwen3.7-plus", "opencode-go", at=self.PEAK, prompt_tokens=300_000)
        assert (short.input, long.input, long.cache_read) == (0.40, 1.20, 0.12)

    def test_an_unknown_model_is_unpriced_not_guessed(self):
        estimate = registry.estimate(
            "local/llama", None, prompt_tokens=10, cached_tokens=0, completion_tokens=10, at=0
        )
        assert not estimate.priced and estimate.cost_usd == 0

    def test_live_limits_fill_the_gap(self):
        # Known on Go at Go's limits; on OpenDesign the catalogue's budget applies.
        registry.remember_limits(
            "open-design", "deepseek-v4-pro", max_input=328000, max_output=128000
        )
        assert registry.max_output_tokens("deepseek-v4-pro", endpoint="open-design") == 128000
        assert registry.max_output_tokens("deepseek-v4-pro", endpoint="opencode-go") == 384000
        # A curated entry for the endpoint is not overridden.
        registry.remember_limits("open-design", "deepseek-v4.1-flash", max_input=1, max_output=1)
        assert registry.max_output_tokens("deepseek-v4.1-flash", endpoint="open-design") == 128000

    def test_catalogue_limits_are_read_from_metadata(self):
        from mira.dashboard.model_catalog import _catalog_limits

        assert _catalog_limits(
            {"id": "glm-5.3-flash", "metadata": {"context_budget": 232000, "output_budget": 32000}}
        ) == {"max_input_tokens": 232000, "max_output_tokens": 32000}
        assert _catalog_limits({"id": "x"}) == {}


# ── The ledger ───────────────────────────────────────────────────────


class _ListSink:
    def __init__(self) -> None:
        self.rows: list[dict] = []

    def record_llm_usage(self, entries):
        self.rows.extend(entries)
        return len(entries)

    def prune_llm_usage(self, *, max_age_days):
        return 0


class TestLedger:
    @pytest.mark.asyncio
    async def test_a_call_lands_in_the_ledger_with_its_cost(self):
        sink = _ListSink()
        ledger.set_sink(sink)

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=SPLIT_REPLY)

        provider = _od(usage_purpose="review")
        with patch("mira.llm.provider.httpx.AsyncClient", side_effect=_client_factory(handler)):
            await provider.complete_agentic([{"role": "user", "content": "x"}], tools=_TOOLS)
        ledger.flush()

        (row,) = sink.rows
        assert row["endpoint"] == "config"
        assert row["provider"] == "open-design"
        assert row["model"] == "deepseek-v4.1-flash-eco"
        assert row["purpose"] == "review"
        assert (row["prompt_tokens"], row["cached_tokens"], row["completion_tokens"]) == (
            362,
            256,
            46,
        )
        assert row["priced"] == 1 and row["credits"] == pytest.approx(row["cost_usd"] * 0.27)
        usage = provider.usage
        assert usage["cached_tokens"] == 256
        assert usage["cost_usd"] == pytest.approx(row["cost_usd"], abs=1e-6)

    def test_a_failing_sink_never_reaches_the_caller(self):
        class Broken:
            def record_llm_usage(self, entries):
                raise RuntimeError("disk full")

            def prune_llm_usage(self, *, max_age_days):
                raise RuntimeError("disk full")

        ledger.set_sink(Broken())
        before = ledger.stats()["write_errors"]
        ledger.submit({"model": "m"})
        ledger.flush()
        assert ledger.stats()["write_errors"] >= before

    def test_switched_off(self, monkeypatch: pytest.MonkeyPatch):
        sink = _ListSink()
        ledger.set_sink(sink)
        monkeypatch.setenv("MIRA_USAGE_LEDGER", "0")
        ledger.submit({"model": "m"})
        ledger.flush()
        assert sink.rows == []

    def test_app_database_round_trip_and_retention(self, db: AppDatabase):
        now = time.time()
        rows = [
            {
                "created_at": now - 100 * 86400,
                "endpoint": "od",
                "provider": "open-design",
                "model": "deepseek-v4.1-flash-eco",
                "purpose": "review",
                "prompt_tokens": 10,
                "credits": 9.0,
                "priced": 1,
            },
            {
                "created_at": now - 60,
                "endpoint": "od",
                "provider": "open-design",
                "model": "deepseek-v4.1-flash-eco",
                "purpose": "review",
                "prompt_tokens": 1000,
                "cached_tokens": 800,
                "completion_tokens": 50,
                "cost_usd": 0.01,
                "credits": 0.0027,
                "priced": 1,
            },
            {
                "created_at": now - 30,
                "endpoint": "od",
                "provider": "open-design",
                "model": "glm-5.3-flash",
                "purpose": "indexing",
                "prompt_tokens": 5,
                "priced": 0,
            },
        ]
        assert db.record_llm_usage(rows) == 3
        summary = db.llm_usage_summary(since=now - 86400)
        assert {(r["model"], r["purpose"]) for r in summary} == {
            ("deepseek-v4.1-flash-eco", "review"),
            ("glm-5.3-flash", "indexing"),
        }
        eco = next(r for r in summary if r["model"] == "deepseek-v4.1-flash-eco")
        assert (eco["calls"], eco["cached_tokens"], eco["unpriced_calls"]) == (1, 800, 0)
        credits, earliest = db.llm_usage_credits("od", since=now - 86400)
        assert credits == pytest.approx(0.0027) and earliest == pytest.approx(now - 60)
        assert db.prune_llm_usage(max_age_days=90) == 1
        assert db.llm_usage_credits("od", since=0)[0] == pytest.approx(0.0027)


# ── The estimated meter ──────────────────────────────────────────────


def _od_endpoint(db, **kw):
    return endpoints.create(label="OpenDesign", base_url=OD_URL, preset="open-design", db=db, **kw)


class TestEstimatedMeter:
    def _spend(self, db, endpoint_id, credits, at):
        db.record_llm_usage(
            [
                {
                    "created_at": at,
                    "endpoint": endpoint_id,
                    "provider": "open-design",
                    "model": "deepseek-v4.1-flash-eco",
                    "credits": credits,
                    "priced": 1,
                }
            ]
        )

    def test_go_has_a_five_hour_and_an_anchored_weekly_window(self, db):
        now = datetime(2026, 11, 13, 12, 0, tzinfo=UTC).timestamp()
        endpoint = _od_endpoint(db, plan="go", plan_started="2026-11-10")
        self._spend(db, endpoint.id, 1.0, now - 3600)  # in both windows
        self._spend(db, endpoint.id, 2.0, now - 10 * 3600)  # weekly only
        self._spend(db, endpoint.id, 50.0, now - 4 * 86400)  # before this week began

        snapshot = key_providers.ledger_snapshot(
            endpoint.id,
            endpoints.profile_for(endpoint),
            key_providers.PlanSettings.of_endpoint(endpoint),
            db,
            now=now,
        )
        assert snapshot.source == "estimate"
        assert snapshot.plan == "Go"
        assert snapshot.primary.window_minutes == 300
        assert snapshot.primary.used_percent == pytest.approx(1.0 / 5.0 * 100)
        assert snapshot.primary.resets_at is None  # rolling
        assert snapshot.secondary.used_percent == pytest.approx(3.0 / 15.0 * 100)
        week_start = datetime(2026, 11, 10, tzinfo=UTC).timestamp()
        assert snapshot.secondary.resets_at == pytest.approx(week_start + 7 * 86400)
        assert "check the provider's console" in snapshot.estimate["note"]
        left = {entry["model"]: entry["usd"] for entry in snapshot.estimate["left_as"]}
        # 4 credits left in the 5-hour window: $14.81 of Eco or $8 of Flash.
        assert left["deepseek-v4.1-flash-eco"] == pytest.approx(4.0 / 0.27, abs=0.01)
        assert left["deepseek-v4.1-flash"] == pytest.approx(8.0)

    def test_calibration_scales_the_estimate(self, db):
        now = time.time()
        endpoint = _od_endpoint(db, plan="plus", cost_scale=2.0)
        self._spend(db, endpoint.id, 5.0, now - 60)
        snapshot = key_providers.ledger_snapshot(
            endpoint.id,
            endpoints.profile_for(endpoint),
            key_providers.PlanSettings.of_endpoint(endpoint),
            db,
            now=now,
        )
        assert snapshot.secondary is None
        assert snapshot.primary.used_percent == pytest.approx(10.0 / 20.0 * 100)

    def test_no_plan_no_meter(self, db):
        endpoint = _od_endpoint(db)
        assert (
            key_providers.ledger_snapshot(
                endpoint.id, endpoints.profile_for(endpoint), key_providers.PlanSettings(), db
            )
            is None
        )

    @pytest.mark.asyncio
    async def test_the_card_says_it_is_an_estimate(self, db):
        endpoint = _od_endpoint(db, plan="go")
        card = await key_providers.endpoint_card(endpoint, is_default=True, db=db)
        assert card["reports_usage"] and card["usage_estimated"]
        assert card["plan"] == "go"
        assert card["usage"]["source"] == "estimate"
        assert card["usage"]["estimate"]["note"]

    def test_plan_settings_are_validated(self, db):
        with pytest.raises(endpoints.EndpointError, match="Plan must be one of"):
            _od_endpoint(db, plan="enterprise")
        with pytest.raises(endpoints.EndpointError, match="YYYY-MM-DD"):
            _od_endpoint(db, plan="go", plan_started="last tuesday")
        with pytest.raises(endpoints.EndpointError, match="calibration"):
            _od_endpoint(db, plan="go", cost_scale=0)
        with pytest.raises(endpoints.EndpointError, match="Plan must be one of"):
            endpoints.create(
                label="Go",
                base_url="https://opencode.ai/zen/go/v1",
                preset="opencode-go",
                plan="go",
                db=db,
            )


# ── The cost view ────────────────────────────────────────────────────


class TestCostReport:
    def test_per_endpoint_and_model(self, db):
        endpoint = _od_endpoint(db, plan="go", cost_scale=1.5)
        now = time.time()
        db.record_llm_usage(
            [
                {
                    "created_at": now - 86400 * 2,
                    "endpoint": endpoint.id,
                    "provider": "open-design",
                    "model": "deepseek-v4.1-flash-eco",
                    "purpose": purpose,
                    "prompt_tokens": 1000,
                    "cached_tokens": 750,
                    "completion_tokens": 100,
                    "cost_usd": 0.01,
                    "credits": 0.0027,
                    "priced": 1,
                }
                for purpose in ("review", "indexing")
            ]
        )
        report = costs.report(db, days=7, now=now)
        (row,) = report["rows"]
        assert row["endpoint_label"] == "OpenDesign"
        assert row["model_label"] == "DeepSeek V4.1 Flash Eco"
        assert row["calls"] == 2
        assert row["cache_hit_rate"] == 0.75
        assert row["purposes"] == {"review": 1, "indexing": 1}
        assert row["credits"] == pytest.approx(0.0054 * 1.5)
        # Two days of history projected over thirty.
        assert row["projected_month_usd"] == pytest.approx(0.02 / 2 * 30, rel=0.01)
        assert report["total"]["calls"] == 2

    def test_route(self, db):
        from fastapi import HTTPException

        from mira.dashboard.routers.providers import usage_costs
        from tests.test_oauth import _admin

        assert usage_costs(_admin(), days=7)["rows"] == []
        with pytest.raises(HTTPException):
            usage_costs(_admin(), days=5)


# ── Per-review totals ────────────────────────────────────────────────


def test_review_record_carries_cost(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from mira.index.store import IndexStore

    monkeypatch.setenv("MIRA_INDEX_DIR", str(tmp_path))
    store = IndexStore(str(tmp_path / "r.db"))
    store.record_review(
        pr_number=1,
        pr_title="t",
        pr_url="u",
        comments_posted=0,
        blockers=0,
        warnings=0,
        tokens_used=1100,
        prompt_tokens=1000,
        cached_tokens=900,
        completion_tokens=100,
        cost_usd=0.0123,
    )
    (event,) = store.list_review_events()
    assert (event.prompt_tokens, event.cached_tokens, event.cost_usd) == (1000, 900, 0.0123)
    stats = store.get_review_stats()
    assert stats["total_cached_tokens"] == 900
    assert stats["total_cost_usd"] == pytest.approx(0.0123)


def test_llm_config_for_tags_the_purpose():
    from mira.dashboard.models_config import llm_config_for

    config = llm_config_for(
        "indexing", LLMConfig(review_fallback_models=[], indexing_fallback_models=["m2"])
    )
    assert config.usage_purpose == "indexing"
    assert all(f.usage_purpose == "indexing" for f in config.fallbacks)


def test_an_id_only_scoped_on_several_endpoints_resolves_by_scope_name(monkeypatch) -> None:
    from mira.llm import registry

    models = {
        "solo@zeta": {"label": "Z", "input_cost_per_1m": 9.0, "output_cost_per_1m": 9.0},
        "solo@alpha": {"label": "A", "input_cost_per_1m": 1.0, "output_cost_per_1m": 2.0},
    }
    monkeypatch.setattr(registry, "_load", lambda: models)
    assert registry.get("solo")["endpoint"] == "alpha"
    assert registry.pricing("solo") == (1.0, 2.0)
    assert registry.pricing("solo", "zeta") == (9.0, 9.0)


def test_indexing_estimate_prices_at_the_endpoint_scoped_rate(monkeypatch) -> None:
    from mira.dashboard.models_config import estimate_indexing_cost
    from mira.llm import registry

    models = {
        "m": {"label": "M", "input_cost_per_1m": 10.0, "output_cost_per_1m": 10.0},
        "m@cheap": {"label": "M", "input_cost_per_1m": 1.0, "output_cost_per_1m": 1.0},
    }
    monkeypatch.setattr(registry, "_load", lambda: models)
    plain = estimate_indexing_cost(1000, "m")["estimated_usd"]
    cheap = estimate_indexing_cost(1000, "m", "cheap")["estimated_usd"]
    assert cheap < plain
