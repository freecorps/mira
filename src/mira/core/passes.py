"""Review passes that run alongside the main chunked review.

Each pass takes an `LLMProvider` and the input it needs; none touches the
ReviewEngine instance. Most route through the configured indexing model so
the heavyweight review model isn't paying for verification work.
"""

from __future__ import annotations

import asyncio
import json
import logging

from mira.config import load_config
from mira.core.context import number_hunk_lines
from mira.dashboard.models_config import llm_config_for
from mira.exceptions import ResponseParseError
from mira.llm.prompts.review import (
    build_dependency_review_prompt,
    build_security_review_prompt,
)
from mira.llm.provider import LLMProvider
from mira.llm.response_parser import (
    convert_to_review_comments,
    loads_lenient,
    parse_llm_response,
)
from mira.llm.tool_schemas import SUBMIT_CRITIQUE_TOOL, SUBMIT_REVIEW_TOOL
from mira.llm.utils import parse_xml_tool_call
from mira.models import KeyIssue, ReviewComment, Severity

logger = logging.getLogger(__name__)


_AGENTIC_MAX_HOPS = 6
# What the fallback review is told about the lookups the loop made. The
# executor already caps tool output per chunk; this bounds the digest too, so a
# chatty loop cannot push the diff out of the fallback's context.
_DIGEST_MAX_CHARS = 24_000

_FINAL_HOP_NUDGE = (
    "This is your last turn. Do not look anything else up: call `submit_review` "
    "now with every finding you can support, or with an empty `comments` list if "
    "you found none."
)


def _submission_problem(arguments: object) -> str | None:
    """Why a ``submit_review`` payload cannot be used, or None when it can."""
    if isinstance(arguments, dict):
        arguments = json.dumps(arguments)
    if not isinstance(arguments, str) or not arguments.strip():
        return "the arguments were empty"
    try:
        parse_llm_response(arguments)
    except ResponseParseError as exc:
        return str(exc)[:600]
    return None


def _review_json_in(content: str) -> str | None:
    """A review the model wrote as its reply instead of submitting it, if it is one.

    Either the object itself, or a ``submit_review`` call written out in the
    XML form some models fall back to (see ``parse_xml_tool_call``).
    """
    if "comments" not in content:
        return None
    xml_call = parse_xml_tool_call(content)
    if xml_call is not None and xml_call[0] in (None, "submit_review"):
        data: object = xml_call[1]
    else:
        data = loads_lenient(content)
    if not isinstance(data, dict) or "comments" not in data:
        return None
    text = json.dumps(data)
    return text if _submission_problem(text) is None else None


def _lookup_digest(lookups: list[tuple[str, str]], notes: list[str]) -> str:
    """The loop's findings so far, as a message a fresh review call can read."""
    parts = [
        "## What you already looked up\n\n"
        "Before writing this review you investigated the repository. These are "
        "the results of those lookups — rely on them, they are the repository "
        "at this pull request's head."
    ]
    used = len(parts[0])
    for call, result in lookups:
        block = f"\n\n### {call}\n{result}"
        if used + len(block) > _DIGEST_MAX_CHARS:
            parts.append("\n\n*(further lookups omitted for length)*")
            break
        parts.append(block)
        used += len(block)
    if notes:
        text = "\n\n".join(notes)[-4_000:]
        parts.append("\n\n## Your notes so far\n\n" + text)
    parts.append(
        "\n\nNow call `submit_review` with your findings on the diff. Every "
        "finding you can support with the diff or the lookups above belongs in it."
    )
    return "".join(parts)


def _with_digest(messages: list[dict], digest: str) -> list[dict]:
    """``messages`` with the digest appended to the last user turn.

    Appended, not sent as a turn of its own: some chat templates (Gemma, older
    Mistral) refuse two user messages in a row.
    """
    out = [dict(m) for m in messages]
    for message in reversed(out):
        if message.get("role") == "user" and isinstance(message.get("content"), str):
            message["content"] = message["content"] + "\n\n" + digest
            return out
    out.append({"role": "user", "content": digest})
    return out


