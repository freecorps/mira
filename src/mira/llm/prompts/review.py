"""Prompt builder for PR review."""

from __future__ import annotations

import re
from pathlib import Path

from jinja2 import Environment, FileSystemLoader

from mira.config import MiraConfig
from mira.core.context import build_file_context_string
from mira.llm.prompts.footguns import get_footguns_for_files
from mira.llm.prompts.verify_fixes import _extract_issue_description
from mira.models import FileDiff, UnresolvedThread

_TEMPLATE_DIR = Path(__file__).parent / "templates"

_REVIEW_REMINDER = (
    "---\n"
    'Review every hunk above, going through "How to review" for each change. '
    "Check anything that depends on code outside the hunk before deciding. File "
    "every real defect you can support, with `line` taken from the gutter, then "
    "call `submit_review` once."
)


def _get_template_env() -> Environment:
    return Environment(
        loader=FileSystemLoader(str(_TEMPLATE_DIR)),
        trim_blocks=True,
        lstrip_blocks=True,
    )


def build_review_prompt(
    files: list[FileDiff],
    config: MiraConfig,
    pr_title: str = "",
    pr_description: str = "",
    existing_comments: list[UnresolvedThread] | None = None,
    code_context: str = "",
    learned_rules: list[str] | None = None,
    custom_rules: list[dict[str, str]] | None = None,
    file_history: dict | None = None,
    review_round: int = 1,
    resolved_threads: list[dict] | None = None,
    team_conventions: str = "",
    dependency_updates: str = "",
    learned_checks: list[str] | None = None,
    repo_instructions: str = "",
) -> list[dict[str, str]]:
    """Build the review prompt messages for the LLM.

    ``dependency_updates`` is the rendered block from
    :func:`mira.dependency_updates.review_context` — upstream release notes,
    already inside an untrusted block — placed ahead of the diffs.

    ``learned_checks`` are positive learned rules ("look for …"), rendered
    apart from ``learned_rules``, which say what the team does not want
    reported.

    ``repo_instructions`` is this part's block from
    :meth:`mira.core.review_instructions.RepoInstructions.render`: the
    `.mira/REVIEW.md` and `review.path_instructions` text covering its files.

    Returns a list of message dicts with 'role' and 'content' keys.
    """
    env = _get_template_env()
    template = env.get_template("review.jinja2")

    file_contexts = [build_file_context_string(f) for f in files]
    file_paths = [f.path for f in files]

    # Pre-clean existing comment bodies so the template gets concise descriptions
    cleaned_comments = None
    if existing_comments:
        cleaned_comments = [
            {"path": c.path, "line": c.line, "description": _extract_issue_description(c.body)}
            for c in existing_comments
        ]

    # Decision archaeology — flatten history dict into a list of (path, entries)
    history_for_template = None
    if file_history:
        history_for_template = [
            {
                "path": path,
                "commits": [
                    {
                        "sha": e.sha,
                        "message": e.message,
                        "author": e.author,
                        "date": e.date,
                    }
                    for e in entries
                ],
            }
            for path, entries in file_history.items()
            if entries
        ]

    footguns = get_footguns_for_files(files)

    system_content = template.render(
        pr_title=pr_title,
        pr_description=pr_description,
        file_contexts=file_contexts,
        file_paths=file_paths,
        confidence_threshold=config.filter.confidence_threshold,
        max_comments=config.filter.max_comments,
        focus_only_on_problems=config.review.focus_only_on_problems,
        review_profile=config.review.profile,
        existing_comments=cleaned_comments,
        has_code_context=bool(code_context),
        learned_rules=learned_rules,
        learned_checks=learned_checks,
        custom_rules=custom_rules,
        file_history=history_for_template,
        review_round=review_round,
        resolved_threads=resolved_threads,
        team_conventions=team_conventions,
        footguns=footguns,
        has_dependency_updates=bool(dependency_updates),
        repo_instructions=repo_instructions,
    )

    # Build user message with optional code context before diffs
    user_parts = []
    if code_context:
        user_parts.append(code_context)
    if dependency_updates:
        user_parts.append(dependency_updates)
    user_parts.extend(file_contexts)
    # Said again after the diffs, where the model reads last: on a long prompt a
    # smaller model follows what is nearest the end of it, and the procedure
    # sits in a system prompt thousands of tokens back.
    if file_contexts:
        user_parts.append(_REVIEW_REMINDER)

    return [
        {"role": "system", "content": system_content},
        {"role": "user", "content": "\n\n".join(user_parts)},
    ]


def build_security_review_prompt(
    files: list[FileDiff],
    pr_title: str = "",
) -> list[dict[str, str]]:
    """Build the security-focused review prompt messages for the LLM.

    Runs in parallel with the main review pass. Output is merged into the
    main review's comments list and goes through the same noise filter
    (dedup against any overlap with main-pass findings).
    """
    env = _get_template_env()
    template = env.get_template("security_review.jinja2")
    file_contexts = [build_file_context_string(f) for f in files]
    file_paths = [f.path for f in files]
    system_content = template.render(pr_title=pr_title, file_paths=file_paths)
    return [
        {"role": "system", "content": system_content},
        {"role": "user", "content": "\n\n".join(file_contexts)},
    ]


