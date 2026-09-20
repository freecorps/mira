"""What the Connections page knows about an endpoint reached with a key.

An endpoint is not an account Mira signed in to — there is no session to
renew, and nothing here touches the OAuth store. What a key-based endpoint
can still have is a metered allowance: OpenCode Go is a subscription with
5-hour, weekly and monthly windows, reported by ``GET /zen/go/v1/usage``
against the key. A preset that names a ``usage_url`` gets the same meters an
OAuth account gets, read from that endpoint on demand and kept in the
settings table next to the OAuth snapshots.

This module is what the page and the CLI read: one card per endpoint —
those configured in the dashboard (:mod:`mira.llm.endpoints`) and, when the
config file names one of its own, that too, marked as coming from the file
and not editable here. It also holds the connection test the form runs
before saving, which asks the endpoint for its model list rather than
spending tokens on a completion.

The snapshot is asked for on a page load only when the stored one is older
than a few minutes, and a failed lookup is not retried for a minute — the
page must not re-block on a dead endpoint every time it opens.
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
from mira.llm import endpoints
from mira.llm import provider_profiles as profiles
from mira.llm.base import _get_api_key
from mira.llm.endpoints import CONFIG_ID, Endpoint
from mira.oauth.usage import UsageSnapshot, UsageWindow

logger = logging.getLogger(__name__)

_SETTING_PREFIX = "provider_usage:"
# A stored snapshot this recent is shown as-is; older, it is asked for again.
FRESH_SECONDS = 300.0
# After a failed lookup, how long the stored (or absent) snapshot stands in
# before the endpoint is tried again.
FAILURE_SECONDS = 60.0
_USAGE_TIMEOUT = 10.0
_TEST_TIMEOUT = 15.0

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
    read just under 100 at that point, so that window is recorded as fully
    spent — which keeps the refusal attached to *its* reset rather than to
    the earliest reset of any window, as a snapshot-wide flag alone would.
    A document with no window at all is not a snapshot.
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
        window_limited = raw.get("status") == "rate-limited"
        limited = limited or window_limited
        windows.append(
            UsageWindow(
                used_percent=100.0 if window_limited else float(percent),
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


# ── The config file's own endpoint ──────────────────────────────────


def config_profile(config: LLMConfig | None) -> dict:
    """The profile for the endpoint ``mira.yaml`` names, or {} for none."""
    if config is None or config.provider == "bedrock":
        return {}
    return profiles.resolve(config.base_url)


def config_key(config: LLMConfig | None) -> str:
    """The key the config file's endpoint would use, or "". Never raises."""
    if config is None:
        return ""
    try:
        return _get_api_key(config.model_copy(update={"endpoint": None}), config_profile(config))
    except LLMError:
        return ""


def config_key_env(config: LLMConfig | None) -> str:
    """The variable that key is read from, as the page should name it."""
    if config is None:
        return ""
    if config.api_key_env:
        return config.api_key_env
    return str(config_profile(config).get("api_key_env") or "")


# ── Storage ─────────────────────────────────────────────────────────


def _db(db: Any) -> Any:
    if db is not None:
        return db
    from mira.oauth.store import default_db

    return default_db()


def _fingerprint(api_key: str) -> str:
    return hashlib.sha256(api_key.encode("utf-8")).hexdigest()[:12]


def _slot(endpoint_id: str, api_key: str) -> str:
    # Keyed by a digest of the key as well as the endpoint: a rotated key
    # starts from an empty snapshot rather than inheriting the old one's
    # windows, and one endpoint's refusal says nothing about another's.
    return f"{_SETTING_PREFIX}{endpoint_id}:{_fingerprint(api_key)}"


def load_usage(endpoint_id: str, api_key: str, db: Any = None) -> UsageSnapshot | None:
    store = _db(db)
    if store is None or not api_key:
        return None
    raw = store.get_setting(_slot(endpoint_id, api_key))
    if not raw:
        return None
    try:
        return UsageSnapshot.from_dict(json.loads(raw))
    except json.JSONDecodeError:
        return None


