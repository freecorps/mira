"""Endpoints configured from the dashboard, and OpenCode Go on top of them.

Covers:
- The store: adding, editing and removing an endpoint, the key beside it
  (never inside it), which endpoint bare model ids go to, and what happens
  to a pointer whose endpoint is gone.
- Presets: the quirks a stored endpoint inherits, a URL a preset claims,
  and the ``llm.provider: <preset>`` shortcut in the config file.
- Binding: the URL, protocol and key a call is made with, the routes that
  name one endpoint, and a route to an endpoint that is not configured.
- Usage: the OpenCode Go document read into three windows, the snapshot
  cache, and the failure memory scoped to one endpoint and one key.
- The dashboard routes and ``mira auth status`` on top of all of it.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest
from click.testing import CliRunner

from mira.config import LLMConfig
from mira.dashboard.db import AppDatabase
from mira.dashboard.model_catalog import active_backend, build_options, endpoint_options
from mira.dashboard.models_config import (
    apply_endpoint_binding,
    bind_model,
    describe_call,
    llm_config_for,
    resolve_endpoint_default,
)
from mira.exceptions import LLMError
from mira.llm import create_llm, endpoints, key_providers
from mira.llm import provider_profiles as profiles
from mira.llm.endpoints import Endpoint, EndpointError
from mira.oauth.routes import endpoint_route, parse_route
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
    """Fresh per-test SQLite DB, swapped in for the module-level ``_app_db``."""
    monkeypatch.setenv("MIRA_INDEX_DIR", str(tmp_path))
    database = AppDatabase(url="", admin_password="admin")
    monkeypatch.setattr("mira.dashboard.api._app_db", database)
    return database


@pytest.fixture(autouse=True)
def _fresh_state(monkeypatch: pytest.MonkeyPatch):
    """No usage memory and no stray keys leaking between tests."""
    from mira.dashboard import model_catalog

    key_providers._failed_at.clear()
    key_providers._locks.clear()
    model_catalog._cache.clear()
    for var in ("OPENCODE_API_KEY", "OPENROUTER_API_KEY", "OPENAI_API_KEY"):
        monkeypatch.delenv(var, raising=False)
    yield
    key_providers._failed_at.clear()
    key_providers._locks.clear()
    model_catalog._cache.clear()


def _async(value):
    async def _coro(*_args, **_kwargs):
        return value

    return _coro


def _go(db: AppDatabase, *, key: str = "oc_sk_test_1234", default: bool = True) -> Endpoint:
    """A stored OpenCode Go endpoint, with a key, usually the default."""
    endpoint = endpoints.create(label="OpenCode Go", base_url="", preset="opencode-go", db=db)
    if key:
        endpoints.set_secret(endpoint.id, key, db)
    if default:
        endpoints.set_active(endpoint.id, db)
    return endpoint


# ── Presets ──────────────────────────────────────────────────────────


class TestPresets:
    def test_go_carries_what_the_endpoint_needs(self):
        p = profiles.resolve(GO_URL)
        assert p["name"] == "opencode-go"
        assert p["label"] == "OpenCode Go"
        assert p["api_key_env"] == "OPENCODE_API_KEY"
        assert p["model_prefix"] == "strip"
        assert p["session_header"] == "x-opencode-session"
        assert p["usage_url"] == f"{GO_URL}/usage"
        assert p["usage_format"] in key_providers.PARSERS

    def test_the_form_offers_custom_first_then_the_presets(self):
        options = endpoints.preset_options()
        assert options[0]["id"] == ""
        names = [o["id"] for o in options]
        assert {"openrouter", "opencode-go", "ollama"} <= set(names)
        go = next(o for o in options if o["id"] == "opencode-go")
        assert go["base_url"] == GO_URL
        assert go["reports_usage"] is True
        assert next(o for o in options if o["id"] == "openrouter")["reports_usage"] is False

    def test_an_unlabelled_override_is_not_a_preset(self, tmp_path, monkeypatch):
        custom = tmp_path / "providers.json"
        custom.write_text(json.dumps({"quiet": {"base_url": "https://quiet.test/v1"}}))
        monkeypatch.setenv("MIRA_PROVIDERS_JSON_PATH", str(custom))
        profiles._load.cache_clear()
        try:
            assert "quiet" not in endpoints.presets()
            assert profiles.resolve("https://quiet.test/v1")["name"] == "quiet"
        finally:
            profiles._load.cache_clear()


class TestConfigFileShortcut:
    def test_a_preset_name_fills_in_the_endpoint_and_the_key(self):
        cfg = LLMConfig(provider="opencode-go")
        assert cfg.base_url == GO_URL
        assert cfg.api_key_env == "OPENCODE_API_KEY"
        assert cfg.provider == "openai"

    def test_an_explicit_endpoint_or_variable_wins(self):
        cfg = LLMConfig(provider="opencode-go", base_url="https://proxy.test/v1", api_key_env="K")
        assert cfg.base_url == "https://proxy.test/v1"
        assert cfg.api_key_env == "K"

    def test_an_unknown_name_is_rejected_at_load(self):
        with pytest.raises(ValueError, match="not 'openai', 'bedrock' or a provider profile"):
            LLMConfig(provider="opencode-gone")

    def test_a_presets_url_is_checked_like_a_written_one(self, tmp_path, monkeypatch):
        custom = tmp_path / "providers.json"
        custom.write_text(
            json.dumps(
                {
                    "insecure": {"base_url": "http://public.example/v1", "api_key_env": "K"},
                    "broken": {"base_url": "not a url"},
                    "local": {"base_url": "http://localhost:11434/v1", "api_key_env": ""},
                }
            )
        )
        monkeypatch.setenv("MIRA_PROVIDERS_JSON_PATH", str(custom))
        profiles._load.cache_clear()
        try:
            with pytest.raises(ValueError, match="plain http to a public host"):
                LLMConfig(provider="insecure")
            with pytest.raises(ValueError, match="must be an http\\(s\\) URL"):
                LLMConfig(provider="broken")
            local = LLMConfig(provider="local")
            assert local.base_url == "http://localhost:11434/v1"
            assert local.api_key_env == ""
        finally:
            profiles._load.cache_clear()


# ── The store ────────────────────────────────────────────────────────


class TestStore:
    def test_a_preset_fills_in_what_was_not_typed(self, db: AppDatabase):
        endpoint = endpoints.create(label="OpenCode Go", base_url="", preset="opencode-go", db=db)
        assert endpoint.id == "opencode-go"
        assert endpoint.base_url == GO_URL
        assert endpoint.api_style == "chat"
        # The registry knows models for this preset, so the endpoint has one
        # to fall back to when the model in force belongs elsewhere.
        assert endpoint.default_model == "kimi-k2.7-code"
        assert endpoints.get("opencode-go", db) == endpoint

    def test_a_custom_endpoint_keeps_its_own_url_and_guesses_nothing(self, db: AppDatabase):
        endpoint = endpoints.create(label="My proxy", base_url="https://llm.acme.test/v1", db=db)
        assert endpoint.id == "my-proxy"
        assert endpoint.preset == ""
        assert endpoint.default_model == ""
        assert endpoints.profile_for(endpoint)["model_prefix"] == "strip"

    def test_ids_are_unique_and_never_shadow_a_route(self, db: AppDatabase):
        first = endpoints.create(label="Proxy", base_url="https://a.test/v1", db=db)
        second = endpoints.create(label="Proxy", base_url="https://b.test/v1", db=db)
        assert (first.id, second.id) == ("proxy", "proxy-2")
        for reserved in ("config", "active", "test"):
            taken = endpoints.create(label=reserved, base_url="https://c.test/v1", db=db)
            assert taken.id != reserved

    def test_a_url_from_the_form_is_checked_like_one_from_the_file(self, db: AppDatabase):
        with pytest.raises(ValueError, match="plain http to a public host"):
            endpoints.create(label="Bad", base_url="http://public.example/v1", db=db)
        with pytest.raises(ValueError, match="must be an http\\(s\\) URL"):
            endpoints.create(label="Bad", base_url="ftp://x/v1", db=db)
        # …and a private one is allowed, which is the point of the exception.
        assert endpoints.create(label="Local", base_url="http://10.0.0.5:8000/v1", db=db)

    def test_a_bad_protocol_or_preset_is_refused(self, db: AppDatabase):
        with pytest.raises(EndpointError, match="Protocol must be"):
            endpoints.create(label="X", base_url="https://x.test/v1", api_style="grpc", db=db)
        with pytest.raises(EndpointError, match="Unknown preset"):
            endpoints.create(label="X", base_url="https://x.test/v1", preset="nope", db=db)

    def test_the_key_lives_beside_the_endpoint_not_inside_it(self, db: AppDatabase):
        endpoint = _go(db, key="oc_sk_secret_value")
        assert "api_key" not in endpoint.to_dict()
        assert "oc_sk_secret_value" not in json.dumps(endpoint.to_dict())
        assert endpoints.key_for(endpoint, db) == "oc_sk_secret_value"
        assert endpoints.key_source(endpoint, db) == "stored"
        assert endpoints.key_hint(endpoint, db) == "…alue"[-5:]
        endpoints.set_secret(endpoint.id, "", db)
        assert endpoints.key_for(endpoint, db) == ""
        assert endpoints.key_source(endpoint, db) == ""

    def test_a_key_may_stay_in_the_environment(self, db: AppDatabase, monkeypatch):
        monkeypatch.setenv("MY_KEY", "env-key-value")
        endpoint = endpoints.create(
            label="Proxy", base_url="https://llm.acme.test/v1", api_key_env="MY_KEY", db=db
        )
        assert endpoints.key_for(endpoint, db) == "env-key-value"
        assert endpoints.key_source(endpoint, db) == "env:MY_KEY"
        # A stored key wins over the variable: it is the more specific answer.
        endpoints.set_secret(endpoint.id, "stored-key", db)
        assert endpoints.key_for(endpoint, db) == "stored-key"

    def test_a_preset_variable_is_read_when_nothing_else_is_set(self, db, monkeypatch):
        monkeypatch.setenv("OPENCODE_API_KEY", "from-the-environment")
        endpoint = _go(db, key="")
        assert endpoints.key_for(endpoint, db) == "from-the-environment"
        assert endpoints.key_source(endpoint, db) == "env:OPENCODE_API_KEY"

    def test_an_endpoint_with_no_key_anywhere_has_none(self, db: AppDatabase):
        endpoint = endpoints.create(label="Ollama", base_url="", preset="ollama", db=db)
        assert endpoints.key_for(endpoint, db) == ""
        assert endpoints.key_source(endpoint, db) == ""

    def test_the_default_survives_everything_but_deletion(self, db: AppDatabase):
        endpoint = _go(db)
        assert endpoints.active(db) == endpoint.id
        endpoints.delete(endpoint.id, db)
        assert endpoints.get(endpoint.id, db) is None
        assert endpoints.secret(endpoint.id, db) == ""
        assert endpoints.active(db) == ""

    def test_a_pointer_to_a_gone_endpoint_reads_as_none(self, db: AppDatabase):
        db.set_setting(endpoints.ACTIVE_KEY, "ghost")
        assert endpoints.active(db) == ""
        with pytest.raises(EndpointError, match="No endpoint 'ghost'"):
            endpoints.set_active("ghost", db)

    def test_an_unreadable_row_is_skipped_not_fatal(self, db: AppDatabase):
        _go(db)
        db.set_setting("llm_endpoint:broken", "{not json")
        assert list(endpoints.all_endpoints(db)) == ["opencode-go"]


class TestStoredProfile:
    def test_the_preset_supplies_the_quirks_and_the_row_the_url(self, db: AppDatabase):
        endpoint = _go(db)
        endpoint.base_url = "https://proxy.internal.test/go/v1"
        endpoints.save(endpoint, db)
        profile = endpoints.profile_for(endpoint)
        assert profile["base_url"] == "https://proxy.internal.test/go/v1"
        # Behind a proxy it is still OpenCode Go: same headers, same session
        # header, same place it reports usage.
        assert profile["session_header"] == "x-opencode-session"
        assert profile["extra_headers"]["x-opencode-client"] == "mira"
        assert profile["usage_url"] == f"{GO_URL}/usage"

    def test_a_preset_this_build_lost_degrades_to_portable(self, db: AppDatabase):
        endpoint = Endpoint(
            id="x", label="X", base_url="https://x.test/v1", preset="from-the-future"
        )
        profile = endpoints.profile_for(endpoint)
        assert profile["model_prefix"] == "strip"
        assert profile["extra_headers"] == {}

    def test_the_model_prefix_can_be_overridden(self, db: AppDatabase):
        endpoint = endpoints.create(
            label="Router", base_url="https://r.test/v1", model_prefix="keep", db=db
        )
        assert endpoints.profile_for(endpoint)["model_prefix"] == "keep"


# ── Binding ──────────────────────────────────────────────────────────


class TestBinding:
    def test_the_url_protocol_and_key_all_come_from_the_endpoint(self, db: AppDatabase):
        endpoint = _go(db)
        bound = apply_endpoint_binding(LLMConfig(), endpoint.id)
        assert bound.endpoint == endpoint.id
        assert bound.base_url == GO_URL
        assert bound.api_style == "chat"
        from mira.llm.base import _get_api_key

        assert _get_api_key(bound) == "oc_sk_test_1234"

    def test_binding_clears_a_signed_in_account(self, db: AppDatabase):
        endpoint = _go(db)
        config = LLMConfig(oauth_provider="chatgpt", oauth_account="acct_1")
        bound = apply_endpoint_binding(config, endpoint.id)
        assert bound.oauth_provider is None and bound.oauth_account is None

    def test_a_model_from_another_backend_is_replaced_when_one_is_known(self, db):
        endpoint = _go(db)
        # The built-in default is an OpenRouter-style Claude id this endpoint
        # has never served.
        bound = apply_endpoint_binding(LLMConfig(), endpoint.id, model_is_explicit=False)
        assert bound.model == "kimi-k2.7-code"
        # A vendor-prefixed id somebody did choose is replaced too…
        chosen = apply_endpoint_binding(
            LLMConfig(model="anthropic/claude-sonnet-4-6"), endpoint.id, model_is_explicit=True
        )
        assert chosen.model == "kimi-k2.7-code"
        # …but an unfamiliar bare id is a deliberate choice and is sent as-is.
        bare = apply_endpoint_binding(
            LLMConfig(model="glm-6-preview"), endpoint.id, model_is_explicit=True
        )
        assert bare.model == "glm-6-preview"

    def test_an_endpoint_with_nothing_to_offer_leaves_the_model_alone(self, db):
        endpoint = endpoints.create(label="Proxy", base_url="https://p.test/v1", db=db)
        bound = apply_endpoint_binding(
            LLMConfig(model="anthropic/claude-sonnet-4-6"), endpoint.id, model_is_explicit=False
        )
        assert bound.model == "anthropic/claude-sonnet-4-6"

    def test_binding_to_a_gone_endpoint_changes_nothing(self, db: AppDatabase):
        config = LLMConfig()
        assert apply_endpoint_binding(config, "ghost") is config

    def test_the_default_is_the_dashboards_then_the_files(self, db: AppDatabase):
        endpoint = _go(db, default=False)
        assert resolve_endpoint_default(LLMConfig()) == ""
        assert resolve_endpoint_default(LLMConfig(), endpoint.id) == endpoint.id
        assert resolve_endpoint_default(LLMConfig(endpoint=endpoint.id)) == endpoint.id
        # A dashboard choice outranks the file's…
        other = endpoints.create(label="Proxy", base_url="https://p.test/v1", db=db)
        assert resolve_endpoint_default(LLMConfig(endpoint=endpoint.id), other.id) == other.id
        # …and a pointer to nothing degrades to the configured endpoint.
        assert resolve_endpoint_default(LLMConfig(endpoint="ghost")) == ""
        assert resolve_endpoint_default(LLMConfig(), "ghost") == ""


class TestRoutes:
    def test_an_endpoint_route_parses(self):
        route = parse_route("endpoint:opencode-go:kimi-k2.7-code")
        assert route is not None
        assert (route.backend, route.provider, route.model) == (
            "endpoint",
            "opencode-go",
            "kimi-k2.7-code",
        )
        assert route.value == "endpoint:opencode-go:kimi-k2.7-code"
        # A model id may itself carry colons; only the first two fields split.
        deep = parse_route("endpoint:aws:us.anthropic.claude:v1:0")
        assert deep is not None and deep.model == "us.anthropic.claude:v1:0"
        assert parse_route("endpoint:only-one-field") is None
        assert parse_route("kimi-k2.7-code") is None

    def test_a_route_sends_one_purpose_to_one_endpoint(self, db: AppDatabase):
        go = _go(db, default=False)
        bound = bind_model(
            LLMConfig(),
            endpoint_route(go.id, "glm-5.3-flash"),
            model_is_explicit=True,
            default=("", ""),
        )
        assert bound.endpoint == go.id
        assert bound.model == "glm-5.3-flash"
        assert bound.base_url == GO_URL

    def test_a_route_to_an_endpoint_that_is_gone_is_refused_not_redirected(self, db):
        _go(db)  # …and is the default, so a fallback would be silent and wrong
        bound = bind_model(
            LLMConfig(), "endpoint:ghost:some-model", model_is_explicit=True, default=("", "")
        )
        assert bound.endpoint == "ghost"
        with pytest.raises(LLMError, match="not configured"):
            create_llm(bound)

    def test_the_api_route_follows_whichever_endpoint_is_selected(self, db: AppDatabase):
        go = _go(db)
        bound = bind_model(
            LLMConfig(),
            "api:glm-5.3-flash",
            model_is_explicit=True,
            default=("", ""),
            api_endpoint=go.id,
        )
        assert bound.endpoint == go.id
        assert bound.oauth_provider is None
        # With nothing selected it stays on the configured endpoint.
        plain = bind_model(
            LLMConfig(), "api:openai/gpt-5.1", model_is_explicit=True, default=("", "")
        )
        assert plain.endpoint is None
        assert plain.base_url == "https://openrouter.ai/api/v1"

    def test_a_bare_id_goes_to_the_selected_endpoint(self, db: AppDatabase):
        go = _go(db)
        bound = bind_model(
            LLMConfig(),
            "glm-5.3-flash",
            model_is_explicit=True,
            default=("", ""),
            api_endpoint=go.id,
        )
        assert bound.endpoint == go.id
        assert bound.model == "glm-5.3-flash"

    def test_a_signed_in_account_still_outranks_an_endpoint_for_bare_ids(self, db):
        from mira.oauth import store as oauth_store
        from tests.test_oauth import _tokens

        oauth_store.save(_tokens(), db)
        go = _go(db)
        bound = bind_model(
            LLMConfig(),
            "gpt-5.6-sol",
            model_is_explicit=True,
            default=("chatgpt", "acct_123"),
            api_endpoint=go.id,
        )
        assert bound.oauth_provider == "chatgpt"
        assert bound.endpoint is None


class TestPurposes:
    def test_each_purpose_can_sit_on_a_different_endpoint(self, db: AppDatabase):
        go = _go(db)
        proxy = endpoints.create(label="Proxy", base_url="https://p.test/v1", db=db)
        db.set_setting("indexing_model", endpoint_route(proxy.id, "small-model"))
        indexing = llm_config_for("indexing", LLMConfig())
        review = llm_config_for("review", LLMConfig())
        assert (indexing.endpoint, indexing.base_url) == (proxy.id, "https://p.test/v1")
        assert (review.endpoint, review.base_url) == (go.id, GO_URL)

    def test_the_selected_endpoint_serves_every_purpose_by_default(self, db: AppDatabase):
        go = _go(db)
        for purpose in ("indexing", "review", "security"):
            assert llm_config_for(purpose, LLMConfig()).endpoint == go.id

    def test_indexing_falls_back_to_the_cheap_model_not_the_review_one(self, db):
        """Nobody picked a model yet, so each purpose gets the one the
        registry recommends for it — indexing runs over every file, and
        inheriting the review model there is a bill nobody asked for."""
        _go(db)
        assert llm_config_for("indexing", LLMConfig()).model == "glm-5.3-flash"
        assert llm_config_for("review", LLMConfig()).model == "kimi-k2.7-code"
        # The security sweep is the highest-stakes cheap pass; it follows
        # the review tier, never the indexing one.
        assert llm_config_for("security", LLMConfig()).model == "kimi-k2.7-code"

    def test_a_model_somebody_picked_is_left_alone(self, db: AppDatabase):
        _go(db)
        db.set_setting("indexing_model", "mimo-v2.5")
        assert llm_config_for("indexing", LLMConfig()).model == "mimo-v2.5"


class TestClient:
    def test_calls_carry_the_presets_headers_and_the_stored_key(self, db: AppDatabase):
        endpoint = _go(db)
        llm = create_llm(apply_endpoint_binding(LLMConfig(model="kimi-k2.7-code"), endpoint.id))
        headers = llm._build_headers()
        assert headers["Authorization"] == "Bearer oc_sk_test_1234"
        assert headers["x-opencode-client"] == "mira"
        assert len(headers["x-opencode-session"]) == 30
        assert headers["User-Agent"].startswith("mira/")

    def test_a_preset_behind_a_proxy_keeps_its_quirks(self, db: AppDatabase):
        endpoint = _go(db)
        endpoint.base_url = "https://proxy.internal.test/v1"
        endpoints.save(endpoint, db)
        llm = create_llm(apply_endpoint_binding(LLMConfig(), endpoint.id))
        assert "x-opencode-session" in llm._build_headers()
        assert llm._chat_url() == "https://proxy.internal.test/v1/chat/completions"

    def test_the_model_prefix_policy_follows_the_preset(self, db: AppDatabase):
        from mira.llm.base import _strip_model_prefix

        router = endpoints.create(
            label="Router", base_url="https://r.test/v1", preset="openrouter", db=db
        )
        go = _go(db, default=False)
        assert (
            _strip_model_prefix(
                "anthropic/claude-sonnet-4-6",
                endpoints.profile_for(endpoints.require(router.id, db)),
            )
            == "anthropic/claude-sonnet-4-6"
        )
        assert _strip_model_prefix("opencode-go/kimi-k3", endpoints.profile_for(go)) == "kimi-k3"

    def test_an_endpoint_that_is_gone_is_refused_rather_than_guessed_at(self, db):
        with pytest.raises(LLMError, match="not configured"):
            create_llm(LLMConfig(endpoint="ghost"))

    @pytest.mark.asyncio
    async def test_a_model_fixed_at_its_own_temperature_is_retried_without_one(self, db):
        """What the live endpoint taught us: Kimi K2.7 Code — the model
        recommended for reviews on Go — 400s on any temperature but its own."""
        import httpx

        bodies = []

        def handler(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content)
            bodies.append(body)
            if "temperature" in body:
                return httpx.Response(
                    400,
                    json={
                        "error": {
                            "type": "invalid_request_error",
                            "message": "invalid temperature: only 1 is allowed for this model",
                        }
                    },
                )
            return httpx.Response(
                200, json={"choices": [{"message": {"content": "pong"}}], "usage": {}}
            )

        endpoint = _go(db)
        llm = create_llm(apply_endpoint_binding(LLMConfig(), endpoint.id))
        real = httpx.AsyncClient
        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(
                httpx,
                "AsyncClient",
                lambda **kw: real(transport=httpx.MockTransport(handler), **kw),
            )
            assert await llm.complete([{"role": "user", "content": "hi"}]) == "pong"
            assert [("temperature" in b) for b in bodies] == [True, False]
            # The refusal is remembered, so the next call does not spend a
            # round trip rediscovering it.
            assert await llm.complete([{"role": "user", "content": "hi"}]) == "pong"
            assert [("temperature" in b) for b in bodies] == [True, False, False]


class TestDescribeCall:
    def test_the_models_page_names_the_endpoint_and_what_opens_it(self, db: AppDatabase):
        endpoint = _go(db)
        described = describe_call(apply_endpoint_binding(LLMConfig(), endpoint.id))
        assert described["backend"] == "endpoint"
        assert described["provider_label"] == "OpenCode Go"
        assert described["endpoint"] == GO_URL
        assert described["account_label"] == "stored key"
        assert described["connected"] is True

    def test_an_environment_key_is_named_by_its_variable(self, db, monkeypatch):
        monkeypatch.setenv("MY_KEY", "abc")
        endpoint = endpoints.create(
            label="Proxy", base_url="https://p.test/v1", api_key_env="MY_KEY", db=db
        )
        described = describe_call(apply_endpoint_binding(LLMConfig(), endpoint.id))
        assert described["account_label"] == "key from MY_KEY"

    def test_a_dead_endpoint_route_is_shown_as_one(self, db: AppDatabase):
        described = describe_call(LLMConfig(endpoint="ghost"))
        assert described["connected"] is False
        assert "not a configured endpoint" in described["provider_label"]

    def test_the_configured_endpoint_still_describes_itself(self, db: AppDatabase):
        assert describe_call(LLMConfig())["provider_label"] == "OpenRouter"


# ── Catalog ──────────────────────────────────────────────────────────


class TestCatalog:
    def test_a_stored_endpoint_is_named_by_its_preset(self, db: AppDatabase):
        endpoint = _go(db)
        config = apply_endpoint_binding(LLMConfig(), endpoint.id)
        assert active_backend(config) == "opencode-go"

    def test_a_custom_endpoint_has_no_curated_list(self, db: AppDatabase):
        endpoint = endpoints.create(label="Proxy", base_url="https://p.test/v1", db=db)
        config = apply_endpoint_binding(LLMConfig(), endpoint.id)
        assert active_backend(config) == "openai-compatible"

    def test_go_models_are_offered_on_go_and_nowhere_else(self):
        values = [o["value"] for o in build_options("opencode-go", None, "review")]
        assert values[0] == "kimi-k2.7-code"
        assert not any(v.startswith("anthropic/") for v in values)
        for backend in ("openrouter", "bedrock"):
            assert "kimi-k2.7-code" not in {
                o["value"] for o in build_options(backend, None, "review")
            }

    def test_options_name_the_endpoint_they_go_to(self, db: AppDatabase):
        go = _go(db)
        entries = [
            {
                "endpoint": go,
                "backend": "opencode-go",
                "catalog": None,
                "group": "OpenCode Go · opencode.ai/zen/go/v1",
                "detail": "Chat Completions · stored key",
            }
        ]
        options = endpoint_options(entries, "review")
        assert options[0]["value"] == "endpoint:opencode-go:kimi-k2.7-code"
        assert options[0]["group"] == "OpenCode Go · opencode.ai/zen/go/v1"
        # The endpoint bare ids already reach is not offered twice.
        assert endpoint_options(entries, "review", bare=go.id) == []


# ── Usage ────────────────────────────────────────────────────────────


class TestGoUsageDocument:
    def test_three_windows_with_their_names_and_resets(self):
        snapshot = key_providers.parse_opencode_go(GO_USAGE)
        assert snapshot is not None
        assert [w.name for w in snapshot.windows()] == ["5-hour", "weekly", "monthly"]
        assert snapshot.primary.used_percent == 12.5
        assert snapshot.secondary.used_percent == 40
        # A window the provider calls rate-limited is recorded as spent, at
        # whatever percent it read, so the refusal stays attached to *its*
        # reset (October 1st) and not to the 5-hour window's (09:00Z).
        assert snapshot.tertiary.used_percent == 100.0
        assert snapshot.tertiary.resets_at == 1790812800.0
        assert snapshot.source == "endpoint"
        assert snapshot.limit_reached is True
        assert snapshot.available(now=1789894801.0) is False  # 5-hour reset passed
        assert snapshot.available(now=1790812801.0) is True

    def test_a_missing_window_is_left_out(self):
        doc = {"usage": {"rolling": {"status": "ok", "percent": 5, "resetsAt": "x"}}}
        snapshot = key_providers.parse_opencode_go(doc)
        assert snapshot is not None
        assert snapshot.primary is not None and snapshot.primary.resets_at is None
        assert snapshot.secondary is None and snapshot.tertiary is None
        assert snapshot.headroom() == 95.0

    @pytest.mark.parametrize("doc", [None, "nope", {}, {"usage": {}}, {"usage": {"rolling": {}}}])
    def test_a_document_with_no_window_is_not_a_snapshot(self, doc):
        assert key_providers.parse_opencode_go(doc) is None

    def test_the_third_window_survives_the_round_trip(self):
        snapshot = key_providers.parse_opencode_go(GO_USAGE)
        again = UsageSnapshot.from_dict(json.loads(json.dumps(snapshot.to_dict())))
        assert again is not None and again.tertiary is not None
        assert again.tertiary.to_dict() == snapshot.tertiary.to_dict()
        assert again.headroom() == 0.0
        old = UsageSnapshot.from_dict({"primary": {"used_percent": 1}})
        assert old is not None and old.tertiary is None and old.has_data()


class TestUsageLookup:
    @pytest.mark.asyncio
    async def test_the_providers_refusal_is_quoted(self, monkeypatch):
        import httpx

        def handler(request: httpx.Request) -> httpx.Response:
            assert request.headers["Authorization"] == "Bearer bad"
            assert request.headers["x-opencode-client"] == "mira"
            return httpx.Response(
                401, json={"error": {"type": "AuthError", "message": "Invalid API key."}}
            )

        transport = httpx.MockTransport(handler)
        real = httpx.AsyncClient
        monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: real(transport=transport, **kw))
        with pytest.raises(key_providers.UsageError, match="HTTP 401: Invalid API key."):
            await key_providers.fetch_usage(profiles.get("opencode-go"), "bad")

    @pytest.mark.asyncio
    async def test_no_key_and_no_usage_endpoint_are_named(self):
        with pytest.raises(key_providers.UsageError, match="OPENCODE_API_KEY"):
            await key_providers.fetch_usage(profiles.get("opencode-go"), "")
        with pytest.raises(key_providers.UsageError, match="does not report usage"):
            await key_providers.fetch_usage(profiles.get("openrouter"), "sk-or-1")

    @pytest.mark.asyncio
    async def test_fresh_snapshot_is_reused_and_persisted(self, db, monkeypatch):
        calls = 0

        async def fetch(profile, api_key):
            nonlocal calls
            calls += 1
            return key_providers.parse_opencode_go(GO_USAGE)

        monkeypatch.setattr(key_providers, "fetch_usage", fetch)
        profile = profiles.get("opencode-go")
        first = await key_providers.usage_for("go", profile, "oc_sk_1", db)
        second = await key_providers.usage_for("go", profile, "oc_sk_1", db)
        assert calls == 1
        assert first is not None and second is not None
        assert second.fetched_at == first.fetched_at
        rows = db.list_settings("provider_usage:")
        assert len(rows) == 1 and "oc_sk_1" not in json.dumps(rows)
        # A rotated key starts from nothing rather than the old key's windows.
        assert key_providers.load_usage("go", "oc_sk_2", db) is None
        await key_providers.usage_for("go", profile, "oc_sk_1", db, force=True)
        assert calls == 2

    @pytest.mark.asyncio
    async def test_a_stale_snapshot_is_asked_for_again(self, db, monkeypatch):
        profile = profiles.get("opencode-go")
        stale = key_providers.parse_opencode_go(GO_USAGE)
        stale.fetched_at = time.time() - key_providers.FRESH_SECONDS - 1
        key_providers.save_usage("go", "oc_sk_1", stale, db)
        fresh = key_providers.parse_opencode_go(GO_USAGE)
        monkeypatch.setattr(key_providers, "fetch_usage", _async(fresh))
        found = await key_providers.usage_for("go", profile, "oc_sk_1", db)
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
        key_providers.save_usage("go", "oc_sk_1", stale, db)
        monkeypatch.setattr(key_providers, "fetch_usage", fetch)
        found = await key_providers.usage_for("go", profile, "oc_sk_1", db)
        assert found is not None and found.fetched_at == stale.fetched_at
        await key_providers.usage_for("go", profile, "oc_sk_1", db)
        assert calls == 1  # not retried within the failure window
        with pytest.raises(key_providers.UsageError):
            await key_providers.usage_for("go", profile, "oc_sk_1", db, force=True)
        assert calls == 2

    @pytest.mark.asyncio
    async def test_one_failure_does_not_silence_another_key_or_endpoint(self, db, monkeypatch):
        asked: list[tuple[str, str]] = []

        async def fetch(profile, api_key):
            asked.append((profile.get("name"), api_key))
            if api_key == "bad":
                raise key_providers.UsageError("Usage lookup answered HTTP 401: Invalid API key.")
            return key_providers.parse_opencode_go(GO_USAGE)

        monkeypatch.setattr(key_providers, "fetch_usage", fetch)
        profile = profiles.get("opencode-go")
        assert await key_providers.usage_for("go", profile, "bad", db) is None
        assert await key_providers.usage_for("go", profile, "good", db) is not None
        assert await key_providers.usage_for("go-2", profile, "bad", db) is None
        assert [k for _, k in asked] == ["bad", "good", "bad"]

    @pytest.mark.asyncio
    async def test_forgetting_an_endpoint_drops_its_snapshots(self, db, monkeypatch):
        monkeypatch.setattr(
            key_providers, "fetch_usage", _async(key_providers.parse_opencode_go(GO_USAGE))
        )
        profile = profiles.get("opencode-go")
        await key_providers.usage_for("go", profile, "k1", db)
        await key_providers.usage_for("go", profile, "k2", db)
        await key_providers.usage_for("other", profile, "k1", db)
        key_providers.forget_usage("go", db)
        assert key_providers.load_usage("go", "k1", db) is None
        assert key_providers.load_usage("go", "k2", db) is None
        assert key_providers.load_usage("other", "k1", db) is not None


class TestConnectionTest:
    @staticmethod
    def _serve(monkeypatch, handler):
        import httpx

        real = httpx.AsyncClient
        monkeypatch.setattr(
            httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(handler), **kw)
        )

    @pytest.mark.asyncio
    async def test_a_key_the_provider_accepts_is_reported_as_accepted(self, monkeypatch):
        import httpx

        seen = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(str(request.url))
            assert request.headers["Authorization"] == "Bearer good"
            if request.url.path.endswith("/usage"):
                return httpx.Response(200, json=GO_USAGE)
            return httpx.Response(200, json={"data": [{"id": "kimi-k3"}, {"id": "glm-5.3"}]})

        self._serve(monkeypatch, handler)
        result = await key_providers.test_connection(profiles.get("opencode-go"), "good")
        assert result["ok"] is True and result["models"] == 2
        assert "kimi-k3" in result["detail"] and "Key accepted" in result["detail"]
        # The list proves the URL; the usage endpoint is what proves the key.
        assert seen == [f"{GO_URL}/models", f"{GO_URL}/usage"]

    @pytest.mark.asyncio
    async def test_a_public_model_list_does_not_vouch_for_the_key(self, monkeypatch):
        """The failure the live endpoint taught us: OpenRouter lists its
        models to anybody, so a 200 there says nothing about the key."""
        import httpx

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"data": [{"id": "anthropic/claude-sonnet-4-6"}]})

        self._serve(monkeypatch, handler)
        result = await key_providers.test_connection(profiles.get("openrouter"), "sk-or-nonsense")
        assert result["ok"] is True
        assert "the key was not checked" in result["detail"]

    @pytest.mark.asyncio
    async def test_a_list_that_needs_a_key_vouches_for_it(self, monkeypatch):
        import httpx

        def handler(request: httpx.Request) -> httpx.Response:
            if "authorization" not in request.headers:
                return httpx.Response(401, json={"error": {"message": "Unauthorized"}})
            return httpx.Response(200, json={"data": [{"id": "some-model"}]})

        self._serve(monkeypatch, handler)
        result = await key_providers.test_connection({"base_url": "https://p.test/v1"}, "good")
        assert result["ok"] is True and "Key accepted" in result["detail"]

    @pytest.mark.asyncio
    async def test_with_no_key_the_answer_says_so(self, monkeypatch):
        import httpx

        self._serve(
            monkeypatch, lambda r: httpx.Response(200, json={"data": [{"id": "llama-3.3"}]})
        )
        result = await key_providers.test_connection({"base_url": "https://p.test/v1"}, "")
        assert result["ok"] is True and "No key was sent" in result["detail"]

    @pytest.mark.asyncio
    async def test_a_refusal_is_reported_in_the_providers_words(self, monkeypatch):
        import httpx

        self._serve(
            monkeypatch,
            lambda r: httpx.Response(401, json={"error": {"message": "Invalid API key."}}),
        )
        result = await key_providers.test_connection(profiles.get("opencode-go"), "bad")
        assert result["ok"] is False and "Invalid API key." in result["detail"]

    @pytest.mark.asyncio
    async def test_a_key_the_usage_endpoint_refuses_fails_the_test(self, monkeypatch):
        import httpx

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/usage"):
                return httpx.Response(403, json={"error": {"message": "Subscription required."}})
            return httpx.Response(200, json={"data": [{"id": "kimi-k3"}]})

        self._serve(monkeypatch, handler)
        result = await key_providers.test_connection(profiles.get("opencode-go"), "no-plan")
        assert result["ok"] is False and "Subscription required." in result["detail"]

    @pytest.mark.asyncio
    async def test_an_unreachable_endpoint_says_so(self, monkeypatch):
        import httpx

        def boom(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("name or service not known")

        self._serve(monkeypatch, boom)
        result = await key_providers.test_connection(
            {"base_url": "https://nope.test/v1"}, "whatever"
        )
        assert result["ok"] is False and "Could not reach" in result["detail"]


# ── Cards ────────────────────────────────────────────────────────────


class TestCards:
    @pytest.mark.asyncio
    async def test_stored_endpoints_come_first_then_the_files_own(self, db, monkeypatch):
        monkeypatch.setattr(
            key_providers, "fetch_usage", _async(key_providers.parse_opencode_go(GO_USAGE))
        )
        _go(db)
        cards = await key_providers.list_status(LLMConfig(), db)
        assert [c["id"] for c in cards] == ["opencode-go", "config"]
        go, from_config = cards
        assert go["is_default"] is True and go["editable"] is True
        assert go["key_source"] == "stored" and go["key_hint"] == "…1234"
        assert go["usage"]["tertiary"]["name"] == "monthly"
        assert go["available"] is False  # the monthly window is spent
        assert from_config["editable"] is False and from_config["is_default"] is False
        assert from_config["label"] == "OpenRouter"
        assert "oc_sk_test_1234" not in json.dumps(cards)

    @pytest.mark.asyncio
    async def test_with_nothing_stored_the_file_is_the_default(self, db, monkeypatch):
        monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-abcd1234")
        cards = await key_providers.list_status(LLMConfig(), db)
        assert len(cards) == 1
        assert cards[0]["is_default"] is True
        assert cards[0]["key_source"] == "env:OPENROUTER_API_KEY"
        assert cards[0]["key_hint"] == "…1234"

    @pytest.mark.asyncio
    async def test_bedrock_has_no_key_endpoint_to_show(self, db):
        assert await key_providers.list_status(LLMConfig(provider="bedrock"), db) == []


# ── Dashboard routes ─────────────────────────────────────────────────


class TestDashboardRoutes:
    @pytest.mark.asyncio
    async def test_add_an_endpoint_and_make_it_the_default(self, db: AppDatabase):
        from mira.dashboard.routers.providers import (
            EndpointBody,
            create_provider,
            list_providers,
        )

        card = await create_provider(
            EndpointBody(
                label="OpenCode Go",
                preset="opencode-go",
                api_key="oc_sk_live_9876",
                make_default=True,
            ),
            _admin(),
        )
        assert card["id"] == "opencode-go"
        assert card["is_default"] is True
        assert card["key_source"] == "stored"
        assert card["endpoint"] == GO_URL
        assert "oc_sk_live_9876" not in json.dumps(card)
        listed = await list_providers(_admin())
        assert listed["active"] == "opencode-go"
        assert listed["configured"] is True
        assert [p["id"] for p in listed["presets"]][0] == ""
        assert "oc_sk_live_9876" not in json.dumps(listed)
        # Adding a working endpoint is enough to leave the setup wizard.
        assert db.setup_complete is True

    @pytest.mark.asyncio
    async def test_editing_leaves_alone_what_it_was_not_given(self, db: AppDatabase):
        """A rename must not wipe the rest. What the live API taught us: a
        body carrying only a label was clearing the preset — and with it the
        endpoint's identity, its quirks and its fallback model."""
        from mira.dashboard.routers.providers import EndpointBody, update_provider

        endpoint = _go(db)
        card = await update_provider(endpoint.id, EndpointBody(label="Go (work account)"), _admin())
        assert card["label"] == "Go (work account)"
        stored = endpoints.require(endpoint.id, db)
        assert stored.preset == "opencode-go"
        assert stored.default_model == "kimi-k2.7-code"
        assert stored.base_url == GO_URL
        assert endpoints.key_for(stored, db) == "oc_sk_test_1234"
        # An empty string is the way to clear a field, and is not the same
        # as leaving it out.
        await update_provider(endpoint.id, EndpointBody(api_key=""), _admin())
        assert endpoints.key_for(endpoints.require(endpoint.id, db), db) == ""
        assert endpoints.require(endpoint.id, db).preset == "opencode-go"

    @pytest.mark.asyncio
    async def test_changing_the_preset_takes_its_model_with_it(self, db: AppDatabase):
        from mira.dashboard.routers.providers import EndpointBody, update_provider

        endpoint = _go(db)
        await update_provider(
            endpoint.id,
            EndpointBody(preset="openrouter", base_url="https://openrouter.ai/api/v1"),
            _admin(),
        )
        stored = endpoints.require(endpoint.id, db)
        assert stored.preset == "openrouter"
        # The registry names no model for OpenRouter's endpoint, so there is
        # nothing to fall back to and Go's pick does not linger.
        assert stored.default_model == ""

    @pytest.mark.asyncio
    async def test_a_bad_url_is_refused_with_the_reason(self, db: AppDatabase):
        from fastapi import HTTPException

        from mira.dashboard.routers.providers import EndpointBody, create_provider

        with pytest.raises(HTTPException) as refused:
            await create_provider(
                EndpointBody(label="Bad", base_url="http://public.example/v1"), _admin()
            )
        assert refused.value.status_code == 400
        assert "plain http" in refused.value.detail

    @pytest.mark.asyncio
    async def test_deleting_hands_the_default_back_to_the_file(self, db: AppDatabase):
        from mira.dashboard.routers.providers import (
            ActiveBody,
            delete_provider,
            set_active_provider,
        )

        endpoint = _go(db)
        assert set_active_provider(ActiveBody(endpoint=""), _admin())["active"] == ""
        set_active_provider(ActiveBody(endpoint=endpoint.id), _admin())
        assert delete_provider(endpoint.id, _admin())["active"] == ""
        assert endpoints.get(endpoint.id, db) is None

    @pytest.mark.asyncio
    async def test_unknown_endpoints_are_404(self, db: AppDatabase):
        from fastapi import HTTPException

        from mira.dashboard.routers.providers import (
            EndpointBody,
            delete_provider,
            refresh_provider_usage,
            update_provider,
        )

        for call in (
            lambda: update_provider("ghost", EndpointBody(label="x"), _admin()),
            lambda: delete_provider("ghost", _admin()),
            lambda: refresh_provider_usage("ghost", _admin()),
        ):
            with pytest.raises(HTTPException) as missing:
                result = call()
                if hasattr(result, "__await__"):
                    await result
            assert missing.value.status_code == 404

    @pytest.mark.asyncio
    async def test_the_test_route_reports_a_refusal_as_a_result(self, db, monkeypatch):
        from mira.dashboard.routers.providers import TestBody, test_provider

        monkeypatch.setattr(
            key_providers, "test_connection", _async({"ok": False, "detail": "Invalid API key."})
        )
        answer = await test_provider(TestBody(preset="opencode-go", api_key="bad"), _admin())
        assert answer == {"ok": False, "detail": "Invalid API key."}

    @pytest.mark.asyncio
    async def test_the_test_route_refuses_a_url_it_would_not_store(self, db):
        from fastapi import HTTPException

        from mira.dashboard.routers.providers import TestBody, test_provider

        with pytest.raises(HTTPException) as refused:
            await test_provider(TestBody(base_url="http://public.example/v1"), _admin())
        assert refused.value.status_code == 400

    @pytest.mark.asyncio
    async def test_usage_refresh_quotes_the_provider(self, db, monkeypatch):
        from fastapi import HTTPException

        from mira.dashboard.routers.providers import refresh_provider_usage

        _go(db)

        async def fetch(profile, api_key):
            raise key_providers.UsageError("Usage lookup answered HTTP 403: subscription required")

        monkeypatch.setattr(key_providers, "fetch_usage", fetch)
        with pytest.raises(HTTPException) as refused:
            await refresh_provider_usage("opencode-go", _admin())
        assert refused.value.status_code == 400
        assert "subscription required" in refused.value.detail

        monkeypatch.setattr(
            key_providers, "fetch_usage", _async(key_providers.parse_opencode_go(GO_USAGE))
        )
        card = await refresh_provider_usage("opencode-go", _admin())
        assert card["usage"]["primary"]["used_percent"] == 12.5

    @pytest.mark.asyncio
    async def test_every_route_is_admin_only(self, db: AppDatabase):
        from fastapi import HTTPException

        from mira.dashboard.routers.providers import (
            ActiveBody,
            EndpointBody,
            TestBody,
            create_provider,
            delete_provider,
            list_providers,
            refresh_provider_usage,
            set_active_provider,
            test_provider,
            update_provider,
        )

        _go(db)
        calls = [
            lambda: list_providers(_user()),
            lambda: create_provider(EndpointBody(label="x", base_url="https://x.test/v1"), _user()),
            lambda: update_provider("opencode-go", EndpointBody(label="x"), _user()),
            lambda: delete_provider("opencode-go", _user()),
            lambda: set_active_provider(ActiveBody(endpoint=""), _user()),
            lambda: refresh_provider_usage("opencode-go", _user()),
            lambda: test_provider(TestBody(base_url="https://x.test/v1"), _user()),
        ]
        for call in calls:
            with pytest.raises(HTTPException) as forbidden:
                result = call()
                if hasattr(result, "__await__"):
                    await result
            assert forbidden.value.status_code == 403


