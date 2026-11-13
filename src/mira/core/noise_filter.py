"""Noise filtering pipeline for review comments."""

from __future__ import annotations

from mira.config import FilterConfig
from mira.core.grouping import add_occurrence, cap_comments, is_other_occurrence, prune_group
from mira.models import ReviewComment, Severity


def _jaccard_similarity(a: str, b: str) -> float:
    """Compute Jaccard similarity between two strings (word-level)."""
    words_a = set(a.lower().split())
    words_b = set(b.lower().split())
    if not words_a or not words_b:
        return 0.0
    intersection = words_a & words_b
    union = words_a | words_b
    return len(intersection) / len(union)


def _lines_overlap(c1: ReviewComment, c2: ReviewComment) -> bool:
    """Check if two comments target the same file and overlapping lines."""
    if c1.path != c2.path:
        return False
    end1 = c1.end_line or c1.line
    end2 = c2.end_line or c2.line
    return c1.line <= end2 and c2.line <= end1


def _is_duplicate(
    a: ReviewComment,
    b: ReviewComment,
    title_threshold: float = 0.6,
) -> bool:
    """Determine if two comments are duplicates using a composite check.

    Two comments are duplicates if:
    - Same file + overlapping lines + any title similarity (>= 0.25), OR
    - Same file + exact same line + same category (one line can host two
      distinct findings — e.g. a resource leak and an injection on one
      `open()` call — so different categories are kept apart)
    - Same file + non-overlapping lines but near-identical titles (>= title_threshold)
    """
    title_sim = _jaccard_similarity(a.title, b.title)

    # Cross-file: near-identical title + similar body → same conceptual issue
    if a.path != b.path:
        body_sim = _jaccard_similarity(a.body, b.body)
        return title_sim >= 0.8 and body_sim >= 0.5

    overlap = _lines_overlap(a, b)

    # Exact same line + same category — almost certainly the same finding.
    # Different categories are distinct findings even on the same line;
    # collapsing them lets a later critique drop take both out.
    end_a = a.end_line or a.line
    end_b = b.end_line or b.line
    same_line = (a.line == b.line) and (end_a == end_b)
    if same_line and a.category == b.category:
        return True

    # Overlapping lines — lower title similarity bar
    if overlap and title_sim >= 0.2:
        return True

    # Non-overlapping but very similar titles in the same file
    return title_sim >= title_threshold


def _deduplicate(
    comments: list[ReviewComment],
    title_threshold: float = 0.6,
    *,
    group: bool = False,
) -> list[ReviewComment]:
    """Remove duplicate comments using composite similarity.

    With ``group``, a duplicate found at *another* location in the same
    category is the same problem recurring, not the same comment twice: it is
    kept as an occurrence of the first one (see ``mira.core.grouping``) rather
    than dropped. Input order is rank order, so the strongest occurrence is
    the one that carries the group.
    """
    if not comments:
        return []

    kept: list[ReviewComment] = []
    for comment in comments:
        match = next(
            (existing for existing in kept if _is_duplicate(comment, existing, title_threshold)),
            None,
        )
        if match is None:
            kept.append(comment)
        elif group and is_other_occurrence(match, comment):
            add_occurrence(match, comment)

    return kept


def filter_noise(
    comments: list[ReviewComment],
    config: FilterConfig,
    review_round: int = 1,
    *,
    group_findings: bool = False,
    apply_cap: bool = True,
) -> list[ReviewComment]:
    """Apply the full noise filtering pipeline.

    1. Drop below confidence threshold (escalates per ``review_round``)
    2. Drop below minimum severity (escalates per ``review_round``)
    3. Sort by severity (desc) then confidence (desc)
    4. Deduplicate (first occurrence = highest quality); with
       ``group_findings``, the same problem at another location becomes an
       occurrence of the first instead of being dropped
    5. Apply the cap **only to suggestions and nitpicks** — every blocker
       and warning that survives the floor gets posted, no matter how
       many. Real bugs deserve to be flagged; the cap is a noise control
       for low-priority findings, not a quality cap on real ones.

    ``apply_cap=False`` skips step 5 and leaves the cap to the caller: the
    review engine caps after self-critique with :func:`cap_comments`, so the
    findings past the cap are graded like the rest and kept as overflow
    instead of being discarded here.

    ``review_round`` is the 1-indexed count of bot reviews on this PR.
    Round 1 uses ``config`` defaults; round 2+ raise the floor so the bot
    converges quickly across follow-up pushes — the principle being
    "round 1 sets the bar; later rounds only flag new high-confidence
    findings, typically bugs the team introduced while applying fixes."
    """
    confidence_floor = config.confidence_threshold
    min_severity = Severity.from_str(config.min_severity)

    if review_round >= 2:
        # Tighten without dropping real catches: confident bugs are scored
        # 0.85+ by the LLM, so 0.8 still lets them through while filtering
        # the marginal/hedged ones.
        confidence_floor = max(confidence_floor, 0.8)
        min_severity = max(min_severity, Severity.WARNING)
    if review_round >= 3:
        confidence_floor = max(confidence_floor, 0.85)

    category_floors = config.category_confidence_thresholds

    def _floor(c: ReviewComment) -> float:
        return max(confidence_floor, category_floors.get(c.category, 0.0))

    result = [c for c in comments if c.confidence >= _floor(c)]
    result = [c for c in result if c.severity >= min_severity]
    result.sort(key=lambda c: (c.severity, c.confidence), reverse=True)
    result = _deduplicate(result, group=group_findings)
    if not apply_cap:
        return result

    # Split: blockers + warnings always post; suggestions + nitpicks share
    # the cap so they can't drown out real findings.
    posted, _ = cap_comments(result, config.max_comments, exempt_severity=Severity.WARNING)
    return posted


def drop_already_posted(
    comments: list[ReviewComment],
    existing_threads: list,
) -> list[ReviewComment]:
    """Drop comments that overlap an open bot thread from an earlier round.

    filter_noise dedupes within one review; this dedupes across rounds, so a
    re-review on the next push doesn't re-post a finding that still has an open
    thread. Threads with a non-positive line (outdated) can't be located, so
    they never suppress.
    """
    if not existing_threads:
        return comments

    open_lines: dict[str, list[int]] = {}
    for t in existing_threads:
        if t.line > 0:
            open_lines.setdefault(t.path, []).append(t.line)
    if not open_lines:
        return comments

    def _not_posted(c: ReviewComment) -> bool:
        end = c.end_line or c.line
        return not any(c.line <= ln <= end for ln in open_lines.get(c.path, []))

    # Per occurrence, not per group: a grouped finding whose first location
    # already has a thread still has news at the others.
    kept: list[ReviewComment] = []
    for c in comments:
        survivor = prune_group(c, _not_posted)
        if survivor is not None:
            kept.append(survivor)
    return kept
