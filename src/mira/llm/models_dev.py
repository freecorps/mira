"""models.dev — the model metadata OpenCode itself reads: reasoning levels and protocols.

Mira's thinking-mode dropdown was a fixed list (low, medium, high, max)
whatever the model, and the level went on the wire as written. But the
levels are the model's, not ours: GLM-5.3 Flash on OpenCode Go takes
``low``, ``high`` and ``max``; a GPT-5 takes ``none`` through ``xhigh``;
Kimi K2.7 Code takes no level at all. `models.dev <https://models.dev>`_
publishes exactly this — ``reasoning_options`` per provider and model — and
it is what the OpenCode client reads to offer its own variants, so reading
the same document puts the provider's levels in the picker and lets a
requested level be snapped to one the model has.

The document is a few megabytes and changes rarely, so it is fetched at
most once an hour and reduced on arrival to the one map this module
answers from: provider id → model id → the effort levels it takes. A
provider is matched by the preset an endpoint was built from (each preset
in ``providers.json`` names its models.dev id) or, failing that, by its
API URL. The ChatGPT backend is not here: it reports its own levels per
account, see ``mira.oauth.chatgpt``.

The same document says which protocol serves a model. A provider names
the SDK its models are called through (``npm``), and a model served some
other way names its own: on OpenCode Go most models answer on Chat
Completions, but Muse Spark and the GPT and Grok models only on the
Responses API, and a Chat Completions request for one of them is a 503.
Only those per-model exceptions are kept — see :func:`native_protocol`.

Lookups never touch the network — a request must not wait on a catalog —
so :func:`warm` is awaited where there is time to (before a call, on the
Models page) and the answer is whatever the last fetch said. Set
``MIRA_MODELS_DEV_URL`` to override the document's location, or to "" to
stay offline.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import Any

import httpx

logger = logging.getLogger(__name__)

_DEFAULT_URL = "https://models.dev/api.json"
_TTL = 3600.0
_RETRY = 60.0
_TIMEOUT = 10.0

# Reasoning levels from least to most, as providers name them across the
# board. Used to snap a requested level to the nearest one a model has.
EFFORT_ORDER: tuple[str, ...] = ("none", "minimal", "low", "medium", "high", "xhigh", "max")

# provider id → model id → effort levels ("" values dropped). A model listed
# with reasoning but no effort option maps to (); one the document does not
# name is absent.
_levels: dict[str, dict[str, tuple[str, ...]]] = {}
# provider id → API URL, for endpoints whose preset names no models.dev id.
_apis: dict[str, str] = {}
# provider id → model id → the protocol that model is served over, for the
# models whose own ``provider.npm`` differs from their provider's.
_protocols: dict[str, dict[str, str]] = {}

# The SDK a model is called through, as OpenCode names it, → the protocol it
# speaks. The first two are what Mira's ``api_style`` can be; the others are
# protocols Mira does not speak, kept so the picker can say so.
_NPM_PROTOCOLS = {
    "@ai-sdk/openai": "responses",
    "@ai-sdk/openai-compatible": "chat",
    "@ai-sdk/anthropic": "anthropic",
    "@ai-sdk/google": "google",
}
_fetched_at: float | None = None
_loaded = False
# Whether the last attempt failed. A failed refresh keeps the previous answer
# but is retried after _RETRY, not after the hour a success stands for.
_last_failed = False
_lock = asyncio.Lock()


def url() -> str:
    return os.environ.get("MIRA_MODELS_DEV_URL", _DEFAULT_URL)


def reduce_document(document: Any) -> tuple[dict[str, dict[str, tuple[str, ...]]], dict[str, str]]:
    """The two maps this module answers from, from the whole document."""
    levels: dict[str, dict[str, tuple[str, ...]]] = {}
    apis: dict[str, str] = {}
    if not isinstance(document, dict):
        return levels, apis
    for provider_id, provider in document.items():
        if not isinstance(provider, dict):
            continue
        api = provider.get("api")
        if isinstance(api, str) and api:
            apis[provider_id] = _norm_url(api)
        models = provider.get("models")
        if not isinstance(models, dict):
            continue
        known: dict[str, tuple[str, ...]] = {}
        for model_id, info in models.items():
            if not isinstance(info, dict):
                continue
            known[model_id] = _effort_levels(info)
        levels[provider_id] = known
    return levels, apis


def reduce_protocols(document: Any) -> dict[str, dict[str, str]]:
    """provider id → model id → protocol, for models served unlike their provider.

    A model with no ``provider.npm`` of its own, or one naming its
    provider's, is left out: the endpoint's protocol already serves it, and
    an endpoint the operator set to the Responses API stays there.
    """
    out: dict[str, dict[str, str]] = {}
    if not isinstance(document, dict):
        return out
    for provider_id, provider in document.items():
        if not isinstance(provider, dict) or not isinstance(provider.get("models"), dict):
            continue
        default = provider.get("npm")
        exceptions: dict[str, str] = {}
        for model_id, info in provider["models"].items():
            own = (info.get("provider") or {}) if isinstance(info, dict) else {}
            npm = own.get("npm") if isinstance(own, dict) else None
            if isinstance(npm, str) and npm and npm != default:
                exceptions[model_id] = _NPM_PROTOCOLS.get(npm, npm)
        if exceptions:
            out[provider_id] = exceptions
    return out


def _effort_levels(info: dict) -> tuple[str, ...]:
    out: list[str] = []
    for option in info.get("reasoning_options") or []:
        if not isinstance(option, dict) or option.get("type") != "effort":
            continue
        for value in option.get("values") or []:
            if isinstance(value, str) and value and value not in out:
                out.append(value)
    return tuple(out)


def _norm_url(value: str) -> str:
    return value.strip().rstrip("/").lower()


def load(document: Any) -> None:
    """Install ``document`` as the current answer (tests, or a bundled copy)."""
    global _fetched_at, _loaded, _last_failed
    _last_failed = False
    _levels.clear()
    _apis.clear()
    _protocols.clear()
    levels, apis = reduce_document(document)
    _levels.update(levels)
    _apis.update(apis)
    _protocols.update(reduce_protocols(document))
    _fetched_at = time.time()
    _loaded = True


def reset() -> None:
    """Forget everything (tests)."""
    global _fetched_at, _loaded, _last_failed
    _last_failed = False
    _levels.clear()
    _apis.clear()
    _protocols.clear()
    _fetched_at = None
    _loaded = False


def loaded() -> bool:
    return _loaded


async def warm() -> bool:
    """Fetch the document if what we have is stale. True when an answer is in hand.

    A failure is remembered for a minute so a dead network costs one attempt
    per minute, not one per call; a success stands for an hour.
    """
    global _fetched_at, _last_failed
    target = url()
    if not target:
        return _loaded
    if _fresh():
        return _loaded
    async with _lock:
        if _fresh():
            return _loaded
        try:
            async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
                resp = await client.get(target)
            resp.raise_for_status()
            load(resp.json())
            logger.info("Loaded reasoning levels for %d providers from models.dev", len(_levels))
        except Exception as exc:
            _fetched_at = time.time()
            _last_failed = True
            logger.warning("Could not load models.dev (%s: %s)", type(exc).__name__, exc)
        return _loaded


def _fresh() -> bool:
    """Is the last attempt recent enough not to try again yet?"""
    if _fetched_at is None:
        return False
    ttl = _RETRY if _last_failed or not _loaded else _TTL
    return time.time() - _fetched_at < ttl


def provider_id_for(preset: str = "", base_url: str = "") -> str:
    """The models.dev provider an endpoint is, or "" when it is not there.

    The preset's own id first (``providers.json`` names it); otherwise the
    endpoint's URL against the ids' API URLs, which catches a custom
    endpoint pointed at a known provider without its preset.
    """
    if preset:
        from mira.llm import provider_profiles as profiles

        named = profiles.get(preset)
        mapped = (named or {}).get("models_dev") if named else ""
        if isinstance(mapped, str) and mapped:
            return mapped
    if base_url:
        wanted = _norm_url(base_url)
        for provider_id, api in _apis.items():
            if api == wanted:
                return provider_id
    return ""


def levels_for(provider_id: str, model: str) -> tuple[str, ...] | None:
    """What ``model`` takes on ``provider_id``: levels, () for none, None if unknown.

    A vendor-prefixed id is tried as written and then bare, and the other
    way round, since OpenRouter keys by ``vendor/model`` and the rest by the
    bare name.
    """
    known = _levels.get(provider_id)
    if known is None or not model:
        return None
    candidates = [model]
    if "/" in model:
        candidates.append(model.split("/", 1)[1])
    for candidate in candidates:
        if candidate in known:
            return known[candidate]
    if "/" not in model:
        # A bare id against a catalogue keyed by ``vendor/model``: taken only
        # when every prefixed entry of that name agrees, since two vendors
        # serving one name may take different levels.
        found = {levels for key, levels in known.items() if key.rsplit("/", 1)[-1] == model}
        if len(found) == 1:
            return found.pop()
    return None


def native_protocol(provider_id: str, model: str) -> str | None:
    """The protocol ``model`` is served over on ``provider_id``, when it is not its provider's.

    ``"responses"`` or ``"chat"`` for the two Mira speaks, the SDK's own name
    (``"anthropic"``, ``"google"``) for one it does not, None when the model
    is served like the rest of its provider or the catalogue does not know it.
    """
    known = _protocols.get(provider_id)
    if not known or not model:
        return None
    return known.get(model) or (known.get(model.split("/", 1)[1]) if "/" in model else None)


def endpoint_provider_id(config: Any) -> str:
    """The models.dev provider a config's endpoint is, or ""."""
    from mira.llm import endpoints

    try:
        profile = endpoints.profile_for_config(config)
    except Exception:  # pragma: no cover - a config with no URL
        return ""
    preset = str(profile.get("name") or "")
    return provider_id_for(preset, str(getattr(config, "base_url", "") or ""))