def save_usage(endpoint_id: str, api_key: str, snapshot: UsageSnapshot, db: Any = None) -> None:
    store = _db(db)
    if store is None:
        return
    store.set_setting(_slot(endpoint_id, api_key), json.dumps(snapshot.to_dict()))


def forget_usage(endpoint_id: str, db: Any = None) -> None:
    """Drop every snapshot recorded for an endpoint, whatever key made it."""
    store = _db(db)
    if store is None:
        return
    for key in store.list_settings(f"{_SETTING_PREFIX}{endpoint_id}:"):
        store.delete_setting(key)
    for slot in [k for k in _failed_at if k.startswith(f"{_SETTING_PREFIX}{endpoint_id}:")]:
        del _failed_at[slot]


# ── Asking the provider ─────────────────────────────────────────────


async def fetch_usage(profile: dict, api_key: str) -> UsageSnapshot:
    """Ask the profile's usage endpoint where the key's allowance stands.

    Raises :class:`UsageError` with the provider's own message where it gave
    one ("Invalid API key.", "OpenCode Go subscription required.") — the
    Refresh button exists to answer exactly that question.
    """
    url = profile.get("usage_url")
    parser = PARSERS.get(profile.get("usage_format") or "")
    if not url or parser is None:
        raise UsageError(f"{profile.get('label') or profile.get('name')} does not report usage")
    if not api_key:
        env = profile.get("api_key_env") or "its API key variable"
        raise UsageError(f"No API key: add one here, or set {env}")
    headers = {"Authorization": f"Bearer {api_key}", **profile.get("extra_headers", {})}
    try:
        async with httpx.AsyncClient(timeout=_USAGE_TIMEOUT) as client:
            resp = await client.get(url, headers=headers)
    except httpx.HTTPError as exc:
        raise UsageError(f"Usage lookup failed: {exc}") from exc
    if resp.status_code != 200:
        raise UsageError(_provider_message(resp, "Usage lookup"))
    try:
        payload = resp.json()
    except ValueError as exc:
        raise UsageError("Usage lookup returned a non-JSON body") from exc
    snapshot = parser(payload)
    if snapshot is None:
        raise UsageError("Usage lookup returned a document with no windows in it")
    return snapshot


def _provider_message(resp: httpx.Response, what: str) -> str:
    """The provider's own words for a refusal, or the status if it had none."""
    try:
        body = resp.json()
        error = body.get("error") if isinstance(body, dict) else None
        message = error.get("message") if isinstance(error, dict) else error
        if isinstance(message, str) and message.strip():
            return f"{what} answered HTTP {resp.status_code}: {message.strip()}"
    except ValueError:
        pass
    return f"{what} answered HTTP {resp.status_code}"


async def test_connection(profile: dict, api_key: str) -> dict[str, Any]:
    """Try an endpoint and report what was actually proved.

    The model list is asked for first: it is the cheapest call that shows
    the URL resolves and speaks the protocol, and it spends no tokens. But
    plenty of endpoints — OpenCode Go among them — serve that list to
    anybody, so a 200 there says nothing about the key. Whether the key was
    exercised is then established rather than assumed:

    * where the provider has a usage endpoint, that call authenticates, so
      its refusal is the test's refusal;
    * otherwise the list is asked for again with no key at all, and if that
      works too the answer says the key went unchecked instead of implying
      it is good.
    """
    base = (profile.get("base_url") or "").rstrip("/")
    if not base:
        return {"ok": False, "detail": "No URL to test"}
    extra = dict(profile.get("extra_headers", {}))
    headers = dict(extra)
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    try:
        async with httpx.AsyncClient(timeout=_TEST_TIMEOUT) as client:
            resp = await client.get(f"{base}/models", headers=headers)
            if resp.status_code != 200:
                return {"ok": False, "detail": _provider_message(resp, f"GET {base}/models")}
            try:
                data = resp.json().get("data")
            except ValueError:
                return {"ok": False, "detail": "The endpoint answered, but not with JSON"}
            names = (
                [m.get("id") for m in data if isinstance(m, dict) and m.get("id")]
                if isinstance(data, list)
                else []
            )
            sample = ", ".join(str(n) for n in names[:3])
            found = (
                f"{len(names)} models available{f' ({sample}…)' if sample else ''}"
                if names
                else "The endpoint answered, with no model list in it"
            )
            if not api_key:
                return {"ok": True, "models": len(names), "detail": f"{found}. No key was sent."}

            # Does the key work? Ask something that needs one.
            if profile.get("usage_url"):
                try:
                    await fetch_usage(profile, api_key)
                except UsageError as exc:
                    return {"ok": False, "detail": str(exc)}
                return {"ok": True, "models": len(names), "detail": f"{found}. Key accepted."}

            anonymous = await client.get(f"{base}/models", headers=extra)
    except httpx.HTTPError as exc:
        return {"ok": False, "detail": f"Could not reach {base}: {exc}"}
    if anonymous.status_code == 200:
        return {
            "ok": True,
            "models": len(names),
            "detail": f"{found}. This endpoint lists models without a key, so the key was not checked here.",
        }
    return {"ok": True, "models": len(names), "detail": f"{found}. Key accepted."}


