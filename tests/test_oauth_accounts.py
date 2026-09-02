"""Several accounts per provider, their allowance, and routing between them.

Covers:
- Account slots: two accounts side by side, reconnecting into the same slot,
  the previous build's one-per-provider row moving into a slot, and what
  removing an account does to the default pointer.
- Usage: the rate-limit headers and the usage endpoint read into a snapshot,
  the bookkeeping that survives a refresh, and the choice between accounts.
- Model routes: ``oauth:<provider>:<account>:<model>`` and ``api:<model>``
  parsed, bound, and described for the Models page.
- Rotation: a 429 moving the call to another account, one account handing
  the 429 back, a pinned account never swapped, and usage recorded per call.
- The account-scoped dashboard routes and the CLI.
"""

from __future__ import annotations

import json
import time

import httpx
import pytest

from mira.config import LLMConfig
from mira.dashboard.db import AppDatabase
from mira.dashboard.models_config import llm_config_for
from mira.exceptions import LLMError
from mira.llm import create_llm
from mira.llm.oauth import OAuthResponsesProvider
from mira.oauth import manager, store
from mira.oauth.base import OAuthTokens
from mira.oauth.chatgpt import ChatGPTOAuthProvider
from tests.test_oauth import _admin, _async, _tokens


@pytest.fixture
def db(tmp_path, monkeypatch: pytest.MonkeyPatch) -> AppDatabase:
    """Fresh per-test SQLite DB, swapped in for the module-level ``_app_db``."""
    monkeypatch.setenv("MIRA_INDEX_DIR", str(tmp_path))
    database = AppDatabase(url="", admin_password="admin")
    monkeypatch.setattr("mira.dashboard.api._app_db", database)
    return database


@pytest.fixture(autouse=True)
def _no_live_model_list(monkeypatch: pytest.MonkeyPatch):
    """Keep the account model list off the network: the curated list stands in."""
    from mira.dashboard import model_catalog

    model_catalog._account_model_cache.clear()
    monkeypatch.setattr(
        ChatGPTOAuthProvider, "fetch_models", classmethod(lambda cls, tokens: _async(None))
    )
    yield
    model_catalog._account_model_cache.clear()


def _second_tokens(**overrides) -> OAuthTokens:
    base = {
        "provider": "chatgpt",
        "access_token": "at_b",
        "refresh_token": "rt_b",
        "expires_at": time.time() + 3600,
        "account_id": "acct_456",
        "account_label": "ops@example.com",
        "plan": "plus",
    }
    return OAuthTokens(**{**base, **overrides})