def reasoning_levels(config: Any, model: str = "") -> tuple[str, ...] | None:
    """The levels ``model`` (default: the config's) takes on the config's endpoint."""
    from mira.llm import endpoints

    try:
        profile = endpoints.profile_for_config(config)
    except Exception:  # pragma: no cover - a config with no URL
        return None
    preset = str(profile.get("name") or "")
    provider_id = provider_id_for(preset, str(getattr(config, "base_url", "") or ""))
    if not provider_id:
        return None
    return levels_for(provider_id, model or str(getattr(config, "model", "") or ""))


def snap_effort(levels: tuple[str, ...] | list[str], effort: str) -> str:
    """The level to send when ``effort`` was asked for and the model takes ``levels``.

    The requested level when the model has it; else the highest it has
    below it — "max" on a model that stops at "high" is "high", not a 400;
    else the lowest it has. A level outside the known scale is sent as
    written, since it may be one this provider alone spells that way.
    """
    if not levels or effort in levels:
        return effort
    if effort not in EFFORT_ORDER:
        return effort
    rank = EFFORT_ORDER.index(effort)
    for candidate in reversed(EFFORT_ORDER[:rank]):
        if candidate in levels:
            return candidate
    for candidate in EFFORT_ORDER:
        if candidate in levels:
            return candidate
    return effort
