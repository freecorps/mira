"""A chain of providers tried in order: the next answers when one fails.

One provider already does everything it can for its own model — transport
retries, tool-call re-rolls, its ``fallback_model`` on the same endpoint,
the JSON-mode rescue. This is the layer above that: when all of it is
spent, the same call is made through the next provider in the chain, which
may be another model on the same endpoint or a different endpoint or
account altogether. A review therefore survives a model that has started
answering with nothing (the case that motivated this), a rate limit that
outlasts the backoff, or a provider outage — as long as something further
down the list can answer.

Each call walks the chain from the top: a fallback answers one call, not
the rest of the review, so the primary is back for the next chunk if it
has recovered. The chain is built by :func:`mira.llm.create_llm` from
``LLMConfig.fallbacks``, and reads as one provider to everything upstream.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any

from mira.config import LLMConfig
from mira.exceptions import LLMError
from mira.llm.base import LLMProviderProtocol

logger = logging.getLogger(__name__)


def describe_provider(provider: object) -> str:
    """``model @ where`` for a log line, from the provider's own config."""
    config = getattr(provider, "config", None)
    if not isinstance(config, LLMConfig):
        return type(provider).__name__
    if config.oauth_provider:
        where = f"{config.oauth_provider}:{config.oauth_account or '*'}"
    elif config.provider == "bedrock":
        where = f"bedrock:{config.region}"
    else:
        where = config.endpoint or config.base_url
    return f"{config.model} @ {where}"


