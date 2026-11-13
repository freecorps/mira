"""``@mira <question>`` on a pull request: the platform-neutral chat handler.

Three things happen here, all through the provider so GitHub, GitLab and
Forgejo behave the same:

- **The conversation so far is read**, so "and the other file?" means
  something. Earlier messages — the humans' and Mira's own — go into the
  prompt oldest first, bounded, framed as untrusted text.
- **The answer comes from the tool loop** (``core/chat.py``) over the
  repository at the pull request's head, with the one-shot answer as the
  fallback when the loop cannot finish.
- **A request to change code is handed to autofix** rather than answered:
  "fix this" or "pode corrigir?" in a chat is the same request as
  ``@mira fix``, and gets the same path, the same permission checks and the
  same reply. Nothing here writes code.
"""

from __future__ import annotations

import logging
import re
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Any

from mira.config import MiraConfig
from mira.core.chat import ChatAnswer, answer_question
from mira.llm.prompts.review import build_conversation_prompt
from mira.models import ConversationMessage
from mira.platforms.mentions import mention_names, strip_mentions

logger = logging.getLogger(__name__)

# Each earlier message is cut here. A review summary runs to many kilobytes,
# and what the next question needs from it is what it was about.
_MAX_MESSAGE_CHARS = 1500

# Hidden markers Mira writes into its own comments (finding ids, summary
# anchors). They carry nothing the model can use.
_HTML_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)

# ── Change requests ──────────────────────────────────────────────────────
#
# Matched on the human's own words only — quoted lines and code blocks are
# dropped first, so a message quoting "can you fix this?" back is not one.
# Two shapes count: the verb as an imperative opening the message ("fix the
# null check", "corrige isso"), and a request form around it ("can you fix",
# "pode corrigir?", "¿puedes arreglarlo?"). A verb anywhere else is talk
# about a fix, not a request for one.
_FIX_VERBS_EN = r"fix|patch|correct|apply"
_FIX_VERBS_PT = (
    r"corrigir|corrige|corrija|corrijam|consertar|conserta|conserte|arrumar|arruma|"
    r"arrume|ajustar|aplicar|resolver|fixar"
)
_FIX_VERBS_ES = r"corregir|arreglar|arregla|arreglarlo|arreglalo|aplicar"
# English "fix" and "patch" are nouns as often as verbs ("Fix applied in
# abc123" reports a fix), so as an opening they count only with an object.
# The Portuguese and Spanish forms listed are verbs only.
_IMPERATIVE_RE = re.compile(
    r"^(?:(?:please|pls|ok|okay|por\s+favor|então|entao|beleza),?\s+)?"
    r"(?:(?:fix|patch)\s+(?:this|that|it|the|these|those|them|all|everything|my|our|its)\b"
    # Every verb form the request lists hold, minus "resolver": "resolve" opens
    # the existing resolve command, and as an opening it is not a code change.
    r"|(?:corrige|corrija|corrijam|corrigir|conserta|conserte|consertar|arruma|arrume|"
    r"arrumar|ajusta|ajuste|ajustar|aplica|aplique|aplicar|fixa|fixar|"
    r"corregir|arregla|arreglar|arreglalo|arréglalo)\b)",
    re.IGNORECASE,
)
_REQUEST_RE = re.compile(
    r"\b(?:"
    rf"(?:can|could|would|will)\s+you\s+(?:please\s+|also\s+|just\s+)*(?:{_FIX_VERBS_EN})"
    rf"|please\s+(?:{_FIX_VERBS_EN})"
    r"|(?:pode|podes|poderia|poderias|consegue|consegues|conseguiria|tem\s+como|d[áa]\s+pr[ao])"
    r"\s+(?:(?:voc[êe]|vc|tu|por\s+favor|j[áa]|s[óo])\s+)*"
    rf"(?:{_FIX_VERBS_PT}|fazer\s+o\s+fix)"
    rf"|(?:puedes|podr[íi]as|podr[íi]a)\s+(?:{_FIX_VERBS_ES})"
    r")\b",
    re.IGNORECASE,
)
# "How would you fix this?" asks for an explanation, and "don't fix it" for
# the opposite.
_NOT_A_REQUEST_RE = re.compile(
    r"\b(?:how|why|como|por\s*que|porque|c[óo]mo)\s+(?:\w+\s+){0,2}"
    r"(?:would|could|can|should|poderia|pode|deveria|puedes)\b"
    r"|\b(?:don'?t|do\s+not|never|n[ãa]o|no)\s+(?:\w+\s+){0,2}"
    r"(?:fix|corrig|consert|arrum|arregl)",
    re.IGNORECASE,
)
# "...all of them", "corrige tudo": the batch request rather than one finding.
_ALL_RE = re.compile(r"\b(?:all|everything|every|tudo|todos|todas)\b", re.IGNORECASE)


