"""Tests for the agentic tool executor used on unindexed-repo reviews."""

from __future__ import annotations

import json

import pytest

from mira.core.passes import agentic_review_loop
from mira.llm.agentic_tools import (
    AGENTIC_TOOLS,
    GREP_REPO_TOOL,
    READ_FILE_TOOL,
    AgenticToolExecutor,
)


class _FakeFetcher:
    def __init__(self, sources: dict[str, str | None]):
        self._sources = sources

    async def fetch(self, path: str) -> str | None:
        return self._sources.get(path)


def _executor(sources: dict[str, str | None], tree: list[str]) -> AgenticToolExecutor:
    return AgenticToolExecutor(source_fetcher=_FakeFetcher(sources), repo_tree=tree)


class TestSchemas:
    def test_tool_set_exposes_both_helpers(self):
        names = [t["function"]["name"] for t in AGENTIC_TOOLS]
        assert "read_file" in names
        assert "grep_repo" in names

    def test_read_file_requires_path(self):
        assert READ_FILE_TOOL["function"]["parameters"]["required"] == ["path"]

    def test_grep_repo_requires_pattern(self):
        assert GREP_REPO_TOOL["function"]["parameters"]["required"] == ["pattern"]


class TestReadFile:
    @pytest.mark.asyncio
    async def test_returns_numbered_content(self):
        ex = _executor({"src/a.py": "alpha\nbeta\n"}, ["src/a.py"])
        out = await ex.execute("read_file", {"path": "src/a.py"})
        assert "src/a.py" in out
        assert "    1  alpha" in out
        assert "    2  beta" in out

    @pytest.mark.asyncio
    async def test_truncates_huge_files(self):
        big = "x" * 20_000
        ex = _executor({"src/big.py": big}, ["src/big.py"])
        out = await ex.execute("read_file", {"path": "src/big.py"})
        assert "truncated" in out

    @pytest.mark.asyncio
    async def test_missing_path_returns_error_string(self):
        ex = _executor({}, [])
        out = await ex.execute("read_file", {})
        assert out.startswith("[error")

    @pytest.mark.asyncio
    async def test_unknown_path_in_tree_suggests_close_match(self):
        ex = _executor({}, ["src/auth/middleware.py", "src/util.py"])
        out = await ex.execute("read_file", {"path": "AUTH/middleware.py"})
        assert "not found" in out
        assert "src/auth/middleware.py" in out

    @pytest.mark.asyncio
    async def test_caches_repeated_reads(self):
        seen: list[str] = []

        class _Counting:
            async def fetch(self, path: str) -> str | None:
                seen.append(path)
                return "hello"

        ex = AgenticToolExecutor(source_fetcher=_Counting(), repo_tree=["src/a.py"])
        await ex.execute("read_file", {"path": "src/a.py"})
        await ex.execute("read_file", {"path": "src/a.py"})
        assert seen == ["src/a.py"]  # only fetched once


class TestGrepRepo:
    @pytest.mark.asyncio
    async def test_path_only_returns_matching_paths(self):
        ex = _executor({}, ["src/auth.py", "src/util.py", "tests/test_auth.py"])
        out = await ex.execute("grep_repo", {"pattern": "auth", "path_only": True})
        assert "src/auth.py" in out
        assert "tests/test_auth.py" in out
        assert "src/util.py" not in out

    @pytest.mark.asyncio
    async def test_content_search_returns_line_hits(self):
        sources = {
            "src/a.py": "def foo():\n    return BAR\n",
            "src/b.py": "import os\nBAR = 1\n",
        }
        ex = _executor(sources, list(sources))
        out = await ex.execute("grep_repo", {"pattern": r"\bBAR\b"})
        assert "src/a.py:2" in out
        assert "src/b.py:2" in out

    @pytest.mark.asyncio
    async def test_path_glob_filters_candidates(self):
        sources = {
            "src/a.py": "needle\n",
            "src/a.go": "needle\n",
        }
        ex = _executor(sources, list(sources))
        out = await ex.execute("grep_repo", {"pattern": "needle", "path_glob": "**/*.go"})
        assert "src/a.go" in out
        assert "src/a.py" not in out

    @pytest.mark.asyncio
    async def test_invalid_regex_returns_error_string(self):
        ex = _executor({"a.py": "x"}, ["a.py"])
        out = await ex.execute("grep_repo", {"pattern": "[unclosed"})
        assert out.startswith("[invalid regex")


