"""Structured output: strict schemas, validation that talks back, and the
json_schema → tool-calling ladder on every provider."""

from __future__ import annotations

import json
import os
from typing import Literal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pydantic import BaseModel, Field

from mira.config import LLMConfig
from mira.exceptions import LLMError, ToolCallFormatError
from mira.llm import structured
from mira.llm.provider import LLMProvider
from mira.llm.responses import ResponsesProvider
from mira.llm.tool_schemas import SUBMIT_REVIEW_TOOL

os.environ.setdefault("OPENROUTER_API_KEY", "test-key-for-unit-tests")


class _Finding(BaseModel):
    path: str
    line: int
    severity: Literal["blocker", "warning"]
    note: str | None = None


class _Findings(BaseModel):
    """Submit the findings."""

    findings: list[_Finding] = Field(default_factory=list)
    summary: str = ""


_TOOL = structured.tool_for(_Findings, "submit_findings")
_MESSAGES = [{"role": "user", "content": "review this"}]


# ── Schema conversion ──────────────────────────────────────────────


class TestSchemaFor:
    def test_inlines_nested_models_and_drops_annotations(self):
        schema = structured.schema_for(_Findings)
        assert "$defs" not in json.dumps(schema)
        assert "title" not in schema
        item = schema["properties"]["findings"]["items"]
        assert item["properties"]["severity"]["enum"] == ["blocker", "warning"]
        assert "default" not in json.dumps(schema)

    def test_optional_becomes_a_type_list(self):
        item = structured.schema_for(_Findings)["properties"]["findings"]["items"]
        assert item["properties"]["note"]["type"] == ["string", "null"]

    def test_tool_takes_the_docstring_as_description(self):
        assert _TOOL["function"]["description"] == "Submit the findings."
        assert _TOOL["function"]["name"] == "submit_findings"


class TestStrictSchema:
    def test_closes_every_object_and_requires_every_property(self):
        strict = structured.strict_schema(SUBMIT_REVIEW_TOOL["function"]["parameters"])
        assert strict["additionalProperties"] is False
        assert set(strict["required"]) == set(strict["properties"])
        comment = strict["properties"]["comments"]["items"]
        assert comment["additionalProperties"] is False
        assert set(comment["required"]) == set(comment["properties"])

    def test_optional_properties_become_nullable(self):
        strict = structured.strict_schema(SUBMIT_REVIEW_TOOL["function"]["parameters"])
        # metadata was optional: now required, and allowed to be null.
        assert strict["properties"]["metadata"]["type"] == ["object", "null"]
        # A required property stays non-null.
        assert strict["properties"]["summary"]["type"] == "string"

    def test_nullable_enum_admits_null(self):
        schema = {
            "type": "object",
            "properties": {"kind": {"type": "string", "enum": ["a", "b"]}},
        }
        kind = structured.strict_schema(schema)["properties"]["kind"]
        assert kind["type"] == ["string", "null"]
        assert kind["enum"] == ["a", "b", None]

    def test_value_constraints_move_into_the_description(self):
        strict = structured.strict_schema(SUBMIT_REVIEW_TOOL["function"]["parameters"])
        confidence = strict["properties"]["comments"]["items"]["properties"]["confidence"]
        assert "minimum" not in confidence and "maximum" not in confidence
        assert ">= 0.0" in confidence["description"]
        assert "<= 1.0" in confidence["description"]

    def test_a_property_named_title_survives(self):
        strict = structured.strict_schema(SUBMIT_REVIEW_TOOL["function"]["parameters"])
        assert "title" in strict["properties"]["comments"]["items"]["properties"]

    @pytest.mark.parametrize(
        "schema",
        [
            {},
            {"type": "object"},
            {"type": "object", "additionalProperties": {"type": "string"}},
            {"type": "object", "properties": {"x": {"oneOf": [{"type": "string"}]}}},
        ],
    )
    def test_refuses_what_strict_mode_cannot_express(self, schema):
        with pytest.raises(structured.UnstrictableSchema):
            structured.strict_schema(schema)


