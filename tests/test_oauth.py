"""Tests for the generic OAuth layer and the ChatGPT provider on top of it.

Covers:
- PKCE + authorization URL construction, and the ChatGPT-specific parameters.
- Token exchange/refresh: identity extraction, expiry, non-rotating refresh
  tokens, and issuer errors.
- Storage: round-trip, disconnect clearing the active pointer, refresh-on-read.
- The login state machine: pasted redirects, replayed/expired attempts.
- Config resolution: which endpoint and model a review ends up using.
- The streaming Responses transport the ChatGPT backend requires.
"""

from __future__ import annotations

import base64
import json
import time
from pathlib import Path

import httpx
import pytest

from mira.config import LLMConfig
from mira.dashboard.db import AppDatabase
from mira.dashboard.model_catalog import active_backend, build_options
from mira.dashboard.models_config import (
    apply_oauth_binding,
    llm_config_for,
    resolve_oauth_provider,
)
from mira.exceptions import LLMError
from mira.llm import create_llm
from mira.llm.oauth import OAuthResponsesProvider, _collect_stream
from mira.oauth import manager, registry, store
from mira.oauth.base import OAuthError, OAuthTokens, PkcePair, decode_jwt_claims
from mira.oauth.chatgpt import ChatGPTOAuthProvider


@pytest.fixture
def db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AppDatabase:
    """Fresh per-test SQLite DB, swapped in for the module-level ``_app_db``."""
    monkeypatch.setenv("MIRA_INDEX_DIR", str(tmp_path))
    database = AppDatabase(url="", admin_password="admin")
    monkeypatch.setattr("mira.dashboard.api._app_db", database)
    return database


def _jwt(claims: dict) -> str:
    """An unsigned JWT carrying ``claims`` — enough for the client-side read."""

    def part(obj: dict) -> str:
        return base64.urlsafe_b64encode(json.dumps(obj).encode()).decode().rstrip("=")

    return f"{part({'alg': 'none'})}.{part(claims)}.signature"


CHATGPT_ID_TOKEN = _jwt(
    {
        "email": "dev@example.com",
        "https://api.openai.com/auth": {
            "chatgpt_account_id": "acct_123",
            "chatgpt_plan_type": "pro",
        },
    }
)


def _tokens(**overrides) -> OAuthTokens:
    base = {
        "provider": "chatgpt",
        "access_token": "at_1",
        "refresh_token": "rt_1",
        "expires_at": time.time() + 3600,
        "account_id": "acct_123",
        "account_label": "dev@example.com",
        "plan": "pro",
    }
    return OAuthTokens(**{**base, **overrides})


class TestPkceAndAuthUrl:
    def test_challenge_is_derived_from_the_verifier(self):
        import hashlib

        pair = PkcePair.generate()
        expected = (
            base64.urlsafe_b64encode(hashlib.sha256(pair.verifier.encode()).digest())
            .decode()
            .rstrip("=")
        )
        assert pair.challenge == expected
        assert "=" not in pair.challenge

    def test_each_pair_is_unique(self):
        assert PkcePair.generate().verifier != PkcePair.generate().verifier

    def test_authorization_url_carries_the_flow_parameters(self):
        url = ChatGPTOAuthProvider.authorization_url(
            state="st", challenge="ch", redirect_uri="http://localhost:1455/auth/callback"
        )
        assert url.startswith("https://auth.openai.com/oauth/authorize?")
        assert "code_challenge=ch" in url
        assert "code_challenge_method=S256" in url
        assert "state=st" in url
        assert "offline_access" in url

    def test_chatgpt_asks_for_the_account_claim(self):
        # Without this the id_token has no account id and every API call 401s.
        url = ChatGPTOAuthProvider.authorization_url(
            state="st", challenge="ch", redirect_uri=ChatGPTOAuthProvider.loopback_redirect_uri()
        )
        assert "id_token_add_organizations=true" in url

    def test_loopback_redirect_is_the_registered_one(self):
        assert ChatGPTOAuthProvider.loopback_redirect_uri() == "http://localhost:1455/auth/callback"


