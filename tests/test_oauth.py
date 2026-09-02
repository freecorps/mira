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


@pytest.fixture
def unused_port() -> int:
    """A free localhost port, so the listener tests never fight for 1455."""
    import socket

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


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
        loaded = store.load("chatgpt", "acct_123", db)
        assert loaded is not None
        assert loaded.access_token == "at_1"
        assert loaded.account_label == "dev@example.com"
        assert loaded.account_key == "acct_123"
        # With one account, asking for "any" finds it too.
        assert store.load("chatgpt", db=db) is not None

    def test_unknown_fields_in_a_stored_blob_are_ignored(self, db: AppDatabase):
        db.set_setting(
            "oauth_credentials:chatgpt:k1",
            json.dumps({"provider": "chatgpt", "access_token": "at", "future_field": 1}),
        )
        loaded = store.load("chatgpt", "k1", db)
        assert loaded is not None and loaded.access_token == "at"

    def test_a_row_from_the_previous_build_is_moved_into_a_slot(self, db: AppDatabase):
        # The previous build stored one account per provider under a bare
        # key. It is read once, moved into its slot, and never left behind
        # where a downgrade could resurrect a disconnected account.
        db.set_setting("oauth_credentials:chatgpt", json.dumps(_tokens().to_dict()))
        found = store.accounts("chatgpt", db)
        assert list(found) == ["acct_123"]
        assert db.get_setting("oauth_credentials:chatgpt") is None
        assert db.get_setting("oauth_credentials:chatgpt:acct_123")

    def test_corrupt_blob_reads_as_not_connected(self, db: AppDatabase):
        db.set_setting("oauth_credentials:chatgpt:k1", "{not json")
        assert store.load("chatgpt", "k1", db) is None

    def test_disconnect_clears_the_active_pointer(self, db: AppDatabase):
        store.save(_tokens(), db)
        store.set_active_provider("chatgpt", db)
        store.delete("chatgpt", "acct_123", db)
        assert store.load("chatgpt", "acct_123", db) is None
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
        assert status["accounts"][0]["plan"] == "pro"
        assert status["accounts"][0]["key"] == "acct_123"
        assert status["protocol"]["api_style"] == "responses"

    @pytest.mark.asyncio
    async def test_valid_tokens_refreshes_and_persists(self, db: AppDatabase, monkeypatch):
        store.save(_tokens(expires_at=time.time() + 10), db)
        monkeypatch.setattr(
            ChatGPTOAuthProvider,
            "refresh",
            classmethod(lambda cls, t: _async(_tokens(access_token="at_2"))),
        )
        fresh = await store.valid_tokens("chatgpt", db=db)
        assert fresh.access_token == "at_2"
        stored = store.load("chatgpt", "acct_123", db)
        assert stored is not None and stored.access_token == "at_2"

    @pytest.mark.asyncio
    async def test_valid_tokens_without_a_session_explains_itself(self, db: AppDatabase):
        with pytest.raises(OAuthError, match="not connected"):
            await store.valid_tokens("chatgpt", db=db)

    @pytest.mark.asyncio
    async def test_force_renews_a_token_that_has_not_expired(self, db, monkeypatch):
        # The 401 path: the endpoint has said the token is dead, which its
        # expiry cannot say. Without `force` the store hands back the same one
        # and the retry repeats the rejected request.
        store.save(_tokens(expires_at=time.time() + 3600), db)
        monkeypatch.setattr(
            ChatGPTOAuthProvider,
            "refresh",
            classmethod(lambda cls, t: _async(_tokens(access_token="at_2"))),
        )
        assert (await store.valid_tokens("chatgpt", db=db)).access_token == "at_1"
        assert (await store.valid_tokens("chatgpt", db=db, force=True)).access_token == "at_2"

    @pytest.mark.asyncio
    async def test_a_grant_rotated_elsewhere_is_picked_up(self, db, monkeypatch):
        # The refresh lock is per-process. Another worker rotating this grant
        # leaves ours refused — but its replacement is already in the store, so
        # a refusal must not be reported as a lost session.
        store.save(_tokens(expires_at=time.time() + 10), db)

        def _rotated(cls, tokens):
            store.save(_tokens(access_token="at_elsewhere"), db)
            raise OAuthError("invalid_grant")

        monkeypatch.setattr(ChatGPTOAuthProvider, "refresh", classmethod(_rotated))
        fresh = await store.valid_tokens("chatgpt", db=db)
        assert fresh.access_token == "at_elsewhere"

    @pytest.mark.asyncio
    async def test_a_genuinely_dead_grant_still_raises(self, db, monkeypatch):
        store.save(_tokens(expires_at=time.time() + 10), db)

        def _dead(cls, tokens):
            raise OAuthError("invalid_grant")

        monkeypatch.setattr(ChatGPTOAuthProvider, "refresh", classmethod(_dead))
        with pytest.raises(OAuthError, match="invalid_grant"):
            await store.valid_tokens("chatgpt", db=db)


