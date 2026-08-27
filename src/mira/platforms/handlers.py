"""Platform-neutral webhook handlers — shared by the GitHub and GitLab
webhook layers. Each takes a provider/auth and operates through the engine;
none is tied to a specific platform's payload shape."""

from __future__ import annotations

import json
import logging
import sqlite3
from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader

from mira.autofix.redact import redact
from mira.config import load_config
from mira.core.diff_parser import parse_diff
from mira.core.engine import ReviewEngine
from mira.core.review_status import tracker as review_tracker
from mira.dashboard.models_config import llm_config_for
from mira.feedback.models import FeedbackEventV2
from mira.feedback.service import (
    create_learning_candidate_for_feedback,
    feedback_ack,
    record_finding_feedback,
    resolve_finding,
    set_finding_state,
)
from mira.feedback.synthesis import synthesize_candidate
from mira.index.store import IndexStore
from mira.llm import create_llm, untrusted
from mira.llm.prompts.review import build_conversation_prompt
from mira.llm.tool_schemas import SUBMIT_FINDING_RECHECK_TOOL, SUBMIT_THREAD_REPLY_TOOL
from mira.llm.utils import strip_code_fences, strip_think_blocks

logger = logging.getLogger(__name__)

_REVIEW_KEYWORDS = {"review", "review this", "review this pr"}

_REJECT_KEYWORDS = {"reject", "dismiss", "resolve", "ignore"}

_REVIEW_REST_KEYWORDS = {"review-rest", "review rest", "rest", "continue"}

_HELP_KEYWORDS = {"help", "?", "commands"}

_THREAD_REPLY_ENV = Environment(
    loader=FileSystemLoader(
        str(Path(__file__).resolve().parents[1] / "llm" / "prompts" / "templates")
    ),
    trim_blocks=True,
    lstrip_blocks=True,
)

_THREAD_REPLY_TEMPLATE = _THREAD_REPLY_ENV.get_template("thread_reply.jinja2")
_THREAD_RECHECK_TEMPLATE = _THREAD_REPLY_ENV.get_template("thread_recheck.jinja2")

PAUSE_LABEL = "mira-paused"

_PAUSE_KEYWORDS = {"pause"}

_RESUME_KEYWORDS = {"resume"}

_MAX_THREAD_CODE_CONTEXT = 6000


async def _thread_code_context(
    provider: Any,
    pr_info: Any,
    path: str,
    line: int,
) -> str:
    """Fetch only the relevant diff hunk for reply classification/synthesis."""
    if not path or not hasattr(provider, "get_pr_diff"):
        return ""
    try:
        diff_text = await provider.get_pr_diff(pr_info)
        if not isinstance(diff_text, str):
            return ""
        file = next((item for item in parse_diff(diff_text).files if item.path == path), None)
        if file is None or not file.hunks:
            return ""
        hunk = next(
            (
                item
                for item in file.hunks
                if item.target_start <= line <= item.target_start + max(item.target_length - 1, 0)
            ),
            file.hunks[0],
        )
        return hunk.content[:_MAX_THREAD_CODE_CONTEXT]
    except Exception as exc:
        logger.debug("Could not load thread code context for %s: %s", path, exc)
        return ""


async def _recheck_finding(
    llm: Any,
    *,
    original_suggestion: str,
    user_reply: str,
    comment_path: str,
    comment_line: int,
    code_context: str,
) -> tuple[bool | None, str]:
    """Look at the code again after the author says they fixed something.

    Returns ``(resolved, reply)`` where ``resolved`` is ``None`` when the check
    could not be made at all. Three answers rather than two, because "I looked
    and it is fixed", "I looked and it is not" and "I could not look" lead to
    three different things happening to the thread — and collapsing the third
    into either of the others is how a thread gets closed on a fix nobody
    verified, or reopened on one that landed.
    """
    prompt = _THREAD_RECHECK_TEMPLATE.render(
        finding_block=untrusted.block("FINDING", original_suggestion or "(none)", redactor=redact),
        reply_block=untrusted.block("REPLY", user_reply or "(empty)", redactor=redact),
        code_block=untrusted.block("DIFF", code_context, redactor=redact) if code_context else "",
        comment_path=comment_path,
        comment_line=comment_line,
    )
    try:
        raw = await llm.complete_with_tools(
            messages=[{"role": "user", "content": prompt}],
            tools=[SUBMIT_FINDING_RECHECK_TOOL],
            temperature=0.0,
        )
        data = json.loads(strip_think_blocks(strip_code_fences(raw))) if raw else {}
    except Exception as exc:
        logger.warning("Finding recheck failed: %s", exc)
        return None, ""
    if "resolved" not in data:
        return None, ""
    return bool(data.get("resolved")), str(data.get("reply", "")).strip()