class TestExecutorBudget:
    @pytest.mark.asyncio
    async def test_unknown_tool_returns_error(self):
        ex = _executor({}, [])
        out = await ex.execute("delete_repo", {})
        assert "unknown tool" in out

    @pytest.mark.asyncio
    async def test_exhausted_budget_blocks_further_calls(self):
        ex = _executor({"a.py": "hi"}, ["a.py"])
        ex.bytes_used = 1_000_000  # simulate exhaustion
        out = await ex.execute("read_file", {"path": "a.py"})
        assert "budget exhausted" in out


class TestAgenticLoopFallback:
    @pytest.mark.asyncio
    async def test_malformed_provider_message_falls_back(self):
        class _MalformedProvider:
            async def complete_agentic(self, messages, tools):  # type: ignore[no-untyped-def]
                return object()

        result = await agentic_review_loop(  # type: ignore[arg-type]
            _MalformedProvider(),
            [{"role": "user", "content": "review"}],
            object(),
        )

        assert result == ""

    @pytest.mark.asyncio
    async def test_malformed_tool_calls_fall_back(self):
        class _MalformedProvider:
            async def complete_agentic(self, messages, tools):  # type: ignore[no-untyped-def]
                return {"content": "", "tool_calls": {"not": "a list"}}

        result = await agentic_review_loop(  # type: ignore[arg-type]
            _MalformedProvider(),
            [{"role": "user", "content": "review"}],
            object(),
        )

        assert result == ""

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "message",
        [
            {"content": "", "tool_calls": [object()]},
            {"content": "", "tool_calls": [{"function": object()}]},
            {"content": object(), "tool_calls": []},
        ],
    )
    async def test_malformed_nested_response_falls_back(self, message):  # type: ignore[no-untyped-def]
        class _MalformedProvider:
            async def complete_agentic(self, messages, tools):  # type: ignore[no-untyped-def]
                return message

        result = await agentic_review_loop(  # type: ignore[arg-type]
            _MalformedProvider(),
            [{"role": "user", "content": "review"}],
            object(),
        )

        assert result == ""

    @pytest.mark.asyncio
    async def test_a_call_with_unparsable_arguments_is_not_executed(self):
        """Running the tool on invented arguments hands the model a failed
        lookup, which it can only read as a fact about the repository."""
        executed: list[tuple[str, dict]] = []

        class _Executor:
            call_log: list = []

            async def execute(self, name, args):  # type: ignore[no-untyped-def]
                executed.append((name, args))
                return "file contents"

        class _Provider:
            def __init__(self) -> None:
                self.hops = 0

            async def complete_agentic(self, messages, tools):  # type: ignore[no-untyped-def]
                self.hops += 1
                if self.hops == 1:
                    return {
                        "content": "",
                        "tool_calls": [
                            {
                                "id": "call_1",
                                "function": {"name": "read_file", "arguments": "path=a.py"},
                            }
                        ],
                    }
                self.last_messages = list(messages)  # the loop mutates this list
                return {
                    "content": "",
                    "tool_calls": [
                        {
                            "id": "call_2",
                            "function": {
                                "name": "submit_review",
                                "arguments": '{"comments": [], "summary": "done"}',
                            },
                        }
                    ],
                }

        provider = _Provider()
        result = await agentic_review_loop(  # type: ignore[arg-type]
            provider,
            [{"role": "user", "content": "review"}],
            _Executor(),
        )

        assert json.loads(result)["summary"] == "done"
        assert executed == [], "the tool must not run on arguments we invented"
        tool_reply = provider.last_messages[-1]
        assert tool_reply["role"] == "tool"
        assert "not valid JSON" in tool_reply["content"]