def _own_words(text: str) -> str:
    """``text`` without quoted lines and fenced code, which are not the author's ask."""
    text = re.sub(r"```.*?(?:```|$)", " ", text or "", flags=re.DOTALL)
    return "\n".join(line for line in text.splitlines() if not line.lstrip().startswith(">"))


def wants_code_change(text: str) -> str | None:
    """``"single"`` or ``"all"`` when ``text`` asks Mira to change code, else None.

    Deliberately narrow: a question that is answered when it was a request
    costs one more comment, a request that is mistaken for one queues work
    nobody asked for.
    """
    words = " ".join(_own_words(text).split())
    if not words or _NOT_A_REQUEST_RE.search(words):
        return None
    if not (_IMPERATIVE_RE.search(words) or _REQUEST_RE.search(words)):
        return None
    return "all" if _ALL_RE.search(words) else "single"


async def handoff_to_autofix(
    provider: Any,
    pr_info: Any,
    *,
    actor: str,
    kind: str,
    bot_name: str,
    config: MiraConfig,
    original_body: str = "",
    finding_id: str = "",
    reply: Callable[[str], Awaitable[None]] | None = None,
) -> None:
    """Send a change request made in chat down the ``@mira fix`` path.

    When autofix is off here, say how to turn it on: a request answered with
    an explanation of the code it asked to change has been ignored. A
    single-finding request with no finding to anchor to — the pull request's
    own conversation — says how to name one, as ``@mira fix`` there does.
    """
    from mira.autofix.commands import handle_fix_command
    from mira.autofix.policy import resolve_policy

    async def _post(body: str) -> None:
        if reply is not None:
            await reply(body)
        else:
            await provider.post_comment(pr_info, body)

    policy = resolve_policy(config.autofix, pr_info.owner, pr_info.repo)
    if not policy.active:
        await _post(autofix_off_message(actor, bot_name, kill_switch=config.autofix.kill_switch))
        logger.info("Chat change request on %s by @%s: autofix is off", pr_info.url, actor)
        return
    if kind == "single" and not (original_body or finding_id):
        await _post(
            f"> @{actor}: that reads as a request to change code. Reply `@{bot_name} fix` "
            f"to one of my review comments to fix that finding, or comment "
            f"`@{bot_name} fix all` here for the most serious open ones."
        )
        return
    logger.info("Chat change request on %s by @%s → autofix (%s)", pr_info.url, actor, kind)
    await handle_fix_command(
        provider,
        pr_info,
        actor=actor,
        kind=kind,
        original_body=original_body,
        finding_id=finding_id,
        config=config,
        reply=reply,
    )


def autofix_off_message(actor: str, bot_name: str, *, kill_switch: bool = False) -> str:
    """What a change request gets when autofix cannot take it."""
    if kill_switch:
        return (
            f"> @{actor}: that reads as a request to change code, but autofix is switched "
            "off for every repository on this deployment (`autofix.kill_switch`). An "
            "operator has to lift it first."
        )
    return (
        f"> @{actor}: that reads as a request to change code, and autofix is not enabled "
        "for this repository, so I cannot make the change from here. A maintainer can "
        "turn it on with `autofix.mode: suggest` (the patch is generated, validated and "
        "shown; nothing is written) or `autofix.mode: on` (delivered as a branch and a "
        "stacked pull request), in Mira's configuration or the dashboard. Then reply "
        f"`@{bot_name} fix` to one of my review comments, or comment `@{bot_name} fix all` "
        "here."
    )


# Written into every chat answer. Mira's other comments carry markers of
# their own (finding ids, summary anchors), and a bot account whose comment
# carries one is Mira even when the handler does not know the account's name
# — a review-thread reply is given the configured name only.
CHAT_MARKER = "<!-- mira:chat -->"
_MIRA_MARKER_RE = re.compile(r"<!--\s*mira[:-]", re.IGNORECASE)


def _is_mira(message: ConversationMessage, names: list[str]) -> bool:
    login = message.author.lower().removesuffix("[bot]")
    if login in {n.lower() for n in names if n}:
        return True
    return message.is_bot and bool(_MIRA_MARKER_RE.search(message.body or ""))