class TestNormalize:
    _SCHEMA = SUBMIT_REVIEW_TOOL["function"]["parameters"]

    def test_drops_nulls_strict_mode_added(self):
        data = {"comments": [], "summary": "ok", "metadata": None, "key_issues": None}
        assert structured.normalize(data, self._SCHEMA) == {"comments": [], "summary": "ok"}

    def test_keeps_nulls_the_schema_allows(self):
        data = {
            "comments": [{"path": "a", "line": 1, "end_line": None, "suggestion": None}],
            "summary": "ok",
        }
        assert structured.normalize(data, self._SCHEMA) == data

    def test_decodes_an_array_sent_as_a_string(self):
        data = {"comments": '[{"path": "a", "line": 1}]', "summary": "ok"}
        out = structured.normalize(data, self._SCHEMA)
        assert out["comments"] == [{"path": "a", "line": 1}]

    def test_a_stringified_array_gets_the_lenient_repairs(self):
        data = {"comments": '[{"path": "C:\\users\\a.py", "line": 1}]', "summary": "ok"}
        out = structured.normalize(data, self._SCHEMA)
        assert out["comments"] == [{"path": "C:\\users\\a.py", "line": 1}]

    def test_leaves_a_string_field_holding_json_alone(self):
        data = {"comments": [], "summary": '["not", "a", "list"]'}
        assert structured.normalize(data, self._SCHEMA) == data


class TestValidator:
    def test_names_the_fields_at_fault(self):
        validate = structured.validator_for(_Findings, "submit_findings")
        bad = json.dumps({"findings": [{"path": "a", "line": "x", "severity": "high"}]})
        with pytest.raises(ToolCallFormatError) as err:
            validate(bad)
        assert err.value.code == "invalid_structured_output"
        assert "findings.0.line" in err.value.safe_message
        assert "findings.0.severity" in err.value.safe_message

    def test_accepts_a_fitting_object(self):
        validate = structured.validator_for(_Findings, "submit_findings")
        validate(json.dumps({"findings": [{"path": "a", "line": 1, "severity": "warning"}]}))


# ── Chat Completions provider ──────────────────────────────────────


def _resp(data: dict, status: int = 200) -> MagicMock:
    resp = MagicMock()
    resp.status_code = status
    resp.json.return_value = data
    resp.text = json.dumps(data)
    return resp


def _chat_content(content: str, finish_reason: str = "stop") -> MagicMock:
    return _resp({"choices": [{"message": {"content": content}, "finish_reason": finish_reason}]})


def _chat_tool_call(name: str, arguments: dict) -> MagicMock:
    return _resp(
        {
            "choices": [
                {
                    "message": {
                        "content": None,
                        "tool_calls": [
                            {"function": {"name": name, "arguments": json.dumps(arguments)}}
                        ],
                    }
                }
            ]
        }
    )


def _client(responses: list) -> AsyncMock:
    client = AsyncMock()
    client.post = AsyncMock(side_effect=responses)
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=False)
    return client


_GOOD = {"findings": [{"path": "a.py", "line": 3, "severity": "warning"}], "summary": "one"}


