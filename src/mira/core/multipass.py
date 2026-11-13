"""Several shuffled review passes per part, merged by vote.

One pass over a part reads its files in one order and stops looking once it
has a handful of findings. What it misses depends on what it read first: in
pull requests Mira also reviewed, the bots that found what Mira did not (403
Bugbot threads, 297 of them fixed) run several passes with the diff order
randomised and keep what recurs. This module is that, for one review part:

- :func:`shuffle_files` gives each pass after the first the part's files — and
  the hunks inside a file, when that is safe — in another order, from a seed
  derived from the part and the pass, so a replay reads the same order.
- :func:`run_passes` runs the passes side by side and degrades: a pass that
  fails or is still running at the soft deadline is left out of the vote,
  and only no pass at all landing fails the part.
- :func:`vote` keeps a finding reported by at least ``threshold`` passes at
  overlapping locations. A blocker reported fewer times is not dropped
  outright — a blocker is the costliest miss — but marked ``require_proven``,
  so self-critique keeps it only when the code proves it.

Pass 0 always reads the part in its original order at the configured
temperature, so ``review.passes: 1`` is exactly the single-pass review.
"""

from __future__ import annotations

import asyncio
import dataclasses
import hashlib
import logging
import random
import time
from collections.abc import Callable, Coroutine
from dataclasses import dataclass, field
from typing import Any, Generic, TypeVar

from mira.core.ensemble import cluster_representative, cluster_runs, cluster_votes
from mira.core.grouping import prune_group
from mira.core.noise_filter import _jaccard_similarity
from mira.models import FileDiff, ReviewComment, Severity

logger = logging.getLogger(__name__)

T = TypeVar("T")

# Two passes anchoring one finding a couple of lines apart (the statement vs.
# the call inside it) are still reporting the same thing.
_LOCATION_SLACK = 3
# Below this title likeness, two findings on the same lines are different
# findings unless they also share a category.
_TITLE_SIMILARITY = 0.2


def pass_seed(key: str, pass_index: int) -> int:
    """A stable seed for one pass of one part.

    ``hash()`` is salted per process, so it would give a replay a different
    order; a digest of the part's key and the pass does not.
    """
    digest = hashlib.sha256(f"{key}\0{pass_index}".encode()).digest()
    return int.from_bytes(digest[:8], "big")


def _hunks_shuffle_safe(file: FileDiff) -> bool:
    """Whether a file's hunks can be read in another order without misleading.

    Every hunk is numbered from its own header, so order never changes a line
    number. What it can change is meaning when hunks touch or overlap (after
    context expansion they may abut), since one then reads as the
    continuation of the other; those files keep their order.
    """
    if len(file.hunks) < 2:
        return False
    spans = sorted(
        (h.target_start, h.target_start + max(h.target_length, 1) - 1) for h in file.hunks
    )
    return all(
        prev_end + 1 < start for (_, prev_end), (start, _) in zip(spans, spans[1:], strict=False)
    )


def shuffle_files(files: list[FileDiff], seed: int) -> list[FileDiff]:
    """The part's files in a seeded order, each one's hunks too where safe.

    Returns copies; the part's own files (which the response is parsed and
    anchored against) are untouched.
    """
    rng = random.Random(seed)
    order = list(files)
    rng.shuffle(order)
    out: list[FileDiff] = []
    for f in order:
        if _hunks_shuffle_safe(f):
            hunks = list(f.hunks)
            rng.shuffle(hunks)
            f = dataclasses.replace(f, hunks=hunks)
        out.append(f)
    return out


def pass_temperature(base: float, spread: float, pass_index: int, passes: int) -> float | None:
    """Sampling temperature for one pass, or None for the configured default.

    Pass 0 keeps the configured temperature (None: the provider applies it).
    The others step up evenly to ``base + spread``, capped at 1.0: enough
    diversity for the vote to mean something, not enough to make passes
    sloppy.
    """
    if pass_index == 0 or passes <= 1:
        return None
    step = spread * pass_index / (passes - 1)
    return round(min(1.0, base + step), 3)


