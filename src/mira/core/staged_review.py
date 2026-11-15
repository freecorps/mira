"""The staged review of one part: context first, then at most two thinking calls.

The agentic loop this replaces let the model decide, one hop at a time, what
to look up next. On a thinking model every hop re-read the whole growing
conversation, sixty-odd calls on a large pull request and up to five per part
and pass, with parts that ran out their 480 s and were read again. Most of
what the hops looked up is known before the model is called:
the functions a part changes, who calls them, what the functions they call
do, the tests next to them.

So a part is reviewed in stages here:

1. :func:`build_context_pack` gathers that context deterministically from the
   code graph over the reviewed commit — callers of the changed functions,
   definitions of what they call, related test files — bounded in time and
   size and ranked, so the most useful lookups land first.
2. :func:`review_part` makes the first thinking call over the part's prompt
   (which carries the pack, the changed functions in full and the index
   context), offering the lookup tools beside ``submit_review``. A thinking
   model that calls a tool stops thinking to call it, so the model either
   submits or asks, early, for what it still needs; those lookups run side by
   side (:func:`run_lookups`) and the second call can only submit. Two
   thinking calls per part at most, one round of lookups.
3. A part never fails for lack of time and is never re-run from scratch: a
   thinking call that times out, errors, or answers with nothing usable is
   followed by one call without thinking over the same prompt, with the time
   left (and at least ``degraded_call_seconds``). Only when that fails too is
   the part's files reported unread.

Deadlines come from the review's budget (:class:`ReviewBudget`): every stage
derives its own from what is left. A thinking call is also hedged — sent a
second time when its reply has not started streaming after
``hedge_after_seconds`` — so one call stuck in a queue does not hold the
review.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
from collections import Counter
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass, field
from typing import Any, TypeVar

from mira.index.code_graph import added_lines, grammar_for_path
from mira.llm import accounting, concurrency
from mira.llm.response_parser import loads_lenient, parse_llm_response
from mira.llm.tool_schemas import SUBMIT_REVIEW_TOOL
from mira.models import FileChangeType, FileDiff

logger = logging.getLogger(__name__)

T = TypeVar("T")

# Fractions of the review's time budget by which each phase must be done.
PARTS_FRACTION = 0.70
CRITIQUE_FRACTION = 0.90

# A thinking call is not started with less than this left: it would be cut
# before it answered, and the degraded read is the better use of the time.
_MIN_THINKING_SECONDS = 30.0
# Lookups for the follow-up run for at most this long, together.
_LOOKUP_SECONDS = 20.0
# Characters of one follow-up lookup's result, and of all of them.
_LOOKUP_RESULT_CHARS = 6_000
_LOOKUP_TOTAL_CHARS = 24_000
# Room the submitting turn needs on top of the first prompt: the lookups'
# results and the first turn. Reserved when the prompt is sized.
FOLLOWUP_RESERVE_TOKENS = 9_000

# The context pack: changed functions whose callers are listed, callers per
# function, called definitions shown, lines of each definition's excerpt.
_PACK_MAX_SYMBOLS = 6
_PACK_MAX_CALLERS = 6
_PACK_MAX_CALLEES = 8
_PACK_DEFINITION_LINES = 14
_PACK_MAX_TESTS = 4

# Names a changed line calls that are never worth a definition lookup.
_COMMON_CALLEES = frozenset(
    {
        # Python builtins and the methods every container has
        "len", "str", "int", "float", "bool", "dict", "list", "set", "tuple", "range",
        "isinstance", "issubclass", "getattr", "setattr", "hasattr", "type", "super",
        "print", "repr", "sorted", "reversed", "enumerate", "zip", "map", "filter",
        "min", "max", "sum", "any", "all", "abs", "round", "open", "iter", "next",
        "append", "extend", "insert", "pop", "remove", "get", "items", "keys", "values",
        "update", "copy", "join", "split", "strip", "rstrip", "lstrip", "replace",
        "format", "startswith", "endswith", "lower", "upper", "encode", "decode",
        "add", "discard", "clear", "setdefault", "sort", "index", "count", "find",
        "frozenset", "bytes", "object", "property", "staticmethod", "classmethod",
        "Exception", "ValueError", "TypeError", "KeyError", "RuntimeError", "OSError",
        "TimeoutError", "NotImplementedError", "AssertionError", "AttributeError",
        # JavaScript / TypeScript
        "push", "slice", "splice", "concat", "includes", "indexOf", "forEach", "reduce",
        "some", "every", "then", "catch", "finally", "resolve", "reject", "toString",
        "parseInt", "parseFloat", "Number", "String", "Boolean", "Array", "Object",
        "Promise", "Error", "Map", "Set", "JSON", "parse", "stringify", "log", "warn",
        "error", "info", "debug", "require", "trim", "test", "exec", "match",
        "useState", "useEffect", "useMemo", "useCallback", "useRef", "setTimeout",
        "clearTimeout", "setInterval", "clearInterval",
        # Go / Rust / Java
        "make", "new", "cap", "panic", "recover", "Println", "Printf", "Sprintf",
        "Errorf", "unwrap", "expect", "clone", "into", "to_string", "as_str",
        "collect", "Some", "Ok", "Err", "equals", "hashCode", "size", "isEmpty",
    }
)  # fmt: skip

_TEST_MARKERS = ("test", "spec")


# ── The review's time budget ──


@dataclass
class ReviewBudget:
    """The review's wall-clock budget, and the deadline of each phase in it.

    ``total`` is in seconds; 0 means unbounded, and every deadline is then
    None. Monotonic time throughout.
    """

    started: float
    total: float

    def deadline(self, fraction: float) -> float | None:
        return self.started + self.total * fraction if self.total > 0 else None

    def left(self, fraction: float, now: float | None = None) -> float | None:
        at = self.deadline(fraction)
        return None if at is None else at - (time.monotonic() if now is None else now)


async def within(
    awaitable: Awaitable[T], seconds: float | None, default: T, what: str, audit: list | None = None
) -> T:
    """``awaitable``'s result, or ``default`` once ``seconds`` have passed or it failed.

    For the stages beside the parts (security, dependencies, the whole-PR
    pass): they add findings, and none of them may hold the review past its
    budget or fail it.
    """
    try:
        if seconds is None:
            return await awaitable
        return await asyncio.wait_for(awaitable, max(1.0, seconds))
    except Exception as exc:  # noqa: BLE001 — a side stage never fails the review
        if isinstance(exc, TimeoutError) and not str(exc):
            exc = TimeoutError(f"ran past its {max(1.0, seconds or 0):.0f}s share of the budget")
        logger.warning("%s left out: %s", what, exc)
        if audit is not None:
            audit.append({"stage": "stage_left_out", "what": what, "error": str(exc)[:300]})
        return default


# ── Calls ──


@dataclass
class StagedSettings:
    """What :func:`review_part` reads from ``review.*``."""

    call_timeout: float = 180.0
    degraded_seconds: float = 60.0
    max_lookups: int = 6
    hedge_after: float = 60.0

    @classmethod
    def from_config(cls, review: Any, *, streams: bool) -> StagedSettings:
        return cls(
            call_timeout=float(review.call_timeout_seconds),
            degraded_seconds=float(review.degraded_call_seconds),
            max_lookups=int(review.max_lookups),
            hedge_after=float(review.hedge_after_seconds) if streams else 0.0,
        )


async def hedged(
    factory: Callable[[], Awaitable[T]],
    hedge_after: float,
    on_hedge: Callable[[], None] | None = None,
) -> T:
    """``factory()``'s result, sent a second time if its reply has not started in time.

    The first reply to start streaming is what is watched
    (:func:`mira.llm.concurrency.watch_first_response`): a call that has
    started is left to finish, however long it thinks. One that shows no
    sign of life after ``hedge_after`` seconds was queued or stalled, so the
    same call goes out again and whichever answers first wins; the other is
    cancelled. A failure of one waits for the other. 0 turns it off.
    """
    if hedge_after <= 0:
        return await factory()
    started = asyncio.Event()
    with concurrency.watch_first_response(started):
        first: asyncio.Future[T] = asyncio.ensure_future(factory())
    tasks: list[asyncio.Future] = [first]
    try:
        waiter = asyncio.ensure_future(started.wait())
        try:
            await asyncio.wait({first, waiter}, timeout=hedge_after, return_when="FIRST_COMPLETED")
        finally:
            waiter.cancel()
        if first.done() or started.is_set():
            return await first
        logger.info("No reply after %.0fs; sending the call again (hedge)", hedge_after)
        if on_hedge is not None:
            on_hedge()
        second: asyncio.Future[T] = asyncio.ensure_future(factory())
        tasks.append(second)
        pending: set[asyncio.Future] = {first, second}
        failure: BaseException | None = None
        while pending:
            done, pending = await asyncio.wait(pending, return_when="FIRST_COMPLETED")
            for task in done:
                exc = task.exception()
                if exc is None:
                    return task.result()
                failure = exc
        assert failure is not None
        raise failure
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        for task in tasks:
            with contextlib.suppress(BaseException):
                if task.cancelled() or not task.done():
                    continue
                task.exception()  # retrieved, so the loser's error is not logged as lost


def submission_problem(raw: object) -> str | None:
    """Why a submitted review cannot be used, or None when it can."""
    if not isinstance(raw, str) or not raw.strip():
        return "the answer was empty"
    try:
        parse_llm_response(raw)
    except Exception as exc:  # noqa: BLE001 — any parse failure makes it unusable
        return str(exc)[:300]
    return None


def _label(tool: str, args: dict) -> str:
    shown = ", ".join(f"{k}={v!r}" for k, v in args.items() if v not in (None, ""))
    return f"{tool}({shown})"


async def run_lookups(
    executor: Any, calls: list[tuple[str, dict]], seconds: float = _LOOKUP_SECONDS
) -> list[tuple[str, str]]:
    """``calls`` (tool, arguments) run side by side; ``(label, result)`` for each, in order.

    Each result is cut to a share of the room, and a lookup still running
    after ``seconds`` is reported as such rather than waited for.
    """
    if not calls:
        return []
    if executor is None:
        return [(_label(t, a), "[no repository to look this up in]") for t, a in calls]
    tasks = [asyncio.ensure_future(executor.execute(tool, args)) for tool, args in calls]
    done, pending = await asyncio.wait(tasks, timeout=max(0.5, seconds))
    for task in pending:
        task.cancel()
    out: list[tuple[str, str]] = []
    used = 0
    for (tool, args), task in zip(calls, tasks, strict=True):
        label = _label(tool, args)
        if task not in done or task.cancelled() or task.exception() is not None:
            out.append((label, "[this lookup did not finish in time]"))
            continue
        text = str(task.result())
        if len(text) > _LOOKUP_RESULT_CHARS:
            text = text[:_LOOKUP_RESULT_CHARS] + "\n… [cut]"
        if used + len(text) > _LOOKUP_TOTAL_CHARS:
            out.append((label, "[left out: the lookups' room is spent]"))
            continue
        used += len(text)
        out.append((label, text))
    return out


def render_evidence(lookups: list[tuple[str, str]], max_chars: int = 8_000) -> str:
    """The lookups a part made, as the critic reads them."""
    blocks: list[str] = []
    used = 0
    for label, result in lookups:
        block = f"{label}:\n{result}"
        if used + len(block) > max_chars:
            blocks.append(f"{label}: [left out for length]")
            continue
        blocks.append(block)
        used += len(block)
    return "\n\n".join(blocks)


def _guidance(max_lookups: int) -> str:
    return (
        "\n\n## Context and lookups\n\n"
        "What the repository can tell you about this change was gathered before this "
        "call — the changed functions in full, the callers of the functions this part "
        "changes, the definitions they call, the index's notes. Review from it, and "
        "trace contracts across it.\n\n"
        "When a suspected issue can only be settled by code you were not shown, call "
        "the lookup tools for it — `read_file`, `grep_repo`, `find_usages`, "
        "`find_definition`. You get **one** round: ask for everything you need in the "
        f"same turn (at most {max_lookups} calls; they run together), and the next turn "
        "can only submit. When the context is enough — most parts need nothing more — "
        "call `submit_review` straight away."
    )


def with_guidance(messages: list[dict], max_lookups: int) -> list[dict]:
    """``messages`` with the staged instructions after the system prompt."""
    out = [dict(m) for m in messages]
    if out and out[0].get("role") == "system" and max_lookups > 0:
        out[0]["content"] = str(out[0].get("content") or "") + _guidance(max_lookups)
    return out


_SUBMIT_NOW = (
    "The lookups you asked for are above. This is your last turn: call `submit_review` "
    "now with every finding you can support, or with an empty `comments` list."
)


def _digest(lookups: list[tuple[str, str]]) -> str:
    """The lookups, for a degraded read that could not hold the conversation."""
    if not lookups:
        return ""
    body = "\n\n".join(f"### {label}\n{result}" for label, result in lookups)
    return (
        "\n\n## Lookups already made for this review\n\n"
        "The repository at this pull request's head:\n\n" + body[:_LOOKUP_TOTAL_CHARS]
    )


def _with_text(messages: list[dict], text: str) -> list[dict]:
    """``messages`` with ``text`` after the last user turn."""
    if not text:
        return messages
    out = [dict(m) for m in messages]
    for message in reversed(out):
        if message.get("role") == "user" and isinstance(message.get("content"), str):
            message["content"] = message["content"] + text
            return out
    out.append({"role": "user", "content": text})
    return out


def _submission_of(msg: object) -> tuple[str | None, list[dict]]:
    """``(review JSON or None, lookup calls)`` from one agentic turn."""
    from mira.core.passes import _review_json_in

    if not isinstance(msg, dict):
        return None, []
    calls = msg.get("tool_calls") or []
    lookups: list[dict] = []
    if isinstance(calls, list):
        for call in calls:
            fn = call.get("function") if isinstance(call, dict) else None
            if not isinstance(fn, dict):
                continue
            if fn.get("name") == "submit_review":
                args = fn.get("arguments")
                raw = args if isinstance(args, str) else json.dumps(args or {})
                if submission_problem(raw) is None:
                    return raw, []
            else:
                lookups.append(call)
    content = msg.get("content")
    if isinstance(content, str) and content.strip():
        written = _review_json_in(content)
        if written is not None:
            return written, []
    return None, lookups


@dataclass
class PartOutcome:
    """How a staged part went, for the audit trail and the tests."""

    raw: str = ""
    thinking_calls: int = 0
    degraded: bool = False
    hedged: bool = False
    lookups: list[str] = field(default_factory=list)
    # The lookups' results, handed to verification with the part's findings.
    evidence: list[tuple[str, str]] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    seconds: float = 0.0

    def audit(self, chunk: int | str) -> dict:
        entry: dict = {
            "stage": "staged_part",
            "chunk": chunk,
            "thinking_calls": self.thinking_calls,
            "degraded": self.degraded,
            "seconds": round(self.seconds, 2),
        }
        if self.lookups:
            entry["lookups"] = self.lookups
        if self.hedged:
            entry["hedged"] = True
        if self.errors:
            entry["errors"] = self.errors[:4]
        return entry


async def review_part(
    llm: Any,
    messages: list[dict],
    *,
    deadline_at: float | None,
    settings: StagedSettings,
    executor: Any = None,
    degraded_llm: Any = None,
    temperature: float | None = None,
    outcome: PartOutcome | None = None,
    degrade: bool = True,
    stage: str = "pass",
) -> str:
    """One part, reviewed with at most two thinking calls; the ``submit_review`` JSON.

    The first call has the part's prompt (context pack included) and the
    lookup tools. A thinking model that calls a tool stops thinking to call
    it — in production the hops of the old loop reasoned 1.8k tokens at the
    median, where one call asked to review a whole part outright ran past
    16k on DeepSeek Flash Eco and was cut — so a model unsure of something
    asks early instead of reasoning its way around it. Every lookup it asks
    for runs side by side, and the second call is offered ``submit_review``
    alone: it can only answer. Its reasoning is carried over, so it goes on
    from where the first stopped.

    ``deadline_at`` (monotonic) is when the part should be done. A call that
    fails, times out or answers with nothing usable is followed by one read
    without thinking (``degraded_llm``, or ``llm`` when None) over the same
    prompt and the lookups made so far, given what is left and at least
    ``settings.degraded_seconds``. Raises only when that read fails too —
    or, with ``degrade=False`` (the whole-PR pass, which adds findings and
    may be dropped), instead of making it.
    """
    from mira.core.passes import assistant_turn
    from mira.llm.agentic_tools import AGENTIC_TOOLS

    out = outcome if outcome is not None else PartOutcome()
    started = time.monotonic()
    sampling: dict = {} if temperature is None else {"temperature": temperature}
    offered = getattr(executor, "tools", None) if executor is not None else None
    helpers = list(offered) if isinstance(offered, list) and offered else list(AGENTIC_TOOLS)
    use_tools = executor is not None and settings.max_lookups > 0
    convo = with_guidance(messages, settings.max_lookups if use_tools else 0)
    min_thinking = min(_MIN_THINKING_SECONDS, settings.call_timeout)

    def _left() -> float:
        return float("inf") if deadline_at is None else deadline_at - time.monotonic()

    def _mark_hedged() -> None:
        out.hedged = True

    async def _hop(msgs: list[dict], tools: list[dict], seconds: float) -> object:
        out.thinking_calls += 1
        hedge_after = settings.hedge_after if settings.hedge_after < seconds else 0.0

        async def _call() -> object:
            if use_tools:
                return await llm.complete_agentic(msgs, tools=tools, **sampling)
            return await llm.complete_with_tools(msgs, tools=tools, **sampling)

        return await asyncio.wait_for(hedged(_call, hedge_after, on_hedge=_mark_hedged), seconds)

    review: str | None = None
    seconds = min(settings.call_timeout, _left())
    if seconds < min_thinking:
        out.errors.append(f"no time for a thinking call ({max(0.0, _left()):.0f}s left)")
    else:
        try:
            with accounting.stage(stage):
                first = await _hop(
                    convo,
                    [*helpers, SUBMIT_REVIEW_TOOL] if use_tools else [SUBMIT_REVIEW_TOOL],
                    seconds,
                )
            if not use_tools:
                if submission_problem(first) is None:
                    review = str(first)
                else:
                    out.errors.append("first reading unusable")
                lookup_calls: list[dict] = []
            else:
                review, lookup_calls = _submission_of(first)
            if review is None and lookup_calls:
                parsed: list[tuple[str, dict]] = []
                for call in lookup_calls[: settings.max_lookups]:
                    fn = call["function"]
                    raw_args = fn.get("arguments") or "{}"
                    args = raw_args if isinstance(raw_args, dict) else loads_lenient(raw_args)
                    parsed.append(
                        (str(fn.get("name") or ""), args if isinstance(args, dict) else {})
                    )
                results = await run_lookups(
                    executor, parsed, min(_LOOKUP_SECONDS, max(1.0, _left()))
                )
                out.lookups = [label for label, _ in results]
                out.evidence = results
                assert isinstance(first, dict)
                content = first.get("content") if isinstance(first.get("content"), str) else ""
                turn = [
                    *convo,
                    assistant_turn(first, content or "", lookup_calls),
                ]
                for i, call in enumerate(lookup_calls):
                    text = (
                        results[i][1]
                        if i < len(results)
                        else f"[not run: one round of at most {settings.max_lookups} lookups]"
                    )
                    turn.append(
                        {"role": "tool", "tool_call_id": call.get("id") or "", "content": text}
                    )
                turn.append({"role": "user", "content": _SUBMIT_NOW})
                seconds = min(settings.call_timeout, _left())
                if seconds >= min_thinking:
                    with accounting.stage(f"{stage}_followup"):
                        second = await _hop(turn, [SUBMIT_REVIEW_TOOL], seconds)
                    review, _ignored = _submission_of(second)
                    if review is None:
                        out.errors.append("the submitting turn did not submit")
                else:
                    out.errors.append(
                        f"no time for the submitting turn ({max(0.0, _left()):.0f}s left)"
                    )
            elif review is None:
                out.errors.append("the first turn neither submitted nor looked anything up")
        except Exception as exc:  # noqa: BLE001 — degraded below
            if isinstance(exc, TimeoutError) and not str(exc):
                exc = TimeoutError(f"thinking call ran past {seconds:.0f}s")
            out.errors.append(f"{type(exc).__name__}: {exc}"[:300])
            logger.warning(
                "Staged part: thinking call failed (%s); reading it without thinking", exc
            )
            review = None

    if review is not None:
        out.raw = review
        out.seconds = time.monotonic() - started
        return review

    if not degrade:
        out.seconds = time.monotonic() - started
        raise RuntimeError("; ".join(out.errors) or "no usable review")
    # Degraded: one read without thinking, with what is left and at least the floor.
    out.degraded = True
    client = degraded_llm or llm
    seconds = max(settings.degraded_seconds, min(settings.call_timeout, _left()))
    try:
        with accounting.stage(f"{stage}_degraded"):
            raw = await asyncio.wait_for(
                client.review(_with_text(messages, _digest(out.evidence)), **sampling), seconds
            )
    except Exception as exc:
        out.seconds = time.monotonic() - started
        if isinstance(exc, TimeoutError) and not str(exc):
            raise TimeoutError(f"the degraded read ran past {seconds:.0f}s") from exc
        raise
    problem = submission_problem(raw)
    out.seconds = time.monotonic() - started
    if problem is not None:
        raise ValueError(f"the degraded read was unusable: {problem}")
    out.raw = raw
    return raw


# ── The context pack ──


def _test_like(path: str) -> bool:
    lower = path.lower()
    name = lower.rsplit("/", 1)[-1]
    return any(m in name for m in _TEST_MARKERS) or "/tests/" in lower or lower.startswith("tests/")


def related_tests(
    files: Iterable[FileDiff], tree: Iterable[str], limit: int = _PACK_MAX_TESTS
) -> list[str]:
    """Test files named after a changed file (`test_x.py`, `x.test.ts`, `x_test.go`…).

    The pull request's own files are left out: their diff is already in the
    prompt.
    """
    changed = {f.path for f in files}
    stems: set[str] = set()
    for f in files:
        if _test_like(f.path):
            continue
        stem = f.path.rsplit("/", 1)[-1].split(".", 1)[0].lower()
        if len(stem) >= 3 and stem not in ("index", "main", "__init__", "mod", "lib", "utils"):
            stems.add(stem)
    if not stems:
        return []
    out: list[str] = []
    for path in sorted(tree):
        if path in changed:
            continue
        name = path.rsplit("/", 1)[-1].lower()
        base = name.split(".", 1)[0]
        for stem in stems:
            if base in (f"test_{stem}", f"{stem}_test", f"{stem}test", f"test{stem}") or (
                base == stem and any(f".{m}." in name for m in _TEST_MARKERS)
            ):
                out.append(path)
                break
        if len(out) >= limit:
            break
    return out


def _callee_name(defn: Any) -> str:
    """The name a definition is called by: a constructor by its class."""
    if defn.name in ("__init__", "constructor") and "." in defn.qualified_name:
        return defn.qualified_name.rsplit(".", 2)[-2]
    return defn.name


def _worth_looking_up(name: str) -> bool:
    if len(name) < 3 or name in _COMMON_CALLEES:
        return False
    return not (name.startswith("__") and name.endswith("__"))


@dataclass
class ContextPack:
    text: str = ""
    symbols: list[str] = field(default_factory=list)
    callees: list[str] = field(default_factory=list)
    tests: list[str] = field(default_factory=list)
    seconds: float = 0.0
    timed_out: bool = False

    def audit(self, chunk: int) -> dict:
        return {
            "stage": "context_pack",
            "chunk": chunk,
            "symbols": self.symbols,
            "callees": self.callees,
            "tests": self.tests,
            "chars": len(self.text),
            "seconds": round(self.seconds, 2),
            "timed_out": self.timed_out,
        }


async def build_context_pack(
    files: list[FileDiff],
    executor: Any,
    *,
    char_budget: int,
    seconds: float,
    changed_lines: dict[str, set[int]] | None = None,
) -> ContextPack:
    """Callers, callees and related tests for a part's changes; never raises.

    ``executor`` is the part's :class:`~mira.llm.agentic_tools.AgenticToolExecutor`
    (its code graph and source reader). ``changed_lines`` maps every reviewed
    file to its added lines, so a call site this pull request itself
    rewrites is marked as such. What is ready after ``seconds`` goes in.
    """
    started = time.monotonic()
    pack = ContextPack()
    if executor is None or char_budget <= 0:
        return pack
    try:
        return await _build_pack(
            files, executor, char_budget, seconds, changed_lines or {}, pack, started
        )
    except Exception as exc:  # noqa: BLE001 — optional context, never a failed part
        logger.warning("Context pack failed, reviewing without it: %s", exc)
        pack.seconds = time.monotonic() - started
        return pack


async def _build_pack(
    files: list[FileDiff],
    executor: Any,
    char_budget: int,
    seconds: float,
    changed_lines: dict[str, set[int]],
    pack: ContextPack,
    started: float,
) -> ContextPack:
    deadline = started + seconds
    targets = [
        f
        for f in files
        if not f.is_binary
        and f.hunks
        and f.change_type != FileChangeType.DELETED
        and grammar_for_path(f.path)
    ]
    pack.tests = related_tests(files, getattr(executor, "repo_tree", []) or [])
    graph = executor._graph() if hasattr(executor, "_graph") else None
    fetch = getattr(getattr(executor, "source_fetcher", None), "fetch", None)
    symbols: list[tuple[int, Any]] = []  # (changed lines, definition)
    call_counts: Counter[str] = Counter()
    own_names: set[str] = set()
    if graph is not None and fetch is not None and targets:
        heads = await asyncio.wait_for(
            asyncio.gather(*(fetch(f.path) for f in targets), return_exceptions=True),
            max(1.0, deadline - time.monotonic()),
        )
        sources = [(f, h) for f, h in zip(targets, heads, strict=True) if isinstance(h, str) and h]
        parsed = await asyncio.to_thread(
            lambda: [(f, graph.add_file(f.path, h)) for f, h in sources]
        )
        for f, fg in parsed:
            if fg is None:
                continue
            added = added_lines(f)
            if not added:
                continue
            defs = [d for d in fg.definitions if d.kind in ("function", "method")]
            for d in defs:
                touched = sum(1 for n in added if d.start_line <= n <= d.end_line)
                if touched:
                    symbols.append((touched, d))
                    own_names.add(d.name)
            for ref in fg.references:
                if ref.line in added and _worth_looking_up(ref.name):
                    call_counts[ref.name] += 1
    # Changed functions whose callers to list: most-changed first, one per name.
    ranked: list[Any] = []
    seen_names: set[str] = set()
    for _touched, d in sorted(symbols, key=lambda item: -item[0]):
        name = _callee_name(d)
        if name in seen_names or not _worth_looking_up(name) or name.lower().startswith("test"):
            continue
        seen_names.add(name)
        ranked.append(d)
        if len(ranked) >= _PACK_MAX_SYMBOLS:
            break
    callees = [n for n, _ in call_counts.most_common() if n not in own_names][:_PACK_MAX_CALLEES]

    caller_tasks = {d: asyncio.ensure_future(executor.references(_callee_name(d))) for d in ranked}
    callee_tasks = {n: asyncio.ensure_future(executor.definitions(n)) for n in callees}
    everything = [*caller_tasks.values(), *callee_tasks.values()]
    if everything:
        _done, pending = await asyncio.wait(
            everything, timeout=max(0.5, deadline - time.monotonic())
        )
        for task in pending:
            task.cancel()
            pack.timed_out = True

    def _result(task: asyncio.Future) -> Any:
        if not task.done() or task.cancelled() or task.exception() is not None:
            return None
        return task.result()

    sections: list[str] = []
    caller_blocks: list[str] = []
    for d, task in caller_tasks.items():
        found = _result(task)
        if not found:
            continue
        refs, complete = found
        name = _callee_name(d)
        callers = [
            r
            for r in refs
            if r.name == name and not (r.path == d.path and d.start_line <= r.line <= d.end_line)
        ]
        if not callers:
            continue
        callers.sort(key=lambda r: (_test_like(r.path), r.path, r.line))
        lines = [f"### Callers of `{d.qualified_name}` (`{d.path}`:{d.start_line}, changed here)"]
        for r in callers[:_PACK_MAX_CALLERS]:
            where = f" in `{r.enclosing}`" if r.enclosing else ""
            mark = (
                " (on a line this PR changes)" if r.line in changed_lines.get(r.path, set()) else ""
            )
            lines.append(f"- `{r.path}:{r.line}`{where}{mark}: `{r.text}`")
        if len(callers) > _PACK_MAX_CALLERS:
            lines.append(f"- … {len(callers) - _PACK_MAX_CALLERS} more")
        if not complete:
            lines.append("- (a partial search: an absent caller proves nothing)")
        caller_blocks.append("\n".join(lines))
        pack.symbols.append(d.qualified_name)
    callee_blocks: list[str] = []
    for name, task in callee_tasks.items():
        defs = _result(task)
        if not defs:
            continue
        d = defs[0]
        fg = graph.get(d.path) if graph is not None else None
        lines_of = fg.lines if fg else []
        last = min(d.end_line, d.start_line + _PACK_DEFINITION_LINES - 1, len(lines_of))
        body = "\n".join(f"{n:>5}  {lines_of[n - 1]}" for n in range(d.start_line, last + 1))
        more = f"\n  … {d.end_line - last} more lines" if d.end_line > last else ""
        others = f" ({len(defs) - 1} other definition(s) of the name)" if len(defs) > 1 else ""
        callee_blocks.append(
            f"### `{d.qualified_name}` — `{d.path}` lines {d.start_line}-{d.end_line}{others}\n"
            f"```\n{body}{more}\n```"
        )
        pack.callees.append(name)

    if caller_blocks:
        sections.append(
            "Who calls the functions this part changes — check each call site against "
            "the new behaviour:\n\n" + "\n\n".join(caller_blocks)
        )
    if callee_blocks:
        sections.append(
            "What the changed lines call, as defined at this pull request's head:\n\n"
            + "\n\n".join(callee_blocks)
        )
    if pack.tests:
        sections.append(
            "Tests next to these files that this pull request does not change: "
            + ", ".join(f"`{t}`" for t in pack.tests)
        )
    if not sections:
        pack.seconds = time.monotonic() - started
        return pack
    header = (
        "\n\n## Code around this change (gathered before the review)\n\n"
        "Looked up for you from the code graph at this pull request's head — use it "
        "instead of guessing what a caller passes or a callee returns. File comments "
        "only on changed lines of the diff.\n\n"
    )
    text = header
    for section in sections:
        if len(text) + len(section) + 2 > char_budget:
            room = char_budget - len(text) - 40
            if room > 400:
                text += section[:room].rsplit("\n", 1)[0] + "\n… [cut to fit]\n\n"
            break
        text += section + "\n\n"
    pack.text = text.rstrip() + "\n" if text != header else ""
    pack.seconds = time.monotonic() - started
    return pack