class TestAgenticLoopReplay:
    @pytest.mark.asyncio
    async def test_raw_items_ride_along_on_the_next_hop(self):
        """A Responses-protocol provider hands back its raw output items
        (encrypted reasoning, the call with the id the endpoint issued); the
        loop must send them back with the tool output or the next request
        is refused."""
        seen: list[list[dict]] = []
        raw = [
            {"type": "reasoning", "id": "rs_1", "encrypted_content": "abc"},
            {"type": "function_call", "id": "fc_1", "call_id": "call_1", "name": "read_file"},
        ]

        class _Provider:
            async def complete_agentic(self, messages, tools):  # type: ignore[no-untyped-def]
                seen.append(list(messages))
                if len(seen) == 1:
                    return {
                        "content": None,
                        "tool_calls": [
                            {
                                "id": "call_1",
                                "type": "function",
                                "function": {"name": "read_file", "arguments": '{"path": "a"}'},
                            }
                        ],
                        "items": raw,
                    }
                return {
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "call_2",
                            "type": "function",
                            "function": {"name": "submit_review", "arguments": '{"comments": []}'},
                        }
                    ],
                }

        class _Executor:
            async def execute(self, name, args):  # type: ignore[no-untyped-def]
                return "x = 1"

        result = await agentic_review_loop(
            _Provider(), [{"role": "user", "content": "go"}], _Executor()
        )  # type: ignore[arg-type]

        assert result == '{"comments": []}'
        assistant = seen[1][1]
        assert assistant["role"] == "assistant"
        assert assistant["items"] == raw
        assert seen[1][2]["role"] == "tool"


class _SnapshotFetcher(_FakeFetcher):
    """A review's shared reader once its archive has landed."""

    def __init__(self, sources: dict[str, str]):
        super().__init__(dict(sources))
        from mira.platforms.fetch import RepoSnapshot

        self._snapshot = RepoSnapshot(files=dict(sources), paths=set(sources))

    def loaded(self):  # type: ignore[no-untyped-def]
        return self._snapshot


class TestWholeRepoGrep:
    @pytest.mark.asyncio
    async def test_searches_every_file_in_the_snapshot(self):
        """The per-file path could afford 15 fetches, alphabetically: a symbol
        defined in the 16th file was reported as absent."""
        sources = {f"src/mod_{i:03}.py": "x = 1\n" for i in range(200)}
        sources["src/zz_last.py"] = "def target_symbol():\n    pass\n"
        ex = AgenticToolExecutor(source_fetcher=_SnapshotFetcher(sources), repo_tree=list(sources))
        out = await ex.execute("grep_repo", {"pattern": r"def target_symbol"})
        assert "src/zz_last.py:1: def target_symbol():" in out
        assert "searched all 201 files" in out

    @pytest.mark.asyncio
    async def test_anchored_pattern_matches_inside_a_file(self):
        sources = {"a.py": "import os\n\ndef handle():\n    pass\n"}
        ex = AgenticToolExecutor(source_fetcher=_SnapshotFetcher(sources), repo_tree=list(sources))
        out = await ex.execute("grep_repo", {"pattern": "^def handle"})
        assert "a.py:3: def handle():" in out

    @pytest.mark.asyncio
    async def test_files_missing_from_the_archive_weaken_the_claim(self):
        sources = {"a.py": "a"}
        ex = AgenticToolExecutor(
            source_fetcher=_SnapshotFetcher(sources), repo_tree=["a.py", "tests/ignored.py"]
        )
        out = await ex.execute("grep_repo", {"pattern": "nowhere"})
        assert "could not be searched" in out and "whole repository" not in out

    @pytest.mark.asyncio
    async def test_miss_over_the_whole_repo_says_so(self):
        sources = {"a.py": "a", "b.py": "b"}
        ex = AgenticToolExecutor(source_fetcher=_SnapshotFetcher(sources), repo_tree=list(sources))
        out = await ex.execute("grep_repo", {"pattern": "nowhere"})
        assert "whole repository" in out

    @pytest.mark.asyncio
    async def test_source_hits_come_before_test_hits(self):
        sources = {"tests/test_a.py": "call_me()\n", "src/a.py": "def call_me(): ...\n"}
        ex = AgenticToolExecutor(source_fetcher=_SnapshotFetcher(sources), repo_tree=list(sources))
        out = await ex.execute("grep_repo", {"pattern": "call_me"})
        assert out.index("src/a.py") < out.index("tests/test_a.py")

    @pytest.mark.asyncio
    async def test_partial_per_file_search_does_not_claim_absence(self):
        tree = [f"src/f{i:02}.py" for i in range(40)]
        ex = _executor(dict.fromkeys(tree, "nothing here"), tree)
        out = await ex.execute("grep_repo", {"pattern": "target"})
        assert "absence here proves nothing" in out


