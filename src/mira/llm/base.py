"""Provider protocol — the interface that all LLM backends must satisfy."""

from __future__ import annotations

import json
import logging
import os
import random
import secrets
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any, ClassVar, Protocol, TypeVar, runtime_checkable

import httpx
from pydantic import BaseModel
from tenacity import retry, retry_if_exception, stop_after_attempt, wait_exponential

from mira import __version__
from mira.config import LLMConfig
from mira.exceptions import LLMError, NonRetriableLLMError, ToolCallFormatError
from mira.llm import endpoints, structured
from mira.llm import provider_profiles as profiles
from mira.llm.tool_schemas import SUBMIT_REVIEW_TOOL, SUBMIT_WALKTHROUGH_TOOL
from mira.llm.utils import loads_lenient, strip_code_fences, strip_think_blocks

logger = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseModel)


@runtime_checkable
class LLMProviderProtocol(Protocol):
    """Structural interface for LLM providers.

    Both the OpenAI-compatible provider and direct-API providers
    (Bedrock, Anthropic, Vertex, etc.) satisfy this protocol.

    Capability annotations:
        supports_json_mode: Provider natively supports response_format=json_object.
        supports_tool_calling: Provider supports function/tool calling.
    """

    supports_json_mode: bool
    supports_tool_calling: bool

    total_prompt_tokens: int
    total_completion_tokens: int

    async def complete(
        self,
        messages: list[dict[str, str]],
        json_mode: bool = True,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> str: ...

    async def complete_with_tools(
        self,
        messages: list[dict[str, str]],
        tools: list[dict],
        temperature: float | None = None,
    ) -> str: ...

    async def complete_agentic(
        self,
        messages: list,
        tools: list[dict],
        temperature: float | None = None,
    ) -> dict: ...

    async def generate_object(
        self,
        messages: list[dict[str, str]],
        schema: type[T],
        *,
        name: str,
        description: str = "",
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> T: ...

    async def review(
        self, messages: list[dict[str, str]], temperature: float | None = None
    ) -> str: ...

    async def walkthrough(self, messages: list[dict[str, str]]) -> str: ...

    def count_tokens(self, text: str) -> int: ...

    @property
    def usage(self) -> dict[str, int]: ...


# ── Module-level helpers (shared by both providers) ─────────────────


def _get_api_key(config: LLMConfig, profile: dict | None = None) -> str:
    """Resolve the API key for the configured endpoint.

    An endpoint configured from the dashboard answers for itself: the key
    stored alongside it, or the environment variable it names. It is
    returned even when empty, because "this endpoint needs no key" is
    something the operator said there rather than something to guess at.

    Otherwise, as before: `config.api_key_env` first, then the matched
    provider profile's `api_key_env`, then the legacy `OPENROUTER_API_KEY` /
    `OPENAI_API_KEY` lookup for backward compatibility. If `api_key_env` is
    explicitly "" the empty string is returned without error — useful for
    local endpoints (Ollama, llama.cpp server) that don't require auth.
    """
    named = endpoints.name_of(config.endpoint)
    if named:
        stored = endpoints.get(named)
        if stored is not None:
            return endpoints.key_for(stored)
        # The endpoint was there when this client was built and is gone now
        # — removed between the binding and the call. Falling back to the
        # environment here would send the *config file's* key to the URL the
        # deleted endpoint left behind, which is one provider's credential
        # handed to another. Refuse instead; the caller reports it.
        raise LLMError("unknown_endpoint", endpoint=named)
    if config.api_key_env == "":
        return ""
    key = os.environ.get(config.api_key_env, "")
    if not key and profile and profile.get("api_key_env"):
        key = os.environ.get(profile["api_key_env"], "")
    if not key:
        # Back-compat with pre-`api_key_env` setups.
        key = os.environ.get("OPENROUTER_API_KEY") or os.environ.get("OPENAI_API_KEY", "")
    if not key:
        raise LLMError("no_api_key", api_key_env=config.api_key_env)
    return key


def _strip_model_prefix(model: str, profile: dict | str) -> str:
    """Apply the endpoint's model-prefix policy from its provider profile.

    'keep' (OpenRouter) routes on the full `vendor/model` string and only sheds
    a redundant self-prefix (`openrouter/…`). 'strip' (the default for other
    endpoints) sends the bare model name (e.g. 'minimax/MiniMax-M2.7' →
    'MiniMax-M2.7').

    Takes the resolved profile — the client already holds one, and a stored
    endpoint's policy comes from its preset rather than from its URL. A bare
    URL is still accepted, for callers that have only that.
    """
    if isinstance(profile, str):
        profile = profiles.resolve(profile)
    if profile.get("model_prefix") == "keep":
        self_prefix = f"{profile.get('name') or ''}/"
        if len(self_prefix) > 1 and model.startswith(self_prefix):
            return model[len(self_prefix) :]
        return model
    return model.split("/", 1)[1] if "/" in model else model


def _retriable(exception: BaseException) -> bool:
    """Return True for transient errors; False for ones a retry can't fix.

    ``TransportError`` is the whole httpx transport family — timeouts, network
    errors, and protocol errors such as a server hanging up mid-response, which
    used to escape the retry and fail the review outright. A body that isn't
    JSON gets the same treatment: that's a truncated or proxy-mangled response,
    not a client mistake.

    Excluded: 4xx (the request is wrong, sending it again won't help) and
    tool-call format errors, which ``complete_with_tools`` re-rolls itself with
    a corrective prompt rather than resampling the identical request.
    """
    if isinstance(exception, (NonRetriableLLMError, ToolCallFormatError)):
        return False
    return isinstance(
        exception,
        (httpx.TransportError, json.JSONDecodeError, LLMError),
    )


def _retry_after_seconds(resp: httpx.Response) -> float | None:
    """Seconds to wait per a Retry-After header, in either form the RFC allows.

    Delay-seconds is the common one, but an HTTP date is equally valid and
    reading it as a number silently discards the server's answer — we would
    then retry on the backoff curve, ahead of the time it asked for.
    """
    try:
        raw = resp.headers.get("retry-after")
    except Exception:
        return None
    if not isinstance(raw, str) or not raw.strip():
        return None
    header = raw.strip()
    try:
        return max(0.0, float(header))
    except ValueError:
        pass
    try:
        deadline = parsedate_to_datetime(header)
    except (TypeError, ValueError):
        return None
    if deadline is None:
        return None
    if deadline.tzinfo is None:
        deadline = deadline.replace(tzinfo=UTC)
    return max(0.0, (deadline - datetime.now(UTC)).total_seconds())


# Attempts a request gets when it keeps timing out. A timeout on a
# non-streaming call means the answer takes longer than ``request_timeout`` —
# usually a reasoning model thinking at length — and asking again the same
# way waits the same time again. One more try covers a stalled connection;
# the full ``max_retries`` used to turn one slow model into six minutes.
_TIMEOUT_ATTEMPTS = 2


def _make_stop(config: LLMConfig) -> Any:
    """Stop after ``max_retries`` attempts, or after two timeouts in a row."""
    by_count = stop_after_attempt(config.max_retries)

    def _stop(retry_state: Any) -> bool:
        # Consecutive timeouts, kept on the call's own retry state: a 503
        # followed by one timeout is not two timeouts, and still retries.
        outcome = retry_state.outcome
        exc = outcome.exception() if outcome is not None else None
        timeouts = getattr(retry_state, "mira_timeouts", 0)
        timeouts = timeouts + 1 if isinstance(exc, httpx.TimeoutException) else 0
        retry_state.mira_timeouts = timeouts
        return by_count(retry_state) or timeouts >= _TIMEOUT_ATTEMPTS

    return _stop


def _make_wait(config: LLMConfig) -> Any:
    """Backoff curve: honour Retry-After, otherwise exponential plus jitter.

    Chunks are reviewed concurrently, so a bare exponential curve retries them
    all in lockstep and re-hits the same rate limit together; the jitter spreads
    them out. A server-supplied Retry-After wins over the curve, clamped to
    ``retry_max_wait`` so a bad header can't stall a review.
    """
    base = wait_exponential(
        multiplier=1,
        min=config.retry_min_wait,
        max=config.retry_max_wait,
    )

    def _wait(retry_state: Any) -> float:
        outcome = retry_state.outcome
        exc = outcome.exception() if outcome is not None else None
        hinted = getattr(exc, "retry_after", None)
        if isinstance(hinted, (int, float)):
            return max(0.0, min(float(hinted), float(config.retry_max_wait)))
        delay = float(base(retry_state))
        if delay <= 0:
            return 0.0
        return delay + random.uniform(0, min(1.0, delay))

    return _wait


# ── Tool-call response handling ─────────────────────────────────────

_TOOL_CORRECTION = (
    "Your previous reply was not a usable `{tool}` tool call ({reason}). "
    "Reply again with exactly one call to `{tool}`. Its arguments must be a "
    "single complete JSON object — no prose, no markdown fences, no commentary "
    "outside the call, and nothing truncated. If the full answer would be long, "
    "report fewer items rather than cutting the JSON short."
)

_SCHEMA_CORRECTION = (
    "Your previous `{tool}` reply was valid JSON but did not match the "
    "schema ({reason}). Reply again with the complete object, those fields "
    "corrected: every field of the right type, enums spelled exactly as "
    "listed, nothing extra."
)

_TRUNCATION_CORRECTION = (
    "Your previous reply hit the output limit before it produced a usable "
    "`{tool}` tool call ({reason}). Keep any thinking brief, then reply with "
    "exactly one call to `{tool}` whose arguments are a single complete JSON "
    "object. Report fewer items and keep each `body` short rather than "
    "running out of room again."
)

_JSON_FALLBACK_PROMPT = (
    "Do not use tools for this reply. Respond with a single JSON object and "
    "nothing else — no prose, no markdown fences. It must match the argument "
    "schema of the `{tool}` tool:\n\n{schema}"
)

_JSON_SCHEMA_PROMPT = (
    "Reply with the arguments of `{tool}` as a single JSON object. The "
    "response format holds the reply to that tool's schema, so write the "
    "object itself: no tool call, no prose around it, no markdown fences."
)

# Words a 400 about ``response_format: json_schema`` carries when the endpoint
# (or the model behind it) does not take the parameter. Anything else — a
# context overflow, a bad key — is not about the strategy and says nothing
# about the next call.
_JSON_SCHEMA_REFUSAL_HINTS = ("response_format", "json_schema", "schema", "structured", "strict")

# (endpoint, model) pairs seen not to honour ``response_format: json_schema``:
# refused with a 400 naming it, or answered as though it were not there. Kept
# for the life of the process rather than of one client — a client lasts one
# review pass, and rediscovering the same refusal on every pass would spend a
# request per review on it.
_NO_JSON_SCHEMA: set[tuple[str, str]] = set()

# Upper bound for the output budget a truncated re-roll may grow to when the
# registry does not know the model. Every current model serves at least this
# much, and it keeps a bad finish_reason from asking for a million tokens.
_OUTPUT_BUDGET_CEILING = 16_384


def _set_budget(body: dict, key: str, explicit: int | None, default: int | None) -> None:
    """Put the output cap on a request body, or leave it off for "unlimited".

    ``explicit`` is a cap the caller asked for on this one call; ``default``
    is the client's budget for the model, None when ``max_tokens: 0`` said
    to send none. A body with no cap lets the endpoint apply its own, which
    for most is the model's maximum.
    """
    cap = explicit if explicit is not None else default
    if cap:
        body[key] = cap


def _json_mode_can_help(exc: BaseException) -> bool:
    """Would asking the same model for plain JSON fix this failure?

    Only when the model answered and the answer was unusable as a tool call.
    A timeout, a 5xx or a transport error is the endpoint not answering at
    all, and a JSON-mode request goes to the same endpoint.
    """
    return isinstance(exc, ToolCallFormatError)


def _asked_for_reasoning(body: dict) -> bool:
    """True when the request carries a reasoning level, in either spelling."""
    return "reasoning" in body or "reasoning_effort" in body


def _drop_reasoning(body: dict) -> None:
    body.pop("reasoning", None)
    body.pop("reasoning_effort", None)


def _finish_reason(data: object) -> str | None:
    """``finish_reason`` of the first choice in a chat/completions payload."""
    if not isinstance(data, dict):
        return None
    choices = data.get("choices")
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
        return None
    reason = choices[0].get("finish_reason")
    return str(reason) if reason else None


def _empty_reply_detail(finish_reason: str | None, *, reasoning: bool) -> str:
    """Why an empty reply was empty, in the words the log line needs.

    "empty response" alone was the whole diagnosis for a review that died,
    and it fits three very different causes: a model that spent its output
    budget thinking and had nothing left for the call, a gateway that
    dropped the content, and a model that simply stopped. The finish reason
    and whether reasoning came back tell them apart.
    """
    parts = ["empty response"]
    if finish_reason:
        parts.append(f"finish_reason={finish_reason}")
    if reasoning:
        parts.append("the reply carried reasoning but no content")
    if finish_reason == "length" and reasoning:
        parts.append("the output budget was spent on thinking")
    return parts[0] + (f" ({'; '.join(parts[1:])})" if len(parts) > 1 else "")


def _carries_reasoning(message: object) -> bool:
    """True when a chat message holds reasoning text (any of the field names in use)."""
    if not isinstance(message, dict):
        return False
    return any(
        isinstance(message.get(key), str) and message[key].strip()
        for key in ("reasoning_content", "reasoning", "thinking")
    )


def _tool_name(tools: list[dict]) -> str:
    """Name of the tool we asked the model to call ('the tool' if malformed)."""
    try:
        return str(tools[0]["function"]["name"])
    except (KeyError, IndexError, TypeError):
        return "the tool"


def _preview(value: object, limit: int = 200) -> str:
    """Short, log-safe excerpt of a model's malformed output."""
    text = value if isinstance(value, str) else repr(value)
    text = " ".join(text.split())
    return text[:limit] + "..." if len(text) > limit else text


def _carries_content(obj: dict) -> bool:
    """True when at least one field holds something the model actually said."""
    return any(value not in (None, "", [], {}) for value in obj.values())


def _as_json_object(raw: object) -> str | None:
    """Normalize a model payload to a JSON-object string, or None if it isn't one.

    Accepts what providers actually return: an already-decoded dict, a clean
    JSON string, a fenced one, or a truncated / XML-polluted one that
    ``loads_lenient`` can repair. The repaired object is re-serialized, so
    everything downstream sees valid JSON.

    Two things are refused rather than passed on, because both would surface as
    a review that found nothing rather than as a review that failed: an empty
    object, and a repair that balanced its way to no content. A reply cut off
    at ``{"comments":[`` closes into ``{"comments": []}``, which is
    indistinguishable from a clean review nobody performed. A repair that
    salvaged real findings before the cut is still kept — that is the case the
    repair pass exists for. The distinction is deliberate: an *unrepaired*
    ``{"comments": []}`` is the model saying it found nothing, and is accepted.
    """
    if isinstance(raw, dict):
        return json.dumps(raw) if raw else None
    if not isinstance(raw, str) or not raw.strip():
        return None
    text = raw.strip()
    try:
        parsed = json.loads(text, strict=False)
    except (json.JSONDecodeError, TypeError):
        repaired = loads_lenient(strip_code_fences(strip_think_blocks(text)))
        if not isinstance(repaired, dict) or not _carries_content(repaired):
            return None
        return json.dumps(repaired)
    if not isinstance(parsed, dict) or not parsed:
        return None
    return text  # already clean — hand it on verbatim


def _tool_parameters(tools: list[dict]) -> dict | None:
    """The argument schema of the tool we asked for, or None if malformed."""
    try:
        parameters = tools[0]["function"]["parameters"]
    except (KeyError, IndexError, TypeError):
        return None
    return parameters if isinstance(parameters, dict) else None


def _settle(payload: str, tools: list[dict]) -> str:
    """``payload`` brought back to the tool's schema (see :func:`structured.normalize`).

    Handed on verbatim when there was nothing to fix, so a clean answer
    reaches the parser byte for byte as the model wrote it.
    """
    parameters = _tool_parameters(tools)
    if parameters is None:
        return payload
    data = json.loads(payload)
    fixed = structured.normalize(data, parameters)
    return payload if fixed == data else json.dumps(fixed)


def _answers_the_tool(payload: str, tools: list[dict]) -> bool:
    """True when the object plausibly answers the tool we asked for.

    Not schema validation — it is one question the JSON itself cannot answer:
    whether this object belongs to *this* tool. An object carrying none of the
    fields the schema declares required is either another tool's arguments or
    the model changing the subject, and accepting it turns both into a review
    that found nothing.
    """
    try:
        required = tools[0]["function"]["parameters"]["required"]
    except (KeyError, IndexError, TypeError):
        return True
    if not isinstance(required, list) or not required:
        return True
    data = json.loads(payload)
    return any(key in data for key in required)


def _tool_arguments(raw: object, tools: list[dict]) -> str:
    """Return a tool call's arguments as a JSON-object string.

    Raises ``ToolCallFormatError`` when the arguments can't be salvaged, so the
    caller re-rolls instead of handing unparsable text to the response parser —
    which would silently drop the whole chunk.
    """
    normalized = _as_json_object(raw)
    if normalized is None or not _answers_the_tool(normalized, tools):
        raise ToolCallFormatError(
            "bad_tool_arguments", tool=_tool_name(tools), preview=_preview(raw)
        )
    return normalized


def _pick_tool_call(tool_calls: object, tools: list[dict]) -> dict | None:
    """Pick the call to the tool we asked for.

    Models sometimes emit several calls, or repeat the call alongside a stray
    extra entry, so the name decides. A call to a *different* tool is not a
    near-miss to fall back on — its arguments would be accepted as the review —
    so it counts as no call at all and the caller re-rolls. The one exception is
    a single unnamed call, which is a gateway dropping the field rather than the
    model answering something else.
    """
    if not isinstance(tool_calls, list):
        return None
    wanted = _tool_name(tools)
    candidates = [
        c for c in tool_calls if isinstance(c, dict) and isinstance(c.get("function"), dict)
    ]
    for call in candidates:
        if call["function"].get("name") == wanted:
            return call
    if len(candidates) == 1 and not candidates[0]["function"].get("name"):
        return candidates[0]
    return None


# ── Shared base for OpenAI-compatible providers ─────────────────────


class OpenAICompatibleProvider:
    """Protocol-agnostic base for OpenAI-compatible API providers.

    Captures the code shared by ``LLMProvider`` (chat/completions) and
    ``ResponsesProvider`` (/responses protocol): retry setup, header
    building, reasoning, fallback model logic, token accounting, and the
    public API surface.  Protocol-specific paths (URL, input/output
    format, tool transformation) remain in each subclass.
    """

    supports_json_mode: ClassVar[bool] = True
    supports_tool_calling: ClassVar[bool] = True
    # Whether this protocol can ask for a reply constrained to a JSON Schema
    # (``response_format: json_schema`` / ``text.format``). Whether a given
    # endpoint honours it is learned per call; see ``_takes_json_schema``.
    supports_json_schema: ClassVar[bool] = True
    # Whether this protocol names a reasoning level as a nested
    # ``reasoning: {effort}`` object whatever the profile says. True for the
    # Responses API, where that is the only spelling.
    _nested_reasoning: ClassVar[bool] = False

    def __init__(self, config: LLMConfig) -> None:
        self.config = config
        # The endpoint's quirks: from the stored endpoint's preset when the
        # dashboard configured one, otherwise matched to the URL as before.
        self.profile = endpoints.profile_for_config(config)
        self.total_prompt_tokens = 0
        self.total_completion_tokens = 0
        self._no_forced_tool_choice: set[str] = set()
        self._no_reasoning: set[str] = set()
        self._no_temperature: set[str] = set()
        # Output budget per model, once a truncated reply has shown that
        # ``config.max_tokens`` is not enough for it. Kept for the life of
        # the client (one review pass) so every later call to that model
        # starts with the budget it was seen to need.
        self._output_budget: dict[str, int] = {}
        # One id per client instance — which is one per review pass, since a
        # client is created per purpose per review. Sent where the profile
        # names a session header, so an endpoint that routes and caches by
        # conversation sees the calls of one review as one conversation.
        self._session_id = secrets.token_hex(15)

        # Apply retry decorator imperatively so it reads config values
        # (max_retries, retry_min_wait, retry_max_wait) at instance time.
        self._retry = retry(
            stop=_make_stop(config),
            wait=_make_wait(config),
            retry=retry_if_exception(_retriable),
            reraise=True,
        )
        # Decorate the concrete subclass methods with retry logic.
        self._call_llm = self._retry(self._call_llm)
        self._call_llm_with_tools = self._retry(self._call_llm_with_tools)
        self._call_llm_agentic = self._retry(self._call_llm_agentic)
        self._call_llm_json_schema = self._retry(self._call_llm_json_schema)

    # ── Shared helpers ─────────────────────────────────────────────

    def _build_headers(self) -> dict[str, str]:
        """Build request headers: Content-Type, a User-Agent naming Mira,
        optional Bearer auth, and any provider-specific extras from the
        profile. Authorization is omitted entirely if the endpoint needs no
        key (Ollama, llama.cpp, etc.)."""
        if hasattr(self, "_cached_headers"):
            return dict(self._cached_headers)
        headers: dict[str, str] = {
            "Content-Type": "application/json",
            # Say who is calling rather than hiding behind the HTTP library's
            # name: some gateways (OpenCode Go) ask coding agents to.
            "User-Agent": f"mira/{__version__} (+https://github.com/miracodeai/mira)",
        }
        key = _get_api_key(self.config, self.profile)
        if key:
            headers["Authorization"] = f"Bearer {key}"
        headers.update(self.profile.get("extra_headers", {}))
        if self.profile.get("session_header"):
            headers[self.profile["session_header"]] = self._session_id
        self._cached_headers = headers
        return dict(headers)

    def _apply_reasoning(self, body: dict) -> None:
        """Enable extended thinking when a reasoning effort is configured.

        The level goes through the profile's rename map, then — when
        models.dev has described this model — is snapped to the nearest
        level the model actually takes, so "max" on a model that stops at
        "high" is "high" rather than a 400 (see :mod:`mira.llm.models_dev`).
        How it is named on the wire is the profile's call: the OpenAI
        ``reasoning_effort`` field for OpenAI-compatible endpoints, the
        nested ``reasoning.effort`` object for OpenRouter and for the
        Responses API. Anthropic models reject a custom ``temperature``
        while thinking is on, so it is dropped. No-op when reasoning is off.
        """
        from mira.llm import models_dev

        effort = self.config.reasoning_effort
        if not effort or effort == "off":
            return
        model = str(body.get("model") or "")
        if model in self._no_reasoning:
            return
        effort = self.profile.get("reasoning_effort_map", {}).get(effort, effort)
        levels = models_dev.reasoning_levels(self.config, model)
        if levels:
            snapped = models_dev.snap_effort(levels, effort)
            if snapped != effort:
                logger.info(
                    "Model %s takes reasoning levels %s; sending %s for %s",
                    model,
                    "/".join(levels),
                    snapped,
                    effort,
                )
                effort = snapped
        nested = (
            self._nested_reasoning
            or self.profile.get("reasoning_param", "reasoning_effort") == "reasoning"
        )
        if nested:
            body["reasoning"] = {"effort": effort}
        else:
            body["reasoning_effort"] = effort
        body.pop("temperature", None)

    async def _prepare_reasoning(self) -> None:
        """Have models.dev's levels in hand before a call that asks for thinking.

        Lookups are synchronous and never fetch, so the fetch happens here,
        where a call can afford to wait — and only when a level will be
        sent, so a review with thinking off never touches the catalogue.
        """
        effort = self.config.reasoning_effort
        if not effort or effort == "off":
            return
        from mira.llm import models_dev

        await models_dev.warm()

    def _temperature(self, body: dict, temperature: float | None = None) -> None:
        """Set the sampling temperature, unless this model has refused one.

        Some models are fixed at their own default and 400 on any other value
        (Kimi K2.7 Code, the GPT-5 family on the Responses API). Once one has
        said so, the field is left out of its later requests rather than
        spending a round trip on the same refusal every call.
        """
        if body.get("model") in self._no_temperature:
            body.pop("temperature", None)
            return
        body["temperature"] = temperature if temperature is not None else self.config.temperature

    def _max_tokens_for(self, model: str) -> int | None:
        """The output budget a call to ``model`` is made with, or None for no cap.

        ``config.max_tokens`` unless a truncated reply has raised it for this
        model (see :meth:`_raise_output_budget`). ``max_tokens: 0`` is
        "unlimited": the request carries no cap and the model stops where
        it stops.
        """
        if not self.config.max_tokens:
            return None
        return self._output_budget.get(model, self.config.max_tokens)

    def _structured_budget(self, model: str, floor: int | None) -> int | None:
        """The output cap for a structured call: the model's budget, at least ``floor``.

        ``floor`` is what the caller knows its answer needs (a batch of file
        summaries needs more than a verdict). It raises the cap without
        pinning it, so a truncated re-roll can still grow the budget past it.
        None means no cap, as for ``_max_tokens_for``.
        """
        budget = self._max_tokens_for(model)
        if budget is None:
            return None
        return max(budget, floor) if floor else budget

    def _raise_output_budget(self, model: str) -> bool:
        """Double ``model``'s output budget after a truncated reply, up to its cap.

        The cap is the registry's ``max_output_tokens`` for the model, or a
        fixed ceiling when the registry does not know it. Returns False when
        the budget is already at the cap — or when there is no budget to
        raise — so the caller knows a re-roll with the same budget is all
        that is left.
        """
        from mira.llm import registry

        current = self._max_tokens_for(model)
        if current is None:
            return False
        cap = max(
            self.config.max_tokens,
            registry.max_output_tokens(model, default=_OUTPUT_BUDGET_CEILING),
        )
        if current >= cap:
            return False
        self._output_budget[model] = min(current * 2, cap)
        logger.info(
            "Model %s ran out of output budget at %d tokens; raising it to %d for its next calls",
            model,
            current,
            self._output_budget[model],
        )
        return True

    def _refused_temperature(self, resp: httpx.Response, body: dict) -> bool:
        """Was this a 400 about the temperature? If so, drop it for a retry.

        The caller re-posts the same body without the field: a fixed
        temperature is the model's, and reviewing at its default beats
        failing the review over a sampling knob.
        """
        if resp.status_code != 400 or "temperature" not in body:
            return False
        if "temperature" not in resp.text.lower():
            return False
        model = body.get("model")
        logger.info("Model %s rejected a custom temperature; retrying without it", model)
        self._no_temperature.add(str(model))
        body.pop("temperature", None)
        return True

    def _account_usage(self, data: dict) -> None:
        """Accumulate token counts. Default: chat/completions key names.

        Subclasses override when the API uses different keys
        (e.g. Responses API uses ``input_tokens`` / ``output_tokens``).
        """
        usage = data.get("usage")
        if usage:
            self.total_prompt_tokens += usage.get("prompt_tokens", 0)
            self.total_completion_tokens += usage.get("completion_tokens", 0)

    @staticmethod
    def _handle_error(resp: httpx.Response) -> None:
        """Raise LLMError or NonRetriableLLMError on non-200 responses."""
        if resp.status_code != 200:
            if 400 <= resp.status_code < 500 and resp.status_code != 429:
                raise NonRetriableLLMError("api_error", status=resp.status_code, body=resp.text)
            error = LLMError("api_error", status=resp.status_code, body=resp.text)
            error.retry_after = _retry_after_seconds(resp)
            raise error

    @staticmethod
    def _chat_message(data: object) -> dict:
        """Pull the assistant message out of a chat/completions payload.

        Gateways answer 200 with an error object, or with an empty ``choices``
        list, often enough that indexing straight into the payload was a real
        source of failed reviews: the ``KeyError`` bypassed the retry and killed
        the run. A retriable ``LLMError`` gives the call the same second chance
        a 500 gets.
        """
        if not isinstance(data, dict):
            raise LLMError(
                "malformed_response", detail=f"expected object, got {type(data).__name__}"
            )
        choices = data.get("choices")
        if not isinstance(choices, list) or not choices:
            detail = _preview(data.get("error")) if data.get("error") else "no choices returned"
            raise LLMError("malformed_response", detail=detail)
        message = choices[0].get("message") if isinstance(choices[0], dict) else None
        if not isinstance(message, dict):
            raise LLMError("malformed_response", detail="choice carried no message")
        return message

    # ── Schema-constrained output ──────────────────────────────────

    def _endpoint_key(self) -> str:
        """Where this client's calls land, as a key for what it has refused."""
        if self.config.oauth_provider:
            return f"oauth:{self.config.oauth_provider}"
        if self.config.endpoint:
            return f"endpoint:{self.config.endpoint}"
        return self.config.base_url.rstrip("/")

    def _json_schema_for(self, tools: list[dict]) -> dict | None:
        """The strict schema for this call's tool, or None to skip the strategy.

        None when the strategy is switched off, when the protocol cannot ask
        for it, or when the tool's schema uses something strict mode cannot
        express — an open map, a ``oneOf`` — in which case tool calling
        carries the call, as it always did.
        """
        if not self.config.json_schema_mode or not self.supports_json_schema:
            return None
        parameters = _tool_parameters(tools)
        if parameters is None:
            return None
        try:
            return structured.strict_schema(parameters)
        except structured.UnstrictableSchema as exc:
            logger.debug("Not asking for json_schema output for %s: %s", _tool_name(tools), exc)
            return None

    def _takes_json_schema(self, model: str) -> bool:
        """False once this endpoint has shown ``model`` does not honour json_schema."""
        return (self._endpoint_key(), model) not in _NO_JSON_SCHEMA

    def _refuse_json_schema(self, model: str, why: str, *, remember: bool = True) -> None:
        """Stop asking ``model`` for json_schema output — for good, or for this call."""
        if remember:
            _NO_JSON_SCHEMA.add((self._endpoint_key(), model))
        logger.info(
            "Model %s %s; structured output goes through tool calling %s",
            model,
            why,
            "from now on" if remember else "for this call",
        )

    def _schema_reply(
        self,
        model: str,
        content: str,
        tools: list[dict],
        *,
        truncated: bool,
        empty_detail: str,
    ) -> str:
        """Read a json_schema-mode reply as the tool's arguments, or raise.

        A reply that is not the object we asked for, when it was not cut off,
        is evidence the endpoint ignored the format — under real enforcement
        the model cannot write anything else — so the model is moved to tool
        calling before the error goes up for the re-roll.
        """
        if content.strip():
            try:
                return _tool_arguments(content, tools)
            except ToolCallFormatError as exc:
                exc.truncated = truncated
                if not truncated:
                    self._refuse_json_schema(model, "answered outside the json_schema format")
                raise
        raise ToolCallFormatError(
            "bad_tool_arguments",
            tool=_tool_name(tools),
            preview=empty_detail,
            truncated=truncated,
        )

    # ── Subclass hooks (abstract) ──────────────────────────────────

    async def _call_llm(
        self,
        model: str,
        messages: list[dict[str, str]],
        json_mode: bool,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> str:
        raise NotImplementedError

    async def _call_llm_with_tools(
        self,
        model: str,
        messages: list[dict[str, str]],
        tools: list[dict],
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> str:
        raise NotImplementedError

    async def _call_llm_agentic(
        self,
        model: str,
        messages: list,
        tools: list[dict],
        temperature: float | None = None,
    ) -> dict:
        raise NotImplementedError

    async def _call_llm_json_schema(
        self,
        model: str,
        messages: list[dict[str, str]],
        tools: list[dict],
        schema: dict,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> str:
        """Ask for the tool's arguments as the reply itself, held to ``schema``.

        Returns the arguments as a JSON-object string, like
        ``_call_llm_with_tools``, and raises ``ToolCallFormatError`` for a
        reply that is not one (see :meth:`_schema_reply`).
        """
        raise NotImplementedError

    # ── Public API (shared across chat and responses providers) ─────

    async def complete(
        self,
        messages: list[dict[str, str]],
        json_mode: bool = True,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> str:
        """Complete a prompt using JSON mode, with fallback model support."""
        await self._prepare_reasoning()
        try:
            return await self._call_llm(
                self.config.model,
                messages,
                json_mode,
                temperature=temperature,
                max_tokens=max_tokens,
            )
        except NonRetriableLLMError:
            raise
        except Exception as primary_err:
            if self.config.fallback_model:
                logger.warning(
                    "Primary model %s failed (%s), trying fallback %s",
                    self.config.model,
                    primary_err,
                    self.config.fallback_model,
                )
                try:
                    return await self._call_llm(
                        self.config.fallback_model,
                        messages,
                        json_mode,
                        temperature=temperature,
                        max_tokens=max_tokens,
                    )
                except Exception as fallback_err:
                    raise LLMError(
                        "both_models_failed",
                        primary_model=self.config.model,
                        fallback_model=self.config.fallback_model,
                        error=fallback_err,
                    ) from fallback_err
            raise LLMError(
                "completion_failed", model=self.config.model, error=primary_err
            ) from primary_err

    async def _tool_call_with_rerolls(
        self,
        model: str,
        messages: list[dict[str, str]],
        tools: list[dict],
        temperature: float | None = None,
        validate: structured.Validator | None = None,
        max_tokens: int | None = None,
    ) -> str:
        """Call the tool on ``model``, re-rolling a badly-formatted answer.

        Transport failures are already retried a level down; this loop is for
        the model's own mistakes — truncated JSON arguments, prose instead of a
        call, an object that does not fit the schema. Resending the identical
        request would resample the same mistake, so each re-roll tells the
        model what was wrong and nudges the temperature up to break out of the
        bad sample. ``validate``, when given, is the schema check: its error
        names the fields at fault, and the correction reads them back.

        A reply cut off at the output limit is a different mistake: the model
        did not misunderstand the format, it ran out of room — usually a
        reasoning model that spent the whole budget thinking. That re-roll
        keeps the temperature and doubles the budget instead.

        Each attempt asks, when it can, for a reply constrained to the tool's
        schema (``response_format: json_schema`` with ``strict``), which leaves
        the model no way out of the shape; tool calling is the second choice.
        An endpoint that refuses the format costs no attempt — the same turn
        goes out as a tool call — and one that ignores it is moved to tool
        calling for the attempts after.
        """
        attempts = 1 + self.config.tool_call_retries
        convo = messages
        temp = temperature
        last_err: ToolCallFormatError | None = None
        schema = self._json_schema_for(tools)
        attempt = 0

        while attempt < attempts:
            use_schema = schema is not None and self._takes_json_schema(model)
            try:
                if use_schema:
                    assert schema is not None
                    hint = {
                        "role": "user",
                        "content": _JSON_SCHEMA_PROMPT.format(tool=_tool_name(tools)),
                    }
                    payload = await self._call_llm_json_schema(
                        model,
                        [*convo, hint],
                        tools,
                        schema,
                        temperature=temp,
                        max_tokens=max_tokens,
                    )
                else:
                    payload = await self._call_llm_with_tools(
                        model, convo, tools, temperature=temp, max_tokens=max_tokens
                    )
                payload = _settle(payload, tools)
                if validate is not None:
                    validate(payload)
                return payload
            except NonRetriableLLMError as exc:
                if not use_schema or exc.status not in (400, 404, 422):
                    raise
                # Refused: the endpoint or the model does not take the format.
                # Remembered only when the error says so — a context overflow
                # would fail the tool call too, and is no reason to stop asking.
                detail = str(exc).lower()
                self._refuse_json_schema(
                    model,
                    f"rejected json_schema output with HTTP {exc.status}",
                    remember=any(word in detail for word in _JSON_SCHEMA_REFUSAL_HINTS),
                )
                schema = None
                continue
            except ToolCallFormatError as exc:
                attempt += 1
                last_err = exc
                logger.warning(
                    "Model %s returned an unusable tool call (attempt %d/%d): %s",
                    model,
                    attempt,
                    attempts,
                    exc,
                )
                if exc.truncated:
                    self._raise_output_budget(model)
                    correction = _TRUNCATION_CORRECTION
                else:
                    correction = (
                        _SCHEMA_CORRECTION
                        if exc.code == "invalid_structured_output"
                        else _TOOL_CORRECTION
                    )
                    base_temp = self.config.temperature if temperature is None else temperature
                    temp = min(1.0, base_temp + 0.2 * attempt)
                convo = [
                    *messages,
                    {
                        "role": "user",
                        "content": correction.format(
                            tool=_tool_name(tools), reason=exc.safe_message
                        ),
                    },
                ]

        assert last_err is not None  # the loop only exits here after a failure
        raise last_err

    async def _json_mode_tool_fallback(
        self,
        messages: list[dict[str, str]],
        tools: list[dict],
        temperature: float | None = None,
        model: str | None = None,
        validate: structured.Validator | None = None,
        max_tokens: int | None = None,
    ) -> str | None:
        """Ask for the tool's arguments as plain JSON when tool calling won't work.

        ``model`` is the one whose tool call went wrong — the primary unless
        said otherwise — since that is the model the rescue is for.

        Some models — and some gateways in front of them — advertise tool
        calling but answer with prose, truncated arguments, or a 400. Rather
        than failing the whole review, describe the tool's schema in the prompt
        and use JSON mode instead. Returns None if this path fails too — the
        answer is not JSON, or ``validate`` finds it does not fit the schema —
        leaving the caller to raise.
        """
        if not self.config.json_mode_fallback or not tools:
            # Said out loud: from the outside, "the last recovery path was
            # switched off" and "the last recovery path ran and failed" produce
            # the same failure, and only one of them is a configuration answer.
            logger.info(
                "Skipping the JSON-mode fallback (%s)",
                "llm.json_mode_fallback is off"
                if not self.config.json_mode_fallback
                else "no tools were offered",
            )
            return None
        function = tools[0].get("function") or {}
        prompt = [
            *messages,
            {
                "role": "user",
                "content": _JSON_FALLBACK_PROMPT.format(
                    tool=function.get("name", ""),
                    schema=json.dumps(function.get("parameters") or {}),
                ),
            },
        ]
        target = model or self.config.model
        try:
            raw = await self._call_llm(
                target,
                prompt,
                True,
                temperature=temperature,
                max_tokens=self._structured_budget(target, max_tokens),
            )
        except Exception as exc:
            logger.warning(
                "JSON-mode fallback failed on %s (%s: %s)",
                target,
                type(exc).__name__,
                exc,
                exc_info=True,
            )
            return None
        normalized = _as_json_object(raw)
        if normalized is None:
            logger.warning("JSON-mode fallback returned unusable output: %s", _preview(raw))
            return None
        normalized = _settle(normalized, tools)
        if validate is not None:
            try:
                validate(normalized)
            except ToolCallFormatError as exc:
                logger.warning("JSON-mode fallback returned an object off the schema: %s", exc)
                return None
        logger.info("Recovered structured output via JSON-mode fallback")
        return normalized

    async def complete_with_tools(
        self,
        messages: list[dict[str, str]],
        tools: list[dict],
        temperature: float | None = None,
    ) -> str:
        """Complete a prompt using tool calling for structured output.

        Escalates through the recovery paths in cost order: re-roll on the
        primary model, then the fallback model, then plain JSON mode. Only when
        all of them come back empty does the call fail.
        """
        return await self._structured(messages, tools, temperature=temperature)

    async def generate_object(
        self,
        messages: list[dict[str, str]],
        schema: type[T],
        *,
        name: str,
        description: str = "",
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> T:
        """Ask for an object of type ``schema`` and return it validated.

        The structured-output entry point for new code: the tool, the strict
        schema and the validation all come from the one Pydantic model, so
        what the model is told, what the endpoint enforces and what the caller
        receives cannot drift apart. ``name`` names the tool the object is
        submitted through (``submit_…``); ``description`` defaults to the
        model's docstring. Goes through the same recovery ladder as
        :meth:`complete_with_tools`, with every answer checked against
        ``schema`` and a misfit re-rolled with the fields named.
        """
        tool = structured.tool_for(schema, name, description)
        payload = await self._structured(
            messages,
            [tool],
            temperature=temperature,
            validate=structured.validator_for(schema, name),
            max_tokens=max_tokens,
        )
        return schema.model_validate_json(payload)

    async def _structured(
        self,
        messages: list[dict[str, str]],
        tools: list[dict],
        temperature: float | None = None,
        validate: structured.Validator | None = None,
        max_tokens: int | None = None,
    ) -> str:
        """The recovery ladder behind ``complete_with_tools`` and ``generate_object``.

        ``max_tokens`` is a floor for the output budget (see
        :meth:`_structured_budget`), not a cap.
        """
        await self._prepare_reasoning()
        try:
            return await self._tool_call_with_rerolls(
                self.config.model,
                messages,
                tools,
                temperature=temperature,
                validate=validate,
                max_tokens=max_tokens,
            )
        except NonRetriableLLMError as exc:
            # A 400/404/422 on a tool-calling request often means this model or
            # gateway doesn't take tools at all — worth one JSON-mode attempt.
            # Anything else (auth, quota) would fail the same way, so re-raise.
            if exc.status in (400, 404, 422):
                logger.warning(
                    "Tool-calling request rejected by %s with HTTP %s; trying JSON mode. Error: %s",
                    self.config.model,
                    exc.status,
                    exc,
                )
                recovered = await self._json_mode_tool_fallback(
                    messages,
                    tools,
                    temperature=temperature,
                    validate=validate,
                    max_tokens=max_tokens,
                )
                if recovered is not None:
                    return recovered
            logger.warning(
                "Tool call to %s failed and is not retriable (HTTP %s): %s",
                self.config.model,
                exc.status,
                exc,
                exc_info=True,
            )
            raise
        except Exception as primary_err:
            if self.config.fallback_model:
                logger.warning(
                    "Primary model %s failed (%s: %s), trying fallback %s",
                    self.config.model,
                    type(primary_err).__name__,
                    primary_err,
                    self.config.fallback_model,
                )
                try:
                    return await self._tool_call_with_rerolls(
                        self.config.fallback_model,
                        messages,
                        tools,
                        temperature=temperature,
                        validate=validate,
                        max_tokens=max_tokens,
                    )
                except Exception as fallback_err:
                    # Rescue the model whose answer was malformed: the fallback
                    # when it was, else the primary when it was. A model that
                    # did not answer at all gets no JSON-mode request.
                    rescue_model = (
                        self.config.fallback_model
                        if _json_mode_can_help(fallback_err)
                        else self.config.model
                        if _json_mode_can_help(primary_err)
                        else None
                    )
                    recovered = (
                        await self._json_mode_tool_fallback(
                            messages,
                            tools,
                            temperature=temperature,
                            model=rescue_model,
                            validate=validate,
                            max_tokens=max_tokens,
                        )
                        if rescue_model
                        else None
                    )
                    if recovered is not None:
                        return recovered
                    # Every recovery path is spent, so this is where the review
                    # dies. Log the whole chain before the message is collapsed
                    # into the one safe line a pull request gets to see: the
                    # notice says "LLM tool-call failed" and nothing else, and
                    # which model, which status and which stack is exactly what
                    # the person reading it needs next.
                    logger.error(
                        "Tool call failed on both models (primary %s: %s; "
                        "fallback %s: %s), and JSON mode did not recover it",
                        self.config.model,
                        primary_err,
                        self.config.fallback_model,
                        fallback_err,
                        exc_info=True,
                    )
                    raise LLMError(
                        "both_models_failed",
                        primary_model=self.config.model,
                        fallback_model=self.config.fallback_model,
                        error=fallback_err,
                    ) from fallback_err
            if _json_mode_can_help(primary_err):
                recovered = await self._json_mode_tool_fallback(
                    messages,
                    tools,
                    temperature=temperature,
                    validate=validate,
                    max_tokens=max_tokens,
                )
                if recovered is not None:
                    return recovered
                rescue = "JSON mode did not recover it"
            else:
                # A timeout, a 5xx, a dropped connection: the endpoint did not
                # answer, and a JSON-mode request to the same model would wait
                # on the same endpoint. Hand the failure up at once, where a
                # fallback chain can try another model.
                rescue = "JSON mode was skipped, since the endpoint did not answer"
            logger.error(
                "Tool call failed on %s (%s: %s); %s",
                self.config.model,
                type(primary_err).__name__,
                primary_err,
                rescue,
                exc_info=True,
            )
            raise LLMError(
                "tool_call_failed", model=self.config.model, error=primary_err
            ) from primary_err

    async def complete_agentic(
        self,
        messages: list,
        tools: list[dict],
        temperature: float | None = None,
    ) -> dict:
        """Single hop of an agentic loop. Returns the assistant message dict."""
        await self._prepare_reasoning()
        try:
            return await self._call_llm_agentic(
                self.config.model, messages, tools, temperature=temperature
            )
        except NonRetriableLLMError:
            raise
        except Exception as primary_err:
            if self.config.fallback_model:
                logger.warning(
                    "Primary model %s failed (%s), trying fallback %s",
                    self.config.model,
                    primary_err,
                    self.config.fallback_model,
                )
                try:
                    return await self._call_llm_agentic(
                        self.config.fallback_model, messages, tools, temperature=temperature
                    )
                except Exception as fallback_err:
                    raise LLMError(
                        "both_models_failed",
                        primary_model=self.config.model,
                        fallback_model=self.config.fallback_model,
                        error=fallback_err,
                    ) from fallback_err
            raise LLMError(
                "agentic_failed", model=self.config.model, error=primary_err
            ) from primary_err

    async def review(self, messages: list[dict[str, str]], temperature: float | None = None) -> str:
        """Submit a review using tool calling."""
        return await self.complete_with_tools(
            messages, tools=[SUBMIT_REVIEW_TOOL], temperature=temperature
        )

    async def walkthrough(self, messages: list[dict[str, str]]) -> str:
        """Submit a walkthrough using tool calling."""
        return await self.complete_with_tools(messages, tools=[SUBMIT_WALKTHROUGH_TOOL])

    def count_tokens(self, text: str) -> int:
        """Estimate token count. Uses ~4 chars per token heuristic."""
        return len(text) // 4

    @property
    def usage(self) -> dict[str, int]:
        return {
            "prompt_tokens": self.total_prompt_tokens,
            "completion_tokens": self.total_completion_tokens,
            "total_tokens": self.total_prompt_tokens + self.total_completion_tokens,
        }
