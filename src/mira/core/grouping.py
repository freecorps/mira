"""One comment per root cause, and a comment cap that never forgets a finding.

Two problems with posting findings one per location under a fixed cap:

- The same mistake made in five files (a missing ``await`` on one helper, the
  same unvalidated parameter in five handlers) spent five of the cap's slots
  and drowned out everything else. The noise filter already recognised these
  as duplicates of each other, and dropped all but the first — so the author
  fixed one call site and never heard about the other four.
- Whatever did not fit under ``filter.max_comments`` was dropped silently:
  not posted, not listed, not persisted, not counted.

So a finding that repeats is *grouped*: the strongest occurrence is posted with
an "Also at" list of the others, and every occurrence is still persisted as a
finding of its own that points at the posted one. And a finding past the cap
*overflows*: it is listed in the walkthrough, persisted, and counted by the
verdict and the commit status like any other. The cap decides what is posted
inline; it never decides what Mira knows.

Everything here is deterministic — no model call — so it costs nothing and an
operator can predict it from the findings alone.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from urllib.parse import quote

from mira.models import ReviewComment, Severity

# Listing every location of a pattern that recurs in 80 files would bury the
# comment under its own footnote; past this many the rest are counted.
MAX_LISTED_OCCURRENCES = 20
# Same reasoning for the walkthrough's "More findings" section.
MAX_LISTED_OVERFLOW = 50


def rank_key(comment: ReviewComment) -> tuple[Severity, float]:
    """Severity first, then confidence: the order findings compete for the cap in."""
    return (comment.severity, comment.confidence)


def _overlaps(a: ReviewComment, b: ReviewComment) -> bool:
    if a.path != b.path:
        return False
    return a.line <= (b.end_line or b.line) and b.line <= (a.end_line or a.line)


def is_other_occurrence(primary: ReviewComment, candidate: ReviewComment) -> bool:
    """Whether a duplicate of ``primary`` is the same problem somewhere else.

    The caller has already decided the two read alike; this decides whether
    that likeness is one finding said twice (same place: drop it) or one
    problem found twice (another place, same category: group it). A look-alike
    in a different category is not grouped, because "the same words" is not
    "the same root cause" when one is a bug and the other a style note; the
    duplicate check drops it, as it always has, since it is the same text
    under another label rather than a second problem.
    """
    return primary.category == candidate.category and not _overlaps(primary, candidate)


def add_occurrence(primary: ReviewComment, other: ReviewComment) -> None:
    """Fold ``other`` (and anything already grouped under it) into ``primary``.

    Groups stay flat — an occurrence never carries occurrences of its own — and
    a location already in the group is not listed twice.
    """
    incoming = [other, *other.occurrences]
    other.occurrences = []
    for candidate in incoming:
        candidate.occurrences = []
        if any(_overlaps(candidate, member) for member in (primary, *primary.occurrences)):
            continue
        primary.occurrences.append(candidate)


def prune_group(
    comment: ReviewComment, keep: Callable[[ReviewComment], bool]
) -> ReviewComment | None:
    """The group with the members ``keep`` rejects removed, or None if none is left.

    When the posted member itself is rejected (its line already has an open
    thread, or it sits outside the pull request's diff) the next occurrence is
    promoted to carry the group: the problem at the other locations is still
    unreported, and dropping the whole group would lose it.
    """
    members = [member for member in (comment, *comment.occurrences) if keep(member)]
    comment.occurrences = []
    if not members:
        return None
    head, rest = members[0], members[1:]
    head.occurrences = rest
    return head


def cap_comments(
    comments: list[ReviewComment],
    max_comments: int,
    *,
    exempt_severity: Severity = Severity.WARNING,
) -> tuple[list[ReviewComment], list[ReviewComment]]:
    """Split ranked findings into ``(posted, overflow)``.

    Findings at ``exempt_severity`` or above are always posted; the rest share
    ``max_comments`` slots in rank order. Blockers are exempt whatever the
    setting says: a blocker hidden in a collapsed section is a blocker merged.
    A group counts as one finding — that is the point of grouping it.
    """
    exempt_at = min(exempt_severity, Severity.BLOCKER)
    ranked = sorted(comments, key=rank_key, reverse=True)
    exempt = [c for c in ranked if c.severity >= exempt_at]
    capped = [c for c in ranked if c.severity < exempt_at]
    limit = max(0, max_comments)
    return exempt + capped[:limit], capped[limit:]


# ── Links to a location in the reviewed commit ──

_PR_URL_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    # GitHub (and GitHub Enterprise): …/owner/repo/pull/12
    (re.compile(r"^(?P<repo>https?://.+?)/pull/\d+/?$"), "{repo}/blob/{sha}/{path}#L{line}"),
    # GitLab: …/group/project/-/merge_requests/12
    (
        re.compile(r"^(?P<repo>https?://.+?)/-/merge_requests/\d+/?$"),
        "{repo}/-/blob/{sha}/{path}#L{line}",
    ),
    # Forgejo / Gitea: …/owner/repo/pulls/12
    (re.compile(r"^(?P<repo>https?://.+?)/pulls/\d+/?$"), "{repo}/src/commit/{sha}/{path}#L{line}"),
)


def location_url(pr_url: str, sha: str, path: str, line: int) -> str:
    """A link to ``path:line`` at ``sha`` on the pull request's platform, or "".

    Pinned to the reviewed commit rather than the branch, so the link still
    points at the line that was flagged after the author pushes again.
    """
    if not pr_url or not sha or not path:
        return ""
    for pattern, template in _PR_URL_PATTERNS:
        match = pattern.match(pr_url.strip())
        if match:
            return template.format(
                repo=match.group("repo"), sha=sha, path=quote(path), line=max(1, line)
            )
    return ""


def format_location(comment: ReviewComment, pr_url: str = "", sha: str = "") -> str:
    """``path:line`` as inline code, linked when the platform URL is known."""
    label = f"`{comment.path}:{comment.line}`"
    url = location_url(pr_url, sha, comment.path, comment.line)
    return f"[{label}]({url})" if url else label


def format_also_at(comment: ReviewComment, pr_url: str = "", sha: str = "") -> str:
    """The "Also at" block for a grouped comment, or "" for a lone finding."""
    if not comment.occurrences:
        return ""
    shown = comment.occurrences[:MAX_LISTED_OCCURRENCES]
    lines = ["**Also at:**"]
    lines.extend(f"- {format_location(other, pr_url, sha)}" for other in shown)
    hidden = len(comment.occurrences) - len(shown)
    if hidden:
        lines.append(f"- _…and {hidden} more_")
    return "\n".join(lines)


def _one_line(text: str) -> str:
    return " ".join((text or "").split())


def render_overflow_section(
    overflow: list[ReviewComment],
    *,
    pr_url: str = "",
    sha: str = "",
    max_comments: int = 0,
) -> str:
    """The walkthrough's collapsible "More findings" section, or "" when empty."""
    if not overflow:
        return ""
    shown = overflow[:MAX_LISTED_OVERFLOW]
    limit = f" (`filter.max_comments`: {max_comments})" if max_comments else ""
    lines = [
        "<details>",
        f"<summary><b>More findings ({len(overflow)})</b> — not posted inline, "
        f"past the comment limit{limit}</summary>",
        "",
    ]
    for comment in shown:
        severity = comment.severity.name.lower()
        entry = (
            f"- {comment.severity.emoji} **{severity}** · "
            f"{format_location(comment, pr_url, sha)} — {_one_line(comment.title)}"
        )
        if comment.occurrences:
            count = len(comment.occurrences)
            entry += f" _(also at {count} other location{'s' if count != 1 else ''})_"
        lines.append(entry)
    hidden = len(overflow) - len(shown)
    if hidden:
        lines.append(f"- _…and {hidden} more_")
    lines.extend(
        [
            "",
            "Each one is recorded and counts toward the review's verdict like a posted comment.",
            "",
            "</details>",
        ]
    )
    return "\n".join(lines)
