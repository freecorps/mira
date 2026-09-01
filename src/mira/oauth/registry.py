"""Registry of OAuth login providers.

Adding a provider is: write the spec module, import it here, add it to
``_PROVIDERS``. Everything downstream — the dashboard page, the API routes, the
CLI, the LLM binding — reads this registry and needs no change.
"""

from __future__ import annotations

from mira.oauth.base import OAuthProviderSpec
from mira.oauth.chatgpt import ChatGPTOAuthProvider

_PROVIDERS: dict[str, type[OAuthProviderSpec]] = {
    ChatGPTOAuthProvider.id: ChatGPTOAuthProvider,
}


def all_providers() -> dict[str, type[OAuthProviderSpec]]:
    """Every registered provider, keyed by id."""
    return dict(_PROVIDERS)


def get(provider_id: str) -> type[OAuthProviderSpec] | None:
    """The provider spec for ``provider_id``, or None if unknown."""
    return _PROVIDERS.get((provider_id or "").strip().lower())


def require(provider_id: str) -> type[OAuthProviderSpec]:
    """Like :func:`get`, but raises for an unknown id."""
    spec = get(provider_id)
    if spec is None:
        known = ", ".join(sorted(_PROVIDERS)) or "none"
        raise KeyError(f"Unknown OAuth provider {provider_id!r} (known: {known})")
    return spec


def llm_providers() -> dict[str, type[OAuthProviderSpec]]:
    """Only the providers that can serve as an LLM backend."""
    return {pid: spec for pid, spec in _PROVIDERS.items() if spec.llm is not None}