async def usage_for(
    endpoint_id: str, profile: dict, api_key: str, db: Any = None, *, force: bool = False
) -> UsageSnapshot | None:
    """The key's allowance: the stored snapshot when fresh, else a fresh one.

    ``force`` asks the endpoint whatever is stored and raises when it does
    not answer; without it a failure is logged, remembered for a minute, and
    the stored snapshot (or none) stands in.
    """
    if not api_key or not profile.get("usage_url"):
        return None
    # The failure memory and the lock share the snapshot's identity — this
    # endpoint under this key — so one refused key does not silence lookups
    # for another key, or for another endpoint.
    slot = _slot(endpoint_id, api_key)
    stored = load_usage(endpoint_id, api_key, db)

    def fresh_enough() -> bool:
        now = time.time()
        if stored is not None and now - stored.fetched_at < FRESH_SECONDS:
            return True
        return now - _failed_at.get(slot, 0.0) < FAILURE_SECONDS

    if not force and fresh_enough():
        return stored
    async with _locks[slot]:
        if not force:
            stored = load_usage(endpoint_id, api_key, db)
            if fresh_enough():
                return stored
        try:
            snapshot = await fetch_usage(profile, api_key)
        except UsageError as exc:
            _failed_at[slot] = time.time()
            if force:
                raise
            logger.warning("%s: %s", profile.get("label") or endpoint_id, exc)
            return stored
        _failed_at.pop(slot, None)
        save_usage(endpoint_id, api_key, snapshot, db)
        return snapshot


# ── Cards ───────────────────────────────────────────────────────────


def _card(
    *,
    endpoint_id: str,
    label: str,
    profile: dict,
    base_url: str,
    api_style: str,
    key_source: str,
    key_hint: str,
    editable: bool,
    is_default: bool,
    preset: str,
    usage: UsageSnapshot | None,
) -> dict[str, Any]:
    """One endpoint's card. Never includes the key, or anything derived from
    it beyond the last four characters, which is what tells two keys apart."""
    style = api_style if api_style in _PROTOCOLS else "chat"
    return {
        "id": endpoint_id,
        "label": label,
        "preset": preset,
        "description": profile.get("description") or "",
        "docs_url": profile.get("docs_url") or "",
        "endpoint": base_url,
        "key_source": key_source,  # "stored" | "env:<VAR>" | ""
        "key_hint": key_hint,
        "key_configured": bool(key_source),
        # Bare model ids go here (no signed-in account outranks it).
        "is_default": is_default,
        # A row in the database, as opposed to what the config file names.
        "editable": editable,
        "reports_usage": bool(profile.get("usage_url")),
        "protocol": {
            "api_style": style,
            "protocol": _PROTOCOLS[style],
            "transport": "HTTPS",
            "endpoint": base_url,
        },
        "usage": usage.to_dict() if usage else None,
        "available": usage.available() if usage else True,
    }


