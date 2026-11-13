"""Markdown formatting for review comments — shared across providers.

GitHub and GitLab both render the same Markdown (category badge, severity,
``suggestion`` blocks), so the comment body builders live here rather than in
either provider. Provider-specific concerns (how a comment is *posted*) stay in
the provider classes.
"""

from __future__ import annotations

import html
import re

from mira.core.grouping import format_also_at
from mira.feedback.provenance import finding_marker, parse_finding_id, votes_marker
from mira.models import KeyIssue, PRInfo, ReviewComment, Severity

_CATEGORY_DISPLAY: dict[str, tuple[str, str]] = {
    "bug": ("\U0001f41b", "Bug"),
    "security": ("\U0001f512", "Security issue"),
    "performance": ("⚡", "Performance"),
    "error-handling": ("⚠️", "Error Handling"),
    "race-condition": ("\U0001f3c1", "Race Condition"),
    "resource-leak": ("\U0001f4a7", "Resource Leak"),
    "maintainability": ("\U0001f527", "Refactor suggestion"),
    "style": ("\U0001f3a8", "Style"),
    "clarity": ("\U0001f4dd", "Clarity"),
    "configuration": ("⚙️", "Configuration"),
    "license": ("\U0001f4dc", "License compliance"),
    "other": ("\U0001f4cc", "Note"),
}

_SEVERITY_BADGE: dict[Severity, str] = {
    Severity.BLOCKER: "\U0001f6d1 Blocker — must fix before merge",
    Severity.WARNING: "⚠️ Warning",
    Severity.SUGGESTION: "\U0001f4a1 Suggestion",
    Severity.NITPICK: "\U0001f4ac Nitpick",
}

_LABEL_TO_CATEGORY = {label: cat for cat, (_, label) in _CATEGORY_DISPLAY.items()}
_CATEGORY_EMOJI_TO_NAME = {emoji: cat for cat, (emoji, _) in _CATEGORY_DISPLAY.items()}
_SEVERITY_EMOJI_MAP: dict[str, str] = {
    "\U0001f6d1": "blocker",
    "⚠️": "warning",
    "⚠": "warning",
    "\U0001f4a1": "suggestion",
    "\U0001f4ac": "nitpick",
}

_CATEGORY_LINE_RE = re.compile(r"^(\S+)\s+\*\*([^*]+)\*\*\s*$")
_BOLD_LINE_RE = re.compile(r"^\*\*([^*\n]+)\*\*\s*$")


def parse_bot_comment_metadata(body: str) -> dict[str, str]:
    """Extract category, severity, and title from a bot review-comment body.

    The bot formats comments as:
        **{Category Label}**
        {severity-emoji} {severity label}

        **{Title}**

        {body...}

    Older comments led the category line with an emoji; both forms parse.

    Returns a dict with keys 'category', 'severity', 'title'. Missing fields
    default to empty string. Safe on malformed input.
    """
    category = ""
    severity = ""
    title = ""

    for raw in body.split("\n"):
        line = raw.strip()
        if not line:
            continue

        if not category:
            m = _CATEGORY_LINE_RE.match(line)
            if m:
                emoji, label = m.group(1), m.group(2).strip()
                if label in _LABEL_TO_CATEGORY:
                    category = _LABEL_TO_CATEGORY[label]
                    continue
                if emoji in _CATEGORY_EMOJI_TO_NAME:
                    category = _CATEGORY_EMOJI_TO_NAME[emoji]
                    continue
            # New format: a plain bold line whose label names a known category.
            mb = _BOLD_LINE_RE.match(line)
            if mb and mb.group(1).strip() in _LABEL_TO_CATEGORY:
                category = _LABEL_TO_CATEGORY[mb.group(1).strip()]
                continue

        if not severity:
            matched = False
            for emoji, sev in _SEVERITY_EMOJI_MAP.items():
                if line.startswith(emoji):
                    severity = sev
                    matched = True
                    break
            if matched:
                continue

        if not title:
            m = _BOLD_LINE_RE.match(line)
            if m:
                title = m.group(1).strip()

        if category and severity and title:
            break

    return {"category": category, "severity": severity, "title": title}


def parse_bot_comment_finding_id(body: str) -> str | None:
    """Return the hidden durable finding ID from a Mira inline comment."""
    return parse_finding_id(body)


def format_key_issues(key_issues: list[KeyIssue]) -> str:
    """Format key issues as a markdown table for the review body."""
    lines = [
        "",
        "",
        "### Key Issues",
        "",
        "| | Issue | Location |",
        "|---|---|---|",
    ]
    for ki in key_issues:
        lines.append(f"| :red_circle: | {ki.issue} | `{ki.path}:{ki.line}` |")
    return "\n".join(lines)


_FENCE_RE = re.compile(r"^(`{3,})")


def _strip_suggestion_fences(text: str) -> str:
    """Remove wrapping triple-backtick fences the LLM may add to suggestion code.

    The suggestion content is placed inside a ```suggestion``` fence by the
    caller, so any fences inside the content itself would break rendering. We
    strip a leading fence line, a trailing fence line, and any stray
    backtick-only lines in the middle.
    """
    lines = text.split("\n")
    if lines and _FENCE_RE.match(lines[0].strip()):
        lines = lines[1:]
    if lines and _FENCE_RE.match(lines[-1].strip()):
        lines = lines[:-1]
    lines = [ln for ln in lines if not re.fullmatch(r"`{3,}\s*", ln.strip())]
    return "\n".join(lines)


