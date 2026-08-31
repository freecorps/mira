"""Provider registry and factory."""

from __future__ import annotations

import threading

from mira.providers.base import BaseProvider

_REGISTRY: dict[str, type[BaseProvider]] = {}
_LOCK = threading.Lock()


def register_provider(name: str, cls: type[BaseProvider]) -> None:
    """Register a provider class under the given name."""
    with _LOCK:
        _REGISTRY[name] = cls


def get_available_providers() -> list[str]:
    """Return a sorted list of registered provider names."""
    return sorted(_REGISTRY)


def platform_for_url(pr_url: str, default: str = "github") -> str:
    """Which platform a pull-request URL belongs to.

    Shaped by the bug a review found in the substring version this replaces:
    ``"gitlab" in pr_url`` classifies
    ``https://github.com/acme/gitlab-tools/pull/1`` as GitLab, because the
    *repository* is named after the thing. A repository name is chosen by
    whoever made the repository, so it cannot be what selects the API client.

    So the decision is made on the two parts of a URL nobody downstream owns:
    the host, and the path shape each platform uses. Anything that matches
    neither falls back to the configured default rather than guessing — an
    explicit setting is a better answer than a substring.
    """
    from urllib.parse import urlparse

    parsed = urlparse((pr_url or "").strip())
    host = (parsed.hostname or "").lower()
    path = parsed.path or ""

    if host == "github.com" or host.endswith(".github.com"):
        return "github"
    if host == "gitlab.com" or host.endswith(".gitlab.com"):
        return "gitlab"
    if host == "codeberg.org" or host.endswith(".codeberg.org"):
        return "forgejo"

    # Self-hosted instances answer to any hostname, so the path shape is what
    # is left: GitLab merge requests, Forgejo pulls, GitHub pull.
    if "/-/merge_requests/" in path:
        return "gitlab"
    if "/pulls/" in path:
        return "forgejo"
    if "/pull/" in path:
        return "github"
    return default or "github"


def create_provider(name: str, token: str) -> BaseProvider:
    """Instantiate a registered provider by name."""
    with _LOCK:
        if name not in _REGISTRY:
            available = ", ".join(sorted(_REGISTRY)) or "(none)"
            raise ValueError(f"Unknown provider {name!r}. Available: {available}")
        return _REGISTRY[name](token)


# Register built-in providers
from mira.providers.github import GitHubProvider  # noqa: E402
from mira.providers.gitlab import GitLabProvider  # noqa: E402

register_provider("github", GitHubProvider)
register_provider("gitlab", GitLabProvider)
from mira.providers.forgejo import ForgejoProvider  # noqa: E402

register_provider("forgejo", ForgejoProvider)
