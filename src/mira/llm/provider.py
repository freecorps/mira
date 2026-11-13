"""OpenAI-compatible API provider with retry/fallback and tool calling support.

Per-provider quirks (attribution headers, model-prefix policy, reasoning
remapping) come from the profile registry in ``mira.llm.provider_profiles``, matched
to the configured ``base_url``. OpenRouter is the one profile with quirks; any
other OpenAI-compatible endpoint works off the portable default, no entry needed.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, ClassVar

import httpx

from mira.exceptions import LLMError, ToolCallFormatError
from mira.llm.base import (
    OpenAICompatibleProvider,
    _as_json_object,
    _asked_for_reasoning,
    _carries_reasoning,
    _drop_reasoning,
    _empty_reply_detail,
    _finish_reason,
    _pick_tool_call,
    _preview,
    _set_budget,
    _strip_model_prefix,
    _tool_arguments,
    _tool_name,
)

logger = logging.getLogger(__name__)


class LLMProvider(OpenAICompatibleProvider):
    """OpenAI-compatible API client for LLM completions (/chat/completions).

    Inherits protocol-agnostic infrastructure (retry setup, headers, reasoning,
    fallback model logic, public API) from :class:`OpenAICompatibleProvider`.
    """

    supports_json_mode: ClassVar[bool] = True
    supports_tool_calling: ClassVar[bool] = True

    def _chat_url(self) -> str:
        return f"{self.config.base_url.rstrip('/')}/chat/completions"

    def _streams(self, body: dict) -> bool:
        return bool(self.profile.get("stream")) and body.get("model") not in self._no_stream

    def _timeout(self) -> httpx.Timeout | int:
        """Per-read limits. Streaming turns the read limit into an idle limit."""
        if self.profile.get("stream"):
            return httpx.Timeout(
                float(self.config.request_timeout),
                read=float(self.config.stream_idle_timeout),
            )
        return self.config.request_timeout

    async def _post(self, client: Any, body: dict) -> Any:
        """POST a chat request, streamed where the endpoint's profile says so.

        A streamed reply is assembled into the same payload a plain one has,
        so every caller reads both alike. An endpoint that answers the
        streamed request with a 400 about streaming is asked again plainly,
        and that model is not streamed again in this process.
        """
        if not self._streams(body):
            return await client.post(self._chat_url(), headers=self._build_headers(), json=body)
        resp = await _stream_chat(
            client,
            self._chat_url(),
            self._build_headers(),
            body,
            total=float(self.config.stream_total_timeout),
        )
        if resp.status_code == 400 and "stream" in resp.text.lower():
            logger.info("Model %s rejected a streamed request; asking plainly", body.get("model"))
            self._no_stream.add(str(body.get("model")))
            return await client.post(self._chat_url(), headers=self._build_headers(), json=body)
        return resp

    def _wire_messages(self, model: str, messages: list) -> list:
        """The loop's messages as this endpoint takes them.

        The agentic loop keeps each assistant turn's reasoning. DeepSeek's
        thinking mode needs it back on every later request of the loop, or it
        reasons from scratch each hop over a growing conversation; endpoints
        whose profile does not say so get the turns without it.
        """
        echo = bool(self.profile.get("echo_reasoning")) and model not in self._no_echo_reasoning
        if echo:
            return messages
        return [_without_reasoning(message) for message in messages]

    async def _call_llm(
        self,
        model: str,
        messages: list[dict[str, str]],
        json_mode: bool,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> str:
        """Make a single LLM call with retries against the /chat/completions endpoint."""
        body: dict = {
            "model": _strip_model_prefix(model, self.profile),
            "messages": messages,
        }
        _set_budget(body, "max_tokens", max_tokens, self._max_tokens_for(model))
        self._temperature(body, temperature)
        if json_mode:
            body["response_format"] = {"type": "json_object"}
        self._apply_reasoning(body)

        async with httpx.AsyncClient(timeout=self._timeout()) as client:
            resp = await self._post(client, body)
            if self._refused_temperature(resp, body):
                resp = await self._post(client, body)
            self._handle_error(resp)
            data = resp.json()

        self._account_usage(data)
        message = self._chat_message(data)
        content = message.get("content") or ""
        if not content.strip():
            # Said here, where the payload still is: the caller only sees an
            # empty string and cannot tell a spent thinking budget from a
            # gateway that dropped the answer.
            logger.warning(
                "Model %s returned an %s",
                model,
                _empty_reply_detail(_finish_reason(data), reasoning=_carries_reasoning(message)),
            )
        return content

    async def _call_llm_with_tools(
        self,
        model: str,
        messages: list[dict[str, str]],
        tools: list[dict],
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> str:
        """Make an LLM call with tool/function calling and retries.

        The LLM returns structured data by 'calling' a tool. We extract the
        tool arguments as the JSON response.
        """
        api_model = _strip_model_prefix(model, self.profile)
        if not tools:
            raise LLMError("no_tools")
        forced_choice: dict | str = {
            "type": "function",
            "function": {"name": tools[0]["function"]["name"]},
        }
        body: dict = {
            "model": api_model,
            "messages": messages,
            "tools": tools,
            # Force the one tool for structured args; models that reject a
            # forced choice fall back to "auto" (handled on the 400 below).
            "tool_choice": "auto" if api_model in self._no_forced_tool_choice else forced_choice,
        }
        _set_budget(body, "max_tokens", None, self._structured_budget(model, max_tokens))
        self._temperature(body, temperature)
        self._apply_reasoning(body)

        async with httpx.AsyncClient(timeout=self._timeout()) as client:
            resp = await self._post(client, body)
            if (
                resp.status_code == 400
                and body["tool_choice"] != "auto"
                and "tool_choice" in resp.text.lower()
            ):
                # Forced choice unsupported — remember it and let the model pick.
                logger.info("Model %s rejected forced tool_choice; retrying with auto", api_model)
                self._no_forced_tool_choice.add(api_model)
                body["tool_choice"] = "auto"
                resp = await self._post(client, body)
            if (
                resp.status_code == 400
                and _asked_for_reasoning(body)
                and "reasoning" in resp.text.lower()
            ):
                # Reasoning effort unsupported on this model/endpoint — drop it
                # and review without thinking instead of failing the review.
                logger.info("Model %s rejected reasoning effort; retrying without it", api_model)
                self._no_reasoning.add(api_model)
                _drop_reasoning(body)
                self._temperature(body, temperature)
                resp = await self._post(client, body)
            if self._refused_temperature(resp, body):
                resp = await self._post(client, body)
            self._handle_error(resp)
            data = resp.json()

        self._account_usage(data)

        message = self._chat_message(data)
        finish_reason = _finish_reason(data)
        call = _pick_tool_call(message.get("tool_calls"), tools)
        if call is not None:
            try:
                return _tool_arguments(call["function"].get("arguments"), tools)
            except ToolCallFormatError as exc:
                # Arguments the repair pass could not salvage. When the reply
                # was cut off, more room — not a hotter sample — is the fix.
                exc.truncated = finish_reason == "length"
                raise

        # Some models answer with content instead of calling the tool. That is
        # fine when the content is the JSON object we asked for; when it is
        # prose we raise, so the caller re-rolls with a correction rather than
        # sending unparsable text downstream.
        content = message.get("content") or ""
        as_json = _as_json_object(content) if content else None
        if as_json is not None:
            logger.warning("Model returned content instead of a tool call; parsed it as JSON")
            return as_json

        raise ToolCallFormatError(
            "bad_tool_arguments",
            tool=_tool_name(tools),
            preview=_preview(content)
            if content.strip()
            else _empty_reply_detail(finish_reason, reasoning=_carries_reasoning(message)),
            truncated=finish_reason == "length",
        )

    async def _call_llm_json_schema(
        self,
        model: str,
        messages: list[dict[str, str]],
        tools: list[dict],
        schema: dict,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> str:
        """Ask for the tool's arguments as the reply, constrained to ``schema``.

        ``response_format: json_schema`` with ``strict: true`` — where the
        endpoint enforces it, decoding is held to the schema and the reply
        cannot be anything but the object. A 400 the endpoint answers with is
        raised as-is; the caller decides whether it was about the format.
        """
        api_model = _strip_model_prefix(model, self.profile)
        body: dict = {
            "model": api_model,
            "messages": messages,
            "response_format": {
                "type": "json_schema",
                "json_schema": {"name": _tool_name(tools), "strict": True, "schema": schema},
            },
        }
        _set_budget(body, "max_tokens", None, self._structured_budget(model, max_tokens))
        self._temperature(body, temperature)
        self._apply_reasoning(body)

        async with httpx.AsyncClient(timeout=self._timeout()) as client:
            resp = await self._post(client, body)
            if (
                resp.status_code == 400
                and _asked_for_reasoning(body)
                and "reasoning" in resp.text.lower()
            ):
                logger.info("Model %s rejected reasoning effort; retrying without it", api_model)
                self._no_reasoning.add(api_model)
                _drop_reasoning(body)
                self._temperature(body, temperature)
                resp = await self._post(client, body)
            if self._refused_temperature(resp, body):
                resp = await self._post(client, body)
            self._handle_error(resp)
            data = resp.json()

        self._account_usage(data)
        message = self._chat_message(data)
        finish_reason = _finish_reason(data)
        return self._schema_reply(
            model,
            message.get("content") or "",
            tools,
            truncated=finish_reason == "length",
            empty_detail=_empty_reply_detail(finish_reason, reasoning=_carries_reasoning(message)),
        )

    async def _call_llm_agentic(
        self,
        model: str,
        messages: list,
        tools: list[dict],
        temperature: float | None = None,
    ) -> dict:
        """Make a tool-using LLM call without forcing a specific tool.

        Returns the full assistant message (with ``tool_calls`` and ``content``)
        so the caller can dispatch the calls and continue the conversation.
        """
        if not tools:
            raise LLMError("no_tools")
        api_model = _strip_model_prefix(model, self.profile)
        body: dict = {
            "model": api_model,
            "messages": self._wire_messages(api_model, messages),
            "tools": tools,
            "tool_choice": "auto",
        }
        _set_budget(body, "max_tokens", None, self._max_tokens_for(model))
        self._temperature(body, temperature)
        self._apply_reasoning(body)

        async with httpx.AsyncClient(timeout=self._timeout()) as client:
            resp = await self._post(client, body)
            if resp.status_code == 400 and "reasoning_content" in resp.text:
                logger.info(
                    "Model %s refused reasoning sent back in a tool loop; dropping it", api_model
                )
                self._no_echo_reasoning.add(api_model)
                body["messages"] = self._wire_messages(api_model, messages)
                resp = await self._post(client, body)
            if self._refused_temperature(resp, body):
                resp = await self._post(client, body)
            self._handle_error(resp)
            data = resp.json()

        self._account_usage(data)
        message = self._chat_message(data)
        content = message.get("content")
        if not message.get("tool_calls") and not (isinstance(content, str) and content.strip()):
            # Neither a call nor an answer: say why, where the payload still is.
            logger.warning(
                "Model %s returned an agentic turn with no tool call and an %s",
                model,
                _empty_reply_detail(_finish_reason(data), reasoning=_carries_reasoning(message)),
            )
        return message


_REASONING_KEYS = ("reasoning_content", "reasoning")


def _without_reasoning(message: Any) -> Any:
    if not isinstance(message, dict) or not any(key in message for key in _REASONING_KEYS):
        return message
    return {key: value for key, value in message.items() if key not in _REASONING_KEYS}


class _StreamedResponse:
    """A streamed chat reply, assembled, with the part of httpx.Response callers use."""

    status_code = 200

    def __init__(self, data: dict, headers: Any) -> None:
        self._data = data
        self.headers = headers

    @property
    def text(self) -> str:
        return json.dumps(self._data)

    def json(self) -> dict:
        return self._data


async def _stream_chat(client: Any, url: str, headers: dict, body: dict, *, total: float) -> Any:
    """POST ``body`` with ``stream: true`` and assemble the events into one payload.

    The client's read timeout bounds the silence between two events; ``total``
    bounds the whole reply. Either running out raises an httpx timeout, which
    the retry policy already treats as one. A reply that is not an event
    stream (an error, or an endpoint that ignored ``stream``) is returned as
    the response it is.
    """
    streamed = {**body, "stream": True, "stream_options": {"include_usage": True}}
    try:
        async with asyncio.timeout(total):
            async with client.stream("POST", url, headers=headers, json=streamed) as resp:
                content_type = resp.headers.get("content-type", "")
                if resp.status_code != 200 or "text/event-stream" not in content_type:
                    await resp.aread()
                    return resp
                return _StreamedResponse(await _assemble_stream(resp.aiter_lines()), resp.headers)
    except TimeoutError as exc:
        raise httpx.ReadTimeout(f"streamed reply ran past {total:.0f}s") from exc


async def _assemble_stream(lines: Any) -> dict:
    """Chat-completions events folded back into a non-streamed payload."""
    content: list[str] = []
    reasoning: list[str] = []
    reasoning_key = "reasoning_content"
    calls: dict[int, dict] = {}
    finish_reason = None
    usage = None
    async for line in lines:
        if not line or not line.startswith("data:"):
            continue  # blank separators and ": keep-alive" comments
        payload = line[5:].strip()
        if payload == "[DONE]":
            break
        try:
            chunk = json.loads(payload)
        except json.JSONDecodeError:
            continue
        if not isinstance(chunk, dict):
            continue
        if chunk.get("error"):
            raise LLMError("api_error", status=200, body=json.dumps(chunk["error"])[:2000])
        if isinstance(chunk.get("usage"), dict):
            usage = chunk["usage"]
        for choice in chunk.get("choices") or []:
            if not isinstance(choice, dict):
                continue
            delta = choice.get("delta") or {}
            if isinstance(delta.get("content"), str):
                content.append(delta["content"])
            for key in _REASONING_KEYS:
                if isinstance(delta.get(key), str):
                    reasoning.append(delta[key])
                    reasoning_key = key
            for position, call in enumerate(delta.get("tool_calls") or []):
                if not isinstance(call, dict):
                    continue
                index = call.get("index")
                index = index if isinstance(index, int) else position
                slot = calls.setdefault(
                    index, {"id": "", "type": "function", "function": {"name": "", "arguments": ""}}
                )
                if call.get("id"):
                    slot["id"] = call["id"]
                function = call.get("function") or {}
                name = function.get("name")
                if isinstance(name, str) and name and not slot["function"]["name"].endswith(name):
                    slot["function"]["name"] += name
                arguments = function.get("arguments")
                if isinstance(arguments, str):
                    slot["function"]["arguments"] += arguments
            if choice.get("finish_reason"):
                finish_reason = choice["finish_reason"]
    message: dict = {"role": "assistant", "content": "".join(content) or None}
    if reasoning:
        message[reasoning_key] = "".join(reasoning)
    if calls:
        message["tool_calls"] = [calls[index] for index in sorted(calls)]
    data: dict = {"choices": [{"index": 0, "message": message, "finish_reason": finish_reason}]}
    if usage is not None:
        data["usage"] = usage
    return data
