"""Vocabulary and records for pull-request triage and reviewer suggestion.

This phase answers two questions about a pull request and shows its working
for both: *what kind of change is this* and *who is likely to be the right
human to look at it*. Nothing here decides, requests or assigns anything.

Three things about the vocabulary are load-bearing.

**A suggestion is not an assignment.** There is no state in this module that
means "assigned", no field that names a review request, and no code path in
this package that calls one. What is produced is a ranked list with the
evidence behind each name, published where a human reads it. Choosing a
reviewer stays a human act; Mira is trying to save the thirty seconds of
`git log` that precedes it, not to make the choice.

**"Nobody qualified" and "could not work it out" are different answers.** A run
that read every signal and found no candidate is ``no_candidates`` — an answer,
and often the correct one on a repository with two contributors, one of whom
opened the pull request. A run that could not read CODEOWNERS, or could not
reach the history it ranks on, is ``unavailable``. The second must never be
rendered as the first: "no suggestions" reads as *there is nobody obvious*, and
saying that when the truth is *Mira is broken* is the same class of lie the
pre-merge checks were built to stop telling.

**A candidate without evidence is not a candidate.** Every name carries the
signals that produced it and every signal carries what it looked at — the
CODEOWNERS line, the commit, the pull request someone reviewed. A ranking
nobody can audit is a ranking nobody should follow, and an unevidenced name is
dropped by the ranker rather than shown with a shrug.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from typing import Any, Literal

# What a whole run means.
#
# `ok`             at least one candidate, ranked, each with evidence.
# `no_candidates`  every signal was read and nobody qualified. An answer.
# `unavailable`    a signal this run depends on could not be read, and no
#                  candidate survived. Says something about Mira, never about
#                  the pull request or the people who work on it.
# `not_run`        triage is off for this repository, or the kill switch is on.
TriageStatus = Literal["ok", "no_candidates", "unavailable", "not_run"]

TRIAGE_STATUSES: tuple[TriageStatus, ...] = ("ok", "no_candidates", "unavailable", "not_run")

# Where a candidate came from.
#
# `codeowners`  the repository declared them the owner of a changed path.
# `authored`    they have changed these files before.
# `reviewed`    they have reviewed changes to these files before.
SignalKind = Literal["codeowners", "authored", "reviewed"]

SIGNAL_KINDS: tuple[SignalKind, ...] = ("codeowners", "authored", "reviewed")

# How one signal fared.
#
# `available`    it was read and produced something.
# `empty`        it was read and produced nothing. An answer.
# `unavailable`  it could not be read. Not an answer.
# `unsupported`  this platform cannot supply it. Also not an answer, but a
#                permanent one, so it is worth telling apart from an outage.
# `disabled`     configuration turned it off.
SignalStatus = Literal["available", "empty", "unavailable", "unsupported", "disabled"]

SIGNAL_STATUSES: tuple[SignalStatus, ...] = (
    "available",
    "empty",
    "unavailable",
    "unsupported",
    "disabled",
)

# Signal statuses that leave the question open. A run with no candidates and
# one of these is `unavailable`, never `no_candidates`.
UNANSWERED_SIGNALS: frozenset[str] = frozenset({"unavailable", "unsupported"})


# Why a name that a signal produced is not being suggested. Recorded rather
# than filtered silently: "we would have said Dana, but Dana opened this" is
# the single most useful line in a suggestion nobody got.
class ExclusionReason:
    """Strings, because they are persisted and rendered."""

    AUTHOR = "author"
    BOT = "bot"
    OPTED_OUT = "opted_out"
    NO_EVIDENCE = "no_evidence"
    BELOW_THRESHOLD = "below_threshold"
    # Qualified, ranked, and cut by `max_suggestions`. Recorded rather than
    # dropped, because "you were fourth" is a real answer to "why was I not
    # suggested" and an empty space is not.
    NOT_TOP_RANKED = "not_top_ranked"


EXCLUSION_REASONS: tuple[str, ...] = (
    ExclusionReason.AUTHOR,
    ExclusionReason.BOT,
    ExclusionReason.OPTED_OUT,
    ExclusionReason.NO_EVIDENCE,
    ExclusionReason.BELOW_THRESHOLD,
    ExclusionReason.NOT_TOP_RANKED,
)

# Marker for the suggestion comment, so an update lands in place rather than
# stacking a new comment on every push.
COMMENT_MARKER = "<!-- mira:reviewer-triage -->"

# Size buckets, by changed lines excluding generated files. Fixed rather than
# configurable: they exist to make two pull requests comparable across an
# install, and a per-repository scale would make the word "large" mean nothing.
SIZE_BUCKETS: tuple[tuple[str, int], ...] = (
    ("xs", 10),
    ("s", 50),
    ("m", 250),
    ("l", 1000),
)
SIZE_LARGEST = "xl"


def size_bucket(lines: int) -> str:
    for name, ceiling in SIZE_BUCKETS:
        if lines <= ceiling:
            return name
    return SIZE_LARGEST


@dataclass(frozen=True)
class Evidence:
    """One concrete thing a signal looked at.

    Quoted to humans, never interpreted. A CODEOWNERS rule cites the file and
    the line it is written on; a history signal cites the commit or the pull
    request and when it happened. Both are checkable by hand, which is the
    only standard that matters for a ranking that names people.
    """

    # What the evidence is about: a repository path, or "" for a signal that
    # is about the change as a whole.
    path: str = ""
    line: int = 0
    detail: str = ""
    url: str = ""
    # Where it came from: "codeowners", "commit", "review", "pull_request".
    source: str = ""
    # Epoch seconds of the underlying event, when there is one. Drives recency
    # weighting and is shown as an age.
    at: float = 0.0

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)

    @property
    def locator(self) -> str:
        if not self.path:
            return ""
        return f"{self.path}:{self.line}" if self.line else self.path


def evidence_from(data: Any) -> list[Evidence]:
    """Rehydrate evidence from a persisted blob, dropping anything malformed."""
    if not isinstance(data, list):
        return []
    out: list[Evidence] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        out.append(
            Evidence(
                path=str(item.get("path") or ""),
                line=int(item.get("line") or 0),
                detail=str(item.get("detail") or ""),
                url=str(item.get("url") or ""),
                source=str(item.get("source") or ""),
                at=float(item.get("at") or 0.0),
            )
        )
    return out


@dataclass
class SignalContribution:
    """What one signal said about one candidate, and what it cost them.

    ``raw`` is the signal's own magnitude — how many owned paths, how many
    commits, how many reviews — before any weight or decay. Kept alongside the
    final score so a ranking can be explained in the units a human thinks in
    ("owns 4 of the changed files") rather than only in points.
    """

    kind: str = "codeowners"
    raw: float = 0.0
    weight: float = 1.0
    score: float = 0.0
    detail: str = ""
    evidence: list[Evidence] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "raw": round(self.raw, 4),
            "weight": round(self.weight, 4),
            "score": round(self.score, 4),
            "detail": self.detail,
            "evidence": [item.as_dict() for item in self.evidence],
        }


def contributions_from(data: Any) -> list[SignalContribution]:
    if not isinstance(data, list):
        return []
    out: list[SignalContribution] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        out.append(
            SignalContribution(
                kind=str(item.get("kind") or ""),
                raw=float(item.get("raw") or 0.0),
                weight=float(item.get("weight") or 0.0),
                score=float(item.get("score") or 0.0),
                detail=str(item.get("detail") or ""),
                evidence=evidence_from(item.get("evidence")),
            )
        )
    return out


@dataclass
class ReviewerCandidate:
    """One name, its score, and everything that put it there.

    ``identity`` is the platform login or the ``@org/team`` handle exactly as
    the platform spells it. ``kind`` separates the two because a team is not a
    person: it cannot be checked against the pull request's author, it has no
    review load, and rendering it as an individual would be wrong.
    """

    identity: str = ""
    kind: str = "user"  # "user" | "team"
    score: float = 0.0
    contributions: list[SignalContribution] = field(default_factory=list)
    # Penalty already applied to `score` for how much is already on their
    # plate. Recorded separately so a low rank is explainable.
    load_penalty: float = 0.0
    open_reviews: int = 0

    @property
    def signals(self) -> list[str]:
        return [item.kind for item in self.contributions]

    @property
    def evidence(self) -> list[Evidence]:
        return [item for contribution in self.contributions for item in contribution.evidence]

    def as_dict(self) -> dict[str, Any]:
        return {
            "identity": self.identity,
            "kind": self.kind,
            "score": round(self.score, 4),
            "load_penalty": round(self.load_penalty, 4),
            "open_reviews": self.open_reviews,
            "signals": self.signals,
            "contributions": [item.as_dict() for item in self.contributions],
        }


def candidates_from(data: Any) -> list[ReviewerCandidate]:
    if not isinstance(data, list):
        return []
    out: list[ReviewerCandidate] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        out.append(
            ReviewerCandidate(
                identity=str(item.get("identity") or ""),
                kind=str(item.get("kind") or "user"),
                score=float(item.get("score") or 0.0),
                contributions=contributions_from(item.get("contributions")),
                load_penalty=float(item.get("load_penalty") or 0.0),
                open_reviews=int(item.get("open_reviews") or 0),
            )
        )
    return out


@dataclass
class SignalReport:
    """How one signal fared, whatever it produced.

    Always present for every configured signal, including the ones that
    produced nothing. A signal that is simply absent from a run is
    indistinguishable from one that ran and found nothing, and those are the
    two facts an operator most needs to tell apart.
    """

    kind: str = "codeowners"
    status: str = "empty"
    detail: str = ""
    candidates: int = 0
    duration_seconds: float = 0.0

    @property
    def answered(self) -> bool:
        return self.status not in UNANSWERED_SIGNALS

    def as_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "status": self.status,
            "detail": self.detail,
            "candidates": self.candidates,
            "duration_seconds": round(self.duration_seconds, 4),
            "answered": self.answered,
        }


def reports_from(data: Any) -> list[SignalReport]:
    if not isinstance(data, list):
        return []
    out: list[SignalReport] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        out.append(
            SignalReport(
                kind=str(item.get("kind") or ""),
                status=str(item.get("status") or "empty"),
                detail=str(item.get("detail") or ""),
                candidates=int(item.get("candidates") or 0),
                duration_seconds=float(item.get("duration_seconds") or 0.0),
            )
        )
    return out


@dataclass
class Exclusion:
    """A name a signal produced that will not be suggested, and why."""

    identity: str = ""
    reason: str = ExclusionReason.AUTHOR
    detail: str = ""

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def exclusions_from(data: Any) -> list[Exclusion]:
    if not isinstance(data, list):
        return []
    out: list[Exclusion] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        out.append(
            Exclusion(
                identity=str(item.get("identity") or ""),
                reason=str(item.get("reason") or ""),
                detail=str(item.get("detail") or ""),
            )
        )
    return out


@dataclass
class Classification:
    """What kind of change this is, in deterministic terms.

    Derived from the diff alone — paths and line counts — and from nothing
    written by a human in the pull request. A title claiming "docs only" does
    not make a change docs-only, and the classification would be worthless if
    it could be asserted rather than observed.
    """

    size: str = "xs"
    changed_files: int = 0
    changed_lines: int = 0
    # Directory areas, most-changed first: "src/mira/checks", "ui/mira/src".
    areas: list[str] = field(default_factory=list)
    # What the change is made of: "code", "tests", "docs", "migration",
    # "dependencies", "ci", "generated".
    kinds: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)

    @property
    def summary(self) -> str:
        kinds = ", ".join(self.kinds) or "no recognised file kinds"
        areas = ", ".join(self.areas[:3]) or "no shared area"
        return f"{self.size} · {kinds} · {areas}"


def classification_from(data: Any) -> Classification:
    if not isinstance(data, dict):
        return Classification()
    return Classification(
        size=str(data.get("size") or "xs"),
        changed_files=int(data.get("changed_files") or 0),
        changed_lines=int(data.get("changed_lines") or 0),
        areas=[str(item) for item in (data.get("areas") or [])],
        kinds=[str(item) for item in (data.get("kinds") or [])],
    )


@dataclass
class TriageInputs:
    """The facts this run was computed from.

    Snapshotted onto the run so a stored suggestion can be re-derived and
    checked. ``base_sha`` is here for a reason worth stating: every ownership
    fact is read at the *base* of the pull request, so that a change cannot
    nominate its own reviewers by editing CODEOWNERS in the same commit.
    """

    platform: str = "github"
    owner: str = ""
    repo: str = ""
    pr_number: int = 0
    pr_url: str = ""
    pr_author: str = ""
    pr_title: str = ""
    base_branch: str = ""
    base_sha: str = ""
    head_sha: str = ""
    draft: bool = False
    changed_paths: list[str] = field(default_factory=list)
    changed_files: int = 0
    added_lines: int = 0
    deleted_lines: int = 0
    # The ref ownership was actually read at, spelled out rather than implied.
    ownership_ref: str = ""
    review_id: int = 0

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)

    @property
    def digest(self) -> str:
        """Content hash of the facts a suggestion depends on."""
        payload = {
            "changed_paths": sorted(self.changed_paths),
            "pr_author": self.pr_author,
            "base_sha": self.base_sha,
            "head_sha": self.head_sha,
        }
        return hashlib.sha256(json.dumps(payload, sort_keys=True).encode("utf-8")).hexdigest()[:16]


def inputs_from(data: Any) -> TriageInputs:
    if not isinstance(data, dict):
        return TriageInputs()
    return TriageInputs(
        platform=str(data.get("platform") or "github"),
        owner=str(data.get("owner") or ""),
        repo=str(data.get("repo") or ""),
        pr_number=int(data.get("pr_number") or 0),
        pr_url=str(data.get("pr_url") or ""),
        pr_author=str(data.get("pr_author") or ""),
        pr_title=str(data.get("pr_title") or ""),
        base_branch=str(data.get("base_branch") or ""),
        base_sha=str(data.get("base_sha") or ""),
        head_sha=str(data.get("head_sha") or ""),
        draft=bool(data.get("draft")),
        changed_paths=[str(item) for item in (data.get("changed_paths") or [])],
        changed_files=int(data.get("changed_files") or 0),
        added_lines=int(data.get("added_lines") or 0),
        deleted_lines=int(data.get("deleted_lines") or 0),
        ownership_ref=str(data.get("ownership_ref") or ""),
        review_id=int(data.get("review_id") or 0),
    )


def run_key(
    *,
    platform: str,
    owner: str,
    repo: str,
    pr_number: int,
    head_sha: str,
    policy_version: str,
    inputs_digest: str,
) -> str:
    """Content-derived identity for one triage of one pull request.

    The same pull request, at the same commit, under the same policy, over the
    same files is the same run — so a redelivered webhook updates one row
    instead of stacking a second suggestion under a different id. A push, a
    policy edit or a different file list all produce a new run, because all
    three can legitimately change who should look at it.
    """
    parts = [
        platform or "github",
        (owner or "").lower(),
        (repo or "").lower(),
        str(pr_number or 0),
        head_sha or "",
        policy_version or "",
        inputs_digest or "",
    ]
    return hashlib.sha256("\x1f".join(parts).encode("utf-8")).hexdigest()[:32]


@dataclass
class TriageRun:
    """One triage of one pull request: what it is, and who might review it."""

    run_key: str = ""
    policy_version: str = ""
    inputs: TriageInputs = field(default_factory=TriageInputs)
    classification: Classification = field(default_factory=Classification)
    candidates: list[ReviewerCandidate] = field(default_factory=list)
    signals: list[SignalReport] = field(default_factory=list)
    excluded: list[Exclusion] = field(default_factory=list)
    duration_seconds: float = 0.0
    # Degradations that changed how a suggestion was computed without changing
    # what it answered: the review-load table being unreadable is the one this
    # exists for. A signal that fails leaves the question open and belongs in
    # `signals`; a dampener that fails leaves the ranking correct but less
    # balanced, and silently dropping it would make a ranking that stopped
    # balancing look exactly like one with nothing to balance.
    notes: list[str] = field(default_factory=list)
    # Set when the run itself could not be produced. Distinct from a signal
    # that failed: this one means there is no suggestion at all.
    error: str = ""
    attempts: int = 1
    run_id: int = 0
    created_at: float = 0.0
    updated_at: float = 0.0

    @property
    def degraded(self) -> bool:
        """Whether any configured signal failed to answer."""
        return any(not report.answered for report in self.signals)

    @property
    def status(self) -> TriageStatus:
        """What this run is, derived rather than stored.

        Derived so the two states that must never be confused cannot drift
        apart from the facts that define them. In particular: no candidates
        *plus* a signal that could not be read is ``unavailable``, never
        ``no_candidates`` — Mira does not get to say "there is nobody obvious"
        on the strength of a lookup that failed.
        """
        if self.error:
            return "unavailable"
        if self.candidates:
            return "ok"
        if not self.signals:
            return "not_run"
        if self.degraded:
            return "unavailable"
        return "no_candidates"

    @property
    def suggested(self) -> list[str]:
        return [candidate.identity for candidate in self.candidates]

    def signal(self, kind: str) -> SignalReport | None:
        for report in self.signals:
            if report.kind == kind:
                return report
        return None

    def counts(self) -> dict[str, int]:
        counts = {
            "candidates": len(self.candidates),
            "excluded": len(self.excluded),
            "degraded_signals": sum(1 for report in self.signals if not report.answered),
        }
        for kind in SIGNAL_KINDS:
            report = self.signal(kind)
            counts[f"signal_{kind}"] = report.candidates if report else 0
        return counts

    def as_dict(self) -> dict[str, Any]:
        return {
            "run_key": self.run_key,
            "run_id": self.run_id,
            "policy_version": self.policy_version,
            "status": self.status,
            "degraded": self.degraded,
            "inputs": self.inputs.as_dict(),
            "classification": self.classification.as_dict(),
            "candidates": [candidate.as_dict() for candidate in self.candidates],
            "signals": [report.as_dict() for report in self.signals],
            "excluded": [item.as_dict() for item in self.excluded],
            "notes": list(self.notes),
            "counts": self.counts(),
            "duration_seconds": round(self.duration_seconds, 4),
            "error": self.error,
            "attempts": self.attempts,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }
