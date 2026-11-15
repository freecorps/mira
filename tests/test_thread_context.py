"""A reply on a review thread is checked against the whole fix.

37 of 123 thread replies Mira posted in two days said some form of "not
visible, please show me the code" — about fixes that were pushed, in another
file or past the window. These cover the two halves of the remedy: the diff
since the finding across every file, bounded and prioritised; and the
read-only lookups at the head before the verdict, with their bounds and the
fallback that keeps a thread from going unanswered.
"""

from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from mira.config import LLMConfig, MiraConfig
from mira.core.change_context import mentions_in, render_changes
from mira.core.threads import resolve_verified_threads
from mira.feedback.models import ReviewFinding
from mira.feedback.provenance import finding_marker
from mira.index.store import IndexStore
from mira.llm.agentic_tools import AgenticToolExecutor
from mira.models import PRInfo, UnresolvedThread
from mira.platforms import pr_chat
from mira.platforms.handlers import run_thread_reply
from mira.platforms.thread_context import (
    gather_thread_evidence,
    lookup_llm_config,
    thread_code_context,
)


def _file_diff(path: str, removed: str, added: str, start: int = 1) -> str:
    return (
        f"diff --git a/{path} b/{path}\n"
        f"--- a/{path}\n"
        f"+++ b/{path}\n"
        f"@@ -{start},1 +{start},1 @@\n"
        f"-{removed}\n"
        f"+{added}\n"
    )


_COMPARE = (
    _file_diff("src/app.py", "x = 1", "x = 2")
    + _file_diff("docs/notes.md", "old words", "new words")
    + _file_diff("packages/offline/src/store.ts", "readRows(rows)", "readRows(rows, map)")
    + _file_diff("packages/runtime/src/index.ts", "export { a }", "export { a, COUNT }")
)


# ── The diff since the finding, across every file ────────────────────────────


def test_mentions_pick_up_paths_and_code_identifiers() -> None:
    found = mentions_in(
        "Fixed in `packages/offline/src/store.ts` — `readRows` now takes the map, "
        "and checkImportDoc rejects it. See store.ts too."
    )
    assert "packages/offline/src/store.ts" in found.paths
    assert "store.ts" in found.paths
    assert {"readRows", "checkImportDoc"} <= found.identifiers
    # Prose is not code.
    assert "rejects" not in found.identifiers


def test_every_changed_file_is_listed_with_the_commented_one_first() -> None:
    rendered = render_changes(
        _COMPARE,
        focus_paths=["src/app.py"],
        mentions=mentions_in("the fix is in store.ts"),
    )
    assert rendered.startswith("4 file(s) changed:")
    listing = rendered.split("\n\n", 1)[0].splitlines()[1:]
    assert listing[0].startswith("- src/app.py (+1 -1) [shown]")
    # Named by its filename alone, it comes second.
    assert listing[1].startswith("- packages/offline/src/store.ts")
    assert {line.split(" ")[1] for line in listing} == {
        "src/app.py",
        "docs/notes.md",
        "packages/offline/src/store.ts",
        "packages/runtime/src/index.ts",
    }
    assert "+readRows(rows, map)" in rendered
    assert rendered.index("### src/app.py") < rendered.index("### packages/offline/src/store.ts")


def test_a_symbol_on_a_changed_line_ranks_its_file_above_the_rest() -> None:
    rendered = render_changes(_COMPARE, mentions=mentions_in("is `COUNT` exported now?"))
    listing = rendered.split("\n\n", 1)[0].splitlines()[1:]
    assert listing[0].startswith("- packages/runtime/src/index.ts")


def test_the_budget_bounds_the_hunks_and_says_what_was_left_out() -> None:
    big = _file_diff("src/app.py", "a", "b" * 3000)
    rendered = render_changes(
        big + _file_diff("src/other.py", "c", "d" * 3000),
        focus_paths=["src/app.py"],
        budget=3200,
    )
    assert "src/app.py (+1 -1) [shown]" in rendered
    assert "src/other.py (+1 -1) [not shown — read it with read_file]" in rendered
    assert "d" * 3000 not in rendered


def test_the_commented_file_is_shown_in_part_rather_than_dropped() -> None:
    rendered = render_changes(
        _file_diff("src/app.py", "a", "b" * 5000), focus_paths=["src/app.py"], budget=1000
    )
    assert "[shown in part]" in rendered
    assert "read the rest with read_file" in rendered
    assert len(rendered) < 1500