class TestAccountSlots:
    def test_two_accounts_live_side_by_side(self, db: AppDatabase):
        store.save(_tokens(), db)
        store.save(_second_tokens(), db)
        assert set(store.accounts("chatgpt", db)) == {"acct_123", "acct_456"}
        # Two accounts make "any" ambiguous for a plain load.
        assert store.load("chatgpt", db=db) is None

    def test_signing_in_again_replaces_the_same_slot(self, db: AppDatabase):
        store.save(_tokens(), db)
        store.save(_tokens(access_token="at_new"), db)
        found = store.accounts("chatgpt", db)
        assert list(found) == ["acct_123"]
        assert found["acct_123"].access_token == "at_new"

    def test_slot_key_is_safe_for_routes_and_row_keys(self):
        from mira.oauth.base import account_key_for

        assert account_key_for("acct 12:3/x") == "acct123x"
        assert account_key_for("") != account_key_for("")  # random, never empty

    @pytest.mark.asyncio
    async def test_a_refresh_lands_in_the_same_slot(self, monkeypatch):
        monkeypatch.setattr(
            ChatGPTOAuthProvider,
            "_post_token",
            classmethod(lambda cls, data: _async({"access_token": "at_2", "expires_in": 60})),
        )
        refreshed = await ChatGPTOAuthProvider.refresh(_tokens(account_key="slot_a"))
        assert refreshed.account_key == "slot_a"

    def test_removing_one_account_keeps_a_provider_wide_default(self, db: AppDatabase):
        store.save(_tokens(), db)
        store.save(_second_tokens(), db)
        store.set_active("chatgpt", "", db)
        store.delete("chatgpt", "acct_123", db)
        assert store.get_active_ref(db) == "chatgpt"
        store.delete("chatgpt", "acct_456", db)
        assert store.get_active_ref(db) == ""

    def test_removing_the_pinned_account_clears_the_pointer(self, db: AppDatabase):
        store.save(_tokens(), db)
        store.save(_second_tokens(), db)
        store.set_active("chatgpt", "acct_123", db)
        store.delete("chatgpt", "acct_123", db)
        assert store.get_active_ref(db) == ""

    def test_a_pinned_account_that_is_gone_degrades_to_any(self, db: AppDatabase):
        store.save(_second_tokens(), db)
        store.set_active("chatgpt", "acct_123", db)
        assert store.active_binding(db) == ("chatgpt", "")

    def test_ref_parsing(self):
        assert store.parse_ref("chatgpt") == ("chatgpt", "")
        assert store.parse_ref("chatgpt:*") == ("chatgpt", "")
        assert store.parse_ref("ChatGPT:acct_1") == ("chatgpt", "acct_1")
        assert store.parse_ref("") == ("", "")
        assert store.format_ref("chatgpt", "acct_1") == "chatgpt:acct_1"
        assert store.format_ref("chatgpt", "") == "chatgpt"

    def test_the_first_account_becomes_the_default_and_a_second_does_not_change_it(
        self, db: AppDatabase
    ):
        manager.store_session(_tokens(), db)
        assert store.get_active_ref(db) == "chatgpt"
        store.set_active("chatgpt", "acct_123", db)
        manager.store_session(_second_tokens(), db)
        assert store.get_active_ref(db) == "chatgpt:acct_123"

    def test_status_lists_every_account_with_its_default_mark(self, db: AppDatabase):
        store.save(_tokens(), db)
        store.save(_second_tokens(), db)
        store.set_active("chatgpt", "acct_456", db)
        status = store.status("chatgpt", db)
        assert status["default_mode"] == "pinned"
        marks = {a["key"]: a["is_default"] for a in status["accounts"]}
        assert marks == {"acct_123": False, "acct_456": True}


