"""What each hosting platform can actually do for pre-merge checks.

Same shape and same reasoning as :mod:`mira.gate.capabilities`, and for the
same reason: the honest answer to a missing capability is to say so. A provider
that cannot read a job's log must produce a ``skipped`` result naming the
limitation, never a ``pass`` that reads as "CI is fine" — and never a
``violation``, which would blame a pull request for a provider's API.

Capabilities are declared, not probed. A probe costs a round trip per pull
request on a device that has none to spare, and a probe that failed transiently
would silently remove a check an operator believes is running. When a declared
capability turns out to be unavailable at run time, the check that needed it
records an infrastructure error with the provider's own words, which is exactly
the distinction this phase exists to make visible.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class CheckCapabilities:
    """One provider's check-relevant surface."""

    provider: str = "unknown"
    # Can fetch an issue by number, and can distinguish "no such issue" from
    # "could not ask". Both halves matter: a provider that reports every
    # failure as absence would turn an outage into a wave of violations.
    can_read_issues: bool = False
    # Can report the head commit's CI outcome.
    can_read_ci: bool = False
    # Can report each failing job with an excerpt of its output.
    can_read_ci_logs: bool = False
    # Can publish the run as a check run / commit status.
    can_publish_status: bool = False
    # Human-readable notes about the degradations this provider imposes.
    notes: tuple[str, ...] = field(default_factory=tuple)

    def as_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "can_read_issues": self.can_read_issues,
            "can_read_ci": self.can_read_ci,
            "can_read_ci_logs": self.can_read_ci_logs,
            "can_publish_status": self.can_publish_status,
            "notes": list(self.notes),
        }


# A provider that declares nothing. Used for unknown providers and for the
# no-provider path (CLI, tests): every check that needs the platform skips
# with a reason, and nothing is reported as a violation.
NO_CAPABILITIES = CheckCapabilities(
    provider="none",
    notes=("No provider was attached, so checks that need the platform cannot run.",),
)

GITHUB_CAPABILITIES = CheckCapabilities(
    provider="github",
    can_read_issues=True,
    can_read_ci=True,
    can_read_ci_logs=True,
    can_publish_status=True,
    notes=(
        "CI evidence comes from each failing check run's own output summary, "
        "which is what the action chose to publish — not from downloading the "
        "raw Actions log archive.",
    ),
)

# GitLab's limitation here is the same one the gate found: a commit status
# posted through the API joins the head pipeline. Publishing one would corrupt
# the CI signal these checks read back, and on a project with "pipelines must
# succeed" it could satisfy the very restriction a failing check just refused
# to satisfy. So nothing is published; the summary goes to the comment and the
# dashboard.
GITLAB_CAPABILITIES = CheckCapabilities(
    provider="gitlab",
    can_read_issues=True,
    can_read_ci=True,
    can_read_ci_logs=True,
    can_publish_status=False,
    notes=(
        "Mira publishes no commit status on GitLab: a status joins the head "
        "pipeline, so it would corrupt the CI signal these checks read and could "
        "satisfy a 'pipelines must succeed' rule. Turn on checks.comment to "
        "surface the summary on the merge request.",
    ),
)

FORGEJO_CAPABILITIES = CheckCapabilities(
    provider="forgejo",
    can_read_issues=True,
    can_read_ci=True,
    # Forgejo reports CI as commit statuses. A status carries a description and
    # a link and no log at all, so the CI check reports the job, the link and
    # the description, and says plainly that no output was available.
    can_read_ci_logs=False,
    can_publish_status=True,
    notes=(
        "Forgejo reports CI through commit statuses, which carry a description "
        "and a link but no job output. The CI check quotes what the status says "
        "and records that the log itself was not available.",
    ),
)

_BY_PLATFORM = {
    "github": GITHUB_CAPABILITIES,
    "gitlab": GITLAB_CAPABILITIES,
    "forgejo": FORGEJO_CAPABILITIES,
}


def for_platform(platform: str) -> CheckCapabilities:
    """Declared capabilities for a platform name, degrading on anything unknown."""
    return _BY_PLATFORM.get((platform or "").lower(), NO_CAPABILITIES)


_FLAGS = (
    "can_read_issues",
    "can_read_ci",
    "can_read_ci_logs",
    "can_publish_status",
)


def narrow(reported: CheckCapabilities, platform_default: CheckCapabilities) -> CheckCapabilities:
    """Intersect a provider's claim with the platform's reviewed capabilities.

    A provider may *narrow* the default — a token with reduced scopes, a
    self-hosted instance with no CI integration. It may never widen one: the
    table above is the reviewed statement of what each platform supports, and a
    provider that claimed more than it can deliver would turn a skip that
    explains itself into a check that fails at run time.
    """
    return CheckCapabilities(
        provider=reported.provider or platform_default.provider,
        notes=tuple(dict.fromkeys((*platform_default.notes, *reported.notes))),
        **{
            flag: bool(getattr(reported, flag) and getattr(platform_default, flag))
            for flag in _FLAGS
        },
    )


def for_provider(provider: Any) -> CheckCapabilities:
    """Capabilities of a live provider instance, never wider than its platform."""
    if provider is None:
        return NO_CAPABILITIES
    declared = getattr(provider, "checks_capabilities", None)
    if not callable(declared):
        return NO_CAPABILITIES
    try:
        reported = declared()
    except Exception:  # noqa: BLE001 - a broken provider degrades, never widens
        return NO_CAPABILITIES
    if not isinstance(reported, CheckCapabilities):
        return NO_CAPABILITIES
    return narrow(reported, for_platform(reported.provider))