class TestChatJsonSchema:
    async def _run(self, provider, responses, call):
        with patch("mira.llm.provider.httpx.AsyncClient") as cls:
            cls.return_value = _client(responses)
            result = await call()
            return result, [c.kwargs["json"] for c in cls.return_value.post.call_args_list]

    @pytest.mark.asyncio
    async def test_asks_for_strict_json_schema_first(self):
        provider = LLMProvider(LLMConfig(model="m"))
        result, bodies = await self._run(
            provider,
            [_chat_content(json.dumps(_GOOD))],
            lambda: provider.generate_object(_MESSAGES, _Findings, name="submit_findings"),
        )
        assert isinstance(result, _Findings)
        assert result.findings[0].line == 3
        assert len(bodies) == 1
        body = bodies[0]
        fmt = body["response_format"]
        assert fmt["type"] == "json_schema"
        assert fmt["json_schema"]["strict"] is True
        assert fmt["json_schema"]["name"] == "submit_findings"
        assert fmt["json_schema"]["schema"]["additionalProperties"] is False
        assert "tools" not in body
        # The prompt says what the format holds it to.
        assert "submit_findings" in body["messages"][-1]["content"]

    @pytest.mark.asyncio
    async def test_strict_nulls_are_dropped_before_the_parser_sees_them(self):
        provider = LLMProvider(LLMConfig(model="m"))
        reply = {"comments": [], "summary": "ok", "key_issues": None, "metadata": None}
        result, _ = await self._run(
            provider,
            [_chat_content(json.dumps(reply))],
            lambda: provider.complete_with_tools(_MESSAGES, [SUBMIT_REVIEW_TOOL]),
        )
        assert json.loads(result) == {"comments": [], "summary": "ok"}

    @pytest.mark.asyncio
    async def test_a_refusal_falls_back_to_tools_and_is_remembered(self):
        provider = LLMProvider(LLMConfig(model="m"))
        refused = _resp({"error": {"message": "response_format json_schema is not supported"}}, 400)
        result, bodies = await self._run(
            provider,
            [refused, _chat_tool_call("submit_findings", _GOOD)],
            lambda: provider.generate_object(_MESSAGES, _Findings, name="submit_findings"),
        )
        assert result.summary == "one"
        assert "response_format" in bodies[0]
        assert "tools" in bodies[1] and "response_format" not in bodies[1]

        # Remembered: the next call goes straight to tool calling.
        _, later = await self._run(
            provider,
            [_chat_tool_call("submit_findings", _GOOD)],
            lambda: provider.generate_object(_MESSAGES, _Findings, name="submit_findings"),
        )
        assert len(later) == 1 and "tools" in later[0]

    @pytest.mark.asyncio
    async def test_an_unrelated_400_is_not_remembered(self):
        provider = LLMProvider(LLMConfig(model="m"))
        overflow = _resp({"error": {"message": "context length exceeded"}}, 400)
        _, bodies = await self._run(
            provider,
            [overflow, _chat_tool_call("submit_findings", _GOOD)],
            lambda: provider.generate_object(_MESSAGES, _Findings, name="submit_findings"),
        )
        assert "tools" in bodies[1]
        assert provider._takes_json_schema("m")

    @pytest.mark.asyncio
    async def test_a_complaint_about_this_schema_is_not_remembered(self):
        # About this tool's schema, not the format: the next tool may be fine.
        provider = LLMProvider(LLMConfig(model="m"))
        invalid = _resp({"error": {"message": "Invalid schema: 'minimum' is not permitted"}}, 400)
        _, bodies = await self._run(
            provider,
            [invalid, _chat_tool_call("submit_findings", _GOOD)],
            lambda: provider.generate_object(_MESSAGES, _Findings, name="submit_findings"),
        )
        assert "tools" in bodies[1]
        assert provider._takes_json_schema("m")

    @pytest.mark.asyncio
    async def test_prose_under_json_schema_moves_the_model_to_tools(self):
        # The endpoint took the parameter and ignored it.
        provider = LLMProvider(LLMConfig(model="m"))
        result, bodies = await self._run(
            provider,
            [
                _chat_content("Sure! Here are my findings: the code looks fine."),
                _chat_tool_call("submit_findings", _GOOD),
            ],
            lambda: provider.generate_object(_MESSAGES, _Findings, name="submit_findings"),
        )
        assert result.summary == "one"
        assert "response_format" in bodies[0]
        assert "tools" in bodies[1]
        assert not provider._takes_json_schema("m")

    @pytest.mark.asyncio
    async def test_a_misfit_is_rerolled_with_the_fields_named(self):
        provider = LLMProvider(LLMConfig(model="m"))
        misfit = {"findings": [{"path": "a.py", "line": "three", "severity": "high"}]}
        result, bodies = await self._run(
            provider,
            [_chat_content(json.dumps(misfit)), _chat_content(json.dumps(_GOOD))],
            lambda: provider.generate_object(_MESSAGES, _Findings, name="submit_findings"),
        )
        assert result.findings[0].severity == "warning"
        correction = bodies[1]["messages"][-2]["content"]
        assert "did not match the schema" in correction
        assert "findings.0.line" in correction
        assert "findings.0.severity" in correction

    @pytest.mark.asyncio
    async def test_gives_up_after_the_rerolls_when_nothing_fits(self):
        provider = LLMProvider(LLMConfig(model="m", tool_call_retries=1, json_mode_fallback=False))
        misfit = _chat_content(json.dumps({"findings": "none"}))
        with pytest.raises(LLMError):
            await self._run(
                provider,
                [misfit, misfit],
                lambda: provider.generate_object(_MESSAGES, _Findings, name="submit_findings"),
            )

    @pytest.mark.asyncio
    async def test_switched_off_goes_straight_to_tools(self):
        provider = LLMProvider(LLMConfig(model="m", json_schema_mode=False))
        _, bodies = await self._run(
            provider,
            [_chat_tool_call("submit_findings", _GOOD)],
            lambda: provider.generate_object(_MESSAGES, _Findings, name="submit_findings"),
        )
        assert len(bodies) == 1 and "tools" in bodies[0]

    @pytest.mark.asyncio
    async def test_max_tokens_is_a_floor(self):
        provider = LLMProvider(LLMConfig(model="m", max_tokens=4096))
        _, bodies = await self._run(
            provider,
            [_chat_content(json.dumps(_GOOD))],
            lambda: provider.generate_object(
                _MESSAGES, _Findings, name="submit_findings", max_tokens=20000
            ),
        )
        assert bodies[0]["max_tokens"] == 20000

    @pytest.mark.asyncio
    async def test_truncated_reply_raises_the_budget(self):
        provider = LLMProvider(LLMConfig(model="m", max_tokens=1000))
        _, bodies = await self._run(
            provider,
            [
                _chat_content('{"findings": [{"path": "a.py", "li', finish_reason="length"),
                _chat_content(json.dumps(_GOOD)),
            ],
            lambda: provider.generate_object(_MESSAGES, _Findings, name="submit_findings"),
        )
        assert bodies[1]["max_tokens"] == 2000
        # A cut-off reply is not evidence the format was ignored.
        assert provider._takes_json_schema("m")