def test_excluded_files_are_listed_but_not_repeated() -> None:
    rendered = render_changes(_COMPARE, exclude_paths={"src/app.py"})
    assert "src/app.py (+1 -1) [shown above]" in rendered
    assert "### src/app.py" not in rendered


def _pr(head: str = "head2") -> PRInfo:
    return PRInfo(
        title="PR",
        description="",
        base_branch="main",
        head_branch="feature",
        url="https://github.com/acme/app/pull/7",
        number=7,
        owner="acme",
        repo="app",
        base_sha="base",
        head_sha=head,
    )


async def test_thread_context_shows_a_fix_in_another_file() -> None:
    provider = MagicMock()
    provider.get_file_content = AsyncMock(return_value="x = 2\n")
    provider.get_compare_diff = AsyncMock(
        return_value=_file_diff("packages/offline/src/store.ts", "readRows(r)", "readRows(r, m)")
    )
    provider.get_pr_diff = AsyncMock(return_value="")

    text = await thread_code_context(
        provider,
        _pr(),
        "src/app.py",
        1,
        reply="corrigido no store.ts",
        finding_head_sha="head1",
    )

    assert provider.get_compare_diff.await_args.args[1:] == ("head1", "head2")
    assert "The commented file has not changed since the comment" in text
    assert "packages/offline/src/store.ts (+1 -1) [shown]" in text
    assert "+readRows(r, m)" in text


async def test_thread_context_says_when_nothing_was_pushed() -> None:
    provider = MagicMock()
    provider.get_file_content = AsyncMock(return_value="x = 1\n")
    provider.get_compare_diff = AsyncMock()
    provider.get_pr_diff = AsyncMock(return_value="")

    text = await thread_code_context(
        provider, _pr("head1"), "src/app.py", 1, reply="fixed", finding_head_sha="head1"
    )

    provider.get_compare_diff.assert_not_awaited()
    assert "Nothing has been pushed to the pull request since the comment" in text


# ── The lookups at the head ──────────────────────────────────────────────────


class _FakeFetcher:
    def __init__(self, sources: dict[str, str]):
        self._sources = sources

    async def fetch(self, path: str) -> str | None:
        return self._sources.get(path)


_SOURCES = {
    "src/app.py": "def run(rows):\n    return readRows(rows)\n",
    "packages/offline/src/store.ts": (
        "export function readRows(rows, map) {\n  return rows.map((r) => map[r]);\n}\n"
    ),
}


def _executor() -> AgenticToolExecutor:
    return AgenticToolExecutor(
        source_fetcher=_FakeFetcher(_SOURCES), repo_tree=sorted(_SOURCES), graph_tools=False
    )


@asynccontextmanager
async def _tools(*_args: Any, **_kwargs: Any):
    yield _executor()


@asynccontextmanager
async def _no_tools(*_args: Any, **_kwargs: Any):
    yield None


def _call(name: str, args: dict, call_id: str = "c1") -> dict:
    return {
        "id": call_id,
        "type": "function",
        "function": {"name": name, "arguments": json.dumps(args)},
    }


_READ_STORE = {
    "content": "",
    "tool_calls": [_call("read_file", {"path": "packages/offline/src/store.ts"})],
}
_NOTE = {"content": "packages/offline/src/store.ts:1 readRows takes the map.", "tool_calls": []}


def _config(**chat: Any) -> MiraConfig:
    config = MiraConfig()
    for key, value in chat.items():
        setattr(config.chat, key, value)
    return config


async def _gather(llm: Any, config: MiraConfig | None = None, tools: Any = _tools):
    return await gather_thread_evidence(
        MagicMock(),
        _pr(),
        llm=llm,
        config=config or _config(),
        purpose="recheck",
        finding_text="store.ts readRows ignores the map",
        reply="fixed in store.ts",
        path="src/app.py",
        line=2,
        code_context="File at the pull request's head …",
        tools=tools,
    )


async def test_lookups_read_the_fix_and_frame_it_as_data() -> None:
    llm = MagicMock()
    llm.complete_agentic = AsyncMock(side_effect=[_READ_STORE, _NOTE])

    found = await _gather(llm)

    assert found.route == "agentic"
    assert found.lookups == ["read_file(path='packages/offline/src/store.ts')"]
    assert "rows.map((r) => map[r])" in found.text
    assert "<<<MIRA-UNTRUSTED-FILE>>>" in found.text
    assert "readRows takes the map" in found.text
    # The lookup prompt is told not to write the verdict, and frames the reply.
    first = llm.complete_agentic.await_args_list[0].args[0]
    assert "do not decide the verdict" in first[0]["content"]
    assert "<<<MIRA-UNTRUSTED-REPLY>>>" in first[1]["content"]