class TestRangedRead:
    @pytest.mark.asyncio
    async def test_reads_the_asked_lines(self):
        source = "\n".join(f"line {i}" for i in range(1, 1001))
        ex = _executor({"big.py": source}, ["big.py"])
        out = await ex.execute("read_file", {"path": "big.py", "start_line": 500, "end_line": 502})
        assert "lines 500-502 of 1000" in out
        assert "  500  line 500" in out and "  502  line 502" in out
        assert "line 503" not in out
        assert "start_line=503" in out

    @pytest.mark.asyncio
    async def test_truncated_read_says_how_to_read_on(self):
        ex = _executor({"big.py": "x = 1\n" * 5000}, ["big.py"])
        out = await ex.execute("read_file", {"path": "big.py"})
        assert "pass start_line and end_line" in out

    @pytest.mark.asyncio
    async def test_numeric_string_lines_are_accepted(self):
        ex = _executor({"a.py": "one\ntwo\nthree"}, ["a.py"])
        out = await ex.execute("read_file", {"path": "a.py", "start_line": "2", "end_line": "2"})
        assert "    2  two" in out and "three" not in out


def _call(call_id: str, name: str, arguments: str) -> dict:
    return {"id": call_id, "type": "function", "function": {"name": name, "arguments": arguments}}


class _ScriptedLLM:
    """An agentic provider replaying a script, with a forced review to fall back on."""

    def __init__(self, hops: list[dict], review: str = '{"comments": [], "summary": "forced"}'):
        self.hops = list(hops)
        self.seen: list[list[dict]] = []
        self.review_messages: list[list[dict]] = []
        self._review = review

    async def complete_agentic(self, messages, tools):  # type: ignore[no-untyped-def]
        self.seen.append(list(messages))
        return self.hops.pop(0)

    async def review(self, messages, temperature=None):  # type: ignore[no-untyped-def]
        self.review_messages.append(list(messages))
        return self._review


class _Recorder:
    call_log: list = []

    async def execute(self, name, args):  # type: ignore[no-untyped-def]
        return f"result of {name} {args.get('path') or args.get('pattern')}"


