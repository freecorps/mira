"""Endpoints configured from the dashboard: URL, protocol and key.

Mira has always read its LLM endpoint from ``mira.yaml`` and its key from the
environment. That is the right default for a deployment somebody owns the
file and the process of — and the wrong one for a container, a hosted image
or a proxy somebody else runs, where changing either means a rebuild or a
restart to answer a question as small as "which key".

So an endpoint can also be a row in Mira's own database: a label, a URL, a
protocol, and either a key stored here or the name of an environment variable
to read one from. Several can be configured; one of them is the default that
bare model ids go to, the same way one signed-in account is
(:mod:`mira.oauth.store`). Nothing is required — with no endpoint stored, the
config file and the environment decide exactly as before.

A stored endpoint starts from a **preset**: an entry in ``providers.json``
(OpenRouter, OpenCode Go, Ollama, …) that supplies the URL to prefill and the
quirks the endpoint needs — its model-prefix policy, the headers it wants,
the reasoning levels it spells differently, and where it reports usage. The
preset is what the endpoint *is*; the stored row is where that install points
it and what it opens it with. An endpoint with no preset is a plain
OpenAI-compatible URL, which is what most of them are.

Two rules the rest of the codebase leans on:

* **The key is not part of the endpoint.** It lives in its own row, and
  :class:`Endpoint` has no field for it, so no route can return one by
  forgetting to leave it out. :func:`key_for` is the only way to read it.
* **A URL from here is checked like a URL from the config file.** The same
  validator runs on both, so an endpoint typed into a browser cannot send a
  key somewhere ``mira.yaml`` would have been refused for.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from dataclasses import asdict, dataclass, field
from typing import Any

from mira.config import validate_base_url
from mira.llm import provider_profiles as profiles

logger = logging.getLogger(__name__)

_PREFIX = "llm_endpoint:"
_KEY_PREFIX = "llm_endpoint_key:"
# Which stored endpoint serves calls that do not name a backend. "" = none,
# leaving the config file's own endpoint in charge.
ACTIVE_KEY = "llm_endpoint_active"

API_STYLES = ("chat", "responses")
MODEL_PREFIXES = ("strip", "keep")

# The id the Connections page gives the endpoint ``mira.yaml`` names. It has
# no row here, so no stored endpoint may take the name either — two cards
# with one id would make "refresh this one" ambiguous.
CONFIG_ID = "config"
# …along with the words that are routes of their own under /api/providers,
# which a stored endpoint of the same id would shadow.
RESERVED_IDS = frozenset({CONFIG_ID, "active", "test"})

_SLUG_CHARS = re.compile(r"[^a-z0-9-]+")
_ID_MAX = 40


class EndpointError(Exception):
    """A stored endpoint could not be written, or does not exist."""


@dataclass
class Endpoint:
    """One endpoint an operator configured. Never holds the key."""

    id: str
    label: str
    base_url: str
    # The ``providers.json`` entry this was built from, or "" for a plain
    # OpenAI-compatible endpoint with no quirks.
    preset: str = ""
    api_style: str = "chat"
    # Read the key from this environment variable instead of storing one
    # here. "" means the stored key (or no key at all).
    api_key_env: str = ""
    model_prefix: str = ""
    # The model to fall back to when the one in force plainly belongs to
    # another backend. Filled in from the registry for a preset it knows
    # models for; empty for an endpoint nothing is known about, where
    # replacing somebody's model id would be a guess.
    default_model: str = ""
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Any) -> Endpoint | None:
        if not isinstance(data, dict) or not data.get("id") or not data.get("base_url"):
            return None
        known = set(cls.__dataclass_fields__)
        return cls(**{k: v for k, v in data.items() if k in known})


# ── Presets ─────────────────────────────────────────────────────────


def presets() -> dict[str, dict]:
    """The entries an endpoint can be started from, keyed by name."""
    return profiles.labelled()


def preset_options() -> list[dict[str, Any]]:
    """The preset list for the dashboard's form, plus the blank one.

    "Custom" is first and deliberate: an endpoint that is not one of these is
    the ordinary case (a proxy, a gateway, a model server), not a fallback.
    """
    options = [
        {
            "id": "",
            "label": "Custom endpoint",
            "description": "Any OpenAI-compatible URL — a proxy, a gateway, or a model server.",
            "docs_url": "",
            "base_url": "",
            "api_key_env": "",
            "api_style": "chat",
            "reports_usage": False,
        }
    ]
    for name, profile in presets().items():
        options.append(
            {
                "id": name,
                "label": profile["label"],
                "description": profile.get("description") or "",
                "docs_url": profile.get("docs_url") or "",
                "base_url": profile.get("base_url") or "",
                "api_key_env": profile.get("api_key_env") or "",
                "api_style": profile.get("api_style") or "chat",
                "reports_usage": bool(profile.get("usage_url")),
            }
        )
    return options


def recommended_model(preset: str, purpose: str = "review") -> str:
    """The registry's pick for a preset's endpoint, or "" if it names none.

    Only an entry pinned to this preset counts: the rest of the registry is
    OpenRouter-style ids, and offering one here would name a model the
    endpoint has never served.
    """
    if not preset:
        return ""
    from mira.llm import registry

    fallback = ""
    for model_id, info in registry.all_models().items():
        if info.get("endpoint") != preset:
            continue
        if purpose in (info.get("recommended_for") or []):
            return model_id
        if not fallback and purpose in (info.get("purposes") or []):
            fallback = model_id
    return fallback


def profile_for(endpoint: Endpoint) -> dict:
    """The quirk table a stored endpoint's calls are made with.

    The preset supplies the quirks; the row supplies where this install
    points them. A preset whose URL was changed — OpenCode Go behind a proxy
    — keeps its headers and its session header, which is the whole reason the
    preset is recorded rather than inferred back from the URL.
    """
    base = profiles.get(endpoint.preset) if endpoint.preset else None
    if base is None:
        base = dict(profiles.DEFAULT_PROFILE)
        if endpoint.preset:
            logger.warning(
                "Endpoint %s names preset %r, which this build does not have; "
                "treating it as a plain OpenAI-compatible endpoint",
                endpoint.id,
                endpoint.preset,
            )
    profile = {**base, "base_url": endpoint.base_url, "api_style": endpoint.api_style}
    if endpoint.model_prefix in MODEL_PREFIXES:
        profile["model_prefix"] = endpoint.model_prefix
    # The usage endpoint belongs to the provider, not to this install's URL:
    # a preset pointed at a proxy still reports its allowance where it always
    # did, and an endpoint with no preset has nothing to report.
    profile["endpoint_id"] = endpoint.id
    profile["label"] = endpoint.label or base.get("label") or endpoint.id
    return profile


def profile_for_config(config: Any) -> dict:
    """The quirk table for a bound config: its stored endpoint, or its URL."""
    endpoint_id = getattr(config, "endpoint", None)
    if endpoint_id:
        endpoint = get(endpoint_id)
        if endpoint is not None:
            return profile_for(endpoint)
        logger.warning("Endpoint %r is configured but not stored; using its URL", endpoint_id)
    return profiles.resolve(getattr(config, "base_url", "") or "")


# ── Storage ─────────────────────────────────────────────────────────


def _db(db: Any) -> Any:
    if db is not None:
        return db
    from mira.oauth.store import default_db

    return default_db()


def _require_db(db: Any) -> Any:
    store = _db(db)
    if store is None:
        raise EndpointError("No dashboard database available to store an endpoint")
    return store


def slugify(text: str, taken: set[str]) -> str:
    """A short, unique, url-safe id for a label ("OpenCode Go" → "opencode-go")."""
    slug = _SLUG_CHARS.sub("-", (text or "").strip().lower()).strip("-")[:_ID_MAX]
    slug = slug or "endpoint"
    if slug not in taken:
        return slug
    for n in range(2, 1000):
        candidate = f"{slug[: _ID_MAX - len(str(n)) - 1]}-{n}"
        if candidate not in taken:
            return candidate
    raise EndpointError("Could not derive a free id for this endpoint")


def all_endpoints(db: Any = None) -> dict[str, Endpoint]:
    """Every stored endpoint, oldest first."""
    store = _db(db)
    if store is None:
        return {}
    found = []
    for key, raw in store.list_settings(_PREFIX).items():
        try:
            endpoint = Endpoint.from_dict(json.loads(raw))
        except json.JSONDecodeError:
            logger.warning("Ignoring unreadable endpoint row %s", key)
            continue
        if endpoint is not None:
            found.append(endpoint)
    found.sort(key=lambda e: (e.created_at, e.id))
    return {e.id: e for e in found}


def name_of(value: Any) -> str:
    """The endpoint id a value names, or "" if it names none.

    ``llm.endpoint`` is read straight off a config object, and a config is
    not always the real thing — a test hands over a stand-in, and a stale
    deployment may hold whatever an older build wrote. Anything that is not
    a string names no endpoint, rather than being concatenated into a
    settings key and raising halfway through a review.
    """
    return value.strip() if isinstance(value, str) else ""


def get(endpoint_id: Any, db: Any = None) -> Endpoint | None:
    """One stored endpoint, or None."""
    store = _db(db)
    endpoint_id = name_of(endpoint_id)
    if store is None or not endpoint_id:
        return None
    raw = store.get_setting(_PREFIX + endpoint_id)
    if not raw:
        return None
    try:
        return Endpoint.from_dict(json.loads(raw))
    except json.JSONDecodeError:
        logger.warning("Endpoint %s is stored unreadably", endpoint_id)
        return None


def require(endpoint_id: str, db: Any = None) -> Endpoint:
    endpoint = get(endpoint_id, db)
    if endpoint is None:
        raise EndpointError(f"No endpoint {endpoint_id!r} is configured")
    return endpoint


def save(endpoint: Endpoint, db: Any = None) -> Endpoint:
    """Write an endpoint, validating what a call would be made with."""
    store = _require_db(db)
    endpoint.base_url = validate_base_url(endpoint.base_url.strip(), "The endpoint URL")
    endpoint.label = (endpoint.label or "").strip() or endpoint.id
    if endpoint.api_style not in API_STYLES:
        raise EndpointError(f"Protocol must be one of {', '.join(API_STYLES)}")
    if endpoint.model_prefix and endpoint.model_prefix not in MODEL_PREFIXES:
        raise EndpointError(f"Model prefix must be one of {', '.join(MODEL_PREFIXES)}")
    if endpoint.preset and endpoint.preset not in presets():
        raise EndpointError(f"Unknown preset {endpoint.preset!r}")
    endpoint.api_key_env = (endpoint.api_key_env or "").strip()
    if not endpoint.default_model:
        endpoint.default_model = recommended_model(endpoint.preset)
    endpoint.updated_at = time.time()
    store.set_setting(_PREFIX + endpoint.id, json.dumps(endpoint.to_dict()))
    return endpoint


def create(
    *,
    label: str,
    base_url: str,
    preset: str = "",
    api_style: str = "",
    api_key_env: str = "",
    model_prefix: str = "",
    db: Any = None,
) -> Endpoint:
    """Add an endpoint, deriving its id from the label."""
    store = _require_db(db)
    profile = presets().get(preset) if preset else None
    endpoint = Endpoint(
        id=slugify(
            label or (profile or {}).get("label", "") or "endpoint",
            set(all_endpoints(store)) | RESERVED_IDS,
        ),
        label=label,
        base_url=base_url or (profile or {}).get("base_url", ""),
        preset=preset,
        api_style=api_style or (profile or {}).get("api_style", "") or "chat",
        api_key_env=api_key_env,
        model_prefix=model_prefix,
    )
    return save(endpoint, store)


def delete(endpoint_id: str, db: Any = None) -> None:
    """Forget an endpoint, its key, its usage, and the default if it was one."""
    store = _require_db(db)
    store.delete_setting(_PREFIX + endpoint_id)
    store.delete_setting(_KEY_PREFIX + endpoint_id)
    if active(store) == endpoint_id:
        set_active("", store)
    from mira.llm import key_providers

    key_providers.forget_usage(endpoint_id, store)


# ── The key ─────────────────────────────────────────────────────────


def set_secret(endpoint_id: str, api_key: str, db: Any = None) -> None:
    """Store (or clear) the key an endpoint's requests carry.

    Its own row, so nothing that reads an endpoint reads a key by accident.
    """
    store = _require_db(db)
    text = (api_key or "").strip()
    if text:
        store.set_setting(_KEY_PREFIX + endpoint_id, text)
    else:
        store.delete_setting(_KEY_PREFIX + endpoint_id)


def secret(endpoint_id: str, db: Any = None) -> str:
    """The stored key for an endpoint, or ""."""
    store = _db(db)
    if store is None or not endpoint_id:
        return ""
    return (store.get_setting(_KEY_PREFIX + endpoint_id) or "").strip()


def has_secret(endpoint_id: str, db: Any = None) -> bool:
    return bool(secret(endpoint_id, db))


def key_for(endpoint: Endpoint, db: Any = None) -> str:
    """The key this endpoint's calls carry: stored, else from its variable.

    Never raises and never guesses at another endpoint's variable: an
    endpoint configured with neither a stored key nor a readable variable
    needs no key as far as anybody said, and a provider that disagrees says
    so with a 401 naming itself.
    """
    stored = secret(endpoint.id, db)
    if stored:
        return stored
    if endpoint.api_key_env:
        return os.environ.get(endpoint.api_key_env, "")
    preset = presets().get(endpoint.preset) if endpoint.preset else None
    if preset and preset.get("api_key_env"):
        return os.environ.get(preset["api_key_env"], "")
    return ""


def key_source(endpoint: Endpoint, db: Any = None) -> str:
    """Where the key comes from: "stored", "env:<VAR>", or "" for none."""
    if has_secret(endpoint.id, db):
        return "stored"
    env = endpoint.api_key_env
    if not env and endpoint.preset:
        env = (presets().get(endpoint.preset) or {}).get("api_key_env", "")
    if env and os.environ.get(env):
        return f"env:{env}"
    return ""


def key_hint(endpoint: Endpoint, db: Any = None) -> str:
    """The last four characters of the key, or "" — enough to tell two apart."""
    key = key_for(endpoint, db)
    return f"…{key[-4:]}" if len(key) >= 8 else ""


# ── The default ─────────────────────────────────────────────────────


def active(db: Any = None) -> str:
    """The endpoint bare model ids go to, or "" for the config file's own.

    An id whose endpoint has since been deleted reads as "": a stale pointer
    must fall back to the configured endpoint rather than fail every review.
    """
    store = _db(db)
    if store is None:
        return ""
    chosen = (store.get_setting(ACTIVE_KEY) or "").strip()
    if not chosen:
        return ""
    if get(chosen, store) is None:
        logger.warning("Endpoint %r is selected but not stored; using the configured one", chosen)
        return ""
    return chosen


def set_active(endpoint_id: str, db: Any = None) -> None:
    """Choose which stored endpoint serves bare model ids ("" = the config)."""
    store = _require_db(db)
    if endpoint_id and get(endpoint_id, store) is None:
        raise EndpointError(f"No endpoint {endpoint_id!r} is configured")
    store.set_setting(ACTIVE_KEY, endpoint_id)
