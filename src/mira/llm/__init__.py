"""LLM provider package — factory entry point."""

from __future__ import annotations

import logging

from mira.config import LLMConfig
from mira.llm.base import LLMProviderProtocol


def create_llm(config: LLMConfig) -> LLMProviderProtocol:
    """Create the appropriate LLM provider based on config.provider.

    Returns an instance satisfying LLMProviderProtocol.
    """
    # An OAuth session outranks the API-key path: the operator signed in on
    # purpose, and the endpoint/auth then both come from the provider spec.
    # Config validation and the dashboard both reject ids that aren't
    # registered, so an unknown one here is a bug rather than a typo — say so
    # and review with the configured key instead of failing the run outright.
    if config.oauth_provider:
        from mira.oauth import registry

        if registry.get(config.oauth_provider) is not None:
            from mira.llm.oauth import OAuthResponsesProvider

            return OAuthResponsesProvider(config)
        logging.getLogger(__name__).warning(
            "Ignoring unknown llm.oauth_provider %r", config.oauth_provider
        )

    if config.provider == "bedrock":
        from mira.llm.bedrock import BedrockProvider

        return BedrockProvider(config)

    if config.api_style == "responses":
        from mira.llm.responses import ResponsesProvider

        return ResponsesProvider(config)

    # Default: OpenAI-compatible endpoint (OpenRouter, vLLM, Ollama, etc.)
    from mira.llm.provider import LLMProvider

    return LLMProvider(config)