async def agentic_review_loop(
    llm: LLMProvider,
    messages: list[dict],
    executor: object,
    max_hops: int = _AGENTIC_MAX_HOPS,
) -> str:
    """Run an agentic tool-use loop until the LLM submits a review.

    Hands the model `read_file` and `grep_repo` alongside the terminal
    `submit_review` tool. Caps at ``max_hops`` to bound token spend; returns
    the JSON args of the final `submit_review` call (same shape `llm.review`
    returns), or "" when the loop got nowhere — the caller then makes a
    forced single-tool call.

    A loop that did get somewhere is not thrown away. When the model runs out
    of hops, answers in prose, or submits arguments that do not parse, the
    old behaviour was to return "" and review the chunk again from scratch —
    one more full call, made without anything the lookups had found. Now the
    last turn is told to submit, a broken submission is sent back with the
    parse error for one more try, and if the loop still ends without a usable
    review, the forced call here carries a digest of every lookup and note.
    """
    from mira.llm.agentic_tools import AGENTIC_TOOLS

    tools = [*AGENTIC_TOOLS, SUBMIT_REVIEW_TOOL]
    convo: list[dict] = [dict(m) for m in messages]
    if convo and convo[0].get("role") == "system":
        convo[0]["content"] = (
            convo[0]["content"] + "\n\n## Tools\n\n"
            "You have two helpers for cross-file checks: "
            "`read_file(path, start_line?, end_line?)` and "
            "`grep_repo(pattern, path_glob?, path_only?)`, which searches the "
            "whole repository at this pull request's head. Use them to verify a "
            "cross-file claim before filing — *what does the caller pass?*, *is "
            "this new value handled where it is consumed?*, *does the function "
            "actually raise X?* — and to confirm a suspicion rather than drop it. "
            "Don't browse; fetch what you need.\n\n"
            f"You have at most {max_hops - 1} rounds of lookups before you must "
            "submit. Ask for everything you need in the same turn — several "
            "tool calls in one reply run together — instead of one lookup per "
            "turn. Skip the tools if the diff and context are enough. When "
            "ready, call `submit_review` once with all your findings."
        )

    lookups: list[tuple[str, str]] = []
    notes: list[str] = []

    async def _finish() -> str:
        # Nothing learned, nothing to carry: the caller's plain review is the
        # same call without a digest.
        if not lookups and not notes:
            return ""
        review = getattr(llm, "review", None)
        if review is None:
            return ""
        logger.info(
            "Agentic loop ended without a usable review; submitting with a digest of %d lookup(s)",
            len(lookups),
        )
        try:
            return await review(_with_digest(messages, _lookup_digest(lookups, notes)))
        except Exception as exc:  # noqa: BLE001 — the caller's plain review still runs
            logger.warning("Review with the lookup digest failed: %s", exc)
            return ""

    for hop in range(max_hops):
        final_hop = hop == max_hops - 1
        if final_hop and hop > 0:
            convo.append({"role": "user", "content": _FINAL_HOP_NUDGE})
        try:
            msg = await llm.complete_agentic(convo, tools=tools)
        except Exception as exc:
            logger.warning("Agentic hop %d failed: %s", hop + 1, exc)
            return await _finish()

        # Provider adapters are expected to return a normalized mapping, but a
        # malformed or partially mocked provider must degrade to the regular
        # review path instead of crashing the whole PR review.
        if not isinstance(msg, dict):
            logger.warning(
                "Agentic hop %d returned %s instead of a mapping",
                hop + 1,
                type(msg).__name__,
            )
            return await _finish()

        tool_calls = msg.get("tool_calls")
        content = msg.get("content")
        tool_calls = [] if tool_calls is None else tool_calls
        content = "" if content is None else content

        malformed_calls = not isinstance(tool_calls, list) or any(
            not isinstance(call, dict) or not isinstance(call.get("function"), dict)
            for call in tool_calls
        )
        if malformed_calls or not isinstance(content, str):
            logger.warning("Agentic hop %d returned a malformed response", hop + 1)
            return await _finish()

        if not tool_calls:
            # A model that wrote the review as its reply has still reviewed.
            written = _review_json_in(content)
            if written is not None:
                logger.info("Agentic hop %d answered with the review as text", hop + 1)
                return written
            logger.info(
                "Agentic loop stopped at hop %d without submit_review (%d chars of prose)",
                hop + 1,
                len(content),
            )
            if content.strip():
                notes.append(content.strip())
            return await _finish()

        if content.strip():
            notes.append(content.strip())

        assistant: dict = {
            "role": "assistant",
            "content": content,
            "tool_calls": tool_calls,
        }
        # A Responses-protocol provider hands back its raw output items too;
        # they go back with the next turn so the model keeps its reasoning
        # and the endpoint sees the call ids it issued.
        raw_items = msg.get("items")
        if isinstance(raw_items, list) and raw_items:
            assistant["items"] = raw_items
        convo.append(assistant)

        for call in tool_calls:
            fn = call.get("function") or {}
            name = fn.get("name") or ""
            if name == "submit_review":
                arguments = fn.get("arguments") or ""
                problem = _submission_problem(arguments)
                if problem is None:
                    return arguments if isinstance(arguments, str) else json.dumps(arguments)
                logger.warning("Agentic hop %d: submit_review unusable: %s", hop + 1, problem)
                if final_hop:
                    return await _finish()
                convo.append(
                    {
                        "role": "tool",
                        "tool_call_id": call.get("id") or "",
                        "content": (
                            f"[error: this submission could not be read ({problem}). Call "
                            "`submit_review` again with one complete JSON object: `comments` "
                            "must be a JSON array of objects, not a string.]"
                        ),
                    }
                )
                continue

            raw_args = fn.get("arguments") or "{}"
            parsed_args = raw_args if isinstance(raw_args, dict) else loads_lenient(raw_args)
            if not isinstance(parsed_args, dict):
                # Running the tool on invented arguments hands the model a
                # failed lookup, which it can only read as a fact about the
                # repository. Say what actually went wrong and let it re-issue
                # the call.
                logger.warning("Agentic hop %d: unparsable arguments for %s", hop + 1, name)
                convo.append(
                    {
                        "role": "tool",
                        "tool_call_id": call.get("id") or "",
                        "content": (
                            "[error: the arguments were not valid JSON, so this call did not "
                            "run — re-issue it with one complete JSON object]"
                        ),
                    }
                )
                continue

            tool_result = await executor.execute(name, parsed_args)  # type: ignore[attr-defined]
            convo.append(
                {
                    "role": "tool",
                    "tool_call_id": call.get("id") or "",
                    "content": tool_result,
                }
            )
            shown = ", ".join(f"{k}={v!r}" for k, v in parsed_args.items() if v not in (None, ""))
            lookups.append((f"{name}({shown})", str(tool_result)))

    logger.info("Agentic loop hit its %d-hop cap without submit_review", max_hops)
    return await _finish()