def conversation_history(
    messages: list[ConversationMessage],
    *,
    names: list[str],
    exclude_id: str = "",
    question: str = "",
    max_messages: int = 20,
    max_chars: int = 12_000,
) -> str:
    """The earlier conversation as plain text, oldest first, newest kept.

    The question being answered is left out — by id when the webhook gave
    one, or as the last message when it matches the question's text. Mira's
    own messages are labelled as such, so the model reads them as what it
    said, not as something a human claimed.
    """
    items = [m for m in messages if not (exclude_id and m.comment_id == str(exclude_id))]
    if not exclude_id and items and question:
        last = " ".join(strip_mentions(items[-1].body, names).split())
        if last == " ".join(question.split()):
            items = items[:-1]
    if max_messages <= 0 or max_chars <= 0:
        return ""

    blocks: list[str] = []
    used = 0
    for message in reversed(items[-max_messages:]):
        body = _HTML_COMMENT_RE.sub("", message.body or "").strip()
        if not body:
            continue
        if len(body) > _MAX_MESSAGE_CHARS:
            body = body[:_MAX_MESSAGE_CHARS].rstrip() + " …(cut)"
        if _is_mira(message, names):
            who = "Mira (you)"
        else:
            who = f"@{message.author or 'unknown'}" + (" (bot)" if message.is_bot else "")
        when = f" at {message.created_at}" if message.created_at else ""
        block = f"{who}{when}:\n{body}"
        if used + len(block) > max_chars:
            break
        blocks.append(block)
        used += len(block) + 2
    return "\n\n".join(reversed(blocks))


async def read_history(
    provider: Any,
    pr_info: Any,
    *,
    names: list[str],
    config: MiraConfig,
    question: str,
    exclude_id: str = "",
    comment_id: str | int = "",
    thread_id: str = "",
) -> str:
    """The conversation the question belongs to, rendered for the prompt. Never raises."""
    chat = config.chat
    if chat.history_messages <= 0 or not hasattr(provider, "get_conversation"):
        return ""
    try:
        messages = await provider.get_conversation(
            pr_info,
            comment_id=comment_id,
            thread_id=thread_id,
            # One spare for the question itself, which is dropped.
            limit=chat.history_messages + 1,
        )
    except Exception as exc:  # noqa: BLE001 — the answer goes ahead without memory
        logger.debug("Could not read the conversation on %s: %s", pr_info.url, exc)
        return ""
    if not isinstance(messages, list):
        return ""
    return conversation_history(
        [m for m in messages if isinstance(m, ConversationMessage)],
        names=names,
        exclude_id=str(exclude_id or ""),
        question=question,
        max_messages=chat.history_messages,
        max_chars=chat.history_chars,
    )


@asynccontextmanager
async def repository_tools(provider: Any, pr_info: Any, config: MiraConfig) -> AsyncIterator[Any]:
    """The review's read-only tool executor, over the pull request's head.

    The same pieces a review assembles (``core/engine.py``): one snapshot
    reader of the head commit, its file list, the code graph and, when the
    repository is indexed, the index search. Yields None when the head cannot
    be read at all, and the chat then answers without tools.
    """
    from mira.index.code_graph import CodeGraph
    from mira.index.context import SnapshotSourceFetcher
    from mira.llm.agentic_tools import AgenticToolExecutor

    ref = getattr(pr_info, "head_sha", "") or getattr(pr_info, "head_branch", "")
    if not ref:
        yield None
        return
    source = SnapshotSourceFetcher(
        provider,
        pr_info,
        ref,
        use_snapshot=config.review.repo_snapshot,
        max_bytes=config.review.repo_snapshot_max_mb * 1024 * 1024,
    )
    source.start()
    try:
        executor = None
        try:
            tree = await source.tree()
            graph = CodeGraph() if config.review.code_graph.enabled else None
            executor = AgenticToolExecutor(
                source_fetcher=source,
                repo_tree=list(tree),
                graph_tools=graph is not None,
                code_graph=graph,
                index_search=_index_search(pr_info),
            )
        except Exception as exc:  # noqa: BLE001 — answer without tools
            logger.info("Chat tools unavailable on %s: %s", pr_info.url, exc)
        yield executor
    finally:
        await source.aclose()


def _index_search(pr_info: Any) -> Any:
    """``grep_index`` for an indexed repository, or None: nothing to search otherwise."""
    from mira.index.store import IndexStore
    from mira.llm.agentic_tools import index_search_for

    platform = getattr(pr_info, "platform", "github")
    try:
        store = IndexStore.open(pr_info.owner, pr_info.repo, platform=platform)
        try:
            indexed = bool(store.all_paths())
        finally:
            store.close()
    except Exception:  # noqa: BLE001
        return None
    return index_search_for(pr_info.owner, pr_info.repo, platform) if indexed else None


