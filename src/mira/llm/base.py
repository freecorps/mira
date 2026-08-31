"""Provider protocol — the interface that all LLM backends must satisfy."""

from __future__ import annotations

import json
import logging
import os
import random
from typing import Any, ClassVar, Protocol, runtime_checkable

import httpx
from tenacity import retry, retry_if_exception, stop_after_attempt, wait_exponential

from mira.config import LLMConfig
from mira.exceptions import LLMError, NonRetriableLLMError, ToolCallFormatError
from mira.llm import provider_profiles as profiles
from mira.llm.tool_schemas import SUBMIT_REVIEW_TOOL, SUBMIT_WALKTHROUGH_TOOL
from mira.llm.utils import loads_lenient, strip_code_fences, strip_think_blocks

logger = logging.getLogger(__name__)


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

    Reads `config.api_key_env` first, then the matched provider profile's
    `api_key_env`, then the legacy `OPENROUTER_API_KEY` / `OPENAI_API_KEY`
    lookup for backward compatibility. If `api_key_env` is explicitly "" the
    empty string is returned without error — useful for local endpoints
    (Ollama, llama.cpp server) that don't require auth.
    """
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


def _strip_model_prefix(model: str, base_url: str) -> str:
    """Apply the endpoint's model-prefix policy from its provider profile.

    'keep' (OpenRouter) routes on the full `vendor/model` string and only sheds
    a redundant self-prefix (`openrouter/…`). 'strip' (the default for other
    endpoints) sends the bare model name (e.g. 'minimax/MiniMax-M2.7' →
    'MiniMax-M2.7').
    """
    profile = profiles.resolve(base_url)
    if profile.get("model_prefix") == "keep":
        self_prefix = f"{profile['name']}/"
        return model[len(self_prefix) :] if model.startswith(self_prefix) else model
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
    """Parse a Retry-After header expressed in seconds, if the server sent one."""
    try:
        raw = resp.headers.get("retry-after")
    except Exception:
        return None
    try:
        value = float(str(raw).strip())
    except (TypeError, ValueError):
        return None
    return value if value >= 0 else None


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

_JSON_FALLBACK_PROMPT = (
    "Do not use tools for this reply. Respond with a single JSON object and "
    "nothing else — no prose, no markdown fences. It must match the argument "
    "schema of the `{tool}` tool:\n\n{schema}"
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


def _as_json_object(raw: object) -> str | None:
    """Normalize a model payload to a JSON-object string, or None if it isn't one.

    Accepts what providers actually return: an already-decoded dict, a clean
    JSON string, a fenced one, or a truncated / XML-polluted one that
    ``loads_lenient`` can repair. The repaired object is re-serialized, so
    everything downstream sees valid JSON.
    """
    if isinstance(raw, dict):
        return json.dumps(raw)
    if not isinstance(raw, str) or not raw.strip():
        return None
    text = raw.strip()
    try:
        if isinstance(json.loads(text, strict=False), dict):
            return text  # already clean — hand it on verbatim
    except (json.JSONDecodeError, TypeError):
        pass
    parsed = loads_lenient(strip_code_fences(strip_think_blocks(text)))
    return json.dumps(parsed) if isinstance(parsed, dict) else None


def _tool_arguments(raw: object, tools: list[dict]) -> str:
    """Return a tool call's arguments as a JSON-object string.

    Raises ``ToolCallFormatError`` when the arguments can't be salvaged, so the
    caller re-rolls instead of handing unparsable text to the response parser —
    which would silently drop the whole chunk.
    """
    normalized = _as_json_object(raw)
    if normalized is None:
        raise ToolCallFormatError(
            "bad_tool_arguments", tool=_tool_name(tools), preview=_preview(raw)
        )
    return normalized


def _pick_tool_call(tool_calls: object, tools: list[dict]) -> dict | None:
    """Pick the call to the tool we asked for, else the first well-formed one.

    Models sometimes emit several calls, or repeat the call alongside a stray
    extra entry; matching on the name keeps us on the one we asked for.
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
    return candidates[0] if candidates else None


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

    def __init__(self, config: LLMConfig) -> None:
        self.config = config
        self.profile = profiles.resolve(config.base_url)
        self.total_prompt_tokens = 0
        self.total_completion_tokens = 0
        self._no_forced_tool_choice: set[str] = set()
        self._no_reasoning: set[str] = set()

        # Apply retry decorator imperatively so it reads config values
        # (max_retries, retry_min_wait, retry_max_wait) at instance time.
        self._retry = retry(
            stop=stop_after_attempt(config.max_retries),
            wait=_make_wait(config),
            retry=retry_if_exception(_retriable),
            reraise=True,
        )
        # Decorate the concrete subclass methods with retry logic.
        self._call_llm = self._retry(self._call_llm)
        self._call_llm_with_tools = self._retry(self._call_llm_with_tools)
        self._call_llm_agentic = self._retry(self._call_llm_agentic)

    # ── Shared helpers ─────────────────────────────────────────────

    def _build_headers(self) -> dict[str, str]:
        """Build request headers: Content-Type, optional Bearer auth, and any
        provider-specific extras from the profile. Authorization is omitted
        entirely if the endpoint needs no key (Ollama, llama.cpp, etc.)."""
        if hasattr(self, "_cached_headers"):
            return dict(self._cached_headers)
        headers: dict[str, str] = {"Content-Type": "application/json"}
        key = _get_api_key(self.config, self.profile)
        if key:
            headers["Authorization"] = f"Bearer {key}"
        headers.update(self.profile.get("extra_headers", {}))
        self._cached_headers = headers
        return dict(headers)

    def _apply_reasoning(self, body: dict) -> None:
        """Enable extended thinking when a reasoning effort is configured.

        The effort is passed via the unified ``reasoning.effort`` knob, after
        any per-provider remap from the profile. Anthropic models reject a
        custom ``temperature`` while thinking is on, so we drop it.
        No-op when reasoning is off, keeping the request unchanged.
        """
        effort = self.config.reasoning_effort
        if not effort or effort == "off":
            return
        if body.get("model") in self._no_reasoning:
            return
        effort = self.profile.get("reasoning_effort_map", {}).get(effort, effort)
        body["reasoning"] = {"effort": effort}
        body.pop("temperature", None)

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

    # ── Public API (shared across chat and responses providers) ─────

    async def complete(
        self,
        messages: list[dict[str, str]],
        json_mode: bool = True,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> str:
        """Complete a prompt using JSON mode, with fallback model support."""
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
    ) -> str:
        """Call the tool on ``model``, re-rolling a badly-formatted answer.

        Transport failures are already retried a level down; this loop is for
        the model's own mistakes — truncated JSON arguments, prose instead of a
        call. Resending the identical request would resample the same mistake,
        so each re-roll tells the model what was wrong and nudges the
        temperature up to break out of the bad sample.
        """
        attempts = 1 + self.config.tool_call_retries
        convo = messages
        temp = temperature
        last_err: ToolCallFormatError | None = None

        for attempt in range(attempts):
            try:
                return await self._call_llm_with_tools(model, convo, tools, temperature=temp)
            except ToolCallFormatError as exc:
                last_err = exc
                logger.warning(
                    "Model %s returned an unusable tool call (attempt %d/%d): %s",
                    model,
                    attempt + 1,
                    attempts,
                    exc,
                )
                convo = [
                    *messages,
                    {
                        "role": "user",
                        "content": _TOOL_CORRECTION.format(
                            tool=_tool_name(tools), reason=exc.safe_message
                        ),
                    },
                ]
                base_temp = self.config.temperature if temperature is None else temperature
                temp = min(1.0, base_temp + 0.2 * (attempt + 1))

        assert last_err is not None  # the loop only exits here after a failure
        raise last_err

    async def _json_mode_tool_fallback(
        self,
        messages: list[dict[str, str]],
        tools: list[dict],
        temperature: float | None = None,
    ) -> str | None:
        """Ask for the tool's arguments as plain JSON when tool calling won't work.

        Some models — and some gateways in front of them — advertise tool
        calling but answer with prose, truncated arguments, or a 400. Rather
        than failing the whole review, describe the tool's schema in the prompt
        and use JSON mode instead. Returns None if this path fails too, leaving
        the caller to raise.
        """
        if not self.config.json_mode_fallback or not tools:
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
        try:
            raw = await self._call_llm(self.config.model, prompt, True, temperature=temperature)
        except Exception as exc:
            logger.warning("JSON-mode fallback failed on %s: %s", self.config.model, exc)
            return None
        normalized = _as_json_object(raw)
        if normalized is None:
            logger.warning("JSON-mode fallback returned unusable output: %s", _preview(raw))
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
        try:
            return await self._tool_call_with_rerolls(
                self.config.model, messages, tools, temperature=temperature
            )
        except NonRetriableLLMError as exc:
            # A 400/404/422 on a tool-calling request often means this model or
            # gateway doesn't take tools at all — worth one JSON-mode attempt.
            # Anything else (auth, quota) would fail the same way, so re-raise.
            if exc.status in (400, 404, 422):
                recovered = await self._json_mode_tool_fallback(
                    messages, tools, temperature=temperature
                )
                if recovered is not None:
                    return recovered
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
                    return await self._tool_call_with_rerolls(
                        self.config.fallback_model,
                        messages,
                        tools,
                        temperature=temperature,
                    )
                except Exception as fallback_err:
                    recovered = await self._json_mode_tool_fallback(
                        messages, tools, temperature=temperature
                    )
                    if recovered is not None:
                        return recovered
                    raise LLMError(
                        "both_models_failed",
                        primary_model=self.config.model,
                        fallback_model=self.config.fallback_model,
                        error=fallback_err,
                    ) from fallback_err
            recovered = await self._json_mode_tool_fallback(
                messages, tools, temperature=temperature
            )
            if recovered is not None:
                return recovered
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