def _open_store(owner: str, repo: str, platform: str = "github") -> IndexStore:
    """Open an IndexStore for the given owner/repo."""
    return IndexStore.open(owner, repo, platform=platform)


def _help_message(bot_name: str) -> str:
    """Markdown help comment listing every command Mira understands."""
    return (
        f"### Mira commands\n\n"
        f"Mention `@{bot_name}` in a PR comment followed by one of these verbs:\n\n"
        f"| Command | What it does |\n"
        f"|---|---|\n"
        f"| `@{bot_name} review` | Re-run the full review on this PR. Useful after force-pushes or when you want a fresh pass. |\n"
        f"| `@{bot_name} review-rest` | Review files that were skipped on the first pass because the PR was too large. Aliases: `rest`, `continue`. |\n"
        f"| `@{bot_name} pause` | Pause Mira on this PR. No more reviews until you resume. Adds a `mira-paused` label. |\n"
        f"| `@{bot_name} resume` | Resume Mira on a paused PR and re-review the latest diff. |\n"
        f"| `@{bot_name} fix all` | Have Mira write fixes for the most serious open findings on this PR, each on its own branch and stacked PR. Bounded by a configured limit, and the reply lists whatever it left out. Disabled unless a maintainer turned autofix on. |\n"
        f"| `@{bot_name} help` | Show this message. Aliases: `?`, `commands`. |\n"
        f"| `@{bot_name} <anything else>` | Ask a free-form question about the PR. Mira will reply inline using the PR diff as context. |\n\n"
        f"On an inline review comment Mira posted, reply with `@{bot_name} reject` "
        f"(aliases: `dismiss`, `resolve`, `ignore`) to mark the thread resolved and "
        f"record the suggestion as a false positive, or `@{bot_name} fix` to have Mira "
        f"write the change on its own branch. Direct replies do not require a mention.\n\n"
        f"To skip a PR entirely, include `@{bot_name} ignore` in the PR body.\n\n"
        f"Full docs: https://docs.miracode.ai/commands"
    )


async def run_pr_review(
    provider: Any,
    owner: str,
    repo: str,
    number: int,
    pr_url: str,
    is_private: bool,
    bot_name: str,
    platform: str = "github",
    pr_title: str = "",
) -> None:
    """Platform-neutral review core: review a PR/MR and post the result.

    Shared by the GitHub and GitLab webhook handlers — everything here goes
    through the ``provider`` abstraction and the engine, so it's the same for
    every platform.
    """
    repo_full = f"{owner}/{repo}"

    # Atomically claim the slot — avoids stacking redundant runs when
    # two concurrent webhooks arrive. Returns False if already reviewing.
    if not review_tracker.try_start(repo_full, number, pr_title, pr_url):
        logger.info("Review already in progress for %s, skipping", pr_url)
        return

    config = load_config()
    from mira.dashboard.models_config import llm_config_for

    llm = create_llm(llm_config_for("review", config.llm))
    indexing_llm = create_llm(llm_config_for("indexing", config.llm))
    security_llm = create_llm(llm_config_for("security", config.llm))
    engine = ReviewEngine(
        config=config,
        llm=llm,
        provider=provider,
        bot_name=bot_name,
        indexing_llm=indexing_llm,
        security_llm=security_llm,
    )

    from mira.dashboard.api import _app_db

    # Keep visibility current — the blast-radius filter relies on it to avoid
    # naming private repos in a public repo's review.
    try:
        _app_db.set_repo_visibility(owner, repo, is_private, platform=platform)
    except sqlite3.OperationalError as exc:
        logger.debug("set_repo_visibility failed (ignored): %s", exc)

    repo_record = _app_db.get_repo(owner, repo, platform=platform)
    is_indexed = bool(repo_record and repo_record.status == "ready")

    logger.info("Reviewing %s (indexed=%s)", pr_url, is_indexed)
    try:
        result = await engine.review_pr(pr_url)
        review_tracker.complete(repo_full, number)
    except Exception as exc:
        review_tracker.fail(repo_full, number, str(exc))
        raise

    # The walkthrough comment already carries the "more accurate after indexing"
    # nudge for unindexed repos, so we don't post a separate note here — that
    # would repeat on every push.

    logger.info("Review complete for %s", pr_url)

    from mira.models import Severity, build_review_stats
    from mira.outbound_webhooks import (
        REVIEW_COMPLETED,
        REVIEW_HIGH_SEVERITY,
        dispatch_event,
    )

    stats = build_review_stats(result.comments)
    event_data = {
        "repo": repo_full,
        "pr_url": pr_url,
        "number": number,
        "comments": len(result.comments),
        "key_issues": len(result.key_issues),
        "severities": {sev.name.lower(): n for sev, n in stats.items()},
    }
    await dispatch_event(REVIEW_COMPLETED, event_data)
    if any(sev >= Severity.WARNING for sev in stats):
        await dispatch_event(REVIEW_HIGH_SEVERITY, event_data)


