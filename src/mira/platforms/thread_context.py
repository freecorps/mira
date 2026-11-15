"""The code a reply on one of Mira's review threads is checked against.

Two pieces, both best-effort and neither able to leave a thread unanswered:

- :func:`thread_code_context` — the file at the head around the finding and
  around every line the reply cites; what changed since the finding **across
  every file**, the commented file first, then the files the conversation
  names, with a list of all of them; and the pull request's hunk at the line.
- :func:`gather_thread_evidence` — a short, read-only tool loop at the head
  (``read_file``, ``grep_repo``, ``find_usages``, ``find_definition``,
  ``grep_index``: the chat's loop, ``core/chat.py``) that fetches whatever the
  context above still does not show, before the recheck or the pushback judge
  gives its verdict through its usual submit tool.

The diff since the finding used to be filtered to the commented file, and the
check that followed "fixed" saw nothing else. 37 of 123 thread replies Mira
posted in two days asked the author to "show", "push" or "point at" code that
was already there — in another file, or past the window.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader

from mira.autofix.redact import redact
from mira.core.change_context import mentions_in, render_changes
from mira.core.chat import _digest, agentic_chat_loop
from mira.core.diff_parser import parse_diff
from mira.llm import untrusted

logger = logging.getLogger(__name__)

_ENV = Environment(
    loader=FileSystemLoader(
        str(Path(__file__).resolve().parents[1] / "llm" / "prompts" / "templates")
    ),
    trim_blocks=True,
    lstrip_blocks=True,
)

_MAX_THREAD_CODE_CONTEXT = 6000
# Lines of the file at the head shown around the finding, and around each line
# the reply points at. A single diff hunk was all a reply used to get, and in
# 28% of Mira's thread replies that hunk did not reach the code in question.
_THREAD_WINDOW = 60
_MAX_THREAD_FILE_CONTEXT = 12000
# What changed since the finding, across every file. The file list itself is
# outside this budget: one line per file, so nothing is silently missing.
_MAX_THREAD_CHANGE_CONTEXT = 16000
_LINE_REFERENCE_RE = re.compile(
    r"(?:\b(?:line|linha|lines|linhas|L)\s*|:)(\d{1,6})\b", re.IGNORECASE
)

# Past this share of the lookup deadline, the next turn is the last one.
_ANSWER_BY_SHARE = 0.6
# The lookup loop's closing note, when it wrote one.
_MAX_NOTE_CHARS = 3000

_EFFORT_RANK = {"minimal": 0, "low": 1, "medium": 2, "high": 3, "xhigh": 4, "max": 5}


def numbered_windows(content: str, centres: list[int], radius: int) -> str:
    lines = content.splitlines()
    if not lines:
        return ""
    ranges: list[tuple[int, int]] = []
    for centre in centres:
        if centre <= 0:
            continue
        ranges.append((max(0, centre - 1 - radius), min(len(lines), centre + radius)))
    if not ranges:
        ranges = [(0, min(len(lines), 2 * radius))]
    ranges.sort()
    merged = [ranges[0]]
    for lo, hi in ranges[1:]:
        if lo <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], hi))
        else:
            merged.append((lo, hi))
    width = len(str(len(lines)))
    return "\n...\n".join(
        "\n".join(f"{i + 1:>{width}}| {lines[i]}" for i in range(lo, hi)) for lo, hi in merged
    )


def referenced_lines(text: str, limit: int = 3) -> list[int]:
    seen: list[int] = []
    for match in _LINE_REFERENCE_RE.finditer(text or ""):
        value = int(match.group(1))
        if value > 0 and value not in seen:
            seen.append(value)
        if len(seen) >= limit:
            break
    return seen


async def pr_hunk(provider: Any, pr_info: Any, path: str, line: int) -> str:
    if not hasattr(provider, "get_pr_diff"):
        return ""
    diff_text = await provider.get_pr_diff(pr_info)
    if not isinstance(diff_text, str):
        return ""
    file = next((item for item in parse_diff(diff_text).files if item.path == path), None)
    if file is None or not file.hunks:
        return ""

    def distance(item: Any) -> int:
        last = item.target_start + max(item.target_length - 1, 0)
        if item.target_start <= line <= last:
            return 0
        return min(abs(line - item.target_start), abs(line - last))

    hunk = min(file.hunks, key=distance)
    # Only a hunk at or near the line. The first hunk of the file used to be
    # shown whatever the line, as though it were the code under discussion.
    return hunk.content[:_MAX_THREAD_CODE_CONTEXT] if distance(hunk) <= 20 else ""


def changes_since_finding(
    compare: str, path: str, *, mention_text: str = "", since: str = "", until: str = ""
) -> str:
    """The compare diff since a finding, for its thread: every file, the commented one first."""
    mentions = mentions_in(mention_text)
    rendered = render_changes(
        compare,
        focus_paths=[path] if path else [],
        mentions=mentions,
        budget=_MAX_THREAD_CHANGE_CONTEXT,
    )
    if not rendered:
        return ""
    files = parse_diff(compare).files
    commented_changed = any(path in {f.path, f.old_path} for f in files)
    span = f" ({since[:12]}..{until[:12]})" if since and until else ""
    lead = f"Changes since the comment{span}, across the whole pull request."
    if not commented_changed:
        lead += (
            " The commented file has not changed since the comment; a fix may"
            " still live in one of the other files below."
        )
    return lead + "\n" + rendered


async def thread_code_context(
    provider: Any,
    pr_info: Any,
    path: str,
    line: int,
    *,
    reply: str = "",
    finding_head_sha: str = "",
    mention_text: str = "",
) -> str:
    """The code a thread reply is about, as the pull request now has it.

    Three parts, each best-effort: the file at the head, numbered, around the
    finding and around every line the reply cites; what changed since the
    finding was raised, in every file, prioritised and bounded; and the pull
    request's hunk at the line. ``mention_text`` (the finding, the thread so
    far) adds to ``reply`` in deciding which changed files come first.
    """
    if not path:
        return ""
    parts: list[str] = []
    head = getattr(pr_info, "head_sha", "") or getattr(pr_info, "head_branch", "")
    try:
        if head and hasattr(provider, "get_file_content"):
            content = await provider.get_file_content(pr_info, path, head)
            if isinstance(content, str) and content:
                window = numbered_windows(content, [line, *referenced_lines(reply)], _THREAD_WINDOW)
                parts.append(
                    f"File at the pull request's head ({head[:12]}), numbered:\n"
                    + window[:_MAX_THREAD_FILE_CONTEXT]
                )
    except Exception as exc:
        logger.debug("Could not read %s at head for a thread reply: %s", path, exc)
    try:
        current = getattr(pr_info, "head_sha", "") or ""
        if finding_head_sha and current and finding_head_sha == current:
            parts.append("Nothing has been pushed to the pull request since the comment was made.")
        elif finding_head_sha and current:
            compare = await provider.get_compare_diff(pr_info, finding_head_sha, current)
            text = (
                changes_since_finding(
                    compare,
                    path,
                    mention_text=f"{reply}\n{mention_text}",
                    since=finding_head_sha,
                    until=current,
                )
                if isinstance(compare, str) and compare.strip()
                else ""
            )
            parts.append(text or "No file has changed since the comment was made.")
    except Exception as exc:
        logger.debug("Could not diff the pull request since the finding on %s: %s", path, exc)
    try:
        hunk = await pr_hunk(provider, pr_info, path, line)
        if hunk:
            parts.append("Pull request diff hunk at the comment's line:\n" + hunk)
    except Exception as exc:
        logger.debug("Could not load thread code context for %s: %s", path, exc)
    return "\n\n".join(parts)


@dataclass
class ThreadEvidence:
    """What the lookups found, rendered for the verdict's prompt."""

    text: str = ""
    # "agentic": the loop finished with a note; "partial": it was cut short
    # but had looked things up; "none": it ran and found nothing to add;
    # "off": it did not run (disabled, no tools, or failed before a lookup).
    route: str = "off"
    lookups: list[str] = field(default_factory=list)


