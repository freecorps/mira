"""Tests for the model fallback chain.

A chain is built by ``create_llm`` from ``LLMConfig.fallbacks`` and reads as
one provider: each call walks the list until a provider answers, and the
error when none does names the whole chain.
"""

from __future__ import annotations

import logging
from unittest.mock import AsyncMock, MagicMock

import pytest

from mira.config import LLMConfig
from mira.exceptions import LLMError, NonRetriableLLMError
from mira.llm import create_llm
from mira.llm.chain import FallbackChain, describe_provider


def _provider(model: str, *, review=None, complete=None, usage=(0, 0)) -> MagicMock:
    p = MagicMock()
    p.config = LLMConfig(model=model)
    p.supports_json_mode = True
    p.supports_tool_calling = True
    p.total_prompt_tokens, p.total_completion_tokens = usage
    p.review = (
        AsyncMock(side_effect=review)
        if isinstance(review, Exception)
        else AsyncMock(return_value=review)
    )
    p.complete = (
        AsyncMock(side_effect=complete)
        if isinstance(complete, Exception)
        else AsyncMock(return_value=complete)
    )
    p.complete_with_tools = AsyncMock(return_value='{"comments": []}')
    p.complete_agentic = AsyncMock(return_value={"role": "assistant", "content": "hi"})
    p.walkthrough = AsyncMock(return_value='{"summary": "ok"}')
    p.count_tokens = MagicMock(return_value=42)
    return p


class TestWalk:
    async def test_first_provider_that_answers_wins(self):
        first = _provider("a", review=LLMError("tool_call_failed", model="a", error="empty"))
        second = _provider("b", review='{"comments": [], "summary": "b did it"}')
        third = _provider("c", review='{"comments": []}')
        chain = FallbackChain([first, second, third])

        result = await chain.review([{"role": "user", "content": "review"}])

        assert "b did it" in result
        first.review.assert_awaited_once()
        second.review.assert_awaited_once()
        third.review.assert_not_awaited()

    async def test_primary_answering_never_touches_the_rest(self):
        first = _provider("a", review='{"comments": []}')
        second = _provider("b", review='{"comments": []}')
        chain = FallbackChain([first, second])

        await chain.review([{"role": "user", "content": "review"}])

        second.review.assert_not_awaited()

    async def test_every_failure_moves_on_including_non_retriable_ones(self):
        """A 400 from one model is a reason to try another, not to stop:
        "this model cannot take this call" is exactly the case a chain is for."""
        first = _provider(
            "a", review=NonRetriableLLMError("api_error", status=400, body="no tools here")
        )
        second = _provider("b", review='{"comments": []}')
        chain = FallbackChain([first, second])

        assert await chain.review([{"role": "user", "content": "x"}]) == '{"comments": []}'

    async def test_all_failing_raises_an_error_naming_the_chain(self, caplog):
        first = _provider("a", review=LLMError("tool_call_failed", model="a", error="e1"))
        second = _provider("b", review=LLMError("tool_call_failed", model="b", error="e2"))
        chain = FallbackChain([first, second])

        with (
            caplog.at_level(logging.WARNING, logger="mira.llm.chain"),
            pytest.raises(LLMError) as info,
        ):
            await chain.review([{"role": "user", "content": "x"}])

        assert "a @" in str(info.value) and "b @" in str(info.value)
        assert "e2" in str(info.value)
        # The safe line the pull request sees carries no model names.
        assert info.value.safe_message == "Every configured model failed"
        assert isinstance(info.value.__cause__, LLMError)
        assert "falling back to b" in caplog.text

    async def test_each_call_starts_from_the_top_again(self):
        """A fallback answers one call, not the rest of the review."""
        calls = {"n": 0}

        async def flaky(*a, **kw):
            calls["n"] += 1
            if calls["n"] == 1:
                raise LLMError("tool_call_failed", model="a", error="once")
            return '{"comments": [], "summary": "a recovered"}'

        first = _provider("a")
        first.review = AsyncMock(side_effect=flaky)
        second = _provider("b", review='{"comments": [], "summary": "b"}')
        chain = FallbackChain([first, second])

        assert "b" in await chain.review([{"role": "user", "content": "x"}])
        assert "a recovered" in await chain.review([{"role": "user", "content": "x"}])
        assert second.review.await_count == 1

    async def test_every_protocol_method_walks(self):
        failing = _provider("a")
        for name in ("complete", "complete_with_tools", "complete_agentic", "walkthrough"):
            setattr(
                failing, name, AsyncMock(side_effect=LLMError("api_error", status=500, body=""))
            )
        good = _provider("b", complete="text")
        chain = FallbackChain([failing, good])

        assert await chain.complete([{"role": "user", "content": "x"}]) == "text"
        assert await chain.complete_with_tools([], tools=[{"type": "function"}]) == (
            '{"comments": []}'
        )
        assert (await chain.complete_agentic([], tools=[{"type": "function"}]))["content"] == "hi"
        assert await chain.walkthrough([]) == '{"summary": "ok"}'