async def run_gate_evaluation(
    provider: Any,
    owner: str,
    repo: str,
    number: int,
    pr_url: str,
    bot_name: str,
    platform: str = "github",
) -> None:
    """Re-evaluate the merge gate for a PR without re-running the review.

    A gate decision is only as current as the facts behind it, and the two that
    move without a new commit are CI and labels. Re-evaluating on those events
    is what turns "not approved: CI has not finished" into an answer instead of
    a permanent state — and it costs no LLM call, which is why it can afford to
    happen on every check-suite completion.

    Cheap to call when the gate is off: the policy is resolved first, and an
    inactive gate returns before anything is fetched.
    """
    from mira.gate import service as gate_service
    from mira.gate.policy import resolve_policy

    config = load_config()
    if not resolve_policy(config.gate, owner, repo).active:
        return
    try:
        pr_info = await provider.get_pr_info(pr_url)
        await gate_service.evaluate(provider, pr_info, config=config, bot_name=bot_name)
    except Exception as exc:
        logger.warning("Merge gate re-evaluation failed for %s: %s", pr_url, exc)


async def run_pr_command(
    provider: Any,
    owner: str,
    repo: str,
    number: int,
    pr_url: str,
    question: str,
    actor: str,
    bot_name: str,
    platform: str = "github",
    pr_title: str = "",
) -> None:
    """Platform-neutral handler for an @-mention command on a PR/MR.

    Dispatches help / review / review-rest / free-form Q&A through the provider
    and engine. Shared by the GitHub and GitLab comment handlers.
    """
    repo_full = f"{owner}/{repo}"
    config = load_config()
    from mira.dashboard.models_config import llm_config_for

    llm = create_llm(llm_config_for("review", config.llm))
    indexing_llm = create_llm(llm_config_for("indexing", config.llm))
    security_llm = create_llm(llm_config_for("security", config.llm))

    normalized = question.lower().strip()
    is_review = normalized in _REVIEW_KEYWORDS
    is_review_rest = normalized in _REVIEW_REST_KEYWORDS
    is_help = normalized in _HELP_KEYWORDS

    if is_help:
        pr_info_for_help = await provider.get_pr_info(pr_url)
        await provider.post_comment(pr_info_for_help, _help_message(bot_name))
        logger.info("Help requested on %s by @%s", pr_url, actor)
        return

    if is_review_rest:
        from mira.dashboard.api import _app_db

        progress = _app_db.get_pr_review_progress(owner, repo, number, platform=platform)
        if not progress or not progress.skipped_paths:
            pr_info_for_reply = await provider.get_pr_info(pr_url)
            await provider.post_comment(
                pr_info_for_reply,
                f"> @{actor}: nothing left to review — every file in this "
                "PR has already been covered. 🎉",
            )
            return
        engine = ReviewEngine(
            config=config,
            llm=llm,
            provider=provider,
            bot_name=bot_name,
            indexing_llm=indexing_llm,
            security_llm=security_llm,
        )
        engine._review_only_paths = set(progress.skipped_paths)  # type: ignore[attr-defined]
        if not review_tracker.try_start(repo_full, number, pr_title, pr_url):
            logger.info("Review already in progress for %s, skipping", pr_url)
            return
        logger.info(
            "review-rest on %s by @%s — %d file(s)", pr_url, actor, len(progress.skipped_paths)
        )
        try:
            await engine.review_pr(pr_url)
            review_tracker.complete(repo_full, number)
        except Exception as exc:
            review_tracker.fail(repo_full, number, str(exc))
            raise
    elif is_review:
        engine = ReviewEngine(
            config=config,
            llm=llm,
            provider=provider,
            bot_name=bot_name,
            indexing_llm=indexing_llm,
            security_llm=security_llm,
        )
        if not review_tracker.try_start(repo_full, number, pr_title, pr_url):
            logger.info("Review already in progress for %s, skipping", pr_url)
            return
        logger.info("Re-review triggered for %s by @%s", pr_url, actor)
        try:
            await engine.review_pr(pr_url)
            review_tracker.complete(repo_full, number)
        except Exception as exc:
            review_tracker.fail(repo_full, number, str(exc))
            raise
    else:
        pr_info = await provider.get_pr_info(pr_url)
        diff_text = await provider.get_pr_diff(pr_info)
        messages = build_conversation_prompt(
            question=question,
            diff_text=diff_text,
            pr_title=pr_info.title,
            pr_description=pr_info.description,
        )
        response = await llm.complete(messages, json_mode=False)
        await provider.post_comment(pr_info, f"> @{actor} asked: {question}\n\n{response}")
        logger.info("Replied to comment on %s", pr_url)