class TestIdentity:
    def test_reads_account_and_plan_from_the_id_token(self):
        identity = ChatGPTOAuthProvider.identify({"id_token": CHATGPT_ID_TOKEN})
        assert identity == {
            "account_id": "acct_123",
            "account_label": "dev@example.com",
            "plan": "pro",
        }

    def test_falls_back_to_the_access_token(self):
        access = _jwt({"https://api.openai.com/auth": {"chatgpt_account_id": "acct_9"}})
        identity = ChatGPTOAuthProvider.identify(
            {"id_token": _jwt({"email": "a@b.c"}), "access_token": access}
        )
        assert identity["account_id"] == "acct_9"

    def test_unparsable_token_is_not_fatal(self):
        assert decode_jwt_claims("not-a-jwt") == {}
        assert ChatGPTOAuthProvider.identify({"id_token": "garbage"})["account_id"] == ""


class TestTokenExchange:
    @pytest.mark.asyncio
    async def test_exchange_stores_expiry_as_an_absolute_time(self, monkeypatch):
        payload = {
            "access_token": "at_1",
            "refresh_token": "rt_1",
            "id_token": CHATGPT_ID_TOKEN,
            "expires_in": 3600,
        }
        monkeypatch.setattr(
            ChatGPTOAuthProvider, "_post_token", classmethod(lambda cls, data: _async(payload))
        )
        before = time.time()
        tokens = await ChatGPTOAuthProvider.exchange_code(code="c", verifier="v", redirect_uri="r")
        assert tokens.account_id == "acct_123"
        assert tokens.plan == "pro"
        assert before + 3500 < tokens.expires_at < before + 3700

    @pytest.mark.asyncio
    async def test_refresh_keeps_a_non_rotating_refresh_token(self, monkeypatch):
        # The issuer answers without a refresh_token; dropping ours would turn
        # a working login into one that dies at the next expiry.
        monkeypatch.setattr(
            ChatGPTOAuthProvider,
            "_post_token",
            classmethod(lambda cls, data: _async({"access_token": "at_2", "expires_in": 60})),
        )
        refreshed = await ChatGPTOAuthProvider.refresh(_tokens())
        assert refreshed.access_token == "at_2"
        assert refreshed.refresh_token == "rt_1"
        assert refreshed.account_id == "acct_123"

    @pytest.mark.asyncio
    async def test_refresh_without_a_token_is_an_error(self):
        with pytest.raises(OAuthError, match="sign in again"):
            await ChatGPTOAuthProvider.refresh(_tokens(refresh_token=""))

    @pytest.mark.asyncio
    async def test_issuer_error_message_is_surfaced(self, monkeypatch):
        async def _post(*_args, **_kwargs):
            return httpx.Response(
                400, json={"error": "invalid_grant", "error_description": "code expired"}
            )

        monkeypatch.setattr(httpx.AsyncClient, "post", _post)
        with pytest.raises(OAuthError, match="code expired"):
            await ChatGPTOAuthProvider.exchange_code(code="c", verifier="v", redirect_uri="r")


class TestExpiry:
    def test_a_token_close_to_expiry_counts_as_expired(self):
        # Reviews are long; a token with a minute left will die mid-request.
        assert _tokens(expires_at=time.time() + 60).is_expired()
        assert not _tokens(expires_at=time.time() + 3600).is_expired()

    def test_no_advertised_expiry_means_not_expired(self):
        assert not _tokens(expires_at=0).is_expired()