class TestUsage:
    def test_window_names(self):
        from mira.oauth.usage import window_name

        assert window_name(300) == "5-hour"
        assert window_name(10080) == "weekly"
        assert window_name(1440) == "daily"
        assert window_name(90) == "90-minute"
        assert window_name(None) == "window"

    def test_headers_become_a_snapshot(self):
        headers = httpx.Headers(
            {
                "x-codex-primary-used-percent": "42.5",
                "x-codex-primary-window-minutes": "300",
                "x-codex-primary-reset-at": "1704069000",
                "x-codex-secondary-used-percent": "80",
                "x-codex-secondary-window-minutes": "10080",
                "x-codex-credits-has-credits": "true",
                "x-codex-credits-unlimited": "false",
                "x-codex-credits-balance": "9.99",
            }
        )
        snapshot = ChatGPTOAuthProvider.usage_from_headers(headers)
        assert snapshot is not None
        assert snapshot.primary.used_percent == 42.5
        assert snapshot.primary.name == "5-hour"
        assert snapshot.primary.resets_at == 1704069000
        assert snapshot.secondary.name == "weekly"
        assert snapshot.credits == {"has_credits": True, "unlimited": False, "balance": "9.99"}
        assert snapshot.source == "headers"

    def test_headers_without_usage_are_not_a_snapshot(self):
        assert ChatGPTOAuthProvider.usage_from_headers(httpx.Headers({"x-request-id": "1"})) is None

    def test_the_usage_endpoint_document_is_read(self):
        from mira.oauth.chatgpt import usage_from_payload

        snapshot = usage_from_payload(
            {
                "plan_type": "plus",
                "rate_limit": {
                    "primary_window": {
                        "used_percent": 12,
                        "limit_window_seconds": 18000,
                        "reset_after_seconds": 600,
                        "reset_at": 1704069000,
                    },
                    "secondary_window": {
                        "used_percent": 55,
                        "limit_window_seconds": 604800,
                        "reset_after_seconds": 0,
                        "reset_at": 1704500000,
                    },
                },
                "credits": {"has_credits": False, "unlimited": False, "balance": None},
            }
        )
        assert snapshot is not None
        assert snapshot.plan == "plus"
        assert snapshot.primary.window_minutes == 300
        assert snapshot.secondary.name == "weekly"
        assert snapshot.secondary.resets_at == 1704500000
        assert snapshot.source == "endpoint"

    def test_the_model_list_document_is_read_in_the_backends_order(self):
        from mira.oauth.chatgpt import models_from_payload

        models = models_from_payload(
            {
                "models": [
                    {"slug": "gpt-5.6", "display_name": "GPT-5.6", "priority": 2},
                    {
                        "slug": "gpt-5.6-codex",
                        "display_name": "GPT-5.6 Codex",
                        "priority": 1,
                        "supported_reasoning_levels": [{"effort": "medium"}, {"effort": "high"}],
                    },
                    {"slug": "hidden-model", "display_name": "Hidden", "visibility": "hide"},
                ]
            }
        )
        assert [m["value"] for m in models] == ["gpt-5.6-codex", "gpt-5.6"]
        assert models[0]["reasoning_levels"] == ["medium", "high"]

    def test_snapshot_round_trips_with_bookkeeping(self, db: AppDatabase):
        from mira.oauth.usage import UsageSnapshot, UsageWindow

        store.save(_tokens(), db)
        store.mark_exhausted("chatgpt", "acct_123", time.time() + 100, db)
        saved = store.save_usage(
            "chatgpt",
            "acct_123",
            UsageSnapshot(primary=UsageWindow(used_percent=10, window_minutes=300)),
            db,
        )
        # The backend's report does not know about the refusal; Mira's note survives.
        assert saved.exhausted_until > time.time()
        loaded = store.load_usage("chatgpt", "acct_123", db)
        assert loaded is not None and loaded.primary.used_percent == 10
        assert not loaded.available()

    def test_choose_account_prefers_headroom_then_least_recently_used(self):
        from mira.oauth.usage import UsageSnapshot, UsageWindow, choose_account

        fresh = UsageSnapshot(primary=UsageWindow(used_percent=10), last_used_at=50)
        busy = UsageSnapshot(primary=UsageWindow(used_percent=70), last_used_at=10)
        spent = UsageSnapshot(primary=UsageWindow(used_percent=100, resets_at=time.time() + 3600))
        refused = UsageSnapshot(exhausted_until=time.time() + 60)
        assert choose_account([("a", busy), ("b", fresh)]) == "b"
        assert choose_account([("a", spent), ("b", refused), ("c", busy)]) == "c"
        assert choose_account([("a", spent), ("b", refused)]) is None
        # An account nobody has used yet counts as untouched.
        assert choose_account([("a", fresh), ("new", None)]) == "new"
        # Equal headroom: the one used longest ago takes the call.
        tie_a = UsageSnapshot(primary=UsageWindow(used_percent=10), last_used_at=100)
        tie_b = UsageSnapshot(primary=UsageWindow(used_percent=10), last_used_at=20)
        assert choose_account([("a", tie_a), ("b", tie_b)]) == "b"
        assert choose_account([("a", tie_a), ("b", tie_b)], exclude={"b"}) == "a"

    @pytest.mark.asyncio
    async def test_refresh_usage_records_what_the_provider_says(self, db, monkeypatch):
        from mira.oauth.usage import UsageSnapshot, UsageWindow

        store.save(_tokens(plan=""), db)
        monkeypatch.setattr(
            ChatGPTOAuthProvider,
            "fetch_usage",
            classmethod(
                lambda cls, t: _async(
                    UsageSnapshot(
                        primary=UsageWindow(used_percent=33, window_minutes=300),
                        plan="plus",
                        source="endpoint",
                    )
                )
            ),
        )
        status = await manager.refresh_usage("chatgpt", "acct_123", db)
        assert status["usage"]["primary"]["used_percent"] == 33
        # A plan the token did not carry is learned from the endpoint.
        assert status["plan"] == "plus"


