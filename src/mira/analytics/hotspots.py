"""Change-frequency heatmap and hotspot ranking.

A hotspot is code that changes often, is big or dense, and keeps drawing
findings: the place a reviewer's attention pays back most. Each signal alone
misleads — a changelog churns constantly, a large generated file never
changes, a file with many findings may have had one bad week — so the score
multiplies them:

    churn       = changes + lines_changed / 100
    complexity  = 1 + log2(1 + loc / 100 + symbols / 5)
    density     = findings / changes
    score       = churn × complexity × (1 + density)

``changes`` is the larger of two independent counts so that either source
alone gives a usable heatmap: distinct default-branch changes recorded in
``file_churn`` (provider commit history + merges Mira watched), and distinct
PRs Mira reviewed that touched the file (``review_events.reviewed_paths``).
Taking the max rather than the sum avoids counting one PR twice when both
sources saw it.

Complexity is a log so a 5,000-line file is not fifty times a 100-line one;
an unindexed file gets 1.0, which ranks it on churn and findings alone.
"""

from __future__ import annotations

import logging
import math
import time
from dataclasses import asdict, dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

DAY = 86400.0


@dataclass
class FileHotspot:
    path: str
    changes: int = 0
    prs: int = 0
    lines_changed: int = 0
    loc: int = 0
    symbols: int = 0
    findings: int = 0
    churn: float = 0.0
    complexity: float = 1.0
    finding_density: float = 0.0
    score: float = 0.0
    last_changed_at: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class DirectoryHotspot:
    path: str
    files: int = 0
    changes: int = 0
    lines_changed: int = 0
    findings: int = 0
    score: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class HotspotReport:
    window_days: int
    files: list[FileHotspot] = field(default_factory=list)
    directories: list[DirectoryHotspot] = field(default_factory=list)
    total_files: int = 0
    max_score: float = 0.0
    sources: dict[str, bool] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "window_days": self.window_days,
            "files": [f.to_dict() for f in self.files],
            "directories": [d.to_dict() for d in self.directories],
            "total_files": self.total_files,
            "max_score": self.max_score,
            "sources": self.sources,
        }


def complexity_proxy(loc: int, symbols: int) -> float:
    return 1.0 + math.log2(1.0 + max(0, loc) / 100.0 + max(0, symbols) / 5.0)


def score_file(
    path: str,
    *,
    changes: int,
    prs: int = 0,
    lines_changed: int = 0,
    loc: int = 0,
    symbols: int = 0,
    findings: int = 0,
    last_changed_at: float = 0.0,
) -> FileHotspot:
    touches = max(changes, prs)
    churn = touches + max(0, lines_changed) / 100.0
    complexity = complexity_proxy(loc, symbols)
    density = (findings / touches) if touches else 0.0
    score = churn * complexity * (1.0 + density)
    return FileHotspot(
        path=path,
        changes=touches,
        prs=prs,
        lines_changed=lines_changed,
        loc=loc,
        symbols=symbols,
        findings=findings,
        churn=round(churn, 3),
        complexity=round(complexity, 3),
        finding_density=round(density, 3),
        score=round(score, 3),
        last_changed_at=last_changed_at,
    )


def directory_of(path: str, depth: int) -> str:
    parts = path.split("/")[:-1]
    if not parts:
        return "."
    return "/".join(parts[: max(1, depth)])