class TestStore:
    def test_round_trip(self, db: AppDatabase):
        store.save(_tokens(), db)
        loaded = store.load("chatgpt", db)
        assert loaded is not None
        assert loaded.access_token == "at_1"
        assert loaded.account_label == "dev@example.com"

    def test_unknown_fields_in_a_stored_blob_are_ignored(self, db: AppDatabase):
        db.set_setting(
            "oauth_credentials:chatgpt",
            json.dumps({"provider": "chatgpt", "access_token": "at", "future_field": 1}),
        )
        loaded = store.load("chatgpt", db)
        assert loaded is not None and loaded.access_token == "at"

    def test_corrupt_blob_reads_as_not_connected(self, db: AppDatabase):
        db.set_setting("oauth_credentials:chatgpt", "{not json")
        assert store.load("chatgpt", db) is None

    def test_disconnect_clears_the_active_pointer(self, db: AppDatabase):
        store.save(_tokens(), db)
        store.set_active_provider("chatgpt", db)
        store.delete("chatgpt", db)
        assert store.load("chatgpt", db) is None
        assert store.get_active_provider(db) == ""

    def test_active_binding_ignores_a_disconnected_provider(self, db: AppDatabase):
        store.set_active_provider("chatgpt", db)
        assert store.active_binding(db) is None

    def test_status_never_leaks_token_material(self, db: AppDatabase):
        store.save(_tokens(), db)
        status = store.status("chatgpt", db)
        assert "at_1" not in json.dumps(status)
        assert "rt_1" not in json.dumps(status)
        assert status["connected"] is True
        assert status["plan"] == "pro"

    @pytest.mark.asyncio
    async def test_valid_tokens_refreshes_and_persists(self, db: AppDatabase, monkeypatch):
        store.save(_tokens(expires_at=time.time() + 10), db)
        monkeypatch.setattr(
            ChatGPTOAuthProvider,
            "refresh",
            classmethod(lambda cls, t: _async(_tokens(access_token="at_2"))),
        )
        fresh = await store.valid_tokens("chatgpt", db)
        assert fresh.access_token == "at_2"
        stored = store.load("chatgpt", db)
        assert stored is not None and stored.access_token == "at_2"

    @pytest.mark.asyncio
    async def test_valid_tokens_without_a_session_explains_itself(self, db: AppDatabase):
        with pytest.raises(OAuthError, match="not connected"):
            await store.valid_tokens("chatgpt", db)


class TestManager:
    def test_start_records_the_attempt(self, db: AppDatabase):
        started = manager.start_login("chatgpt", db=db)
        assert started["manual_exchange"] is True
        assert started["redirect_uri"] == "http://localhost:1455/auth/callback"
        assert started["state"] in json.loads(db.get_setting("oauth_pending_logins"))

    def test_parse_redirect_extracts_code_and_state(self):
        parsed = manager.parse_redirect("http://localhost:1455/auth/callback?code=abc&state=xyz")
        assert parsed == {"code": "abc", "state": "xyz"}

    def test_parse_redirect_surfaces_a_denied_consent(self):
        with pytest.raises(OAuthError, match="not approved"):
            manager.parse_redirect(
                "http://localhost:1455/auth/callback?error=access_denied"
                "&error_description=not%20approved"
            )

    def test_parse_redirect_rejects_a_url_with_no_code(self):
        with pytest.raises(OAuthError, match="no authorization code"):
            manager.parse_redirect("http://localhost:1455/auth/callback")

    @pytest.mark.asyncio
    async def test_complete_consumes_the_attempt(self, db: AppDatabase, monkeypatch):
        started = manager.start_login("chatgpt", db=db)
        monkeypatch.setattr(
            ChatGPTOAuthProvider,
            "exchange_code",
            classmethod(lambda cls, **kw: _async(_tokens())),
        )
        url = f"http://localhost:1455/auth/callback?code=c&state={started['state']}"
        status = await manager.complete_login(redirect_url=url, db=db)
        assert status["connected"] is True
        # Replaying the same redirect must not re-run the exchange: the code is
        # single-use and the issuer has already burned it.
        with pytest.raises(OAuthError, match="expired or was already completed"):
            await manager.complete_login(redirect_url=url, db=db)

    @pytest.mark.asyncio
    async def test_unknown_state_is_rejected(self, db: AppDatabase):
        manager.start_login("chatgpt", db=db)
        with pytest.raises(OAuthError, match="expired or was already completed"):
            await manager.complete_login(code="c", state="not-ours", db=db)

    @pytest.mark.asyncio
    async def test_expired_attempts_are_pruned(self, db: AppDatabase, monkeypatch):
        started = manager.start_login("chatgpt", db=db)
        stale = json.loads(db.get_setting("oauth_pending_logins"))
        stale[started["state"]]["created_at"] = time.time() - manager.PENDING_TTL_SECONDS - 1
        db.set_setting("oauth_pending_logins", json.dumps(stale))
        with pytest.raises(OAuthError, match="expired"):
            await manager.complete_login(code="c", state=started["state"], db=db)

    def test_list_status_covers_every_provider(self, db: AppDatabase):
        state = manager.list_status(db)
        assert {p["id"] for p in state["providers"]} == set(registry.all_providers())
        assert state["active_provider"] == ""


