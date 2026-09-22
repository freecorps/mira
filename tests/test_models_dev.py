"""Tests for the models.dev reasoning-level catalogue and its use on the wire."""

from __future__ import annotations

import logging
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from mira.config import LLMConfig
from mira.llm import models_dev
from mira.llm.provider import LLMProvider

DOCUMENT = {
    "opencode-go": {
        "id": "opencode-go",
        "api": "https://opencode.ai/zen/go/v1",
        "models": {
            "glm-5.3-flash": {
                "reasoning": True,
                "reasoning_options": [{"type": "effort", "values": ["low", "high", "max"]}],
            },
            "kimi-k2.7-code": {"reasoning": True, "reasoning_options": []},
            "deepseek-v4-flash": {
                "reasoning": True,
                "reasoning_options": [
                    {"type": "toggle"},
                    {"type": "effort", "values": ["low", "high", "max"]},
                ],
            },
        },
    },
    "openrouter": {
        "id": "openrouter",
        "api": "https://openrouter.ai/api/v1",
        "models": {
            "z-ai/glm-5.3-flash": {
                "reasoning_options": [{"type": "effort", "values": ["low", "high", "max"]}]
            },
            "openai/gpt-5.6": {
                "reasoning_options": [
                    {"type": "effort", "values": ["none", "low", "medium", "high", "xhigh"]}
                ]
            },
        },
    },
    "somewhere": {"id": "somewhere", "api": "https://llm.example.com/v1/", "models": {}},
    "_junk": "not a provider",
}


@pytest.fixture
def catalogue():
    models_dev.load(DOCUMENT)
    yield
    models_dev.reset()


class TestReduce:
    def test_effort_levels_per_model(self, catalogue):
        assert models_dev.levels_for("opencode-go", "glm-5.3-flash") == ("low", "high", "max")
        # Reasoning without an effort option: known, but nothing to pick.
        assert models_dev.levels_for("opencode-go", "kimi-k2.7-code") == ()
        # A toggle beside the effort option is ignored; the levels remain.
        assert models_dev.levels_for("opencode-go", "deepseek-v4-flash") == ("low", "high", "max")

    def test_unknown_model_or_provider_is_none(self, catalogue):
        assert models_dev.levels_for("opencode-go", "nope") is None
        assert models_dev.levels_for("nope", "glm-5.3-flash") is None
        assert models_dev.levels_for("opencode-go", "") is None

    def test_prefixed_and_bare_ids_find_each_other(self, catalogue):
        assert models_dev.levels_for("opencode-go", "zhipu/glm-5.3-flash") == ("low", "high", "max")
        assert models_dev.levels_for("openrouter", "z-ai/glm-5.3-flash") == ("low", "high", "max")

    def test_junk_in_the_document_is_skipped(self, catalogue):
        assert models_dev.levels_for("_junk", "x") is None


class TestProviderMatch:
    def test_preset_names_its_provider(self, catalogue):
        assert models_dev.provider_id_for("opencode-go") == "opencode-go"
        assert models_dev.provider_id_for("opencode-zen") == "opencode"
        assert models_dev.provider_id_for("openrouter") == "openrouter"

    def test_a_url_matches_when_the_preset_says_nothing(self, catalogue):
        assert models_dev.provider_id_for("", "https://opencode.ai/zen/go/v1/") == "opencode-go"
        assert models_dev.provider_id_for("ollama", "https://LLM.example.com/v1") == "somewhere"
        assert models_dev.provider_id_for("", "https://other.example.com/v1") == ""

    def test_levels_from_a_config(self, catalogue):
        cfg = LLMConfig(model="glm-5.3-flash", provider="opencode-go")
        assert models_dev.reasoning_levels(cfg) == ("low", "high", "max")
        assert models_dev.reasoning_levels(cfg, "kimi-k2.7-code") == ()
        # OpenRouter is the default base_url, keyed by full id.
        assert models_dev.reasoning_levels(LLMConfig(model="openai/gpt-5.6")) == (
            "none",
            "low",
            "medium",
            "high",
            "xhigh",
        )
        assert models_dev.reasoning_levels(LLMConfig(model="unknown/model")) is None


class TestSnap:
    def test_a_level_the_model_has_is_sent_as_is(self):
        assert models_dev.snap_effort(("low", "high", "max"), "high") == "high"

    def test_a_missing_level_snaps_to_the_next_below(self):
        assert models_dev.snap_effort(("low", "high"), "max") == "high"
        assert models_dev.snap_effort(("low", "high"), "medium") == "low"

    def test_below_the_lowest_snaps_up(self):
        assert models_dev.snap_effort(("medium", "high"), "low") == "medium"

    def test_unknown_scales_and_empty_lists_pass_through(self):
        assert models_dev.snap_effort((), "high") == "high"
        assert models_dev.snap_effort(("low", "high"), "ultra") == "ultra"