def lookup_llm_config(base: Any, effort: str) -> Any:
    """``base`` with a lower reasoning effort for lookup turns, or ``base`` itself.

    Only ever lowers: a model configured without reasoning is not made to
    reason for a lookup, and an unknown value leaves the config alone.
    """
    current = getattr(base, "reasoning_effort", None)
    if not (isinstance(current, str) and isinstance(effort, str) and effort):
        return base
    if current not in _EFFORT_RANK or effort not in _EFFORT_RANK:
        return base
    if _EFFORT_RANK[effort] >= _EFFORT_RANK[current]:
        return base
    try:
        return base.model_copy(update={"reasoning_effort": effort})
    except Exception:  # noqa: BLE001 — not a pydantic config: use it as it is
        return base


def _lookup_messages(
    *,
    purpose: str,
    finding_text: str,
    reply: str,
    history: str,
    path: str,
    line: int,
    code_context: str,
) -> list[dict[str, str]]:
    system = _ENV.get_template("thread_lookup.jinja2").render(purpose=purpose)
    parts = [
        "## Your finding\n\n"
        + untrusted.block("FINDING", finding_text or "(none)", redactor=redact),
        "## The human's reply\n\n" + untrusted.block("REPLY", reply or "(empty)", redactor=redact),
    ]
    if history:
        parts.append(
            "## Earlier in this thread (oldest first)\n\n"
            + untrusted.block("COMMENT", history, redactor=redact)
        )
    if path:
        where = f"Line {line}." if line else ""
        parts.append(
            "## The file the finding is on\n\n"
            + untrusted.block("FILE", path, redactor=redact)
            + (f"\n{where}" if where else "")
        )
    parts.append(
        "## What you can already see\n\n"
        + (
            untrusted.block("DIFF", code_context, redactor=redact)
            if code_context
            else "Nothing: no code could be loaded for this thread. Look it up."
        )
    )
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": "\n\n".join(parts)},
    ]


