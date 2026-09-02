"""Model routes: a model id that also says which backend serves it.

The Models page used to offer one list at a time — the API key's catalog, or
a signed-in account's — and a bare id like ``gpt-5-codex`` meant "whatever
the current backend is". With more than one place a call can go, the same
name in two lists is indistinguishable, and "which one did I pick" is exactly
the question the picker exists to answer.

A route is a model id with the backend written in front of it::

    oauth:chatgpt:<account>:gpt-5-codex   a specific signed-in account
    oauth:chatgpt:*:gpt-5-codex           any account of that provider (rotates)
    api:openai/gpt-5.1                    the configured API-key endpoint
    anthropic/claude-sonnet-4-6           bare: the default backend, as before

The prefixes cannot collide with a real model id — no vendor ships an id
starting with ``oauth:`` or ``api:`` — and the model part may itself contain
colons (Bedrock ids do), which is why it is always the last, unsplit field.
Routes are accepted anywhere a model id is: the dashboard, ``mira.yaml``,
and a repository's own ``.mira.yaml``.
"""

from __future__ import annotations

from dataclasses import dataclass

OAUTH_PREFIX = "oauth:"
API_PREFIX = "api:"
# The account field meaning "any account this provider has".
ANY_ACCOUNT = "*"


@dataclass(frozen=True)
class ModelRoute:
    """A parsed route. ``backend`` is ``"oauth"`` or ``"api"``."""

    backend: str
    model: str
    provider: str = ""
    account: str = ""  # "*" = any account (rotate)

    @property
    def rotates(self) -> bool:
        return self.backend == "oauth" and self.account in ("", ANY_ACCOUNT)

    @property
    def value(self) -> str:
        if self.backend == "oauth":
            return oauth_route(self.provider, self.account, self.model)
        return api_route(self.model)


def parse_route(value: str | None) -> ModelRoute | None:
    """The route a model value names, or None for a bare model id.

    A malformed ``oauth:`` value (no provider, no model) reads as bare rather
    than raising: it fails at the endpoint with the id in the error, which is
    more useful than failing config load over a prefix.
    """
    text = (value or "").strip()
    if text.startswith(API_PREFIX):
        model = text[len(API_PREFIX) :].strip()
        return ModelRoute(backend="api", model=model) if model else None
    if text.startswith(OAUTH_PREFIX):
        parts = text[len(OAUTH_PREFIX) :].split(":", 2)
        if len(parts) != 3:
            return None
        provider, account, model = (p.strip() for p in parts)
        if not provider or not model:
            return None
        return ModelRoute(
            backend="oauth",
            model=model,
            provider=provider.lower(),
            account=ANY_ACCOUNT if account in ("", ANY_ACCOUNT) else account,
        )
    return None


def oauth_route(provider: str, account: str, model: str) -> str:
    return f"{OAUTH_PREFIX}{provider}:{account or ANY_ACCOUNT}:{model}"


def api_route(model: str) -> str:
    return f"{API_PREFIX}{model}"


def bare_model(value: str | None) -> str:
    """The model id with any route prefix removed."""
    route = parse_route(value)
    return route.model if route else (value or "").strip()