class TestConfigResolution:
    def test_unconnected_provider_falls_back_to_the_api_key_path(self, db: AppDatabase):
        assert resolve_oauth_provider(LLMConfig(oauth_provider="chatgpt"), None) == ""

    def test_unknown_provider_is_ignored(self, db: AppDatabase):
        assert resolve_oauth_provider(LLMConfig(), "not-a-provider") == ""

    def test_db_choice_wins_over_config(self, db: AppDatabase):
        store.save(_tokens(), db)
        assert resolve_oauth_provider(LLMConfig(), "chatgpt") == "chatgpt"

    def test_binding_replaces_endpoint_and_protocol(self):
        bound = apply_oauth_binding(LLMConfig(), "chatgpt", model_is_explicit=True)
        assert bound.oauth_provider == "chatgpt"
        assert bound.base_url == "https://chatgpt.com/backend-api/codex"
        assert bound.api_style == "responses"

    def test_default_model_is_replaced_but_a_chosen_one_is_not(self):
        # The default id is an OpenRouter-style Claude model this endpoint
        # cannot serve; an id somebody typed is theirs to keep.
        inherited = apply_oauth_binding(LLMConfig(), "chatgpt", model_is_explicit=False)
        assert inherited.model == "gpt-5-codex"
        chosen = apply_oauth_binding(LLMConfig(model="gpt-5"), "chatgpt", model_is_explicit=True)
        assert chosen.model == "gpt-5"

    def test_llm_config_for_routes_reviews_through_the_session(self, db: AppDatabase):
        store.save(_tokens(), db)
        db.set_setting("llm_oauth_provider", "chatgpt")
        resolved = llm_config_for("review", LLMConfig())
        assert resolved.oauth_provider == "chatgpt"
        assert resolved.model == "gpt-5-codex"
        assert resolved.api_style == "responses"

    def test_llm_config_for_keeps_a_dashboard_model(self, db: AppDatabase):
        store.save(_tokens(), db)
        db.set_setting("llm_oauth_provider", "chatgpt")
        db.set_setting("review_model", "gpt-5")
        assert llm_config_for("review", LLMConfig()).model == "gpt-5"

    def test_no_session_leaves_the_config_alone(self, db: AppDatabase):
        resolved = llm_config_for("review", LLMConfig())
        assert resolved.oauth_provider is None
        assert resolved.base_url == "https://openrouter.ai/api/v1"

    def test_catalog_offers_the_providers_models(self):
        backend = active_backend(LLMConfig(oauth_provider="chatgpt"))
        assert backend == "oauth:chatgpt"
        options = build_options(
            backend, [dict(m) for m in ChatGPTOAuthProvider.llm.models], "review"
        )
        assert [o["value"] for o in options][0] == "gpt-5-codex"
        assert all("recommended" in o for o in options)


class TestConfigValidation:
    def test_a_typo_fails_at_config_load(self):
        # Left unvalidated this resolves to "no session" and silently puts
        # every review back on the API key.
        with pytest.raises(ValueError, match="not a known provider"):
            LLMConfig(oauth_provider="chatgtp")

    def test_the_id_is_normalised(self):
        assert LLMConfig(oauth_provider="ChatGPT").oauth_provider == "chatgpt"

    def test_empty_means_unset(self):
        assert LLMConfig(oauth_provider="").oauth_provider is None