def rank(
    *,
    churn_stats: list[dict[str, Any]],
    reviewed_paths: dict[str, set[int]],
    findings: dict[str, int],
    complexity: dict[str, tuple[int, int]],
    window_days: int,
    limit: int = 200,
    dir_depth: int = 2,
) -> HotspotReport:
    """Combine the raw signals into a ranked report. Pure — no I/O."""
    churn_by_path = {row["path"]: row for row in churn_stats}
    paths = set(churn_by_path) | set(reviewed_paths)
    scored: list[FileHotspot] = []
    for path in paths:
        row = churn_by_path.get(path, {})
        loc, symbols = complexity.get(path, (0, 0))
        hotspot = score_file(
            path,
            changes=int(row.get("changes") or 0),
            prs=len(reviewed_paths.get(path, ())),
            lines_changed=int(row.get("lines") or 0),
            loc=loc,
            symbols=symbols,
            findings=int(findings.get(path, 0)),
            last_changed_at=float(row.get("last_changed_at") or 0.0),
        )
        if hotspot.changes > 0:
            scored.append(hotspot)
    scored.sort(key=lambda h: (-h.score, -h.changes, h.path))

    dirs: dict[str, DirectoryHotspot] = {}
    for h in scored:
        key = directory_of(h.path, dir_depth)
        d = dirs.setdefault(key, DirectoryHotspot(path=key))
        d.files += 1
        d.changes += h.changes
        d.lines_changed += h.lines_changed
        d.findings += h.findings
        d.score = round(d.score + h.score, 3)
    directories = sorted(dirs.values(), key=lambda d: (-d.score, d.path))

    return HotspotReport(
        window_days=window_days,
        files=scored[:limit],
        directories=directories[:limit],
        total_files=len(scored),
        max_score=scored[0].score if scored else 0.0,
        sources={
            "history": bool(churn_stats),
            "reviews": bool(reviewed_paths),
            "findings": bool(findings),
            "index": bool(complexity),
        },
    )


def compute_hotspots(
    store: Any,
    *,
    window_days: int = 90,
    limit: int = 200,
    now: float = 0.0,
) -> HotspotReport:
    """Read every signal from an index store and rank. Never raises on a
    missing signal: an older index without a table just contributes nothing."""
    since = (now or time.time()) - window_days * DAY

    def _safe(fn: Any, default: Any) -> Any:
        try:
            return fn()
        except Exception as exc:  # noqa: BLE001 - one missing signal is not fatal
            logger.debug("Hotspot signal unavailable: %s", exc)
            return default

    return rank(
        churn_stats=_safe(lambda: store.file_churn_stats(since), []),
        reviewed_paths=_safe(lambda: store.reviewed_pr_paths(since), {}),
        findings=_safe(lambda: store.finding_counts_by_path(since), {}),
        complexity=_safe(lambda: store.file_complexity(), {}),
        window_days=window_days,
        limit=limit,
    )


def hotspot_review_note(store: Any, changed_paths: list[str], cfg: Any, *, now: float = 0.0) -> str:
    """A short review-context block naming the top hotspots this PR touches.

    Empty when the feature is off, nothing qualifies, or anything fails —
    a review must never wait on, or break because of, an analytics read.
    """
    if not changed_paths or cfg is None:
        return ""
    if not getattr(cfg, "enabled", False) or not getattr(cfg, "review_note", False):
        return ""
    try:
        report = compute_hotspots(
            store, window_days=int(cfg.window_days), limit=int(cfg.review_top_n), now=now
        )
    except Exception as exc:  # noqa: BLE001
        logger.debug("Hotspot review note skipped: %s", exc)
        return ""
    changed = set(changed_paths)
    hits = [
        h
        for h in report.files
        if h.path in changed and h.changes >= int(getattr(cfg, "review_min_changes", 1))
    ]
    if not hits:
        return ""
    lines = [
        "",
        "",
        "### Change Hotspots",
        "This PR touches files that change often in this repository and have "
        "drawn findings before. Scrutinize these closely for regressions, "
        "edge cases and missing tests:",
    ]
    for h in hits[:5]:
        rank_no = report.files.index(h) + 1
        detail = f"{h.changes} changes in {report.window_days}d"
        if h.findings:
            detail += f", {h.findings} prior finding(s)"
        lines.append(f"- `{h.path}` (hotspot #{rank_no}: {detail})")
    return "\n".join(lines) + "\n"