async def run_thread_reply(
    provider: Any,
    pr_info: Any,
    human_reply: str,
    comment_id: int,
    *,
    original_suggestion: str = "",
    thread_id: str | None = None,
    comment_node_id: str | None = None,
    comment_path: str = "",
    comment_line: int = 0,
    actor: str = "",
    actor_role: str = "",
    bot_name: str = "miracodeai",
    platform: str = "github",
    parent_comment_id: str | int = "",
    source_event_id: str = "",
    explicit_mention: bool = False,
) -> None:
    """Platform-neutral free-form thread reply with intent classification.

    The LLM classifies the human's message and we respond accordingly:

    ``disagreement``
        The human says the finding is *wrong*. Reply, resolve the thread,
        record a ``rejected`` signal and propose a learning rule.
    ``fixed``
        The human says the finding is *right* and they have changed the code.
        Opposite meaning, opposite outcome — so it must not be classified as
        disagreement, which is what used to happen to most replies and which
        both dismissed a real finding and taught Mira not to raise it again.
        Their word triggers a second look rather than settling the matter: the
        code is rechecked, and only a recheck that comes back resolved closes
        the thread, as ``fixed`` rather than ``dismissed``.
    ``question``
        Answer, leave open.
    ``agreement`` / ``other``
        Acknowledge, leave open.
    """
    config = load_config()
    llm = create_llm(llm_config_for("indexing", config.llm))
    code_context = await _thread_code_context(provider, pr_info, comment_path, comment_line)
    prompt = _THREAD_REPLY_TEMPLATE.render(
        reply_block=untrusted.block("REPLY", human_reply or "(empty)", redactor=redact),
        finding_block=(
            untrusted.block("FINDING", original_suggestion, redactor=redact)
            if original_suggestion
            else ""
        ),
        code_block=untrusted.block("DIFF", code_context, redactor=redact) if code_context else "",
        comment_path=comment_path,
        comment_line=comment_line,
    )
    # Tool calling forces a schema-valid result — more reliable than parsing
    # free-form JSON. The provider's tenacity decorator retries transient fails.
    classification_error = ""
    try:
        raw = await llm.complete_with_tools(
            messages=[{"role": "user", "content": prompt}],
            tools=[SUBMIT_THREAD_REPLY_TOOL],
            temperature=0.0,
        )
        data = json.loads(strip_think_blocks(strip_code_fences(raw))) if raw else {}
    except Exception as exc:
        logger.warning("Free-form thread reply LLM call failed: %s", exc)
        data = {"intent": "other", "reply": ""}
        classification_error = str(exc)

    intent = str(data.get("intent", "other")).lower()
    reply_text = str(data.get("reply", "")).strip()
    kind_by_intent = {
        "disagreement": "reply_disagree",
        # A human saying "fixed" has agreed with the finding, and that is all
        # this event records. Whether it is actually fixed is a separate signal
        # with a separate provenance, recorded below only if a recheck says so
        # — the claim and the verification are different facts.
        "fixed": "reply_agree",
        "agreement": "reply_agree",
        "question": "reply_question",
        "other": "reply_other",
    }
    source_id = source_event_id or f"review-comment:{comment_id}"
    finding, feedback_event, created = record_finding_feedback(
        pr_info,
        kind=kind_by_intent.get(intent, "reply_other"),
        source_event_id=source_id,
        actor=actor,
        actor_role=actor_role,
        raw_text=human_reply,
        rationale=reply_text,
        original_body=original_suggestion,
        platform_comment_id=parent_comment_id,
        platform_thread_id=thread_id or "",
        path=comment_path,
        line=comment_line,
        thread_state="open",
        platform=platform,
        audit={"classification_error": classification_error} if classification_error else None,
    )
    # A reply outside a Mira finding is not ours to answer unless the caller
    # explicitly mentioned the bot (legacy integrations pass no parent ID).
    if not created:
        logger.info("Ignoring duplicate feedback event %s", source_id)
        return
    if finding is None:
        if not explicit_mention:
            logger.debug("Ignoring thread reply without Mira finding provenance")
            return
        if not reply_text:
            logger.warning("Mentioned thread reply was recorded but produced no answer")
            return
        await provider.reply_to_review_comment(pr_info, comment_id, reply_text)
        return

    resolved: bool | None = None
    if intent == "disagreement":
        proposal = data.get("learning")
        candidate, _candidate_created = create_learning_candidate_for_feedback(
            pr_info,
            finding,
            feedback_event,
            proposal=proposal if isinstance(proposal, dict) else None,
            platform=platform,
            config=config.learning,
        )
        reply_text = feedback_ack(candidate, pr_info.owner, pr_info.repo)
    elif intent == "fixed":
        # Look, rather than take their word for it. The reply the classifier
        # wrote is a holding sentence; what gets posted is the result of the
        # check, so the author learns whether the thread actually closed.
        resolved, verdict = await _recheck_finding(
            llm,
            original_suggestion=original_suggestion,
            user_reply=human_reply,
            comment_path=comment_path,
            comment_line=comment_line,
            code_context=code_context,
        )
        if verdict:
            reply_text = verdict
        elif resolved is None:
            reply_text = (
                "Thanks — I could not re-read the code just now, so I have left "
                "this thread open rather than closing it on an unverified fix."
            )
    elif not reply_text:
        logger.warning("Free-form thread reply: empty reply (intent=%s). Recorded only.", intent)
        return

    try:
        await provider.reply_to_review_comment(pr_info, comment_id, reply_text)
    except Exception as exc:
        logger.warning("Failed to post thread reply: %s", exc)
        return

    if intent == "disagreement":
        await _close_thread(
            provider,
            pr_info,
            finding,
            thread_id=thread_id,
            comment_node_id=comment_node_id,
            state="dismissed",
            platform=platform,
        )
    elif intent == "fixed" and resolved:
        # A second event, under its own provenance: the first records that the
        # author claimed a fix, this one records that Mira went and confirmed
        # it. `fixed` is the kind evaluation counts as addressed, and it is
        # earned by the recheck rather than by the claim.
        record_finding_feedback(
            pr_info,
            kind="fixed",
            source_event_id=f"recheck:{source_id}",
            actor=actor,
            actor_role=actor_role,
            raw_text=human_reply,
            rationale=reply_text,
            original_body=original_suggestion,
            platform_comment_id=parent_comment_id,
            platform_thread_id=thread_id or "",
            path=comment_path,
            line=comment_line,
            thread_state="resolved",
            platform=platform,
        )
        await _close_thread(
            provider,
            pr_info,
            finding,
            thread_id=thread_id,
            comment_node_id=comment_node_id,
            state="fixed",
            platform=platform,
        )

    logger.info("Thread reply (%s) on %s: %s", intent, pr_info.url, reply_text[:80])