def _indexing_llm(fallback: LLMProvider) -> LLMProvider:
    """Build an indexing-tier provider, falling back to ``fallback`` on error."""
    from mira.llm import create_llm

    try:
        return create_llm(llm_config_for("indexing", load_config().llm))  # type: ignore[return-value]
    except Exception:
        return fallback


def _security_llm(fallback: LLMProvider) -> LLMProvider:
    """Build a security-tier provider, falling back to ``fallback`` on error."""
    from mira.llm import create_llm

    try:
        return create_llm(llm_config_for("security", load_config().llm))  # type: ignore[return-value]
    except Exception:
        return fallback


async def security_review_pass(
    llm: LLMProvider,
    files: list,
    narrowed: list,
    pr_title: str = "",
    security_llm: LLMProvider | None = None,
) -> list[ReviewComment]:
    """Dedicated security review on the security tier (``security_model`` → review model).

    Runs in parallel with the main review. Returns ``[]`` on any failure
    so a transient LLM/API error doesn't kill the main review.

    `narrowed` is `files` with migrations/lockfiles/specs stripped (caller
    decides what counts); falls back to `files` if `narrowed` is empty.

    `security_llm`, when passed, is the caller's already-built security-tier
    provider; otherwise one is constructed from ``load_config()``.
    """
    if not files:
        return []

    target_files = narrowed or files
    if not target_files:
        return []
    sec_llm = security_llm or _security_llm(llm)

    # Split big diffs rather than send one call as large as the window allows:
    # that call was the last to finish on a large pull request, and a smaller
    # model's attention thins out long before its context window does.
    config = load_config()
    budget = min(
        int(config.llm.max_context_tokens * 0.75),
        config.review.agent_token_budget * 2,
    )
    from mira.core.chunker import chunk_files

    chunks = chunk_files(
        target_files,
        budget,
        provider=sec_llm if hasattr(sec_llm, "count_tokens") else None,
    )
    if len(chunks) <= 1:
        return await _security_scan_once(sec_llm, llm, target_files, pr_title)

    logger.info(
        "Security pass: splitting %d files into %d chunks (single-call budget %d tokens)",
        len(target_files),
        len(chunks),
        budget,
    )
    sem = asyncio.Semaphore(load_config().review.max_concurrent_chunks)

    async def _bounded(chunk_files_list: list) -> list[ReviewComment]:
        async with sem:
            return await _security_scan_once(sec_llm, llm, chunk_files_list, pr_title)

    results = await asyncio.gather(*[_bounded(c.files) for c in chunks])
    return [c for chunk_comments in results for c in chunk_comments]


