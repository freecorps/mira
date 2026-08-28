"""The CODEOWNERS signal: who the repository says owns the changed files.

Two decisions in this module carry the weight.

**It is read at the base, never at the head.** CODEOWNERS is repository policy,
and the pull request is the thing being judged against it. A branch that adds
``src/auth/ @friendly-account`` to CODEOWNERS and is then ranked under that
line has nominated its own reviewer, which is not a subtle attack — it is one
commit. So the ref is the merge target's, the run records which ref was used,
and a pull request whose base commit cannot be determined gets no ownership
signal at all rather than a head-read one.

**An unreadable CODEOWNERS is not an absent one.** The parser this shares with
the merge gate is strict: a file with a line it cannot understand is
``unreadable``, and here that becomes ``unavailable`` — the signal could not
answer. A repository with no CODEOWNERS at all is ``empty``, which is an
answer. Collapsing the two would let a broken ownership map read as "nobody
owns this", and the whole point of the phase is that Mira's own failures are
never dressed up as facts about the code.

Nothing here mentions anybody. Owners are recorded as identities and rendered
as literal text; notifying five people about a suggestion nobody asked for is
the fastest way to have the suggestion turned off.
"""

from __future__ import annotations

import inspect
import logging
import time
from dataclasses import dataclass, field
from typing import Any

from mira.gate.codeowners import CodeownersFile, Rule
from mira.gate.codeowners import parse as parse_codeowners
from mira.gate.paths import PatternError, matches, normalize
from mira.triage.models import Evidence, SignalReport

logger = logging.getLogger(__name__)


class OwnershipRefUnknown(Exception):
    """The pull request's base commit could not be determined.

    Its own exception because the only alternative — reading ownership at the
    head — is the one thing this module must never do, and a caller that
    handled a generic failure by "trying the other ref" would reintroduce
    exactly that.
    """


@dataclass
class OwnershipOutcome:
    """Owners of the changed paths, and how the lookup went."""

    owners: dict[str, list[Evidence]] = field(default_factory=dict)
    report: SignalReport = field(default_factory=lambda: SignalReport(kind="codeowners"))
    ref: str = ""
    source_path: str = ""

    @property
    def usable(self) -> bool:
        return self.report.status == "available"


def base_ref(pr_info: Any) -> str:
    """The ref ownership is read at: the base commit, or the base branch.

    The commit is preferred because it cannot move underneath the run. The
    branch is the fallback for providers that do not expose a base sha, and it
    is still the *target* branch — a moving ref the pull request does not
    control.
    """
    ref = str(getattr(pr_info, "base_sha", "") or "").strip()
    if ref:
        return ref
    return str(getattr(pr_info, "base_branch", "") or "").strip()


def _accepts_ref(getter: Any) -> bool:
    """Whether this provider's ``get_codeowners`` takes an explicit ref.

    Checked rather than attempted, because the failure mode of attempting it is
    a ``TypeError`` that a caller cannot tell apart from a bug inside the
    provider — and the recovery for one of those is "read the head", which is
    not available to us.
    """
    try:
        return "ref" in inspect.signature(getter).parameters
    except (TypeError, ValueError):  # pragma: no cover - exotic callables
        return False


def matching_rule(codeowners: CodeownersFile, path: str) -> Rule | None:
    """The rule that decides this path, last match winning, as git does."""
    candidate = normalize(path)
    winner: Rule | None = None
    for rule in codeowners.rules:
        try:
            if matches(candidate, rule.pattern):
                winner = rule
        except PatternError:  # pragma: no cover - parse rejected these already
            continue
    return winner


