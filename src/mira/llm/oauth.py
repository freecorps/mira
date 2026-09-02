"""Responses-API provider authenticated by a stored OAuth grant.

Same protocol as :class:`~mira.llm.responses.ResponsesProvider` — it is a
subclass, so message conversion, tool handling, retries and fallbacks are
shared — with four things swapped underneath:

* **auth**: the bearer token comes from :mod:`mira.oauth.store` and is renewed
  before it expires, instead of from an environment variable;
* **which account**: ``config.oauth_account`` pins one; left empty, the
  provider rotates across every account the operator connected — each call
  goes to the one with the most allowance left, and an account the backend
  refuses (429) is set aside until its window resets and the call is retried
  on the next one;
* **body shape**: the provider spec gets the last word on the request, because
  a consumer endpoint accepts a narrower set of fields than the public API;
* **transport**: endpoints that only answer as an event stream are read back
  into the one final response object the rest of the code already knows how to
  parse, so streaming stays an implementation detail of this file.

Every response's rate-limit headers are recorded against the account that
made the call, which is what the Connections page shows and what the
rotation ranks by.
"""

from __future__ import annotations

import contextlib
import json
import logging
import time
from typing import Any

import httpx

from mira.config import LLMConfig
from mira.exceptions import LLMError
from mira.llm.base import _retry_after_seconds
from mira.llm.responses import ResponsesProvider
from mira.oauth import registry, store
from mira.oauth.base import OAuthError, OAuthProviderSpec, OAuthTokens
from mira.oauth.routes import ANY_ACCOUNT

logger = logging.getLogger(__name__)

# Long enough for a slow first token on a big review prompt; the per-request
# ceiling is still `config.request_timeout` on the client.
_STREAM_CHUNK_LIMIT = 1_000_000
# When a 429 says nothing about when to come back, set the account aside for
# this long before rotation considers it again.
_DEFAULT_COOLDOWN = 300.0
# Response headers worth carrying across the stream-to-response collapse:
# the retry policy reads one, the usage bookkeeping reads the rest.
_KEPT_HEADER_PREFIXES = ("x-codex-", "x-ratelimit-", "x-request-id")
_KEPT_HEADERS = {"retry-after", "content-type"}


