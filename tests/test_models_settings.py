"""Tests for model override visibility and clearing (issue #124).

Covers:
- `llm_config_for` logging the effective model and its source.
- `set_models` accepting "" (inherit from config) and free-form model ids.
- `get_models` reporting the override source and the config-resolved models.
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from mira.config import LLMConfig
from mira.dashboard.api import ModelsUpdate
from mira.dashboard.db import AppDatabase
from mira.dashboard.models_config import llm_config_for
from mira.dashboard.routers.admin import get_models, set_models


def _admin_req():
    from types import SimpleNamespace

    user = SimpleNamespace(is_admin=True)
    return SimpleNamespace(state=SimpleNamespace(user=user))


@pytest.fixture
def in_memory_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AppDatabase:
    """Fresh per-test SQLite DB swapped in for the module-level `_app_db`."""
    monkeypatch.setenv("MIRA_INDEX_DIR", str(tmp_path))
    db = AppDatabase(url="", admin_password="admin")
    monkeypatch.setattr("mira.dashboard.api._app_db", db)
    return db


class TestEffectiveModelLogging:
    def test_dashboard_override_logged_as_source(
        self, in_memory_db: AppDatabase, caplog: pytest.LogCaptureFixture
    ):
        in_memory_db.set_setting("review_model", "custom/model-x")
        with caplog.at_level(logging.INFO, logger="mira.dashboard.models_config"):
            resolved = llm_config_for("review", LLMConfig(review_model="openai/gpt-5.1"))
        assert resolved.model == "custom/model-x"
        assert "Review model: custom/model-x (source: dashboard setting)" in caplog.text

    def test_config_model_logged_as_mira_yaml(
        self, in_memory_db: AppDatabase, caplog: pytest.LogCaptureFixture
    ):
        with caplog.at_level(logging.INFO, logger="mira.dashboard.models_config"):
            resolved = llm_config_for("review", LLMConfig(review_model="openai/gpt-5.1"))
        assert resolved.model == "openai/gpt-5.1"
        assert "Review model: openai/gpt-5.1 (source: mira.yaml)" in caplog.text

    def test_fallback_model_logged_as_default(
        self, in_memory_db: AppDatabase, caplog: pytest.LogCaptureFixture
    ):
        with caplog.at_level(logging.INFO, logger="mira.dashboard.models_config"):
            llm_config_for("indexing", LLMConfig(model="anthropic/claude-sonnet-4-6"))
        assert "Indexing model: anthropic/claude-sonnet-4-6 (source: default)" in caplog.text


class TestSetModelsInheritAndCustom:
    def test_empty_value_clears_override(self, in_memory_db: AppDatabase):
        in_memory_db.set_setting("review_model", "anthropic/claude-sonnet-4-6")
        body = ModelsUpdate(indexing_model="", review_model="")
        assert set_models(body, _admin_req()) == {"ok": True}
        assert in_memory_db.get_setting("review_model") == ""
        # Cleared override → config is authoritative again.
        cfg = LLMConfig(review_model="openai/gpt-5.1")
        assert llm_config_for("review", cfg).model == "openai/gpt-5.1"

    def test_non_registry_model_accepted(self, in_memory_db: AppDatabase):
        body = ModelsUpdate(
            indexing_model="local/llama-3.3-70b",
            review_model="openai/gpt-5.1-codex-mini",
        )
        assert set_models(body, _admin_req()) == {"ok": True}
        assert in_memory_db.get_setting("review_model") == "openai/gpt-5.1-codex-mini"
        assert llm_config_for("review", LLMConfig()).model == "openai/gpt-5.1-codex-mini"


@pytest.fixture
def no_catalog_fetch(monkeypatch: pytest.MonkeyPatch):
    """Keep get_models off the network and hermetic — default config,
    static registry options only (a developer's env/mira.yaml must not
    leak into assertions)."""
    from mira.config import MiraConfig

    async def _none(config):
        return None

    monkeypatch.setattr("mira.dashboard.model_catalog.fetch_catalog", _none)
    monkeypatch.setattr("mira.config.load_config", lambda *a, **kw: MiraConfig())


class TestGetModelsSource:
    @pytest.mark.asyncio
    async def test_reports_dashboard_source_and_config_target(
        self, in_memory_db: AppDatabase, no_catalog_fetch
    ):
        in_memory_db.set_setting("review_model", "custom/model-x")
        resp = await get_models()
        assert resp.review_source == "dashboard"
        assert resp.review_model == "custom/model-x"
        assert resp.indexing_source == "config"
        # The inherit target ignores the override.
        assert resp.config_review_model != "custom/model-x"

    @pytest.mark.asyncio
    async def test_reports_config_source_without_override(
        self, in_memory_db: AppDatabase, no_catalog_fetch
    ):
        resp = await get_models()
        assert resp.review_source == "config"
        assert resp.indexing_source == "config"
        assert resp.review_model == resp.config_review_model
        assert resp.backend == "openrouter"


class TestFallbackChains:
    """The per-purpose fallback chain: DB → mira.yaml, bound like the primary."""

    def test_no_chain_leaves_the_config_alone(self, in_memory_db: AppDatabase):
        assert llm_config_for("review", LLMConfig()).fallbacks == []

    def test_config_chain_is_bound_in_order(self, in_memory_db: AppDatabase):
        cfg = LLMConfig(
            review_model="openai/gpt-5.1",
            review_fallback_models=["anthropic/claude-sonnet-4-6", "api:z-ai/glm-5.3"],
        )
        resolved = llm_config_for("review", cfg)
        assert [f.model for f in resolved.fallbacks] == [
            "anthropic/claude-sonnet-4-6",
            "z-ai/glm-5.3",
        ]
        # Members carry the review's protocol, and no chain of their own.
        assert all(f.fallbacks == [] for f in resolved.fallbacks)
        assert all(f.api_style == resolved.api_style for f in resolved.fallbacks)

    def test_dashboard_chain_outranks_the_config_one(
        self, in_memory_db: AppDatabase, caplog: pytest.LogCaptureFixture
    ):
        in_memory_db.set_setting("review_fallback_models", '["custom/b", "custom/c"]')
        cfg = LLMConfig(review_fallback_models=["config/only"])
        with caplog.at_level(logging.INFO, logger="mira.dashboard.models_config"):
            resolved = llm_config_for("review", cfg)
        assert [f.model for f in resolved.fallbacks] == ["custom/b", "custom/c"]
        assert "Review fallbacks: custom/b → custom/c (source: dashboard setting)" in caplog.text

    def test_an_explicitly_empty_dashboard_chain_means_none(self, in_memory_db: AppDatabase):
        in_memory_db.set_setting("review_fallback_models", "[]")
        cfg = LLMConfig(review_fallback_models=["config/only"])
        assert llm_config_for("review", cfg).fallbacks == []

    def test_a_cleared_dashboard_chain_inherits_the_config_one(self, in_memory_db: AppDatabase):
        in_memory_db.set_setting("review_fallback_models", "")
        cfg = LLMConfig(review_fallback_models=["config/only"])
        assert [f.model for f in llm_config_for("review", cfg).fallbacks] == ["config/only"]

    def test_entries_repeating_the_primary_or_each_other_are_dropped(
        self, in_memory_db: AppDatabase
    ):
        cfg = LLMConfig(
            review_model="openai/gpt-5.1",
            review_fallback_models=["openai/gpt-5.1", "custom/b", "custom/b", " ", "custom/c"],
        )
        assert [f.model for f in llm_config_for("review", cfg).fallbacks] == [
            "custom/b",
            "custom/c",
        ]

    def test_security_inherits_the_review_chain(self, in_memory_db: AppDatabase):
        in_memory_db.set_setting("review_fallback_models", '["custom/b"]')
        resolved = llm_config_for("security", LLMConfig(security_model="cheap/model"))
        assert [f.model for f in resolved.fallbacks] == ["custom/b"]

    def test_security_chain_of_its_own_wins(self, in_memory_db: AppDatabase):
        in_memory_db.set_setting("review_fallback_models", '["custom/b"]')
        cfg = LLMConfig(security_fallback_models=["secure/x"])
        assert [f.model for f in llm_config_for("security", cfg).fallbacks] == ["secure/x"]

    def test_indexing_never_inherits(self, in_memory_db: AppDatabase):
        in_memory_db.set_setting("review_fallback_models", '["custom/b"]')
        assert llm_config_for("indexing", LLMConfig()).fallbacks == []

    def test_an_unreadable_setting_reads_as_unset(self, in_memory_db: AppDatabase):
        in_memory_db.set_setting("review_fallback_models", "not json")
        cfg = LLMConfig(review_fallback_models=["config/only"])
        assert [f.model for f in llm_config_for("review", cfg).fallbacks] == ["config/only"]

    def test_the_client_built_from_it_is_a_chain(self, in_memory_db: AppDatabase):
        from mira.llm import create_llm
        from mira.llm.chain import FallbackChain

        cfg = LLMConfig(review_fallback_models=["custom/b"])
        llm = create_llm(llm_config_for("review", cfg))
        assert isinstance(llm, FallbackChain)
        assert [p.config.model for p in llm.providers][1] == "custom/b"


class TestSetModelsFallbacks:
    def test_absent_leaves_the_stored_chain_alone(self, in_memory_db: AppDatabase):
        in_memory_db.set_setting("review_fallback_models", '["custom/b"]')
        set_models(ModelsUpdate(indexing_model="", review_model=""), _admin_req())
        assert in_memory_db.get_setting("review_fallback_models") == '["custom/b"]'

    def test_a_list_is_stored_normalized(self, in_memory_db: AppDatabase):
        body = ModelsUpdate(
            indexing_model="",
            review_model="",
            review_fallbacks=[" custom/b ", "", "custom/b", "custom/c"],
        )
        assert set_models(body, _admin_req()) == {"ok": True}
        assert in_memory_db.get_setting("review_fallback_models") == '["custom/b", "custom/c"]'

    def test_an_empty_list_is_stored_as_none(self, in_memory_db: AppDatabase):
        set_models(
            ModelsUpdate(indexing_model="", review_model="", review_fallbacks=[]), _admin_req()
        )
        assert in_memory_db.get_setting("review_fallback_models") == "[]"

    def test_null_clears_the_override(self, in_memory_db: AppDatabase):
        in_memory_db.set_setting("security_fallback_models", '["custom/b"]')
        set_models(
            ModelsUpdate(indexing_model="", review_model="", security_fallbacks=None),
            _admin_req(),
        )
        assert in_memory_db.get_setting("security_fallback_models") == ""

    def test_a_chain_past_the_limit_is_refused(self, in_memory_db: AppDatabase):
        from fastapi import HTTPException

        body = ModelsUpdate(
            indexing_model="", review_model="", review_fallbacks=[f"m/{i}" for i in range(6)]
        )
        with pytest.raises(HTTPException) as info:
            set_models(body, _admin_req())
        assert info.value.status_code == 400
        assert in_memory_db.get_setting("review_fallback_models") is None


class TestGetModelsFallbacks:
    @pytest.mark.asyncio
    async def test_reports_the_chain_its_source_and_the_inherit_target(
        self, in_memory_db: AppDatabase, no_catalog_fetch, monkeypatch: pytest.MonkeyPatch
    ):
        from mira.config import MiraConfig

        cfg = MiraConfig()
        cfg.llm.review_fallback_models = ["config/x"]
        monkeypatch.setattr("mira.config.load_config", lambda *a, **kw: cfg)
        in_memory_db.set_setting("review_fallback_models", '["custom/b"]')

        resp = await get_models()

        assert resp.review_fallbacks == ["custom/b"]
        assert resp.review_fallbacks_source == "dashboard"
        assert resp.config_review_fallbacks == ["config/x"]
        # Security has no chain of its own, so it reports the review one.
        assert resp.security_fallbacks == ["custom/b"]
        assert resp.security_fallbacks_source == "config"
        assert resp.indexing_fallbacks == []
        assert resp.fallback_chain_limit == 5


class TestOutputBudget:
    """`max_tokens` from the dashboard: a count, or 0 for no cap at all."""

    def test_config_value_when_nothing_stored(self, in_memory_db: AppDatabase):
        assert llm_config_for("review", LLMConfig(max_tokens=8192)).max_tokens == 8192

    def test_dashboard_value_outranks_the_config_one(
        self, in_memory_db: AppDatabase, caplog: pytest.LogCaptureFixture
    ):
        in_memory_db.set_setting("llm_max_tokens", "16384")
        with caplog.at_level(logging.INFO, logger="mira.dashboard.models_config"):
            resolved = llm_config_for("review", LLMConfig(max_tokens=4096))
        assert resolved.max_tokens == 16384
        assert "Output budget: 16384 (source: dashboard setting)" in caplog.text

    def test_zero_means_unlimited_and_reaches_the_chain(self, in_memory_db: AppDatabase):
        in_memory_db.set_setting("llm_max_tokens", "0")
        resolved = llm_config_for("review", LLMConfig(review_fallback_models=["custom/b"]))
        assert resolved.max_tokens == 0
        assert resolved.fallbacks[0].max_tokens == 0

    def test_a_cleared_or_broken_value_inherits(self, in_memory_db: AppDatabase):
        in_memory_db.set_setting("llm_max_tokens", "")
        assert llm_config_for("indexing", LLMConfig(max_tokens=2048)).max_tokens == 2048
        in_memory_db.set_setting("llm_max_tokens", "lots")
        assert llm_config_for("indexing", LLMConfig(max_tokens=2048)).max_tokens == 2048
        in_memory_db.set_setting("llm_max_tokens", "-5")
        assert llm_config_for("indexing", LLMConfig(max_tokens=2048)).max_tokens == 2048

    def test_set_models_stores_clears_and_refuses(self, in_memory_db: AppDatabase):
        from fastapi import HTTPException

        set_models(ModelsUpdate(indexing_model="", review_model=""), _admin_req())
        assert in_memory_db.get_setting("llm_max_tokens") is None
        set_models(ModelsUpdate(indexing_model="", review_model="", max_tokens=0), _admin_req())
        assert in_memory_db.get_setting("llm_max_tokens") == "0"
        set_models(ModelsUpdate(indexing_model="", review_model="", max_tokens=12000), _admin_req())
        assert in_memory_db.get_setting("llm_max_tokens") == "12000"
        set_models(ModelsUpdate(indexing_model="", review_model="", max_tokens=None), _admin_req())
        assert in_memory_db.get_setting("llm_max_tokens") == ""
        with pytest.raises(HTTPException):
            set_models(
                ModelsUpdate(indexing_model="", review_model="", max_tokens=-1), _admin_req()
            )

    @pytest.mark.asyncio
    async def test_get_models_reports_it(self, in_memory_db: AppDatabase, no_catalog_fetch):
        resp = await get_models()
        assert resp.max_tokens == resp.config_max_tokens == 4096
        assert resp.max_tokens_source == "config"
        in_memory_db.set_setting("llm_max_tokens", "0")
        resp = await get_models()
        assert resp.max_tokens == 0
        assert resp.max_tokens_source == "dashboard"


class TestProviderReasoningLevels:
    """The thinking picker offers what the review model's provider reports."""

    @pytest.mark.asyncio
    async def test_levels_ride_on_the_options_and_the_saved_review_model(
        self, in_memory_db: AppDatabase, no_catalog_fetch
    ):
        from mira.llm import models_dev

        models_dev.load(
            {
                "openrouter": {
                    "api": "https://openrouter.ai/api/v1",
                    "models": {
                        "anthropic/claude-sonnet-4-6": {
                            "reasoning_options": [
                                {"type": "effort", "values": ["low", "medium", "high"]}
                            ]
                        }
                    },
                }
            }
        )
        try:
            in_memory_db.set_setting("review_model", "anthropic/claude-sonnet-4-6")
            resp = await get_models()
        finally:
            models_dev.reset()

        option = next(o for o in resp.review_options if o.value == "anthropic/claude-sonnet-4-6")
        assert option.reasoning_levels == ["low", "medium", "high"]
        assert resp.review_thinking_levels == ["low", "medium", "high"]
        assert resp.review_thinking_source == "provider"

    @pytest.mark.asyncio
    async def test_builtin_when_the_provider_has_not_said(
        self, in_memory_db: AppDatabase, no_catalog_fetch
    ):
        resp = await get_models()
        assert resp.review_thinking_levels == []
        assert resp.review_thinking_source == "builtin"
        assert [o.value for o in resp.thinking_options][:2] == ["off", "low"]

    def test_a_provider_level_outside_the_builtin_list_is_accepted(self, in_memory_db: AppDatabase):
        from fastapi import HTTPException

        body = ModelsUpdate(indexing_model="", review_model="", review_thinking_mode="xhigh")
        assert set_models(body, _admin_req()) == {"ok": True}
        assert in_memory_db.get_setting("review_thinking_mode") == "xhigh"
        with pytest.raises(HTTPException):
            set_models(
                ModelsUpdate(indexing_model="", review_model="", review_thinking_mode="not ok!"),
                _admin_req(),
            )


class TestModelsReviewFindings:
    def test_a_refused_request_writes_nothing(self, in_memory_db: AppDatabase):
        from fastapi import HTTPException

        in_memory_db.set_setting("review_model", "kept/model")
        body = ModelsUpdate(
            indexing_model="",
            review_model="new/model",
            review_fallbacks=[f"m/{i}" for i in range(6)],
        )
        with pytest.raises(HTTPException):
            set_models(body, _admin_req())
        assert in_memory_db.get_setting("review_model") == "kept/model"

        with pytest.raises(HTTPException):
            set_models(
                ModelsUpdate(indexing_model="", review_model="new/model", max_tokens=-1),
                _admin_req(),
            )
        assert in_memory_db.get_setting("review_model") == "kept/model"

    @pytest.mark.asyncio
    async def test_an_override_equal_to_the_file_is_still_an_override(
        self, in_memory_db: AppDatabase, no_catalog_fetch
    ):
        in_memory_db.set_setting("llm_max_tokens", "4096")
        resp = await get_models()
        assert resp.max_tokens == resp.config_max_tokens == 4096
        assert resp.max_tokens_source == "dashboard"
