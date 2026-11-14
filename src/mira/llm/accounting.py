"""What each LLM call spent: tokens by kind, an estimated cost, plan credits.

Mira used to add up ``prompt_tokens`` and ``completion_tokens`` and store a
single total per review. That hid the two numbers a subscription is actually
metered by: how much of the input came out of the provider's prompt cache
(billed at a fraction of the input rate, sometimes a fiftieth of it), and what
the call cost in the plan's own currency. A review that re-read the same
context across twenty agentic hops looked twenty times as expensive as it was.

Every API reports usage in its own shape, so this module first normalises it
to one :class:`TokenUsage`, with the OpenAI convention throughout: ``prompt``
counts every input token, cached ones included, and ``completion`` every
output token, reasoning included. Then :class:`Tally` keeps a client's running
totals — what a review's record carries — and :func:`record` hands each call
to the ledger (:mod:`mira.llm.ledger`), which the cost view and the estimated
allowance meters read.

Nothing here may fail a call: a usage block nobody can parse is a call with
no usage, and a ledger that cannot be written to is a ledger with a gap.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class TokenUsage:
    """One call's tokens. ``cached`` and ``reasoning`` are parts of the totals."""

    prompt: int = 0
    cached: int = 0
    completion: int = 0
    reasoning: int = 0

    @property
    def total(self) -> int:
        return self.prompt + self.completion

    def __bool__(self) -> bool:
        return bool(self.prompt or self.completion)


def _int(value: Any) -> int:
    if isinstance(value, bool):
        return 0
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def _details(usage: dict, key: str) -> dict:
    found = usage.get(key)
    return found if isinstance(found, dict) else {}


def from_chat(usage: Any) -> TokenUsage:
    """Chat Completions usage, in any of the spellings gateways use.

    ``prompt_tokens_details.cached_tokens`` is OpenAI's (and OpenRouter's);
    OpenDesign sends ``cached_read_tokens`` beside it; DeepSeek's own API
    reports ``prompt_cache_hit_tokens`` at the top level; a LiteLLM-style
    proxy in front of Anthropic may pass ``cache_read_input_tokens`` through.
    """
    if not isinstance(usage, dict):
        return TokenUsage()
    prompt = _int(usage.get("prompt_tokens"))
    prompt_details = _details(usage, "prompt_tokens_details")
    cached = max(
        _int(prompt_details.get("cached_tokens")),
        _int(prompt_details.get("cached_read_tokens")),
        _int(usage.get("prompt_cache_hit_tokens")),
        _int(usage.get("cache_read_input_tokens")),
    )
    completion = _int(usage.get("completion_tokens"))
    reasoning = _int(_details(usage, "completion_tokens_details").get("reasoning_tokens"))
    return TokenUsage(
        prompt=prompt,
        cached=min(cached, prompt),
        completion=completion,
        reasoning=min(reasoning, completion) if completion else reasoning,
    )


def from_responses(usage: Any) -> TokenUsage:
    """Responses API usage: ``input_tokens`` with ``input_tokens_details.cached_tokens``."""
    if not isinstance(usage, dict):
        return TokenUsage()
    prompt = _int(usage.get("input_tokens"))
    cached = _int(_details(usage, "input_tokens_details").get("cached_tokens"))
    completion = _int(usage.get("output_tokens"))
    reasoning = _int(_details(usage, "output_tokens_details").get("reasoning_tokens"))
    return TokenUsage(
        prompt=prompt,
        cached=min(cached, prompt),
        completion=completion,
        reasoning=min(reasoning, completion) if completion else reasoning,
    )


def from_bedrock(usage: Any) -> TokenUsage:
    """Bedrock Converse usage.

    Anthropic's convention, not OpenAI's: ``inputTokens`` leaves out what was
    read from or written to the cache, so both are added back to make
    ``prompt`` the whole input.
    """
    if not isinstance(usage, dict):
        return TokenUsage()
    read = _int(usage.get("cacheReadInputTokens"))
    written = _int(usage.get("cacheWriteInputTokens"))
    return TokenUsage(
        prompt=_int(usage.get("inputTokens")) + read + written,
        cached=read,
        completion=_int(usage.get("outputTokens")),
    )


