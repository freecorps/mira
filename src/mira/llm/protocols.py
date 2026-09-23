"""Which protocol a call to a model goes out on: the endpoint's, unless the model's differs.

An endpoint has one protocol — Chat Completions or the Responses API — and
until now every model on it was called that way. A gateway can serve its
models over more than one: OpenCode Go answers most on Chat Completions,
but Muse Spark and its GPT and Grok models only on the Responses API, and a
Chat Completions request for one of them comes back ``503 Endpoint is
unavailable``. The picker offered them anyway, so choosing one failed every
review.

Two sources say when a model is the exception, in order:

* its registry entry (``models.json``) naming an ``api_style`` — curated, and
  in hand without the network;
* models.dev, where a model served unlike its provider names its own SDK
  (see :func:`mira.llm.models_dev.native_protocol`).

Only an exception moves a call. A model neither source singles out is
called the way its endpoint is, so an endpoint the operator set to the
Responses API stays there. A model served over a protocol Mira does not
speak (Anthropic Messages, Gemini) is left on the endpoint's protocol too;
:func:`unsupported_protocol` names it so the picker can say so.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

API_STYLES = ("chat", "responses")

# How the picker names each protocol.
LABELS = {
    "chat": "Chat Completions",
    "responses": "Responses API",
    "anthropic": "Anthropic Messages API",
    "google": "Gemini API",
}


def native_protocol(config: Any, model: str = "") -> str | None:
    """The protocol ``model`` is served over on the config's endpoint, if it is an exception.

    ``model`` defaults to the config's own. None when nothing says the model
    differs from its endpoint — the usual case.
    """
    from mira.llm import models_dev, registry

    model = model or str(getattr(config, "model", "") or "")
    if not model:
        return None
    preset = _preset(config)
    entry = registry.get(model)
    if entry and entry.get("api_style") and entry.get("endpoint") == preset:
        return str(entry["api_style"])
    provider_id = models_dev.endpoint_provider_id(config)
    return models_dev.native_protocol(provider_id, model) if provider_id else None


def api_style_for(config: Any, model: str = "") -> str:
    """The ``api_style`` a call to ``model`` must use on the config's endpoint.

    The model's own protocol when it is one Mira speaks, otherwise the
    config's. OAuth bindings and Bedrock carry their own protocol and are
    returned as they are.
    """
    current = str(getattr(config, "api_style", "") or "chat")
    if getattr(config, "oauth_provider", None) or getattr(config, "provider", "") == "bedrock":
        return current
    native = native_protocol(config, model)
    return native if native in API_STYLES else current


def unsupported_protocol(config: Any, model: str = "") -> str | None:
    """The protocol serving ``model`` when Mira cannot speak it, else None."""
    native = native_protocol(config, model)
    return native if native and native not in API_STYLES else None


def with_model_protocol(config: Any) -> Any:
    """``config`` with its ``api_style`` set to what its model needs.

    Returned unchanged when nothing needs changing, so an unbound config
    stays the same object. A change is logged, since a call going out on
    another protocol than the endpoint says is worth being able to find.
    """
    style = api_style_for(config)
    if style == getattr(config, "api_style", None):
        return config
    logger.info(
        "Model %s is served over the %s on this endpoint; calling it that way instead of %s",
        config.model,
        LABELS.get(style, style),
        LABELS.get(config.api_style, config.api_style),
    )
    return config.model_copy(update={"api_style": style})


def _preset(config: Any) -> str:
    from mira.llm import endpoints

    try:
        return str(endpoints.profile_for_config(config).get("name") or "")
    except Exception:  # pragma: no cover - a config with no URL
        return ""
