"""What each hosting platform can actually do for assisted correction.

Same discipline as the merge gate's capability table, for the same reason: the
honest response to a missing capability is to say so. A platform that cannot
open a pull request must refuse the request with the reason, never fall back to
writing somewhere else that happens to be reachable.

Capabilities are declared, not probed. A probe costs a round trip per request
on a device that has none to spare, and a probe that fails transiently would
silently downgrade a working install. When a declared capability turns out to
be unavailable at publish time — a token without ``contents:write``, an
instance with protected-branch rules — publication fails loudly and the job
records which call refused.

`can_merge` is declared on every provider and is `False` on every provider.
It exists so that "Mira never merges" is a value in a table somebody can read
rather than an absence somebody has to notice.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class AutofixCapabilities:
    """One provider's correction-relevant surface."""

    provider: str = "unknown"
    # Can create a branch from a commit.
    can_create_branch: bool = False
    # Can create a commit containing file contents on a branch it just made,
    # without a working tree and without a force push.
    can_commit: bool = False
    # Can open a pull request from one branch to another.
    can_open_pull_request: bool = False
    # Can push a commit onto an existing pull request's head branch. Separate
    # from `can_commit` because the permission is different and the blast
    # radius is somebody else's branch.
    can_push_to_pr_branch: bool = False
    # Can report a named account's permission on the repository. Without it,
    # authorization has no evidence and every request is refused.
    can_read_permission: bool = False
    # Can name the repository's default branch. Without it, "never write to the
    # default branch" cannot be enforced, so writing is refused entirely.
    can_read_default_branch: bool = False
    # Can find an already-open pull request for a head branch, which is what
    # makes republication idempotent instead of duplicative.
    can_find_pull_request: bool = False
    # Can report the head commit's CI outcome, for the bounded CI retry loop.
    can_read_ci: bool = False
    # Always False. Mira opens changes; humans merge them.
    can_merge: bool = False
    # Human-readable notes about the degradations this provider imposes.
    notes: tuple[str, ...] = field(default_factory=tuple)

    @property
    def can_publish(self) -> bool:
        """Whether a branch-and-pull-request delivery is possible at all.

        Reading the default branch is part of the answer, not a nicety: the
        guarantee this phase sells is that no failure writes to the default
        branch, and a provider that cannot name it cannot be held to that.
        """
        return (
            self.can_create_branch
            and self.can_commit
            and self.can_open_pull_request
            and self.can_read_default_branch
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "can_create_branch": self.can_create_branch,
            "can_commit": self.can_commit,
            "can_open_pull_request": self.can_open_pull_request,
            "can_push_to_pr_branch": self.can_push_to_pr_branch,
            "can_read_permission": self.can_read_permission,
            "can_read_default_branch": self.can_read_default_branch,
            "can_find_pull_request": self.can_find_pull_request,
            "can_read_ci": self.can_read_ci,
            "can_merge": self.can_merge,
            "can_publish": self.can_publish,
            "notes": list(self.notes),
        }


# A provider that declares nothing. Used for unknown providers and for the
# no-provider path (CLI, tests): everything degrades, nothing is written.
NO_CAPABILITIES = AutofixCapabilities(
    provider="none",
    notes=("No provider was attached, so no change can be written.",),
)

GITHUB_CAPABILITIES = AutofixCapabilities(
    provider="github",
    can_create_branch=True,
    can_commit=True,
    can_open_pull_request=True,
    can_push_to_pr_branch=True,
    can_read_permission=True,
    can_read_default_branch=True,
    can_find_pull_request=True,
    can_read_ci=True,
)

GITLAB_CAPABILITIES = AutofixCapabilities(
    provider="gitlab",
    can_create_branch=True,
    can_commit=True,
    can_open_pull_request=True,
    can_push_to_pr_branch=True,
    can_read_permission=True,
    can_read_default_branch=True,
    can_find_pull_request=True,
    can_read_ci=True,
    notes=(
        "GitLab reports project membership as a numeric access level; anything "
        "below Developer (30) cannot push and is refused write permission.",
        "A merge request whose source project differs from the target project "
        "is a fork; Mira will not commit onto its branch.",
    ),
)

FORGEJO_CAPABILITIES = AutofixCapabilities(
    provider="forgejo",
    can_create_branch=True,
    can_commit=True,
    can_open_pull_request=True,
    can_push_to_pr_branch=True,
    can_read_permission=True,
    can_read_default_branch=True,
    can_find_pull_request=True,
    can_read_ci=True,
    notes=(
        "Forgejo commits through the batch contents endpoint, so a multi-file "
        "patch is one commit. Forgejo older than 1.20 does not have it and "
        "refuses the request before writing anything.",
    ),
)

_BY_PLATFORM = {
    "github": GITHUB_CAPABILITIES,
    "gitlab": GITLAB_CAPABILITIES,
    "forgejo": FORGEJO_CAPABILITIES,
}


def for_platform(platform: str) -> AutofixCapabilities:
    """Declared capabilities for a platform name, degrading on anything unknown."""
    return _BY_PLATFORM.get((platform or "").lower(), NO_CAPABILITIES)


_FLAGS = (
    "can_create_branch",
    "can_commit",
    "can_open_pull_request",
    "can_push_to_pr_branch",
    "can_read_permission",
    "can_read_default_branch",
    "can_find_pull_request",
    "can_read_ci",
)


def narrow(
    reported: AutofixCapabilities, platform_default: AutofixCapabilities
) -> AutofixCapabilities:
    """Intersect a provider's claim with the platform's reviewed capabilities.

    A provider may *narrow* the default — a token with reduced scopes, an
    instance with the contents API disabled. It may never widen one: the table
    above is the reviewed statement of what each platform supports, and a
    provider that claims more than it can deliver would turn a refusal into a
    half-written branch.
    """
    return AutofixCapabilities(
        provider=reported.provider or platform_default.provider,
        notes=tuple(dict.fromkeys((*platform_default.notes, *reported.notes))),
        **{
            flag: bool(getattr(reported, flag) and getattr(platform_default, flag))
            for flag in _FLAGS
        },
    )


def for_provider(provider: Any) -> AutofixCapabilities:
    """Capabilities of a live provider instance, never wider than its platform."""
    if provider is None:
        return NO_CAPABILITIES
    declared = getattr(provider, "autofix_capabilities", None)
    if not callable(declared):
        return NO_CAPABILITIES
    try:
        reported = declared()
    except Exception:  # noqa: BLE001 - a broken provider degrades, never widens
        return NO_CAPABILITIES
    if not isinstance(reported, AutofixCapabilities):
        return NO_CAPABILITIES
    return narrow(reported, for_platform(reported.provider))