class OAuthResponsesProvider(ResponsesProvider):
    """Talks to a provider the operator signed into rather than paid per token."""

    def __init__(self, config: LLMConfig) -> None:
        super().__init__(config)
        self._spec: type[OAuthProviderSpec] = registry.require(config.oauth_provider or "")
        if self._spec.llm is None:
            raise LLMError(
                "oauth_not_connected",
                provider=self._spec.label,
                provider_id=self._spec.id,
            )
        pinned = (config.oauth_account or "").strip()
        self._pinned: str = "" if pinned == ANY_ACCOUNT else pinned
        # The account the next call goes to. Chosen lazily, and kept for the
        # life of this provider (one review pass) unless the backend refuses
        # it: a tool loop carries encrypted reasoning between turns, which is
        # best not bounced between accounts mid-conversation.
        self._account_key: str = self._pinned
        self._tokens: OAuthTokens | None = None
        # Accounts this instance has seen refused; rotation skips them.
        self._refused: set[str] = set()
        # `_apply_reasoning` reads the effort map off `self.profile`, which the
        # base class resolves from `base_url`. An OAuth endpoint has no entry in
        # providers.json, so give it the spec's map here rather than asking
        # operators to register a profile for a URL they never typed.
        self.profile = {
            **self.profile,
            "name": f"oauth:{self._spec.id}",
            "reasoning_effort_map": dict(self._spec.llm.reasoning_effort_map),
        }

    @property
    def rotates(self) -> bool:
        return not self._pinned

    @property
    def account_key(self) -> str:
        """The account currently serving this provider ("" until the first call)."""
        return self._account_key

    # ── Account selection ──────────────────────────────────────────

    def _choose_account(self) -> str:
        """Settle on an account for the next call.

        Pinned: that one, whether or not it is connected — the error for a
        missing pinned account should name it, not quietly use another.
        Rotating: the store's pick by headroom, skipping any this instance
        has already been refused by.
        """
        if self._pinned:
            return self._pinned
        if self._account_key and self._account_key not in self._refused:
            return self._account_key
        key = store.pick_account(self._spec.id, exclude=self._refused)
        if key is None:
            raise self._nothing_available()
        return key

    def _nothing_available(self) -> LLMError:
        if not store.accounts(self._spec.id):
            return LLMError(
                "oauth_not_connected", provider=self._spec.label, provider_id=self._spec.id
            )
        return LLMError(
            "oauth_accounts_exhausted",
            provider=self._spec.label,
            count=len(store.accounts(self._spec.id)),
        )

    # ── Auth ───────────────────────────────────────────────────────

    async def _ensure_token(self, force_refresh: bool = False) -> OAuthTokens:
        """Load (and if needed renew) the grant for the account in use.

        ``force_refresh`` renews against the issuer rather than just dropping
        the cached copy. The caller reaches for it after a 401, which means the
        token the endpoint just rejected — and re-reading the store would hand
        back that same token, since "the server revoked it" and "it expired"
        are not the same condition and only the second one is visible here.
        """
        key = self._choose_account()
        if key != self._account_key:
            self._account_key = key
            self._tokens = None
        if force_refresh:
            self._tokens = None
        elif self._tokens is not None and not self._tokens.is_expired():
            return self._tokens
        try:
            self._tokens = await store.valid_tokens(self._spec.id, key, force=force_refresh)
        except OAuthError as exc:
            if store.load(self._spec.id, key) is None:
                raise LLMError(
                    "oauth_not_connected",
                    provider=self._spec.label,
                    provider_id=self._spec.id,
                ) from exc
            raise LLMError(
                "oauth_session_failed", provider=self._spec.label, error=str(exc)
            ) from exc
        return self._tokens

    def _build_headers(self) -> dict[str, str]:
        """Headers for the current grant.

        Deliberately not memoised the way the base class memoises an API key:
        the token here rotates, and a cached ``Authorization`` outlives it.
        """
        tokens = self._tokens
        if tokens is None:
            raise LLMError(
                "oauth_not_connected", provider=self._spec.label, provider_id=self._spec.id
            )
        return {"Content-Type": "application/json", **self._spec.llm_headers(tokens)}

    # ── Call methods (token first, then the inherited protocol) ────

    async def _call_llm(self, *args: Any, **kwargs: Any) -> str:
        await self._ensure_token()
        return await super()._call_llm(*args, **kwargs)

    async def _call_llm_with_tools(self, *args: Any, **kwargs: Any) -> str:
        await self._ensure_token()
        return await super()._call_llm_with_tools(*args, **kwargs)

    async def _call_llm_agentic(self, *args: Any, **kwargs: Any) -> dict:
        await self._ensure_token()
        return await super()._call_llm_agentic(*args, **kwargs)

    # ── Transport ──────────────────────────────────────────────────

    async def _post(self, client: httpx.AsyncClient, body: dict) -> httpx.Response:
        """Send one request; renew on 401, move to another account on 429.

        A token can go stale between the pre-flight check and the request
        landing — a long queue, a clock a few minutes out, a session revoked
        elsewhere. The base class treats 401 as non-retriable (it is, for an API
        key), so re-authenticating has to happen here or a whole review dies on
        a token that a single refresh would have fixed.

        A 429 is the account's allowance running out. With one account there
        is nothing to do but hand it back, and the retry policy waits out the
        ``Retry-After``. With several, the refused account is set aside until
        its window resets and the same request goes to the next one — that is
        what connecting a second account is for.
        """
        adapted = self._spec.adapt_llm_body(body)
        while True:
            await self._ensure_token()
            key = self._account_key
            resp = await self._send(client, adapted)
            self._record(key, resp)
            if resp.status_code == 401:
                logger.info(
                    "%s rejected the session; refreshing and retrying once", self._spec.label
                )
                await self._ensure_token(force_refresh=True)
                resp = await self._send(client, adapted)
                self._record(key, resp)
            if resp.status_code != 429 or not self.rotates:
                return resp
            self._set_aside(key, resp)
            if store.pick_account(self._spec.id, exclude=self._refused) is None:
                # Nothing left to rotate to. Hand the 429 back so the retry
                # policy waits on its Retry-After rather than failing outright.
                return resp
            logger.info(
                "%s account %s is rate-limited; moving to another account", self._spec.label, key
            )
            self._account_key = ""
            self._tokens = None

    def _set_aside(self, key: str, resp: httpx.Response) -> None:
        """Mark an account refused until the backend says it is worth retrying."""
        self._refused.add(key)
        until = time.time() + _DEFAULT_COOLDOWN
        hinted = _retry_after_seconds(resp)
        if hinted is not None:
            until = time.time() + hinted
        snapshot = self._spec.usage_from_headers(resp.headers)
        if snapshot is not None:
            resets = [
                w.resets_at
                for w in (snapshot.primary, snapshot.secondary)
                if w is not None and w.resets_at and w.used_percent >= 100.0
            ]
            if resets:
                until = max(until, min(resets))
        # No database (a CLI dry path) means nowhere to note it; the in-memory
        # `_refused` set still keeps this instance off the account.
        with contextlib.suppress(OAuthError):
            store.mark_exhausted(self._spec.id, key, until)

    def _record(self, key: str, resp: httpx.Response) -> None:
        """Write what the backend said about the account's allowance."""
        if not key:
            return
        try:
            snapshot = self._spec.usage_from_headers(resp.headers)
            if snapshot is not None:
                snapshot.last_used_at = time.time()
                store.save_usage(self._spec.id, key, snapshot)
            else:
                store.mark_used(self._spec.id, key)
        except OAuthError:  # pragma: no cover - no database (CLI dry paths)
            pass
        except Exception as exc:  # pragma: no cover - bookkeeping must not fail a call
            logger.debug("Could not record %s usage: %s", self._spec.label, exc)

    async def _send(self, client: httpx.AsyncClient, body: dict) -> httpx.Response:
        if not self._spec.requires_stream():
            return await client.post(self._url, headers=self._build_headers(), json=body)
        return await self._stream(client, body)

    async def _stream(self, client: httpx.AsyncClient, body: dict) -> httpx.Response:
        """Consume an SSE response and hand back the final object as a response.

        The caller (and every parser downstream) expects a plain
        ``httpx.Response`` holding one Responses-API payload, so the stream is
        collapsed here into exactly that. Errors are passed through as the real
        response, headers included, so ``Retry-After`` still reaches the retry
        policy; the rate-limit headers ride along on success too, so the
        account's usage can be recorded.
        """
        async with client.stream(
            "POST", self._url, headers=self._build_headers(), json=body
        ) as resp:
            kept = _kept_headers(resp.headers)
            if resp.status_code != 200:
                raw = await resp.aread()
                return httpx.Response(
                    resp.status_code, content=raw[:_STREAM_CHUNK_LIMIT], headers=kept
                )
            payload = await _collect_stream(resp, self._spec.label)
        kept.pop("content-type", None)
        return httpx.Response(200, json=payload, headers=kept)


