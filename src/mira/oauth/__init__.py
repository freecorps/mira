"""OAuth logins for LLM backends.

Sign in to a provider once and review with the models that account already
includes, instead of managing an API key. The layer is generic — a provider is
a spec class in :mod:`mira.oauth.registry` — with ChatGPT (Codex) as the first
one.

Public surface::

    from mira.oauth import manager, registry, store

    manager.start_login("chatgpt")            # → authorization URL
    await manager.complete_login(...)          # → stores the session
    store.active_binding()                     # → what reviews should use
    await store.valid_tokens("chatgpt")        # → a non-expired access token
"""

from __future__ import annotations

from mira.oauth.base import (
    LLMBinding,
    OAuthError,
    OAuthProviderSpec,
    OAuthTokens,
    PkcePair,
)

__all__ = [
    "LLMBinding",
    "OAuthError",
    "OAuthProviderSpec",
    "OAuthTokens",
    "PkcePair",
]