async def _close_thread(
    provider: Any,
    pr_info: Any,
    finding: Any,
    *,
    thread_id: str | None,
    comment_node_id: str | None,
    state: str,
    platform: str,
) -> None:
    """Resolve the platform thread and record the finding's new state.

    Both halves are best-effort and independent: a platform without thread
    resolution should still record that the finding was settled, and a store
    that refused the write should not stop the thread from closing. Neither is
    allowed to raise, because by this point the reply has already been posted
    and failing here would only produce a duplicate on the retry.
    """
    try:
        tid = thread_id
        if tid is None and comment_node_id:
            tid = await provider.get_thread_id_for_comment(comment_node_id, pr_info)
        if tid:
            await provider.resolve_threads(pr_info, [tid])
    except Exception as exc:
        logger.warning("Failed to resolve %s thread: %s", state, exc)
    try:
        set_finding_state(pr_info, finding.id, state, platform)
    except Exception as exc:
        logger.debug("Failed to record %s state: %s", state, exc)


async def run_pr_merged_learning(
    provider: Any,
    pr_info: Any,
    bot_name: str,
    merged_by: str,
    platform: str = "github",
) -> None:
    """Record merge state without treating silence as positive feedback."""
    owner, repo, number, pr_url = pr_info.owner, pr_info.repo, pr_info.number, pr_info.url
    learning_config = load_config().learning
    store = _open_store(owner, repo, platform)
    unobserved = 0
    fixed = 0
    try:
        legacy_rejected_locations = {
            (event.comment_path, event.comment_line)
            for event in store.list_feedback(limit=2000)
            if event.pr_number == number and event.signal == "rejected"
        }
        try:
            bot_threads = await provider.get_all_bot_threads(pr_info)
        except Exception as exc:
            logger.warning("Failed to fetch bot threads for %s: %s", pr_url, exc)
            bot_threads = []

        for thread in bot_threads:
            if (thread.path, thread.line) in legacy_rejected_locations:
                continue
            finding = resolve_finding(
                store,
                pr_info,
                original_body=thread.body,
                platform_comment_id=thread.platform_comment_id,
                platform_thread_id=thread.thread_id,
                path=thread.path,
                line=thread.line,
                platform=platform,
            )
            if finding is None:
                continue
            for reaction_kind, actors in (
                ("thumbs_up", thread.positive_reactors),
                ("thumbs_down", thread.negative_reactors),
            ):
                for reaction_actor in actors:
                    reaction_event, reaction_created = store.record_feedback_v2(
                        FeedbackEventV2(
                            id=0,
                            finding_id=finding.id,
                            kind=reaction_kind,
                            actor=reaction_actor,
                            actor_role="",
                            raw_text="+1" if reaction_kind == "thumbs_up" else "-1",
                            rationale="GitHub reaction snapshot",
                            platform=platform,
                            source_event_id=(
                                f"reaction-snapshot:{finding.id}:{reaction_actor}:{reaction_kind}"
                            ),
                            head_sha=finding.head_sha,
                            thread_state="resolved" if thread.is_resolved else "open",
                            provenance_complete=False,
                            audit_json=json.dumps(
                                {"platform_comment_id": thread.platform_comment_id},
                                sort_keys=True,
                            ),
                        )
                    )
                    if reaction_kind == "thumbs_down" and reaction_created:
                        try:
                            synthesize_candidate(
                                store,
                                finding,
                                reaction_event,
                                config=learning_config,
                            )
                        except Exception:
                            logger.exception(
                                "Failed to synthesize merge-time learning for finding %s",
                                finding.id,
                            )
            prior = store.list_feedback_v2(finding_id=finding.id, limit=100)
            if any(event.kind in {"thumbs_down", "reply_disagree", "dismissed"} for event in prior):
                store.update_review_finding_state(finding.id, "dismissed")
                continue
            if any(event.kind == "thumbs_up" for event in prior):
                continue
            kind = "fixed" if thread.is_resolved else "unobserved"
            state = "fixed" if thread.is_resolved else "outdated" if thread.is_outdated else "open"
            _event, created = store.record_feedback_v2(
                FeedbackEventV2(
                    id=0,
                    finding_id=finding.id,
                    kind=kind,
                    actor=merged_by,
                    actor_role="merger",
                    raw_text="",
                    rationale=(
                        "thread was resolved before merge"
                        if thread.is_resolved
                        else "PR merged without explicit feedback"
                    ),
                    platform=platform,
                    source_event_id=(
                        f"merge:{number}:{pr_info.head_sha or 'unknown'}:{finding.id}"
                    ),
                    head_sha=finding.head_sha,
                    thread_state=(
                        "resolved"
                        if thread.is_resolved
                        else "outdated"
                        if thread.is_outdated
                        else "open"
                    ),
                    provenance_complete=False,
                    audit_json=json.dumps(
                        {
                            "thread_id": thread.thread_id,
                            "is_resolved": thread.is_resolved,
                            "is_outdated": thread.is_outdated,
                        },
                        sort_keys=True,
                    ),
                )
            )
            if created:
                if kind == "fixed":
                    fixed += 1
                else:
                    unobserved += 1
                store.update_review_finding_state(finding.id, state)
    finally:
        store.close()

    logger.info(
        "PR merged %s: recorded %d unobserved + %d evidence-backed fixed events",
        pr_url,
        unobserved,
        fixed,
    )
