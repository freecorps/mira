"""ChatGPT (Codex) OAuth provider.

Signs in with a ChatGPT account and reviews with the models included in that
plan, through the same backend the Codex CLI uses
(``https://chatgpt.com/backend-api/codex/responses``) — no API key, no
per-token billing.

Three things make this endpoint different from a plain OpenAI Responses one,
and all three are captured here rather than in the provider class:

* every request carries the ChatGPT account id, which arrives inside the
  id_token rather than as a top-level field;
* it answers as a server-sent event stream only, so ``stream`` is forced on
  (:mod:`mira.llm.oauth` reassembles the final response from the stream);
* it rejects several fields a stock Responses request would send
  (``temperature``, ``max_output_tokens``, ``store: true``).

Two more things the backend offers that a key-based endpoint does not, and
which the dashboard and the account rotation lean on:

* **usage.** Every response carries ``x-codex-primary-*`` / ``x-codex-
  secondary-*`` headers saying how much of the plan's 5-hour and weekly
  windows is spent and when each resets, and ``GET /wham/usage`` answers the
  same question on demand. Both are read into a :class:`UsageSnapshot`.
* **models.** ``GET /codex/models`` lists what this account may use, so the
  dropdown offers the account's real list rather than a list frozen at
  build time. The curated list below is the fallback when that call fails.

The client id and loopback port are the Codex CLI's own public values: this is
a public OAuth client with PKCE, so there is no secret to keep, and the
redirect URI is fixed by OpenAI's registration — which is why the dashboard
flow asks the user to paste the redirect URL back (see
:mod:`mira.oauth.manager`).
"""

from __future__ import annotations

import logging
import os
import time
from typing import Any, ClassVar

import httpx

from mira.oauth.base import LLMBinding, OAuthProviderSpec, OAuthTokens, decode_jwt_claims
from mira.oauth.usage import UsageSnapshot, UsageWindow

logger = logging.getLogger(__name__)

# Claim namespace OpenAI uses inside the id_token for ChatGPT account data.
_AUTH_CLAIM = "https://api.openai.com/auth"

# The ChatGPT backend root. The Codex responses endpoint hangs off
# ``/codex``; the account-level usage endpoint hangs off ``/wham`` (the
# backend's own name for the Codex service). Both are the paths the Codex CLI
# calls.
_BACKEND = "https://chatgpt.com/backend-api"
_USAGE_URL = f"{_BACKEND}/wham/usage"
_MODELS_URL = f"{_BACKEND}/codex/models"
# The models endpoint filters by the client version asking, hiding models a
# too-old CLI could not drive. Mira drives them all the same way, so it asks
# as the newest client there is; override for a backend that objects.
_CLIENT_VERSION = os.environ.get("MIRA_CODEX_CLIENT_VERSION", "99.0.0")
_ORIGINATOR = "codex_cli_rs"
_USAGE_TIMEOUT = 15.0


def _hoist_instructions(body: dict[str, Any]) -> None:
    """Move a leading system message into the top-level ``instructions`` field.

    The Codex backend expects the system prompt where its own client puts it,
    and this is the same prompt either way — the field is what the Responses
    API calls a system message, not an extra one.
    """
    if body.get("instructions"):
        return
    items = body.get("input")
    if not isinstance(items, list) or not items:
        return
    first = items[0]
    if not isinstance(first, dict) or first.get("role") != "system":
        return
    content = first.get("content")
    if not isinstance(content, str) or not content:
        return
    body["instructions"] = content
    body["input"] = items[1:]