async def compose_answer(
    provider: Any,
    pr_info: Any,
    llm: Any,
    *,
    question: str,
    config: MiraConfig,
    bot_name: str,
    history: str = "",
    focus: str = "",
    diff_text: str | None = None,
) -> ChatAnswer:
    """The answer to ``question``: tool loop when on and possible, one call otherwise."""
    if diff_text is None:
        try:
            diff_text = await provider.get_pr_diff(pr_info)
        except Exception as exc:  # noqa: BLE001 — the tools can still read the head
            logger.warning("Could not read the diff of %s for chat: %s", pr_info.url, exc)
            diff_text = ""
    common: dict[str, Any] = {
        "question": question,
        "diff_text": diff_text if isinstance(diff_text, str) else "",
        "pr_title": getattr(pr_info, "title", "") or "",
        "pr_description": getattr(pr_info, "description", "") or "",
        "history": history,
        "focus": focus,
        "bot_name": bot_name,
        "max_diff_chars": config.chat.max_diff_chars,
    }
    one_shot = build_conversation_prompt(**common, tools=False)
    if not config.chat.agentic:
        return await answer_question(llm, one_shot, executor=None, config=config.chat)
    async with repository_tools(provider, pr_info, config) as executor:
        messages = build_conversation_prompt(**common, tools=executor is not None)
        return await answer_question(
            llm, messages, executor=executor, config=config.chat, fallback_messages=one_shot
        )


def _quote(actor: str, question: str) -> str:
    """The ``> @actor asked: …`` line an answer opens with: one line, bounded."""
    first = next((line.strip() for line in question.splitlines() if line.strip()), "")
    if len(first) > 200:
        first = first[:200].rstrip() + "…"
    return f"> @{actor} asked: {first}"


_NO_ANSWER = (
    "I could not produce an answer just now — the model did not respond. Ask again in a moment."
)


async def run_pr_chat(
    provider: Any,
    pr_info: Any,
    question: str,
    *,
    actor: str,
    bot_name: str,
    config: MiraConfig,
    llm: Any,
    bot_identity: str | None = None,
    comment_id: str | int = "",
) -> None:
    """Answer a free-form ``@mira`` comment on the pull request's conversation.

    ``comment_id`` is the comment being answered, kept out of the history it
    would otherwise appear in as its own last message.
    """
    kind = wants_code_change(question) if config.chat.autofix_handoff else None
    if kind:
        await handoff_to_autofix(
            provider, pr_info, actor=actor, kind=kind, bot_name=bot_name, config=config
        )
        return
    names = mention_names(bot_name, bot_identity)
    history = await read_history(
        provider,
        pr_info,
        names=names,
        config=config,
        question=question,
        exclude_id=str(comment_id or ""),
    )
    answer = await compose_answer(
        provider, pr_info, llm, question=question, config=config, bot_name=bot_name, history=history
    )
    await provider.post_comment(
        pr_info, f"{_quote(actor, question)}\n\n{answer.text or _NO_ANSWER}\n\n{CHAT_MARKER}"
    )
    logger.info(
        "Answered @%s on %s (%s, %d lookup(s), %d chars)",
        actor,
        pr_info.url,
        answer.route,
        len(answer.lookups),
        len(answer.text),
    )


async def answer_in_thread(
    provider: Any,
    pr_info: Any,
    question: str,
    *,
    llm: Any,
    config: MiraConfig,
    bot_name: str,
    comment_id: str | int,
    thread_id: str = "",
    path: str = "",
    line: int = 0,
    code_context: str = "",
    finding_text: str = "",
) -> str:
    """The chat's answer to a question asked in a review thread, or "".

    The thread's own messages are the history, and the code the thread sits
    on — already read for the reply classifier — is the focus. "" when the
    chat is off or produced nothing; the caller then posts what it had.
    """
    if not config.chat.agentic:
        return ""
    focus_parts = []
    if path:
        focus_parts.append(f"Review thread on {path}" + (f", line {line}" if line else "") + ".")
    if finding_text:
        focus_parts.append("Mira's comment that opened the thread:\n" + finding_text)
    if code_context:
        focus_parts.append(code_context)
    try:
        history = await read_history(
            provider,
            pr_info,
            names=[bot_name],
            config=config,
            question=question,
            exclude_id=str(comment_id or ""),
            comment_id=comment_id,
            thread_id=thread_id,
        )
        answer = await compose_answer(
            provider,
            pr_info,
            llm,
            question=question,
            config=config,
            bot_name=bot_name,
            history=history,
            focus="\n\n".join(focus_parts),
        )
    except Exception as exc:  # noqa: BLE001 — the classifier's reply still goes out
        logger.warning("Chat answer in a thread on %s failed: %s", pr_info.url, exc)
        return ""
    logger.info(
        "Thread question on %s answered (%s, %d lookup(s))",
        pr_info.url,
        answer.route,
        len(answer.lookups),
    )
    return f"{answer.text}\n\n{CHAT_MARKER}" if answer.text else ""