async def _security_scan_once(
    sec_llm: LLMProvider,
    fallback_llm: LLMProvider,
    files: list,
    pr_title: str,
) -> list[ReviewComment]:
    """Run the security scan on a single chunk of files."""
    messages = build_security_review_prompt(files=files, pr_title=pr_title)
    try:
        raw = await sec_llm.complete_with_tools(
            messages=messages,
            tools=[SUBMIT_REVIEW_TOOL],
            temperature=0.0,
        )
    except Exception as exc:
        # Retry on the main LLM rather than drop the security pass entirely.
        if sec_llm is not fallback_llm:
            logger.warning(
                "Security pass on security tier failed (%s); retrying on review LLM",
                exc,
            )
            try:
                raw = await fallback_llm.complete_with_tools(
                    messages=messages,
                    tools=[SUBMIT_REVIEW_TOOL],
                    temperature=0.0,
                )
            except Exception as exc2:
                logger.warning("Security review pass failed: %s", exc2)
                return []
        else:
            logger.warning("Security review pass failed: %s", exc)
            return []

    try:
        parsed = parse_llm_response(raw)
        comments = convert_to_review_comments(parsed, diff_files=files)
    except ResponseParseError as exc:
        logger.warning("Security review pass parse error: %s", exc)
        return []
    except Exception as exc:
        logger.warning("Security review pass conversion failed: %s", exc)
        return []

    for c in comments:
        if not c.category or c.category != "security":
            c.category = "security"
        c.source_pass = "security"
    if comments:
        logger.info("Security pass produced %d candidate comment(s)", len(comments))
    return comments


