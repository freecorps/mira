"""Streaming, reasoning echo, process-wide refusals, deadlines and timeout text."""

from __future__ import annotations

import json
import time
from functools import partial
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from mira.config import LLMConfig
from mira.core.engine import _timed_out
from mira.core.passes import _FINAL_HOP_NUDGE, agentic_review_loop
from mira.exceptions import LLMError
from mira.llm.provider import LLMProvider, _assemble_stream

_GO = "https://opencode.ai/zen/go/v1"
_TOOLS = [
    {
        "type": "function",
        "function": {"name": "read_file", "parameters": {"type": "object", "properties": {}}},
    }
]


@pytest.fixture(autouse=True)
def _keys(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENCODE_API_KEY", "test-key")
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")


def _sse(*events: object) -> bytes:
    lines = [": keep-alive", ""]
    for event in events:
        lines += [f"data: {json.dumps(event)}", ""]
    lines += ["data: [DONE]", ""]
    return "\n".join(lines).encode()


async def _lines(*lines: str):
    for line in lines:
        yield line


def _client_factory(handler):
    transport = httpx.MockTransport(handler)
    return partial(httpx.AsyncClient, transport=transport)


@pytest.mark.asyncio
async def test_a_stream_assembles_into_the_plain_payload() -> None:
    events = [
        {"choices": [{"delta": {"reasoning_content": "Look at "}}]},
        {"choices": [{"delta": {"reasoning_content": "app.py."}}]},
        {
            "choices": [
                {
                    "delta": {
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": "call_1",
                                "function": {"name": "read_file", "arguments": '{"pa'},
                            }
                        ]
                    }
                }
            ]
        },
        {
            "choices": [
                {"delta": {"tool_calls": [{"index": 0, "function": {"arguments": 'th": "a"}'}}]}}
            ]
        },
        {"choices": [{"delta": {}, "finish_reason": "tool_calls"}]},
        {"choices": [], "usage": {"prompt_tokens": 10, "completion_tokens": 4}},
    ]
    data = await _assemble_stream(
        _lines(": keep-alive", *(f"data: {json.dumps(e)}" for e in events), "data: [DONE]")
    )
    message = data["choices"][0]["message"]
    assert message["reasoning_content"] == "Look at app.py."
    assert message["tool_calls"] == [
        {
            "id": "call_1",
            "type": "function",
            "function": {"name": "read_file", "arguments": '{"path": "a"}'},
        }
    ]
    assert data["choices"][0]["finish_reason"] == "tool_calls"
    assert data["usage"]["completion_tokens"] == 4


@pytest.mark.asyncio
async def test_an_error_event_mid_stream_is_an_llm_error() -> None:
    with pytest.raises(LLMError):
        await _assemble_stream(_lines('data: {"error": {"message": "overloaded"}}'))


@pytest.mark.asyncio
async def test_a_streaming_endpoint_echoes_reasoning_in_the_tool_loop() -> None:
    seen: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        seen.append(body)
        reply = {"choices": [{"delta": {"content": "done"}, "finish_reason": "stop"}]}
        return httpx.Response(
            200, content=_sse(reply), headers={"content-type": "text/event-stream"}
        )

    provider = LLMProvider(LLMConfig(model="deepseek-v4.1-flash", base_url=_GO))
    messages = [
        {"role": "user", "content": "review"},
        {"role": "assistant", "content": "", "reasoning_content": "thinking", "tool_calls": []},
    ]
    with patch("mira.llm.provider.httpx.AsyncClient", side_effect=_client_factory(handler)):
        message = await provider.complete_agentic(messages, tools=_TOOLS)

    assert message["content"] == "done"
    assert seen[0]["stream"] is True
    assert seen[0]["messages"][1]["reasoning_content"] == "thinking"


@pytest.mark.asyncio
async def test_an_endpoint_that_refuses_echoed_reasoning_is_asked_again_without_it() -> None:
    seen: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        seen.append(body)
        if "reasoning_content" in body["messages"][1]:
            return httpx.Response(400, json={"error": "unknown field reasoning_content"})
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

    provider = LLMProvider(LLMConfig(model="deepseek-v4.1-flash", base_url=_GO))
    messages = [
        {"role": "user", "content": "review"},
        {"role": "assistant", "content": "", "reasoning_content": "thinking", "tool_calls": []},
    ]
    with patch("mira.llm.provider.httpx.AsyncClient", side_effect=_client_factory(handler)):
        assert (await provider.complete_agentic(messages, tools=_TOOLS))["content"] == "ok"
        # Remembered for the process: a new client does not ask with it again.
        again = LLMProvider(LLMConfig(model="deepseek-v4.1-flash", base_url=_GO))
        await again.complete_agentic(messages, tools=_TOOLS)

    assert [("reasoning_content" in body["messages"][1]) for body in seen] == [True, False, False]


