"""What each platform can tell triage, declared rather than probed.

Same shape as the gate's and the check framework's capability tables, for the
same reason: a provider may *narrow* what its platform supports — a token with
fewer scopes, a self-hosted instance with a feature switched off — and may
never widen it. Widening would turn a signal that explains why it is missing
into a call that fails at run time.

One capability here carries a security argument rather than an API argument,
and it is the reason this table exists at all.

``can_attribute_commits`` is not "can this platform list commits" — all three
can. It is "does this platform tell us *which account* made the commit". A git
commit's author name and email are written by whoever made the commit and are
verified by nobody: on a repository that accepts pull requests from strangers,
a contributor can author their commits as anyone. GitHub and Forgejo resolve
each commit to the account that pushed it, and that resolved login is an
identity worth ranking on. GitLab's commit API returns only the git author
fields, so on GitLab the commit history is not used to name anybody — the
signal reports ``unsupported``, which is a permanent answer rather than an
outage, and the history that *is* used there is what Mira observed itself.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class TriageCapabilities:
    """One provider's triage-relevant surface."""

    provider: str = "unknown"
    # Can read a file — CODEOWNERS — at an arbitrary ref, which for triage
    # always means the pull request's base.
    can_read_ownership: bool = False
    # Can attribute a commit to a platform account, rather than only to the
    # free-text author fields inside the commit itself.
    can_attribute_commits: bool = False
    notes: tuple[str, ...] = field(default_factory=tuple)

    def as_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "can_read_ownership": self.can_read_ownership,
            "can_attribute_commits": self.can_attribute_commits,
            "notes": list(self.notes),
        }


# No provider attached: the local CLI, and tests. Ownership cannot be read and
# commits cannot be attributed, so both signals report why rather than
# producing an unsourced name.
NO_CAPABILITIES = TriageCapabilities(
    provider="none",
    notes=("No provider was attached, so ownership and commit history cannot be read.",),
)

GITHUB_CAPABILITIES = TriageCapabilities(
    provider="github",
    can_read_ownership=True,
    can_attribute_commits=True,
)

GITLAB_CAPABILITIES = TriageCapabilities(
    provider="gitlab",
    can_read_ownership=True,
    # The commits API returns `author_name` and `author_email` — the fields
    # inside the commit, which whoever made it chose — and no account. Ranking
    # a person on those would mean ranking on a string a contributor typed.
    can_attribute_commits=False,
    notes=(
        "GitLab's commit API identifies authors only by the name and email "
        "written into the commit, which are not verified. Authorship history "
        "on GitLab therefore comes from the merge requests Mira has itself "
        "seen, and the commit signal reports 'unsupported'.",
    ),
)

FORGEJO_CAPABILITIES = TriageCapabilities(
    provider="forgejo",
    can_read_ownership=True,
    # Forgejo resolves a commit's author to a user account when the email
    # belongs to one, and returns null when it does not — so an unresolved
    # commit is visibly unresolved rather than silently attributed.
    can_attribute_commits=True,
)

_BY_PLATFORM = {
    "github": GITHUB_CAPABILITIES,
    "gitlab": GITLAB_CAPABILITIES,
    "forgejo": FORGEJO_CAPABILITIES,
}

_FLAGS = ("can_read_ownership", "can_attribute_commits")


def for_platform(platform: str) -> TriageCapabilities:
    return _BY_PLATFORM.get((platform or "").lower(), NO_CAPABILITIES)


def narrow(
    reported: TriageCapabilities, platform_default: TriageCapabilities
) -> TriageCapabilities:
    """Intersect a provider's claim with the platform's reviewed capabilities."""
    return TriageCapabilities(
        provider=reported.provider or platform_default.provider,
        notes=tuple(dict.fromkeys((*platform_default.notes, *reported.notes))),
        **{
            flag: bool(getattr(reported, flag) and getattr(platform_default, flag))
            for flag in _FLAGS
        },
    )


def for_provider(provider: Any) -> TriageCapabilities:
    """Capabilities of a live provider instance, never wider than its platform."""
    if provider is None:
        return NO_CAPABILITIES
    declared = getattr(provider, "triage_capabilities", None)
    if not callable(declared):
        return NO_CAPABILITIES
    try:
        reported = declared()
    except Exception:  # noqa: BLE001 - a broken provider degrades, never widens
        return NO_CAPABILITIES
    if not isinstance(reported, TriageCapabilities):
        return NO_CAPABILITIES
    return narrow(reported, for_platform(reported.provider))