class TestModelRoutes:
    def test_parsing(self):
        from mira.oauth.routes import ModelRoute, parse_route

        assert parse_route("gpt-5-codex") is None
        assert parse_route("anthropic/claude-sonnet-4-6") is None
        assert parse_route("oauth:chatgpt:acct_1:gpt-5-codex") == ModelRoute(
            backend="oauth", model="gpt-5-codex", provider="chatgpt", account="acct_1"
        )
        assert parse_route("oauth:chatgpt:*:gpt-5").rotates
        assert parse_route("oauth:chatgpt::gpt-5").account == "*"
        # The model part keeps its own colons (Bedrock-style ids).
        assert parse_route("oauth:p:k:vendor.model-v1:0").model == "vendor.model-v1:0"
        assert parse_route("api:openai/gpt-5.1") == ModelRoute(
            backend="api", model="openai/gpt-5.1"
        )
        assert parse_route("oauth:broken") is None
        assert parse_route("api:") is None

    def test_formatting_round_trips(self):
        from mira.oauth.routes import api_route, bare_model, oauth_route, parse_route

        value = oauth_route("chatgpt", "", "gpt-5")
        assert value == "oauth:chatgpt:*:gpt-5"
        assert parse_route(value).value == value
        assert bare_model(value) == "gpt-5"
        assert bare_model(api_route("x/y")) == "x/y"
        assert bare_model("plain") == "plain"

    def test_an_explicit_account_route_binds_that_account(self, db: AppDatabase):
        store.save(_tokens(), db)
        store.save(_second_tokens(), db)
        db.set_setting("review_model", "oauth:chatgpt:acct_456:gpt-5")
        resolved = llm_config_for("review", LLMConfig())
        assert resolved.oauth_provider == "chatgpt"
        assert resolved.oauth_account == "acct_456"
        assert resolved.model == "gpt-5"
        assert resolved.base_url == "https://chatgpt.com/backend-api/codex"
        assert resolved.api_style == "responses"

    def test_a_rotating_route_leaves_the_account_open(self, db: AppDatabase):
        store.save(_tokens(), db)
        db.set_setting("review_model", "oauth:chatgpt:*:gpt-5-codex")
        resolved = llm_config_for("review", LLMConfig())
        assert resolved.oauth_provider == "chatgpt"
        assert resolved.oauth_account is None

    def test_an_api_route_stays_on_the_key_despite_a_default_session(self, db: AppDatabase):
        # Indexing through a cheap key-based model while reviews use the
        # signed-in account: the whole reason the prefix exists.
        store.save(_tokens(), db)
        db.set_setting("llm_oauth_provider", "chatgpt")
        db.set_setting("indexing_model", "api:gpt-5.6")
        resolved = llm_config_for("indexing", LLMConfig())
        assert resolved.oauth_provider is None
        assert resolved.model == "gpt-5.6"
        assert resolved.base_url == "https://openrouter.ai/api/v1"
        # …while a bare id for the review still goes to the session.
        assert llm_config_for("review", LLMConfig()).oauth_provider == "chatgpt"

    def test_a_route_model_is_sent_as_written(self, db: AppDatabase):
        # A route was chosen from that account's own list; the foreign-vendor
        # replacement is for bare ids inherited from somewhere else.
        store.save(_tokens(), db)
        db.set_setting("review_model", "oauth:chatgpt:*:gpt-5.6-codex")
        assert llm_config_for("review", LLMConfig()).model == "gpt-5.6-codex"

    def test_a_pinned_default_account_is_carried_into_the_config(self, db: AppDatabase):
        store.save(_tokens(), db)
        store.save(_second_tokens(), db)
        db.set_setting("llm_oauth_provider", "chatgpt:acct_456")
        resolved = llm_config_for("review", LLMConfig())
        assert resolved.oauth_account == "acct_456"

    def test_mira_yaml_can_pin_an_account(self, db: AppDatabase):
        store.save(_tokens(), db)
        store.save(_second_tokens(), db)
        cfg = LLMConfig(oauth_provider="chatgpt", oauth_account="acct_123")
        assert llm_config_for("review", cfg).oauth_account == "acct_123"
        assert LLMConfig(oauth_account="*").oauth_account is None

    def test_describe_call_names_the_backend(self, db: AppDatabase):
        from mira.dashboard.models_config import describe_call

        store.save(_tokens(), db)
        db.set_setting("review_model", "oauth:chatgpt:acct_123:gpt-5")
        described = describe_call(llm_config_for("review", LLMConfig()))
        assert described["backend"] == "oauth"
        assert described["account_label"] == "dev@example.com"
        assert described["protocol"].startswith("Responses API")
        assert described["model"] == "gpt-5"
        plain = describe_call(LLMConfig())
        assert plain["backend"] == "api"
        assert plain["provider_label"] == "OpenRouter"
        assert plain["api_style"] == "chat"


