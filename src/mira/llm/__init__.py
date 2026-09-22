"""LLM provider package — factory entry point."""

from __future__ import annotations

import logging

from mira.config import LLMConfig
from mira.llm.base import LLMProviderProtocol

logger = logging.getLogger(__name__)


def create_llm(config: LLMConfig) -> LLMProviderProtocol:
    """Create the appropriate LLM provider based on config.provider.

    Returns an instance satisfying LLMProviderProtocol. A config carrying
    ``fallbacks`` (the chain ``llm_config_for`` resolved for its purpose)
    yields a :class:`~mira.llm.chain.FallbackChain` over one provider per
    entry, so the caller sees one provider that tries them in order.
    """
    if config.fallbacks:
        from mira.llm.chain import FallbackChain, describe_provider

        primary = _create_one(config.model_copy(update={"fallbacks": []}))
        providers = [primary]
        for entry in config.fallbacks:
            try:
                providers.append(_create_one(entry.model_copy(update={"fallbacks": []})))
            except Exception as exc:  # noqa: BLE001 — a bad fallback must not fail the primary
                # A route to an endpoint or account this install does not
                # have. The primary still reviews; the chain is one short and
                # the log says which entry and why.
                logger.warning(
                    "Skipping fallback model %s (%s: %s)",
                    entry.model,
                    type(exc).__name__,
                    exc,
                )
        if len(providers) == 1:
            return primary
        logger.info("Model chain: %s", " → ".join(describe_provider(p) for p in providers))
        return FallbackChain(providers)
    return _create_one(config)


def _create_one(config: LLMConfig) -> LLMProviderProtocol:
    """One provider for one config — the factory as it was before chains."""
    # An OAuth session outranks the API-key path: the operator signed in on
    # purpose, and the endpoint/auth then both come from the provider spec.
    # Config validation rejects ids that aren't registered and the default
    # resolver drops them, so an unknown one here came from an explicit model
    # route — and a route the operator wrote is refused, not redirected to
    # the API key behind their back.
    if config.oauth_provider:
        from mira.exceptions import LLMError
        from mira.oauth import registry

        if registry.get(config.oauth_provider) is not None:
            from mira.llm.oauth import OAuthResponsesProvider

            return OAuthResponsesProvider(config)
        raise LLMError("oauth_unknown_provider", provider_id=config.oauth_provider)

    # Same rule for an endpoint configured in the dashboard: a route or a
    # setting naming one that is not stored is refused rather than served by
    # whichever endpoint the config file happens to name, which would spend
    # the wrong key and report the wrong destination.
    from mira.llm import endpoints

    named = endpoints.name_of(config.endpoint)
    if named and endpoints.get(named) is None:
        from mira.exceptions import LLMError

        raise LLMError("unknown_endpoint", endpoint=named)

    if config.provider == "bedrock":
        from mira.llm.bedrock import BedrockProvider

        return BedrockProvider(config)

    if config.api_style == "responses":
        from mira.llm.responses import ResponsesProvider

        return ResponsesProvider(config)

    # Default: OpenAI-compatible endpoint (OpenRouter, vLLM, Ollama, etc.)
    from mira.llm.provider import LLMProvider

    return LLMProvider(config)