async def endpoint_card(
    endpoint: Endpoint, *, is_default: bool, db: Any = None, force_usage: bool = False
) -> dict[str, Any]:
    """The card for one stored endpoint, asking for its usage if it has any."""
    profile = endpoints.profile_for(endpoint)
    api_key = endpoints.key_for(endpoint, db)
    usage = await usage_for(endpoint.id, profile, api_key, db, force=force_usage)
    return _card(
        endpoint_id=endpoint.id,
        label=endpoint.label,
        profile=profile,
        base_url=endpoint.base_url,
        api_style=endpoint.api_style,
        key_source=endpoints.key_source(endpoint, db),
        key_hint=endpoints.key_hint(endpoint, db),
        editable=True,
        is_default=is_default,
        preset=endpoint.preset,
        usage=usage,
    )


async def config_card(config: LLMConfig | None, *, is_default: bool, db: Any = None) -> dict:
    """The card for the endpoint ``mira.yaml`` names, if it names one.

    Shown but not editable: the file is the authority for it, and a form
    that appeared to edit a file it cannot write would be a lie. Its usage
    is read the same way a stored endpoint's is.
    """
    profile = config_profile(config)
    if not profile or config is None:
        return {}
    api_key = config_key(config)
    env = config_key_env(config)
    usage = await usage_for(CONFIG_ID, profile, api_key, db)
    card = _card(
        endpoint_id=CONFIG_ID,
        label=profile.get("label") or "Endpoint from mira.yaml",
        profile=profile,
        base_url=config.base_url,
        api_style=config.api_style,
        # The environment is where this one's key comes from, always: it has
        # no row of its own to store one in.
        key_source=f"env:{env}" if api_key else "",
        key_hint=f"…{api_key[-4:]}" if len(api_key) >= 8 else "",
        editable=False,
        is_default=is_default,
        preset=profile.get("name") or "",
        usage=usage,
    )
    card["source"] = "config"
    return card


async def list_status(config: LLMConfig | None, db: Any = None) -> list[dict[str, Any]]:
    """Every endpoint the page shows: the stored ones, then the file's own.

    The default is whichever a bare model id reaches — a stored endpoint the
    dashboard selected, or the config file's when it selected none.
    """
    selected = endpoints.active(db)
    cards = [
        await endpoint_card(endpoint, is_default=endpoint.id == selected, db=db)
        for endpoint in endpoints.all_endpoints(db).values()
    ]
    from_config = await config_card(config, is_default=not selected, db=db)
    if from_config:
        cards.append(from_config)
    return cards


async def refresh(endpoint_id: str, config: LLMConfig | None, db: Any = None) -> dict[str, Any]:
    """Ask the provider now, record the answer, and return the card.

    Raises ``KeyError`` for an endpoint that is not on the page and
    :class:`UsageError` when the provider did not answer.
    """
    selected = endpoints.active(db)
    if endpoint_id == CONFIG_ID:
        profile = config_profile(config)
        if not profile:
            raise KeyError(endpoint_id)
        await usage_for(CONFIG_ID, profile, config_key(config), db, force=True)
        return await config_card(config, is_default=not selected, db=db)
    endpoint = endpoints.get(endpoint_id, db)
    if endpoint is None:
        raise KeyError(endpoint_id)
    return await endpoint_card(
        endpoint, is_default=endpoint.id == selected, db=db, force_usage=True
    )


def env_candidates() -> list[str]:
    """LLM key variables that are set in this process's environment.

    Offered by the form so a deployment that already has its key in the
    environment can point an endpoint at it instead of copying the secret
    into the database. Only names are reported, never values.
    """
    named = {
        str(profile.get("api_key_env"))
        for profile in profiles.labelled().values()
        if profile.get("api_key_env")
    }
    named.update({"OPENAI_API_KEY", "OPENROUTER_API_KEY", "ANTHROPIC_API_KEY"})
    return sorted(name for name in named if os.environ.get(name))