# ── Responses provider ─────────────────────────────────────────────


class TestResponsesJsonSchema:
    @pytest.mark.asyncio
    async def test_uses_text_format(self):
        provider = ResponsesProvider(
            LLMConfig(model="gpt-4o", base_url="https://api.openai.com/v1", api_style="responses")
        )
        reply = _resp(
            {
                "status": "completed",
                "output": [
                    {
                        "type": "message",
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": json.dumps(_GOOD)}],
                    }
                ],
            }
        )
        with patch("mira.llm.responses.httpx.AsyncClient") as cls:
            cls.return_value = _client([reply])
            result = await provider.generate_object(_MESSAGES, _Findings, name="submit_findings")
            body = cls.return_value.post.call_args.kwargs["json"]

        assert result.findings[0].path == "a.py"
        fmt = body["text"]["format"]
        assert fmt["type"] == "json_schema"
        assert fmt["strict"] is True
        assert fmt["name"] == "submit_findings"
        assert "tools" not in body


# ── Bedrock ────────────────────────────────────────────────────────


class TestBedrockGenerateObject:
    @pytest.mark.asyncio
    async def test_a_misfit_is_rerolled_with_the_fields_named(self):
        pytest.importorskip("boto3")
        from mira.llm.bedrock import BedrockProvider

        def tool_use(data: dict) -> dict:
            return {
                "output": {
                    "message": {
                        "role": "assistant",
                        "content": [
                            {
                                "toolUse": {
                                    "toolUseId": "c1",
                                    "name": "submit_findings",
                                    "input": data,
                                }
                            }
                        ],
                    }
                },
                "usage": {"inputTokens": 1, "outputTokens": 1},
                "stopReason": "tool_use",
            }

        client = MagicMock()
        client.converse.side_effect = [
            tool_use({"findings": [{"path": "a.py", "line": 1, "severity": "high"}]}),
            tool_use(_GOOD),
        ]
        with patch("boto3.Session") as session_cls:
            session_cls.return_value.client.return_value = client
            provider = BedrockProvider(
                LLMConfig(provider="bedrock", model="anthropic.claude", region="us-east-1")
            )
            result = await provider.generate_object(_MESSAGES, _Findings, name="submit_findings")

        assert result.summary == "one"
        second = client.converse.call_args_list[1].kwargs["messages"]
        correction = second[-1]["content"][0]["text"]
        assert "findings.0.severity" in correction


# ── Fallback chain ─────────────────────────────────────────────────


class TestChainGenerateObject:
    @pytest.mark.asyncio
    async def test_walks_to_the_next_provider(self):
        from mira.llm.chain import FallbackChain

        first = MagicMock()
        first.generate_object = AsyncMock(side_effect=LLMError("no_tools"))
        second = MagicMock()
        second.generate_object = AsyncMock(return_value=_Findings(summary="from second"))
        chain = FallbackChain([first, second])

        result = await chain.generate_object(
            _MESSAGES, _Findings, name="submit_findings", max_tokens=123
        )

        assert result.summary == "from second"
        assert second.generate_object.call_args.kwargs["max_tokens"] == 123
