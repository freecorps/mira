"""Model resolution — reads from DB settings first, falls back to config.

Model lists, pricing, and capabilities all come from
``src/mira/llm/models.json`` via ``mira.llm.registry``. Add or remove a
model there; this file picks it up automatically.
"""

from __future__ import annotations

import logging

from mira.config import LLMConfig
from mira.llm import registry
from mira.oauth.base import LLMBinding

logger = logging.getLogger(__name__)

MODEL_PRICING: dict[str, tuple[float, float]] = {
    model_id: registry.pricing(model_id) for model_id in registry.all_models()
}

# Thinking-mode options for the review model. "off" disables extended thinking
# (today's behavior); low/medium/high map to OpenRouter's unified
# ``reasoning.effort``. Single source for the dashboard dropdown and validation.
THINKING_MODES: list[dict[str, str]] = [
    {"value": "off", "label": "Off"},
    {"value": "low", "label": "Low"},
    {"value": "medium", "label": "Medium"},
    {"value": "high", "label": "High"},
    # DeepSeek's top "max" level (sent as "xhigh" on OpenRouter, which rejects
    # "max"). Not every provider supports it.
    {"value": "max", "label": "Max"},
]
THINKING_MODE_VALUES = {m["value"] for m in THINKING_MODES}

# API-protocol options for the Models page. Single source for the dropdown
# and validation, mirroring THINKING_MODES.
API_STYLES: list[dict[str, str]] = [
    {"value": "chat", "label": "Chat Completions"},
    {"value": "responses", "label": "Responses API"},
]
API_STYLE_VALUES = {m["value"] for m in API_STYLES}


def resolve_api_style(config: LLMConfig, db_value: str | None = None) -> str:
    """Resolve the API protocol: DB → config.api_style → "chat"."""
    if db_value and db_value in API_STYLE_VALUES:
        return db_value
    return config.api_style if config.api_style in API_STYLE_VALUES else "chat"


def resolve_oauth_default(config: LLMConfig, db_value: str | None = None) -> tuple[str, str]:
    """Where a *bare* model id goes: ``(provider, account)``, or ``("", "")``.

    DB → config → none. The stored value is the Connections page's choice —
    ``"chatgpt"`` (any of that provider's accounts, rotating) or
    ``"chatgpt:<key>"`` (one of them). A stored "" means "not set" (the same
    convention the model settings use), so clearing the dashboard's choice
    hands the decision back to mira.yaml rather than pinning it off forever.

    An id we do not recognise, or one whose accounts have all been
    disconnected, resolves to none — a stale pointer must degrade to the
    API-key path instead of failing every review with an unexplainable
    provider error. A pinned account that is gone degrades one step less, to
    "any account", since the provider itself is still usable.
    """
    from mira.oauth import registry, store

    chosen = db_value.strip() if isinstance(db_value, str) else ""
    if chosen:
        provider, account = store.parse_ref(chosen)
    else:
        provider = (config.oauth_provider or "").strip()
        account = (config.oauth_account or "").strip()
    if not provider:
        return "", ""
    spec = registry.get(provider)
    if spec is None or spec.llm is None:
        logger.warning("Ignoring unknown OAuth provider %r", provider)
        return "", ""
    found = store.accounts(spec.id)
    if not found:
        logger.warning("OAuth provider %s is selected but not connected", spec.id)
        return "", ""
    if account and account not in found:
        logger.warning(
            "OAuth account %s:%s is selected but not connected; using any account",
            spec.id,
            account,
        )
        account = ""
    return spec.id, account


def resolve_oauth_provider(config: LLMConfig, db_value: str | None = None) -> str:
    """The provider bare model ids go to, or "" (see :func:`resolve_oauth_default`)."""
    return resolve_oauth_default(config, db_value)[0]


def apply_oauth_binding(
    config: LLMConfig,
    provider_id: str,
    *,
    model_is_explicit: bool,
    account: str = "",
) -> LLMConfig:
    """Point an LLMConfig at an OAuth provider's endpoint.

    The endpoint and protocol come from the provider spec, not from
    ``base_url``/``api_style``: those describe the API-key path, and leaving
    them in place would send an OAuth bearer token to OpenRouter. ``account``
    names one of the provider's signed-in accounts; empty lets the client
    rotate across all of them.

    A model somebody typed is sent as-is, since the dropdown is a guide and
    these endpoints accept ids the registry has never heard of. Two kinds of id
    are replaced by the provider's default instead:

    * the one nobody chose — Mira's built-in default is an OpenRouter-style
      Claude id this endpoint cannot serve, so inheriting it would turn
      "connect ChatGPT" into a 400 on the next pull request;
    * one belonging to a *different* vendor. This binding applies to indexing
      and the security sweep as well as reviews, and those two are usually
      pinned to a cheap model in mira.yaml — an `anthropic/…` id that would now
      be sent to an endpoint that has never served one. A vendor-prefixed id is
      the reliable tell: OpenRouter and Bedrock ids carry a prefix, the ids
      these accounts serve do not.
    """
    from mira.oauth import registry

    spec = registry.get(provider_id)
    if spec is None or spec.llm is None:
        return config
    update: dict = {
        "oauth_provider": spec.id,
        "oauth_account": account or None,
        "base_url": spec.llm.base_url,
        "api_style": spec.llm.api_style,
    }
    if spec.llm.default_model and (
        not model_is_explicit or _is_foreign_model(config.model, spec.llm)
    ):
        update["model"] = spec.llm.default_model
    return config.model_copy(update=update)