async def test_lookups_stop_at_their_hop_limit_and_keep_what_they_found() -> None:
    llm = MagicMock()
    llm.complete_agentic = AsyncMock(return_value=_READ_STORE)

    found = await _gather(llm, _config(thread_max_hops=3))

    assert llm.complete_agentic.await_count == 3
    assert found.route == "partial"
    assert found.lookups  # the reads before the limit are kept
    assert "rows.map" in found.text


async def test_a_failed_hop_never_raises_and_keeps_earlier_lookups() -> None:
    llm = MagicMock()
    llm.complete_agentic = AsyncMock(side_effect=[_READ_STORE, RuntimeError("down")])

    found = await _gather(llm)

    assert found.route == "partial"
    assert "rows.map" in found.text


async def test_lookups_past_their_deadline_keep_what_they_found() -> None:
    async def slow_second(*_args: Any, **_kwargs: Any) -> dict:
        if slow_second.calls:
            await asyncio.sleep(5)
        slow_second.calls += 1
        return _READ_STORE

    slow_second.calls = 0
    llm = MagicMock()
    llm.complete_agentic = slow_second
    config = _config()
    config.chat.thread_deadline_seconds = 0.2  # below the field's floor, set for speed

    found = await _gather(llm, config)

    assert found.route == "partial"
    assert "rows.map" in found.text


async def test_no_tools_or_lookups_off_means_no_calls() -> None:
    llm = MagicMock()
    llm.complete_agentic = AsyncMock()

    assert (await _gather(llm, tools=_no_tools)).route == "off"
    assert (await _gather(llm, _config(thread_lookups=False))).route == "off"
    assert (await _gather(llm, _config(agentic=False))).route == "off"
    llm.complete_agentic.assert_not_awaited()


def test_lookup_effort_only_ever_lowers() -> None:
    high = LLMConfig(reasoning_effort="high")
    assert lookup_llm_config(high, "low").reasoning_effort == "low"
    assert high.reasoning_effort == "high"
    low = LLMConfig(reasoning_effort="low")
    assert lookup_llm_config(low, "medium") is low
    # A model that does not reason is not made to for a lookup.
    off = LLMConfig(reasoning_effort=None)
    assert lookup_llm_config(off, "low") is off
    assert lookup_llm_config(LLMConfig(reasoning_effort="off"), "low").reasoning_effort == "off"
    assert lookup_llm_config(high, "") is high


# ── The whole reply ──────────────────────────────────────────────────────────


def _finding(finding_id: str) -> ReviewFinding:
    return ReviewFinding(
        id=finding_id,
        fingerprint="fingerprint",
        review_id=0,
        platform="github",
        owner="acme",
        repo="app",
        pr_number=7,
        pr_url="https://github.com/acme/app/pull/7",
        base_sha="base",
        head_sha="head1",
        path="src/app.py",
        start_line=2,
        end_line=2,
        symbol="run",
        category="bug",
        severity="warning",
        confidence=0.9,
        title="readRows ignores the map",
        body="The offline store's readRows never receives the map.",
        suggestion="",
        detector="main",
        prompt_model="test-model",
    )


def _reply_setup(tmp_path, monkeypatch, finding_id: str):
    monkeypatch.setenv("MIRA_INDEX_DIR", str(tmp_path))
    monkeypatch.setenv("MIRA_DASHBOARD_URL", "https://mira.example")
    finding = _finding(finding_id)
    store = IndexStore.open("acme", "app")
    store.save_review_finding(finding)
    store.update_review_finding_posted(finding.id, 123)
    store.close()
    provider = AsyncMock()
    provider.get_file_content = AsyncMock(return_value=_SOURCES["src/app.py"])
    provider.get_compare_diff = AsyncMock(
        return_value=_file_diff(
            "packages/offline/src/store.ts", "readRows(rows)", "readRows(rows, map)"
        )
    )
    provider.get_pr_diff = AsyncMock(return_value="")
    provider.reply_to_review_comment = AsyncMock()
    provider.resolve_threads = AsyncMock(return_value=1)
    kwargs = {
        "original_suggestion": finding_marker(finding.id),
        "thread_id": "thread-1",
        "comment_path": finding.path,
        "comment_line": finding.start_line,
        "actor": "alice",
        "parent_comment_id": 123,
        "source_event_id": "review-comment:900",
    }
    return finding, provider, kwargs