class ChatGPTOAuthProvider(OAuthProviderSpec):
    """ChatGPT Plus/Pro/Team/Enterprise sign-in, served by the Codex backend."""

    id: ClassVar[str] = "chatgpt"
    label: ClassVar[str] = "ChatGPT (Codex)"
    description: ClassVar[str] = (
        "Sign in with ChatGPT and review using your plan's included Codex "
        "usage instead of an OpenAI API key."
    )
    docs_url: ClassVar[str] = "https://developers.openai.com/codex/cli"

    authorize_url: ClassVar[str] = "https://auth.openai.com/oauth/authorize"
    token_url: ClassVar[str] = "https://auth.openai.com/oauth/token"
    client_id: ClassVar[str] = "app_EMoamEEZ73f0CkXaXp7hrann"
    scopes: ClassVar[tuple[str, ...]] = ("openid", "profile", "email", "offline_access")
    refresh_scopes: ClassVar[tuple[str, ...]] = ("openid", "profile", "email")

    redirect_mode: ClassVar[str] = "loopback"
    loopback_port: ClassVar[int] = 1455
    loopback_path: ClassVar[str] = "/auth/callback"

    reports_usage: ClassVar[bool] = True

    llm: ClassVar[LLMBinding | None] = LLMBinding(
        base_url=f"{_BACKEND}/codex",
        api_style="responses",
        default_model="gpt-5-codex",
        models=(
            {"value": "gpt-5-codex", "label": "GPT-5 Codex", "recommended": True},
            {"value": "gpt-5", "label": "GPT-5"},
            {"value": "gpt-5.1-codex", "label": "GPT-5.1 Codex"},
            {"value": "gpt-5.1", "label": "GPT-5.1"},
            {"value": "codex-mini-latest", "label": "Codex Mini"},
        ),
        # The backend takes minimal/low/medium/high; our "max" has no
        # equivalent, so it lands on the highest level it does accept.
        reasoning_effort_map={"max": "high"},
        protocol_label="Responses API (Codex backend)",
        transport_label="server-sent events",
    )

    @classmethod
    def authorize_params(cls, *, state: str, challenge: str, redirect_uri: str) -> dict[str, str]:
        params = super().authorize_params(
            state=state, challenge=challenge, redirect_uri=redirect_uri
        )
        # Without `id_token_add_organizations` the id_token comes back with no
        # account claim, and every API call is then rejected for having no
        # account to bill against.
        params["id_token_add_organizations"] = "true"
        params["codex_cli_simplified_flow"] = "true"
        return params

    @classmethod
    def identify(cls, payload: dict[str, Any]) -> dict[str, str]:
        """Read the ChatGPT account id and plan out of the id_token.

        Falls back to the access token's claims: the two are issued together
        and the account has, in some responses, only been present on one.
        """
        claims = decode_jwt_claims(payload.get("id_token", "") or "")
        auth = claims.get(_AUTH_CLAIM) or {}
        if not isinstance(auth, dict):
            auth = {}
        account_id = str(auth.get("chatgpt_account_id", "") or "")
        plan = str(auth.get("chatgpt_plan_type", "") or "")
        if not account_id:
            access_claims = decode_jwt_claims(payload.get("access_token", "") or "")
            access_auth = access_claims.get(_AUTH_CLAIM) or {}
            if isinstance(access_auth, dict):
                account_id = str(access_auth.get("chatgpt_account_id", "") or "")
                plan = plan or str(access_auth.get("chatgpt_plan_type", "") or "")
        return {
            "account_id": account_id,
            "account_label": str(claims.get("email") or claims.get("name") or ""),
            "plan": plan,
        }

    @classmethod
    def llm_headers(cls, tokens: OAuthTokens) -> dict[str, str]:
        headers = {
            "Authorization": f"Bearer {tokens.access_token}",
            "OpenAI-Beta": "responses=experimental",
            "originator": _ORIGINATOR,
            "Accept": "text/event-stream",
        }
        if tokens.account_id:
            headers["chatgpt-account-id"] = tokens.account_id
        return headers

    @classmethod
    def _api_headers(cls, tokens: OAuthTokens) -> dict[str, str]:
        """Headers for the backend's JSON endpoints (usage, models)."""
        headers = {
            "Authorization": f"Bearer {tokens.access_token}",
            "originator": _ORIGINATOR,
            "User-Agent": f"codex_cli_rs/{_CLIENT_VERSION} (mira)",
            "Accept": "application/json",
        }
        if tokens.account_id:
            headers["chatgpt-account-id"] = tokens.account_id
        return headers

    @classmethod
    def requires_stream(cls) -> bool:
        return True

    @classmethod
    def adapt_llm_body(cls, body: dict[str, Any]) -> dict[str, Any]:
        """Shape a stock Responses body into what the Codex backend accepts.

        Sampling knobs and output caps are rejected outright here rather than
        ignored, so they are dropped instead of passed through; ``store`` must
        be false because this endpoint does not persist responses for us; and
        the encrypted reasoning trace has to be requested explicitly or a
        multi-turn tool loop loses the model's thinking between calls.
        """
        body = dict(body)
        body["stream"] = True
        body["store"] = False
        _hoist_instructions(body)
        body.pop("temperature", None)
        body.pop("max_output_tokens", None)
        body.pop("top_p", None)
        if body.get("tools"):
            body.setdefault("parallel_tool_calls", False)
        reasoning = body.get("reasoning")
        if isinstance(reasoning, dict):
            reasoning.setdefault("summary", "auto")
            include = list(body.get("include") or [])
            if "reasoning.encrypted_content" not in include:
                include.append("reasoning.encrypted_content")
            body["include"] = include
        return body

    # ── Usage ──────────────────────────────────────────────────────

    @classmethod
    def usage_from_headers(cls, headers: Any) -> UsageSnapshot | None:
        """The rate-limit headers the Codex backend puts on every response.

        ``x-codex-primary-*`` is the short (5-hour) window, ``x-codex-
        secondary-*`` the long (weekly) one; each reports a used percentage,
        the window length in minutes and a unix reset time. A response with
        none of them (an error page from a proxy, say) yields None rather
        than a snapshot that says "0% used".
        """
        if headers is None:
            return None
        primary = _window_from_headers(headers, "x-codex-primary")
        secondary = _window_from_headers(headers, "x-codex-secondary")
        credits = _credits_from_headers(headers)
        if primary is None and secondary is None and credits is None:
            return None
        reached = _header(headers, "x-codex-rate-limit-reached-type")
        return UsageSnapshot(
            primary=primary,
            secondary=secondary,
            credits=credits,
            limit_reached=bool(reached),
            source="headers",
            fetched_at=time.time(),
        )

    @classmethod
    async def fetch_usage(cls, tokens: OAuthTokens) -> UsageSnapshot | None:
        """``GET /wham/usage``: the account's windows, plan and credits.

        The same call the Codex CLI makes for its ``/status`` screen. A
        failure is logged and answered with None — a usage meter that cannot
        be drawn is not a reason to fail a settings page.
        """
        try:
            async with httpx.AsyncClient(timeout=_USAGE_TIMEOUT) as client:
                resp = await client.get(_USAGE_URL, headers=cls._api_headers(tokens))
        except httpx.HTTPError as exc:
            logger.warning("%s usage lookup failed: %s", cls.label, exc)
            return None
        if resp.status_code != 200:
            logger.warning(
                "%s usage lookup answered HTTP %s: %s",
                cls.label,
                resp.status_code,
                resp.text[:200],
            )
            return None
        try:
            payload = resp.json()
        except ValueError:
            logger.warning("%s usage lookup returned a non-JSON body", cls.label)
            return None
        return usage_from_payload(payload)

    # ── Models ─────────────────────────────────────────────────────

    @classmethod
    async def fetch_models(cls, tokens: OAuthTokens) -> list[dict[str, Any]] | None:
        """``GET /codex/models``: what this account may use, in the backend's order."""
        params = {"client_version": _CLIENT_VERSION}
        try:
            async with httpx.AsyncClient(timeout=_USAGE_TIMEOUT) as client:
                resp = await client.get(
                    _MODELS_URL, params=params, headers=cls._api_headers(tokens)
                )
        except httpx.HTTPError as exc:
            logger.warning("%s model list failed: %s", cls.label, exc)
            return None
        if resp.status_code != 200:
            logger.warning(
                "%s model list answered HTTP %s: %s", cls.label, resp.status_code, resp.text[:200]
            )
            return None
        try:
            payload = resp.json()
        except ValueError:
            return None
        return models_from_payload(payload)


