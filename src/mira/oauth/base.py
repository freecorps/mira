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
import re
import secrets
import time
from dataclasses import dataclass, field
from typing import Any, ClassVar
from urllib.parse import urlencode

import httpx

from mira.oauth.usage import UsageSnapshot

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
    # The slot this grant occupies under its provider. A provider can hold
    # several accounts at once; the key is what the dashboard, the CLI and a
    # model route (``oauth:chatgpt:<key>:…``) use to name one of them. Derived
    # from the account id when the issuer gives us one, so signing in to the
    # same account again lands in the same slot instead of adding a twin.
    account_key: str = ""

    def is_expired(self, skew: float = REFRESH_SKEW_SECONDS) -> bool:
        """True when the access token is gone or about to be."""
        if not self.expires_at:
            return False  # No expiry advertised — assume it lives until a 401.
        return time.time() >= (self.expires_at - skew)

    def ensure_key(self) -> str:
        """Assign the slot key if the grant has none yet, and return it."""
        if not self.account_key:
            self.account_key = account_key_for(self.account_id)
        return self.account_key

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
            "account_key": self.account_key,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> OAuthTokens:
        """Rebuild from a stored blob, ignoring keys we no longer read.

        Forward compatibility matters here: a downgrade must not wipe a working
        grant just because a newer build stored an extra field.
        """
        known = set(cls.__dataclass_fields__)
        return cls(**{k: v for k, v in data.items() if k in known})


_KEY_CHARS = re.compile(r"[^A-Za-z0-9_-]+")
_KEY_MAX = 48


def account_key_for(account_id: str) -> str:
    """A slot key for an account: its id made safe for keys and routes.

    Keys live inside settings-table row names and inside model routes, both
    colon-separated, so the id is reduced to a URL-safe alphabet and kept
    short. An id that loses characters to that, or is cut to length, gets a
    digest of the original appended so two ids that differ only in what was
    dropped still get their own slots. An account with no id at all (a
    provider whose tokens carry none) gets a random key, which still serves
    as a slot — it just cannot be matched on a second sign-in.
    """
    raw = (account_id or "").strip()
    cleaned = _KEY_CHARS.sub("", raw)
    if not cleaned:
        return secrets.token_hex(6)
    if cleaned == raw and len(cleaned) <= _KEY_MAX:
        return cleaned
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:8]
    return f"{cleaned[: _KEY_MAX - len(digest) - 1]}-{digest}"


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
    # The curated list — what the dropdown offers when the provider cannot be
    # asked for its live list (see ``OAuthProviderSpec.fetch_models``).
    models: tuple[dict[str, Any], ...] = ()
    # Maps our thinking-mode values onto whatever the endpoint accepts,
    # mirroring ``provider_profiles``' ``reasoning_effort_map``.
    reasoning_effort_map: dict[str, str] = field(default_factory=dict)
    # How the endpoint is spoken to, in words the dashboard can show next to
    # the account so an operator knows what "use this" means: which protocol,
    # whether the answer streams, and where it goes.
    protocol_label: str = "Responses API"
    transport_label: str = "HTTPS"

    def describe(self) -> dict[str, str]:
        return {
            "api_style": self.api_style,
            "protocol": self.protocol_label,
            "transport": self.transport_label,
            "endpoint": self.base_url,
        }


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

    # ── Usage and model discovery (optional) ───────────────────────

    # True when the provider reports how much of a plan's allowance is spent.
    # Drives whether the dashboard shows usage meters and whether rotation
    # has anything to go on beyond "it answered 429".
    reports_usage: ClassVar[bool] = False

    @classmethod
    def usage_from_headers(cls, headers: Any) -> UsageSnapshot | None:
        """Read a usage snapshot off an LLM response's headers, if it carries one."""
        return None

    @classmethod
    async def fetch_usage(cls, tokens: OAuthTokens) -> UsageSnapshot | None:
        """Ask the provider where this account's allowance stands right now."""
        return None

    @classmethod
    async def fetch_models(cls, tokens: OAuthTokens) -> list[dict[str, Any]] | None:
        """The models this account can use, from the provider itself.

        None means "no such endpoint, or it did not answer" — the caller
        falls back to the curated ``llm.models`` list. A list, even an empty
        one, is the provider's word.
        """
        return None

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
        # The slot is the grant's identity in the store; a refresh must land
        # in the same one whatever the new payload says about itself.
        refreshed.account_key = tokens.account_key or refreshed.ensure_key()
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
        tokens = OAuthTokens(
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
        tokens.ensure_key()
        return tokens


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