@dataclass
class PassOutcome(Generic[T]):
    """What one pass produced, or why it did not."""

    index: int
    seconds: float
    result: T | None = None
    error: BaseException | None = None
    status: str = "ok"  # ok | failed | late


@dataclass
class PassRun(Generic[T]):
    """Every pass of one part: those that landed first, in pass order."""

    outcomes: list[PassOutcome[T]] = field(default_factory=list)

    @property
    def landed(self) -> list[PassOutcome[T]]:
        return [o for o in self.outcomes if o.status == "ok"]

    def timing(self) -> list[dict]:
        return [
            {"pass": o.index, "seconds": round(o.seconds, 2), "status": o.status}
            for o in self.outcomes
        ]


async def run_passes(
    run_one: Callable[[int], Coroutine[Any, Any, T]],
    passes: int,
    *,
    soft_deadline: float | None = None,
) -> PassRun[T]:
    """Run ``passes`` passes concurrently and collect what lands.

    ``soft_deadline`` is a ``time.monotonic()`` value. Once it passes, the
    passes still running are cancelled and left out — as long as at least one
    landed; with none landed yet, the first to land is waited for (the part's
    own hard deadline still bounds that). A failed pass is left out too. Only
    when every pass failed does this raise, with the first pass's error, so
    the part's retry rules apply as they did to a single pass.
    """
    started = time.monotonic()
    tasks: dict[asyncio.Task[T], int] = {asyncio.create_task(run_one(i)): i for i in range(passes)}
    finished: dict[int, PassOutcome[T]] = {}

    def _collect(done: set[asyncio.Task[T]]) -> None:
        for task in done:
            idx = tasks[task]
            elapsed = time.monotonic() - started
            exc = task.exception() if not task.cancelled() else asyncio.CancelledError()
            if exc is None:
                finished[idx] = PassOutcome(idx, elapsed, result=task.result())
            else:
                logger.warning("Review pass %d failed: %s", idx + 1, exc)
                finished[idx] = PassOutcome(idx, elapsed, error=exc, status="failed")

    pending: set[asyncio.Task[T]] = set(tasks)
    try:
        timeout = None if soft_deadline is None else max(0.0, soft_deadline - time.monotonic())
        done, pending = await asyncio.wait(pending, timeout=timeout)
        _collect(done)
        while pending and not any(o.status == "ok" for o in finished.values()):
            done, pending = await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)
            _collect(done)
    finally:
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
    for task in pending:
        idx = tasks[task]
        logger.info("Review pass %d still running at the soft deadline; left out", idx + 1)
        finished[idx] = PassOutcome(idx, time.monotonic() - started, status="late")

    run = PassRun([finished[i] for i in sorted(finished)])
    if not run.landed:
        errors = [o.error for o in run.outcomes if o.error is not None]
        if errors and not isinstance(errors[0], asyncio.CancelledError):
            raise errors[0]
        raise TimeoutError("no review pass landed")
    return run


def same_location(a: ReviewComment, b: ReviewComment) -> bool:
    """Whether two passes' findings are one finding, for the vote.

    Stricter than the noise filter's duplicate check: passes vote on a
    *location*, so a look-alike title in another file or far down the same
    one is a different finding here, not a second vote for this one.
    """
    if a.path != b.path:
        return False
    a_end, b_end = a.end_line or a.line, b.end_line or b.line
    if a.line > b_end + _LOCATION_SLACK or b.line > a_end + _LOCATION_SLACK:
        return False
    return a.category == b.category or _jaccard_similarity(a.title, b.title) >= _TITLE_SIMILARITY


@dataclass
class VoteResult:
    kept: list[ReviewComment]
    # Findings the vote left out, each with how many passes reported it.
    dropped: list[tuple[ReviewComment, int]]
    single_vote_blockers: int = 0


