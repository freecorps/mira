"""PR-level chat commands that need more than a keyword: `full review`,
`resolve` and `config`.

Kept out of ``handlers.py`` so the dispatcher there stays a list of branches.
Everything here is platform-neutral and works through the provider, so GitHub,
GitLab and Forgejo get the same behaviour from the same code.
"""

from __future__ import annotations

import logging
import re
from typing import Any

import yaml

from mira.autofix.redact import redact
from mira.config import MiraConfig
from mira.platforms.mentions import strip_mentions

logger = logging.getLogger(__name__)

# Two-word commands are matched on the whole comment (mentions stripped), not
# on the first word after the mention, which is all `command_after_mention`
# returns: "full review" and a question that starts with "full" differ only
# after the first word.
FULL_REVIEW_KEYWORDS = {"full review", "full-review", "full review this pr"}

# Only as a PR-level comment. On an inline reply `resolve` is an alias of
# `reject`, which records the finding as a false positive; that meaning is
# untouched because inline replies never reach the PR-level dispatcher.
RESOLVE_KEYWORDS = {"resolve", "resolve all"}

CONFIG_KEYWORDS = {"config", "configuration"}

# The sections a `@mira config` reply shows. The rest of the configuration is
# operator-facing (database, OAuth, MCP, autofix sandbox) and either secret-
# bearing or irrelevant to what a reviewer sees on a pull request.
_CONFIG_SECTIONS = ("review", "filter")
# The LLM section is reduced to which models review the code. Keys, key env
# names, endpoints and base URLs never leave the server.
_LLM_FIELDS = (
    "provider",
    "model",
    "review_model",
    "indexing_model",
    "security_model",
    "fallback_model",
    "review_reasoning_effort",
    "reasoning_effort",
    "temperature",
)
# Key-name parts that mark a value as secret-bearing, dropped whatever their
# value looks like. Matched on whole `_`-separated parts, so `allowed_authors`
# is not mistaken for `auth`. Token budgets are exempted in `_is_secret_key`.
_SECRET_KEY_PARTS = {
    "key",
    "token",
    "secret",
    "secrets",
    "password",
    "passwd",
    "credential",
    "credentials",
    "auth",
    "cookie",
    "dsn",
    "url",
    "urls",
    "endpoint",
    "webhook",
}
_MAX_CONFIG_CHARS = 12_000


def normalize_command(text: str) -> str:
    """Lowercase and collapse whitespace, so `Full   Review` is `full review`."""
    return " ".join((text or "").lower().split())


def is_review_request(text: str, names: list[str]) -> bool:
    """Whether a mention asks for a review (`review` or `full review`).

    The webhook layers let a review request through the author filter, so a
    blocked author can still ask for a review by hand. A full review is the
    same request and gets the same treatment. The whole command has to match,
    as it does when ``run_pr_command`` dispatches it: "review please" is a
    free-form question there, so it must not get past the filter here either.
    """
    from mira.platforms.handlers import _REVIEW_KEYWORDS

    command = normalize_command(strip_mentions(text, names))
    return command in _REVIEW_KEYWORDS or command in FULL_REVIEW_KEYWORDS


def _is_secret_key(name: str) -> bool:
    parts = re.split(r"[_\-.]", name.lower())
    # A token *budget* is a count, not a credential.
    if parts[-1] in {"budget", "tokens"}:
        return False
    return any(part in _SECRET_KEY_PARTS for part in parts)


def _scrub(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: _scrub(v) for k, v in value.items() if not _is_secret_key(str(k))}
    if isinstance(value, list):
        return [_scrub(v) for v in value]
    return value


def effective_config_view(config: MiraConfig) -> dict[str, Any]:
    """The review-relevant part of ``config`` with every secret-bearing key removed."""
    dumped = config.model_dump(mode="json")
    view: dict[str, Any] = {}
    llm = dumped.get("llm") or {}
    view["llm"] = {k: llm[k] for k in _LLM_FIELDS if llm.get(k) not in (None, "", [])}
    for section in _CONFIG_SECTIONS:
        view[section] = dumped.get(section) or {}
    return _scrub(view)


def render_config_reply(config: MiraConfig, actor: str) -> str:
    """Markdown reply for `@mira config`: the effective configuration as YAML.

    Secret-bearing keys are dropped by name first, then the rendered text goes
    through the same redaction filter that guards model input, so a credential
    that hid in an innocuous-looking value (a URL with a password in it, a
    token pasted into a rule) is masked as well.
    """
    body = yaml.safe_dump(
        effective_config_view(config), sort_keys=False, default_flow_style=False
    ).strip()
    body = redact(body)
    if len(body) > _MAX_CONFIG_CHARS:
        body = body[:_MAX_CONFIG_CHARS].rsplit("\n", 1)[0] + "\n# … truncated"
    return (
        f"> @{actor}: effective configuration for this repository\n\n"
        f"Deployment defaults, dashboard settings and the repository's "
        f"`.mira.yaml`, merged. Review profile: `{config.review.profile}`. "
        f"Credentials, keys and endpoints are omitted.\n\n"
        f"```yaml\n{body}\n```"
    )


async def run_resolve_command(provider: Any, pr_info: Any, actor: str) -> int:
    """Resolve every open review thread Mira opened on this PR; return how many.

    Deliberately silent towards the feedback loop: no feedback event, no
    finding state, no learning candidate. "Close these" is housekeeping, not a
    verdict that the findings were wrong, and recording it as one would teach
    Mira to stop raising exactly the issues a team just worked through.
    """
    try:
        threads = await provider.get_unresolved_bot_threads(pr_info, None)
    except Exception as exc:
        logger.warning("Could not list open Mira threads on %s: %s", pr_info.url, exc)
        await provider.post_comment(
            pr_info, f"> @{actor}: I could not read the open review threads just now."
        )
        return 0
    thread_ids = [t.thread_id for t in threads or [] if getattr(t, "thread_id", "")]
    if not thread_ids:
        await provider.post_comment(
            pr_info, f"> @{actor}: there are no open Mira review threads on this PR."
        )
        return 0
    try:
        resolved = int(await provider.resolve_threads(pr_info, thread_ids) or 0)
    except Exception as exc:
        logger.warning("Failed to resolve Mira threads on %s: %s", pr_info.url, exc)
        resolved = 0
    total = len(thread_ids)
    noun = "thread" if total == 1 else "threads"
    if resolved >= total:
        message = f"Resolved {resolved} open Mira review {noun}."
    elif resolved:
        message = (
            f"Resolved {resolved} of {total} open Mira review {noun}; "
            "the rest could not be resolved."
        )
    else:
        message = (
            f"Found {total} open Mira review {noun} but could not resolve them "
            "(this platform may not support resolving threads through the API)."
        )
    await provider.post_comment(
        pr_info,
        f"> @{actor}: {message} They were not recorded as false positives.",
    )
    logger.info("resolve on %s by @%s: %d/%d thread(s)", pr_info.url, actor, resolved, total)
    return resolved