def build_dependency_review_prompt(
    files: list[FileDiff],
    existing_packages: list[str] | None = None,
    pr_title: str = "",
) -> list[dict[str, str]]:
    """Build the dependency-overlap review prompt messages for the LLM.

    Runs in parallel with the main review pass over only the changed manifest
    files. ``existing_packages`` is the set of dependency names already declared
    in the repo (from the index) so the model can spot a newly-added package
    that duplicates the functionality of one already present. Output is merged
    into the main review's comments list and goes through the same noise filter.
    """
    env = _get_template_env()
    template = env.get_template("dependency_review.jinja2")
    file_contexts = [build_file_context_string(f) for f in files]
    file_paths = [f.path for f in files]
    system_content = template.render(
        pr_title=pr_title,
        file_paths=file_paths,
        existing_packages=existing_packages or [],
    )
    return [
        {"role": "system", "content": system_content},
        {"role": "user", "content": "\n\n".join(file_contexts)},
    ]


_HUNK_HEADER_RE = re.compile(r"^@@\s.*@@", re.MULTILINE)


def _extract_hunk_headers(f: FileDiff) -> list[str]:
    """Extract @@ ... @@ header lines from a file's hunks."""
    headers: list[str] = []
    for hunk in f.hunks:
        for m in _HUNK_HEADER_RE.finditer(hunk.content):
            headers.append(m.group(0))
    return headers


def build_walkthrough_prompt(
    files: list[FileDiff],
    config: MiraConfig,
    pr_title: str = "",
    pr_description: str = "",
) -> list[dict[str, str]]:
    """Build the walkthrough prompt messages for the LLM.

    Uses only file metadata (not full diffs) to keep the prompt compact.
    Returns a list of message dicts with 'role' and 'content' keys.
    """
    env = _get_template_env()
    template = env.get_template("walkthrough.jinja2")

    files_metadata = [
        {
            "path": f.path,
            "change_type": f.change_type.value,
            "language": f.language,
            "added_lines": f.added_lines,
            "deleted_lines": f.deleted_lines,
            "hunk_headers": _extract_hunk_headers(f),
        }
        for f in files
    ]

    system_content = template.render(
        pr_title=pr_title,
        pr_description=pr_description,
        files_metadata=files_metadata,
        include_sequence_diagram=config.review.walkthrough_sequence_diagram,
    )

    return [
        {"role": "system", "content": system_content},
        {"role": "user", "content": "Generate the walkthrough for this PR."},
    ]


def _clip_diff(diff_text: str, max_chars: int) -> str:
    """The diff cut at a file boundary near ``max_chars``, saying what was cut."""
    if len(diff_text) <= max_chars:
        return diff_text
    head = diff_text[:max_chars]
    cut = head.rfind("\ndiff --git ")
    if cut > max_chars // 2:
        head = head[:cut]
    left = diff_text[len(head) :].count("diff --git ")
    return (
        head.rstrip()
        + f"\n\n[diff cut here for length: {left} more file(s) not shown — read them at the "
        "head with `read_file` when tools are available]"
    )


def build_conversation_prompt(
    question: str,
    diff_text: str,
    pr_title: str = "",
    pr_description: str = "",
    *,
    history: str = "",
    focus: str = "",
    bot_name: str = "mira",
    tools: bool = False,
    max_diff_chars: int = 40_000,
) -> list[dict[str, str]]:
    """Prompt messages for answering a question asked on a pull request.

    The system message holds only Mira's own instructions. Everything that
    came from the pull request — title, description, diff, the conversation
    so far, the question — goes in the user message inside untrusted blocks,
    redacted: the title and description used to be written into the system
    message as prose, where a description saying "ignore the above" sat in
    the most trusted place in the prompt.

    ``history`` is the earlier conversation, already rendered oldest first;
    ``focus`` is the code a review-thread question was asked on. ``tools``
    says whether the answer comes from the tool loop, which changes what the
    model is told it can check.
    """
    from mira.autofix.redact import redact
    from mira.llm import untrusted

    env = _get_template_env()
    template = env.get_template("conversation.jinja2")
    system_content = template.render(tools=tools, bot_name=bot_name or "mira")

    parts: list[str] = []
    if pr_title or pr_description:
        pr_text = f"Title: {pr_title}" + (f"\n\n{pr_description}" if pr_description else "")
        parts.append("## Pull request\n\n" + untrusted.block("PR", pr_text, redactor=redact))
    if history:
        parts.append(
            "## Earlier in this conversation (oldest first)\n\n"
            + untrusted.block("COMMENT", history, redactor=redact)
        )
    if focus:
        parts.append(
            "## The code this question was asked on\n\n"
            + untrusted.block("FILE", focus, redactor=redact)
        )
    parts.append(
        "## Diff\n\n"
        + untrusted.block("DIFF", _clip_diff(diff_text or "", max_diff_chars), redactor=redact)
    )
    parts.append("## Question\n\n" + untrusted.block("REPLY", question or "", redactor=redact))

    return [
        {"role": "system", "content": system_content},
        {"role": "user", "content": "\n\n".join(parts)},
    ]