# ── Payload parsing (module-level so it is testable without a network) ──


def usage_from_payload(payload: Any) -> UsageSnapshot | None:
    """Read the ``/wham/usage`` document into a snapshot.

    The document (as the Codex CLI reads it)::

        {"plan_type": "plus",
         "rate_limit": {"primary_window": {"used_percent": 42,
                                           "limit_window_seconds": 18000,
                                           "reset_after_seconds": 3600,
                                           "reset_at": 1704069000},
                        "secondary_window": {...}},
         "credits": {"has_credits": true, "unlimited": false, "balance": "9.99"},
         "rate_limit_reached_type": {...} | null}

    Anything missing is left None; a document with none of it is still a
    snapshot, so the plan name alone can be shown.
    """
    if not isinstance(payload, dict):
        return None
    limits = payload.get("rate_limit")
    if not isinstance(limits, dict):
        limits = {}
    credits_raw = payload.get("credits")
    credits: dict[str, Any] | None = None
    if isinstance(credits_raw, dict):
        credits = {
            "has_credits": bool(credits_raw.get("has_credits", False)),
            "unlimited": bool(credits_raw.get("unlimited", False)),
            "balance": credits_raw.get("balance"),
        }
    reached = payload.get("rate_limit_reached_type")
    return UsageSnapshot(
        primary=_window_from_payload(limits.get("primary_window")),
        secondary=_window_from_payload(limits.get("secondary_window")),
        credits=credits,
        plan=str(payload.get("plan_type", "") or ""),
        limit_reached=bool(reached) or bool(limits.get("limit_reached", False)),
        source="endpoint",
        fetched_at=time.time(),
    )