def _is_foreign_model(model: str, binding: LLMBinding) -> bool:
    """True when ``model`` plainly belongs to another backend, not this one.

    Only a vendor prefix counts. An unfamiliar bare id (``gpt-5.2-codex``, a
    model released after this build) is left alone — the point is to catch ids
    that provably came from somewhere else, not to reject everything the
    curated list does not already name.
    """
    if not model or "/" not in model:
        return False
    if any(model == option.get("value") for option in binding.models):
        return False
    logger.warning(
        "Model %r is not one %s serves; using %s instead",
        model,
        binding.base_url,
        binding.default_model,
    )
    return True


def bind_model(
    base: LLMConfig,
    value: str,
    *,
    model_is_explicit: bool,
    default: tuple[str, str],
    thinking_mode: str | None = None,
    api_style: str | None = None,
) -> LLMConfig:
    """The LLMConfig a call for ``value`` is made with.

    ``value`` is whatever the DB → config chain produced for a purpose: a
    bare model id, or a route naming its backend (see
    :mod:`mira.oauth.routes`). Three cases, in order:

    * ``api:<model>`` — the configured API-key endpoint, whatever the
      default backend is. This is how one purpose stays on a key while the
      others use a signed-in account.
    * ``oauth:<provider>:<account>:<model>`` — that account (or any of the
      provider's, for ``*``). The model is sent as written: the route was
      chosen from that account's own list, so there is nothing to second-guess.
    * a bare id — the default backend: the OAuth provider ``default`` names,
      or the API-key endpoint when it names none. The same rules as before
      apply to the model (see :func:`apply_oauth_binding`).
    """
    from mira.oauth.routes import parse_route

    route = parse_route(value)
    update: dict = {
        "reasoning_effort": thinking_mode,
        "api_style": api_style if api_style is not None else base.api_style,
    }
    if route is not None and route.backend == "api":
        update.update({"model": route.model, "oauth_provider": None, "oauth_account": None})
        return base.model_copy(update=update)
    if route is not None:
        update["model"] = route.model
        config = base.model_copy(update=update)
        bound = apply_oauth_binding(
            config,
            route.provider,
            model_is_explicit=True,
            account="" if route.rotates else route.account,
        )
        if bound is config:
            # The route named a backend that does not exist here. It stays an
            # OAuth binding all the same — the factory refuses it with a clear
            # error — rather than quietly billing the API key for a call the
            # operator pointed somewhere else on purpose.
            logger.warning("Model route %r names an OAuth provider that is not registered", value)
            return config.model_copy(
                update={
                    "oauth_provider": route.provider,
                    "oauth_account": None if route.rotates else route.account,
                }
            )
        return bound
    update["model"] = value
    config = base.model_copy(update=update)
    provider_id, account = default
    if provider_id:
        config = apply_oauth_binding(
            config, provider_id, model_is_explicit=model_is_explicit, account=account
        )
    return config