async def dependency_review_pass(
    llm: LLMProvider,
    manifest_files: list,
    existing_packages: list[str] | None = None,
    pr_title: str = "",
    indexing_llm: LLMProvider | None = None,
) -> list[ReviewComment]:
    """Flag newly-added dependencies that duplicate an existing one.

    Runs in parallel with the main review over only the changed manifest files
    (caller filters those out). ``existing_packages`` is the set of dependency
    names already declared in the repo (from the index) so the model can spot a
    new package that overlaps one already present.

    Returns ``[]`` when no manifest changed or on any failure, so a transient
    LLM/API error doesn't kill the main review. Mirrors ``security_review_pass``.
    """
    if not manifest_files:
        return []

    dep_llm = indexing_llm or _indexing_llm(llm)

    messages = build_dependency_review_prompt(
        files=manifest_files,
        existing_packages=existing_packages,
        pr_title=pr_title,
    )
    try:
        raw = await dep_llm.complete_with_tools(
            messages=messages,
            tools=[SUBMIT_REVIEW_TOOL],
            temperature=0.0,
        )
    except Exception as exc:
        # Retry on the main LLM rather than drop the pass entirely.
        if dep_llm is not llm:
            logger.warning(
                "Dependency pass on indexing tier failed (%s); retrying on review LLM",
                exc,
            )
            try:
                raw = await llm.complete_with_tools(
                    messages=messages,
                    tools=[SUBMIT_REVIEW_TOOL],
                    temperature=0.0,
                )
            except Exception as exc2:
                logger.warning("Dependency review pass failed: %s", exc2)
                return []
        else:
            logger.warning("Dependency review pass failed: %s", exc)
            return []

    try:
        parsed = parse_llm_response(raw)
        comments = convert_to_review_comments(parsed, diff_files=manifest_files)
    except ResponseParseError as exc:
        logger.warning("Dependency review pass parse error: %s", exc)
        return []
    except Exception as exc:
        logger.warning("Dependency review pass conversion failed: %s", exc)
        return []

    for c in comments:
        c.category = "dependency"
    if comments:
        logger.info("Dependency pass produced %d candidate comment(s)", len(comments))
    return comments


# The critic used to see 1,200 characters of hunk and nothing else, and most of
# what it dropped it graded "plausible — depends on code not shown": code the
# reviewer had in front of it, or read with the tools. It now sees the numbered
# hunk and the post-change code around the comment.
_MAX_HUNK_EVIDENCE_CHARS = 3000
_SURROUNDING_LINES = 30
_MAX_SURROUNDING_CHARS = 4000

# A `plausible` warning or blocker is kept at this confidence and above. Lower
# than it was (0.8): with a small critic, "plausible" is mostly "I could not
# see enough to be sure", and the posting floor already holds these to 0.7.
DEFAULT_PLAUSIBLE_MIN_CONFIDENCE = 0.7


def _hunk_evidence(comment: ReviewComment, diff_files: list | None) -> str:
    """The diff hunk(s) covering a comment's lines — the critic's real evidence.

    Numbered like the reviewer's diff, so the comment's line reads straight
    off the hunk.
    """
    if not diff_files:
        return ""
    file = next((f for f in diff_files if f.path == comment.path), None)
    if file is None:
        return ""
    end = comment.end_line or comment.line
    parts = []
    for h in file.hunks:
        h_end = h.target_start + max(h.target_length, 1) - 1
        if comment.line <= h_end and h.target_start <= end:
            parts.append(number_hunk_lines(h))
    text = "\n".join(parts)
    if len(text) > _MAX_HUNK_EVIDENCE_CHARS:
        text = text[:_MAX_HUNK_EVIDENCE_CHARS] + "…"
    return text


async def _surrounding_code(comment: ReviewComment, source_fetcher: object | None) -> str:
    """The post-change file around a comment, numbered, or "" when unavailable."""
    fetch = getattr(source_fetcher, "fetch", None)
    if fetch is None:
        return ""
    try:
        content = await fetch(comment.path)
    except Exception:  # noqa: BLE001 — evidence is best-effort
        return ""
    if not isinstance(content, str) or not content:
        return ""
    # split, not splitlines: a form feed or U+2028 is not a line break to
    # the platform numbering the diff.
    lines = content.split("\n")
    end = comment.end_line or comment.line
    first = max(1, comment.line - _SURROUNDING_LINES)
    last = min(len(lines), end + _SURROUNDING_LINES)
    if first > last:
        return ""
    text = "\n".join(f"{n:>5}  {lines[n - 1]}" for n in range(first, last + 1))
    if len(text) > _MAX_SURROUNDING_CHARS:
        text = text[:_MAX_SURROUNDING_CHARS] + "…"
    return text


def _critique_keep(
    verdict: dict,
    comment: ReviewComment,
    plausible_min_confidence: float = DEFAULT_PLAUSIBLE_MIN_CONFIDENCE,
) -> bool:
    """Deterministic keep rule over the critic's evidence grade.

    The dial lives here, in code, so precision/recall tradeoffs are tunable
    against the benchmark harness instead of buried in prompt wording.
    """
    evidence = verdict.get("evidence")
    if evidence == "proven":
        return True
    if evidence == "plausible":
        return (
            comment.severity >= Severity.WARNING and comment.confidence >= plausible_min_confidence
        )
    if evidence == "unsupported":
        return False
    # Older binary shape (model fallback): honor it.
    return verdict.get("keep") is True