def models_from_payload(payload: Any) -> list[dict[str, Any]] | None:
    """Read the ``/codex/models`` document into dropdown options.

    Each entry carries a ``slug`` (the id to send), a ``display_name``, a
    ``visibility`` ("list" for the picker, "hide" for reachable-but-unlisted)
    and a ``priority`` the backend orders by. Hidden models are kept out of
    the dropdown but nothing stops an operator typing one: the endpoint
    accepts ids the list omits.
    """
    if not isinstance(payload, dict):
        return None
    entries = payload.get("models")
    if not isinstance(entries, list):
        return None
    out: list[dict[str, Any]] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        slug = str(entry.get("slug", "") or "").strip()
        if not slug:
            continue
        visibility = str(entry.get("visibility", "list") or "list").lower()
        if visibility not in ("list", ""):
            continue
        levels = entry.get("supported_reasoning_levels")
        efforts = []
        if isinstance(levels, list):
            for level in levels:
                effort = level.get("effort") if isinstance(level, dict) else level
                if isinstance(effort, str) and effort:
                    efforts.append(effort.lower())
        try:
            priority = int(entry.get("priority", 0) or 0)
        except (TypeError, ValueError):
            priority = 0
        out.append(
            {
                "value": slug,
                "label": str(entry.get("display_name") or slug),
                "description": str(entry.get("description") or ""),
                "reasoning_levels": efforts,
                "priority": priority,
            }
        )
    out.sort(key=lambda m: m["priority"])
    return [{k: v for k, v in m.items() if k != "priority"} for m in out]


def _window_from_payload(raw: Any) -> UsageWindow | None:
    if not isinstance(raw, dict):
        return None
    used = _number(raw.get("used_percent"))
    if used is None:
        return None
    seconds = _number(raw.get("limit_window_seconds"))
    minutes = int(seconds // 60) if seconds else None
    resets = _number(raw.get("reset_at"))
    if resets is None:
        after = _number(raw.get("reset_after_seconds"))
        if after is not None:
            resets = time.time() + after
    return UsageWindow(used_percent=float(used), window_minutes=minutes, resets_at=resets)


def _window_from_headers(headers: Any, prefix: str) -> UsageWindow | None:
    used = _number(_header(headers, f"{prefix}-used-percent"))
    if used is None:
        return None
    minutes = _number(_header(headers, f"{prefix}-window-minutes"))
    resets = _number(_header(headers, f"{prefix}-reset-at"))
    return UsageWindow(
        used_percent=float(used),
        window_minutes=int(minutes) if minutes is not None else None,
        resets_at=resets,
    )


def _credits_from_headers(headers: Any) -> dict[str, Any] | None:
    has = _header(headers, "x-codex-credits-has-credits")
    if has is None:
        return None
    return {
        "has_credits": has.strip().lower() in ("true", "1"),
        "unlimited": (_header(headers, "x-codex-credits-unlimited") or "").strip().lower()
        in ("true", "1"),
        "balance": (_header(headers, "x-codex-credits-balance") or "").strip() or None,
    }


def _header(headers: Any, name: str) -> str | None:
    """Case-insensitive header read that works on httpx headers and plain dicts."""
    try:
        value = headers.get(name)
        if value is None and isinstance(headers, dict):
            lowered = {str(k).lower(): v for k, v in headers.items()}
            value = lowered.get(name.lower())
    except Exception:
        return None
    return str(value) if value is not None else None


def _number(value: Any) -> float | None:
    if value is None:
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    if parsed != parsed:  # NaN
        return None
    return parsed
