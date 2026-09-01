"""Generic OAuth 2.0 (PKCE) plumbing shared by every OAuth login backend.

A provider is an :class:`OAuthProviderSpec` — a mostly-declarative object
describing where to send the user, where to redeem the code, and (optionally)
how the resulting grant is used to talk to an LLM endpoint. Everything that is
the *same* for every provider — the PKCE pair, the authorization URL, the
form-encoded token/refresh calls, expiry bookkeeping — lives here, so adding a
provider is a subclass with a handful of constants rather than a second copy of
the flow.

Nothing in this module touches the database or the dashboard: persistence is
:mod:`mira.oauth.store` and the login state machine is :mod:`mira.oauth.manager`.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import secrets
import time
from dataclasses import dataclass, field
from typing import Any, ClassVar
from urllib.parse import urlencode

import httpx

logger = logging.getLogger(__name__)

# Refresh this long before the token actually expires. A review is a long
# request; handing it a token with 40 seconds left is how you get a 401
# halfway through a pass that already cost money.
REFRESH_SKEW_SECONDS = 300.0

_TOKEN_TIMEOUT = 30.0


class OAuthError(Exception):
    """A login or refresh failed. The message is safe to show in the dashboard."""


# ── PKCE ────────────────────────────────────────────────────────────


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


@dataclass(frozen=True)
class PkcePair:
    """A PKCE verifier and its S256 challenge."""

    verifier: str
    challenge: str

    @classmethod
    def generate(cls) -> PkcePair:
        verifier = _b64url(secrets.token_bytes(64))
        challenge = _b64url(hashlib.sha256(verifier.encode()).digest())
        return cls(verifier=verifier, challenge=challenge)


def new_state() -> str:
    """An unguessable ``state`` value for one login attempt."""
    return _b64url(secrets.token_bytes(32))


# ── Tokens ──────────────────────────────────────────────────────────


@dataclass
class OAuthTokens:
    """A stored grant: what we got back, plus who it belongs to.

    ``expires_at`` is an absolute unix timestamp (not the ``expires_in`` the
    server returns) so a grant read back from the database days later is judged
    against the clock rather than against process start.
    """

    provider: str
    access_token: str
    refresh_token: str = ""
    id_token: str = ""
    token_type: str = "Bearer"
    expires_at: float = 0.0
    scope: str = ""
    # Identity, for the dashboard to show and for providers that need to send
    # the account on every request (ChatGPT does).
    account_id: str = ""
    account_label: str = ""
    plan: str = ""
    obtained_at: float = field(default_factory=time.time)

    def is_expired(self, skew: float = REFRESH_SKEW_SECONDS) -> bool:
        """True when the access token is gone or about to be."""
        if not self.expires_at:
            return False  # No expiry advertised — assume it lives until a 401.
        return time.time() >= (self.expires_at - skew)

    def to_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "access_token": self.access_token,
            "refresh_token": self.refresh_token,
            "id_token": self.id_token,
            "token_type": self.token_type,
            "expires_at": self.expires_at,
            "scope": self.scope,
            "account_id": self.account_id,
            "account_label": self.account_label,
            "plan": self.plan,
            "obtained_at": self.obtained_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> OAuthTokens:
        """Rebuild from a stored blob, ignoring keys we no longer read.

        Forward compatibility matters here: a downgrade must not wipe a working
        grant just because a newer build stored an extra field.
        """
        known = set(cls.__dataclass_fields__)
        return cls(**{k: v for k, v in data.items() if k in known})


def decode_jwt_claims(token: str) -> dict[str, Any]:
    """Read a JWT's payload without verifying it.

    We are the client that just received this token over TLS from the endpoint
    we asked; the claims are read for display and for routing (account id),
    never as an authorization decision. Malformed input returns ``{}`` rather
    than raising — a token we cannot parse is not a reason to fail a login that
    otherwise worked.
    """
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        claims = json.loads(base64.urlsafe_b64decode(payload))
        return claims if isinstance(claims, dict) else {}
    except Exception:
        return {}


# ── Provider spec ───────────────────────────────────────────────────


@dataclass(frozen=True)
class LLMBinding:
    """How a provider's grant is used as an LLM backend.

    ``None`` on a spec means the provider is a login only — the OAuth layer is
    generic, and not every future provider will serve models.
    """

    base_url: str
    api_style: str = "responses"
    default_model: str = ""
    models: tuple[dict[str, Any], ...] = ()
    # Maps our thinking-mode values onto whatever the endpoint accepts,
    # mirroring ``provider_profiles``' ``reasoning_effort_map``.
    reasoning_effort_map: dict[str, str] = field(default_factory=dict)


class OAuthProviderSpec:
    """One OAuth backend. Subclass and fill in the constants.

    The defaults describe a standard authorization-code + PKCE provider.
    Override the hooks only where a provider deviates.
    """

    id: ClassVar[str] = ""
    label: ClassVar[str] = ""
    description: ClassVar[str] = ""
    docs_url: ClassVar[str] = ""

    authorize_url: ClassVar[str] = ""
    token_url: ClassVar[str] = ""
    client_id: ClassVar[str] = ""
    scopes: ClassVar[tuple[str, ...]] = ()
    # Scopes sent on refresh — some issuers reject `offline_access` there.
    refresh_scopes: ClassVar[tuple[str, ...]] = ()

    # "loopback": the provider only accepts a fixed localhost redirect, so a
    #   remote dashboard cannot receive the callback and the user pastes the
    #   URL back (the CLI runs a real listener instead).
    # "dashboard": the provider accepts our own callback URL, so the browser
    #   comes straight back to the dashboard and the flow completes itself.
    redirect_mode: ClassVar[str] = "loopback"
    loopback_port: ClassVar[int] = 0
    loopback_path: ClassVar[str] = "/auth/callback"
    # Path appended to the dashboard's own origin for "dashboard" mode.
    dashboard_callback_path: ClassVar[str] = "/api/oauth/callback"

    llm: ClassVar[LLMBinding | None] = None

    # ── Flow hooks ─────────────────────────────────────────────────

    @classmethod
    def loopback_redirect_uri(cls) -> str:
        return f"http://localhost:{cls.loopback_port}{cls.loopback_path}"

    @classmethod
    def authorize_params(cls, *, state: str, challenge: str, redirect_uri: str) -> dict[str, str]:
        """Query parameters for the authorization URL."""
        return {
            "response_type": "code",
            "client_id": cls.client_id,
            "redirect_uri": redirect_uri,
            "scope": " ".join(cls.scopes),
            "state": state,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        }

    @classmethod
    def token_request_data(cls, *, code: str, verifier: str, redirect_uri: str) -> dict[str, str]:
        """Form body for the authorization-code exchange."""
        return {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect_uri,
            "client_id": cls.client_id,
            "code_verifier": verifier,
        }

    @classmethod
    def refresh_request_data(cls, refresh_token: str) -> dict[str, str]:
        """Form body for a refresh."""
        data = {
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": cls.client_id,
        }
        if cls.refresh_scopes:
            data["scope"] = " ".join(cls.refresh_scopes)
        return data

    @classmethod
    def token_request_headers(cls) -> dict[str, str]:
        return {"Content-Type": "application/x-www-form-urlencoded"}

    @classmethod
    def identify(cls, payload: dict[str, Any]) -> dict[str, str]:
        """Pull ``{account_id, account_label, plan}`` out of a token payload.

        Default: read the id_token's standard claims. Providers that keep the
        account somewhere else override this.
        """
        claims = decode_jwt_claims(payload.get("id_token", "") or "")
        return {
            "account_id": str(claims.get("sub", "") or ""),
            "account_label": str(claims.get("email") or claims.get("name") or ""),
            "plan": "",
        }

    # ── Request building (LLM side) ────────────────────────────────

    @classmethod
    def llm_headers(cls, tokens: OAuthTokens) -> dict[str, str]:
        """Headers the LLM endpoint needs beyond ``Content-Type``."""
        return {"Authorization": f"{tokens.token_type or 'Bearer'} {tokens.access_token}"}

    @classmethod
    def adapt_llm_body(cls, body: dict[str, Any]) -> dict[str, Any]:
        """Last chance to reshape a request body for this endpoint."""
        return body

    @classmethod
    def requires_stream(cls) -> bool:
        """True when the endpoint only answers as a server-sent event stream."""
        return False

    # ── Public flow API ────────────────────────────────────────────

    @classmethod
    def authorization_url(cls, *, state: str, challenge: str, redirect_uri: str) -> str:
        params = cls.authorize_params(state=state, challenge=challenge, redirect_uri=redirect_uri)
        return f"{cls.authorize_url}?{urlencode(params)}"

    @classmethod
    async def exchange_code(cls, *, code: str, verifier: str, redirect_uri: str) -> OAuthTokens:
        """Redeem an authorization code for a grant."""
        data = cls.token_request_data(code=code, verifier=verifier, redirect_uri=redirect_uri)
        payload = await cls._post_token(data)
        return cls._tokens_from_payload(payload)

    @classmethod
    async def refresh(cls, tokens: OAuthTokens) -> OAuthTokens:
        """Exchange the refresh token for a fresh access token.

        The old refresh token is carried over when the response omits one —
        issuers differ on whether refresh tokens rotate, and dropping a
        non-rotating one silently turns a working login into a dead one.
        """
        if not tokens.refresh_token:
            raise OAuthError(
                f"{cls.label} session expired and carries no refresh token — sign in again"
            )
        payload = await cls._post_token(cls.refresh_request_data(tokens.refresh_token))
        refreshed = cls._tokens_from_payload(payload)
        if not refreshed.refresh_token:
            refreshed.refresh_token = tokens.refresh_token
        if not refreshed.account_id:
            refreshed.account_id = tokens.account_id
            refreshed.account_label = tokens.account_label
            refreshed.plan = tokens.plan
        return refreshed

    @classmethod
    async def _post_token(cls, data: dict[str, str]) -> dict[str, Any]:
        async with httpx.AsyncClient(timeout=_TOKEN_TIMEOUT) as client:
            resp = await client.post(cls.token_url, data=data, headers=cls.token_request_headers())
        if resp.status_code != 200:
            raise OAuthError(_token_error(cls.label, resp))
        try:
            payload = resp.json()
        except ValueError as exc:
            raise OAuthError(f"{cls.label} returned a non-JSON token response") from exc
        if not isinstance(payload, dict) or not payload.get("access_token"):
            raise OAuthError(f"{cls.label} returned no access token")
        return payload

    @classmethod
    def _tokens_from_payload(cls, payload: dict[str, Any]) -> OAuthTokens:
        expires_in = payload.get("expires_in")
        try:
            expires_at = time.time() + float(expires_in) if expires_in else 0.0
        except (TypeError, ValueError):
            expires_at = 0.0
        identity = cls.identify(payload)
        return OAuthTokens(
            provider=cls.id,
            access_token=str(payload.get("access_token", "")),
            refresh_token=str(payload.get("refresh_token", "") or ""),
            id_token=str(payload.get("id_token", "") or ""),
            token_type=str(payload.get("token_type", "Bearer") or "Bearer"),
            expires_at=expires_at,
            scope=str(payload.get("scope", "") or ""),
            account_id=identity.get("account_id", ""),
            account_label=identity.get("account_label", ""),
            plan=identity.get("plan", ""),
        )


def _token_error(label: str, resp: httpx.Response) -> str:
    """A message worth reading: the issuer's own error, not just its status.

    OAuth errors are almost always actionable (``invalid_grant`` means the code
    was reused or expired), and the body is where they say so.
    """
    detail = ""
    try:
        body = resp.json()
        if isinstance(body, dict):
            detail = str(body.get("error_description") or body.get("error") or "")
    except ValueError:
        detail = resp.text[:200]
    suffix = f": {detail}" if detail else ""
    return f"{label} rejected the request (HTTP {resp.status_code}){suffix}"
