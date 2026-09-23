"""A model served over another protocol than its endpoint's is called that way.

OpenCode Go serves most models on Chat Completions and Muse Spark (with its
GPT and Grok models) only on the Responses API; a Chat Completions request
for Muse Spark came back 503 ``Router.Unavailable``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from mira.config import LLMConfig
from mira.dashboard.db import AppDatabase
from mira.dashboard.model_catalog import endpoint_options, protocol_detail
from mira.dashboard.models_config import apply_endpoint_binding, bind_model, describe_call
from mira.llm import create_llm, endpoints, models_dev, protocols
from mira.llm.provider import LLMProvider
from mira.llm.responses import ResponsesProvider
from mira.oauth.routes import endpoint_route

GO_URL = "https://opencode.ai/zen/go/v1"

# A trimmed models.dev document, in its real shape.
DOCUMENT = {
    "opencode-go": {
        "npm": "@ai-sdk/openai-compatible",
        "api": GO_URL,
        "models": {
            "kimi-k2.7-code": {},
            "brand-new-model": {"provider": {"npm": "@ai-sdk/openai"}},
            "minimax-m3": {"provider": {"npm": "@ai-sdk/anthropic"}},
            "same-as-provider": {"provider": {"npm": "@ai-sdk/openai-compatible"}},
        },
    },
    "openai": {
        "npm": "@ai-sdk/openai",
        "api": "https://api.openai.com/v1",
        "models": {"gpt-5": {}},
    },
}


@pytest.fixture
def db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AppDatabase:
    monkeypatch.setenv("MIRA_INDEX_DIR", str(tmp_path))
    database = AppDatabase(url="", admin_password="admin")
    monkeypatch.setattr("mira.dashboard.api._app_db", database)
    return database


def _go_endpoint(db: AppDatabase) -> endpoints.Endpoint:
    endpoint = endpoints.create(label="OpenCode Go", base_url="", preset="opencode-go", db=db)
    endpoints.set_secret(endpoint.id, "oc_sk_test", db)
    return endpoint


def _go_config(model: str) -> LLMConfig:
    return LLMConfig(provider="opencode-go", model=model)


class TestModelsDev:
    def test_only_per_model_exceptions_are_kept(self):
        reduced = models_dev.reduce_protocols(DOCUMENT)
        assert reduced == {
            "opencode-go": {"brand-new-model": "responses", "minimax-m3": "anthropic"}
        }

    def test_native_protocol_after_load(self):
        models_dev.load(DOCUMENT)
        assert models_dev.native_protocol("opencode-go", "brand-new-model") == "responses"
        assert models_dev.native_protocol("opencode-go", "kimi-k2.7-code") is None
        assert models_dev.native_protocol("openai", "gpt-5") is None


class TestResolution:
    def test_the_registry_knows_muse_spark_without_the_network(self):
        assert not models_dev.loaded()
        config = _go_config("muse-spark-1.3-contributor")
        assert protocols.native_protocol(config) == "responses"
        assert protocols.api_style_for(config) == "responses"

    def test_models_dev_covers_models_the_registry_does_not_list(self):
        models_dev.load(DOCUMENT)
        assert protocols.api_style_for(_go_config("brand-new-model")) == "responses"

    def test_a_model_without_an_exception_keeps_the_endpoints_protocol(self):
        models_dev.load(DOCUMENT)
        assert protocols.api_style_for(_go_config("kimi-k2.7-code")) == "chat"
        # An endpoint set to the Responses API stays there.
        config = _go_config("kimi-k2.7-code").model_copy(update={"api_style": "responses"})
        assert protocols.api_style_for(config) == "responses"

    def test_the_registry_entry_only_applies_on_its_own_endpoint(self):
        # The same bare id sent through OpenRouter is not an OpenCode Go call.
        config = LLMConfig(model="muse-spark-1.3-contributor")
        assert protocols.native_protocol(config) is None

    def test_a_protocol_mira_does_not_speak_leaves_the_endpoints(self):
        models_dev.load(DOCUMENT)
        config = _go_config("minimax-m3")
        assert protocols.api_style_for(config) == "chat"
        assert protocols.unsupported_protocol(config) == "anthropic"

    def test_oauth_and_bedrock_are_left_alone(self):
        oauth = _go_config("muse-spark-1.3-contributor").model_copy(
            update={"oauth_provider": "chatgpt", "api_style": "chat"}
        )
        assert protocols.api_style_for(oauth) == "chat"


class TestFactory:
    def test_muse_spark_on_opencode_go_gets_the_responses_client(self):
        llm = create_llm(_go_config("muse-spark-1.3-contributor"))
        assert isinstance(llm, ResponsesProvider)
        assert llm._url == f"{GO_URL}/responses"

    def test_other_go_models_stay_on_chat_completions(self):
        assert isinstance(create_llm(_go_config("kimi-k2.7-code")), LLMProvider)


class TestBinding:
    def test_an_endpoint_route_takes_the_models_protocol(self, db: AppDatabase):
        go = _go_endpoint(db)
        bound = bind_model(
            LLMConfig(),
            endpoint_route(go.id, "muse-spark-1.3-contributor"),
            model_is_explicit=True,
            default=("", ""),
        )
        assert bound.endpoint == go.id
        assert bound.api_style == "responses"
        assert describe_call(bound)["protocol"] == "Responses API"

    def test_a_chat_model_on_the_same_endpoint_stays_on_chat(self, db: AppDatabase):
        go = _go_endpoint(db)
        bound = bind_model(
            LLMConfig(),
            endpoint_route(go.id, "kimi-k2.7-code"),
            model_is_explicit=True,
            default=("", ""),
        )
        assert bound.api_style == "chat"


class TestPicker:
    def test_each_option_names_its_own_protocol(self, db: AppDatabase):
        go = _go_endpoint(db)
        config = apply_endpoint_binding(LLMConfig(), go.id, model_is_explicit=True, db=db)
        entries = [
            {
                "endpoint": go,
                "config": config,
                "backend": "opencode-go",
                "catalog": None,
                "group": "OpenCode Go",
                "key_detail": "stored key",
            }
        ]
        by_model = {
            option["value"].rsplit(":", 1)[-1]: option["detail"]
            for option in endpoint_options(entries, "review")
        }
        assert by_model["muse-spark-1.3-contributor"] == "Responses API · stored key"
        assert by_model["kimi-k2.7-code"] == "Chat Completions · stored key"

    def test_an_unspoken_protocol_is_said_out_loud(self):
        models_dev.load(DOCUMENT)
        detail = protocol_detail(_go_config("minimax-m3"), "minimax-m3", "API key")
        assert detail.startswith("Chat Completions · API key")
        assert "Anthropic Messages API" in detail
