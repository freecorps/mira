"""OpenCode Go, and the generic pieces it stands on.

Covers:
- The provider profile: resolved by URL, labelled for the page, and the
  ``llm.provider: opencode-go`` shortcut that fills in endpoint and key.
- The catalog: the Go entries offered on Go and nowhere else, merged with
  the endpoint's live list, and the backend named by its profile.
- The client: the session header the endpoint requires, one per instance.
- Usage: the Go document read into three windows, the snapshot round trip,
  the cache and the failure memory, and the dashboard routes and CLI on top.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest
from click.testing import CliRunner

from mira.config import LLMConfig
from mira.dashboard.db import AppDatabase
from mira.dashboard.model_catalog import active_backend, build_options
from mira.dashboard.models_config import describe_call
from mira.llm import create_llm, key_providers
from mira.llm import provider_profiles as profiles
from mira.oauth.usage import UsageSnapshot
from tests.test_oauth import _admin, _user

GO_URL = "https://opencode.ai/zen/go/v1"

GO_USAGE = {
    "usage": {
        "rolling": {"status": "ok", "percent": 12.5, "resetsAt": "2026-09-20T09:00:00.000Z"},
        "weekly": {"status": "ok", "percent": 40, "resetsAt": "2026-09-21T00:00:00.000Z"},
        "monthly": {"status": "rate-limited", "percent": 99.7, "resetsAt": "2026-10-01T00:00:00Z"},
    }
}


@pytest.fixture
def db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AppDatabase:
    monkeypatch.setenv("MIRA_INDEX_DIR", str(tmp_path))
    database = AppDatabase(url="", admin_password="admin")
    monkeypatch.setattr("mira.dashboard.api._app_db", database)
    return database


@pytest.fixture(autouse=True)
def _fresh_usage_state():
    key_providers._failed_at.clear()
    key_providers._locks.clear()
    yield
    key_providers._failed_at.clear()
    key_providers._locks.clear()


@pytest.fixture
def go_key(monkeypatch: pytest.MonkeyPatch) -> str:
    monkeypatch.setenv("OPENCODE_API_KEY", "oc_sk_test_123")
    return "oc_sk_test_123"


def _async(value):
    async def _coro(*_args, **_kwargs):
        return value

    return _coro


# ── Profile and config ───────────────────────────────────────────────


class TestProfile:
    def test_resolved_by_its_url(self):
        p = profiles.resolve(GO_URL)
        assert p["name"] == "opencode-go"
        assert p["label"] == "OpenCode Go"
        assert p["api_key_env"] == "OPENCODE_API_KEY"
        assert p["model_prefix"] == "strip"
        assert p["session_header"] == "x-opencode-session"
        assert p["usage_url"] == f"{GO_URL}/usage"
        assert p["usage_format"] in key_providers.PARSERS

    def test_get_fills_every_field(self):
        p = profiles.get("opencode-go")
        assert p is not None
        assert set(profiles.DEFAULT_PROFILE) <= set(p)

    def test_labelled_lists_the_presentable_ones(self, tmp_path, monkeypatch):
        custom = tmp_path / "providers.json"
        custom.write_text(json.dumps({"quiet": {"base_url": "https://quiet.test/v1"}}))
        monkeypatch.setenv("MIRA_PROVIDERS_JSON_PATH", str(custom))
        profiles._load.cache_clear()
        try:
            shown = profiles.labelled()
            assert {"openrouter", "opencode-go"} <= set(shown)
            assert "quiet" not in shown
            assert shown["opencode-go"]["docs_url"].startswith("https://opencode.ai/")
        finally:
            profiles._load.cache_clear()

    def test_openrouter_is_untouched(self):
        p = profiles.resolve("https://openrouter.ai/api/v1")
        assert p["label"] == "OpenRouter"
        assert p["session_header"] == ""
        assert p["usage_url"] == ""


class TestProviderShortcut:
    def test_names_the_endpoint_and_the_key(self):
        cfg = LLMConfig(provider="opencode-go")
        assert cfg.base_url == GO_URL
        assert cfg.api_key_env == "OPENCODE_API_KEY"
        # Downstream sees the OpenAI-compatible backend it is.
        assert cfg.provider == "openai"

    def test_is_case_insensitive(self):
        assert LLMConfig(provider="OpenCode-Go").base_url == GO_URL

    def test_an_explicit_endpoint_or_variable_wins(self):
        cfg = LLMConfig(provider="opencode-go", base_url="https://proxy.test/v1", api_key_env="K")
        assert cfg.base_url == "https://proxy.test/v1"
        assert cfg.api_key_env == "K"

    def test_the_built_in_backends_are_left_alone(self):
        assert LLMConfig(provider="bedrock").provider == "bedrock"
        assert LLMConfig(provider="openai").base_url == "https://openrouter.ai/api/v1"
        assert LLMConfig().base_url == "https://openrouter.ai/api/v1"

    def test_an_unknown_name_is_rejected_at_load(self):
        with pytest.raises(ValueError, match="not 'openai', 'bedrock' or a provider profile"):
            LLMConfig(provider="opencode-gone")

    def test_openrouter_is_a_name_too(self):
        cfg = LLMConfig(provider="openrouter")
        assert cfg.base_url == "https://openrouter.ai/api/v1"
        assert cfg.api_key_env == "OPENROUTER_API_KEY"


# ── Catalog ──────────────────────────────────────────────────────────


class TestCatalog:
    def test_backend_is_named_by_its_profile(self):
        assert active_backend(LLMConfig(provider="opencode-go")) == "opencode-go"
        assert active_backend(LLMConfig()) == "openrouter"
        assert active_backend(LLMConfig(base_url="http://localhost:11434/v1")) == (
            "openai-compatible"
        )

    def test_go_entries_are_offered_on_go_with_the_live_list_behind(self):
        dynamic = [
            {"value": "kimi-k2.7-code", "label": "kimi-k2.7-code"},
            {"value": "omen-alpha", "label": "omen-alpha"},
        ]
        options = build_options("opencode-go", dynamic, "review")
        values = [o["value"] for o in options]
        assert options[0]["value"] == "kimi-k2.7-code"
        assert options[0]["recommended"] is True
        assert options[0]["label"] == "Kimi K2.7 Code"  # the registry's label, not the id
        assert "omen-alpha" in values  # the live list's extras still appear
        assert values.count("kimi-k2.7-code") == 1
        assert not any(v.startswith("anthropic/") for v in values)
        assert not any(v.startswith("us.anthropic") for v in values)

    def test_indexing_has_its_own_recommendation(self):
        options = build_options("opencode-go", None, "indexing")
        assert options[0]["value"] == "glm-5.3-flash"
        assert "kimi-k3" not in {o["value"] for o in options}

    def test_go_entries_never_leak_onto_other_backends(self):
        for backend in ("openrouter", "bedrock"):
            values = {o["value"] for o in build_options(backend, None, "review")}
            assert "kimi-k2.7-code" not in values
        # A generic endpoint with no list of its own still gets the
        # OpenRouter-style registry, as before — and not Go's bare ids.
        values = {o["value"] for o in build_options("openai-compatible", None, "review")}
        assert "anthropic/claude-sonnet-4-6" in values
        assert "kimi-k2.7-code" not in values

    def test_a_generic_endpoint_with_a_live_list_shows_only_that(self):
        dynamic = [{"value": "llama-3.3-70b", "label": "llama-3.3-70b"}]
        values = [o["value"] for o in build_options("openai-compatible", dynamic, "review")]
        assert values == ["llama-3.3-70b"]

    def test_the_models_page_names_the_backend(self):
        described = describe_call(LLMConfig(provider="opencode-go", model="kimi-k2.7-code"))
        assert described["backend"] == "api"
        assert described["provider"] == "opencode-go"
        assert described["provider_label"] == "OpenCode Go"
        assert described["endpoint"] == GO_URL
        assert describe_call(LLMConfig())["provider_label"] == "OpenRouter"
        plain = describe_call(LLMConfig(base_url="http://localhost:11434/v1"))
        assert plain["provider_label"] == "API-key endpoint"

    @pytest.mark.asyncio
    async def test_the_live_list_is_asked_with_the_profiles_key(self, go_key, monkeypatch):
        from mira.dashboard import model_catalog

        seen: dict = {}

        class _Resp:
            def raise_for_status(self):
                pass

            def json(self):
                return {"data": [{"id": "kimi-k2.7-code"}]}

        class _Client:
            def __init__(self, **_kw):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            async def get(self, url, headers=None):
                seen["url"] = url
                seen["headers"] = headers
                return _Resp()

        model_catalog._cache.clear()
        monkeypatch.setattr(model_catalog.httpx, "AsyncClient", _Client)
        try:
            found = await model_catalog.fetch_catalog(LLMConfig(provider="opencode-go"))
        finally:
            model_catalog._cache.clear()
        assert found == [{"value": "kimi-k2.7-code", "label": "kimi-k2.7-code"}]
        assert seen["url"] == f"{GO_URL}/models"
        assert seen["headers"]["Authorization"] == f"Bearer {go_key}"


# ── Client headers ───────────────────────────────────────────────────


class TestSessionHeader:
    def test_go_calls_carry_a_session_id_and_a_user_agent(self, go_key):
        llm = create_llm(LLMConfig(provider="opencode-go", model="kimi-k2.7-code"))
        headers = llm._build_headers()
        assert headers["Authorization"] == f"Bearer {go_key}"
        assert headers["User-Agent"].startswith("mira/")
        session = headers["x-opencode-session"]
        assert len(session) == 30 and all(c in "0123456789abcdef" for c in session)
        # Stable for the instance (one review pass)…
        assert llm._build_headers()["x-opencode-session"] == session
        # …and different for the next one.
        other = create_llm(LLMConfig(provider="opencode-go", model="kimi-k2.7-code"))
        assert other._build_headers()["x-opencode-session"] != session

    def test_the_prefixed_id_is_sent_bare(self):
        from mira.llm.base import _strip_model_prefix

        assert _strip_model_prefix("opencode-go/kimi-k3", GO_URL) == "kimi-k3"
        assert _strip_model_prefix("kimi-k3", GO_URL) == "kimi-k3"

    def test_other_endpoints_send_no_session_header(self, monkeypatch):
        monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-1")
        headers = create_llm(LLMConfig())._build_headers()
        assert "x-opencode-session" not in headers
        assert headers["User-Agent"].startswith("mira/")


# ── Usage ────────────────────────────────────────────────────────────


class TestGoUsageDocument:
    def test_three_windows_with_their_names_and_resets(self):
        snapshot = key_providers.parse_opencode_go(GO_USAGE)
        assert snapshot is not None
        names = [w.name for w in snapshot.windows()]
        assert names == ["5-hour", "weekly", "monthly"]
        assert snapshot.primary.used_percent == 12.5
        assert snapshot.secondary.used_percent == 40
        assert snapshot.tertiary.used_percent == 99.7
        # ISO reset times become epoch seconds.
        assert snapshot.tertiary.resets_at == 1790812800.0
        assert snapshot.source == "endpoint"
        # "rate-limited" on any window is believed even at 99.7% — until the
        # next window reset, which is the 5-hour one (09:00Z).
        assert snapshot.limit_reached is True
        assert snapshot.available(now=1789894000.0) is False
        assert snapshot.available(now=1789894801.0) is True

    def test_a_missing_window_is_left_out(self):
        doc = {"usage": {"rolling": {"status": "ok", "percent": 5, "resetsAt": "x"}}}
        snapshot = key_providers.parse_opencode_go(doc)
        assert snapshot is not None
        assert snapshot.primary is not None and snapshot.primary.resets_at is None
        assert snapshot.secondary is None and snapshot.tertiary is None
        assert snapshot.limit_reached is False
        assert snapshot.headroom() == 95.0

    @pytest.mark.parametrize("doc", [None, "nope", {}, {"usage": {}}, {"usage": {"rolling": {}}}])
    def test_a_document_with_no_window_is_not_a_snapshot(self, doc):
        assert key_providers.parse_opencode_go(doc) is None

    def test_the_third_window_survives_the_round_trip(self):
        snapshot = key_providers.parse_opencode_go(GO_USAGE)
        again = UsageSnapshot.from_dict(json.loads(json.dumps(snapshot.to_dict())))
        assert again is not None
        assert again.tertiary is not None
        assert again.tertiary.to_dict() == snapshot.tertiary.to_dict()
        assert again.headroom() == pytest.approx(0.3)
        # A snapshot written before there was a third window still reads.
        old = UsageSnapshot.from_dict({"primary": {"used_percent": 1}})
        assert old is not None and old.tertiary is None and old.has_data()


class TestFetch:
    @pytest.mark.asyncio
    async def test_the_providers_refusal_is_quoted(self, monkeypatch):
        import httpx

        def _handler(request: httpx.Request) -> httpx.Response:
            assert request.headers["Authorization"] == "Bearer bad"
            assert request.headers["x-opencode-client"] == "mira"
            return httpx.Response(
                401,
                json={
                    "type": "error",
                    "error": {"type": "AuthError", "message": "Invalid API key."},
                },
            )

        transport = httpx.MockTransport(_handler)
        real = httpx.AsyncClient
        monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: real(transport=transport, **kw))
        with pytest.raises(key_providers.UsageError, match="HTTP 401: Invalid API key."):
            await key_providers.fetch_usage(profiles.get("opencode-go"), "bad")

    @pytest.mark.asyncio
    async def test_a_good_answer_is_a_snapshot(self, monkeypatch):
        import httpx

        transport = httpx.MockTransport(lambda request: httpx.Response(200, json=GO_USAGE))
        real = httpx.AsyncClient
        monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: real(transport=transport, **kw))
        snapshot = await key_providers.fetch_usage(profiles.get("opencode-go"), "oc_sk_1")
        assert snapshot.tertiary is not None

    @pytest.mark.asyncio
    async def test_no_key_and_no_endpoint_are_named(self):
        with pytest.raises(key_providers.UsageError, match="OPENCODE_API_KEY"):
            await key_providers.fetch_usage(profiles.get("opencode-go"), "")
        with pytest.raises(key_providers.UsageError, match="does not report usage"):
            await key_providers.fetch_usage(profiles.get("openrouter"), "sk-or-1")


class TestUsageCache:
    @pytest.mark.asyncio
    async def test_fresh_snapshot_is_reused_and_persisted(self, db, monkeypatch):
        calls = 0

        async def fetch(profile, api_key):
            nonlocal calls
            calls += 1
            return key_providers.parse_opencode_go(GO_USAGE)

        monkeypatch.setattr(key_providers, "fetch_usage", fetch)
        profile = profiles.get("opencode-go")
        first = await key_providers.usage_for(profile, "oc_sk_1", db)
        second = await key_providers.usage_for(profile, "oc_sk_1", db)
        assert calls == 1
        assert first is not None and second is not None
        assert second.fetched_at == first.fetched_at
        # Stored under a digest of the key, never the key.
        rows = db.list_settings("provider_usage:")
        assert len(rows) == 1
        assert "oc_sk_1" not in json.dumps(rows)
        # Another key starts from nothing.
        assert key_providers.load_usage("opencode-go", "oc_sk_2", db) is None
        # Force asks again regardless.
        await key_providers.usage_for(profile, "oc_sk_1", db, force=True)
        assert calls == 2

    @pytest.mark.asyncio
    async def test_a_stale_snapshot_is_asked_for_again(self, db, monkeypatch):
        profile = profiles.get("opencode-go")
        stale = key_providers.parse_opencode_go(GO_USAGE)
        stale.fetched_at = time.time() - key_providers.FRESH_SECONDS - 1
        key_providers.save_usage("opencode-go", "oc_sk_1", stale, db)
        fresh = key_providers.parse_opencode_go(GO_USAGE)
        monkeypatch.setattr(key_providers, "fetch_usage", _async(fresh))
        found = await key_providers.usage_for(profile, "oc_sk_1", db)
        assert found is not None and found.fetched_at == fresh.fetched_at

    @pytest.mark.asyncio
    async def test_a_failure_keeps_the_stored_snapshot_and_is_remembered(self, db, monkeypatch):
        calls = 0

        async def fetch(profile, api_key):
            nonlocal calls
            calls += 1
            raise key_providers.UsageError("Usage lookup failed: boom")

        profile = profiles.get("opencode-go")
        stale = key_providers.parse_opencode_go(GO_USAGE)
        stale.fetched_at = time.time() - key_providers.FRESH_SECONDS - 1
        key_providers.save_usage("opencode-go", "oc_sk_1", stale, db)
        monkeypatch.setattr(key_providers, "fetch_usage", fetch)
        found = await key_providers.usage_for(profile, "oc_sk_1", db)
        assert found is not None and found.fetched_at == stale.fetched_at
        await key_providers.usage_for(profile, "oc_sk_1", db)
        assert calls == 1  # not retried within the failure window
        with pytest.raises(key_providers.UsageError):
            await key_providers.usage_for(profile, "oc_sk_1", db, force=True)
        assert calls == 2

    @pytest.mark.asyncio
    async def test_no_key_means_no_lookup(self, db, monkeypatch):
        monkeypatch.setattr(
            key_providers, "fetch_usage", _async(key_providers.parse_opencode_go(GO_USAGE))
        )
        assert await key_providers.usage_for(profiles.get("opencode-go"), "", db) is None


class TestStatus:
    @pytest.mark.asyncio
    async def test_cards_say_which_endpoint_is_configured_and_never_the_key(
        self, db, go_key, monkeypatch
    ):
        monkeypatch.setattr(
            key_providers, "fetch_usage", _async(key_providers.parse_opencode_go(GO_USAGE))
        )
        cards = await key_providers.list_status(LLMConfig(provider="opencode-go"), db)
        by_id = {c["id"]: c for c in cards}
        assert cards[0]["id"] == "opencode-go"  # the configured endpoint first
        go = by_id["opencode-go"]
        assert go["is_endpoint"] is True
        assert go["key_configured"] is True
        assert go["api_key_env"] == "OPENCODE_API_KEY"
        assert go["reports_usage"] is True
        assert go["protocol"]["protocol"] == "Chat Completions"
        assert go["usage"]["tertiary"]["name"] == "monthly"
        assert go["available"] is False  # the monthly window is rate-limited
        router = by_id["openrouter"]
        assert router["is_endpoint"] is False
        assert router["reports_usage"] is False
        assert router["usage"] is None
        assert go_key not in json.dumps(cards)

    @pytest.mark.asyncio
    async def test_a_profile_that_is_not_the_endpoint_reads_its_own_variable(self, db, go_key):
        cards = await key_providers.list_status(LLMConfig(), db)
        go = next(c for c in cards if c["id"] == "opencode-go")
        assert go["is_endpoint"] is False
        assert go["key_configured"] is True
        assert cards[0]["id"] == "openrouter"

    @pytest.mark.asyncio
    async def test_bedrock_configures_no_key_endpoint(self, db):
        cards = await key_providers.list_status(LLMConfig(provider="bedrock"), db)
        assert not any(c["is_endpoint"] for c in cards)

    @pytest.mark.asyncio
    async def test_refresh_reports_the_refusal(self, db, go_key, monkeypatch):
        async def fetch(profile, api_key):
            raise key_providers.UsageError("Usage lookup answered HTTP 403: subscription required")

        monkeypatch.setattr(key_providers, "fetch_usage", fetch)
        with pytest.raises(key_providers.UsageError, match="subscription required"):
            await key_providers.refresh("opencode-go", LLMConfig(provider="opencode-go"), db)
        with pytest.raises(KeyError):
            await key_providers.refresh("nope", LLMConfig(), db)


class TestRoutes:
    @pytest.mark.asyncio
    async def test_list_and_refresh(self, db, go_key, monkeypatch):
        from mira.dashboard.routers import providers as routes

        monkeypatch.setattr(routes, "_llm_config", lambda: LLMConfig(provider="opencode-go"))
        monkeypatch.setattr(
            key_providers, "fetch_usage", _async(key_providers.parse_opencode_go(GO_USAGE))
        )
        listed = await routes.list_key_providers(_admin())
        assert listed["providers"][0]["id"] == "opencode-go"
        assert go_key not in json.dumps(listed)
        refreshed = await routes.refresh_key_provider_usage("opencode-go", _admin())
        assert refreshed["usage"]["primary"]["used_percent"] == 12.5

    @pytest.mark.asyncio
    async def test_errors_are_http_errors(self, db, go_key, monkeypatch):
        from fastapi import HTTPException

        from mira.dashboard.routers import providers as routes

        async def fetch(profile, api_key):
            raise key_providers.UsageError("Usage lookup answered HTTP 401: Invalid API key.")

        monkeypatch.setattr(key_providers, "fetch_usage", fetch)
        with pytest.raises(HTTPException) as refused:
            await routes.refresh_key_provider_usage("opencode-go", _admin())
        assert refused.value.status_code == 400
        assert "Invalid API key." in refused.value.detail
        with pytest.raises(HTTPException) as unknown:
            await routes.refresh_key_provider_usage("nope", _admin())
        assert unknown.value.status_code == 404
        with pytest.raises(HTTPException) as forbidden:
            await routes.list_key_providers(_user())
        assert forbidden.value.status_code == 403


class TestCLI:
    def test_auth_status_prints_the_key_endpoints(self, db, go_key, monkeypatch):
        from mira.cli import main

        monkeypatch.setattr(
            key_providers, "fetch_usage", _async(key_providers.parse_opencode_go(GO_USAGE))
        )
        monkeypatch.setattr(
            "mira.cli.load_config",
            lambda: type("C", (), {"llm": LLMConfig(provider="opencode-go")})(),
        )
        result = CliRunner().invoke(main, ["auth", "status"])
        assert result.exit_code == 0, result.output
        assert "API-key endpoints:" in result.output
        assert "OpenCode Go" in result.output
        assert "key in OPENCODE_API_KEY" in result.output
        assert "the configured API-key endpoint" in result.output
        assert "monthly 100% used" in result.output
        assert go_key not in result.output