def _kept_headers(headers: httpx.Headers) -> dict[str, str]:
    out: dict[str, str] = {}
    for name, value in headers.items():
        lowered = name.lower()
        if lowered in _KEPT_HEADERS or lowered.startswith(_KEPT_HEADER_PREFIXES):
            out[lowered] = value
    return out


async def _collect_stream(resp: httpx.Response, label: str) -> dict[str, Any]:
    """Reduce an SSE stream to the final ``response`` object it carried.

    The stream is a running commentary — deltas, item lifecycle, usage — and
    every event that matters here also ships the whole response object, so we
    keep the last one we see and return it when the stream ends. That tolerates
    a provider adding event types without this parser needing to know them.
    """
    latest: dict[str, Any] | None = None
    failure: str = ""

    async for line in resp.aiter_lines():
        if not line.startswith("data:"):
            continue
        data = line[len("data:") :].strip()
        if not data or data == "[DONE]":
            continue
        try:
            event = json.loads(data)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict):
            continue
        kind = event.get("type", "")
        response = event.get("response")
        if isinstance(response, dict):
            latest = response
        if kind == "response.completed":
            return latest or {}
        if kind in ("response.failed", "error", "response.incomplete"):
            failure = _stream_error_detail(event) or kind

    if failure:
        raise LLMError("oauth_stream_failed", provider=label, detail=failure)
    if latest is not None:
        # Stream cut off after the response was described but before it said
        # "completed". The object we have is still the model's answer; letting
        # the normal parser judge it beats failing on a missing event.
        logger.warning("%s stream ended without a completion event", label)
        return latest
    raise LLMError("oauth_stream_failed", provider=label, detail="no response events")


def _stream_error_detail(event: dict[str, Any]) -> str:
    """The human-readable part of a failure event, wherever it was put."""
    for source in (event.get("response"), event):
        if not isinstance(source, dict):
            continue
        error = source.get("error")
        if isinstance(error, dict) and error.get("message"):
            return str(error["message"])
        if isinstance(error, str) and error:
            return error
        detail = source.get("incomplete_details")
        if isinstance(detail, dict) and detail.get("reason"):
            return str(detail["reason"])
    return ""