class TestWarm:
    async def test_offline_by_env_never_fetches(self, monkeypatch):
        monkeypatch.setenv("MIRA_MODELS_DEV_URL", "")
        with patch("mira.llm.models_dev.httpx.AsyncClient") as cls:
            assert await models_dev.warm() is False
            cls.assert_not_called()

    async def test_fetches_once_and_remembers_a_failure(self, monkeypatch, caplog):
        monkeypatch.setenv("MIRA_MODELS_DEV_URL", "https://models.example/api.json")
        models_dev.reset()
        client = AsyncMock()
        client.__aenter__ = AsyncMock(return_value=client)
        client.__aexit__ = AsyncMock(return_value=False)
        resp = MagicMock()
        resp.raise_for_status = MagicMock()
        resp.json.return_value = DOCUMENT
        client.get = AsyncMock(return_value=resp)
        with patch("mira.llm.models_dev.httpx.AsyncClient", return_value=client):
            assert await models_dev.warm() is True
            assert await models_dev.warm() is True
        assert client.get.await_count == 1
        assert models_dev.levels_for("opencode-go", "glm-5.3-flash") == ("low", "high", "max")

        models_dev.reset()
        client.get = AsyncMock(side_effect=RuntimeError("down"))
        with (
            patch("mira.llm.models_dev.httpx.AsyncClient", return_value=client),
            caplog.at_level(logging.WARNING, logger="mira.llm.models_dev"),
        ):
            assert await models_dev.warm() is False
            assert await models_dev.warm() is False  # the miss is remembered
        assert client.get.await_count == 1
        assert "Could not load models.dev" in caplog.text
        models_dev.reset()


class TestOnTheWire:
    """How the level reaches the request, per endpoint and per model."""

    def _body(self, config: LLMConfig, model: str = "") -> dict:
        provider = LLMProvider(config)
        body = {"model": model or config.model, "temperature": 0.2}
        provider._apply_reasoning(body)
        return body

    def test_openrouter_nests_the_level(self):
        body = self._body(LLMConfig(model="openai/gpt-5.6", reasoning_effort="high"))
        assert body["reasoning"] == {"effort": "high"}
        assert "reasoning_effort" not in body
        assert "temperature" not in body

    def test_openai_compatible_endpoints_use_the_openai_field(self):
        body = self._body(
            LLMConfig(model="glm-5.3-flash", provider="opencode-go", reasoning_effort="high")
        )
        assert body["reasoning_effort"] == "high"
        assert "reasoning" not in body

    def test_a_custom_endpoint_uses_the_openai_field_too(self):
        body = self._body(
            LLMConfig(
                model="local-model",
                base_url="http://localhost:11434/v1",
                api_key_env="",
                reasoning_effort="low",
            )
        )
        assert body["reasoning_effort"] == "low"

    def test_the_responses_api_always_nests(self):
        from mira.llm.responses import ResponsesProvider

        provider = ResponsesProvider(
            LLMConfig(
                model="gpt-5.6",
                provider="openai-api",
                api_style="responses",
                reasoning_effort="high",
            )
        )
        body = {"model": "gpt-5.6"}
        provider._apply_reasoning(body)
        assert body["reasoning"] == {"effort": "high"}
        assert "reasoning_effort" not in body

    def test_the_level_is_snapped_to_what_the_model_takes(self, catalogue, caplog):
        cfg = LLMConfig(model="glm-5.3-flash", provider="opencode-go", reasoning_effort="medium")
        with caplog.at_level(logging.INFO, logger="mira.llm.base"):
            body = self._body(cfg)
        assert body["reasoning_effort"] == "low"
        assert "takes reasoning levels low/high/max; sending low for medium" in caplog.text

    def test_a_model_the_catalogue_does_not_know_is_sent_as_written(self, catalogue):
        cfg = LLMConfig(model="mystery", provider="opencode-go", reasoning_effort="medium")
        assert self._body(cfg)["reasoning_effort"] == "medium"

    def test_the_profile_rename_runs_before_the_snap(self, catalogue):
        cfg = LLMConfig(model="openai/gpt-5.6", reasoning_effort="max")
        # OpenRouter spells "max" as "xhigh", which this model has.
        assert self._body(cfg)["reasoning"] == {"effort": "xhigh"}
