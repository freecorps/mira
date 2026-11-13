"""One review pass over the whole pull request: what does this change break elsewhere?

Mira reviews a pull request in parts, and each part looks at its own hunks.
What the other review bots found and Mira missed was mostly not inside any one
part: presence not cleared on ``pagehide``, state left behind after sign-out, a
background task ended twice, an in-flight copy racing a later write. Each of
those needs two places in view — a setup and its teardown, a writer and a
reader, a changed contract and its callers.

So one more agentic pass reads the whole change at once, budgeted: a line for
every changed file, the callers of the changed symbols, and as many hunks as
fit, highest-priority files first — with the repository tools to read the
rest. Its prompt asks only for interactions (lifecycle, state machines,
concurrency, contracts, cross-file invariants) and leaves line-level review to
the parts. Its findings are tagged ``source_pass="system"`` and go through
the same filter, grouping, self-critique and cap as every other finding.

The pass never fails a review: it has its own deadline, and anything that
goes wrong costs only its findings.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from mira.core.context import build_file_context_string
from mira.core.priority import rank_files
from mira.llm.prompts.review import build_system_review_prompt
from mira.models import FileDiff, ReviewComment

logger = logging.getLogger(__name__)

SOURCE_PASS = "system"
# A file line in the summary is cut here, so a file with a hundred hunks does
# not spend the budget on its headers.
_MAX_HEADERS_PER_FILE = 6


@dataclass
class SystemPassInput:
    """The budgeted user message: who changed, who calls them, what fits."""

    summary: str
    callers: str
    hunks: list[str]
    shown_paths: list[str]
    omitted_paths: list[str]


def _hunk_context(hunk: Any) -> str:
    first = (hunk.content or "").split("\n", 1)[0]
    if not first.startswith("@@"):
        return ""
    parts = first.split("@@", 2)
    return parts[2].strip() if len(parts) == 3 else ""


def _summary_line(f: FileDiff, shown: bool) -> str:
    # The function context git prints after each `@@ … @@`: where the change is.
    headers = [_hunk_context(h) for h in f.hunks]
    named = list(dict.fromkeys(h for h in headers if h))
    where = ", ".join(f"`{h}`" for h in named[:_MAX_HEADERS_PER_FILE])
    if len(named) > _MAX_HEADERS_PER_FILE:
        where += f", … {len(named) - _MAX_HEADERS_PER_FILE} more"
    line = f"- `{f.path}` ({f.change_type.value}, +{f.added_lines}/-{f.deleted_lines}"
    line += f", {len(f.hunks)} hunk{'s' if len(f.hunks) != 1 else ''})"
    if where:
        line += f" — in {where}"
    if not shown:
        line += " — hunks not shown; read it with the tools"
    return line


def _fit(text: str, budget: int, count_tokens: Callable[[str], int]) -> str:
    """``text`` cut at a line boundary to fit ``budget`` tokens, saying so."""
    if not text or count_tokens(text) <= budget:
        return text
    note = "\n… (callers cut to fit the budget; find_usages has the rest)"
    lines = text.split("\n")
    lo, hi = 0, len(lines)
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if count_tokens("\n".join(lines[:mid]) + note) <= budget:
            lo = mid
        else:
            hi = mid - 1
    return "\n".join(lines[:lo]) + note if lo else ""


def build_system_input(
    files: list[FileDiff],
    count_tokens: Callable[[str], int],
    token_budget: int,
    callers: str = "",
) -> SystemPassInput:
    """Fit the whole change into ``token_budget`` tokens.

    Every file gets its summary line, whatever the budget: knowing a file
    changed is what lets the model go and read it. Then the callers block,
    then each file's hunks, highest priority first, as long as they fit; a
    file that does not fit is skipped, not cut, and a smaller one after it
    may still fit.
    """
    ranked = [f for f, _ in rank_files(files)]
    used = 0
    shown: list[FileDiff] = []
    hunks: list[str] = []
    # The summary and the callers come out of the same budget, estimated in
    # its longest form — every file marked "hunks not shown" — so the summary
    # written at the end can only be shorter than what was reserved for it.
    head = "## Changed files\n\n" + "\n".join(_summary_line(f, False) for f in ranked)
    callers = _fit(callers, max(0, token_budget - count_tokens(head)), count_tokens)
    used += count_tokens(head) + count_tokens(callers)
    for f in ranked:
        block = build_file_context_string(f)
        cost = count_tokens(block)
        if used + cost > token_budget:
            continue
        used += cost
        shown.append(f)
        hunks.append(block)
    shown_set = {f.path for f in shown}
    summary = "## Changed files\n\n" + "\n".join(
        _summary_line(f, f.path in shown_set) for f in ranked
    )
    return SystemPassInput(
        summary=summary,
        callers=callers,
        hunks=hunks,
        shown_paths=[f.path for f in shown],
        omitted_paths=[f.path for f in ranked if f.path not in shown_set],
    )


async def system_review_pass(
    *,
    llm: Any,
    files: list[FileDiff],
    count_tokens: Callable[[str], int],
    token_budget: int,
    parse: Callable[[str], list[ReviewComment]],
    callers: Callable[[int], Awaitable[str]] | None = None,
    pr_title: str = "",
    pr_description: str = "",
    learned_checks: list[str] | None = None,
    make_executor: Callable[[], Any] | None = None,
    agentic_loop: Callable[..., Awaitable[str]] | None = None,
    deadline_seconds: int = 0,
    audit: list[dict] | None = None,
) -> list[ReviewComment]:
    """Run the system pass and return its findings, tagged ``system``.

    ``parse`` turns the model's ``submit_review`` arguments into comments
    anchored on the diff (the engine's own parser, over every reviewed file).
    ``callers(char_budget)`` renders the callers of changed symbols.
    ``make_executor`` builds the repository tools; without it — or without
    ``agentic_loop`` — the pass is one plain review call. Bounded by
    ``deadline_seconds`` (0: unbounded), and never raises.
    """
    started = time.monotonic()
    entry: dict = {"stage": "system_pass"}

    async def _run() -> list[ReviewComment]:
        caller_block = ""
        if callers is not None:
            try:
                # A quarter of the budget at most: the hunks are the evidence.
                caller_block = await callers(max(0, token_budget // 4) * 4)
            except Exception as exc:  # noqa: BLE001 — optional context
                logger.debug("System pass: callers unavailable: %s", exc)
        shaped = build_system_input(files, count_tokens, token_budget, caller_block)
        entry["files"] = len(files)
        entry["hunks_shown"] = len(shaped.shown_paths)
        entry["hunks_omitted"] = shaped.omitted_paths[:50]
        messages = build_system_review_prompt(
            file_paths=[f.path for f in files],
            sections=[shaped.summary, shaped.callers, *shaped.hunks],
            pr_title=pr_title,
            pr_description=pr_description,
            learned_checks=learned_checks,
        )
        raw = ""
        if make_executor is not None and agentic_loop is not None:
            executor = make_executor()
            raw = await agentic_loop(
                llm,
                messages,
                executor,
                submit_by=(started + deadline_seconds * 0.6) if deadline_seconds else None,
            )
            entry["calls"] = getattr(executor, "call_log", [])
        if not raw:
            raw = await llm.review(messages)
        comments = parse(raw)
        for c in comments:
            c.source_pass = SOURCE_PASS
        return comments

    try:
        run = _run()
        comments = await (asyncio.wait_for(run, deadline_seconds) if deadline_seconds else run)
        entry["status"] = "ok"
        entry["drafted"] = len(comments)
    except Exception as exc:  # noqa: BLE001 — the pass is best-effort by design
        if isinstance(exc, TimeoutError) and not str(exc):
            exc = TimeoutError(f"system pass ran past its {deadline_seconds}s deadline")
        logger.warning("System pass failed, continuing without it: %s", exc)
        entry["status"] = "failed"
        entry["error"] = str(exc)[:300]
        comments = []
    entry["seconds"] = round(time.monotonic() - started, 2)
    logger.info(
        "System pass: %d finding(s) in %.1fs (%s)",
        len(comments),
        entry["seconds"],
        entry["status"],
    )
    if audit is not None:
        audit.append(entry)
    return comments