async def self_critique(
    llm: LLMProvider,
    comments: list[ReviewComment],
    learned_rules: list[str] | None = None,
    custom_rules: list[dict[str, str]] | None = None,
    indexing_llm: LLMProvider | None = None,
    diff_files: list | None = None,
    audit: list[dict] | None = None,
    source_fetcher: object | None = None,
    plausible_min_confidence: float = DEFAULT_PLAUSIBLE_MIN_CONFIDENCE,
) -> list[ReviewComment]:
    """Grade each draft comment's evidence and drop the unsupported ones.

    The critic grades evidence (proven / plausible / unsupported) rather
    than making keep/drop calls — an adversarial "find why this is wrong"
    framing systematically kills subtle-but-real findings. The keep rule
    is applied in code afterwards.

    `diff_files`, when passed, lets the critic see the actual hunks each
    comment targets instead of judging from the truncated citation alone.
    `source_fetcher`, when passed, adds the post-change code around each
    comment, so a claim about the lines just outside the hunk can be checked
    rather than graded "not shown".

    Team-documented preferences (learned + custom rules) are surfaced to the
    critic so it doesn't drop comments that align with them as "style nits".

    `indexing_llm`, when passed, is the caller's already-built indexing-tier
    provider; otherwise one is constructed from ``load_config()``.
    """
    if not comments:
        return comments

    surroundings = await asyncio.gather(*(_surrounding_code(c, source_fetcher) for c in comments))
    draft_lines = []
    for i, c in enumerate(comments):
        cited = (c.existing_code or "").strip()
        if len(cited) > 400:
            cited = cited[:400] + "…"
        entry = (
            f"[{i}] {c.path}:{c.line} — {c.severity.name} / {c.category}\n"
            f"    Title: {c.title}\n"
            f"    Body:  {(c.body or '').strip()[:1200]}\n"
            f"    Cites: {cited or '(no code citation)'}\n"
        )
        hunk = _hunk_evidence(c, diff_files)
        if hunk:
            entry += f"    Diff hunk (new-file line numbers on the left):\n{hunk}\n"
        if surroundings[i]:
            entry += f"    The file around it, after the change:\n{surroundings[i]}\n"
        draft_lines.append(entry)

    rules_block = ""
    rule_texts: list[str] = list(learned_rules or [])
    for r in custom_rules or []:
        title = (r.get("title") or "").strip()
        content = (r.get("content") or "").strip()
        rule_texts.append(f"{title}: {content}" if title else content)
    if rule_texts:
        rules_block = (
            "## Team preferences (do NOT grade comments that enforce these as unsupported)\n\n"
            + "\n".join(f"- {t}" for t in rule_texts)
            + "\n\n"
        )

    critic_prompt = (
        "You are grading draft PR comments produced by another reviewer. "
        "For each comment, grade how well the shown code supports the "
        "claimed issue:\n\n"
        "- `proven` — the shown code demonstrates the issue and the "
        "reasoning is correct.\n"
        "- `plausible` — consistent with the shown code but depends on "
        "behaviour or code not shown (cross-file contracts, runtime "
        "values). Real findings often land here; this is a valid grade, "
        "not a failure. Read the surrounding code first: when it settles "
        "the question, the grade is `proven` or `unsupported`, not this.\n"
        "- `unsupported` — the shown code contradicts the claim, the "
        "language-semantics reasoning is wrong (e.g. 'decorator only "
        "registers last route' — stacked decorators register both), or "
        "it's a style preference dressed up as an issue.\n\n"
        "Grade the evidence; do NOT construct counter-arguments. A subtle "
        "issue clearly visible in the code (a predicate with side effects, "
        "an idiom misuse) is `proven` even if reasonable people might ship "
        "it anyway.\n\n" + rules_block + "## Draft comments\n\n" + "\n".join(draft_lines)
    )

    critic_llm = indexing_llm or _indexing_llm(llm)

    try:
        raw = await critic_llm.complete_with_tools(
            messages=[{"role": "user", "content": critic_prompt}],
            tools=[SUBMIT_CRITIQUE_TOOL],
            temperature=0.0,
        )
        data = loads_lenient(raw) if raw else {}
        if data is None:
            data = {}
    except Exception as exc:
        logger.warning("Self-critique LLM call failed: %s. Keeping all drafts.", exc)
        return comments

    verdicts = data.get("verdicts") or []
    if not isinstance(verdicts, list):
        return comments

    keep_indices: set[int] = set()
    verdict_by_idx: dict[int, dict] = {}
    for v in verdicts:
        try:
            idx = int(v.get("index", -1))
        except (TypeError, ValueError):
            continue
        if not 0 <= idx < len(comments):
            continue
        verdict_by_idx[idx] = v
        if _critique_keep(v, comments[idx], plausible_min_confidence):
            keep_indices.add(idx)

    for i, c in enumerate(comments):
        if i in keep_indices:
            continue
        v = verdict_by_idx.get(i)
        evidence = v.get("evidence", "keep=false") if v else "no-verdict"
        reason = str(v.get("reason", "no reason")) if v else "critic returned no verdict"
        if audit is not None:
            audit.append(
                {
                    "stage": "self_critique",
                    "path": c.path,
                    "line": c.line,
                    "title": c.title,
                    "severity": c.severity.name,
                    "category": c.category,
                    "confidence": c.confidence,
                    "reason": f"{evidence}: {reason}",
                }
            )
        logger.info(
            "Self-critique dropped [%d] %s:%d (%s) — %s", i, c.path, c.line, evidence, reason[:120]
        )

    return [c for i, c in enumerate(comments) if i in keep_indices]