def describe_call(config: LLMConfig) -> dict:
    """Where a bound LLMConfig's calls go, in words the dashboard can show.

    Answers the question the Models page exists for: for the value that is
    selected, which backend, which account, which protocol, and which model
    id will actually be on the wire.
    """
    from mira.oauth import registry, store

    if config.oauth_provider:
        spec = registry.get(config.oauth_provider)
        if spec is not None and spec.llm is not None:
            account = config.oauth_account or ""
            found = store.accounts(spec.id)
            if account:
                tokens = found.get(account)
                account_label = tokens.account_label if tokens else f"{account} (disconnected)"
            elif len(found) == 1:
                only = next(iter(found.values()))
                account, account_label = only.account_key, only.account_label
            else:
                account_label = f"any of {len(found)} accounts (rotating)"
            return {
                "backend": "oauth",
                "provider": spec.id,
                "provider_label": spec.label,
                "account": account,
                "account_label": account_label,
                "model": config.model,
                "api_style": spec.llm.api_style,
                "protocol": spec.llm.protocol_label,
                "transport": spec.llm.transport_label,
                "endpoint": spec.llm.base_url,
                "connected": bool(found) and (not account or account in found),
            }
        # An explicit route to a provider this build does not know. Shown as
        # what it is — a dead OAuth route — not as the API key it will never use.
        return {
            "backend": "oauth",
            "provider": config.oauth_provider,
            "provider_label": f"{config.oauth_provider} (not a known provider)",
            "account": config.oauth_account or "",
            "account_label": config.oauth_account or "",
            "model": config.model,
            "api_style": "",
            "protocol": "",
            "transport": "",
            "endpoint": "",
            "connected": False,
        }
    if config.provider == "bedrock":
        return {
            "backend": "bedrock",
            "provider": "bedrock",
            "provider_label": "AWS Bedrock",
            "account": "",
            "account_label": "",
            "model": config.model,
            "api_style": "converse",
            "protocol": "Bedrock Converse API",
            "transport": "HTTPS",
            "endpoint": f"bedrock:{config.region}",
            "connected": True,
        }
    from mira.llm import provider_profiles as profiles

    profile = profiles.resolve(config.base_url)
    label = "OpenRouter" if profile.get("name") == "openrouter" else "API-key endpoint"
    style = config.api_style if config.api_style in API_STYLE_VALUES else "chat"
    return {
        "backend": "api",
        "provider": profile.get("name") or "openai-compatible",
        "provider_label": label,
        "account": "",
        "account_label": config.api_key_env or "",
        "model": config.model,
        "api_style": style,
        "protocol": next(s["label"] for s in API_STYLES if s["value"] == style),
        "transport": "HTTPS",
        "endpoint": config.base_url,
        "connected": True,
    }