def render_evidence(lookups: list[tuple[str, str]], note: str) -> str:
    """The lookups and the loop's closing note, as a prompt section. "" for neither."""
    if not lookups and not note:
        return ""
    text = _digest(lookups) if lookups else "## What you already looked up\n\nNothing."
    if note:
        text += "\n\n### Your note after the lookups\n" + untrusted.block(
            "COMMENT", note[:_MAX_NOTE_CHARS], redactor=redact
        )
    return text


async def gather_thread_evidence(
    provider: Any,
    pr_info: Any,
    *,
    llm: Any,
    config: Any,
    purpose: str,
    finding_text: str,
    reply: str,
    history: str = "",
    path: str = "",
    line: int = 0,
    code_context: str = "",
    tools: Any = None,
) -> ThreadEvidence:
    """Look up, at the head, the code a thread reply is about. Never raises.

    Bounded by ``chat.thread_max_hops`` turns and ``chat.thread_deadline_seconds``
    of wall clock (snapshot and tree included); past 60% of the deadline the
    next turn is told to stop looking. Whatever was looked up before a failure
    or the deadline is kept. ``purpose`` is ``"recheck"`` or ``"pushback"``.
    ``tools`` replaces :func:`mira.platforms.pr_chat.repository_tools` (tests).
    """
    chat = config.chat
    if not (chat.agentic and chat.thread_lookups):
        return ThreadEvidence(route="off")
    if tools is None:
        from mira.platforms.pr_chat import repository_tools as tools

    messages = _lookup_messages(
        purpose=purpose,
        finding_text=finding_text,
        reply=reply,
        history=history,
        path=path,
        line=line,
        code_context=code_context,
    )
    lookups: list[tuple[str, str]] = []
    deadline = float(chat.thread_deadline_seconds)
    answer_by = time.monotonic() + deadline * _ANSWER_BY_SHARE
    ran = False

    async def _run() -> str:
        nonlocal ran
        async with tools(provider, pr_info, config) as executor:
            if executor is None:
                return ""
            ran = True
            return await agentic_chat_loop(
                llm,
                messages,
                executor,
                max_hops=int(chat.thread_max_hops),
                answer_by=answer_by,
                lookups=lookups,
            )

    note = ""
    try:
        note = await asyncio.wait_for(_run(), timeout=deadline)
    except TimeoutError:
        logger.info(
            "Thread lookups on %s passed their %.0fs deadline after %d lookup(s)",
            getattr(pr_info, "url", ""),
            deadline,
            len(lookups),
        )
    except Exception as exc:  # noqa: BLE001 — the verdict goes ahead without them
        logger.warning("Thread lookups on %s failed: %s", getattr(pr_info, "url", ""), exc)
    calls = [call for call, _ in lookups]
    if not ran and not lookups:
        return ThreadEvidence(route="off")
    text = render_evidence(lookups, note)
    if note:
        route = "agentic"
    elif lookups:
        route = "partial"
    else:
        route = "none"
    return ThreadEvidence(text=text, route=route, lookups=calls)