@pytest.mark.asyncio
async def test_a_plain_endpoint_never_sees_reasoning_or_streaming() -> None:
    seen: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

    provider = LLMProvider(LLMConfig(model="m", base_url="https://llm.example/v1"))
    messages = [
        {"role": "user", "content": "review"},
        {"role": "assistant", "content": "", "reasoning_content": "thinking", "tool_calls": []},
    ]
    with patch("mira.llm.provider.httpx.AsyncClient", side_effect=_client_factory(handler)):
        await provider.complete_agentic(messages, tools=_TOOLS)

    assert "stream" not in seen[0]
    assert "reasoning_content" not in seen[0]["messages"][1]


@pytest.mark.asyncio
async def test_a_streamed_timeout_is_not_asked_again_the_same_way() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        raise httpx.ReadTimeout("silent", request=request)

    provider = LLMProvider(
        LLMConfig(model="deepseek-v4.1-flash", base_url=_GO, retry_min_wait=0, retry_max_wait=0)
    )
    with (
        patch("mira.llm.provider.httpx.AsyncClient", side_effect=_client_factory(handler)),
        pytest.raises(LLMError) as raised,
    ):
        await provider.complete_agentic([{"role": "user", "content": "x"}], tools=_TOOLS)

    assert calls == 1
    assert "silent" in str(raised.value)
    assert _timed_out(raised.value)


def test_refusals_are_shared_by_every_client_of_an_endpoint() -> None:
    first = LLMProvider(LLMConfig(model="m", base_url="https://llm.example/v1"))
    first._no_forced_tool_choice.add("m")
    second = LLMProvider(LLMConfig(model="m", base_url="https://llm.example/v1/"))
    other = LLMProvider(LLMConfig(model="m", base_url="https://other.example/v1"))
    assert "m" in second._no_forced_tool_choice
    assert "m" not in other._no_forced_tool_choice


def test_an_empty_timeout_still_names_itself() -> None:
    error = LLMError("agentic_failed", model="deepseek", error=httpx.ReadTimeout(""))
    assert str(error).endswith("ReadTimeout")


def test_timeouts_are_found_down_the_cause_chain() -> None:
    try:
        try:
            raise httpx.ReadTimeout("")
        except httpx.ReadTimeout as inner:
            raise LLMError("agentic_failed", model="m", error=inner) from inner
    except LLMError as outer:
        assert _timed_out(outer)
    assert not _timed_out(ValueError("bad json"))


class _Executor:
    tools = _TOOLS

    async def execute(self, name, args):
        return "file contents"


@pytest.mark.asyncio
async def test_the_loop_keeps_reasoning_and_submits_once_out_of_time() -> None:
    lookup = {
        "content": "",
        "reasoning_content": "need the caller",
        "tool_calls": [
            {"id": "c1", "type": "function", "function": {"name": "read_file", "arguments": "{}"}}
        ],
    }
    submit = {
        "content": "",
        "tool_calls": [
            {
                "id": "c2",
                "type": "function",
                "function": {"name": "submit_review", "arguments": '{"comments": []}'},
            }
        ],
    }
    llm = AsyncMock()
    llm.complete_agentic = AsyncMock(side_effect=[lookup, submit])

    result = await agentic_review_loop(
        llm,
        [{"role": "system", "content": "sys"}, {"role": "user", "content": "diff"}],
        _Executor(),
        submit_by=time.monotonic() - 1,
    )

    assert json.loads(result) == {"comments": []}
    second = llm.complete_agentic.await_args_list[1].args[0]
    assistant = next(m for m in second if m.get("role") == "assistant")
    assert assistant["reasoning_content"] == "need the caller"
    # The loop's conversation is one list, so look for the nudge in it.
    assert {"role": "user", "content": _FINAL_HOP_NUDGE} in second
