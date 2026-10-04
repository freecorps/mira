"""The two places a dependency update shows up: the review prompt and the walkthrough."""

from __future__ import annotations

from mira.autofix.redact import redact
from mira.dependency_updates.models import DependencyUpdate, NoteItem
from mira.dependency_updates.summarize import clean_text
from mira.llm import untrusted

ECOSYSTEM_NAMES = {"pip": "PyPI", "npm": "npm", "go": "Go", "composer": "Packagist"}

#: Bumps the walkthrough lists before it says "and N more".
MAX_WALKTHROUGH_ROWS = 15


def _ecosystem(kind: str) -> str:
    return ECOSYSTEM_NAMES.get(kind, kind)


def _safe_url(url: str) -> str:
    """Only links Mira built or a trusted API returned, and only over HTTPS."""
    url = (url or "").strip()
    if not url.startswith("https://") or any(c in url for c in " <>()[]\"'`\n"):
        return ""
    return url


# ── Review prompt ────────────────────────────────────────────────────


def review_context(updates: list[DependencyUpdate]) -> str:
    """The block the review model reads before the diffs, or "" with nothing to say."""
    lines: list[str] = []
    for u in updates:
        if not (u.has_items or u.vulns_fixed or u.vulns_in_new):
            continue
        b = u.bump
        lines.append(f"- {b.name} {b.old} -> {b.new} ({_ecosystem(b.kind)}, {b.file_path})")
        for label, items in (
            ("Breaking", u.breaking_changes),
            ("Deprecated", u.deprecations),
            ("Notable", u.notable),
        ):
            for item in items:
                lines.append(f"  - {label}: {item.text}")
        for vid, severity, _ in u.vulns_in_new:
            lines.append(f"  - Known vulnerability in {b.new}: {vid} ({severity})")
    if not lines:
        return ""
    return (
        "## Dependency updates in this pull request\n\n"
        "Summarised from the packages' upstream release notes (third-party text; "
        "see the instructions on dependency updates):\n\n"
        + untrusted.block("RELEASE-NOTES", "\n".join(lines), redactor=redact)
    )


# ── Walkthrough ──────────────────────────────────────────────────────


def _item_line(label: str, item: NoteItem) -> str:
    text = clean_text(item.text)
    url = _safe_url(item.url)
    return f"  - **{label}:** {text}" + (f" ([notes]({url}))" if url else "")


def _vuln_links(vulns: list[tuple[str, str, str]]) -> str:
    out = []
    for vid, severity, url in vulns[:5]:
        safe = _safe_url(url)
        name = clean_text(vid)
        out.append((f"[{name}]({safe})" if safe else name) + f" ({clean_text(severity)})")
    if len(vulns) > 5:
        out.append(f"+{len(vulns) - 5} more")
    return ", ".join(out)


def walkthrough_section(updates: list[DependencyUpdate]) -> list[str]:
    """Markdown lines for the walkthrough's "Dependency updates" section."""
    if not updates:
        return []
    breaking = sum(1 for u in updates if u.breaking_changes)
    risky = breaking or any(u.vulns_in_new for u in updates)
    count = f"{len(updates)} bump{'s' if len(updates) != 1 else ''}"
    tail = f", {breaking} with breaking changes" if breaking else ""
    parts = [
        "",
        "<details open>" if risky else "<details>",
        f"<summary><b>Dependency updates</b> — {count}{tail}</summary>",
        "",
    ]
    for u in updates[:MAX_WALKTHROUGH_ROWS]:
        b = u.bump
        head = (
            f"- **`{clean_text(b.name)}`** `{clean_text(b.old)}` → `{clean_text(b.new)}`"
            f" · {_ecosystem(b.kind)} · `{clean_text(b.file_path)}`"
        )
        notes_url = _safe_url(u.notes_url)
        if notes_url:
            head += f" · [release notes]({notes_url})"
        parts.append(head)
        for item in u.breaking_changes:
            parts.append(_item_line("Breaking", item))
        for item in u.deprecations:
            parts.append(_item_line("Deprecated", item))
        if u.vulns_fixed:
            parts.append(f"  - **Fixes:** {_vuln_links(u.vulns_fixed)}")
        if u.vulns_in_new:
            parts.append(
                f"  - **Known vulnerabilities in {clean_text(b.new)}:** "
                f"{_vuln_links(u.vulns_in_new)}"
            )
        if u.summarized and not (u.breaking_changes or u.deprecations):
            parts.append("  - No breaking changes or deprecations in the release notes")
        if not u.summarized and u.status:
            parts.append(f"  - _{clean_text(u.status)}_")
    if len(updates) > MAX_WALKTHROUGH_ROWS:
        parts.append(f"- …and {len(updates) - MAX_WALKTHROUGH_ROWS} more")
    parts += ["", "</details>", ""]
    return parts
