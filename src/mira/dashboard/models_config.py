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


def resolve_oauth_provider(config: LLMConfig, db_value: str | None = None) -> str:
    """Resolve which OAuth session serves reviews: DB → config → none.

    A stored "" means "not set" (the same convention the model settings use),
    so clearing the dashboard's choice hands the decision back to mira.yaml
    rather than pinning it off forever.

    An id we do not recognise, or one whose session has been disconnected,
    resolves to "" — a stale pointer must degrade to the API-key path instead
    of failing every review with an unexplainable provider error.
    """
    from mira.oauth import registry, store

    chosen = (db_value or "").strip() or (config.oauth_provider or "").strip()
    if not chosen:
        return ""
    spec = registry.get(chosen)
    if spec is None or spec.llm is None:
        logger.warning("Ignoring unknown OAuth provider %r", chosen)
        return ""
    if store.load(spec.id) is None:
        logger.warning("OAuth provider %s is selected but not connected", spec.id)
        return ""
    return spec.id


def apply_oauth_binding(
    config: LLMConfig, provider_id: str, *, model_is_explicit: bool
) -> LLMConfig:
    """Point an LLMConfig at an OAuth provider's endpoint.

    The endpoint and protocol come from the provider spec, not from
    ``base_url``/``api_style``: those describe the API-key path, and leaving
    them in place would send an OAuth bearer token to OpenRouter.

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

    oauth_provider = resolve_oauth_provider(base, db_oauth)

    # Thinking mode only applies to reviews; other purposes leave it off.
    thinking_mode: str | None = None
    resolved_style = resolve_api_style(base, db_style)
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
        config = base.model_copy(update={"reasoning_effort": None, "api_style": resolved_style})
        if oauth_provider:
            config = apply_oauth_binding(
                config, oauth_provider, model_is_explicit=_model_is_explicit(base, None)
            )
        return config

    source = "dashboard setting" if db_model else ("mira.yaml" if config_model else "default")
    logger.info("%s model: %s (source: %s)", purpose.capitalize(), resolved, source)
    config = base.model_copy(
        update={"model": resolved, "reasoning_effort": thinking_mode, "api_style": resolved_style}
    )
    if oauth_provider:
        config = apply_oauth_binding(
            config,
            oauth_provider,
            model_is_explicit=_model_is_explicit(base, db_model or config_model),
        )
        logger.info("Reviewing through the %s OAuth session", oauth_provider)
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


def effective_model(
    base: LLMConfig, resolved: str, per_purpose: str | None, oauth_provider: str
) -> str:
    """The model id a call for this purpose will actually be made with.

    ``resolved`` is what the DB → config chain produced; with an OAuth session
    connected, the binding may still replace it (see :func:`apply_oauth_binding`).
    The dashboard has to report the same answer the review path computes, or the
    Models page names a model that no call will ever use — the mismatch is
    invisible until a review fails on an id the endpoint has never served.
    """
    if not oauth_provider:
        return resolved
    bound = apply_oauth_binding(
        base.model_copy(update={"model": resolved}),
        oauth_provider,
        model_is_explicit=_model_is_explicit(base, per_purpose),
    )
    return bound.model