def _llm(hops: list, verdicts: list) -> MagicMock:
    llm = MagicMock()
    llm.complete_agentic = AsyncMock(side_effect=hops)
    llm.complete_with_tools = AsyncMock(side_effect=[json.dumps(v) for v in verdicts])
    return llm


@pytest.mark.asyncio
async def test_a_fix_in_another_file_is_looked_up_and_closes_the_thread(
    tmp_path, monkeypatch
) -> None:
    finding, provider, kwargs = _reply_setup(
        tmp_path, monkeypatch, "00000000-0000-4000-8000-0000000000e1"
    )
    llm = _llm(
        [_READ_STORE, _NOTE],
        [
            {"intent": "fixed", "reply": "Checking."},
            {"resolved": True, "reply": "store.ts readRows now takes the map."},
        ],
    )
    with (
        patch("mira.platforms.handlers.load_config", return_value=MiraConfig()),
        patch("mira.platforms.handlers.create_llm", return_value=llm),
        patch("mira.platforms.handlers.llm_config_for", return_value=MagicMock()),
        patch.object(pr_chat, "repository_tools", _tools),
    ):
        await run_thread_reply(provider, _pr(), "Corrigido no store.ts", 900, **kwargs)

    recheck = llm.complete_with_tools.await_args_list[1]
    prompt = recheck.kwargs["messages"][0]["content"]
    assert recheck.kwargs["tools"][0]["function"]["name"] == "submit_finding_recheck"
    # The cross-file diff and the lookup both reached the verdict.
    assert "packages/offline/src/store.ts (+1 -1) [shown]" in prompt
    assert "What you looked up at the pull request's head" in prompt
    assert "rows.map((r) => map[r])" in prompt
    assert "Never ask the author" in prompt
    assert "store.ts readRows" in provider.reply_to_review_comment.await_args.args[2]
    provider.resolve_threads.assert_awaited_once()
    store = IndexStore.open("acme", "app")
    assert store.get_review_finding(finding.id).state == "fixed"
    store.close()


@pytest.mark.asyncio
async def test_failed_lookups_fall_back_to_the_one_shot_recheck(tmp_path, monkeypatch) -> None:
    _finding_row, provider, kwargs = _reply_setup(
        tmp_path, monkeypatch, "00000000-0000-4000-8000-0000000000e2"
    )
    llm = _llm(
        [RuntimeError("the endpoint is down")],
        [
            {"intent": "fixed", "reply": "Checking."},
            {"resolved": False, "reply": "readRows in store.ts still drops the map."},
        ],
    )
    with (
        patch("mira.platforms.handlers.load_config", return_value=MiraConfig()),
        patch("mira.platforms.handlers.create_llm", return_value=llm),
        patch("mira.platforms.handlers.llm_config_for", return_value=MagicMock()),
        patch.object(pr_chat, "repository_tools", _tools),
    ):
        await run_thread_reply(provider, _pr(), "fixed", 900, **kwargs)

    prompt = llm.complete_with_tools.await_args_list[1].kwargs["messages"][0]["content"]
    assert "What you looked up" not in prompt
    # The cross-file diff is there even without the lookups.
    assert "+readRows(rows, map)" in prompt
    assert "still drops the map" in provider.reply_to_review_comment.await_args.args[2]
    provider.resolve_threads.assert_not_awaited()


@pytest.mark.asyncio
async def test_tools_that_cannot_open_still_get_a_reply(tmp_path, monkeypatch) -> None:
    @asynccontextmanager
    async def broken(*_args: Any, **_kwargs: Any):
        raise RuntimeError("snapshot unavailable")
        yield  # pragma: no cover

    _row, provider, kwargs = _reply_setup(
        tmp_path, monkeypatch, "00000000-0000-4000-8000-0000000000e3"
    )
    llm = _llm(
        [],
        [{"intent": "fixed", "reply": "Checking."}, {"resolved": True, "reply": "Confirmed."}],
    )
    with (
        patch("mira.platforms.handlers.load_config", return_value=MiraConfig()),
        patch("mira.platforms.handlers.create_llm", return_value=llm),
        patch("mira.platforms.handlers.llm_config_for", return_value=MagicMock()),
        patch.object(pr_chat, "repository_tools", broken),
    ):
        await run_thread_reply(provider, _pr(), "done", 900, **kwargs)

    assert provider.reply_to_review_comment.await_args.args[2] == "Confirmed."
    llm.complete_agentic.assert_not_awaited()