def _close_open_fences(parts: list[str]) -> None:
    """If the accumulated body has an unclosed code fence, close it."""
    open_fence = False
    for part in parts:
        for line in part.split("\n"):
            if _FENCE_RE.match(line.strip()):
                open_fence = not open_fence
    if open_fence:
        parts.append("```")


def _fence_for(text: str) -> str:
    """A backtick fence longer than any run of backticks inside ``text``."""
    longest = max((len(run) for run in re.findall(r"`+", text)), default=0)
    return "`" * max(3, longest + 1)


_TEST_LANGUAGES = {
    ".py": "python",
    ".js": "javascript",
    ".jsx": "jsx",
    ".mjs": "javascript",
    ".cjs": "javascript",
    ".ts": "typescript",
    ".tsx": "tsx",
    ".go": "go",
    ".rb": "ruby",
    ".rs": "rust",
    ".java": "java",
    ".kt": "kotlin",
    ".php": "php",
    ".cs": "csharp",
}


def format_verification(comment: ReviewComment) -> str:
    """The note a reproduction test earned the comment, or "" when it earned none.

    Only the two outcomes that changed something are shown. ``inconclusive``
    leaves the comment as the reviewer wrote it, and saying "we tried and
    could not tell" on every such comment would be noise about Mira, not
    about the code. The test and its output are collapsed: the verdict is
    the useful line, the evidence is there for whoever doubts it.
    """
    verification = comment.verification
    if verification is None or verification.outcome not in {"reproduced", "not_reproduced"}:
        return ""
    if verification.outcome == "reproduced":
        headline = (
            "✅ **Reproduced** — Mira ran a test against this pull request's head and it "
            "failed the way this comment describes."
        )
    else:
        was = verification.original_severity
        downgrade = f" Downgraded from {was}." if was else ""
        headline = (
            "🧪 **Not reproduced** — a test Mira wrote for this claim passed against this "
            f"pull request's head, so it may not hold as stated.{downgrade}"
        )
    if not verification.test_code:
        return headline
    suffix = verification.test_path[verification.test_path.rfind(".") :].lower()
    language = _TEST_LANGUAGES.get(suffix, "")
    code_fence = _fence_for(verification.test_code)
    lines = [
        headline,
        "",
        "<details>",
        f"<summary>Reproduction test ({verification.runner or 'test'}; not committed)</summary>",
        "",
        f"{code_fence}{language}",
        verification.test_code.rstrip(),
        code_fence,
    ]
    if verification.output:
        output_fence = _fence_for(verification.output)
        lines += ["", "Output:", "", output_fence, verification.output.rstrip(), output_fence]
    lines += ["", "</details>"]
    return "\n".join(lines)


def format_comment_body(
    comment: ReviewComment,
    bot_name: str = "miracodeai",
    pr_info: PRInfo | None = None,
) -> str:
    """Format a review comment body with category badge, severity, and suggestion block.

    A grouped finding lists its other occurrences under "Also at", linked to
    the reviewed commit when ``pr_info`` says where that commit lives. The
    hidden marker names the posted finding only: the occurrences are persisted
    pointing at it, so feedback on this thread reaches the whole group.
    """
    label = _CATEGORY_DISPLAY.get(comment.category, ("\U0001f4cc", "Note"))[1]
    badge = _SEVERITY_BADGE.get(comment.severity, "")

    # Two trailing spaces = a Markdown hard break. GitHub renders a bare
    # newline as a break but GitLab doesn't, so the category and severity
    # would otherwise run together on one line on GitLab.
    parts = []
    if comment.finding_id:
        parts.append(finding_marker(comment.finding_id))
    if comment.vote_passes > 1:
        parts.append(votes_marker(comment.votes, comment.vote_passes))
    parts.append(f"**{label}**" + ("  " if badge else ""))
    if badge:
        parts.append(badge)
    parts.append("")
    parts.append(f"**{comment.title}**")
    parts.append("")
    parts.append(comment.body)
    verification = format_verification(comment)
    if verification:
        _close_open_fences(parts)
        parts.append("")
        parts.append(verification)

    also_at = format_also_at(
        comment,
        pr_url=pr_info.url if pr_info else "",
        sha=pr_info.head_sha if pr_info else "",
    )
    if also_at:
        # A fence the body left open would swallow the list.
        _close_open_fences(parts)
        parts.append("")
        parts.append(also_at)

    if comment.suggestion:
        clean_suggestion = html.unescape(comment.suggestion)
        clean_suggestion = _strip_suggestion_fences(clean_suggestion)
        # Close any unbalanced fence in the body so it doesn't swallow the suggestion.
        _close_open_fences(parts)
        parts.append("")
        parts.append("```suggestion")
        parts.append(clean_suggestion)
        parts.append("```")

    if comment.agent_prompt:
        prompt_text = comment.agent_prompt
        if comment.suggestion:
            prompt_text += f"\n\nApply this code change:\n\n{html.unescape(comment.suggestion)}"

        # A fenced block (not <pre>) — GitHub 422'd on <pre>-wrapped prompts.
        max_run = 0
        run = 0
        for ch in prompt_text:
            if ch == "`":
                run += 1
                max_run = max(max_run, run)
            else:
                run = 0
        fence = "`" * max(3, max_run + 1)

        parts.append("")
        parts.append("---")
        parts.append("")
        parts.append(
            "<details>\n"
            "<summary>Prompt for AI Agents</summary>\n"
            "\n"
            f"{fence}\n{prompt_text}\n{fence}\n"
            "\n"
            "</details>"
        )

    parts.append("")
    parts.append("> Reply directly with context; no bot mention is required in this thread.")
    parts.append(f"> Not useful? Reply `@{bot_name} reject` to dismiss this suggestion.")

    return "\n".join(parts)