class TestLocalGuard:
    def test_two_endpoints_sharing_a_url_are_two_destinations(self, db: AppDatabase):
        """The local CLI refuses to send code somewhere else. Two stored
        endpoints can share a URL and open it with different keys, which is
        two recipients however alike the addresses look."""
        from mira.config import MiraConfig
        from mira.local.guard import destination_for

        first = endpoints.create(label="Team key", base_url="https://p.test/v1", db=db)
        second = endpoints.create(label="Personal key", base_url="https://p.test/v1", db=db)
        endpoints.set_secret(first.id, "key-one", db)
        endpoints.set_secret(second.id, "key-two", db)

        endpoints.set_active(first.id, db)
        one = destination_for(MiraConfig(), "review")
        endpoints.set_active(second.id, db)
        two = destination_for(MiraConfig(), "review")
        assert one.endpoint == two.endpoint
        assert one.key != two.key
        assert one.configured == first.id and two.configured == second.id
        assert first.id in one.describe()


class TestModelsPage:
    @pytest.mark.asyncio
    async def test_the_picker_lists_the_selected_endpoint_and_the_others(self, db, monkeypatch):
        from mira.dashboard import model_catalog
        from mira.dashboard.routers.admin import get_models

        monkeypatch.setattr(model_catalog, "_fetch_openai_style", _async([]))
        _go(db)
        endpoints.create(label="Proxy", base_url="https://p.test/v1", preset="openrouter", db=db)
        models = await get_models()
        assert models.backend == "opencode-go"
        assert models.default_backend["provider_label"] == "OpenCode Go"
        # Bare ids go to Go, so its models are offered bare…
        values = {o.value for o in models.review_options}
        assert "kimi-k2.7-code" in values
        # …and the other endpoint is offered as a route that names it.
        assert any(v.startswith("endpoint:proxy:") for v in values)
        assert not any(v.startswith("endpoint:opencode-go:") for v in values)
        assert models.review_route["provider_label"] == "OpenCode Go"
        assert models.review_model == "kimi-k2.7-code"


class TestCLI:
    def test_auth_status_lists_the_endpoints(self, db: AppDatabase, monkeypatch):
        from mira.cli import main

        monkeypatch.setattr(
            key_providers, "fetch_usage", _async(key_providers.parse_opencode_go(GO_USAGE))
        )
        _go(db)
        monkeypatch.setattr("mira.cli.load_config", lambda: type("C", (), {"llm": LLMConfig()})())
        result = CliRunner().invoke(main, ["auth", "status"])
        assert result.exit_code == 0, result.output
        assert "API-key endpoints:" in result.output
        assert "OpenCode Go" in result.output
        assert "key stored here …1234" in result.output
        assert "monthly 100% used" in result.output
        assert "oc_sk_test_1234" not in result.output
