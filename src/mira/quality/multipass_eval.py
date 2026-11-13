"""Evaluating the shuffled voting passes and the system pass with the backtest harness.

The question is a recall question — does reading each part three times, plus
once as a whole, catch what the other review bots caught and Mira missed? —
so it is answered with the harness that already replays reviews without
posting (mira.quality.backtest), on the pull requests where those misses are:

- :func:`pass_variants` turns one configuration into the A/B pair under test:
  A is the single-pass review, B the same configuration with the voting passes
  and the system pass on. Everything else is identical, so the difference is
  the feature.
- :func:`peer_miss_prs` picks the pull requests the peer-recall scan
  (docs/peer-recall.md) recorded a missed-and-addressed thread on. Those
  threads are ground truth already: :func:`mira.quality.ground_truth.peer_signals`
  scores them as ``peer_addressed`` signals.
- :func:`recall_by_kind` splits recall per signal kind, so the report can say
  "peer threads caught: 2/9 → 5/9" rather than one number that mixes them with
  human comments and fix commits.

``mira backtest --compare-passes [--peer-misses]`` runs it; see
docs/multipass-review.md.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any

from mira.quality.lines import ranges_overlap
from mira.quality.models import PRBacktest

# B's pass count when the configuration under test is itself single-pass:
# the default, so "compare passes" means what the feature ships with.
DEFAULT_COMPARED_PASSES = 3


def pass_variants(config: Any, label: str = "default") -> list[tuple[str, str, Any]]:
    """``[(name, label, config), …]`` for A (one pass) and B (voting + system)."""
    passes = config.review.passes if config.review.passes > 1 else DEFAULT_COMPARED_PASSES
    single = config.model_copy(deep=True)
    single.review.passes = 1
    single.review.system_pass = False
    multi = config.model_copy(deep=True)
    multi.review.passes = passes
    multi.review.system_pass = True
    return [
        ("A", f"{label} · passes=1", single),
        ("B", f"{label} · passes={passes} + system", multi),
    ]


def peer_miss_prs(quality_store: Any, *, since: float = 0.0, limit: int = 25) -> list[int]:
    """Pull requests with a peer thread the author addressed and Mira missed, newest first."""
    if quality_store is None:
        return []
    numbers: list[int] = []
    offset = 0
    while len(numbers) < limit:
        page = quality_store.list_peer_threads(
            addressed=True, flagged=False, since=since, limit=200, offset=offset
        )
        if not page:
            break
        for thread in page:
            if thread.pr_number not in numbers:
                numbers.append(int(thread.pr_number))
        offset += len(page)
    return numbers[:limit]


def recall_by_kind(
    results: list[PRBacktest], tolerance: int = 3
) -> dict[str, dict[str, tuple[int, int]]]:
    """``{variant: {signal kind: (caught, total)}}`` over positive signals.

    A signal is caught when a finding of that variant on that pull request
    lands within ``tolerance`` lines of it — the scoring rule, per kind.
    """
    out: dict[str, dict[str, list[int]]] = defaultdict(lambda: defaultdict(lambda: [0, 0]))
    for result in results:
        if result.error or result.skipped_reason:
            continue
        for signal in result.signals:
            if not signal.positive:
                continue
            cell = out[result.variant][signal.kind]
            cell[1] += 1
            if any(
                f.path == signal.path
                and ranges_overlap(
                    f.line, f.end_line or f.line, signal.start, signal.end, tolerance
                )
                for f in result.findings
            ):
                cell[0] += 1
    return {
        variant: {kind: (cell[0], cell[1]) for kind, cell in sorted(kinds.items())}
        for variant, kinds in sorted(out.items())
    }


def render_recall_by_kind(results: list[PRBacktest], tolerance: int = 3) -> str:
    """A Markdown table of :func:`recall_by_kind`, or "" with no positive signals."""
    table = recall_by_kind(results, tolerance)
    kinds = sorted({kind for per in table.values() for kind in per})
    if not kinds:
        return ""
    variants = sorted(table)
    lines = [
        "### Recall by signal kind",
        "",
        "| Signal | " + " | ".join(variants) + " |",
        "|---|" + "---:|" * len(variants),
    ]
    for kind in kinds:
        cells = []
        for variant in variants:
            caught, total = table[variant].get(kind, (0, 0))
            cells.append(f"{caught}/{total}" + (f" ({caught / total:.0%})" if total else ""))
        lines.append(f"| `{kind}` | " + " | ".join(cells) + " |")
    return "\n".join(lines)
