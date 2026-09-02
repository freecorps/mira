"""Dynamic model catalog — lists models from the configured backend.

The static registry (llm/models.json) uses OpenRouter-style ids, so a
deployment on Bedrock or a generic OpenAI-compatible endpoint would be
offered ids its backend can't serve. Detect the active backend, fetch its
live model list (cached for an hour; failures for a minute), and fall back
to the backend-filtered registry when the fetch fails.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import defaultdict
from typing import Any

import httpx

from mira.config import LLMConfig
from mira.llm import provider_profiles as profiles
from mira.llm import registry
from mira.llm.base import _get_api_key

logger = logging.getLogger(__name__)

_CATALOG_TTL = 3600.0
_FAILURE_TTL = 60.0
_cache: dict[str, tuple[float, list[dict] | None]] = {}
_locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
# Per-account model lists from OAuth providers that can be asked for one:
# ``{provider:account: (fetched_at, options, from_provider)}``. Same TTLs as
# the key-based catalog — an hour for the provider's own list, a minute for
# the curated fallback that stands in when the provider did not answer.
_account_model_cache: dict[str, tuple[float, list[dict], bool]] = {}


# Prefix marking a backend served by a signed-in account rather than a key.
OAUTH_BACKEND_PREFIX = "oauth:"


def active_backend(config: LLMConfig) -> str:
    """Return ``"oauth:<id>"``, "bedrock", "openrouter", or "openai-compatible"."""
    if config.oauth_provider:
        return f"{OAUTH_BACKEND_PREFIX}{config.oauth_provider}"
    if config.provider == "bedrock":
        return "bedrock"
    profile = profiles.resolve(config.base_url)
    return "openrouter" if profile.get("name") == "openrouter" else "openai-compatible"


def _oauth_models(backend: str) -> list[dict] | None:
    """The curated model list for an OAuth backend, or None if not one."""
    if not backend.startswith(OAUTH_BACKEND_PREFIX):
        return None
    from mira.oauth import registry

    spec = registry.get(backend[len(OAUTH_BACKEND_PREFIX) :])
    if spec is None or spec.llm is None:
        return []
    return [dict(m) for m in spec.llm.models]


def _norm(model_id: str) -> str:
    # OpenRouter serves dash and dot forms of the same id as aliases
    # (anthropic/claude-haiku-4-5 == anthropic/claude-haiku-4.5).
    return model_id.lower().replace(".", "-")


async def _fetch_openai_style(config: LLMConfig, tools_only: bool) -> list[dict]:
    """GET {base_url}/models. With tools_only (OpenRouter), keep only
    tool-calling models — Mira's review pass needs tool calling."""
    headers = {}
    try:
        key = _get_api_key(config)
    except Exception as exc:
        logger.warning("Could not retrieve API key for model catalog fetch: %s", exc)
        key = ""
    if key:
        headers["Authorization"] = f"Bearer {key}"
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.get(f"{config.base_url.rstrip('/')}/models", headers=headers)
    resp.raise_for_status()
    out = []
    for m in resp.json().get("data", []):
        if tools_only and "tools" not in (m.get("supported_parameters") or []):
            continue
        out.append({"value": m["id"], "label": m.get("name") or m["id"]})
    return out


def _fetch_bedrock_sync(config: LLMConfig) -> list[dict]:
    import boto3
    from botocore.config import Config as BotoConfig

    session = boto3.Session(profile_name=config.aws_profile, region_name=config.region)
    client = session.client(
        "bedrock",
        config=BotoConfig(connect_timeout=5, read_timeout=15, retries={"max_attempts": 1}),
    )
    out = []
    for p in client.list_inference_profiles().get("inferenceProfileSummaries", []):
        out.append(
            {
                "value": p["inferenceProfileId"],
                "label": p.get("inferenceProfileName") or p["inferenceProfileId"],
            }
        )
    models = client.list_foundation_models(byOutputModality="TEXT", byInferenceType="ON_DEMAND")
    for m in models.get("modelSummaries", []):
        out.append({"value": m["modelId"], "label": m.get("modelName") or m["modelId"]})
    return out


