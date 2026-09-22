"""OpenAI-compatible API provider with retry/fallback and tool calling support.

Per-provider quirks (attribution headers, model-prefix policy, reasoning
remapping) come from the profile registry in ``mira.llm.provider_profiles``, matched
to the configured ``base_url``. OpenRouter is the one profile with quirks; any
other OpenAI-compatible endpoint works off the portable default, no entry needed.
"""

from __future__ import annotations

import logging
from typing import ClassVar

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

        async with httpx.AsyncClient(timeout=self.config.request_timeout) as client:
            resp = await client.post(
                self._chat_url(),
                headers=self._build_headers(),
                json=body,
            )
            if self._refused_temperature(resp, body):
                resp = await client.post(self._chat_url(), headers=self._build_headers(), json=body)
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

        async with httpx.AsyncClient(timeout=self.config.request_timeout) as client:
            resp = await client.post(
                self._chat_url(),
                headers=self._build_headers(),
                json=body,
            )
            if (
                resp.status_code == 400
                and body["tool_choice"] != "auto"
                and "tool_choice" in resp.text.lower()
            ):
                # Forced choice unsupported — remember it and let the model pick.
                logger.info("Model %s rejected forced tool_choice; retrying with auto", api_model)
                self._no_forced_tool_choice.add(api_model)
                body["tool_choice"] = "auto"
                resp = await client.post(self._chat_url(), headers=self._build_headers(), json=body)
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
                resp = await client.post(self._chat_url(), headers=self._build_headers(), json=body)
            if self._refused_temperature(resp, body):
                resp = await client.post(self._chat_url(), headers=self._build_headers(), json=body)
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

        async with httpx.AsyncClient(timeout=self.config.request_timeout) as client:
            resp = await client.post(self._chat_url(), headers=self._build_headers(), json=body)
            if (
                resp.status_code == 400
                and _asked_for_reasoning(body)
                and "reasoning" in resp.text.lower()
            ):
                logger.info("Model %s rejected reasoning effort; retrying without it", api_model)
                self._no_reasoning.add(api_model)
                _drop_reasoning(body)
                self._temperature(body, temperature)
                resp = await client.post(self._chat_url(), headers=self._build_headers(), json=body)
            if self._refused_temperature(resp, body):
                resp = await client.post(self._chat_url(), headers=self._build_headers(), json=body)
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
        body: dict = {
            "model": _strip_model_prefix(model, self.profile),
            "messages": messages,
            "tools": tools,
            "tool_choice": "auto",
        }
        _set_budget(body, "max_tokens", None, self._max_tokens_for(model))
        self._temperature(body, temperature)
        self._apply_reasoning(body)

        async with httpx.AsyncClient(timeout=self.config.request_timeout) as client:
            resp = await client.post(
                self._chat_url(),
                headers=self._build_headers(),
                json=body,
            )
            if self._refused_temperature(resp, body):
                resp = await client.post(self._chat_url(), headers=self._build_headers(), json=body)
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
