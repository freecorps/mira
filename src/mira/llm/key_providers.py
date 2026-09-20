"""API-key providers on the Connections page: which key is set, and its allowance.

A key is not an account Mira signed in to — nothing here touches the OAuth
store's credentials, and there is no session to renew. What a key-based
endpoint can still have is a metered allowance: OpenCode Go is a subscription
with 5-hour, weekly and monthly windows, reported by ``GET /zen/go/v1/usage``
against the key. A profile in ``providers.json`` that names a ``usage_url``
gets the same meters an OAuth account gets, read from that endpoint on demand
and kept in the settings table next to the OAuth snapshots.

The snapshot is asked for on a page load only when the stored one is older
than a few minutes, and a failed lookup is not retried for a minute — the
Connections page must not re-block on a dead endpoint every time it opens.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import time
from collections import defaultdict
from collections.abc import Callable
from datetime import datetime
from typing import Any

import httpx

from mira.config import LLMConfig
from mira.exceptions import LLMError
from mira.llm import provider_profiles as profiles
from mira.llm.base import _get_api_key
from mira.oauth.usage import UsageSnapshot, UsageWindow

logger = logging.getLogger(__name__)

_SETTING_PREFIX = "provider_usage:"
# A stored snapshot this recent is shown as-is; older, it is asked for again.
FRESH_SECONDS = 300.0
# After a failed lookup, how long the stored (or absent) snapshot stands in
# before the endpoint is tried again.
FAILURE_SECONDS = 60.0
_USAGE_TIMEOUT = 10.0

_PROTOCOLS = {"chat": "Chat Completions", "responses": "Responses API"}

_failed_at: dict[str, float] = {}
_locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)


class UsageError(Exception):
    """The provider did not report usage, and this is why."""


# ── Parsers ─────────────────────────────────────────────────────────

# OpenCode Go's three windows, in the order the payload names them, with the
# length the plan documents for each (the document carries none).
_GO_WINDOWS: tuple[tuple[str, int], ...] = (
    ("rolling", 5 * 60),
    ("weekly", 7 * 24 * 60),
    ("monthly", 30 * 24 * 60),
)


def parse_opencode_go(payload: Any) -> UsageSnapshot | None:
    """Read ``GET /zen/go/v1/usage`` into a snapshot.

    The document::

        {"usage": {"rolling": {"status": "ok", "percent": 12.3,
                               "resetsAt": "2026-09-20T09:00:00.000Z"},
                   "weekly":  {...}, "monthly": {...}}}

    ``status`` is ``"rate-limited"`` once a window is spent; the percent can
    read just under 100 at that point, so the flag is kept as well as the
    number. A document with no window at all is not a snapshot.
    """
    if not isinstance(payload, dict):
        return None
    usage = payload.get("usage")
    if not isinstance(usage, dict):
        return None
    windows: list[UsageWindow | None] = []
    limited = False
    for key, minutes in _GO_WINDOWS:
        raw = usage.get(key)
        percent = _number(raw.get("percent")) if isinstance(raw, dict) else None
        if not isinstance(raw, dict) or percent is None:
            windows.append(None)
            continue
        limited = limited or raw.get("status") == "rate-limited"
        windows.append(
            UsageWindow(
                used_percent=float(percent),
                window_minutes=minutes,
                resets_at=_iso_epoch(raw.get("resetsAt")),
            )
        )
    if not any(windows):
        return None
    return UsageSnapshot(
        primary=windows[0],
        secondary=windows[1],
        tertiary=windows[2],
        limit_reached=limited,
        source="endpoint",
        fetched_at=time.time(),
    )


PARSERS: dict[str, Callable[[Any], UsageSnapshot | None]] = {
    "opencode-go": parse_opencode_go,
}


def _number(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return None if parsed != parsed else parsed  # NaN


def _iso_epoch(value: Any) -> float | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


# ── Keys ────────────────────────────────────────────────────────────


def is_endpoint(profile: dict, config: LLMConfig) -> bool:
    """Is this profile the API-key endpoint the config is pointed at?"""
    if config.provider == "bedrock":
        return False
    return profiles.resolve(config.base_url).get("name") == profile["name"]


def api_key_for(profile: dict, config: LLMConfig | None) -> str:
    """The key this profile's requests would carry, or "".

    For the endpoint the config points at, that is whatever the client itself
    would resolve (``llm.api_key_env`` first, then the profile's variable);
    for any other profile, its own variable. Never raises: a missing key is a
    state the page reports, not an error.
    """
    if config is not None and is_endpoint(profile, config):
        try:
            return _get_api_key(config, profile)
        except LLMError:
            return ""
    env = profile.get("api_key_env")
    return os.environ.get(env, "") if env else ""


def api_key_env_for(profile: dict, config: LLMConfig | None) -> str:
    """The variable the key is read from, as the page should name it."""
    if config is not None and is_endpoint(profile, config) and config.api_key_env:
        return config.api_key_env
    return str(profile.get("api_key_env") or "")


def _fingerprint(api_key: str) -> str:
    return hashlib.sha256(api_key.encode("utf-8")).hexdigest()[:12]


def _slot(name: str, api_key: str) -> str:
    # Keyed by a digest of the key: a rotated key starts from an empty
    # snapshot rather than inheriting the old one's windows.
    return f"{_SETTING_PREFIX}{name}:{_fingerprint(api_key)}"


# ── Storage ─────────────────────────────────────────────────────────


def _db(db: Any) -> Any:
    if db is not None:
        return db
    from mira.oauth.store import default_db

    return default_db()


def load_usage(name: str, api_key: str, db: Any = None) -> UsageSnapshot | None:
    store = _db(db)
    if store is None or not api_key:
        return None
    raw = store.get_setting(_slot(name, api_key))
    if not raw:
        return None
    try:
        return UsageSnapshot.from_dict(json.loads(raw))
    except json.JSONDecodeError:
        return None


def save_usage(name: str, api_key: str, snapshot: UsageSnapshot, db: Any = None) -> None:
    store = _db(db)
    if store is None:
        return
    store.set_setting(_slot(name, api_key), json.dumps(snapshot.to_dict()))


# ── Fetching ────────────────────────────────────────────────────────


async def fetch_usage(profile: dict, api_key: str) -> UsageSnapshot:
    """Ask the profile's usage endpoint where the key's allowance stands.

    Raises :class:`UsageError` with the provider's own message where it gave
    one ("Invalid API key.", "OpenCode Go subscription required.") — the
    Refresh button exists to answer exactly that question.
    """
    url = profile.get("usage_url")
    parser = PARSERS.get(profile.get("usage_format") or "")
    if not url or parser is None:
        raise UsageError(f"{profile.get('label') or profile['name']} does not report usage")
    if not api_key:
        env = profile.get("api_key_env") or "its API key variable"
        raise UsageError(f"No API key: set {env}")
    headers = {"Authorization": f"Bearer {api_key}", **profile.get("extra_headers", {})}
    try:
        async with httpx.AsyncClient(timeout=_USAGE_TIMEOUT) as client:
            resp = await client.get(url, headers=headers)
    except httpx.HTTPError as exc:
        raise UsageError(f"Usage lookup failed: {exc}") from exc
    if resp.status_code != 200:
        raise UsageError(_provider_message(resp))
    try:
        payload = resp.json()
    except ValueError as exc:
        raise UsageError("Usage lookup returned a non-JSON body") from exc
    snapshot = parser(payload)
    if snapshot is None:
        raise UsageError("Usage lookup returned a document with no windows in it")
    return snapshot


def _provider_message(resp: httpx.Response) -> str:
    """The provider's own words for a refusal, or the status if it had none."""
    try:
        body = resp.json()
        error = body.get("error") if isinstance(body, dict) else None
        message = error.get("message") if isinstance(error, dict) else None
        if isinstance(message, str) and message.strip():
            return f"Usage lookup answered HTTP {resp.status_code}: {message.strip()}"
    except ValueError:
        pass
    return f"Usage lookup answered HTTP {resp.status_code}"


async def usage_for(
    profile: dict, api_key: str, db: Any = None, *, force: bool = False
) -> UsageSnapshot | None:
    """The key's allowance: the stored snapshot when fresh, else a fresh one.

    ``force`` asks the endpoint whatever is stored and raises when it does
    not answer; without it a failure is logged, remembered for a minute, and
    the stored snapshot (or none) stands in.
    """
    name = profile["name"]
    if not api_key or not profile.get("usage_url"):
        return None
    stored = load_usage(name, api_key, db)

    def fresh_enough() -> bool:
        now = time.time()
        if stored is not None and now - stored.fetched_at < FRESH_SECONDS:
            return True
        return now - _failed_at.get(name, 0.0) < FAILURE_SECONDS

    if not force and fresh_enough():
        return stored
    async with _locks[name]:
        if not force:
            stored = load_usage(name, api_key, db)
            if fresh_enough():
                return stored
        try:
            snapshot = await fetch_usage(profile, api_key)
        except UsageError as exc:
            _failed_at[name] = time.time()
            if force:
                raise
            logger.warning("%s: %s", profile.get("label") or name, exc)
            return stored
        _failed_at.pop(name, None)
        save_usage(name, api_key, snapshot, db)
        return snapshot


# ── Status ──────────────────────────────────────────────────────────


def key_status(
    profile: dict, config: LLMConfig | None, api_key: str, usage: UsageSnapshot | None
) -> dict[str, Any]:
    """One profile's card. Never includes the key, or anything derived from it."""
    endpoint = config is not None and is_endpoint(profile, config)
    style = config.api_style if endpoint and config is not None else "chat"
    style = style if style in _PROTOCOLS else "chat"
    return {
        "id": profile["name"],
        "label": profile.get("label") or profile["name"],
        "description": profile.get("description") or "",
        "docs_url": profile.get("docs_url") or "",
        "endpoint": profile.get("base_url") or "",
        "api_key_env": api_key_env_for(profile, config),
        "key_configured": bool(api_key),
        "is_endpoint": endpoint,
        "reports_usage": bool(profile.get("usage_url")),
        "protocol": {
            "api_style": style,
            "protocol": _PROTOCOLS[style],
            "transport": "HTTPS",
            "endpoint": profile.get("base_url") or "",
        },
        "usage": usage.to_dict() if usage else None,
        "available": usage.available() if usage else True,
    }


async def list_status(config: LLMConfig | None, db: Any = None) -> list[dict[str, Any]]:
    """Every labelled profile's card, the configured endpoint first."""
    out = []
    for profile in profiles.labelled().values():
        api_key = api_key_for(profile, config)
        usage = await usage_for(profile, api_key, db) if profile.get("usage_url") else None
        out.append(key_status(profile, config, api_key, usage))
    out.sort(key=lambda entry: (not entry["is_endpoint"], entry["label"].lower()))
    return out


async def refresh(name: str, config: LLMConfig | None, db: Any = None) -> dict[str, Any]:
    """Ask the provider now, record the answer, and return the card.

    Raises ``KeyError`` for a profile that is not on the page and
    :class:`UsageError` when the provider did not answer.
    """
    profile = profiles.labelled().get(name)
    if profile is None:
        raise KeyError(name)
    api_key = api_key_for(profile, config)
    usage = await usage_for(profile, api_key, db, force=True)
    return key_status(profile, config, api_key, usage)