class TestSettingsStore:
    """The settings primitives the OAuth package relies on."""

    def test_take_returns_the_value_and_removes_it(self, db: AppDatabase):
        db.set_setting("k", "v")
        assert db.take_setting("k") == "v"
        assert db.get_setting("k") is None

    def test_taking_a_missing_key_is_none(self, db: AppDatabase):
        assert db.take_setting("nope") is None

    def test_list_settings_matches_only_the_prefix(self, db: AppDatabase):
        db.set_setting("oauth_pending:a", "1")
        db.set_setting("oauth_pending:b", "2")
        db.set_setting("other", "3")
        assert db.list_settings("oauth_pending:") == {
            "oauth_pending:a": "1",
            "oauth_pending:b": "2",
        }

    def test_list_settings_does_not_treat_the_prefix_as_a_pattern(self, db: AppDatabase):
        # `_` is a single-character wildcard in LIKE; unescaped, "a_b" would
        # also match "axb".
        db.set_setting("a_b", "1")
        db.set_setting("axb", "2")
        assert db.list_settings("a_b") == {"a_b": "1"}


class TestManager:
    def test_start_records_the_attempt(self, db: AppDatabase):
        started = manager.start_login("chatgpt", db=db)
        assert started["manual_exchange"] is True
        assert started["redirect_uri"] == "http://localhost:1455/auth/callback"
        assert db.get_setting(f"oauth_pending:{started['state']}")

    def test_concurrent_logins_do_not_evict_each_other(self, db: AppDatabase):
        # One row per attempt: with a shared JSON document, whichever login
        # was started first would be dropped by the second one's write and its
        # redirect — already approved by the user — would be unredeemable.
        first = manager.start_login("chatgpt", db=db)
        second = manager.start_login("chatgpt", db=db)
        assert db.get_setting(f"oauth_pending:{first['state']}")
        assert db.get_setting(f"oauth_pending:{second['state']}")

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
    async def test_an_expired_attempt_is_refused(self, db: AppDatabase):
        started = manager.start_login("chatgpt", db=db)
        _age_attempt(db, started["state"])
        with pytest.raises(OAuthError, match="expired"):
            await manager.complete_login(code="c", state=started["state"], db=db)

    def test_abandoned_attempts_are_pruned_on_the_next_start(self, db: AppDatabase):
        # Otherwise every login anybody walked away from leaves its verifier in
        # the database indefinitely.
        stale = manager.start_login("chatgpt", db=db)
        _age_attempt(db, stale["state"])
        manager.start_login("chatgpt", db=db)
        assert not db.get_setting(f"oauth_pending:{stale['state']}")

    @pytest.mark.asyncio
    async def test_a_redirect_from_another_login_is_refused(self, db: AppDatabase):
        # The dashboard passes the state it started. Letting that stand in for
        # the one in the pasted URL is exactly the mismatch the state parameter
        # exists to catch — and would burn the attempt on screen.
        mine = manager.start_login("chatgpt", db=db)
        theirs = manager.start_login("chatgpt", db=db)
        url = f"http://localhost:1455/auth/callback?code=c&state={theirs['state']}"
        with pytest.raises(OAuthError, match="different sign-in"):
            await manager.complete_login(redirect_url=url, state=mine["state"], db=db)
        # Refused, not consumed: the attempt on screen is still redeemable.
        assert db.get_setting(f"oauth_pending:{mine['state']}")

    @pytest.mark.asyncio
    async def test_a_redirect_without_state_is_refused(self, db: AppDatabase):
        # The state is the only evidence a pasted URL came back from the login
        # we started. Accepting one that carries none — and standing the
        # caller's expectation in for it — proves nothing and spends the
        # pending attempt doing it.
        mine = manager.start_login("chatgpt", db=db)
        with pytest.raises(OAuthError, match="carries no state"):
            await manager.complete_login(
                redirect_url="http://localhost:1455/auth/callback?code=c",
                state=mine["state"],
                db=db,
            )
        assert db.get_setting(f"oauth_pending:{mine['state']}")

    @pytest.mark.asyncio
    async def test_a_stateless_redirect_is_refused_with_no_expectation_either(self, db):
        manager.start_login("chatgpt", db=db)
        with pytest.raises(OAuthError, match="carries no state"):
            await manager.complete_login(
                redirect_url="http://localhost:1455/auth/callback?code=c", db=db
            )

    @pytest.mark.asyncio
    async def test_the_redirects_code_wins_over_a_supplied_one(self, db, monkeypatch):
        # The redirect is what came back from the provider; a code passed
        # alongside it must not override what it actually carries.
        started = manager.start_login("chatgpt", db=db)
        seen: dict = {}

        def _exchange(cls, **kw):
            seen.update(kw)
            return _async(_tokens())

        monkeypatch.setattr(ChatGPTOAuthProvider, "exchange_code", classmethod(_exchange))
        await manager.complete_login(
            redirect_url=f"http://localhost:1455/auth/callback?code=real&state={started['state']}",
            code="other",
            state=started["state"],
            db=db,
        )
        assert seen["code"] == "real"

    @pytest.mark.asyncio
    async def test_code_and_state_without_a_redirect_still_works(self, db, monkeypatch):
        # The dashboard-callback path: the state arrives as its own request
        # parameter rather than inside a URL somebody pasted.
        started = manager.start_login("chatgpt", db=db)
        monkeypatch.setattr(
            ChatGPTOAuthProvider,
            "exchange_code",
            classmethod(lambda cls, **kw: _async(_tokens())),
        )
        status = await manager.complete_login(code="c", state=started["state"], db=db)
        assert status["connected"] is True

    def test_an_attempt_can_only_be_claimed_once(self, db: AppDatabase):
        # Two workers handling the same callback must not both come away with
        # the verifier and both go redeem the single-use code.
        started = manager.start_login("chatgpt", db=db)
        first = manager._take_attempt(db, started["state"])
        second = manager._take_attempt(db, started["state"])
        assert first is not None
        assert second is None

    def test_a_dashboard_provider_needs_a_configured_origin(self, db, monkeypatch):
        # Where a code gets delivered is deployment configuration. A provider
        # that redirects to us cannot start without one.
        monkeypatch.setattr(ChatGPTOAuthProvider, "redirect_mode", "dashboard")
        with pytest.raises(OAuthError, match="MIRA_DASHBOARD_URL"):
            manager.start_login("chatgpt", dashboard_origin="", db=db)
        with pytest.raises(OAuthError, match="MIRA_DASHBOARD_URL"):
            manager.start_login("chatgpt", dashboard_origin="mira.example.com", db=db)

    def test_a_configured_origin_becomes_the_callback(self, db, monkeypatch):
        monkeypatch.setattr(ChatGPTOAuthProvider, "redirect_mode", "dashboard")
        started = manager.start_login(
            "chatgpt", dashboard_origin="https://mira.example.com/", db=db
        )
        assert started["redirect_uri"] == "https://mira.example.com/api/oauth/callback"
        assert started["manual_exchange"] is False

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
        assert inherited.model == "gpt-5.6-sol"
        chosen = apply_oauth_binding(LLMConfig(model="gpt-5"), "chatgpt", model_is_explicit=True)
        assert chosen.model == "gpt-5"

    def test_llm_config_for_routes_reviews_through_the_session(self, db: AppDatabase):
        store.save(_tokens(), db)
        db.set_setting("llm_oauth_provider", "chatgpt")
        resolved = llm_config_for("review", LLMConfig())
        assert resolved.oauth_provider == "chatgpt"
        assert resolved.model == "gpt-5.6-sol"
        assert resolved.api_style == "responses"

    def test_a_foreign_model_id_is_not_sent_to_the_endpoint(self):
        # Indexing and the security sweep are usually pinned to a cheap model
        # in mira.yaml. Those ids belong to another backend and this endpoint
        # has never served one.
        bound = apply_oauth_binding(
            LLMConfig(model="anthropic/claude-haiku-4-5"), "chatgpt", model_is_explicit=True
        )
        assert bound.model == "gpt-5.6-sol"

    def test_an_unfamiliar_bare_id_is_left_alone(self):
        # A model released after this build is the user's call, not ours.
        bound = apply_oauth_binding(
            LLMConfig(model="gpt-5.2-codex-max"), "chatgpt", model_is_explicit=True
        )
        assert bound.model == "gpt-5.2-codex-max"

    def test_indexing_does_not_inherit_a_model_the_endpoint_cannot_serve(self, db: AppDatabase):
        store.save(_tokens(), db)
        db.set_setting("llm_oauth_provider", "chatgpt")
        db.set_setting("indexing_model", "anthropic/claude-haiku-4-5")
        resolved = llm_config_for("indexing", LLMConfig())
        assert resolved.model == "gpt-5.6-sol"
        assert resolved.base_url == "https://chatgpt.com/backend-api/codex"

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
        assert [o["value"] for o in options][0] == "gpt-5.6-sol"
        assert all("recommended" in o for o in options)


