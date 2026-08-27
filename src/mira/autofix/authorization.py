"""Who may ask Mira to write, and whether Mira may write at all.

Two questions, deliberately answered in one place and deliberately answered
*before* anything is generated:

1. **Does this account have write permission on this repository?** Not "is it
   the pull request author", not "did it react to the comment" — write
   permission, read from the platform, because that is the permission the fix
   is about to exercise on their behalf.
2. **Can the provider Mira is holding actually create a branch and a pull
   request here?** A token that cannot is refused with the reason, never
   worked around.

Both fail closed. A permission the platform cannot report is not a permission;
a provider that cannot say what the default branch is cannot be trusted not to
write to it.

Nothing here consults the pull request. A comment body cannot grant permission,
and neither can a label, a title, or the fact that the requester also happens
to be the author.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from mira.autofix import capabilities as caps
from mira.autofix.models import Reason, ReasonCode
from mira.autofix.policy import EffectivePolicy

logger = logging.getLogger(__name__)

# Platform permission strings that mean "this account can push here". Read as
# an allowlist rather than a ranking: an unrecognised permission is not
# generously interpreted, it is simply not on the list.
WRITE_PERMISSIONS: frozenset[str] = frozenset({"admin", "maintain", "write", "owner", "push"})

# What a provider says when it looked and found nothing, versus when it could
# not look. Only the second is an error; both refuse.
UNKNOWN_PERMISSION = "unknown"


@dataclass(frozen=True)
class Authorization:
    """The answer, with the evidence that produced it."""

    allowed: bool
    actor: str
    permission: str = UNKNOWN_PERMISSION
    reason: Reason | None = None

    @property
    def refusal(self) -> list[Reason]:
        return [self.reason] if self.reason else []


def _deny(actor: str, permission: str, code: str, message: str) -> Authorization:
    return Authorization(
        allowed=False, actor=actor, permission=permission, reason=Reason(code, message)
    )


async def authorize_requester(
    provider: Any,
    pr_info: Any,
    *,
    actor: str,
    policy: EffectivePolicy,
    capabilities: caps.AutofixCapabilities | None = None,
) -> Authorization:
    """Whether ``actor`` may have Mira write a fix for this pull request.

    Order matters and is the reverse of what reads naturally. The blocklist is
    consulted before the permission lookup, so a blocked account cannot use
    ``@mira fix`` to make Mira call the permissions API on its behalf — and so
    a deployment that has blocked somebody does not depend on the platform
    being reachable to keep them blocked.
    """
    capability = capabilities or caps.for_provider(provider)
    login = (actor or "").strip()
    if not login:
        return _deny(
            login,
            UNKNOWN_PERMISSION,
            ReasonCode.ACTOR_UNKNOWN,
            "The request carried no identifiable account, so nothing can be attributed to it",
        )

    lowered = login.lower()
    blocked = {name.strip().lower() for name in policy.blocked_requesters if name.strip()}
    if lowered in blocked:
        return _deny(
            login,
            UNKNOWN_PERMISSION,
            ReasonCode.ACTOR_NOT_ALLOWED,
            f"@{login} is on the autofix blocklist",
        )

    allowed = {name.strip().lower() for name in policy.allowed_requesters if name.strip()}
    if allowed and lowered not in allowed:
        return _deny(
            login,
            UNKNOWN_PERMISSION,
            ReasonCode.ACTOR_NOT_ALLOWED,
            f"@{login} is not on this repository's autofix requester allowlist",
        )

    if not policy.require_write_permission:
        # Still not "anyone": the allowlist above already ran, and an install
        # that turns this off has said in configuration that its allowlist is
        # the permission model.
        return Authorization(allowed=True, actor=login, permission="not_required")

    if not capability.can_read_permission:
        return _deny(
            login,
            UNKNOWN_PERMISSION,
            ReasonCode.PERMISSION_UNREADABLE,
            f"{capability.provider} cannot report repository permissions, "
            "so write access cannot be confirmed",
        )

    getter = getattr(provider, "get_actor_permission", None)
    if not callable(getter):
        return _deny(
            login,
            UNKNOWN_PERMISSION,
            ReasonCode.PERMISSION_UNREADABLE,
            "This provider cannot report repository permissions",
        )

    try:
        permission = str(await getter(pr_info, login) or UNKNOWN_PERMISSION).strip().lower()
    except Exception as exc:  # noqa: BLE001 - unreadable is a refusal, not a crash
        logger.warning("Could not read %s's permission on %s: %s", login, pr_info.url, exc)
        return _deny(
            login,
            UNKNOWN_PERMISSION,
            ReasonCode.PERMISSION_UNREADABLE,
            f"The platform did not answer whether @{login} can write here",
        )

    if permission in WRITE_PERMISSIONS:
        return Authorization(allowed=True, actor=login, permission=permission)

    if permission in ("", UNKNOWN_PERMISSION):
        if policy.allow_unknown_permission:
            return Authorization(allowed=True, actor=login, permission=UNKNOWN_PERMISSION)
        return _deny(
            login,
            UNKNOWN_PERMISSION,
            ReasonCode.ACTOR_UNKNOWN,
            f"The platform could not say what @{login} may do in this repository",
        )

    return _deny(
        login,
        permission,
        ReasonCode.ACTOR_LACKS_WRITE,
        f"@{login} has {permission} access here; asking Mira to commit needs write access",
    )


def authorize_delivery(
    *,
    policy: EffectivePolicy,
    capabilities: caps.AutofixCapabilities,
    requested_mode: str,
) -> tuple[str, Reason | None]:
    """The delivery mode that may actually be used, or a reason none may.

    Returns ``(mode, None)`` on success and ``("", reason)`` on refusal. The
    caller never gets a silently substituted mode: a maintainer who asked for a
    commit on the pull request's own branch and would instead get a stacked
    pull request is told so, and decides.
    """
    if not policy.active:
        return "", Reason(
            ReasonCode.AUTOFIX_OFF, "Assisted correction is not enabled for this repository"
        )
    permitted = policy.permitted_mode(requested_mode, capabilities)
    if permitted:
        return permitted, None

    if requested_mode == "handoff":
        return "", Reason(
            ReasonCode.MODE_NOT_PERMITTED,
            "No handoff adapter is configured for this deployment",
        )
    if requested_mode == "pr_branch":
        if not policy.allow_commit_to_pr_branch:
            return "", Reason(
                ReasonCode.MODE_NOT_PERMITTED,
                "Committing onto the pull request's own branch is disabled "
                "(autofix.allow_commit_to_pr_branch)",
            )
        return "", Reason(
            ReasonCode.PROVIDER_CANNOT_WRITE,
            f"{capabilities.provider} cannot push onto an existing pull request's branch",
        )
    missing = [
        label
        for label, present in (
            ("create a branch", capabilities.can_create_branch),
            ("create a commit", capabilities.can_commit),
            ("open a pull request", capabilities.can_open_pull_request),
            ("read the default branch", capabilities.can_read_default_branch),
        )
        if not present
    ]
    return "", Reason(
        ReasonCode.PROVIDER_CANNOT_WRITE,
        f"{capabilities.provider} cannot " + ", cannot ".join(missing or ["write here"]),
    )