class TestRoutes:
    def test_providers_lists_connection_state(self, db: AppDatabase):
        from mira.dashboard.routers.oauth import list_oauth_providers

        state = list_oauth_providers(_admin())
        assert [p["id"] for p in state["providers"]] == ["chatgpt"]
        assert state["providers"][0]["connected"] is False

    def test_activating_a_disconnected_provider_is_refused(self, db: AppDatabase):
        from fastapi import HTTPException

        from mira.dashboard.routers.oauth import OAuthActiveRequest, set_active_oauth

        with pytest.raises(HTTPException) as exc:
            set_active_oauth(OAuthActiveRequest(provider="chatgpt"), _admin())
        assert exc.value.status_code == 400
        assert db.get_setting("llm_oauth_provider") in (None, "")

    def test_activating_a_connected_provider_completes_setup(self, db: AppDatabase):
        from mira.dashboard.routers.oauth import OAuthActiveRequest, set_active_oauth

        store.save(_tokens(), db)
        assert set_active_oauth(OAuthActiveRequest(provider="chatgpt"), _admin())["ok"]
        assert db.get_setting("llm_oauth_provider") == "chatgpt"
        assert db.setup_complete

    def test_unknown_provider_is_a_404(self, db: AppDatabase):
        from fastapi import HTTPException

        from mira.dashboard.routers.oauth import disconnect_oauth

        with pytest.raises(HTTPException) as exc:
            disconnect_oauth("nope", _admin())
        assert exc.value.status_code == 404

    def test_non_admins_are_refused(self, db: AppDatabase):
        from fastapi import HTTPException

        from mira.dashboard.routers.oauth import list_oauth_providers

        with pytest.raises(HTTPException) as exc:
            list_oauth_providers(_user())
        assert exc.value.status_code == 403

    @pytest.mark.asyncio
    async def test_models_endpoint_offers_the_accounts_models(self, db: AppDatabase):
        from mira.dashboard.routers.admin import get_models

        store.save(_tokens(), db)
        db.set_setting("llm_oauth_provider", "chatgpt")
        models = await get_models()
        assert models.backend == "oauth:chatgpt"
        assert models.oauth_provider == "chatgpt"
        assert "gpt-5-codex" in [o.value for o in models.review_options]


class TestRequestShaping:
    def test_codex_body_drops_what_the_endpoint_rejects(self):
        body = ChatGPTOAuthProvider.adapt_llm_body(
            {
                "model": "gpt-5-codex",
                "input": [{"role": "user", "content": "hi"}],
                "temperature": 0.2,
                "max_output_tokens": 4096,
            }
        )
        assert body["stream"] is True
        assert body["store"] is False
        assert "temperature" not in body
        assert "max_output_tokens" not in body

    def test_system_message_becomes_instructions(self):
        body = ChatGPTOAuthProvider.adapt_llm_body(
            {
                "input": [
                    {"role": "system", "content": "You review code."},
                    {"role": "user", "content": "diff"},
                ]
            }
        )
        assert body["instructions"] == "You review code."
        assert body["input"] == [{"role": "user", "content": "diff"}]

    def test_reasoning_requests_the_encrypted_trace(self):
        body = ChatGPTOAuthProvider.adapt_llm_body({"input": [], "reasoning": {"effort": "high"}})
        assert body["reasoning"]["summary"] == "auto"
        assert "reasoning.encrypted_content" in body["include"]

    def test_the_original_body_is_not_mutated(self):
        original = {"input": [], "temperature": 0.5}
        ChatGPTOAuthProvider.adapt_llm_body(original)
        assert original["temperature"] == 0.5

    def test_headers_carry_the_account(self):
        headers = ChatGPTOAuthProvider.llm_headers(_tokens())
        assert headers["Authorization"] == "Bearer at_1"
        assert headers["chatgpt-account-id"] == "acct_123"


class TestOAuthProvider:
    def test_factory_picks_the_oauth_provider(self):
        provider = create_llm(LLMConfig(oauth_provider="chatgpt"))
        assert isinstance(provider, OAuthResponsesProvider)

    def test_reasoning_effort_map_comes_from_the_spec(self):
        # "max" has no equivalent on this backend and must land on "high"
        # rather than being sent through as an unknown level.
        provider = create_llm(LLMConfig(oauth_provider="chatgpt", reasoning_effort="max"))
        body: dict = {"model": "gpt-5-codex"}
        provider._apply_reasoning(body)
        assert body["reasoning"] == {"effort": "high"}

    def test_headers_without_a_session_explain_what_to_do(self, db: AppDatabase):
        provider = create_llm(LLMConfig(oauth_provider="chatgpt"))
        with pytest.raises(LLMError) as exc:
            provider._build_headers()
        assert exc.value.code == "oauth_not_connected"

    @pytest.mark.asyncio
    async def test_a_missing_session_is_reported_before_the_call(self, db: AppDatabase):
        provider = create_llm(LLMConfig(oauth_provider="chatgpt"))
        with pytest.raises(LLMError) as exc:
            await provider._ensure_token()
        assert exc.value.code == "oauth_not_connected"


class TestStreamCollection:
    @pytest.mark.asyncio
    async def test_completed_event_is_the_response(self):
        resp = _sse(
            [
                {"type": "response.created", "response": {"id": "r1"}},
                {"type": "response.output_text.delta", "delta": "hel"},
                {
                    "type": "response.completed",
                    "response": {"id": "r1", "output": [], "usage": {"input_tokens": 5}},
                },
            ]
        )
        payload = await _collect_stream(resp, "ChatGPT")
        assert payload["usage"] == {"input_tokens": 5}

    @pytest.mark.asyncio
    async def test_a_failure_event_raises_with_its_message(self):
        resp = _sse(
            [{"type": "response.failed", "response": {"error": {"message": "rate limited"}}}]
        )
        with pytest.raises(LLMError, match="rate limited"):
            await _collect_stream(resp, "ChatGPT")

    @pytest.mark.asyncio
    async def test_a_truncated_stream_still_yields_what_arrived(self):
        # The model's answer is in hand; failing on a missing terminator would
        # throw away a whole review pass over a dropped event.
        resp = _sse([{"type": "response.in_progress", "response": {"id": "r1", "output": []}}])
        assert (await _collect_stream(resp, "ChatGPT"))["id"] == "r1"

    @pytest.mark.asyncio
    async def test_an_empty_stream_is_an_error(self):
        with pytest.raises(LLMError, match="no response events"):
            await _collect_stream(_sse([]), "ChatGPT")

    @pytest.mark.asyncio
    async def test_unparsable_lines_are_skipped(self):
        resp = httpx.Response(
            200,
            content=(
                b": keep-alive\n"
                b"data: not-json\n"
                b'data: {"type": "response.completed", "response": {"id": "r2"}}\n'
                b"data: [DONE]\n"
            ),
        )
        assert (await _collect_stream(resp, "ChatGPT"))["id"] == "r2"


# ── helpers ─────────────────────────────────────────────────────────


def _admin():
    from types import SimpleNamespace

    return SimpleNamespace(state=SimpleNamespace(user=SimpleNamespace(is_admin=True)))


def _user():
    from types import SimpleNamespace

    return SimpleNamespace(state=SimpleNamespace(user=SimpleNamespace(is_admin=False)))


def _async(value):
    """Wrap a value in an awaitable, for patching async classmethods."""

    async def _coro():
        return value

    return _coro()


def _sse(events: list[dict]) -> httpx.Response:
    body = "".join(f"data: {json.dumps(e)}\n\n" for e in events)
    return httpx.Response(200, content=body.encode())