async def fetch_catalog(config: LLMConfig) -> list[dict] | None:
    """Live ``[{value, label}]`` list for the active backend, or None if
    unavailable (no network, no boto3, no credentials, ...).

    Successes are cached for an hour, failures for a minute — the settings
    page must not re-block on a dead endpoint on every load. A per-key lock
    coalesces concurrent cold-cache fetches (two tabs, the setup modal poll).
    """
    backend = active_backend(config)
    # An OAuth backend serves a fixed, curated list — there is no catalog
    # endpoint to call, and no key to call it with.
    oauth = _oauth_models(backend)
    if oauth is not None:
        return oauth
    if backend == "bedrock":
        cache_key = f"bedrock:{config.region}:{config.aws_profile or ''}"
    else:
        cache_key = config.base_url

    def cached() -> tuple[float, list[dict] | None] | None:
        hit = _cache.get(cache_key)
        if hit is None:
            return None
        ttl = _CATALOG_TTL if hit[1] is not None else _FAILURE_TTL
        return hit if time.time() - hit[0] < ttl else None

    if (hit := cached()) is not None:
        return hit[1]
    async with _locks[cache_key]:
        if (hit := cached()) is not None:
            return hit[1]
        try:
            if backend == "bedrock":
                models = await asyncio.to_thread(_fetch_bedrock_sync, config)
            elif backend == "openrouter":
                models = await _fetch_openai_style(config, tools_only=True)
            else:
                models = await _fetch_openai_style(config, tools_only=False)
        except Exception as exc:
            logger.warning("Model catalog fetch failed (%s): %s", backend, exc)
            models = None
        _cache[cache_key] = (time.time(), models)
        return models


def endpoint_host(url: str) -> str:
    """``https://chatgpt.com/backend-api/codex`` → ``chatgpt.com/backend-api/codex``."""
    from urllib.parse import urlsplit

    parts = urlsplit(url or "")
    if not parts.netloc:
        return url or ""
    return f"{parts.netloc}{parts.path}".rstrip("/")


async def account_models(spec: Any, tokens: Any, db: Any = None) -> list[dict]:
    """The models one signed-in account may use, from the provider if it can say.

    Asked once an hour per account. When the provider has no such endpoint,
    or it did not answer, the spec's curated list stands in — and the miss is
    not cached, so a transient failure costs one page load, not an hour of a
    frozen list.
    """
    from mira.oauth import store

    # The entry is tied to this grant, not just the account: signing in again
    # under the same key, or a grant that now carries another plan, must not
    # keep serving the list the previous grant was entitled to.
    slot = f"{spec.id}:{tokens.account_key}:"
    key = f"{slot}{tokens.obtained_at}:{tokens.plan}"

    def remember(options: list[dict], from_provider: bool) -> None:
        for stale in [k for k in _account_model_cache if k.startswith(slot) and k != key]:
            del _account_model_cache[stale]
        _account_model_cache[key] = (time.time(), options, from_provider)

    def cached() -> list[dict] | None:
        hit = _account_model_cache.get(key)
        if hit is None:
            return None
        # A miss (the curated list standing in) is kept only briefly: long
        # enough that one page load — which asks for this account several
        # times — does not hit a dead endpoint repeatedly, short enough that
        # a transient failure does not freeze the list for an hour.
        ttl = _CATALOG_TTL if hit[2] else _FAILURE_TTL
        return hit[1] if time.time() - hit[0] < ttl else None

    if (found := cached()) is not None:
        return found
    async with _locks[f"oauth-models:{key}"]:
        if (found := cached()) is not None:
            return found
        models: list[dict] | None = None
        try:
            fresh = await store.valid_tokens(spec.id, tokens.account_key, db)
            models = await spec.fetch_models(fresh)
        except Exception as exc:
            logger.warning(
                "Could not list %s models for %s: %s",
                spec.label,
                tokens.account_label or tokens.account_key,
                exc,
            )
        if models is None:
            fallback = [{"recommended": False, **dict(m)} for m in spec.llm.models]
            remember(fallback, False)
            return fallback
        default = spec.llm.default_model
        options = [
            {
                "recommended": m.get("value") == default or bool(m.get("recommended")),
                **m,
            }
            for m in models
        ]
        remember(options, True)
        return options


