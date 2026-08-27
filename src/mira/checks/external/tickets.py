"""Finding the issue a pull request claims to be about, and asking whether it exists.

Two jobs, deliberately separated, because they fail differently.

**Extraction is offline and always possible.** References are read out of the
title, the body and the branch name with patterns the operator can extend. It
needs no network, so "this pull request references nothing" is a fact about the
pull request that Mira can always establish.

**Resolution needs somebody to ask,** and the answer has three shapes rather
than two: the issue exists, the platform says it does not, or nobody could
find out. Only the middle one is a statement about the pull request. The
adapter contract below is written around that: ``fetch`` returns ``None`` for
"no such issue" and *raises* for everything else, and a check that conflated
the two would report an expired token as a wave of bad references across every
open pull request in the install.

No external tracker is required. The default adapter asks the hosting platform
Mira is already authenticated against; ``provider: "none"`` disables lookups
entirely and leaves extraction working. An install that wants Jira, Linear or
an internal tracker registers an adapter — the rest of this package never
learns that it exists.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol

from mira.models import IssueInfo

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class TicketRef:
    """One reference, as written and as resolved.

    ``kind`` is ``"platform"`` when the reference names something the hosting
    platform can be asked about, and ``"external"`` when it names a key in some
    other system. The distinction decides which adapter, if any, can answer.
    """

    raw: str
    kind: str = "platform"
    number: int = 0
    owner: str = ""
    repo: str = ""
    key: str = ""
    url: str = ""
    # Where it was written: "title", "body", "branch".
    found_in: str = "body"
    # Line number within the body, for evidence. 0 elsewhere.
    line: int = 0

    @property
    def label(self) -> str:
        if self.kind == "platform":
            if self.owner and self.repo:
                return f"{self.owner}/{self.repo}#{self.number}"
            return f"#{self.number}"
        return self.key or self.raw


# owner/repo#123 — checked before the bare form, which would otherwise match
# the "#123" tail of it and lose the repository.
_CROSS_REPO = re.compile(r"\b(?P<owner>[\w.-]+)/(?P<repo>[\w.-]+)#(?P<number>\d+)\b")
# #123 and GH-123.
_SAME_REPO = re.compile(r"(?:(?<=\s)|^)(?:#|[Gg][Hh]-)(?P<number>\d+)\b")
# Issue URLs. GitLab nests groups and puts a `-` before `issues`; GitHub and
# Forgejo do not. One pattern covers all three rather than three that drift.
_ISSUE_URL = re.compile(
    r"https?://(?P<host>[^/\s]+)/(?P<path>[\w.\-/]+?)/(?:-/)?issues/(?P<number>\d+)"
)
# A branch named `feature/123-thing` or `fix/GH-88`.
_BRANCH_REF = re.compile(r"(?:^|[/_-])(?:gh-)?(?P<number>\d{1,6})(?:[/_-]|$)", re.IGNORECASE)


def _dedupe(refs: list[TicketRef]) -> list[TicketRef]:
    """One entry per referenced thing, keeping the first place it was written.

    The title is scanned before the body and the body before the branch, so the
    surviving reference is the most deliberate one — and its evidence points
    where a reader would expect to look.
    """
    seen: set[tuple[str, str, str, int, str]] = set()
    out: list[TicketRef] = []
    for ref in refs:
        identity = (ref.kind, ref.owner.lower(), ref.repo.lower(), ref.number, ref.key.upper())
        if identity in seen:
            continue
        seen.add(identity)
        out.append(ref)
    return out


def extract(
    *,
    title: str = "",
    body: str = "",
    branch: str = "",
    extra_patterns: list[str] | None = None,
) -> list[TicketRef]:
    """Every ticket reference in a pull request, in the order they were written.

    Reads the title, then each line of the body, then the branch name. The
    branch is consulted last and contributes only a bare number, because
    ``release/2024`` is a date and ``fix/500`` is probably an HTTP status —
    a branch reference that duplicates a real one in the body is dropped by the
    deduplication above, and one that stands alone is weak evidence the caller
    can see the source of.
    """
    refs: list[TicketRef] = []

    def _scan(text: str, where: str, line: int = 0) -> None:
        for match in _ISSUE_URL.finditer(text):
            path = match.group("path").strip("/").split("/")
            owner = "/".join(path[:-1]) if len(path) > 1 else ""
            repo = path[-1] if path else ""
            refs.append(
                TicketRef(
                    raw=match.group(0),
                    kind="platform",
                    number=int(match.group("number")),
                    owner=owner,
                    repo=repo,
                    url=match.group(0),
                    found_in=where,
                    line=line,
                )
            )
        for match in _CROSS_REPO.finditer(text):
            refs.append(
                TicketRef(
                    raw=match.group(0),
                    kind="platform",
                    number=int(match.group("number")),
                    owner=match.group("owner"),
                    repo=match.group("repo"),
                    found_in=where,
                    line=line,
                )
            )
        for match in _SAME_REPO.finditer(f" {text}"):
            refs.append(
                TicketRef(
                    raw=match.group(0).strip(),
                    kind="platform",
                    number=int(match.group("number")),
                    found_in=where,
                    line=line,
                )
            )
        for pattern in extra_patterns or []:
            try:
                compiled = re.compile(pattern)
            except re.error:  # pragma: no cover - validated at config load
                continue
            for match in compiled.finditer(text):
                groups = match.groupdict()
                key = groups.get("key") or match.group(0)
                refs.append(
                    TicketRef(
                        raw=match.group(0),
                        kind="external",
                        key=str(key).strip(),
                        found_in=where,
                        line=line,
                    )
                )

    _scan(title or "", "title")
    for index, line in enumerate((body or "").splitlines(), start=1):
        _scan(line, "body", index)

    branch_name = branch or ""
    if branch_name:
        for match in _BRANCH_REF.finditer(branch_name):
            refs.append(
                TicketRef(
                    raw=match.group(0).strip("/_-"),
                    kind="platform",
                    number=int(match.group("number")),
                    found_in="branch",
                )
            )
        for pattern in extra_patterns or []:
            try:
                compiled = re.compile(pattern)
            except re.error:  # pragma: no cover - validated at config load
                continue
            for match in compiled.finditer(branch_name):
                groups = match.groupdict()
                refs.append(
                    TicketRef(
                        raw=match.group(0),
                        kind="external",
                        key=str(groups.get("key") or match.group(0)).strip(),
                        found_in="branch",
                    )
                )

    return _dedupe(refs)


class TicketLookupError(Exception):
    """Nobody could find out whether the ticket exists.

    Distinct from returning ``None`` on purpose, and the distinction is the
    entire reason this exception type exists: ``None`` means the tracker
    answered "no such issue", which is a fact about the pull request, and this
    means the tracker did not answer at all, which is a fact about Mira.
    """


class TicketAdapter(Protocol):
    """How Mira asks some tracker about a reference.

    Implement this and register it to add Jira, Linear, Shortcut or an internal
    tool. Nothing else in the package changes: the checks read ``IssueInfo``
    and never learn which system produced it.
    """

    name: str

    async def fetch(self, ref: TicketRef, ctx: Any) -> IssueInfo | None:
        """The issue, ``None`` if the tracker says there is none.

        Raises :class:`TicketLookupError` when the tracker could not be asked.
        """
        ...


class PlatformTicketAdapter:
    """Asks the hosting platform Mira is already talking to.

    The default, and the only one that needs no configuration: the token that
    reads the pull request can usually read the issue next to it. It answers
    only for platform-shaped references — a Jira key has no meaning to GitHub,
    and pretending otherwise would turn every ``ACME-12`` into a missing issue.
    """

    name = "auto"

    async def fetch(self, ref: TicketRef, ctx: Any) -> IssueInfo | None:
        if ref.kind != "platform" or not ref.number:
            raise TicketLookupError(
                f"{ref.label} is not a reference this platform can be asked about"
            )
        provider = getattr(ctx, "provider", None)
        pr_info = getattr(ctx, "pr_info", None)
        if provider is None or pr_info is None:
            raise TicketLookupError("no provider is attached to this run")
        getter = getattr(provider, "get_issue", None)
        if getter is None:
            raise TicketLookupError("this provider cannot read issues")
        try:
            return await getter(pr_info, ref.number, owner=ref.owner, repo=ref.repo)
        except NotImplementedError as exc:
            raise TicketLookupError(str(exc) or "this provider cannot read issues") from exc
        except Exception as exc:  # noqa: BLE001 - every transport failure is ignorance
            raise TicketLookupError(f"{type(exc).__name__}: {exc}") from exc


class NullTicketAdapter:
    """Looks nothing up, and says so.

    What ``provider: "none"`` selects. Reference *extraction* still works, so a
    deployment that wants "every pull request must name a ticket" without
    letting Mira talk to a tracker gets exactly that.
    """

    name = "none"

    async def fetch(self, ref: TicketRef, ctx: Any) -> IssueInfo | None:
        raise TicketLookupError("ticket lookups are disabled for this deployment")


_ADAPTERS: dict[str, Callable[[], TicketAdapter]] = {
    "auto": PlatformTicketAdapter,
    "platform": PlatformTicketAdapter,
    "none": NullTicketAdapter,
}


def register_adapter(name: str, factory: Callable[[], TicketAdapter]) -> None:
    """Register a ticket adapter under ``name``.

    Deployment-side extension point. Names are lowercased and the built-ins
    cannot be replaced: an install that could rebind ``none`` to something that
    makes network calls would have a kill switch that does not switch anything
    off.
    """
    key = (name or "").strip().lower()
    if not key:
        raise ValueError("a ticket adapter needs a name")
    if key in {"auto", "platform", "none"}:
        raise ValueError(f"{key!r} is a built-in ticket adapter and cannot be replaced")
    _ADAPTERS[key] = factory


def adapter_for(name: str) -> TicketAdapter | None:
    """The adapter registered under ``name``, or None when there is none."""
    factory = _ADAPTERS.get((name or "auto").strip().lower())
    if factory is None:
        return None
    try:
        return factory()
    except Exception as exc:  # noqa: BLE001 - a broken adapter degrades to absent
        logger.warning("Ticket adapter %r could not be constructed: %s", name, exc)
        return None


def registered_adapters() -> list[str]:
    return sorted(_ADAPTERS)


# ── Acceptance criteria ─────────────────────────────────────────────────────

# A heading that introduces criteria. Matched on its own line so a passing
# mention in a sentence does not count as a section.
_CRITERIA_HEADING = re.compile(
    r"^\s*(?:#{1,6}\s*|\*\*|__)?\s*"
    r"(?:acceptance\s+criteria|acceptance|definition\s+of\s+done|dod|"
    r"success\s+criteria|requirements?|expected\s+behaviou?r)\b",
    re.IGNORECASE,
)

_CHECKBOX = re.compile(r"^\s*(?:[-*+]|\d+\.)\s*\[[ xX]\]\s*(?P<text>\S.*)$")
_BULLET = re.compile(r"^\s*(?:[-*+]|\d+\.)\s+(?P<text>\S.*)$")
_GIVEN_WHEN_THEN = re.compile(r"^\s*(?:given|when|then)\b", re.IGNORECASE)


@dataclass(frozen=True)
class Criterion:
    """One acceptance criterion, with the line it was written on."""

    text: str
    line: int
    checked: bool = False


def parse_acceptance_criteria(body: str) -> list[Criterion]:
    """Criteria stated in an issue body, in the order they appear.

    Three shapes are recognised, because those are the three teams actually
    write: a checkbox list, a bullet list under an "Acceptance criteria"
    heading, and Given/When/Then lines. A bullet list *without* such a heading
    is not counted — an issue's description is usually a bullet list, and
    counting it would let every issue pass a check that is supposed to ask
    whether anybody wrote down what "done" means.
    """
    criteria: list[Criterion] = []
    in_section = False
    for index, line in enumerate((body or "").splitlines(), start=1):
        if _CRITERIA_HEADING.match(line):
            in_section = True
            continue
        stripped = line.strip()
        if in_section and stripped.startswith("#"):
            # A new heading ends the section.
            in_section = False
        checkbox = _CHECKBOX.match(line)
        if checkbox:
            criteria.append(
                Criterion(
                    text=checkbox.group("text").strip(),
                    line=index,
                    checked="[x]" in line.lower(),
                )
            )
            continue
        if _GIVEN_WHEN_THEN.match(stripped):
            criteria.append(Criterion(text=stripped, line=index))
            continue
        if in_section:
            bullet = _BULLET.match(line)
            if bullet:
                criteria.append(Criterion(text=bullet.group("text").strip(), line=index))
    return criteria