async def regenerate_summary(
    llm: LLMProvider,
    comments: list[ReviewComment],
    key_issues: list[KeyIssue],
    pr_title: str,
    pr_description: str,
    fallback: str,
    indexing_llm: LLMProvider | None = None,
) -> str:
    """Rewrite the review summary from the final filed outputs.

    The first-pass summary can mention issues that never got filed (LLM put
    them in prose only) or that were dropped by noise filter / self-critique
    / orphan filter. Regenerate from the surviving structured outputs so
    the prose stays grounded in what actually shipped.

    `indexing_llm`, when passed, is the caller's already-built indexing-tier
    provider; otherwise one is constructed from ``load_config()``.
    """
    if not comments and not key_issues:
        return "No issues found."

    filed_lines = []
    for c in comments:
        filed_lines.append(f"- {c.path}:{c.line} [{c.severity.name} / {c.category}] {c.title}")
    for ki in key_issues:
        filed_lines.append(f"- KEY: {ki.path}:{ki.line} — {ki.issue[:200]}")

    title_line = f"PR title: {pr_title}\n" if pr_title else ""
    desc_line = f"PR description: {pr_description[:400]}\n" if pr_description else ""
    prompt = (
        "Write a 2-3 sentence summary of a PR review. The summary will "
        "appear at the top of the review on GitHub. It must describe "
        "ONLY the issues listed below — do NOT invent, speculate, or "
        "mention concerns that aren't in this list. If the list is "
        "empty, say the PR looks clean. Use plain prose, no markdown "
        "headers or bullets. Reference file paths inline where it "
        "helps.\n\n"
        f"{title_line}{desc_line}\n"
        "## Filed issues\n\n"
        + "\n".join(filed_lines)
        + "\n\nReturn just the summary text — no preamble, no quotes."
    )

    summary_llm = indexing_llm or _indexing_llm(llm)

    try:
        text = await summary_llm.complete(
            messages=[{"role": "user", "content": prompt}],
            json_mode=False,
            temperature=0.0,
        )
    except Exception as exc:
        logger.warning("Summary regen LLM call failed: %s", exc)
        return fallback or "No issues found."

    text = (text or "").strip()
    return text or fallback or "No issues found."