class TestLoopbackListener:
    """The CLI's localhost listener, driven over a real socket."""

    def _serve(self, port: int, timeout: float = 5.0):
        import threading
        from concurrent.futures import ThreadPoolExecutor

        from mira.oauth.loopback import _serve_once

        ready = threading.Event()
        pool = ThreadPoolExecutor(max_workers=1)
        pending = pool.submit(_serve_once, port, "/auth/callback", timeout, ready=ready)
        # A connection made before the bind is refused, not served — the
        # listener runs on its own thread, so wait for it to be listening.
        if not ready.wait(timeout=5):
            pending.result(timeout=0)  # surfaces the bind failure, if that is why
            raise AssertionError("the listener never bound its port")
        return pool, pending

    def _get(self, port: int, path: str) -> None:
        import contextlib
        import urllib.error
        import urllib.request

        # A 404 for the wrong path is the point of the test, not a failure.
        with contextlib.suppress(urllib.error.HTTPError):
            urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=5).read()

    def test_a_stray_request_does_not_consume_the_listener(self, unused_port: int):
        # A browser aims more than the redirect at a port it has open. Serving
        # exactly one request means a favicon probe fails a login the user has
        # already approved.
        pool, pending = self._serve(unused_port)
        try:
            self._get(unused_port, "/favicon.ico")
            self._get(unused_port, "/auth/callback?code=abc&state=xyz")
            assert pending.result(timeout=10) == {"code": "abc", "state": "xyz"}
        finally:
            pool.shutdown(wait=True)

    def test_a_silent_client_cannot_hold_the_listener(self, unused_port, monkeypatch):
        # HTTPServer.timeout bounds the wait for a *connection*; a client that
        # connects and then says nothing is a different hang, and connections
        # are served one at a time — so without a read timeout on the accepted
        # socket that one client holds the login open forever.
        import socket

        from mira.oauth.loopback import _CallbackHandler

        monkeypatch.setattr(_CallbackHandler, "timeout", 0.5)
        pool, pending = self._serve(unused_port)
        try:
            with socket.create_connection(("127.0.0.1", unused_port), timeout=5):
                self._get(unused_port, "/auth/callback?code=abc&state=xyz")
                assert pending.result(timeout=20) == {"code": "abc", "state": "xyz"}
        finally:
            pool.shutdown(wait=True)

    def test_it_gives_up_when_nobody_comes_back(self, unused_port: int):
        pool, pending = self._serve(unused_port, timeout=0.3)
        try:
            assert pending.result(timeout=10) == {}
        finally:
            pool.shutdown(wait=True)


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

    def test_start_ignores_any_origin_the_caller_offers(self, db, monkeypatch):
        # The request must not be able to name where the code is delivered.
        from mira.dashboard.routers.oauth import start_oauth

        monkeypatch.setattr(ChatGPTOAuthProvider, "redirect_mode", "dashboard")
        monkeypatch.setenv("MIRA_DASHBOARD_URL", "https://mira.example.com")
        request = _admin()
        request.base_url = "https://attacker.example/"
        started = start_oauth("chatgpt", request)
        assert started["redirect_uri"] == "https://mira.example.com/api/oauth/callback"

    @pytest.mark.asyncio
    async def test_manual_refresh_goes_to_the_issuer(self, db, monkeypatch):
        # The button exists for the case an expiry cannot describe: a grant
        # revoked upstream that still looks valid here.
        from mira.dashboard.routers.oauth import refresh_oauth_account

        store.save(_tokens(expires_at=time.time() + 3600), db)
        monkeypatch.setattr(
            ChatGPTOAuthProvider,
            "refresh",
            classmethod(lambda cls, t: _async(_tokens(access_token="at_renewed"))),
        )
        await refresh_oauth_account("chatgpt", "acct_123", _admin())
        stored = store.load("chatgpt", "acct_123", db)
        assert stored is not None and stored.access_token == "at_renewed"

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
        assert models.default_backend["provider"] == "chatgpt"
        values = [o.value for o in models.review_options]
        # The bare id (default backend), the same account named explicitly,
        # and the API key's own list kept reachable under its prefix.
        assert "gpt-5.6-sol" in values
        assert "oauth:chatgpt:acct_123:gpt-5.6-sol" in values
        assert any(v.startswith("api:") for v in values)
        groups = {o.group for o in models.review_options}
        assert any(g.startswith("Default — ChatGPT (Codex)") for g in groups)
        assert any(g.startswith("API key — ") for g in groups)

    @pytest.mark.asyncio
    async def test_models_endpoint_reports_what_calls_will_carry(self, db: AppDatabase):
        # The page must not name a model next to a catalog that cannot serve
        # it: the mismatch is invisible until a review fails on that id.
        from mira.dashboard.routers.admin import get_models

        store.save(_tokens(), db)
        db.set_setting("llm_oauth_provider", "chatgpt")
        models = await get_models()
        offered = {o.value for o in models.review_options}
        for reported in (models.review_model, models.indexing_model, models.security_model):
            assert reported in offered
        assert models.config_review_model in offered
        assert llm_config_for("review", LLMConfig()).model == models.review_model

    @pytest.mark.asyncio
    async def test_the_callback_page_escapes_what_it_renders(self, db: AppDatabase):
        # `error` is a query parameter: whoever crafts the link the admin
        # follows writes it, and the admin is the one account that can change
        # these settings.
        from mira.dashboard.routers.oauth import oauth_callback

        page = await oauth_callback(_admin(), error="<script>alert(1)</script>")
        body = page.body.decode()
        assert "<script>alert(1)</script>" not in body
        assert "&lt;script&gt;" in body

    @pytest.mark.asyncio
    async def test_the_callback_page_escapes_the_account_label(self, db, monkeypatch):
        from mira.dashboard.routers.oauth import oauth_callback

        started = manager.start_login("chatgpt", db=db)
        monkeypatch.setattr(
            ChatGPTOAuthProvider,
            "exchange_code",
            classmethod(
                lambda cls, **kw: _async(_tokens(account_label="<img src=x onerror=alert(1)>"))
            ),
        )
        page = await oauth_callback(_admin(), code="c", state=started["state"])
        assert "<img src=x" not in page.body.decode()


