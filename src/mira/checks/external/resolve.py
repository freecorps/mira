"""Resolving a pull request's ticket once, for every check that needs it.

The ticket check and the acceptance-criteria check ask the same question of the
same tracker. Doing it twice would double the API calls on every pull request
and — the part that actually matters — could produce two different answers to
one question, so that a pull request was told its issue exists and, three lines
lower, that it does not.

So resolution happens once per run, through :meth:`CheckContext.shared`, and
both checks read the same :class:`Resolution`. A failure is shared the same way:
one outage produces one infrastructure error, not one per interested check.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from mira.checks.context import CheckContext
from mira.checks.external.tickets import (
    TicketLookupError,
    TicketRef,
    adapter_for,
    extract,
)
from mira.models import IssueInfo

_CACHE_KEY = "ticket-resolution"


@dataclass
class Resolution:
    """What the run found out about this pull request's ticket references."""

    refs: list[TicketRef] = field(default_factory=list)
    # ref.label -> the issue, for references that resolved.
    found: dict[str, IssueInfo] = field(default_factory=dict)
    # ref.label -> why, for references the tracker said do not exist.
    missing: list[TicketRef] = field(default_factory=list)
    # ref.label -> the reason nobody could ask. An infrastructure fact.
    unresolved: dict[str, str] = field(default_factory=dict)
    # Set when lookups are switched off by configuration rather than broken.
    lookups_disabled: bool = False

    @property
    def any_found(self) -> bool:
        return bool(self.found)

    def first_found(self) -> IssueInfo | None:
        for issue in self.found.values():
            return issue
        return None


async def _resolve(ctx: CheckContext) -> Resolution:
    settings = ctx.policy.ticket
    refs = extract(
        title=ctx.pr_title,
        body=ctx.pr_body,
        branch=ctx.head_branch,
        extra_patterns=list(settings.reference_patterns),
    )
    resolution = Resolution(refs=refs)

    adapter = adapter_for(settings.provider)
    if adapter is None:
        resolution.unresolved = {
            ref.label: f"no ticket adapter named {settings.provider!r} is registered"
            for ref in refs
        }
        return resolution
    if adapter.name == "none":
        resolution.lookups_disabled = True
        return resolution

    for ref in refs:
        try:
            issue = await adapter.fetch(ref, ctx)
        except TicketLookupError as exc:
            resolution.unresolved[ref.label] = str(exc)
            continue
        if issue is None:
            resolution.missing.append(ref)
        else:
            resolution.found[ref.label] = issue
    return resolution


async def resolution_for(ctx: CheckContext) -> Resolution:
    """The run's one ticket resolution, computed on first use."""
    return await ctx.shared(_CACHE_KEY, lambda: _resolve(ctx))
