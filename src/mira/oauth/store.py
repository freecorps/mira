"""Persistence for OAuth grants, and the refresh that keeps them usable.

Grants live in the dashboard's ``settings`` table, one JSON row per provider
(``oauth_credentials:chatgpt``). That is deliberate: it works identically on
SQLite and Postgres with no migration, and it inherits the same backup and
access story as every other dashboard secret.

Two access rules the rest of the codebase depends on:

* ``load`` returns what is stored, no network. ``valid_tokens`` returns a token
  that is good *now*, refreshing first if it is close to expiry.
* Refreshes are serialised per provider. Mira runs several review passes
  concurrently; without the lock they would each notice the same expiring token
  and fire their own refresh, and an issuer that rotates refresh tokens would
  invalidate all but one of them — logging the user out mid-review.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections import defaultdict
from typing import Any, Protocol

from mira.oauth import registry
from mira.oauth.base import OAuthError, OAuthTokens

logger = logging.getLogger(__name__)

_KEY_PREFIX = "oauth_credentials:"
# Which provider (if any) is serving reviews. "" = none; use the API key path.
ACTIVE_PROVIDER_KEY = "llm_oauth_provider"

_locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)


class SettingsStore(Protocol):
    """The slice of ``AppDatabase`` this module needs."""

    def get_setting(self, key: str) -> str | None: ...

    def set_setting(self, key: str, value: str) -> None: ...


def default_db() -> SettingsStore | None:
    """The dashboard's process-wide database, if one is initialized.

    Imported lazily: the OAuth package is used by the CLI too, and importing
    the dashboard API at module load would drag the whole FastAPI app into a
    plain ``mira review`` run.
    """
    try:
        from mira.dashboard.api import _app_db

        return _app_db
    except Exception:  # pragma: no cover - dashboard not importable
        return None


def _resolve(db: SettingsStore | None) -> SettingsStore:
    resolved = db or default_db()
    if resolved is None:
        raise OAuthError("No dashboard database available to store the OAuth session")
    return resolved


# ── Credentials ─────────────────────────────────────────────────────


def load(provider_id: str, db: SettingsStore | None = None) -> OAuthTokens | None:
    """The stored grant for ``provider_id``, or None if not connected."""
    store = db or default_db()
    if store is None:
        return None
    raw = store.get_setting(_KEY_PREFIX + provider_id)
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        logger.warning("Stored OAuth credentials for %s are not valid JSON", provider_id)
        return None
    if not isinstance(data, dict) or not data.get("access_token"):
        return None
    data.setdefault("provider", provider_id)
    try:
        return OAuthTokens.from_dict(data)
    except TypeError:
        logger.warning("Stored OAuth credentials for %s have an unreadable shape", provider_id)
        return None


def save(tokens: OAuthTokens, db: SettingsStore | None = None) -> None:
    """Persist a grant, replacing any previous one for that provider."""
    _resolve(db).set_setting(_KEY_PREFIX + tokens.provider, json.dumps(tokens.to_dict()))


def delete(provider_id: str, db: SettingsStore | None = None) -> None:
    """Forget a grant, and stop routing reviews at it if it was active.

    Leaving the active pointer behind would leave every review failing with
    "not connected" and no obvious way, from the dashboard, to see why.
    """
    store = _resolve(db)
    store.set_setting(_KEY_PREFIX + provider_id, "")
    if get_active_provider(store) == provider_id:
        set_active_provider("", store)


def connected(db: SettingsStore | None = None) -> dict[str, OAuthTokens]:
    """Every provider with a stored grant, keyed by id."""
    out: dict[str, OAuthTokens] = {}
    for provider_id in registry.all_providers():
        tokens = load(provider_id, db)
        if tokens is not None:
            out[provider_id] = tokens
    return out


# ── Active LLM backend ──────────────────────────────────────────────


def get_active_provider(db: SettingsStore | None = None) -> str:
    """The provider id currently serving reviews, or "" for none."""
    store = db or default_db()
    if store is None:
        return ""
    return (store.get_setting(ACTIVE_PROVIDER_KEY) or "").strip()


def set_active_provider(provider_id: str, db: SettingsStore | None = None) -> None:
    """Route reviews at ``provider_id`` ("" to go back to the API-key path)."""
    _resolve(db).set_setting(ACTIVE_PROVIDER_KEY, provider_id or "")


def active_binding(db: SettingsStore | None = None) -> tuple[str, OAuthTokens] | None:
    """``(provider_id, tokens)`` for the active LLM provider, if it is usable.

    Returns None — rather than raising — when the pointer is set but the
    provider is unknown or disconnected, so config resolution can fall back to
    the API-key path instead of taking the whole review down with it.
    """
    provider_id = get_active_provider(db)
    if not provider_id:
        return None
    spec = registry.get(provider_id)
    if spec is None or spec.llm is None:
        logger.warning("Active OAuth provider %r is not a usable LLM backend", provider_id)
        return None
    tokens = load(provider_id, db)
    if tokens is None:
        logger.warning("Active OAuth provider %r has no stored session", provider_id)
        return None
    return provider_id, tokens


# ── Refresh ─────────────────────────────────────────────────────────


async def valid_tokens(provider_id: str, db: SettingsStore | None = None) -> OAuthTokens:
    """A grant that is good right now, refreshing and persisting if needed."""
    spec = registry.require(provider_id)
    tokens = load(provider_id, db)
    if tokens is None:
        raise OAuthError(f"{spec.label} is not connected — sign in from Settings → Connections")
    if not tokens.is_expired():
        return tokens

    async with _locks[provider_id]:
        # Another caller may have refreshed while we waited for the lock.
        current = load(provider_id, db) or tokens
        if not current.is_expired():
            return current
        logger.info("Refreshing %s OAuth session", spec.label)
        refreshed = await spec.refresh(current)
        save(refreshed, db)
        return refreshed


def status(provider_id: str, db: SettingsStore | None = None) -> dict[str, Any]:
    """Connection state for the dashboard. Never includes token material."""
    spec = registry.require(provider_id)
    tokens = load(provider_id, db)
    return {
        "id": spec.id,
        "label": spec.label,
        "description": spec.description,
        "docs_url": spec.docs_url,
        "serves_models": spec.llm is not None,
        "connected": tokens is not None,
        "account_label": tokens.account_label if tokens else "",
        "plan": tokens.plan if tokens else "",
        "expires_at": tokens.expires_at if tokens else 0.0,
        "connected_at": tokens.obtained_at if tokens else 0.0,
        "can_refresh": bool(tokens and tokens.refresh_token),
        "models": list(spec.llm.models) if spec.llm else [],
        "default_model": spec.llm.default_model if spec.llm else "",
    }