def vote(runs: list[list[ReviewComment]], threshold: int) -> VoteResult:
    """Keep findings reported by at least ``threshold`` of ``runs``.

    ``threshold`` is clamped to the passes that landed, so two passes out of
    three landing with threshold 2 need both to agree, and one landing keeps
    everything it found (the single-pass review). Each kept finding carries
    ``votes``/``vote_passes``. A blocker under the threshold is kept with
    ``require_proven`` set — see the module docstring.
    """
    passes = len(runs)
    if passes == 0:
        return VoteResult([], [])
    needed = max(1, min(threshold, passes))
    kept: list[ReviewComment] = []
    dropped: list[tuple[ReviewComment, int]] = []
    single_blockers = 0
    for cluster in cluster_runs(runs, same_location):
        votes = cluster_votes(cluster)
        if votes >= needed:
            rep = cluster_representative(cluster)
        elif any(c.severity >= Severity.BLOCKER for _, c in cluster):
            blockers = [(i, c) for i, c in cluster if c.severity >= Severity.BLOCKER]
            rep = cluster_representative(blockers)
            rep.require_proven = True
            single_blockers += 1
        else:
            dropped.append((cluster_representative(cluster), votes))
            continue
        rep.votes = votes
        rep.vote_passes = passes
        kept.append(rep)
    return VoteResult(kept, dropped, single_blockers)


def vote_audit(part: int, run: PassRun, threshold: int, result: VoteResult, drafted: int) -> dict:
    """The audit entry for one part's vote: enough to tune the threshold from."""
    return {
        "stage": "pass_vote",
        "chunk": part,
        "passes": len(run.outcomes),
        "landed": len(run.landed),
        "threshold": threshold,
        "drafted": drafted,
        "kept": len(result.kept),
        "single_vote_blockers": result.single_vote_blockers,
        "timing": run.timing(),
        "votes": [
            {
                "path": c.path,
                "line": c.line,
                "title": c.title,
                "severity": c.severity.name,
                "votes": c.votes,
                "require_proven": c.require_proven,
            }
            for c in result.kept
        ],
        "dropped": [
            {
                "path": c.path,
                "line": c.line,
                "title": c.title,
                "severity": c.severity.name,
                "category": c.category,
                "votes": votes,
            }
            for c, votes in result.dropped
        ],
    }


def passes_per_part(
    configured: int,
    parts: int,
    max_runs: int,
    changed_lines: int,
    min_lines: int,
) -> int:
    """How many passes each part gets in this review.

    One on a change below ``min_lines`` changed lines — a typo fix gains
    nothing from three readings — and otherwise ``configured``, lowered
    evenly so ``parts × passes`` stays within ``max_runs``: each pass is an
    agentic loop of up to a dozen model calls, and a 30-part review at five
    passes would be 150 of them.
    """
    if configured <= 1 or changed_lines < min_lines or parts <= 0:
        return 1
    return max(1, min(configured, max_runs // parts))


def drop_unproven(
    comments: list[ReviewComment],
) -> tuple[list[ReviewComment], list[ReviewComment]]:
    """Split off blockers still waiting for a `proven` grade: ``(kept, dropped)``.

    Self-critique clears ``require_proven`` on a blocker it grades proven.
    One still marked after it — the critic said less, gave no verdict, failed,
    or is turned off — had only the vote to stand on, and lost it. When such
    a blocker carries a group (core/grouping.py), the group's next member the
    vote did carry is promoted instead of losing the whole group; under a
    posted finding that stands, occurrences stand with it, as grouping says.
    """
    kept: list[ReviewComment] = []
    dropped: list[ReviewComment] = []
    for c in comments:
        if not c.require_proven:
            kept.append(c)
            continue
        dropped.append(c)
        survivor = prune_group(c, lambda member: not member.require_proven)
        if survivor is not None:
            kept.append(survivor)
    return kept, dropped
