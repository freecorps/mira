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

The client id and loopback port are the Codex CLI's own public values: this is
a public OAuth client with PKCE, so there is no secret to keep, and the
redirect URI is fixed by OpenAI's registration — which is why the dashboard
flow asks the user to paste the redirect URL back (see
:mod:`mira.oauth.manager`).
"""

from __future__ import annotations

from typing import Any, ClassVar

from mira.oauth.base import LLMBinding, OAuthProviderSpec, OAuthTokens, decode_jwt_claims

# Claim namespace OpenAI uses inside the id_token for ChatGPT account data.
_AUTH_CLAIM = "https://api.openai.com/auth"


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

    llm: ClassVar[LLMBinding | None] = LLMBinding(
        base_url="https://chatgpt.com/backend-api/codex",
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
            "originator": "codex_cli_rs",
            "Accept": "text/event-stream",
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