@pytest.mark.asyncio
async def test_lookups_run_at_the_lower_effort_and_the_verdict_at_the_review_one(
    tmp_path, monkeypatch
) -> None:
    _row, provider, kwargs = _reply_setup(
        tmp_path, monkeypatch, "00000000-0000-4000-8000-0000000000e4"
    )
    review_llm = _llm(
        [],
        [{"intent": "fixed", "reply": "Checking."}, {"resolved": True, "reply": "Confirmed."}],
    )
    lookup_llm = _llm([_NOTE], [])
    made: list[Any] = []

    def create(cfg: Any) -> Any:
        made.append(cfg)
        return review_llm if len(made) == 1 else lookup_llm

    with (
        patch("mira.platforms.handlers.load_config", return_value=MiraConfig()),
        patch("mira.platforms.handlers.create_llm", side_effect=create),
        patch(
            "mira.platforms.handlers.llm_config_for",
            return_value=LLMConfig(reasoning_effort="high"),
        ),
        patch.object(pr_chat, "repository_tools", _tools),
    ):
        await run_thread_reply(provider, _pr(), "fixed", 900, **kwargs)

    assert [cfg.reasoning_effort for cfg in made] == ["high", "low"]
    lookup_llm.complete_agentic.assert_awaited_once()
    review_llm.complete_agentic.assert_not_awaited()
    assert review_llm.complete_with_tools.await_count == 2


@pytest.mark.asyncio
async def test_a_reply_that_needs_no_verdict_looks_nothing_up(tmp_path, monkeypatch) -> None:
    _row, provider, kwargs = _reply_setup(
        tmp_path, monkeypatch, "00000000-0000-4000-8000-0000000000e5"
    )
    llm = _llm([], [{"intent": "agreement", "reply": "Thanks."}])
    with (
        patch("mira.platforms.handlers.load_config", return_value=MiraConfig()),
        patch("mira.platforms.handlers.create_llm", return_value=llm),
        patch("mira.platforms.handlers.llm_config_for", return_value=MagicMock()),
        patch.object(pr_chat, "repository_tools", _tools),
    ):
        await run_thread_reply(provider, _pr(), "fair, will do", 900, **kwargs)

    llm.complete_agentic.assert_not_awaited()
    assert provider.reply_to_review_comment.await_args.args[2] == "Thanks."


# ── Verifying fixes on push ──────────────────────────────────────────────────


async def test_verifying_fixes_on_push_sees_the_other_files() -> None:
    provider = MagicMock()
    provider.get_unresolved_bot_threads = AsyncMock(
        return_value=[
            UnresolvedThread(
                thread_id="T1", path="src/app.py", line=2, body="`readRows` drops the map."
            )
        ]
    )
    provider.get_file_content = AsyncMock(return_value=_SOURCES["src/app.py"])
    provider.get_pr_diff = AsyncMock(return_value=_COMPARE)
    provider.resolve_threads = AsyncMock(return_value=1)
    llm = MagicMock()
    llm.generate_object = AsyncMock(
        return_value=MagicMock(results=[MagicMock(id="T1", fixed=True)])
    )

    checked, resolved, _remaining, _decisions = await resolve_verified_threads(
        provider, llm, _pr(), "mira", dry_run=False
    )

    assert (checked, resolved) == (1, 1)
    prompt = llm.generate_object.await_args.args[0][1]["content"]
    assert "Changes elsewhere in this pull request" in prompt
    assert "src/app.py (+1 -1) [shown above]" in prompt
    # Named by a symbol on its changed lines, the store comes first of the rest.
    others = prompt.split("Changes elsewhere", 1)[1]
    assert others.index("packages/offline/src/store.ts") < others.index("docs/notes.md")
    assert "+readRows(rows, map)" in prompt


async def test_verifying_fixes_survives_an_unreadable_diff() -> None:
    provider = MagicMock()
    provider.get_unresolved_bot_threads = AsyncMock(
        return_value=[UnresolvedThread(thread_id="T1", path="src/app.py", line=2, body="bug")]
    )
    provider.get_file_content = AsyncMock(return_value="x\n")
    provider.get_pr_diff = AsyncMock(side_effect=RuntimeError("406"))
    llm = MagicMock()
    llm.generate_object = AsyncMock(return_value=MagicMock(results=[]))

    checked, resolved, _r, _d = await resolve_verified_threads(
        provider, llm, _pr(), "mira", dry_run=True
    )

    assert (checked, resolved) == (1, 0)
    assert "Changes elsewhere" not in llm.generate_object.await_args.args[0][1]["content"]
