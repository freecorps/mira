"""LLM provider package — factory entry point."""

from __future__ import annotations

from mira.config import LLMConfig
from mira.llm.base import LLMProviderProtocol


def create_llm(config: LLMConfig) -> LLMProviderProtocol:
    """Create the appropriate LLM provider based on config.provider.

    Returns an instance satisfying LLMProviderProtocol.
    """
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