class FallbackChain:
    """An ordered list of providers that reads as one.

    Satisfies :class:`LLMProviderProtocol`. Capabilities and token counting
    come from the first provider; usage is summed over all of them, since
    every one of them may have spent tokens on a review.
    """

    supports_json_mode: bool
    supports_tool_calling: bool

    def __init__(self, providers: list[LLMProviderProtocol]) -> None:
        if not providers:
            raise ValueError("a fallback chain needs at least one provider")
        self.providers = list(providers)
        # Which provider issued each assistant turn's raw ``items`` (keyed by
        # the list's id, holding the list so the id cannot be reused).
        self._items_owner: dict[int, tuple[list, int]] = {}
        self.supports_json_mode = bool(getattr(providers[0], "supports_json_mode", True))
        self.supports_tool_calling = bool(getattr(providers[0], "supports_tool_calling", True))

    # ── What upstream reads off a provider ─────────────────────────

    @property
    def primary(self) -> LLMProviderProtocol:
        return self.providers[0]

    @property
    def config(self) -> LLMConfig:
        """The primary's config — what code that inspects ``llm.config`` expects."""
        return self.primary.config  # type: ignore[attr-defined]

    @property
    def total_prompt_tokens(self) -> int:
        return sum(getattr(p, "total_prompt_tokens", 0) for p in self.providers)

    @property
    def total_completion_tokens(self) -> int:
        return sum(getattr(p, "total_completion_tokens", 0) for p in self.providers)

    @property
    def usage(self) -> dict[str, int]:
        prompt = self.total_prompt_tokens
        completion = self.total_completion_tokens
        return {
            "prompt_tokens": prompt,
            "completion_tokens": completion,
            "total_tokens": prompt + completion,
        }

    def count_tokens(self, text: str) -> int:
        return self.primary.count_tokens(text)

    def labels(self) -> list[str]:
        return [describe_provider(p) for p in self.providers]

    # ── The walk ───────────────────────────────────────────────────

    async def _walk(
        self,
        what: str,
        call: Callable[[int, LLMProviderProtocol], Awaitable[Any]],
        usable: Callable[[Any], bool] = lambda _result: True,
    ) -> Any:
        """Make ``call`` on each provider in turn until one answers.

        Anything a provider raises sends the call to the next one: a
        non-retriable 4xx here means "this model or key cannot take this
        call", which is exactly when another model should. So does an answer
        ``usable`` rejects — an empty completion is the failure this chain
        exists for, even when the provider returned it rather than raising.

        When no provider gives a usable answer, the first unusable one is
        returned if there was one: that is what a lone provider would have
        handed back, and its caller already knows what to do with it.
        Otherwise the error names the whole chain and carries the last
        failure, so the log shows every step and the pull request sees one
        safe line.
        """
        last_err: Exception | None = None
        unusable: list[Any] = []
        total = len(self.providers)
        for index, provider in enumerate(self.providers):
            try:
                result = await call(index, provider)
            except Exception as exc:  # noqa: BLE001 — every failure is a reason to move on
                last_err = exc
                reason = f"{type(exc).__name__}: {exc}"
            else:
                if usable(result):
                    return result
                unusable.append(result)
                reason = "empty answer"
            if index + 1 < total:
                logger.warning(
                    "%s failed on %s (%s); falling back to %s (%d of %d)",
                    what,
                    describe_provider(provider),
                    reason,
                    describe_provider(self.providers[index + 1]),
                    index + 2,
                    total,
                )
        if unusable:
            logger.warning(
                "%s came back empty from every model in the chain that answered (%s)",
                what,
                " → ".join(self.labels()),
            )
            return unusable[0]
        assert last_err is not None  # the loop only ends here after a failure
        logger.error(
            "%s failed on every model in the chain (%s)",
            what,
            " → ".join(self.labels()),
            exc_info=True,
        )
        raise LLMError(
            "all_models_failed",
            models=" → ".join(self.labels()),
            last_model=describe_provider(self.providers[-1]),
            error=last_err,
        ) from last_err

    # ── Protocol surface ───────────────────────────────────────────

    async def complete(
        self,
        messages: list[dict[str, str]],
        json_mode: bool = True,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> str:
        return await self._walk(
            "Completion",
            lambda _i, p: p.complete(
                messages, json_mode=json_mode, temperature=temperature, max_tokens=max_tokens
            ),
            usable=lambda text: isinstance(text, str) and bool(text.strip()),
        )

    async def complete_with_tools(
        self,
        messages: list[dict[str, str]],
        tools: list[dict],
        temperature: float | None = None,
    ) -> str:
        return await self._walk(
            "Tool call",
            lambda _i, p: p.complete_with_tools(messages, tools, temperature=temperature),
        )

    async def complete_agentic(
        self,
        messages: list,
        tools: list[dict],
        temperature: float | None = None,
    ) -> dict:
        async def call(index: int, provider: LLMProviderProtocol) -> dict:
            message = await provider.complete_agentic(
                self._items_for(index, messages), tools, temperature=temperature
            )
            items = message.get("items") if isinstance(message, dict) else None
            if isinstance(items, list) and items:
                self._items_owner[id(items)] = (items, index)
            return message

        return await self._walk("Agentic call", call, usable=_agentic_usable)

    def _items_for(self, index: int, messages: list) -> list:
        """``messages`` as provider ``index`` may see them.

        A Responses-protocol provider hands back its raw output items with
        each assistant turn, and the agentic loop replays them so the model
        keeps its encrypted reasoning and call ids. Those belong to the
        endpoint — and the account — that issued them: a Chat Completions
        fallback would reject the unknown field, and another Responses
        endpoint would refuse reasoning it cannot decrypt. So a turn's items
        go back only to the provider that produced them; any other gets the
        turn rebuilt from its content and tool calls, which every protocol
        reads.
        """
        out = []
        for message in messages:
            items = message.get("items") if isinstance(message, dict) else None
            if isinstance(items, list) and items:
                owner = self._items_owner.get(id(items))
                if owner is None or owner[0] is not items or owner[1] != index:
                    message = {k: v for k, v in message.items() if k != "items"}
            out.append(message)
        return out

    async def review(self, messages: list[dict[str, str]], temperature: float | None = None) -> str:
        return await self._walk("Review", lambda _i, p: p.review(messages, temperature=temperature))

    async def walkthrough(self, messages: list[dict[str, str]]) -> str:
        return await self._walk("Walkthrough", lambda _i, p: p.walkthrough(messages))


def _agentic_usable(message: Any) -> bool:
    """An agentic hop answered when it said something or asked for a tool."""
    if not isinstance(message, dict):
        return False
    content = message.get("content")
    return bool(message.get("tool_calls")) or (isinstance(content, str) and bool(content.strip()))