class TestSurface:
    def test_usage_is_summed_over_the_chain(self):
        chain = FallbackChain([_provider("a", usage=(10, 5)), _provider("b", usage=(1, 2))])
        assert chain.usage == {"prompt_tokens": 11, "completion_tokens": 7, "total_tokens": 18}
        assert chain.total_prompt_tokens == 11

    def test_config_and_token_counting_come_from_the_primary(self):
        primary = _provider("a")
        chain = FallbackChain([primary, _provider("b")])
        assert chain.config.model == "a"
        assert chain.count_tokens("anything") == 42

    def test_an_empty_chain_is_refused(self):
        with pytest.raises(ValueError):
            FallbackChain([])

    def test_describe_names_the_model_and_where_it_goes(self):
        assert describe_provider(_provider("kimi-k2.7-code")).startswith("kimi-k2.7-code @ ")
        oauth = MagicMock()
        oauth.config = LLMConfig(model="gpt-5.6", oauth_provider="chatgpt")
        assert describe_provider(oauth) == "gpt-5.6 @ chatgpt:*"
        assert describe_provider(object()) == "object"


class TestFactory:
    def test_no_fallbacks_gives_a_plain_provider(self):
        provider = create_llm(LLMConfig(model="a"))
        assert not isinstance(provider, FallbackChain)

    def test_fallbacks_give_a_chain_in_order(self):
        config = LLMConfig(
            model="a",
            fallbacks=[LLMConfig(model="b"), LLMConfig(model="c", api_style="responses")],
        )
        chain = create_llm(config)
        assert isinstance(chain, FallbackChain)
        assert [p.config.model for p in chain.providers] == ["a", "b", "c"]
        assert type(chain.providers[2]).__name__ == "ResponsesProvider"
        # The members carry no chain of their own.
        assert all(p.config.fallbacks == [] for p in chain.providers)

    def test_a_fallback_that_cannot_be_built_is_skipped_not_fatal(self, caplog):
        """A route to an endpoint this install does not have: the primary
        still reviews, the chain is one short, and the log says which."""
        config = LLMConfig(
            model="a",
            fallbacks=[LLMConfig(model="b", endpoint="gone-endpoint"), LLMConfig(model="c")],
        )
        with caplog.at_level(logging.WARNING, logger="mira.llm"):
            chain = create_llm(config)
        assert isinstance(chain, FallbackChain)
        assert [p.config.model for p in chain.providers] == ["a", "c"]
        assert "Skipping fallback model b" in caplog.text

    def test_only_broken_fallbacks_gives_the_primary_alone(self):
        config = LLMConfig(model="a", fallbacks=[LLMConfig(model="b", endpoint="gone")])
        provider = create_llm(config)
        assert not isinstance(provider, FallbackChain)
        assert provider.config.model == "a"


class TestReviewFindings:
    """Fixes from the PR review of the chain."""

    async def test_an_empty_completion_falls_back(self):
        first = _provider("a", complete="   ")
        second = _provider("b", complete='{"ok": true}')
        chain = FallbackChain([first, second])

        assert await chain.complete([{"role": "user", "content": "x"}]) == '{"ok": true}'

    async def test_all_empty_returns_the_empty_answer_rather_than_raising(self):
        chain = FallbackChain([_provider("a", complete=""), _provider("b", complete="")])
        assert await chain.complete([{"role": "user", "content": "x"}]) == ""

    async def test_an_empty_agentic_hop_falls_back(self):
        first = _provider("a")
        first.complete_agentic = AsyncMock(return_value={"role": "assistant", "content": ""})
        second = _provider("b")
        chain = FallbackChain([first, second])

        assert (await chain.complete_agentic([], tools=[{"type": "function"}]))["content"] == "hi"

    async def test_raw_items_go_back_only_to_the_provider_that_issued_them(self):
        items = [{"type": "reasoning", "encrypted_content": "opaque"}]
        issuer = _provider("responses")
        issuer.complete_agentic = AsyncMock(
            return_value={
                "role": "assistant",
                "content": "",
                "tool_calls": [{"id": "1"}],
                "items": items,
            }
        )
        chain = FallbackChain([issuer, _provider("chat")])
        first = await chain.complete_agentic([], tools=[{"type": "function"}])

        convo = [
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [{"id": "1"}],
                "items": first["items"],
            }
        ]
        # The issuer sees its own items again.
        issuer.complete_agentic = AsyncMock(return_value={"role": "assistant", "content": "ok"})
        await chain.complete_agentic(convo, tools=[{"type": "function"}])
        assert issuer.complete_agentic.call_args.args[0][0]["items"] is items

        # A fallback never does: the turn is rebuilt from content and calls.
        issuer.complete_agentic = AsyncMock(side_effect=LLMError("api_error", status=500, body=""))
        fallback = chain.providers[1]
        await chain.complete_agentic(convo, tools=[{"type": "function"}])
        sent = fallback.complete_agentic.call_args.args[0][0]
        assert "items" not in sent
        assert sent["tool_calls"] == [{"id": "1"}]
        # The caller's conversation is not modified.
        assert convo[0]["items"] is items

    async def test_items_the_chain_did_not_issue_are_dropped_for_everyone(self):
        provider = _provider("a")
        chain = FallbackChain([provider, _provider("b")])
        await chain.complete_agentic(
            [{"role": "assistant", "content": "x", "items": [{"type": "message"}]}],
            tools=[{"type": "function"}],
        )
        assert "items" not in provider.complete_agentic.call_args.args[0][0]