class TestAgenticLoopKeepsItsWork:
    @pytest.mark.asyncio
    async def test_broken_submission_is_sent_back_for_another_try(self):
        llm = _ScriptedLLM(
            [
                {"content": "", "tool_calls": [_call("c1", "submit_review", '{"comments": "[{"')]},
                {
                    "content": "",
                    "tool_calls": [
                        _call("c2", "submit_review", '{"comments": [], "summary": "ok"}')
                    ],
                },
            ]
        )
        result = await agentic_review_loop(llm, [{"role": "user", "content": "go"}], _Recorder())
        assert json.loads(result)["summary"] == "ok"
        error = llm.seen[1][-1]
        assert error["role"] == "tool" and "could not be read" in error["content"]
        assert llm.review_messages == []

    @pytest.mark.asyncio
    async def test_prose_answer_after_lookups_submits_with_a_digest(self):
        llm = _ScriptedLLM(
            [
                {"content": "", "tool_calls": [_call("c1", "read_file", '{"path": "a.py"}')]},
                {"content": "The caller passes None; that looks like a bug.", "tool_calls": []},
            ]
        )
        result = await agentic_review_loop(llm, [{"role": "user", "content": "go"}], _Recorder())
        assert json.loads(result)["summary"] == "forced"
        digest = llm.review_messages[0][-1]["content"]
        assert [m["role"] for m in llm.review_messages[0]] == ["user"]
        assert "read_file(path='a.py')" in digest
        assert "result of read_file a.py" in digest
        assert "The caller passes None" in digest

    @pytest.mark.asyncio
    async def test_review_written_as_text_is_accepted(self):
        llm = _ScriptedLLM(
            [{"content": '{"comments": [], "summary": "as text"}', "tool_calls": []}]
        )
        result = await agentic_review_loop(llm, [{"role": "user", "content": "go"}], _Recorder())
        assert json.loads(result)["summary"] == "as text"
        assert llm.review_messages == []

    @pytest.mark.asyncio
    async def test_last_hop_is_told_to_submit(self):
        lookups = [
            {"content": "", "tool_calls": [_call(f"c{i}", "grep_repo", '{"pattern": "x"}')]}
            for i in range(2)
        ]
        final = {
            "content": "",
            "tool_calls": [_call("c9", "submit_review", '{"comments": [], "summary": "last"}')],
        }
        llm = _ScriptedLLM([*lookups, final])
        result = await agentic_review_loop(
            llm, [{"role": "user", "content": "go"}], _Recorder(), max_hops=3
        )
        assert json.loads(result)["summary"] == "last"
        assert "last turn" in llm.seen[2][-1]["content"]

    @pytest.mark.asyncio
    async def test_hop_cap_without_submission_uses_the_digest(self):
        llm = _ScriptedLLM(
            [
                {"content": "", "tool_calls": [_call(f"c{i}", "grep_repo", '{"pattern": "y"}')]}
                for i in range(2)
            ]
        )
        result = await agentic_review_loop(
            llm, [{"role": "user", "content": "go"}], _Recorder(), max_hops=2
        )
        assert json.loads(result)["summary"] == "forced"
        assert "grep_repo(pattern='y')" in llm.review_messages[0][-1]["content"]

    @pytest.mark.asyncio
    async def test_nothing_learned_leaves_the_plain_review_to_the_caller(self):
        llm = _ScriptedLLM([{"content": "", "tool_calls": []}])
        result = await agentic_review_loop(llm, [{"role": "user", "content": "go"}], _Recorder())
        assert result == ""
        assert llm.review_messages == []


# ── Code-graph and index tools ──

_GRAPH_SOURCES = {
    "src/billing/charge.py": (
        "class Charger:\n"
        "    def charge_card(self, card, cents, currency):\n"
        '        """Charge a card."""\n'
        "        validate(card)\n"
        "        return gateway.post(card, cents)\n"
    ),
    "src/api/routes.py": (
        "from billing.charge import Charger\n"
        "\n"
        "def checkout(req):\n"
        "    # charge_card(req) used to live here\n"
        "    return Charger().charge_card(req.card, req.cents)\n"
        "\n"
        "HANDLERS = {'charge': Charger.charge_card}\n"
    ),
    "tests/test_charge.py": "def test_it():\n    Charger().charge_card(1, 2, 'usd')\n",
    "README.md": "charge_card(x)\n",
}


def _graph_executor(**kwargs) -> AgenticToolExecutor:  # type: ignore[no-untyped-def]
    return AgenticToolExecutor(
        source_fetcher=_SnapshotFetcher(_GRAPH_SOURCES),
        repo_tree=list(_GRAPH_SOURCES),
        **kwargs,
    )


class TestToolOffer:
    def test_default_offer_has_the_graph_tools_but_not_the_index(self):
        names = [t["function"]["name"] for t in _graph_executor().tools]
        assert names == ["read_file", "grep_repo", "find_usages", "find_definition"]

    def test_index_search_adds_grep_index(self):
        ex = _graph_executor(index_search=lambda q, n: [])
        assert "grep_index" in [t["function"]["name"] for t in ex.tools]

    def test_graph_tools_can_be_turned_off(self):
        ex = _graph_executor(graph_tools=False)
        assert [t["function"]["name"] for t in ex.tools] == ["read_file", "grep_repo"]

    @pytest.mark.asyncio
    async def test_tools_not_offered_are_not_run(self):
        ex = _graph_executor(graph_tools=False)
        assert "unknown tool" in await ex.execute("find_usages", {"symbol": "charge_card"})
        assert "unknown tool" in await ex.execute("grep_index", {"query": "billing"})

    def test_schemas_require_their_argument(self):
        from mira.llm.agentic_tools import (
            FIND_DEFINITION_TOOL,
            FIND_USAGES_TOOL,
            GREP_INDEX_TOOL,
        )

        assert FIND_USAGES_TOOL["function"]["parameters"]["required"] == ["symbol"]
        assert FIND_DEFINITION_TOOL["function"]["parameters"]["required"] == ["symbol"]
        assert GREP_INDEX_TOOL["function"]["parameters"]["required"] == ["query"]


