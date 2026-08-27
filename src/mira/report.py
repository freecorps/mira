"""One serialisation of a review result, for every surface that emits JSON.

``mira review --output json`` and ``mira local review --output json`` describe
the same object, so they describe it with the same code. Two formatters would
drift — one would gain ``end_line`` and the other would not — and a CI job that
worked against one would silently misparse the other.

The field names here are an interface. Adding a key is a compatible change;
renaming or removing one is not, and the local CLI's ``schema_version`` exists
to say which is which.
"""

from __future__ import annotations

from typing import Any

from mira.models import ReviewComment, ReviewResult, WalkthroughResult


def comment_dict(comment: ReviewComment) -> dict[str, Any]:
    """One finding, as every JSON surface reports it."""
    return {
        "path": comment.path,
        "line": comment.line,
        "end_line": comment.end_line,
        "severity": comment.severity.name.lower(),
        "category": comment.category,
        "title": comment.title,
        "body": comment.body,
        "confidence": comment.confidence,
        "suggestion": comment.suggestion,
    }


def walkthrough_dict(walkthrough: WalkthroughResult | None) -> dict[str, Any] | None:
    """The walkthrough, with its file changes grouped as they are rendered."""
    if walkthrough is None:
        return None
    groups: dict[str, list[dict[str, str]]] = {}
    for change in walkthrough.file_changes:
        label = change.group or "Other"
        groups.setdefault(label, []).append(
            {
                "path": change.path,
                "change_type": change.change_type.value,
                "description": change.description,
            }
        )
    effort = None
    if walkthrough.effort:
        effort = {
            "level": walkthrough.effort.level,
            "label": walkthrough.effort.label,
            "minutes": walkthrough.effort.minutes,
        }
    return {
        "summary": walkthrough.summary,
        "change_groups": [{"label": label, "files": files} for label, files in groups.items()],
        "effort": effort,
        "sequence_diagram": walkthrough.sequence_diagram,
    }


def review_result_dict(result: ReviewResult) -> dict[str, Any]:
    """A whole review result, ready for ``json.dumps``."""
    return {
        "summary": result.summary,
        "walkthrough": walkthrough_dict(result.walkthrough),
        "comments": [comment_dict(comment) for comment in result.comments],
        "reviewed_files": result.reviewed_files,
        "token_usage": result.token_usage,
    }


def review_result_text(result: ReviewResult) -> str:
    """A whole review result, as the CLI prints it for a human."""
    lines: list[str] = []

    if result.thread_decisions:
        from mira.llm.prompts.verify_fixes import _extract_issue_description

        lines.append("Thread resolution:")
        for d in result.thread_decisions:
            status = "RESOLVE" if d.fixed else "KEEP"
            desc = _extract_issue_description(d.body)
            if len(desc) > 80:
                desc = desc[:77] + "..."
            lines.append(f"  [{status}] {d.path}:{d.line} — {desc}")
        fixed = sum(1 for d in result.thread_decisions if d.fixed)
        lines.append(f"  {fixed}/{len(result.thread_decisions)} thread(s) would be resolved.")
        lines.append("")

    if result.walkthrough:
        lines.append(result.walkthrough.to_markdown())
        lines.append("")
        lines.append("---")
        lines.append("")

    if result.summary:
        lines.append(result.summary)
        lines.append("")

    if not result.comments:
        lines.append("No issues found.")
        return "\n".join(lines)

    for i, c in enumerate(result.comments, 1):
        lines.append(f"{i}. [{c.severity.name}] {c.path}:{c.line} — {c.title}")
        lines.append(f"   {c.body}")
        if c.suggestion:
            lines.append(f"   Suggestion: {c.suggestion}")
        lines.append("")

    lines.append(f"Reviewed {result.reviewed_files} files, {len(result.comments)} comments.")
    if result.token_usage:
        lines.append(f"Tokens used: {result.token_usage.get('total_tokens', 0)}")

    return "\n".join(lines)
