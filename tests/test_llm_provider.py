"""Tests for LLM provider wrapper (OpenRouter via httpx)."""

from __future__ import annotations

import json
import os
from datetime import UTC
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from mira.config import LLMConfig
from mira.exceptions import LLMError, NonRetriableLLMError, ToolCallFormatError
from mira.llm.provider import LLMProvider

# Set a dummy API key for tests so _get_api_key() doesn't fail
os.environ.setdefault("OPENROUTER_API_KEY", "test-key-for-unit-tests")


def _make_response_json(content: str = "response", usage: dict | None = None) -> dict:
    """Create a mock OpenRouter API response dict."""
    resp = {
        "choices": [
            {
                "message": {
                    "content": content,
                },
            }
        ],
    }
    if usage is not None:
        resp["usage"] = usage
    return resp


def _make_tool_response_json(arguments: str, usage: dict | None = None) -> dict:
    """Create a mock OpenRouter API response with a tool call."""
    resp = {
        "choices": [
            {
                "message": {
                    "content": None,
                    "tool_calls": [
                        {
                            "function": {
                                "name": "submit_review",
                                "arguments": arguments,
                            }
                        }
                    ],
                },
            }
        ],
    }
    if usage is not None:
        resp["usage"] = usage
    return resp


def _retry_state(exception: BaseException, attempt: int):
    """Minimal stand-in for tenacity's RetryCallState: attempt number + outcome."""
    outcome = MagicMock()
    outcome.exception.return_value = exception
    state = MagicMock()
    state.attempt_number = attempt
    state.outcome = outcome
    return state


def _mock_httpx_response(data: dict, status_code: int = 200):
    """Create a mock httpx.Response."""
    resp = MagicMock()
    resp.status_code = status_code
    resp.json.return_value = data
    resp.text = json.dumps(data)
    return resp


class TestLLMProviderInit:
    def test_default_config(self):
        config = LLMConfig()
        provider = LLMProvider(config)
        assert provider.config.model == "anthropic/claude-sonnet-4-6"
        assert provider.total_prompt_tokens == 0
        assert provider.total_completion_tokens == 0


