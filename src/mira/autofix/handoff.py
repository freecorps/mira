"""Handing a fix to something that is not Mira.

Some teams already run a coding agent and would rather Mira described the work
than did it. That is a legitimate answer, and this module is the seam for it —
an interface and a registry, not an integration.

The shape of the seam is the point. An adapter is a small object with one
method; it is looked up by name from configuration; and nothing imports one
until a deployment names it. An install that never configures a handoff never
loads a line of this beyond the registry itself, and no part of the pipeline
depends on an external service existing.

One adapter ships in the box: ``comment``, which posts an agent-ready brief on
the pull request. It writes no code and calls nothing external, which makes it
a real answer for a team whose agent is a human with an editor — and a working
reference for anyone writing the second adapter.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from mira.autofix.models import AutofixJob
from mira.autofix.redact import redact

logger = logging.getLogger(__name__)


@dataclass
class HandoffContext:
    """Everything an adapter is given. Redacted before it gets here."""

    job: AutofixJob
    finding_title: str = ""
    finding_body: str = ""
    finding_path: str = ""
    finding_line: int = 0
    pr_url: str = ""
    pr_title: str = ""
    head_sha: str = ""
    # Adapter-specific settings from `autofix.handoff.options`, passed through
    # untouched. Mira does not interpret them and does not validate them.
    options: dict[str, Any] = field(default_factory=dict)
    # The provider, so an adapter that wants to write a comment can. Passing it
    # is deliberate: an adapter that needs a platform call should make it
    # through the same provider everything else uses, not open its own client
    # with its own credentials.
    provider: Any = None
    pr_info: Any = None


@dataclass
class HandoffResult:
    """What the adapter did, for the audit trail."""

    ok: bool
    # An opaque identifier the adapter can be asked about later — a ticket id,
    # a run id, a comment id. Rendered, never parsed.
    ref: str = ""
    detail: str = ""


@runtime_checkable
class HandoffAdapter(Protocol):
    """The whole interface. One method, and it may not raise usefully.

    An adapter that raises is treated as having failed, which is a job the
    operator can see and retry — never a job that silently looks handed off.
    """

    name: str

    async def dispatch(self, context: HandoffContext) -> HandoffResult: ...


_REGISTRY: dict[str, Any] = {}


def register(adapter: Any) -> None:
    """Register an adapter under its ``name``. Later registrations win.

    Later-wins rather than first-wins so a deployment can replace the built-in
    ``comment`` adapter with its own without editing Mira.
    """
    name = str(getattr(adapter, "name", "") or "").strip().lower()
    if not name:
        raise ValueError("A handoff adapter must have a non-empty name")
    _REGISTRY[name] = adapter


def get(name: str) -> Any | None:
    """The adapter registered under ``name``, or None."""
    return _REGISTRY.get((name or "").strip().lower())


def available() -> list[str]:
    return sorted(_REGISTRY)


def brief(context: HandoffContext) -> str:
    """The work order, in prose an agent or a human can act on.

    Deliberately *not* a prompt. It names the repository, the commit, the file
    and the problem, and it stops there: an adapter that feeds this to a model
    is choosing to, and Mira is not smuggling instructions into a description.
    """
    job = context.job
    lines = [
        f"Repository: {job.owner}/{job.repo} ({job.platform})",
        f"Pull request: {context.pr_url or job.pr_url}",
        f"Commit: {context.head_sha or job.head_sha}",
        f"Branch: {job.head_branch}",
        f"Finding: {job.finding_id}",
    ]
    if context.finding_path:
        location = context.finding_path
        if context.finding_line:
            location += f":{context.finding_line}"
        lines.append(f"Location: {location}")
    lines.append("")
    lines.append(redact(context.finding_title or job.finding_title))
    if context.finding_body:
        lines.extend(["", redact(context.finding_body)])
    return "\n".join(lines)


class CommentAdapter:
    """Post the brief on the pull request and consider the job handed over.

    The zero-dependency adapter: it integrates with nothing, needs no
    credentials of its own, and works on every provider Mira already speaks to.
    For a team whose "external agent" is a person, it is the whole feature.
    """

    name = "comment"

    async def dispatch(self, context: HandoffContext) -> HandoffResult:
        provider = context.provider
        if provider is None or context.pr_info is None:
            return HandoffResult(ok=False, detail="No provider was available to post the handoff")
        # Four backticks, and any run of four or more inside the brief is
        # broken up. The brief quotes review text Mira does not control, and a
        # fence a quoted string can close is a fence that lets that string
        # continue as markdown.
        fenced = re.sub(r"`{3,}", lambda m: "`" * min(len(m.group(0)), 2), brief(context))
        body = (
            "### Mira — fix handoff\n\n"
            "Mira did not write this fix. Here is the work, ready for an agent "
            "or a person to pick up.\n\n"
            "````\n" + fenced + "\n````\n"
        )
        try:
            await provider.post_comment(context.pr_info, body)
        except Exception as exc:  # noqa: BLE001 - a failed handoff is not a handoff
            logger.warning("Handoff comment failed on %s: %s", context.pr_url, exc)
            return HandoffResult(ok=False, detail=f"The handoff comment could not be posted: {exc}")
        return HandoffResult(
            ok=True,
            ref=f"comment:{context.job.job_key[:12]}",
            detail="Handoff posted on the pull request",
        )


register(CommentAdapter())


async def dispatch(name: str, context: HandoffContext) -> HandoffResult:
    """Run the named adapter, turning every failure into a result.

    An adapter is third-party code by construction. It does not get to take the
    worker down, and it does not get to leave a job in a state nobody can read.
    """
    adapter = get(name)
    if adapter is None:
        return HandoffResult(ok=False, detail=f"No handoff adapter named {name!r} is registered")
    try:
        result = await adapter.dispatch(context)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Handoff adapter %s failed: %s", name, exc)
        return HandoffResult(ok=False, detail=f"The {name} handoff adapter failed: {exc}")
    if not isinstance(result, HandoffResult):
        return HandoffResult(
            ok=False, detail=f"The {name} handoff adapter returned something unusable"
        )
    return result
