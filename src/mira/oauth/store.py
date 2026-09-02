"""Persistence for OAuth grants, and the refresh that keeps them usable.

Grants live in the dashboard's ``settings`` table, one JSON row per account
(``oauth_credentials:chatgpt:<account key>``). That is deliberate: it works
identically on SQLite and Postgres with no migration, and it inherits the
same backup and access story as every other dashboard secret. A provider can
hold any number of accounts; the key names one of them (see
``OAuthTokens.account_key``). Rows written by the previous build, which held
one account per provider under ``oauth_credentials:chatgpt``, are moved into
a slot the first time they are read.

Next to each grant sits what is known about that account's allowance
(``oauth_usage:chatgpt:<key>``): the windows the backend last reported and
Mira's own note of a refusal. That is what the dashboard's meters show and
what account rotation ranks by.

Two access rules the rest of the codebase depends on:

* ``load`` returns what is stored, no network. ``valid_tokens`` returns a token
  that is good *now*, refreshing first if it is close to expiry.
* Refreshes are serialised per account. Mira runs several review passes
  concurrently; without the lock they would each notice the same expiring token
  and fire their own refresh, and an issuer that rotates refresh tokens would
  invalidate all but one of them — logging the user out mid-review.

That lock lives in one process, which covers the server as deployed (a single
uvicorn worker) but not a second one alongside it, nor a ``mira auth`` command
run against the same database. So a refusal is not taken at face value: a
refresh that fails re-reads the store, and a grant somebody else has since
rotated in is used instead of reporting a session that was never lost.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections import defaultdict
from typing import Any, Protocol

from mira.oauth import registry
from mira.oauth.base import OAuthError, OAuthTokens
from mira.oauth.routes import ANY_ACCOUNT
from mira.oauth.usage import UsageSnapshot, choose_account

logger = logging.getLogger(__name__)

_KEY_PREFIX = "oauth_credentials:"
_USAGE_PREFIX = "oauth_usage:"
# Which signed-in backend serves calls that name a bare model id (no route).
# "" = none, use the API-key path. "chatgpt" = that provider, rotating across
# its accounts. "chatgpt:<key>" = that one account.
ACTIVE_PROVIDER_KEY = "llm_oauth_provider"

_locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)


class SettingsStore(Protocol):
    """The slice of ``AppDatabase`` the OAuth package needs."""

    def get_setting(self, key: str) -> str | None: ...

    def set_setting(self, key: str, value: str) -> None: ...

    def delete_setting(self, key: str) -> None: ...

    def take_setting(self, key: str) -> str | None: ...

    def list_settings(self, prefix: str) -> dict[str, str]: ...


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


def _slot(provider_id: str, key: str) -> str:
    return f"{_KEY_PREFIX}{provider_id}:{key}"


def _usage_slot(provider_id: str, key: str) -> str:
    return f"{_USAGE_PREFIX}{provider_id}:{key}"


# ── Credentials ─────────────────────────────────────────────────────


def _parse(raw: str | None, provider_id: str) -> OAuthTokens | None:
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


def _migrate_legacy(provider_id: str, store: SettingsStore) -> None:
    """Move a one-per-provider row from the previous build into a slot.

    Done on read rather than at startup so the CLI, which opens the same
    database, sees the same layout without its own migration step. The row
    is moved, not copied: the old key is what the previous build wrote, and
    leaving it behind would resurrect a disconnected account on downgrade.
    """
    legacy_key = _KEY_PREFIX + provider_id
    raw = store.get_setting(legacy_key)
    if not raw:
        return
    tokens = _parse(raw, provider_id)
    store.delete_setting(legacy_key)
    if tokens is None:
        return
    tokens.ensure_key()
    store.set_setting(_slot(provider_id, tokens.account_key), json.dumps(tokens.to_dict()))
    logger.info("Moved the %s session into account slot %s", provider_id, tokens.account_key)


def accounts(provider_id: str, db: SettingsStore | None = None) -> dict[str, OAuthTokens]:
    """Every stored grant for ``provider_id``, keyed by account key."""
    store = db or default_db()
    if store is None:
        return {}
    _migrate_legacy(provider_id, store)
    prefix = f"{_KEY_PREFIX}{provider_id}:"
    out: dict[str, OAuthTokens] = {}
    for key, raw in store.list_settings(prefix).items():
        account_key = key[len(prefix) :]
        if not account_key:
            continue
        tokens = _parse(raw, provider_id)
        if tokens is None:
            continue
        tokens.account_key = tokens.account_key or account_key
        out[account_key] = tokens
    return dict(sorted(out.items(), key=lambda item: (item[1].obtained_at, item[0])))


def load(
    provider_id: str, account_key: str = "", db: SettingsStore | None = None
) -> OAuthTokens | None:
    """The stored grant for one account, or None if not connected.

    With no key: the provider's only account, if it has exactly one. Two or
    more is ambiguous and answers None — a caller that means "any of them"
    asks :func:`pick_account` instead.
    """
    if account_key and account_key != ANY_ACCOUNT:
        store = db or default_db()
        if store is None:
            return None
        _migrate_legacy(provider_id, store)
        tokens = _parse(store.get_setting(_slot(provider_id, account_key)), provider_id)
        if tokens is not None:
            tokens.account_key = tokens.account_key or account_key
        return tokens
    found = accounts(provider_id, db)
    if len(found) == 1:
        return next(iter(found.values()))
    return None


def save(tokens: OAuthTokens, db: SettingsStore | None = None) -> None:
    """Persist a grant into its account slot, replacing what was there."""
    tokens.ensure_key()
    _resolve(db).set_setting(
        _slot(tokens.provider, tokens.account_key), json.dumps(tokens.to_dict())
    )


def delete(provider_id: str, account_key: str, db: SettingsStore | None = None) -> None:
    """Forget one account, and stop routing at it if it was the default.

    A default that named this account exactly is cleared; one that named the
    provider (rotate across whatever it has) is cleared only when this was
    the last account, so removing one of three does not silently send every
    review back to the API key.
    """
    store = _resolve(db)
    store.delete_setting(_slot(provider_id, account_key))
    store.delete_setting(_usage_slot(provider_id, account_key))
    active_provider, active_key = parse_ref(get_active_ref(store))
    if active_provider != provider_id:
        return
    if active_key == account_key or not accounts(provider_id, store):
        set_active("", "", store)


def delete_all(provider_id: str, db: SettingsStore | None = None) -> None:
    """Forget every account of a provider."""
    store = _resolve(db)
    for key in list(accounts(provider_id, store)):
        delete(provider_id, key, store)


def connected(db: SettingsStore | None = None) -> dict[str, dict[str, OAuthTokens]]:
    """Every provider with at least one stored grant: ``{provider: {key: tokens}}``."""
    out: dict[str, dict[str, OAuthTokens]] = {}
    for provider_id in registry.all_providers():
        found = accounts(provider_id, db)
        if found:
            out[provider_id] = found
    return out


# ── Default backend ─────────────────────────────────────────────────


def parse_ref(ref: str) -> tuple[str, str]:
    """Split ``"chatgpt"`` / ``"chatgpt:<key>"`` / ``"chatgpt:*"`` into (provider, key).

    The key is ``""`` for "any account" — both the bare form the previous
    build stored and an explicit ``*`` mean that.
    """
    text = ref.strip() if isinstance(ref, str) else ""
    if not text:
        return "", ""
    provider, _, key = text.partition(":")
    key = key.strip()
    return provider.strip().lower(), "" if key == ANY_ACCOUNT else key


def format_ref(provider_id: str, account_key: str = "") -> str:
    if not provider_id:
        return ""
    return f"{provider_id}:{account_key}" if account_key else provider_id


def get_active_ref(db: SettingsStore | None = None) -> str:
    """The stored default-backend pointer, verbatim ("" for none)."""
    store = db or default_db()
    if store is None:
        return ""
    return (store.get_setting(ACTIVE_PROVIDER_KEY) or "").strip()


def get_active_provider(db: SettingsStore | None = None) -> str:
    """The provider id of the default backend, or "" for none."""
    return parse_ref(get_active_ref(db))[0]


def set_active(provider_id: str, account_key: str = "", db: SettingsStore | None = None) -> None:
    """Route bare model ids at ``provider_id`` (one account, or any of them).

    Empty provider goes back to the API-key path.
    """
    _resolve(db).set_setting(ACTIVE_PROVIDER_KEY, format_ref(provider_id, account_key))


def set_active_provider(provider_id: str, db: SettingsStore | None = None) -> None:
    """Route bare model ids at ``provider_id``, rotating across its accounts."""
    set_active(provider_id, "", db)


def active_binding(db: SettingsStore | None = None) -> tuple[str, str] | None:
    """``(provider_id, account_key)`` for the default backend, if it is usable.

    ``account_key`` is ``""`` when calls may go to any of the provider's
    accounts. Returns None — rather than raising — when the pointer is set
    but the provider is unknown or has no session, so config resolution can
    fall back to the API-key path instead of taking the whole review down.
    """
    provider_id, account_key = parse_ref(get_active_ref(db))
    if not provider_id:
        return None
    spec = registry.get(provider_id)
    if spec is None or spec.llm is None:
        logger.warning("Default OAuth backend %r is not a usable LLM provider", provider_id)
        return None
    found = accounts(provider_id, db)
    if not found:
        logger.warning("Default OAuth backend %r has no stored session", provider_id)
        return None
    if account_key and account_key not in found:
        logger.warning(
            "Default OAuth account %s:%s is no longer connected; using any %s account",
            provider_id,
            account_key,
            provider_id,
        )
        return provider_id, ""
    return provider_id, account_key


# ── Usage ───────────────────────────────────────────────────────────


def load_usage(
    provider_id: str, account_key: str, db: SettingsStore | None = None
) -> UsageSnapshot | None:
    store = db or default_db()
    if store is None:
        return None
    raw = store.get_setting(_usage_slot(provider_id, account_key))
    if not raw:
        return None
    try:
        return UsageSnapshot.from_dict(json.loads(raw))
    except json.JSONDecodeError:
        return None


def save_usage(
    provider_id: str,
    account_key: str,
    snapshot: UsageSnapshot,
    db: SettingsStore | None = None,
) -> UsageSnapshot:
    """Record what the backend just said about an account's allowance.

    Mira's own notes on the previous snapshot (a refusal, the last use) are
    carried over: the report does not know about them.
    """
    store = _resolve(db)
    merged = snapshot.merge_bookkeeping(load_usage(provider_id, account_key, store))
    store.set_setting(_usage_slot(provider_id, account_key), json.dumps(merged.to_dict()))
    return merged


def mark_used(provider_id: str, account_key: str, db: SettingsStore | None = None) -> None:
    """Note that a call just went to this account (for round-robin ties)."""
    store = _resolve(db)
    snapshot = load_usage(provider_id, account_key, store) or UsageSnapshot(fetched_at=0.0)
    snapshot.last_used_at = time.time()
    store.set_setting(_usage_slot(provider_id, account_key), json.dumps(snapshot.to_dict()))


def mark_exhausted(
    provider_id: str, account_key: str, until: float, db: SettingsStore | None = None
) -> None:
    """Note that the backend refused this account until ``until``."""
    store = _resolve(db)
    snapshot = load_usage(provider_id, account_key, store) or UsageSnapshot(fetched_at=0.0)
    snapshot.exhausted_until = max(snapshot.exhausted_until, until)
    snapshot.limit_reached = True
    store.set_setting(_usage_slot(provider_id, account_key), json.dumps(snapshot.to_dict()))


def pick_account(
    provider_id: str,
    db: SettingsStore | None = None,
    *,
    exclude: set[str] | None = None,
) -> str | None:
    """The account the next call should use, by headroom; None if none can."""
    found = accounts(provider_id, db)
    if not found:
        return None
    candidates = [(key, load_usage(provider_id, key, db)) for key in found]
    return choose_account(candidates, exclude=exclude)


# ── Refresh ─────────────────────────────────────────────────────────


async def valid_tokens(
    provider_id: str,
    account_key: str = "",
    db: SettingsStore | None = None,
    *,
    force: bool = False,
) -> OAuthTokens:
    """A grant that is good right now, refreshing and persisting if needed.

    ``force`` renews even when the stored token still looks valid. That is the
    only correct answer to a 401: the endpoint has told us the token is dead,
    which the expiry cannot, and re-reading the store would return it again.
    """
    spec = registry.require(provider_id)
    tokens = load(provider_id, account_key, db)
    if tokens is None:
        raise OAuthError(f"{spec.label} is not connected — sign in from Settings → Connections")
    key = tokens.account_key
    if not force and not tokens.is_expired():
        return tokens

    async with _locks[f"{provider_id}:{key}"]:
        # Another caller may have refreshed while we waited for the lock.
        current = load(provider_id, key, db) or tokens
        if not current.is_expired() and (not force or current.access_token != tokens.access_token):
            return current
        logger.info("Refreshing %s OAuth session for %s", spec.label, current.account_label or key)
        try:
            refreshed = await spec.refresh(current)
        except OAuthError:
            # The lock is per-process, so a second worker (or a `mira auth`
            # command) may have rotated this grant out from under us, leaving
            # ours refused. Its replacement is already in the store, so read
            # once more before reporting a session nobody has actually lost.
            replacement = load(provider_id, key, db)
            if replacement is not None and replacement.access_token != current.access_token:
                logger.info("%s session was renewed elsewhere; using it", spec.label)
                return replacement
            raise
        save(refreshed, db)
        return refreshed


# ── Status (for the dashboard and the CLI) ──────────────────────────


def account_status(
    provider_id: str, tokens: OAuthTokens, db: SettingsStore | None = None
) -> dict[str, Any]:
    """One account's connection state. Never includes token material."""
    usage = load_usage(provider_id, tokens.account_key, db)
    active_provider, active_key = parse_ref(get_active_ref(db))
    return {
        "key": tokens.account_key,
        "account_label": tokens.account_label,
        "account_id_hint": tokens.account_id[-6:] if tokens.account_id else "",
        "plan": tokens.plan or (usage.plan if usage else ""),
        "expires_at": tokens.expires_at,
        "connected_at": tokens.obtained_at,
        "can_refresh": bool(tokens.refresh_token),
        "is_default": active_provider == provider_id and active_key == tokens.account_key,
        "usage": usage.to_dict() if usage else None,
        "available": usage.available() if usage else True,
    }


def status(provider_id: str, db: SettingsStore | None = None) -> dict[str, Any]:
    """Connection state for one provider and all of its accounts."""
    spec = registry.require(provider_id)
    found = accounts(provider_id, db)
    active_provider, active_key = parse_ref(get_active_ref(db))
    is_default = active_provider == provider_id
    return {
        "id": spec.id,
        "label": spec.label,
        "description": spec.description,
        "docs_url": spec.docs_url,
        "serves_models": spec.llm is not None,
        "reports_usage": spec.reports_usage,
        "connected": bool(found),
        "accounts": [account_status(provider_id, t, db) for t in found.values()],
        # Provider-level default: "rotate" when bare ids go to any account,
        # "pinned" when they go to one, "" when this provider is not the default.
        "default_mode": ("pinned" if active_key else "rotate") if is_default else "",
        "protocol": spec.llm.describe() if spec.llm else None,
        "models": list(spec.llm.models) if spec.llm else [],
        "default_model": spec.llm.default_model if spec.llm else "",
        "manual_exchange": spec.redirect_mode != "dashboard",
    }
