"""The history signal: who has worked on these files before.

Two sources feed one table.

**What Mira saw.** Every pull request Mira reviews and that then merges leaves a
row per changed path for its author and for each person who reviewed it. These
identities arrive on a webhook the platform signed, which makes them the
strongest evidence available on any platform — and the only kind available on
GitLab, whose commit API identifies an author only by the name and email
written into the commit.

**What the platform can attribute.** On GitHub and Forgejo a commit resolves to
the account that made it, so the file's history can be fetched directly. That
is what gives a fresh install a useful suggestion before it has watched a
single pull request merge. It is fetched *at the base commit*, for the same
reason ownership is: commits on the pull request's own branch are written by
the person proposing the change, and a signal they can write is a signal they
can aim.

Both sources are bounded hard. The hottest handful of changed paths, a window
in days, a fetch cache with a marker row that distinguishes "asked and nobody
has touched it" from "never asked", and one shot per path per refresh interval.
A suggestion that costs three hundred API calls is not a feature.

A person scores once per changed file they have touched, weighted by how
recently. Not once per commit: forty commits to one file is one person who
knows one file, and letting commit count drive the ranking would put whoever
rebases most at the top of every list.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any

from mira.checks.native import paths as pathkind
from mira.models import FileChangeStat
from mira.triage.models import Evidence, SignalReport

logger = logging.getLogger(__name__)

# Evidence items kept per identity per signal. Enough to show the pattern, few
# enough that a comment stays readable and a stored run stays small.
MAX_EVIDENCE_PER_SIGNAL = 5

# Recency floor. A touch at the far edge of the window is worth a fifth of a
# touch today and never nothing: the window already decided what counts, and a
# decay that reaches zero inside it would mean the window has two edges.
MIN_RECENCY = 0.2

DAY_SECONDS = 86_400.0


def recency(event_at: float, *, now: float, window_days: int) -> float:
    """How much a touch from ``event_at`` is still worth, in [MIN_RECENCY, 1].

    Linear rather than exponential, because this number is shown to people:
    "half the window ago, so worth about half" is a sentence a reader can check
    against the dates in the evidence, and a half-life is not.
    """
    if not event_at or window_days <= 0:
        return MIN_RECENCY
    age_days = max(0.0, (now - event_at) / DAY_SECONDS)
    return max(MIN_RECENCY, min(1.0, 1.0 - age_days / float(window_days)))


@dataclass
class Touch:
    """The most recent time one identity touched one path, and the proof."""

    identity: str = ""
    path: str = ""
    at: float = 0.0
    evidence: Evidence = field(default_factory=Evidence)


@dataclass
class HistoryOutcome:
    """What the history signals produced, and how each of them fared."""

    authored: dict[str, list[Touch]] = field(default_factory=dict)
    reviewed: dict[str, list[Touch]] = field(default_factory=dict)
    authored_report: SignalReport = field(default_factory=lambda: SignalReport(kind="authored"))
    reviewed_report: SignalReport = field(default_factory=lambda: SignalReport(kind="reviewed"))
    paths_considered: list[str] = field(default_factory=list)

    @property
    def reports(self) -> list[SignalReport]:
        return [self.authored_report, self.reviewed_report]


def ranked_paths(changes: list[FileChangeStat], *, limit: int) -> list[str]:
    """The changed files whose history is worth asking about.

    Generated files are dropped: their history is a machine's, and ranking a
    person for having regenerated a lockfile is how a suggestion list stops
    being believed. Ordered by how much of the file changed, so a pull request
    that touches sixty files spends its budget on the six that matter, and
    tie-broken by path so two runs over the same diff ask the same questions.
    """
    candidates = [change for change in changes if not pathkind.is_generated(change.path)]
    candidates.sort(key=lambda change: (-(change.added_lines + change.deleted_lines), change.path))
    return [change.path for change in candidates[:limit]]


def _touches(
    rows: list[dict[str, Any]], role: str, *, now: float, window_days: int
) -> dict[str, list[Touch]]:
    """Fold contribution rows into one touch per identity per path.

    The newest row for a pair wins — the rows arrive newest-first — so a
    person who has edited a file every week for a year contributes what a
    person who edited it once last week does, plus the recency they earned.
    """
    seen: dict[tuple[str, str], Touch] = {}
    for row in rows:
        if row.get("role") != role:
            continue
        identity = str(row.get("identity") or "")
        path = str(row.get("path") or "")
        if not identity or not path or (identity, path) in seen:
            continue
        at = float(row.get("event_at") or 0.0)
        source = str(row.get("source") or "")
        reference = str(row.get("reference") or "")
        detail = {
            "commit": f"commit {reference[:8]}" if reference else "a commit",
            "pull_request": f"pull request #{reference}" if reference else "a pull request",
            "review": f"reviewed pull request #{reference}" if reference else "a review",
        }.get(source, source or "a change")
        seen[(identity, path)] = Touch(
            identity=identity,
            path=path,
            at=at,
            evidence=Evidence(
                path=path,
                detail=detail,
                url=str(row.get("url") or ""),
                source=source or "commit",
                at=at,
            ),
        )

    grouped: dict[str, list[Touch]] = {}
    for touch in seen.values():
        grouped.setdefault(touch.identity, []).append(touch)
    for identity, touches in grouped.items():
        touches.sort(key=lambda item: (-item.at, item.path))
        grouped[identity] = touches[:MAX_EVIDENCE_PER_SIGNAL]
    # Recency is applied by the ranker, which is the only place that turns a
    # touch into a number; this module only decides which touches exist.
    _ = (now, window_days)
    return grouped


def stale_paths(
    fetched: dict[str, float], paths: list[str], *, now: float, refresh_hours: float
) -> list[str]:
    """Paths whose history should be fetched again, or has never been fetched.

    A path missing from ``fetched`` was never asked about. That is not the same
    as a path asked about an hour ago that came back empty, and treating them
    alike would either re-ask everything forever or record silence as an answer.
    """
    if refresh_hours <= 0:
        return list(paths)
    cutoff = now - refresh_hours * 3600.0
    return [path for path in paths if fetched.get(path, 0.0) < cutoff]


async def _fetch_authors(
    provider: Any,
    pr_info: Any,
    paths: list[str],
    *,
    ref: str,
    max_per_path: int,
) -> tuple[dict[str, list[Any]], int]:
    """Ask the platform who has touched these paths. Returns ``(by_path, unattributed)``."""
    getter = getattr(provider, "get_path_authors", None)
    if not callable(getter):
        raise NotImplementedError("this provider cannot attribute commits")
    fetched = await getter(pr_info, paths, ref=ref, max_per_path=max_per_path)
    attributed: dict[str, list[Any]] = {}
    unattributed = 0
    for path, entries in (fetched or {}).items():
        kept = []
        for entry in entries or []:
            login = str(getattr(entry, "login", "") or "").strip()
            if not login:
                # The platform could not resolve this commit to an account.
                # Its author fields are whatever the committer wrote, so it
                # names nobody here — and is counted, so the signal can say so.
                unattributed += 1
                continue
            kept.append(entry)
        if kept:
            attributed[path] = kept
    return attributed, unattributed


async def gather(
    provider: Any,
    pr_info: Any,
    changes: list[FileChangeStat],
    *,
    store: Any,
    enabled: bool = True,
    can_attribute_commits: bool = True,
    window_days: int = 180,
    max_paths: int = 12,
    max_per_path: int = 20,
    refresh_hours: float = 168.0,
    ref: str = "",
    platform: str = "github",
    now: float = 0.0,
) -> HistoryOutcome:
    """Collect who has authored and who has reviewed these files. Never raises."""
    now = now or time.time()
    started = time.monotonic()

    def _report(kind: str, status: str, detail: str, candidates: int = 0) -> SignalReport:
        return SignalReport(
            kind=kind,
            status=status,
            detail=detail,
            candidates=candidates,
            duration_seconds=round(time.monotonic() - started, 4),
        )

    if not enabled:
        return HistoryOutcome(
            authored_report=_report("authored", "disabled", "The history signal is turned off."),
            reviewed_report=_report("reviewed", "disabled", "The history signal is turned off."),
        )

    paths = ranked_paths(changes, limit=max_paths)
    if not paths:
        note = "The pull request changes no files whose history is worth reading."
        return HistoryOutcome(
            authored_report=_report("authored", "empty", note),
            reviewed_report=_report("reviewed", "empty", note),
        )

    if store is None:
        note = "The history store could not be opened, so nothing could be read back."
        return HistoryOutcome(
            paths_considered=paths,
            authored_report=_report("authored", "unavailable", note),
            reviewed_report=_report("reviewed", "unavailable", note),
        )

    fetch_error = ""
    unattributed = 0
    if can_attribute_commits and provider is not None and ref:
        try:
            fetched = store.path_fetch_times(paths, platform=platform)
            wanted = stale_paths(fetched, paths, now=now, refresh_hours=refresh_hours)
            if wanted:
                by_path, unattributed = await _fetch_authors(
                    provider, pr_info, wanted, ref=ref, max_per_path=max_per_path
                )
                rows = [
                    {
                        "platform": platform,
                        "path": path,
                        "identity": str(getattr(entry, "login", "")),
                        "role": "authored",
                        "source": "commit",
                        "reference": str(getattr(entry, "sha", "")),
                        "url": str(getattr(entry, "url", "")),
                        "event_at": float(getattr(entry, "at", 0.0)),
                    }
                    for path, entries in by_path.items()
                    for entry in entries
                ]
                store.record_path_contributions(rows)
                # Marked for every path asked about, including the ones that
                # came back empty: an empty answer is an answer, and re-asking
                # it every push is how an install runs out of API budget.
                store.mark_path_fetched(wanted, platform=platform, entries=len(rows), at=now)
        except NotImplementedError as exc:
            fetch_error = str(exc)
        except Exception as exc:  # noqa: BLE001 - an outage narrows, never invents
            logger.debug("Triage could not fetch path history: %s", exc)
            fetch_error = str(exc)
    elif can_attribute_commits and provider is not None and not ref:
        fetch_error = "the base commit is unknown, and history is never read from the head"

    try:
        rows = store.path_contributions(paths, since=now - window_days * DAY_SECONDS)
    except Exception as exc:  # noqa: BLE001
        logger.debug("Triage could not read path history: %s", exc)
        note = f"The recorded history could not be read: {exc}"
        return HistoryOutcome(
            paths_considered=paths,
            authored_report=_report("authored", "unavailable", note),
            reviewed_report=_report("reviewed", "unavailable", note),
        )

    authored = _touches(rows, "authored", now=now, window_days=window_days)
    reviewed = _touches(rows, "reviewed", now=now, window_days=window_days)

    if fetch_error:
        authored_report = _report(
            "authored",
            "unavailable",
            f"The file history could not be refreshed ({fetch_error}). "
            + (
                f"{len(authored)} author(s) from previously recorded history were still used."
                if authored
                else "No previously recorded history was available either."
            ),
            candidates=len(authored),
        )
    elif not can_attribute_commits:
        detail = (
            "This platform does not attribute commits to accounts, so authorship comes "
            "only from the pull requests Mira has seen merge."
        )
        authored_report = _report(
            "authored",
            "available" if authored else "unsupported",
            detail + (f" {len(authored)} author(s) found." if authored else " None recorded yet."),
            candidates=len(authored),
        )
    else:
        detail = f"{len(authored)} author(s) across {len(paths)} file(s)."
        if unattributed:
            detail += (
                f" {unattributed} commit(s) could not be attributed to an account and were ignored."
            )
        authored_report = _report(
            "authored",
            "available" if authored else "empty",
            detail if authored else f"Nobody has changed these {len(paths)} file(s) in the window.",
            candidates=len(authored),
        )

    reviewed_report = _report(
        "reviewed",
        "available" if reviewed else "empty",
        f"{len(reviewed)} reviewer(s) of these files in the window."
        if reviewed
        else "Mira has recorded no reviews of these files yet.",
        candidates=len(reviewed),
    )

    return HistoryOutcome(
        authored=authored,
        reviewed=reviewed,
        authored_report=authored_report,
        reviewed_report=reviewed_report,
        paths_considered=paths,
    )