class TestRotation:
    """The client moving between accounts when one is refused."""

    def _provider(self, monkeypatch, answers: dict[str, list[httpx.Response]]):
        """An OAuth provider whose transport answers per access token."""
        provider = create_llm(LLMConfig(oauth_provider="chatgpt"))

        async def _send(self, client, body):
            token = self._build_headers()["Authorization"].removeprefix("Bearer ")
            queue = answers[token]
            return queue.pop(0) if len(queue) > 1 else queue[0]

        monkeypatch.setattr(OAuthResponsesProvider, "_send", _send)
        return provider

    def _ok(self, **headers) -> httpx.Response:
        return httpx.Response(200, json={"id": "r", "output": []}, headers=headers)

    @pytest.mark.asyncio
    async def test_a_429_moves_the_call_to_another_account(self, db, monkeypatch):
        store.save(_tokens(), db)
        store.save(_second_tokens(), db)
        limited = httpx.Response(429, text="slow down", headers={"retry-after": "120"})
        provider = self._provider(monkeypatch, {"at_1": [limited], "at_b": [self._ok()]})
        # Make the first account the ranked pick so the test is deterministic.
        store.mark_used("chatgpt", "acct_456", db)
        async with httpx.AsyncClient() as client:
            resp = await provider._post(client, {"model": "gpt-5-codex", "input": []})
        assert resp.status_code == 200
        assert provider.account_key == "acct_456"
        refused = store.load_usage("chatgpt", "acct_123", db)
        assert refused is not None and refused.exhausted_until > time.time() + 60
        assert not refused.available()

    @pytest.mark.asyncio
    async def test_with_one_account_the_429_is_handed_back(self, db, monkeypatch):
        # Nothing to rotate to: the retry policy waits on Retry-After instead.
        store.save(_tokens(), db)
        limited = httpx.Response(429, text="slow down", headers={"retry-after": "5"})
        provider = self._provider(monkeypatch, {"at_1": [limited]})
        async with httpx.AsyncClient() as client:
            resp = await provider._post(client, {"model": "gpt-5-codex", "input": []})
        assert resp.status_code == 429

    @pytest.mark.asyncio
    async def test_a_pinned_account_is_never_swapped(self, db, monkeypatch):
        store.save(_tokens(), db)
        store.save(_second_tokens(), db)
        limited = httpx.Response(429, text="slow down")
        provider = create_llm(LLMConfig(oauth_provider="chatgpt", oauth_account="acct_123"))

        async def _send(self, client, body):
            return limited

        monkeypatch.setattr(OAuthResponsesProvider, "_send", _send)
        async with httpx.AsyncClient() as client:
            resp = await provider._post(client, {"model": "gpt-5-codex", "input": []})
        assert resp.status_code == 429
        assert provider.account_key == "acct_123"

    @pytest.mark.asyncio
    async def test_every_response_records_the_accounts_usage(self, db, monkeypatch):
        store.save(_tokens(), db)
        ok = self._ok(
            **{
                "x-codex-primary-used-percent": "61",
                "x-codex-primary-window-minutes": "300",
                "x-codex-secondary-used-percent": "12",
                "x-codex-secondary-window-minutes": "10080",
            }
        )
        provider = self._provider(monkeypatch, {"at_1": [ok]})
        async with httpx.AsyncClient() as client:
            await provider._post(client, {"model": "gpt-5-codex", "input": []})
        usage = store.load_usage("chatgpt", "acct_123", db)
        assert usage is not None
        assert usage.primary.used_percent == 61
        assert usage.secondary.name == "weekly"
        assert usage.last_used_at > 0

    @pytest.mark.asyncio
    async def test_the_stream_keeps_the_rate_limit_headers(self, db):
        # The SSE body is collapsed into one response; the headers that say
        # how much allowance is left must survive that collapse.
        store.save(_tokens(), db)
        provider = create_llm(LLMConfig(oauth_provider="chatgpt"))
        await provider._ensure_token()

        class _Stream:
            status_code = 200
            headers = httpx.Headers(
                {"x-codex-primary-used-percent": "7", "content-type": "text/event-stream"}
            )

            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return False

            async def aiter_lines(self):
                yield 'data: {"type": "response.completed", "response": {"id": "r1"}}'

        class _Client:
            def stream(self, *args, **kwargs):
                return _Stream()

        resp = await provider._stream(_Client(), {"model": "m"})
        assert resp.json()["id"] == "r1"
        assert resp.headers["x-codex-primary-used-percent"] == "7"

    @pytest.mark.asyncio
    async def test_no_account_available_is_a_clear_error(self, db: AppDatabase):
        store.save(_tokens(), db)
        store.mark_exhausted("chatgpt", "acct_123", time.time() + 600, db)
        provider = create_llm(LLMConfig(oauth_provider="chatgpt"))
        with pytest.raises(LLMError) as exc:
            await provider._ensure_token()
        assert exc.value.code == "oauth_accounts_exhausted"