class TestFindUsages:
    @pytest.mark.asyncio
    async def test_lists_calls_with_their_enclosing_function(self):
        out = await _graph_executor().execute("find_usages", {"symbol": "Charger.charge_card"})
        assert "Usages of `charge_card`" in out
        assert "src/api/routes.py:5 in `checkout`: return Charger().charge_card(" in out
        assert "tests/test_charge.py:2 in `test_it`" in out
        # Calls in source come before calls in tests.
        assert out.index("src/api/routes.py:5") < out.index("tests/test_charge.py:2")
        assert "Defined at: `src/billing/charge.py:2`" in out
        # A function passed as a value and a comment are mentions, not calls.
        mentions = out.split("Other mentions", 1)[1]
        assert "src/api/routes.py:7" in mentions and "src/api/routes.py:4" in mentions
        # A Markdown file is not parsed for calls.
        assert "README.md" not in out

    @pytest.mark.asyncio
    async def test_path_scopes_the_search(self):
        out = await _graph_executor().execute(
            "find_usages", {"symbol": "charge_card", "path": "tests/"}
        )
        assert "tests/test_charge.py:2" in out
        assert "src/api/routes.py" not in out

    @pytest.mark.asyncio
    async def test_a_scope_outside_the_tree_finds_nothing(self):
        out = await _graph_executor().execute(
            "find_usages", {"symbol": "charge_card", "path": "../../etc/"}
        )
        assert out.startswith("[no usages of `charge_card`")

    @pytest.mark.asyncio
    async def test_rejects_patterns(self):
        out = await _graph_executor().execute("find_usages", {"symbol": "charge.*card"})
        assert "not a symbol name" in out
        assert "missing `symbol`" in await _graph_executor().execute("find_usages", {})

    @pytest.mark.asyncio
    async def test_without_a_snapshot_it_greps_by_word(self):
        ex = AgenticToolExecutor(
            source_fetcher=_FakeFetcher(dict(_GRAPH_SOURCES)), repo_tree=list(_GRAPH_SOURCES)
        )
        out = await ex.execute("find_usages", {"symbol": "charge_card", "path": "src/api"})
        assert "word-boundary text matches instead" in out
        assert "src/api/routes.py:5" in out

    @pytest.mark.asyncio
    async def test_works_without_tree_sitter(self, monkeypatch):  # type: ignore[no-untyped-def]
        import sys

        from mira.index import code_graph as cg

        monkeypatch.setitem(sys.modules, "tree_sitter_language_pack", None)
        monkeypatch.setattr(cg, "_ts_state", None)
        monkeypatch.setattr(cg, "_ts_module", None)
        out = await _graph_executor().execute("find_usages", {"symbol": "charge_card"})
        assert "regex parse" in out
        assert "src/api/routes.py:5 in `checkout`" in out

    @pytest.mark.asyncio
    async def test_results_are_cached_and_counted(self, monkeypatch):  # type: ignore[no-untyped-def]
        ex = _graph_executor()
        first = await ex.execute("find_usages", {"symbol": "charge_card"})
        used = ex.bytes_used

        async def _boom(*_a, **_k):  # type: ignore[no-untyped-def]
            raise AssertionError("not cached")

        monkeypatch.setattr(ex, "_find_usages", _boom)
        assert await ex.execute("find_usages", {"symbol": "charge_card"}) == first
        assert ex.bytes_used == used * 2
        assert ex.call_log[-1] == {"tool": "find_usages", "arg": "charge_card"}

    @pytest.mark.asyncio
    async def test_shares_the_review_graph(self):
        from mira.index.code_graph import CodeGraph

        graph = CodeGraph()
        await _graph_executor(code_graph=graph).execute("find_usages", {"symbol": "validate"})
        assert "src/billing/charge.py" in graph