async def provider_models(spec: Any, accounts: dict[str, Any], db: Any = None) -> list[dict]:
    """The models every one of the accounts can serve, in the first's order.

    What "any account" can be asked for: rotation picks an account by
    allowance, not by model, so a model only some accounts have would be sent
    to one that lacks it and fail there. Such a model is still reachable —
    through that account's own group, which pins it.
    """
    lists = [await account_models(spec, tokens, db) for tokens in accounts.values()]
    if not lists:
        return []
    common = set.intersection(*(({o["value"] for o in options}) for options in lists))
    return [o for o in lists[0] if o["value"] in common]


async def oauth_option_groups(default: tuple[str, str], db: Any = None) -> list[dict]:
    """Explicit-route options for every signed-in account, grouped by account.

    Every option's value is a route (``oauth:chatgpt:<key>:<model>``), so
    picking it says which account as well as which model — the whole point,
    when two backends serve a model of the same name. A provider with more
    than one account also gets an "any account" group whose routes rotate;
    it is left out when that provider is already the default for bare ids,
    since the bare group above it then says the same thing.
    """
    from mira.oauth import registry, store
    from mira.oauth.routes import oauth_route

    out: list[dict] = []
    for provider_id, spec in registry.llm_providers().items():
        accounts = store.accounts(provider_id, db)
        if not accounts or spec.llm is None:
            continue
        protocol = spec.llm.describe()
        detail = f"{protocol['protocol']} · {endpoint_host(protocol['endpoint'])}"
        if len(accounts) > 1 and default != (provider_id, ""):
            group = f"{spec.label} · any account (rotate across {len(accounts)})"
            for option in await provider_models(spec, accounts, db):
                out.append(
                    {
                        **option,
                        "value": oauth_route(provider_id, "*", option["value"]),
                        "group": group,
                        "detail": detail,
                    }
                )
        for key, tokens in accounts.items():
            who = tokens.account_label or key
            plan = f" · {tokens.plan}" if tokens.plan else ""
            group = f"{spec.label} · {who}{plan}"
            for option in await account_models(spec, tokens, db):
                out.append(
                    {
                        **option,
                        "value": oauth_route(provider_id, key, option["value"]),
                        "group": group,
                        "detail": detail,
                    }
                )
    return out


def build_options(backend: str, dynamic: list[dict] | None, purpose: str) -> list[dict]:
    """Dropdown options for ``purpose``: registry entries matching the backend
    (carrying the recommended flags) merged with the dynamic catalog.

    Dynamic-only models have unknown capabilities, so they're offered for both
    purposes. On a generic endpoint only its own list is trustworthy — registry
    ids are OpenRouter-style — so the registry is used there only as fallback.
    """
    if backend.startswith(OAUTH_BACKEND_PREFIX):
        # The provider spec's own list, in its own order: it is short, curated,
        # and already says which model to reach for first.
        return [{"recommended": False, **d} for d in (dynamic or [])]

    if backend == "openai-compatible" and dynamic is not None:
        options = [{**d, "recommended": False} for d in dynamic]
        options.sort(key=lambda m: m["label"].lower())
        return options

    wants_bedrock = backend == "bedrock"
    options = []
    for model_id, info in registry.all_models().items():
        if (info.get("provider") == "bedrock") != wants_bedrock:
            continue
        if purpose not in (info.get("purposes") or []):
            continue
        options.append(
            {
                "value": model_id,
                "label": info.get("label", model_id),
                "recommended": purpose in (info.get("recommended_for") or []),
            }
        )
    if dynamic is not None:
        seen = {_norm(o["value"]) for o in options}
        options += [{**d, "recommended": False} for d in dynamic if _norm(d["value"]) not in seen]
    options.sort(key=lambda m: (not m["recommended"], m["label"].lower()))
    return options
