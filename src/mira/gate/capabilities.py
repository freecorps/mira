"""What each hosting platform can actually do for the gate.

Providers differ, and the honest response to a missing capability is to say so.
A platform that cannot record an approval must produce ``would_approve``, never
an ``approved`` that no merge box will ever see — a decision that claims an
approval nobody received is worse than no gate at all, because it is the one
failure mode an operator cannot notice from the dashboard.

Capabilities are declared, not probed. Probing at decision time costs a round
trip per PR on a device that has none to spare, and a probe that fails for a
transient reason would silently downgrade a working install. When a declared
capability turns out to be unavailable at delivery time (a GitLab tier without
merge-request approvals, a token without ``checks:write``), delivery fails
loudly and the decision degrades to ``would_approve``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class GateCapabilities:
    """One provider's gate-relevant surface."""

    provider: str = "unknown"
    # Can record a first-class approval that a branch-protection rule counts.
    can_approve: bool = False
    # Can record a blocking "changes requested" review event.
    can_request_changes: bool = False
    # Can publish a check run / commit status carrying the explanation.
    can_publish_status: bool = False
    # Can report the head commit's CI outcome.
    can_read_ci: bool = False
    # Can report the author's association/permission on the repository.
    can_read_association: bool = False
    # Can list labels on the pull request.
    can_read_labels: bool = False
    # Can read a CODEOWNERS file at the head ref.
    can_read_codeowners: bool = False
    # Human-readable notes about the degradations this provider imposes.
    notes: tuple[str, ...] = field(default_factory=tuple)

    def as_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "can_approve": self.can_approve,
            "can_request_changes": self.can_request_changes,
            "can_publish_status": self.can_publish_status,
            "can_read_ci": self.can_read_ci,
            "can_read_association": self.can_read_association,
            "can_read_labels": self.can_read_labels,
            "can_read_codeowners": self.can_read_codeowners,
            "notes": list(self.notes),
        }


# A provider that declares nothing. Used for unknown providers and for the
# no-provider path (CLI, tests): everything degrades, nothing is approved.
NO_CAPABILITIES = GateCapabilities(
    provider="none",
    notes=("No provider was attached, so the gate can only record a decision.",),
)

GITHUB_CAPABILITIES = GateCapabilities(
    provider="github",
    can_approve=True,
    can_request_changes=True,
    can_publish_status=True,
    can_read_ci=True,
    can_read_association=True,
    can_read_labels=True,
    can_read_codeowners=True,
)

# GitLab has merge-request approvals, but they are a paid-tier feature on
# gitlab.com and can be disabled per project, so the API may refuse at delivery
# time. It has no "request changes" review event at all — the closest thing is
# an unresolved discussion, which Mira already posts as review comments, so the
# gate does not pretend otherwise.
GITLAB_CAPABILITIES = GateCapabilities(
    provider="gitlab",
    can_approve=True,
    can_request_changes=False,
    can_publish_status=True,
    can_read_ci=True,
    can_read_association=True,
    can_read_labels=True,
    can_read_codeowners=True,
    notes=(
        "GitLab has no REQUEST_CHANGES review event; blockers are reported as "
        "review comments and a failed commit status instead.",
        "Merge-request approvals may be unavailable on the project's tier — "
        "delivery degrades to would_approve if the API refuses.",
    ),
)

FORGEJO_CAPABILITIES = GateCapabilities(
    provider="forgejo",
    can_approve=True,
    can_request_changes=True,
    can_publish_status=True,
    can_read_ci=True,
    can_read_association=True,
    can_read_labels=True,
    can_read_codeowners=True,
    notes=(
        "Forgejo reports CI through commit statuses; repositories without a "
        "CI integration report no status, which the gate treats as not green.",
    ),
)

_BY_PLATFORM = {
    "github": GITHUB_CAPABILITIES,
    "gitlab": GITLAB_CAPABILITIES,
    "forgejo": FORGEJO_CAPABILITIES,
}


def for_platform(platform: str) -> GateCapabilities:
    """Declared capabilities for a platform name, degrading on anything unknown."""
    return _BY_PLATFORM.get((platform or "").lower(), NO_CAPABILITIES)


_FLAGS = (
    "can_approve",
    "can_request_changes",
    "can_publish_status",
    "can_read_ci",
    "can_read_association",
    "can_read_labels",
    "can_read_codeowners",
)


def narrow(reported: GateCapabilities, platform_default: GateCapabilities) -> GateCapabilities:
    """Intersect a provider's claim with the platform's reviewed capabilities.

    A provider may *narrow* the default — a token with reduced scopes, a
    self-hosted instance with no CI integration. It may never widen one: the
    table above is the reviewed statement of what each platform supports, and a
    provider that claims more than it can deliver would turn a would_approve
    into an approval nobody received.
    """
    return GateCapabilities(
        provider=reported.provider or platform_default.provider,
        notes=tuple(dict.fromkeys((*platform_default.notes, *reported.notes))),
        **{
            flag: bool(getattr(reported, flag) and getattr(platform_default, flag))
            for flag in _FLAGS
        },
    )


def for_provider(provider: Any) -> GateCapabilities:
    """Capabilities of a live provider instance, never wider than its platform."""
    if provider is None:
        return NO_CAPABILITIES
    declared = getattr(provider, "gate_capabilities", None)
    if not callable(declared):
        return NO_CAPABILITIES
    try:
        reported = declared()
    except Exception:  # noqa: BLE001 - a broken provider degrades, never widens
        return NO_CAPABILITIES
    if not isinstance(reported, GateCapabilities):
        return NO_CAPABILITIES
    return narrow(reported, for_platform(reported.provider))
