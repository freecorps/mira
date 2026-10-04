"""A digest as Markdown, and as the payload the outbound webhooks render.

Titles are contributors' text and summaries are a model's reading of it, so
both go through :mod:`mira.digests.text` before they are written anywhere:
one line, no mentions, and HTML escaped for the Markdown that ends up on a
release page or in a mail client.
"""

from __future__ import annotations

from typing import Any

from mira.digests.models import Change, Digest, day
from mira.digests.text import escape_html, one_line

MAX_CHANGES_PER_AREA = 15
MAX_WEBHOOK_AREAS = 12


def change_line(change: Change) -> str:
    title = escape_html(one_line(change.title, 160)) or "(untitled)"
    ref = escape_html(f"{change.repo}{change.ref}")
    linked = f"[{ref}]({change.url})" if change.url.startswith(("https://", "http://")) else ref
    author = f" by {escape_html(one_line(change.author, 60))}" if change.author else ""
    kind = " (direct commit)" if change.kind == "commit" else ""
    return f"- {title} ({linked}){author}{kind}"


def render_markdown(digest: Digest) -> str:
    lines = [
        f"## {escape_html(digest.title)}",
        "",
        f"_{day(digest.period_start)} to {day(digest.period_end)} (UTC)"
        + (f" on `{escape_html(digest.branch)}`_" if digest.branch else "_"),
        "",
    ]
    if digest.overview:
        lines += [escape_html(digest.overview), ""]
    if digest.is_empty:
        lines += ["Nothing landed in this period.", ""]
    for area in digest.areas:
        lines.append(f"### {escape_html(area.name)} ({len(area.changes)})")
        lines.append("")
        if area.summary:
            lines += [escape_html(area.summary), ""]
        for highlight in area.highlights:
            lines.append(f"- **{escape_html(highlight)}**")
        if area.highlights:
            lines.append("")
        for change in area.changes[:MAX_CHANGES_PER_AREA]:
            lines.append(change_line(change))
        if len(area.changes) > MAX_CHANGES_PER_AREA:
            lines.append(f"- … and {len(area.changes) - MAX_CHANGES_PER_AREA} more")
        lines.append("")
    for note in digest.notes:
        lines.append(f"> {escape_html(note)}")
    if digest.notes:
        lines.append("")
    lines.append(
        f"<sub>{digest.pull_requests} pull request(s), {digest.direct_commits} direct "
        "commit(s). Written by Mira"
        + (" with a model reading the pull requests." if digest.llm_used else ".")
        + "</sub>"
    )
    return "\n".join(lines).rstrip() + "\n"


def render_text(digest: Digest) -> str:
    """Plain text for email: the Markdown is readable as it is."""
    return render_markdown(digest)


def webhook_payload(digest: Digest, *, digest_id: int = 0) -> dict[str, Any]:
    """The ``digest.ready`` event data. Bounded: the full digest is in the dashboard."""
    return {
        "digest_id": digest_id,
        "platform": digest.platform,
        "repo": digest.scope_name,
        "title": digest.title,
        "period_start": digest.period_start,
        "period_end": digest.period_end,
        "branch": digest.branch,
        "overview": digest.overview,
        "pull_requests": digest.pull_requests,
        "direct_commits": digest.direct_commits,
        "areas": [
            {
                "name": area.name,
                "count": len(area.changes),
                "summary": area.summary,
                "highlights": list(area.highlights),
            }
            for area in digest.areas[:MAX_WEBHOOK_AREAS]
        ],
        "more_areas": max(0, len(digest.areas) - MAX_WEBHOOK_AREAS),
    }