class TestRequestShaping:
    def test_codex_body_drops_what_the_endpoint_rejects(self):
        body = ChatGPTOAuthProvider.adapt_llm_body(
            {
                "model": "gpt-5.6-sol",
                "input": [{"role": "user", "content": "hi"}],
                "temperature": 0.2,
                "max_output_tokens": 4096,
            }
        )
        assert body["stream"] is True
        assert body["store"] is False
        assert "temperature" not in body
        assert "max_output_tokens" not in body

    def test_system_message_stays_in_the_input(self):
        # The backend takes it there, and JSON mode needs it there: the
        # "some input message must say json" rule ignores `instructions`.
        messages = [
            {"role": "system", "content": "You review code. Answer in JSON."},
            {"role": "user", "content": "diff"},
        ]
        body = ChatGPTOAuthProvider.adapt_llm_body({"input": messages})
        assert body["input"] == messages
        assert "instructions" not in body

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

    def test_the_endpoint_is_the_specs_whatever_base_url_says(self):
        # A config that names the provider but kept the API-key endpoint
        # (mira.yaml with only `oauth_provider` set, or a test config) must
        # not send a ChatGPT token to OpenRouter.
        provider = create_llm(
            LLMConfig(oauth_provider="chatgpt", base_url="https://openrouter.ai/api/v1")
        )
        assert provider._url == "https://chatgpt.com/backend-api/codex/responses"

    def test_reasoning_effort_map_comes_from_the_spec(self, monkeypatch):
        # A model the backend has not described yet goes through the static
        # map: "max" means "as deep as it goes", which on this backend is
        # "xhigh" for every model it serves.
        from mira.oauth import chatgpt

        monkeypatch.setattr(chatgpt, "_model_levels", {})
        provider = create_llm(LLMConfig(oauth_provider="chatgpt", reasoning_effort="max"))
        body: dict = {"model": "gpt-5.6-sol"}
        provider._apply_reasoning(body)
        assert body["reasoning"] == {"effort": "xhigh"}
        assert "temperature" not in body

    def test_reasoning_effort_is_clamped_to_what_the_model_takes(self, monkeypatch):
        # The backend serves several generations at once; "max" is a real
        # level on one and a 400 on another. The list it gave us decides.
        from mira.oauth import chatgpt

        monkeypatch.setattr(
            chatgpt,
            "_model_levels",
            {
                "gpt-5.6-sol": ("low", "medium", "high", "xhigh", "max", "ultra"),
                "gpt-5.5": ("low", "medium", "high", "xhigh"),
                "tiny": ("medium",),
            },
        )
        spec = ChatGPTOAuthProvider
        assert spec.reasoning_effort("gpt-5.6-sol", "max") == "max"
        assert spec.reasoning_effort("gpt-5.5", "max") == "xhigh"
        assert spec.reasoning_effort("gpt-5.5", "high") == "high"
        # Below the model's floor: the lowest it has, not a level it lacks.
        assert spec.reasoning_effort("tiny", "low") == "medium"
        # A level outside the known scale is not second-guessed.
        assert spec.reasoning_effort("gpt-5.5", "ultra") == "ultra"
        # Unknown model: the static map.
        assert spec.reasoning_effort("gpt-9", "max") == "xhigh"

        provider = create_llm(LLMConfig(oauth_provider="chatgpt", reasoning_effort="max"))
        body: dict = {"model": "gpt-5.6-sol", "temperature": 0.1}
        provider._apply_reasoning(body)
        assert body["reasoning"] == {"effort": "max"}

    def test_the_model_list_teaches_the_levels(self, monkeypatch):
        # Hidden models are kept out of the picker but an operator can still
        # type one, so its levels are remembered all the same.
        from mira.oauth import chatgpt

        monkeypatch.setattr(chatgpt, "_model_levels", {})
        payload = {
            "models": [
                {
                    "slug": "gpt-5.6-sol",
                    "visibility": "list",
                    "supported_reasoning_levels": [{"effort": "low"}, {"effort": "max"}],
                },
                {
                    "slug": "gpt-reserve",
                    "visibility": "hide",
                    "supported_reasoning_levels": [{"effort": "medium"}],
                },
            ]
        }
        chatgpt.remember_reasoning_levels(payload)
        assert ChatGPTOAuthProvider.reasoning_levels("gpt-5.6-sol") == ("low", "max")
        assert ChatGPTOAuthProvider.reasoning_levels("gpt-reserve") == ("medium",)
        assert [m["value"] for m in chatgpt.models_from_payload(payload)] == ["gpt-5.6-sol"]

    def test_the_encrypted_reasoning_trace_is_always_requested(self):
        # With nothing stored server-side, a reasoning item handed back on
        # the next turn is only usable if it carries its content — and the
        # model reasons whether or not an effort was asked for.
        body = ChatGPTOAuthProvider.adapt_llm_body({"model": "gpt-5.6-sol", "input": []})
        assert body["include"] == ["reasoning.encrypted_content"]
        assert "reasoning" not in body

    def test_headers_without_a_session_explain_what_to_do(self, db: AppDatabase):
        provider = create_llm(LLMConfig(oauth_provider="chatgpt"))
        with pytest.raises(LLMError) as exc:
            provider._build_headers()
        assert exc.value.code == "oauth_not_connected"

    @pytest.mark.asyncio
    async def test_a_401_retry_renews_rather_than_replaying(self, db, monkeypatch):
        # Dropping only the cached copy would reload the same rejected token
        # from the store and send the identical request again.
        store.save(_tokens(expires_at=time.time() + 3600), db)
        provider = create_llm(LLMConfig(oauth_provider="chatgpt"))
        monkeypatch.setattr(
            ChatGPTOAuthProvider,
            "refresh",
            classmethod(lambda cls, t: _async(_tokens(access_token="at_2"))),
        )
        await provider._ensure_token()
        assert provider._build_headers()["Authorization"] == "Bearer at_1"
        await provider._ensure_token(force_refresh=True)
        assert provider._build_headers()["Authorization"] == "Bearer at_2"

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
    async def test_items_are_stitched_from_their_done_events(self):
        # The Codex backend's completion event carries ``output: []``; the
        # function call the whole review hangs on is only ever delivered in
        # its own ``output_item.done`` event. Reading just the completion
        # object made every tool call look like an empty answer.
        resp = _sse(
            [
                {"type": "response.created", "response": {"id": "r1", "output": []}},
                {"type": "response.output_item.added", "output_index": 0, "item": {}},
                {
                    "type": "response.output_item.done",
                    "output_index": 1,
                    "item": {
                        "type": "function_call",
                        "id": "fc_1",
                        "call_id": "call_1",
                        "name": "submit_review",
                        "arguments": '{"comments": []}',
                    },
                },
                {
                    "type": "response.output_item.done",
                    "output_index": 0,
                    "item": {"type": "reasoning", "id": "rs_1", "encrypted_content": "x"},
                },
                {
                    "type": "response.completed",
                    "response": {"id": "r1", "output": [], "usage": {"input_tokens": 5}},
                },
            ]
        )
        payload = await _collect_stream(resp, "ChatGPT")
        assert payload["id"] == "r1"
        assert payload["usage"] == {"input_tokens": 5}
        assert [i["type"] for i in payload["output"]] == ["reasoning", "function_call"]
        assert payload["output"][1]["arguments"] == '{"comments": []}'

    @pytest.mark.asyncio
    async def test_a_completion_that_carries_its_output_is_left_alone(self):
        resp = _sse(
            [
                {
                    "type": "response.output_item.done",
                    "output_index": 0,
                    "item": {"type": "message", "role": "assistant", "content": []},
                },
                {
                    "type": "response.completed",
                    "response": {"id": "r1", "output": [{"type": "message", "id": "m1"}]},
                },
            ]
        )
        payload = await _collect_stream(resp, "ChatGPT")
        assert payload["output"] == [{"type": "message", "id": "m1"}]

    @pytest.mark.asyncio
    async def test_text_deltas_stand_in_when_no_item_arrived(self):
        resp = _sse(
            [
                {"type": "response.output_text.delta", "delta": "hel"},
                {"type": "response.output_text.delta", "delta": "lo"},
                {"type": "response.completed", "response": {"id": "r1", "output": []}},
            ]
        )
        payload = await _collect_stream(resp, "ChatGPT")
        assert payload["output"][0]["content"][0]["text"] == "hello"

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


def _age_attempt(db: AppDatabase, state: str) -> None:
    """Backdate a pending attempt past its TTL."""
    key = f"oauth_pending:{state}"
    attempt = json.loads(db.get_setting(key))
    attempt["created_at"] = time.time() - manager.PENDING_TTL_SECONDS - 1
    db.set_setting(key, json.dumps(attempt))


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