class TestAccountRoutes:
    def test_activating_one_account_pins_it(self, db: AppDatabase):
        from mira.dashboard.routers.oauth import OAuthActiveRequest, set_active_oauth

        store.save(_tokens(), db)
        store.save(_second_tokens(), db)
        out = set_active_oauth(OAuthActiveRequest(provider="chatgpt", account="acct_456"), _admin())
        assert out["active_ref"] == "chatgpt:acct_456"
        out = set_active_oauth(OAuthActiveRequest(provider="chatgpt", account="*"), _admin())
        assert out["active_ref"] == "chatgpt"

    def test_activating_an_unknown_account_is_refused(self, db: AppDatabase):
        from fastapi import HTTPException

        from mira.dashboard.routers.oauth import OAuthActiveRequest, set_active_oauth

        store.save(_tokens(), db)
        with pytest.raises(HTTPException) as exc:
            set_active_oauth(OAuthActiveRequest(provider="chatgpt", account="nope"), _admin())
        assert exc.value.status_code == 400

    def test_disconnecting_one_account_leaves_the_other(self, db: AppDatabase):
        from mira.dashboard.routers.oauth import disconnect_oauth_account

        store.save(_tokens(), db)
        store.save(_second_tokens(), db)
        disconnect_oauth_account("chatgpt", "acct_123", _admin())
        assert list(store.accounts("chatgpt", db)) == ["acct_456"]

    def test_disconnecting_a_missing_account_is_a_404(self, db: AppDatabase):
        from fastapi import HTTPException

        from mira.dashboard.routers.oauth import disconnect_oauth_account

        with pytest.raises(HTTPException) as exc:
            disconnect_oauth_account("chatgpt", "acct_123", _admin())
        assert exc.value.status_code == 404

    @pytest.mark.asyncio
    async def test_usage_route_asks_the_provider(self, db, monkeypatch):
        from mira.dashboard.routers.oauth import refresh_oauth_usage
        from mira.oauth.usage import UsageSnapshot, UsageWindow

        store.save(_tokens(), db)
        monkeypatch.setattr(
            ChatGPTOAuthProvider,
            "fetch_usage",
            classmethod(
                lambda cls, t: _async(UsageSnapshot(secondary=UsageWindow(used_percent=90)))
            ),
        )
        status = await refresh_oauth_usage("chatgpt", "acct_123", _admin())
        assert status["usage"]["secondary"]["used_percent"] == 90
        assert "at_1" not in json.dumps(status)

    def test_providers_report_accounts_and_the_default(self, db: AppDatabase):
        from mira.dashboard.routers.oauth import list_oauth_providers

        store.save(_tokens(), db)
        store.save(_second_tokens(), db)
        store.set_active("chatgpt", "", db)
        state = list_oauth_providers(_admin())
        assert state["active_provider"] == "chatgpt"
        assert state["active_account"] == ""
        chatgpt = state["providers"][0]
        assert chatgpt["default_mode"] == "rotate"
        assert [a["key"] for a in chatgpt["accounts"]] == ["acct_123", "acct_456"]
        assert "at_1" not in json.dumps(state)

    @pytest.mark.asyncio
    async def test_models_endpoint_lists_each_account_as_its_own_section(self, db):
        from mira.dashboard.routers.admin import get_models

        store.save(_tokens(), db)
        store.save(_second_tokens(), db)
        models = await get_models()
        # No default session: bare ids are the API key's own catalog…
        assert models.default_backend == {}
        assert models.backend == "openrouter"
        groups = [o.group for o in models.review_options]
        assert groups[0].startswith("OpenRouter")
        # …and every account, plus "any account", is a section of its own.
        assert any("dev@example.com" in g for g in groups)
        assert any("ops@example.com" in g for g in groups)
        assert any("any account" in g for g in groups)
        values = {o.value for o in models.review_options}
        assert "oauth:chatgpt:*:gpt-5-codex" in values
        assert "oauth:chatgpt:acct_456:gpt-5-codex" in values
        # The API key's options are bare here; no `api:` prefix is needed.
        assert not any(v.startswith("api:") for v in values)

    @pytest.mark.asyncio
    async def test_models_endpoint_describes_each_purposes_route(self, db):
        from mira.dashboard.routers.admin import get_models

        store.save(_tokens(), db)
        db.set_setting("llm_oauth_provider", "chatgpt")
        db.set_setting("indexing_model", "api:openai/gpt-5-nano")
        models = await get_models()
        assert models.review_route["backend"] == "oauth"
        assert models.review_route["account_label"] == "dev@example.com"
        assert models.indexing_route["backend"] == "api"
        assert models.indexing_route["model"] == "openai/gpt-5-nano"
        assert models.indexing_model == "api:openai/gpt-5-nano"
        # The same answers the review path gives.
        assert llm_config_for("indexing", LLMConfig()).model == "openai/gpt-5-nano"
        assert llm_config_for("review", LLMConfig()).oauth_provider == "chatgpt"

    @pytest.mark.asyncio
    async def test_the_live_model_list_is_used_when_the_provider_answers(self, db, monkeypatch):
        from mira.dashboard.routers.admin import get_models

        store.save(_tokens(), db)
        monkeypatch.setattr(
            ChatGPTOAuthProvider,
            "fetch_models",
            classmethod(
                lambda cls, t: _async([{"value": "gpt-5.6-codex", "label": "GPT-5.6 Codex"}])
            ),
        )
        models = await get_models()
        values = {o.value for o in models.review_options}
        assert "oauth:chatgpt:acct_123:gpt-5.6-codex" in values
        assert "oauth:chatgpt:acct_123:gpt-5-codex" not in values