class TestFindDefinition:
    @pytest.mark.asyncio
    async def test_shows_signature_and_body(self):
        out = await _graph_executor().execute("find_definition", {"symbol": "charge_card"})
        assert "`src/billing/charge.py` lines 2-5 (method `Charger.charge_card`)" in out
        assert "`def charge_card(self, card, cents, currency)`" in out
        assert "    4          validate(card)" in out

    @pytest.mark.asyncio
    async def test_qualified_name_narrows(self):
        out = await _graph_executor().execute("find_definition", {"symbol": "Other.charge_card"})
        assert out.startswith("[no definition of `Other.charge_card`")

    @pytest.mark.asyncio
    async def test_long_bodies_are_excerpted(self):
        body = "".join(f"    x{i} = {i}\n" for i in range(40))
        sources = {"m.py": "def long_one():\n" + body}
        ex = AgenticToolExecutor(source_fetcher=_SnapshotFetcher(sources), repo_tree=list(sources))
        out = await ex.execute("find_definition", {"symbol": "long_one"})
        assert "more lines; read_file with start_line=16" in out

    @pytest.mark.asyncio
    async def test_without_a_snapshot_it_greps_for_a_definition(self):
        ex = AgenticToolExecutor(
            source_fetcher=_FakeFetcher(dict(_GRAPH_SOURCES)), repo_tree=list(_GRAPH_SOURCES)
        )
        out = await ex.execute("find_definition", {"symbol": "Charger"})
        assert "definition-keyword text matches" in out
        assert "src/billing/charge.py:1: class Charger:" in out


class TestGrepIndex:
    @pytest.mark.asyncio
    async def test_formats_matches(self):
        from mira.index._store_shared import IndexMatch

        seen: list[tuple[str, int]] = []

        def _search(query: str, limit: int) -> list[IndexMatch]:
            seen.append((query, limit))
            return [
                IndexMatch(
                    path="src/limits.py",
                    summary="Token bucket rate limiting.\nMore detail.",
                    symbols=["TokenBucket"],
                )
            ]

        ex = _graph_executor(index_search=_search)
        out = await ex.execute("grep_index", {"query": "rate limiting"})
        assert "- `src/limits.py` — Token bucket rate limiting. (symbols: TokenBucket)" in out
        assert "More detail" not in out
        await ex.execute("grep_index", {"query": "Rate Limiting"})
        assert len(seen) == 1  # cached, case-insensitively

    @pytest.mark.asyncio
    async def test_no_match_and_missing_query(self):
        ex = _graph_executor(index_search=lambda q, n: [])
        assert "no indexed files match" in await ex.execute("grep_index", {"query": "zzz"})
        assert "missing `query`" in await ex.execute("grep_index", {})

    @pytest.mark.asyncio
    async def test_a_failing_index_is_reported_not_raised(self):
        def _broken(query: str, limit: int):  # type: ignore[no-untyped-def]
            raise RuntimeError("db locked")

        out = await _graph_executor(index_search=_broken).execute("grep_index", {"query": "x"})
        assert "error executing `grep_index`" in out


class TestLoopOffersExecutorTools:
    @pytest.mark.asyncio
    async def test_system_prompt_describes_only_offered_tools(self):
        seen: dict = {}

        class _LLM:
            async def complete_agentic(self, messages, tools):  # type: ignore[no-untyped-def]
                seen["tools"] = [t["function"]["name"] for t in tools]
                seen["system"] = messages[0]["content"]
                return {
                    "content": "",
                    "tool_calls": [_call("c1", "submit_review", '{"comments": []}')],
                }

        messages = [{"role": "system", "content": "sys"}, {"role": "user", "content": "go"}]
        await agentic_review_loop(_LLM(), messages, _graph_executor(graph_tools=False))  # type: ignore[arg-type]
        assert seen["tools"] == ["read_file", "grep_repo", "submit_review"]
        assert "find_usages" not in seen["system"]

        ex = _graph_executor(index_search=lambda q, n: [])
        await agentic_review_loop(_LLM(), messages, ex)  # type: ignore[arg-type]
        assert seen["tools"][-1] == "submit_review"
        assert {"find_usages", "find_definition", "grep_index"} <= set(seen["tools"])
        assert "`find_usages(symbol, path?)`" in seen["system"]
        assert "`grep_index(query)`" in seen["system"]
