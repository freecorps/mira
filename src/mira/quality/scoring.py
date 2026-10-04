"""Scoring a replayed review's findings against ground-truth signals."""

from __future__ import annotations

from typing import Any

from mira.quality.lines import ranges_overlap
from mira.quality.models import (
    LABEL_FP,
    LABEL_TP,
    LABEL_UNLABELLED,
    Score,
    ScoredFinding,
    Signal,
)


def _severity_name(value: Any) -> str:
    name = getattr(value, "name", None)
    return str(name).lower() if name else str(value or "").lower()


def _matches(path: str, start: int, end: int, signal: Signal, tolerance: int) -> bool:
    return path == signal.path and ranges_overlap(start, end, signal.start, signal.end, tolerance)


def score_findings(
    comments: list[Any], signals: list[Signal], *, tolerance: int = 3
) -> tuple[list[ScoredFinding], Score]:
    """Label each finding and count which positive signals were caught.

    A finding is a true positive when it lands on any positive signal, a false
    positive when it lands only on negative ones, and unlabelled otherwise.
    A positive signal is caught when any finding lands on it.
    """
    positives = [s for s in signals if s.positive]
    negatives = [s for s in signals if not s.positive]
    scored: list[ScoredFinding] = []
    caught: set[int] = set()
    score = Score(positives=len(positives))

    for comment in comments:
        path = str(getattr(comment, "path", "") or "")
        start = int(getattr(comment, "line", 0) or 0)
        end = int(getattr(comment, "end_line", 0) or 0) or start
        hits = [
            i for i, signal in enumerate(positives) if _matches(path, start, end, signal, tolerance)
        ]
        misses = [s for s in negatives if _matches(path, start, end, s, tolerance)]
        caught.update(hits)
        if hits:
            label = LABEL_TP
            matched = sorted({positives[i].kind for i in hits})
            score.tp += 1
        elif misses:
            label = LABEL_FP
            matched = sorted({s.kind for s in misses})
            score.fp += 1
        else:
            label = LABEL_UNLABELLED
            matched = []
            score.unlabelled += 1
        scored.append(
            ScoredFinding(
                path=path,
                line=start,
                end_line=end,
                severity=_severity_name(getattr(comment, "severity", "")),
                category=str(getattr(comment, "category", "") or ""),
                title=str(getattr(comment, "title", "") or ""),
                confidence=float(getattr(comment, "confidence", 0.0) or 0.0),
                label=label,
                matched=matched,
            )
        )

    score.findings = len(scored)
    score.positives_caught = len(caught)
    return scored, score
