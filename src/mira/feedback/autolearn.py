"""Automatic learning from pushback on review threads.

The first learning loop turned every reply classified as ``disagreement`` into
a pending rule candidate and waited for an admin to approve it. In practice
nobody curated the queue, and close to half of what landed in it was poison:
true findings the author had *fixed*, recorded as false positives because the
reply pushed back on Mira's "not fixed yet" rather than on the finding.

This loop accepts rules automatically, so it earns that by being strict about
what counts as evidence and by undoing itself:

1. **Guards** that need no model veto learning outright: the reply claims a
   fix, the code near the finding changed after it was raised, or the thread
   was already about whether a fix landed. All of those mean the finding was
   probably right.
2. **A judge** on the review model reads the finding, the reply, the thread so
   far and what changed since, and says what the pushback actually is:
   ``premise_wrong`` is the only verdict that can teach a rule. Accepted risk,
   out of scope and fixed-differently close the thread without one.
3. **Acceptance** is automatic for a confident ``premise_wrong`` verdict. The
   rule is attributed to :data:`AUTO_ACTOR`, so retrieval can expire it when no
   new evidence reinforces it, and a per-repository budget keeps the set small.
4. **Retraction**: a later reply saying the finding was not a false positive,
   or that agrees with it, disables every rule learned from that finding.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader

from mira.config import MiraConfig
from mira.feedback.lifecycle import approve_candidate
from mira.feedback.models import FeedbackEventV2, ReviewFinding
from mira.feedback.retrieval import AUTO_ACTOR
from mira.feedback.synthesis import synthesize_candidate

logger = logging.getLogger(__name__)

SYNTHESIZER_VERSION = "autolearn-v1"

# Lines either side of a finding within which a later change counts as having
# touched it. Findings anchor on one line; the fix is often a few lines away.
REGION_WINDOW = 15

# Explicit claims that the code was changed. Deliberately narrow: "já é
# tratado"/"already handled" is exactly how a real false positive is phrased,
# so words that also describe pre-existing handling are left out.
_FIX_CLAIM_RE = re.compile(
    r"\b(?:fixed|fixes|addressed|corrigid[oa]s?|corrigi|arrumad[oa]|arrumei|ajustei"
    r"|(?:in|no|na)\s+(?:the\s+)?(?:last|latest|[úu]ltimo|novo)\s+commit"
    r"|commit\s+`?[0-9a-f]{7,40})\b",
    re.IGNORECASE,
)

# A human correcting an earlier false-positive record.
_NOT_FALSE_POSITIVE_RE = re.compile(
    r"(?:n[ãa]o\s+(?:foi|era|[ée])\s+(?:um\s+)?falso[\s-]+positivo"
    r"|not\s+(?:a\s+)?false[\s-]+positive"
    r"|wasn'?t\s+(?:a\s+)?false[\s-]+positive"
    r"|isn'?t\s+(?:a\s+)?false[\s-]+positive"
    r"|descart[ea]\s+o\s+candidato)",
    re.IGNORECASE,
)

_PT_HINT_RE = re.compile(
    r"\b(?:n[ãa]o|est[áa]|j[áa]|isso|aqui|porque|tamb[ée]m|voc[êe]|c[óo]digo|ent[ãa]o|"
    r"mas|com|para|quando|sem|foi|uma?)\b",
    re.IGNORECASE,
)

_FIX_KINDS = {"reply_agree", "fixed"}

_ENV = Environment(
    loader=FileSystemLoader(
        str(Path(__file__).resolve().parents[1] / "llm" / "prompts" / "templates")
    ),
    trim_blocks=True,
    lstrip_blocks=True,
)


@dataclass
class LearningOutcome:
    """What the loop did with one piece of pushback, and what to tell the human."""

    action: str  # learned | reinforced | pending | declined | retracted | skipped
    reason: str
    verdict: str = ""
    reply: str = ""
    finding_state: str = "dismissed"
    candidate: Any = None
    rule: Any = None
    retracted_rule_ids: list[int] = field(default_factory=list)


def fix_claimed(text: str) -> bool:
    return bool(_FIX_CLAIM_RE.search(text or ""))


def corrects_false_positive(text: str) -> bool:
    return bool(_NOT_FALSE_POSITIVE_RE.search(text or ""))


def looks_portuguese(text: str) -> bool:
    return len(_PT_HINT_RE.findall(text or "")) >= 2


def region_changed_in_diff(diff_text: str, finding: ReviewFinding) -> tuple[bool, str]:
    """Whether a diff from the finding's commit touches the lines around it.

    The finding's line numbers are positions at its own commit, which is the
    *source* side of a diff that starts there. Returns the file's diff text so
    the judge can read what changed.
    """
    from mira.core.diff_parser import parse_diff

    try:
        patch = parse_diff(diff_text)
    except Exception:
        return False, ""
    start = max(1, (finding.start_line or 1) - REGION_WINDOW)
    end = max(finding.end_line or 0, finding.start_line or 0) + REGION_WINDOW
    for file in patch.files:
        if file.path != finding.path and file.old_path != finding.path:
            continue
        text = "\n".join(hunk.content for hunk in file.hunks)
        for hunk in file.hunks:
            hunk_start = hunk.source_start
            hunk_end = hunk.source_start + max(hunk.source_length, 1) - 1
            if hunk_start <= end and hunk_end >= start:
                return True, text
        return False, text
    return False, ""


async def region_changed(
    provider: Any, pr_info: Any, finding: ReviewFinding
) -> tuple[bool | None, str]:
    """``(changed, file_diff)``; ``changed`` is None when it cannot be told."""
    head = getattr(pr_info, "head_sha", "") or ""
    if not finding.head_sha or not head:
        return None, ""
    if finding.head_sha == head:
        return False, ""
    try:
        diff_text = await provider.get_compare_diff(pr_info, finding.head_sha, head)
    except Exception as exc:
        logger.debug("Compare diff for learning guard failed: %s", exc)
        return None, ""
    if not isinstance(diff_text, str):
        return None, ""
    return region_changed_in_diff(diff_text, finding)


def prior_fix_dispute(events: list[FeedbackEventV2], current_id: int) -> bool:
    """The thread was already about a fix before this reply arrived."""
    return any(event.kind in _FIX_KINDS for event in events if event.id != current_id)


def thread_history(events: list[FeedbackEventV2], current_id: int, limit: int = 8) -> str:
    earlier = sorted(
        (event for event in events if event.id != current_id and event.raw_text),
        key=lambda event: (event.created_at, event.id),
    )[-limit:]
    lines: list[str] = []
    for event in earlier:
        lines.append(f"human (@{event.actor or 'unknown'}): {event.raw_text.strip()[:1200]}")
        if event.rationale and not event.source_event_id.startswith("recheck:"):
            lines.append(f"mira: {event.rationale.strip()[:800]}")
    return "\n".join(lines)


async def judge_pushback(
    llm: Any,
    *,
    finding: ReviewFinding,
    finding_text: str,
    human_reply: str,
    history: str,
    code_context: str,
    change_diff: str,
    changed: bool | None,
) -> dict[str, Any]:
    """Ask the review model what the pushback means. ``{}`` when it cannot say."""
    from mira.autofix.redact import redact
    from mira.llm import untrusted
    from mira.llm.tool_schemas import LEARNING_VERDICTS, SUBMIT_LEARNING_VERDICT_TOOL
    from mira.llm.utils import strip_code_fences, strip_think_blocks

    if changed is None:
        change_note = "Unknown: the history since the finding could not be read."
    else:
        change_note = "Nothing near the finding changed since it was raised."
    finding_body = finding_text or f"{finding.title}\n\n{finding.body}"
    prompt = _ENV.get_template("learning_judge.jinja2").render(
        finding_block=untrusted.block("FINDING", finding_body, redactor=redact),
        reply_block=untrusted.block("REPLY", human_reply or "(empty)", redactor=redact),
        history_block=untrusted.block("COMMENT", history, redactor=redact) if history else "",
        path_block=untrusted.block("FILE", finding.path, redactor=redact) if finding.path else "",
        comment_line=max(0, int(finding.start_line or 0)),
        code_block=untrusted.block("DIFF", code_context, redactor=redact) if code_context else "",
        change_block=(
            untrusted.block("DIFF", change_diff[:8000], redactor=redact)
            if changed and change_diff
            else ""
        ),
        change_note=change_note,
    )
    try:
        raw = await llm.complete_with_tools(
            messages=[{"role": "user", "content": prompt}],
            tools=[SUBMIT_LEARNING_VERDICT_TOOL],
            temperature=0.0,
        )
        data = json.loads(strip_think_blocks(strip_code_fences(raw))) if raw else {}
    except Exception as exc:
        logger.warning("Learning judge failed: %s", exc)
        return {}
    if not isinstance(data, dict) or data.get("verdict") not in LEARNING_VERDICTS:
        return {}
    return data


def _confidence(value: Any) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.0
    return max(0.0, min(number, 1.0)) if number == number else 0.0


def _rules_from_candidate(store: Any, candidate_id: int) -> list[Any]:
    return [
        rule
        for rule in store.list_learned_rules()
        if rule.origin_candidate_id == candidate_id and rule.active
    ]


def retract_learning_for_finding(store: Any, finding_id: str) -> list[int]:
    """Disable what was learned from one finding. Returns the rule IDs disabled.

    A candidate whose evidence is wholly this finding is retracted along with
    its rule. One that other findings also support keeps its rule: a single
    correction does not erase independent evidence.
    """
    disabled: list[int] = []
    for candidate in store.list_learning_candidates(limit=2000):
        if candidate.status in {"rejected", "superseded", "retracted"}:
            continue
        try:
            examples = json.loads(candidate.negative_examples_json or "[]")
        except (TypeError, json.JSONDecodeError):
            examples = []
        findings = {
            str(example.get("finding_id"))
            for example in examples
            if isinstance(example, dict) and example.get("finding_id")
        }
        if candidate.source_finding_id:
            findings.add(str(candidate.source_finding_id))
        if finding_id not in findings:
            continue
        if findings - {finding_id}:
            logger.info(
                "Correction on finding %s leaves candidate #%s: other findings support it",
                finding_id,
                candidate.id,
            )
            continue
        for rule in _rules_from_candidate(store, candidate.id):
            store.set_learned_rule_active(rule.id, False)
            disabled.append(rule.id)
        store.set_learning_candidate_status(candidate.id, "retracted")
    return disabled


def enforce_auto_rule_budget(store: Any, limit: int) -> list[int]:
    """Disable the least recently reinforced automatic rules past ``limit``."""
    auto_rules = [
        rule
        for rule in store.list_learned_rules("approved")
        if rule.active and rule.created_by == AUTO_ACTOR
    ]
    if len(auto_rules) <= limit:
        return []
    auto_rules.sort(key=lambda rule: (rule.updated_at or 0.0, rule.id))
    evicted = auto_rules[: len(auto_rules) - limit]
    for rule in evicted:
        store.set_learned_rule_active(rule.id, False)
    return [rule.id for rule in evicted]


def _fallback_reply(verdict: str, human_reply: str) -> str:
    pt = looks_portuguese(human_reply)
    if verdict == "fixed":
        return (
            "Entendi — conto isto como corrigido, não como falso positivo."
            if pt
            else "Understood — I'm counting this as fixed, not as a false positive."
        )
    if verdict in {"accepted_risk", "out_of_scope"}:
        return (
            "Entendi — registrei como decisão do time, sem transformar em regra."
            if pt
            else "Understood — recorded as the team's call, without turning it into a rule."
        )
    return (
        "Entendi o feedback. Não ficou claro que o achado estava errado, então não criei regra."
        if pt
        else "Thanks. It isn't clear the finding was wrong, so I haven't learned a rule from it."
    )


def _learned_suffix(rule: Any, human_reply: str, reinforced: bool) -> str:
    pt = looks_portuguese(human_reply)
    rule_id = getattr(rule, "id", "?")
    text = getattr(rule, "rule_text", "")
    if pt:
        head = "Reforcei a regra" if reinforced else "Aprendi a regra"
        return (
            f"{head} #{rule_id}, que já vale nas próximas reviews: _{text}_\n\n"
            "Se isto não era falso positivo, responda dizendo isso e eu desfaço."
        )
    head = "Reinforced rule" if reinforced else "Learned rule"
    return (
        f"{head} #{rule_id}, active from the next review: _{text}_\n\n"
        "If this was not a false positive, reply saying so and I'll undo it."
    )


def _retracted_reply(rule_ids: list[int], human_reply: str) -> str:
    pt = looks_portuguese(human_reply)
    if rule_ids:
        joined = ", ".join(f"#{rule_id}" for rule_id in rule_ids)
        return (
            f"Entendido — desfiz o registro de falso positivo e desativei a regra {joined}."
            if pt
            else f"Got it — I undid the false-positive record and disabled rule {joined}."
        )
    return (
        "Entendido — não está registrado como falso positivo."
        if pt
        else "Got it — this is not recorded as a false positive."
    )


def retract_for_correction(pr_info: Any, finding: ReviewFinding, *, platform: str) -> list[int]:
    from mira.index.store import IndexStore

    store = IndexStore.open(pr_info.owner, pr_info.repo, platform=platform)
    try:
        return retract_learning_for_finding(store, finding.id)
    finally:
        store.close()


async def learn_from_pushback(
    provider: Any,
    pr_info: Any,
    finding: ReviewFinding,
    event: FeedbackEventV2 | None,
    *,
    human_reply: str,
    finding_text: str,
    code_context: str,
    config: MiraConfig,
    llm: Any,
    platform: str = "github",
) -> LearningOutcome:
    """Decide what a disagreement means, learn from it if it earns it, and say so."""
    from mira.index.store import IndexStore

    learning = config.learning
    if corrects_false_positive(human_reply):
        disabled = retract_for_correction(pr_info, finding, platform=platform)
        return LearningOutcome(
            action="retracted",
            reason="the reply corrects an earlier false-positive record",
            reply=_retracted_reply(disabled, human_reply),
            finding_state="open",
            retracted_rule_ids=disabled,
        )

    store = IndexStore.open(pr_info.owner, pr_info.repo, platform=platform)
    try:
        event_id = event.id if event else 0
        events = store.list_feedback_v2(finding_id=finding.id, limit=100)
        changed, change_diff = await region_changed(provider, pr_info, finding)

        veto = ""
        if fix_claimed(human_reply):
            veto = "the reply says the code was changed"
        elif changed:
            veto = "the code near the finding changed after it was raised"
        elif finding.state in {"fixed", "resolved"} or prior_fix_dispute(events, event_id):
            veto = "the thread was about whether a fix landed"

        data = await judge_pushback(
            llm,
            finding=finding,
            finding_text=finding_text,
            human_reply=human_reply,
            history=thread_history(events, event_id),
            code_context=code_context,
            change_diff=change_diff,
            changed=changed,
        )
        verdict = str(data.get("verdict") or "unclear")
        confidence = _confidence(data.get("confidence"))
        reply = str(data.get("reply") or "").strip()
        reason = str(data.get("reason") or "").strip()
        if verdict == "premise_wrong" and veto:
            verdict = "fixed" if (changed or fix_claimed(human_reply)) else "unclear"
            reason = veto
            reply = ""
        finding_state = "fixed" if verdict == "fixed" else "dismissed"

        if verdict != "premise_wrong":
            return LearningOutcome(
                action="declined",
                reason=reason or veto or "the judge did not find the premise wrong",
                verdict=verdict,
                reply=reply or _fallback_reply(verdict, human_reply),
                finding_state=finding_state,
            )

        rule_text = str(data.get("rule") or "").strip()
        if (
            event is None
            or not event.provenance_complete
            or not rule_text
            or confidence < learning.auto_accept_min_confidence
        ):
            return LearningOutcome(
                action="declined",
                reason="not enough evidence to learn a rule",
                verdict=verdict,
                reply=reply or _fallback_reply("unclear", human_reply),
            )

        candidate, _created = synthesize_candidate(
            store,
            finding,
            event,
            proposal={
                "rule": rule_text,
                "rationale": reason,
                "scope_type": str(data.get("scope_type") or "path"),
                "scope_value": "",
                "confidence": confidence,
            },
            config=learning,
            synthesizer_version=SYNTHESIZER_VERSION,
        )
        if candidate is None:
            return LearningOutcome(
                action="declined",
                reason="learning is disabled",
                verdict=verdict,
                reply=reply or _fallback_reply("unclear", human_reply),
            )
        if candidate.status in {"rejected", "retracted", "superseded"}:
            return LearningOutcome(
                action="declined",
                reason=f"an equivalent rule was {candidate.status} before",
                verdict=verdict,
                reply=reply or _fallback_reply("unclear", human_reply),
                candidate=candidate,
            )
        if not learning.auto_accept:
            return LearningOutcome(
                action="pending",
                reason="automatic acceptance is off",
                verdict=verdict,
                reply=reply,
                candidate=candidate,
            )

        if candidate.status == "approved":
            rules = _rules_from_candidate(store, candidate.id)
            for rule in rules:
                # Touching the rule restarts its expiry clock.
                store.set_learned_rule_active(rule.id, True)
            rule = store.get_learned_rule(rules[0].id) if rules else None
            action = "reinforced"
        else:
            try:
                rule = approve_candidate(store, candidate.id, actor=AUTO_ACTOR, config=learning)
            except (LookupError, ValueError) as exc:
                logger.info("Automatic acceptance of candidate #%s failed: %s", candidate.id, exc)
                return LearningOutcome(
                    action="pending",
                    reason=str(exc),
                    verdict=verdict,
                    reply=reply,
                    candidate=candidate,
                )
            action = "learned"
        evicted = enforce_auto_rule_budget(store, learning.max_auto_rules_per_repo)
        if evicted:
            logger.info("Disabled automatic rules %s over the repository budget", evicted)
        suffix = _learned_suffix(rule, human_reply, action == "reinforced") if rule else ""
        return LearningOutcome(
            action=action,
            reason=reason,
            verdict=verdict,
            reply="\n\n".join(part for part in (reply, suffix) if part),
            candidate=store.get_learning_candidate(candidate.id) or candidate,
            rule=rule,
        )
    finally:
        store.close()
