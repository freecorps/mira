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