class TestComplete:
    @pytest.mark.asyncio
    async def test_successful_completion(self):
        config = LLMConfig(model="test-model")
        provider = LLMProvider(config)

        mock_resp = _mock_httpx_response(
            _make_response_json("hello", {"prompt_tokens": 10, "completion_tokens": 5})
        )

        with patch("mira.llm.provider.httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.post = AsyncMock(return_value=mock_resp)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_client

            result = await provider.complete([{"role": "user", "content": "hi"}])

        assert result == "hello"
        assert provider.total_prompt_tokens == 10
        assert provider.total_completion_tokens == 5

    @pytest.mark.asyncio
    async def test_json_mode_passes_response_format(self):
        config = LLMConfig(model="test-model")
        provider = LLMProvider(config)

        mock_resp = _mock_httpx_response(_make_response_json("{}"))

        with patch("mira.llm.provider.httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.post = AsyncMock(return_value=mock_resp)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_client

            await provider.complete([{"role": "user", "content": "hi"}], json_mode=True)

            call_kwargs = mock_client.post.call_args
            body = call_kwargs.kwargs.get("json") or call_kwargs[1].get("json")
            assert body["response_format"] == {"type": "json_object"}

    @pytest.mark.asyncio
    async def test_non_json_mode_no_response_format(self):
        config = LLMConfig(model="test-model")
        provider = LLMProvider(config)

        mock_resp = _mock_httpx_response(_make_response_json("text"))

        with patch("mira.llm.provider.httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.post = AsyncMock(return_value=mock_resp)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_client

            await provider.complete([{"role": "user", "content": "hi"}], json_mode=False)

            call_kwargs = mock_client.post.call_args
            body = call_kwargs.kwargs.get("json") or call_kwargs[1].get("json")
            assert "response_format" not in body

    @pytest.mark.asyncio
    async def test_reasoning_effort_sets_reasoning_and_drops_temperature(self):
        config = LLMConfig(model="test-model", reasoning_effort="high")
        provider = LLMProvider(config)

        mock_resp = _mock_httpx_response(_make_response_json("ok"))

        with patch("mira.llm.provider.httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.post = AsyncMock(return_value=mock_resp)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_client

            await provider.complete([{"role": "user", "content": "hi"}])

            call_kwargs = mock_client.post.call_args
            body = call_kwargs.kwargs.get("json") or call_kwargs[1].get("json")
            assert body["reasoning"] == {"effort": "high"}
            # Anthropic rejects a custom temperature while thinking — we drop it.
            assert "temperature" not in body

    @pytest.mark.asyncio
    async def test_max_effort_maps_to_xhigh_on_openrouter(self):
        # OpenRouter rejects "max"; "xhigh" is its equivalent top level.
        config = LLMConfig(model="test-model", reasoning_effort="max")
        provider = LLMProvider(config)
        mock_resp = _mock_httpx_response(_make_response_json("ok"))

        with patch("mira.llm.provider.httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.post = AsyncMock(return_value=mock_resp)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_client

            await provider.complete([{"role": "user", "content": "hi"}])
            body = mock_client.post.call_args.kwargs["json"]
            assert body["reasoning"] == {"effort": "xhigh"}

    @pytest.mark.asyncio
    async def test_max_effort_passes_through_on_non_openrouter(self):
        # DeepSeek's native API accepts "max" verbatim.
        config = LLMConfig(
            model="deepseek-reasoner",
            reasoning_effort="max",
            base_url="https://api.deepseek.com/v1",
        )
        provider = LLMProvider(config)
        mock_resp = _mock_httpx_response(_make_response_json("ok"))

        with patch("mira.llm.provider.httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.post = AsyncMock(return_value=mock_resp)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_client

            await provider.complete([{"role": "user", "content": "hi"}])
            body = mock_client.post.call_args.kwargs["json"]
            assert body["reasoning"] == {"effort": "max"}

    @pytest.mark.asyncio
    async def test_reasoning_off_leaves_body_unchanged(self):
        for effort in (None, "off"):
            config = LLMConfig(model="test-model", reasoning_effort=effort)
            provider = LLMProvider(config)

            mock_resp = _mock_httpx_response(_make_response_json("ok"))

            with patch("mira.llm.provider.httpx.AsyncClient") as mock_client_cls:
                mock_client = AsyncMock()
                mock_client.post = AsyncMock(return_value=mock_resp)
                mock_client.__aenter__ = AsyncMock(return_value=mock_client)
                mock_client.__aexit__ = AsyncMock(return_value=False)
                mock_client_cls.return_value = mock_client

                await provider.complete([{"role": "user", "content": "hi"}])

                call_kwargs = mock_client.post.call_args
                body = call_kwargs.kwargs.get("json") or call_kwargs[1].get("json")
                assert "reasoning" not in body
                assert "temperature" in body

    @pytest.mark.asyncio
    async def test_no_usage_tracked_when_missing(self):
        config = LLMConfig(model="test-model")
        provider = LLMProvider(config)

        mock_resp = _mock_httpx_response(_make_response_json("ok"))

        with patch("mira.llm.provider.httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.post = AsyncMock(return_value=mock_resp)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_client

            await provider.complete([{"role": "user", "content": "hi"}])

        assert provider.total_prompt_tokens == 0
        assert provider.total_completion_tokens == 0

    @pytest.mark.asyncio
    async def test_primary_failure_with_fallback(self):
        config = LLMConfig(model="primary", fallback_model="fallback")
        provider = LLMProvider(config)

        call_count = 0

        async def _side_effect(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            body = kwargs.get("json", {})
            if body.get("model") == "primary":
                return _mock_httpx_response({}, status_code=500)
            return _mock_httpx_response(_make_response_json("fallback ok"))

        with patch("mira.llm.provider.httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.post = AsyncMock(side_effect=_side_effect)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_client

            result = await provider.complete([{"role": "user", "content": "hi"}])

        assert result == "fallback ok"

    @pytest.mark.asyncio
    async def test_primary_failure_no_fallback_raises(self):
        config = LLMConfig(model="primary", fallback_model=None)
        provider = LLMProvider(config)

        mock_resp = _mock_httpx_response({}, status_code=500)

        with patch("mira.llm.provider.httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.post = AsyncMock(return_value=mock_resp)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_client

            with pytest.raises(LLMError, match="LLM completion failed"):
                await provider.complete([{"role": "user", "content": "hi"}])

    @pytest.mark.asyncio
    async def test_both_models_fail_raises(self):
        config = LLMConfig(model="primary", fallback_model="fallback")
        provider = LLMProvider(config)

        mock_resp = _mock_httpx_response({}, status_code=500)

        with patch("mira.llm.provider.httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.post = AsyncMock(return_value=mock_resp)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_client

            with pytest.raises(LLMError, match="Both primary.*and fallback.*failed"):
                await provider.complete([{"role": "user", "content": "hi"}])

    @pytest.mark.asyncio
    async def test_empty_content_returns_empty_string(self):
        config = LLMConfig(model="test-model")
        provider = LLMProvider(config)

        resp_data = {"choices": [{"message": {"content": None}}]}
        mock_resp = _mock_httpx_response(resp_data)

        with patch("mira.llm.provider.httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.post = AsyncMock(return_value=mock_resp)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_client

            result = await provider.complete([{"role": "user", "content": "hi"}])

        assert result == ""

    @pytest.mark.asyncio
    async def test_config_timeout_passed_to_httpx(self):
        config = LLMConfig(model="test-model", request_timeout=300)
        provider = LLMProvider(config)

        mock_resp = _mock_httpx_response(_make_response_json("ok"))

        with patch("mira.llm.provider.httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.post = AsyncMock(return_value=mock_resp)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_client

            await provider.complete([{"role": "user", "content": "hi"}])

            assert mock_client_cls.call_args.kwargs["timeout"] == 300

    @pytest.mark.asyncio
    async def test_config_retries_count(self):
        config = LLMConfig(
            model="test-model",
            max_retries=5,
            retry_min_wait=0,
            retry_max_wait=0,
        )
        provider = LLMProvider(config)

        mock_resp = _mock_httpx_response({}, status_code=500)

        with patch("mira.llm.provider.httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.post = AsyncMock(return_value=mock_resp)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_client

            with pytest.raises(LLMError):
                await provider.complete([{"role": "user", "content": "hi"}])

            assert mock_client.post.call_count == 5

    @pytest.mark.asyncio
    async def test_4xx_raises_non_retriable_error(self):
        """4xx errors (except 429) raise NonRetriableLLMError and skip retry."""
        config = LLMConfig(
            model="test-model",
            max_retries=5,
            retry_min_wait=0,
            retry_max_wait=0,
        )
        provider = LLMProvider(config)

        mock_resp = _mock_httpx_response({}, status_code=400)

        with patch("mira.llm.provider.httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.post = AsyncMock(return_value=mock_resp)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_client

            with pytest.raises(NonRetriableLLMError):
                await provider.complete([{"role": "user", "content": "hi"}])

            # Non-retriable: only 1 attempt, no retries
            assert mock_client.post.call_count == 1

    @pytest.mark.asyncio
    async def test_429_raises_retriable_error(self):
        """429 (rate limit) raises LLMError and retries."""
        config = LLMConfig(
            model="test-model",
            max_retries=3,
            retry_min_wait=0,
            retry_max_wait=0,
        )
        provider = LLMProvider(config)

        mock_resp = _mock_httpx_response({}, status_code=429)

        with patch("mira.llm.provider.httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.post = AsyncMock(return_value=mock_resp)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_client

            with pytest.raises(LLMError):
                await provider.complete([{"role": "user", "content": "hi"}])

            # Retriable: 3 attempts
            assert mock_client.post.call_count == 3


class TestCountTokens:
    def test_heuristic_count(self):
        config = LLMConfig(model="test-model")
        provider = LLMProvider(config)
        count = provider.count_tokens("hello world test")
        # ~4 chars per token heuristic
        assert count == len("hello world test") // 4


class TestUsageProperty:
    def test_usage_aggregation(self):
        config = LLMConfig(model="test-model")
        provider = LLMProvider(config)
        provider.total_prompt_tokens = 100
        provider.total_completion_tokens = 50

        usage = provider.usage
        assert usage["prompt_tokens"] == 100
        assert usage["completion_tokens"] == 50
        assert usage["total_tokens"] == 150


class TestStripModelPrefix:
    """Tests for _strip_model_prefix — provider prefix stripping for non-OpenRouter endpoints."""

    def test_openrouter_url_strips_openrouter_prefix(self):
        from mira.llm.provider import _strip_model_prefix

        result = _strip_model_prefix("openrouter/deepseek-r1", "https://openrouter.ai/api/v1")
        assert result == "deepseek-r1"

    def test_openrouter_url_preserves_non_openrouter_prefix(self):
        from mira.llm.provider import _strip_model_prefix

        result = _strip_model_prefix("anthropic/claude-sonnet-4-6", "https://openrouter.ai/api/v1")
        assert result == "anthropic/claude-sonnet-4-6"

    def test_openrouter_url_preserves_model_without_prefix(self):
        from mira.llm.provider import _strip_model_prefix

        result = _strip_model_prefix("gpt-4o", "https://openrouter.ai/api/v1")
        assert result == "gpt-4o"

    def test_non_openrouter_url_strips_provider_prefix(self):
        from mira.llm.provider import _strip_model_prefix

        result = _strip_model_prefix("minimax/MiniMax-M2.7", "https://api.minimax.io/v1")
        assert result == "MiniMax-M2.7"

    def test_non_openrouter_url_strips_anthropic_prefix(self):
        from mira.llm.provider import _strip_model_prefix

        result = _strip_model_prefix("anthropic/claude-sonnet-4-6", "https://api.anthropic.com/v1")
        assert result == "claude-sonnet-4-6"

    def test_non_openrouter_url_preserves_model_without_prefix(self):
        from mira.llm.provider import _strip_model_prefix

        result = _strip_model_prefix("gpt-4o", "https://api.openai.com/v1")
        assert result == "gpt-4o"

    def test_non_openrouter_url_local_ollama(self):
        from mira.llm.provider import _strip_model_prefix

        result = _strip_model_prefix("llama3.1:latest", "http://localhost:11434/v1")
        assert result == "llama3.1:latest"


class TestProfileHeaders:
    """Provider-specific headers come from the matched profile, not hardcoding."""

    def test_openrouter_adds_ranking_headers(self):
        provider = LLMProvider(LLMConfig(model="m"))  # default base_url = openrouter
        headers = provider._build_headers()
        assert headers["HTTP-Referer"] == "https://github.com/miracodeai/mira"
        assert headers["X-Title"] == "Mira Code Reviewer"

    def test_other_endpoint_has_no_ranking_headers(self):
        provider = LLMProvider(LLMConfig(model="m", base_url="https://api.groq.com/openai/v1"))
        headers = provider._build_headers()
        assert "HTTP-Referer" not in headers
        assert "X-Title" not in headers
        assert headers["Authorization"].startswith("Bearer ")


class TestToolChoiceFallback:
    """#82: thinking models (deepseek) 400 on a forced tool_choice; retry
    with "auto" rather than failing the review."""

    _TOOL = {"type": "function", "function": {"name": "submit_review", "parameters": {}}}

    def _client(self, responses):
        mock_client = AsyncMock()
        mock_client.post = AsyncMock(side_effect=responses)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        return mock_client

    @pytest.mark.asyncio
    async def test_retries_with_auto_on_tool_choice_400(self):
        provider = LLMProvider(LLMConfig(model="deepseek/deepseek-v4-pro"))
        rejected = _mock_httpx_response(
            {"error": {"message": "thinking mode does not support this tool_choice"}},
            status_code=400,
        )
        ok = _mock_httpx_response(_make_tool_response_json('{"comments": []}'))

        with patch("mira.llm.provider.httpx.AsyncClient") as cls:
            cls.return_value = self._client([rejected, ok])
            result = await provider.complete_with_tools(
                [{"role": "user", "content": "hi"}], tools=[self._TOOL]
            )
            n_posts = len(cls.return_value.post.call_args_list)

        assert result == '{"comments": []}'
        assert n_posts == 2  # forced 400'd, then the auto retry
        assert "deepseek/deepseek-v4-pro" in provider._no_forced_tool_choice

    @pytest.mark.asyncio
    async def test_remembered_model_skips_forced_attempt(self):
        provider = LLMProvider(LLMConfig(model="deepseek/deepseek-v4-pro"))
        provider._no_forced_tool_choice.add("deepseek/deepseek-v4-pro")
        ok = _mock_httpx_response(_make_tool_response_json('{"comments": []}'))

        with patch("mira.llm.provider.httpx.AsyncClient") as cls:
            cls.return_value = self._client([ok])
            await provider.complete_with_tools(
                [{"role": "user", "content": "hi"}], tools=[self._TOOL]
            )
            posts = cls.return_value.post.call_args_list

        assert len(posts) == 1  # straight to auto, no wasted forced attempt
        assert posts[0].kwargs["json"]["tool_choice"] == "auto"

    @pytest.mark.asyncio
    async def test_unrelated_400_does_not_trigger_auto_fallback(self):
        # A non-tool_choice 400 should error out, not flip the model to auto.
        provider = LLMProvider(LLMConfig(model="anthropic/claude-sonnet-4-6"))
        err = _mock_httpx_response(
            {"error": {"message": "context length exceeded"}}, status_code=400
        )
        client = AsyncMock()
        client.post = AsyncMock(return_value=err)
        client.__aenter__ = AsyncMock(return_value=client)
        client.__aexit__ = AsyncMock(return_value=False)

        with (
            patch("mira.llm.provider.httpx.AsyncClient", return_value=client),
            pytest.raises(LLMError),
        ):
            await provider.complete_with_tools(
                [{"role": "user", "content": "hi"}], tools=[self._TOOL]
            )
        assert "anthropic/claude-sonnet-4-6" not in provider._no_forced_tool_choice


class TestReasoningFallback:
    """Thinking mode is opt-in and applied to whatever model is selected; a
    model/endpoint that rejects a reasoning effort must degrade to a normal
    review, not fail it."""

    _TOOL = {"type": "function", "function": {"name": "submit_review", "parameters": {}}}

    def _client(self, responses):
        mock_client = AsyncMock()
        mock_client.post = AsyncMock(side_effect=responses)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        return mock_client

    @pytest.mark.asyncio
    async def test_retries_without_reasoning_on_400(self):
        provider = LLMProvider(LLMConfig(model="some/model", reasoning_effort="high"))
        rejected = _mock_httpx_response(
            {"error": {"message": "reasoning_effort: Invalid option"}}, status_code=400
        )
        ok = _mock_httpx_response(_make_tool_response_json('{"comments": []}'))

        with patch("mira.llm.provider.httpx.AsyncClient") as cls:
            cls.return_value = self._client([rejected, ok])
            result = await provider.complete_with_tools(
                [{"role": "user", "content": "hi"}], tools=[self._TOOL]
            )
            posts = cls.return_value.post.call_args_list

        assert result == '{"comments": []}'
        assert len(posts) == 2  # reasoning 400'd, then retried without it
        assert "reasoning" not in posts[1].kwargs["json"]  # dropped on the retry
        assert "some/model" in provider._no_reasoning

    @pytest.mark.asyncio
    async def test_remembered_model_skips_reasoning(self):
        provider = LLMProvider(LLMConfig(model="some/model", reasoning_effort="high"))
        provider._no_reasoning.add("some/model")
        ok = _mock_httpx_response(_make_tool_response_json('{"comments": []}'))

        with patch("mira.llm.provider.httpx.AsyncClient") as cls:
            cls.return_value = self._client([ok])
            await provider.complete_with_tools(
                [{"role": "user", "content": "hi"}], tools=[self._TOOL]
            )
            posts = cls.return_value.post.call_args_list

        assert len(posts) == 1  # no wasted reasoning attempt
        assert "reasoning" not in posts[0].kwargs["json"]


class TestToolCallRecovery:
    """A model that flubs the tool call used to fail the whole review. Each
    recovery step here exists because a real model got it wrong that way."""

    _TOOL = {
        "type": "function",
        "function": {"name": "submit_review", "parameters": {"type": "object"}},
    }

    def _config(self, **kw) -> LLMConfig:
        base = {
            "model": "test-model",
            "max_retries": 2,
            "retry_min_wait": 0,
            "retry_max_wait": 0,
        }
        base.update(kw)
        return LLMConfig(**base)

    def _client(self, responses):
        client = AsyncMock()
        client.post = AsyncMock(side_effect=responses)
        client.__aenter__ = AsyncMock(return_value=client)
        client.__aexit__ = AsyncMock(return_value=False)
        return client

    async def _call(self, provider, responses):
        with patch("mira.llm.provider.httpx.AsyncClient") as cls:
            cls.return_value = self._client(responses)
            try:
                result = await provider.complete_with_tools(
                    [{"role": "user", "content": "review this"}], tools=[self._TOOL]
                )
            finally:
                self.posts = cls.return_value.post.call_args_list
        return result

    @pytest.mark.asyncio
    async def test_truncated_arguments_are_repaired(self):
        """Hitting the token cap mid-object is the single most common way a
        tool call arrives broken; the repair pass salvages it."""
        provider = LLMProvider(self._config())
        truncated = '{"comments": [{"path": "a.py", "line": 1, "body": "half a thought'

        result = await self._call(
            provider, [_mock_httpx_response(_make_tool_response_json(truncated))]
        )

        assert json.loads(result)["comments"][0]["path"] == "a.py"
        assert len(self.posts) == 1

    @pytest.mark.asyncio
    async def test_prose_instead_of_a_tool_call_is_rerolled_with_a_correction(self):
        provider = LLMProvider(self._config())
        prose = _mock_httpx_response(_make_response_json("Sure! Here is my review: looks fine."))
        good = _mock_httpx_response(_make_tool_response_json('{"comments": []}'))

        result = await self._call(provider, [prose, good])

        assert result == '{"comments": []}'
        assert len(self.posts) == 2
        correction = self.posts[1].kwargs["json"]["messages"][-1]
        assert correction["role"] == "user"
        assert "submit_review" in correction["content"]
        # An identical sample would reproduce the same mistake.
        assert (
            self.posts[1].kwargs["json"]["temperature"]
            > self.posts[0].kwargs["json"]["temperature"]
        )

    @pytest.mark.asyncio
    async def test_json_mode_rescues_a_model_that_never_calls_the_tool(self):
        provider = LLMProvider(self._config(tool_call_retries=1))
        prose = _mock_httpx_response(_make_response_json("I cannot use tools."))
        as_json = _mock_httpx_response(_make_response_json('{"comments": [], "summary": "ok"}'))

        result = await self._call(provider, [prose, prose, as_json])

        assert json.loads(result)["summary"] == "ok"
        assert len(self.posts) == 3
        assert "tools" not in self.posts[2].kwargs["json"]
        assert self.posts[2].kwargs["json"]["response_format"] == {"type": "json_object"}

    @pytest.mark.asyncio
    async def test_call_fails_when_no_recovery_path_lands(self):
        provider = LLMProvider(self._config(tool_call_retries=1, json_mode_fallback=False))
        prose = _mock_httpx_response(_make_response_json("nope"))

        with pytest.raises(LLMError, match="tool-call failed"):
            await self._call(provider, [prose, prose])

        assert len(self.posts) == 2  # one attempt, one re-roll, no JSON fallback

    @pytest.mark.asyncio
    async def test_the_requested_tool_wins_over_a_stray_extra_call(self):
        provider = LLMProvider(self._config())
        data = _make_tool_response_json('{"comments": []}')
        data["choices"][0]["message"]["tool_calls"].insert(
            0, {"function": {"name": "read_file", "arguments": '{"path": "a.py"}'}}
        )

        result = await self._call(provider, [_mock_httpx_response(data)])

        assert result == '{"comments": []}'

    @pytest.mark.asyncio
    async def test_a_200_carrying_an_error_body_is_retried(self):
        """Gateways answer 200 with an error object. Indexing straight into it
        raised KeyError, which skipped the retry and killed the review."""
        provider = LLMProvider(self._config())
        broken = _mock_httpx_response({"error": {"message": "upstream is down"}})
        good = _mock_httpx_response(_make_tool_response_json('{"comments": []}'))

        result = await self._call(provider, [broken, good])

        assert result == '{"comments": []}'
        assert len(self.posts) == 2


class TestRetryPacing:
    def test_a_server_disconnect_mid_response_is_retriable(self):
        from mira.llm.base import _retriable

        assert _retriable(httpx.RemoteProtocolError("server disconnected"))

    def test_a_4xx_is_not_retriable(self):
        from mira.llm.base import _retriable

        assert not _retriable(NonRetriableLLMError("api_error", status=400, body=""))

    def test_a_bad_tool_call_is_not_retried_at_the_transport_level(self):
        """It is re-rolled with a correction instead — resending the identical
        request just resamples the same mistake."""
        from mira.llm.base import _retriable

        assert not _retriable(ToolCallFormatError("bad_tool_arguments", tool="t", preview=""))

    def test_retry_after_beats_the_backoff_curve(self):
        from mira.llm.base import _make_wait

        wait = _make_wait(LLMConfig(retry_min_wait=1, retry_max_wait=30))
        error = LLMError("api_error", status=429, body="")
        error.retry_after = 7.0

        assert wait(_retry_state(error, attempt=1)) == 7.0

    def test_retry_after_is_clamped_to_the_configured_ceiling(self):
        from mira.llm.base import _make_wait

        wait = _make_wait(LLMConfig(retry_min_wait=1, retry_max_wait=30))
        error = LLMError("api_error", status=429, body="")
        error.retry_after = 3600.0

        assert wait(_retry_state(error, attempt=1)) == 30.0

    def test_concurrent_chunks_do_not_retry_in_lockstep(self):
        """Without jitter every in-flight chunk re-hits the rate limit at the
        same instant, and they all fail together again."""
        from mira.llm.base import _make_wait

        wait = _make_wait(LLMConfig(retry_min_wait=2, retry_max_wait=30))
        error = LLMError("api_error", status=429, body="")
        delays = {wait(_retry_state(error, attempt=1)) for _ in range(20)}

        assert len(delays) > 1
        assert all(2 <= d <= 4 for d in delays)


class TestBotchedCallsThatWouldLookClean:
    """Each of these used to end as a review that found nothing, which is the
    one wrong answer nobody double-checks."""

    _TOOL = {
        "type": "function",
        "function": {
            "name": "submit_review",
            "parameters": {"type": "object", "required": ["comments", "summary"]},
        },
    }

    def _provider(self, **kw) -> LLMProvider:
        base = {
            "model": "test-model",
            "max_retries": 2,
            "retry_min_wait": 0,
            "retry_max_wait": 0,
            "tool_call_retries": 1,
            "json_mode_fallback": False,
        }
        base.update(kw)
        return LLMProvider(LLMConfig(**base))

    def _client(self, responses):
        client = AsyncMock()
        client.post = AsyncMock(side_effect=responses)
        client.__aenter__ = AsyncMock(return_value=client)
        client.__aexit__ = AsyncMock(return_value=False)
        return client

    async def _call(self, provider, responses):
        with patch("mira.llm.provider.httpx.AsyncClient") as cls:
            cls.return_value = self._client(responses)
            try:
                return await provider.complete_with_tools(
                    [{"role": "user", "content": "review this"}], tools=[self._TOOL]
                )
            finally:
                self.posts = cls.return_value.post.call_args_list

    @pytest.mark.asyncio
    async def test_a_reply_cut_off_before_any_finding_is_not_an_empty_review(self):
        """`{"comments":[` balances into `{"comments": []}` — a clean review
        nobody performed. Only a salvage that recovered content is kept."""
        provider = self._provider()
        cut_off = _mock_httpx_response(_make_tool_response_json('{"comments":['))
        good = _mock_httpx_response(
            _make_tool_response_json('{"comments": [], "summary": "nothing found"}')
        )

        result = await self._call(provider, [cut_off, good])

        assert json.loads(result)["summary"] == "nothing found"
        assert len(self.posts) == 2

    @pytest.mark.asyncio
    async def test_an_unrepaired_empty_comment_list_is_a_real_answer(self):
        """The model saying it found nothing must still cost only one call."""
        provider = self._provider()
        clean = _mock_httpx_response(
            _make_tool_response_json('{"comments": [], "summary": "looks fine"}')
        )

        result = await self._call(provider, [clean])

        assert result == '{"comments": [], "summary": "looks fine"}'
        assert len(self.posts) == 1

    @pytest.mark.asyncio
    async def test_an_empty_object_is_not_an_answer(self):
        provider = self._provider()
        empty = _mock_httpx_response(_make_tool_response_json("{}"))

        with pytest.raises(LLMError, match="tool-call failed"):
            await self._call(provider, [empty, empty])

    @pytest.mark.asyncio
    async def test_another_tools_arguments_are_not_taken_as_the_review(self):
        """A call to a tool we didn't ask for is not a near-miss to fall back
        on — its arguments would be filed as the review."""
        provider = self._provider()
        data = _make_tool_response_json('{"path": "a.py"}')
        data["choices"][0]["message"]["tool_calls"][0]["function"]["name"] = "read_file"
        wrong_tool = _mock_httpx_response(data)

        with pytest.raises(LLMError, match="tool-call failed"):
            await self._call(provider, [wrong_tool, wrong_tool])

    @pytest.mark.asyncio
    async def test_an_object_carrying_none_of_the_required_fields_is_rejected(self):
        provider = self._provider()
        off_topic = _mock_httpx_response(_make_tool_response_json('{"thoughts": "hmm"}'))

        with pytest.raises(LLMError, match="tool-call failed"):
            await self._call(provider, [off_topic, off_topic])

    @pytest.mark.asyncio
    async def test_a_gateway_that_drops_the_call_name_is_still_understood(self):
        """One unnamed call is a gateway omitting the field, not the model
        answering something else."""
        provider = self._provider()
        data = _make_tool_response_json('{"comments": [], "summary": "ok"}')
        del data["choices"][0]["message"]["tool_calls"][0]["function"]["name"]

        result = await self._call(provider, [_mock_httpx_response(data)])

        assert json.loads(result)["summary"] == "ok"


class TestRetryAfterForms:
    """RFC 9110 allows delay-seconds or an HTTP date; reading only the first
    discards the server's answer and retries ahead of it."""

    def _response(self, header: str):
        resp = MagicMock()
        resp.headers = {"retry-after": header}
        return resp

    def test_delay_seconds(self):
        from mira.llm.base import _retry_after_seconds

        assert _retry_after_seconds(self._response("12")) == 12.0

    def test_an_http_date_is_read_as_the_delay_until_then(self):
        from datetime import datetime, timedelta
        from email.utils import format_datetime

        from mira.llm.base import _retry_after_seconds

        when = datetime.now(UTC) + timedelta(seconds=45)
        delay = _retry_after_seconds(self._response(format_datetime(when, usegmt=True)))

        assert delay is not None
        assert 40 <= delay <= 46

    def test_a_date_already_past_means_no_wait(self):
        from mira.llm.base import _retry_after_seconds

        assert _retry_after_seconds(self._response("Wed, 21 Oct 2015 07:28:00 GMT")) == 0.0

    def test_nonsense_falls_back_to_the_backoff_curve(self):
        from mira.llm.base import _retry_after_seconds

        assert _retry_after_seconds(self._response("soon")) is None