class TestAuthCli:
    def test_use_pins_an_account_and_logout_forgets_it(self, db: AppDatabase):
        from click.testing import CliRunner

        from mira.cli import main

        store.save(_tokens(), db)
        store.save(_second_tokens(), db)
        runner = CliRunner()
        result = runner.invoke(main, ["auth", "use", "chatgpt:acct_456"])
        assert result.exit_code == 0, result.output
        assert "ops@example.com" in result.output
        assert store.get_active_ref(db) == "chatgpt:acct_456"

        result = runner.invoke(main, ["auth", "status"])
        assert result.exit_code == 0, result.output
        assert "dev@example.com" in result.output and "ops@example.com" in result.output
        assert "chatgpt:acct_456" in result.output

        result = runner.invoke(main, ["auth", "logout", "chatgpt:acct_456"])
        assert result.exit_code == 0, result.output
        assert list(store.accounts("chatgpt", db)) == ["acct_123"]
        assert store.get_active_ref(db) == ""

    def test_use_rejects_an_unknown_account(self, db: AppDatabase):
        from click.testing import CliRunner

        from mira.cli import main

        store.save(_tokens(), db)
        result = CliRunner().invoke(main, ["auth", "use", "chatgpt:nope"])
        assert result.exit_code != 0
        assert "no account" in result.output