def estimate_indexing_cost(file_count: int, model: str) -> dict:
    """Estimate cost of indexing N files with the given model.

    Based on actual indexer behavior:
    - Files batched 5-at-a-time
    - Each batch uses ~4K input tokens (prompt + 5 file contents ~500 lines avg)
    - Each batch outputs ~2K tokens (summaries + symbols JSON)
    - Plus a directory summarization pass at the end (~1 call per 10 files)
    """
    if file_count == 0:
        return {"estimated_usd": 0.0, "input_tokens": 0, "output_tokens": 0}

    input_price, output_price = MODEL_PRICING.get(model, (3.00, 15.00))

    # File summarization batches
    batches = (file_count + 4) // 5  # ceil div
    # Estimate: 800 tokens per file input, 400 tokens per file output
    input_tokens = file_count * 800 + batches * 500  # +prompt overhead per batch
    output_tokens = file_count * 400

    # Directory summarization pass
    dir_batches = max(1, file_count // 10)
    input_tokens += dir_batches * 1500
    output_tokens += dir_batches * 300

    cost = (input_tokens / 1_000_000) * input_price + (output_tokens / 1_000_000) * output_price

    return {
        "estimated_usd": round(cost, 2),
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
    }


def get_indexing_model(config: LLMConfig, db_value: str | None = None) -> str:
    """Resolve the indexing model: DB → config.indexing_model → config.model."""
    if db_value:
        return db_value
    if config.indexing_model:
        return config.indexing_model
    return config.model


def get_review_model(config: LLMConfig, db_value: str | None = None) -> str:
    """Resolve the review model: DB → config.review_model → config.model."""
    if db_value:
        return db_value
    if config.review_model:
        return config.review_model
    return config.model


def get_security_model(
    config: LLMConfig,
    db_value: str | None = None,
    db_review_model: str | None = None,
) -> str:
    """Resolve the security-pass model: DB → config.security_model → review tier.

    The review-tier fallback includes the dashboard's review_model setting
    (``db_review_model``) — without it, an instance whose review model lives
    only in the DB silently falls all the way back to ``config.model``.

    Never falls back to ``indexing_model`` — the security sweep is the
    highest-stakes cheap pass, and silently downgrading it to the indexing
    tier trades security recall for indexing cost savings.
    """
    if db_value:
        return db_value
    if config.security_model:
        return config.security_model
    return get_review_model(config, db_review_model)


def get_review_thinking_mode(config: LLMConfig, db_value: str | None = None) -> str | None:
    """Resolve the review thinking mode: DB → config.review_reasoning_effort → None.

    A DB value of "off" or "" counts as unset and falls through to the
    mira.yaml-level setting — saving the models form always writes this key
    (default "off"), so a stored "off" must not permanently shadow a config
    override. "off" anywhere normalizes to None ("no reasoning").
    """
    resolved = db_value if (db_value and db_value != "off") else config.review_reasoning_effort
    if not resolved or resolved == "off":
        return None
    return resolved


def llm_config_for(purpose: str, base: LLMConfig) -> LLMConfig:
    """Return an LLMConfig with the appropriate model set for the given purpose.

    Reads the DB setting first (via _app_db), falls back to config fields.
    Logs the effective model and where it came from, so a dashboard override
    shadowing mira.yaml is visible instead of silent (issue #124).

    The value may be a route as well as a model id — ``oauth:chatgpt:*:gpt-
    5-codex`` sends this purpose through a signed-in account whatever the
    others do, ``api:…`` keeps it on the key — see :func:`bind_model`.
    """
    db_model: str | None = None
    db_thinking: str | None = None
    db_review: str | None = None
    db_style: str | None = None
    db_oauth: str | None = None
    try:
        from mira.dashboard.api import _app_db

        if _app_db is not None:
            if purpose == "indexing":
                db_model = _app_db.get_setting("indexing_model")
            elif purpose == "review":
                db_model = _app_db.get_setting("review_model")
                db_thinking = _app_db.get_setting("review_thinking_mode")
            elif purpose == "security":
                db_model = _app_db.get_setting("security_model")
                db_thinking = _app_db.get_setting("review_thinking_mode")
                db_review = _app_db.get_setting("review_model")
            db_style = _app_db.get_setting("api_style")
            db_oauth = _app_db.get_setting("llm_oauth_provider")
    except Exception:
        pass  # DB not available — resolve from config fields alone

    default = resolve_oauth_default(base, db_oauth)
    resolved_style = resolve_api_style(base, db_style)

    # Thinking mode only applies to reviews; other purposes leave it off.
    thinking_mode: str | None = None
    if purpose == "indexing":
        resolved = get_indexing_model(base, db_model)
        config_model = base.indexing_model
    elif purpose == "security":
        resolved = get_security_model(base, db_model, db_review)
        config_model = base.security_model or base.review_model
        thinking_mode = get_review_thinking_mode(base, db_thinking)
    elif purpose == "review":
        resolved = get_review_model(base, db_model)
        config_model = base.review_model
        thinking_mode = get_review_thinking_mode(base, db_thinking)
    else:
        return bind_model(
            base,
            base.model,
            model_is_explicit=_model_is_explicit(base, None),
            default=default,
            api_style=resolved_style,
        )

    source = "dashboard setting" if db_model else ("mira.yaml" if config_model else "default")
    logger.info("%s model: %s (source: %s)", purpose.capitalize(), resolved, source)
    config = bind_model(
        base,
        resolved,
        model_is_explicit=_model_is_explicit(base, db_model or config_model),
        default=default,
        thinking_mode=thinking_mode,
        api_style=resolved_style,
    )
    if config.oauth_provider:
        logger.info(
            "%s calls go through the %s OAuth session (%s)",
            purpose.capitalize(),
            config.oauth_provider,
            config.oauth_account or "any account",
        )
    return config


def _model_is_explicit(base: LLMConfig, per_purpose: str | None) -> bool:
    """Did anyone actually choose this model, or is it just the built-in default?

    ``llm.model`` always has a value, so "did the user pick one" cannot be read
    off the resolved id alone — it is compared against the field's default.
    """
    if per_purpose:
        return True
    default = LLMConfig.model_fields["model"].default
    return base.model != default


def effective_route(
    base: LLMConfig, resolved: str, per_purpose: str | None, default: tuple[str, str]
) -> dict:
    """What a call for this purpose will actually do, for the Models page.

    ``resolved`` is what the DB → config chain produced. The answer carries
    ``value`` — the option the picker should show as selected, which is
    ``resolved`` unless the binding replaced its model — and the backend,
    account, protocol and model id the call will carry. The dashboard has to
    report the same answer the review path computes, or the Models page names
    a model that no call will ever use.
    """
    from mira.oauth.routes import parse_route

    bound = bind_model(
        base, resolved, model_is_explicit=_model_is_explicit(base, per_purpose), default=default
    )
    described = describe_call(bound)
    # A route is shown as written; a bare id shows whatever the binding
    # settled on (its default, when the id could not be sent).
    value = resolved if parse_route(resolved) is not None else bound.model
    return {"value": value, **described}


def effective_model(
    base: LLMConfig, resolved: str, per_purpose: str | None, oauth_provider: str
) -> str:
    """The picker value a call for this purpose will be made with.

    Kept for callers that only need the id; :func:`effective_route` is the
    full answer.
    """
    default = (oauth_provider, "") if oauth_provider else ("", "")
    return effective_route(base, resolved, per_purpose, default)["value"]