@dataclass
class Tally:
    """A client's running totals — one review pass, since a client is one."""

    prompt: int = 0
    cached: int = 0
    completion: int = 0
    reasoning: int = 0
    cost_usd: float = 0.0
    credits: float = 0.0
    calls: int = 0
    # Calls whose model the registry had no price for: their cost is not in
    # ``cost_usd``, and a total that silently omits them would read as cheap.
    unpriced_calls: int = 0

    def add(self, usage: TokenUsage, cost_usd: float, credits: float, priced: bool) -> None:
        self.prompt += usage.prompt
        self.cached += usage.cached
        self.completion += usage.completion
        self.reasoning += usage.reasoning
        self.cost_usd += cost_usd
        self.credits += credits
        self.calls += 1
        if not priced:
            self.unpriced_calls += 1


@dataclass
class CallRecord:
    """One row of the ledger."""

    created_at: float
    endpoint: str
    provider: str
    model: str
    purpose: str
    prompt_tokens: int
    cached_tokens: int
    completion_tokens: int
    reasoning_tokens: int
    cost_usd: float
    credits: float
    priced: bool
    trace_id: str = ""
    repo: str = ""
    pr_number: int = 0

    def as_row(self) -> dict[str, Any]:
        return {
            "created_at": self.created_at,
            "endpoint": self.endpoint,
            "provider": self.provider,
            "model": self.model,
            "purpose": self.purpose,
            "prompt_tokens": self.prompt_tokens,
            "cached_tokens": self.cached_tokens,
            "completion_tokens": self.completion_tokens,
            "reasoning_tokens": self.reasoning_tokens,
            "cost_usd": self.cost_usd,
            "credits": self.credits,
            "priced": 1 if self.priced else 0,
            "trace_id": self.trace_id,
            "repo": self.repo,
            "pr_number": self.pr_number,
        }


def endpoint_of(config: Any) -> str:
    """Where a config's calls are metered, as the ledger names it.

    The stored endpoint's id, ``"config"`` for the endpoint ``mira.yaml``
    names (the id the Connections page gives it), ``"oauth:<provider>"`` for
    a signed-in account and ``"bedrock"`` for Bedrock.
    """
    oauth = getattr(config, "oauth_provider", None)
    if oauth:
        return f"oauth:{oauth}"
    if getattr(config, "provider", "") == "bedrock":
        return "bedrock"
    endpoint = getattr(config, "endpoint", None)
    if isinstance(endpoint, str) and endpoint.strip():
        return endpoint.strip()
    from mira.llm.endpoints import CONFIG_ID

    return CONFIG_ID


def account(
    tally: Tally,
    *,
    config: Any,
    provider: str,
    model: str,
    usage: TokenUsage,
    at: float | None = None,
) -> None:
    """Add one call to ``tally`` and to the ledger. Never raises.

    ``provider`` is the provider profile the call went to (``open-design``,
    ``opencode-go``, ``openrouter``…), which is what the registry's prices are
    scoped by; ``model`` is the id as configured, before any prefix strip.
    """
    if not usage:
        return
    try:
        from mira.llm import registry

        now = at if at is not None else time.time()
        estimate = registry.estimate(
            model,
            provider or None,
            prompt_tokens=usage.prompt,
            cached_tokens=usage.cached,
            completion_tokens=usage.completion,
            at=now,
        )
        tally.add(usage, estimate.cost_usd, estimate.credits, estimate.priced)
        record(
            CallRecord(
                created_at=now,
                endpoint=endpoint_of(config),
                provider=provider,
                model=model,
                purpose=str(getattr(config, "usage_purpose", "") or ""),
                prompt_tokens=usage.prompt,
                cached_tokens=usage.cached,
                completion_tokens=usage.completion,
                reasoning_tokens=usage.reasoning,
                cost_usd=estimate.cost_usd,
                credits=estimate.credits,
                priced=estimate.priced,
            )
        )
    except Exception as exc:  # noqa: BLE001 - accounting must never fail a call
        logger.debug("Could not account for a call to %s: %s", model, exc)


def record(entry: CallRecord) -> None:
    """Queue one call for the ledger, tagged with the review it belongs to."""
    from mira import logs
    from mira.llm import ledger

    entry.trace_id = entry.trace_id or logs.current_trace_id()
    if not entry.repo:
        entry.repo = logs._trace_repo.get()
        entry.pr_number = logs._trace_pr.get()
    ledger.submit(entry.as_row())


def usage_dict(tally: Tally) -> dict[str, Any]:
    """The totals as a provider's ``usage`` reports them."""
    return {
        "prompt_tokens": tally.prompt,
        "completion_tokens": tally.completion,
        "total_tokens": tally.prompt + tally.completion,
        "cached_tokens": tally.cached,
        "reasoning_tokens": tally.reasoning,
        "cost_usd": round(tally.cost_usd, 6),
        "credits": round(tally.credits, 6),
        "unpriced_calls": tally.unpriced_calls,
    }
