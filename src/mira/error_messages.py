"""Centralized LLM error message catalog.

Each error has two templates:
- ``full``: the detailed message used at raise time (includes model names, underlying errors)
- ``safe``: the sanitized message shown to users (no model names, no internal errors)

New LLM error types MUST add both templates here. The ``safe`` template is what
appears in PR comments; the full traceback stays in server logs.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ErrorMessage:
    """A pair of full and safe error message templates."""

    full: str
    safe: str


# Maps error code → (full_template, safe_template)
# Templates use {placeholder} syntax for .format() — not f-strings — so they
# can be reused at both raise and display sites without evaluation.
LLM_ERROR_MESSAGES: dict[str, ErrorMessage] = {
    "no_api_key": ErrorMessage(
        full=(
            "No API key found. Set {api_key_env} (or OPENROUTER_API_KEY / "
            'OPENAI_API_KEY) in the environment, or set llm.api_key_env: "" in '
            "your config for a local endpoint that needs no auth."
        ),
        safe="No API key found",
    ),
    "api_error": ErrorMessage(
        full="LLM API error {status}: {body}",
        safe="LLM API error {status}",
    ),
    "no_tool_call": ErrorMessage(
        full="Model returned neither tool call nor content",
        safe="Model returned neither tool call nor content",
    ),
    "malformed_response": ErrorMessage(
        full="LLM returned a malformed response ({detail})",
        safe="LLM returned a malformed response",
    ),
    "bad_tool_arguments": ErrorMessage(
        full="Model called {tool} with arguments that are not valid JSON: {preview}",
        safe="Model returned malformed tool-call arguments",
    ),
    "no_tools": ErrorMessage(
        full="tools list must not be empty",
        safe="tools list must not be empty",
    ),
    "completion_failed": ErrorMessage(
        full="LLM completion failed with {model}: {error}",
        safe="LLM completion failed",
    ),
    "agentic_failed": ErrorMessage(
        full="LLM agentic call failed with {model}: {error}",
        safe="LLM agentic call failed",
    ),
    "tool_call_failed": ErrorMessage(
        full="LLM tool-call failed with {model}: {error}",
        safe="LLM tool-call failed",
    ),
    "both_models_failed": ErrorMessage(
        full="Both primary ({primary_model}) and fallback ({fallback_model}) models failed: {error}",
        safe="Both primary and fallback models failed",
    ),
    # OAuth-backed providers (ChatGPT/Codex and friends)
    "oauth_not_connected": ErrorMessage(
        full=(
            "No {provider} session. Connect one in the dashboard under "
            "Settings → Connections, or run: mira auth login {provider_id}"
        ),
        safe="LLM provider is not connected",
    ),
    "oauth_unknown_provider": ErrorMessage(
        full=(
            "The model route names OAuth provider '{provider_id}', which this build does "
            "not know — pick the model again under Settings → Models"
        ),
        safe="LLM model route names an unknown provider",
    ),
    "oauth_session_failed": ErrorMessage(
        full="{provider} session could not be renewed: {error}",
        safe="LLM provider session expired",
    ),
    "oauth_accounts_exhausted": ErrorMessage(
        full=(
            "Every connected {provider} account ({count}) is rate-limited right now — "
            "wait for a window to reset, or connect another account under "
            "Settings → Connections"
        ),
        safe="Every connected account is rate-limited",
    ),
    "oauth_stream_failed": ErrorMessage(
        full="{provider} ended the response stream without completing it: {detail}",
        safe="LLM response stream ended early",
    ),
    # Bedrock-specific errors
    "bedrock_no_boto3": ErrorMessage(
        full="boto3 is required for the Bedrock provider. Install with: pip install mira-reviewer[bedrock]",
        safe="boto3 is required for the Bedrock provider",
    ),
    "bedrock_access_denied": ErrorMessage(
        full="Bedrock access denied for model {model}. Ensure your IAM role/user has bedrock:InvokeModel permission and the model is enabled in your account.",
        safe="Bedrock access denied",
    ),
    "bedrock_model_not_found": ErrorMessage(
        full="Bedrock model not found: {model}. Check the model ID and ensure it's available in your region.",
        safe="Bedrock model not found",
    ),
    "bedrock_validation_error": ErrorMessage(
        full="Bedrock validation error for {model}: {error}",
        safe="Bedrock validation error",
    ),
    "bedrock_api_error": ErrorMessage(
        full="Bedrock API error for {model}: {error}",
        safe="Bedrock API error",
    ),
    "bedrock_call_failed": ErrorMessage(
        full="Bedrock call failed with {model}: {error}",
        safe="Bedrock call failed",
    ),
    "bedrock_no_tool_call": ErrorMessage(
        full="Bedrock model returned neither tool call nor content",
        safe="Bedrock model returned neither tool call nor content",
    ),
}


def get_error_message(code: str, **kwargs: object) -> tuple[str, str]:
    """Return (full_message, safe_message) for the given error code.

    Raises:
        KeyError: If the error code is not in the catalog.
    """
    entry = LLM_ERROR_MESSAGES[code]
    return entry.full.format(**kwargs), entry.safe.format(**kwargs)