def owners_from(codeowners: CodeownersFile, paths: list[str]) -> dict[str, list[Evidence]]:
    """Map every owner to the paths they own and the line that says so.

    A rule with no owners genuinely un-owns a path — writing one is how a
    repository carves an exception out of a broader rule — so it contributes
    nobody rather than contributing the broader rule's owners.
    """
    found: dict[str, list[Evidence]] = {}
    for path in paths:
        rule = matching_rule(codeowners, path)
        if rule is None or not rule.owners:
            continue
        for owner in rule.owners:
            found.setdefault(owner, []).append(
                Evidence(
                    path=path,
                    line=rule.line,
                    detail=f"{codeowners.path or 'CODEOWNERS'}:{rule.line} — {rule.pattern}",
                    source="codeowners",
                )
            )
    return found


async def gather(
    provider: Any,
    pr_info: Any,
    paths: list[str],
    *,
    enabled: bool = True,
    can_read: bool = True,
) -> OwnershipOutcome:
    """Read CODEOWNERS at the base and map the changed paths to their owners.

    Never raises. Every way this can fail produces a report that says which
    way it was, because "no owners" and "could not look" are the two answers a
    reader must be able to tell apart.
    """
    started = time.monotonic()

    def _report(status: str, detail: str, candidates: int = 0) -> SignalReport:
        return SignalReport(
            kind="codeowners",
            status=status,
            detail=detail,
            candidates=candidates,
            duration_seconds=round(time.monotonic() - started, 4),
        )

    if not enabled:
        return OwnershipOutcome(report=_report("disabled", "The CODEOWNERS signal is turned off."))
    if provider is None or not can_read:
        return OwnershipOutcome(
            report=_report(
                "unsupported",
                "This platform cannot be asked for CODEOWNERS at the base commit.",
            )
        )
    if not paths:
        return OwnershipOutcome(
            report=_report("empty", "The pull request changes no files to attribute.")
        )

    getter = getattr(provider, "get_codeowners", None)
    if not callable(getter):
        return OwnershipOutcome(
            report=_report("unsupported", "This provider cannot read CODEOWNERS.")
        )
    if not _accepts_ref(getter):
        # Reading it at the default ref is not an acceptable degradation: the
        # default is the head, and ownership read from the head is ownership
        # the pull request may have written.
        return OwnershipOutcome(
            report=_report(
                "unsupported",
                "This provider cannot read CODEOWNERS at a chosen ref, and ownership "
                "is never read from the pull request's own head.",
            )
        )

    ref = base_ref(pr_info)
    if not ref:
        return OwnershipOutcome(
            report=_report(
                "unavailable",
                "The pull request's base commit is unknown, so ownership could not be "
                "read from anywhere the pull request does not control.",
            )
        )

    try:
        source_path, content = await getter(pr_info, ref=ref)
    except Exception as exc:  # noqa: BLE001 - an outage is not an absence
        logger.debug("Triage could not read CODEOWNERS at %s: %s", ref, exc)
        return OwnershipOutcome(
            ref=ref,
            report=_report("unavailable", f"CODEOWNERS could not be read at {ref[:12]}: {exc}"),
        )

    if not content:
        return OwnershipOutcome(
            ref=ref,
            source_path=source_path,
            report=_report("empty", f"This repository has no CODEOWNERS at {ref[:12]}."),
        )

    parsed = parse_codeowners(content, source_path=source_path)
    if parsed.status != "ok":
        return OwnershipOutcome(
            ref=ref,
            source_path=source_path,
            report=_report(
                "unavailable",
                f"CODEOWNERS at {ref[:12]} could not be parsed: {parsed.error}",
            ),
        )

    owners = owners_from(parsed, paths)
    status = "available" if owners else "empty"
    detail = (
        f"{len(owners)} owner(s) declared for the changed files in "
        f"{parsed.path or 'CODEOWNERS'} at {ref[:12]}."
        if owners
        else f"No CODEOWNERS rule in {parsed.path or 'CODEOWNERS'} matches the changed files."
    )
    return OwnershipOutcome(
        owners=owners,
        ref=ref,
        source_path=parsed.path,
        report=_report(status, detail, candidates=len(owners)),
    )
