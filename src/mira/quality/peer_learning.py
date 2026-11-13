"""Positive learning from what peer review bots caught and Mira missed.

Every rule the learning loop could produce until now was a negation — "do not
report X here" — because its evidence was a human pushing back on a finding.
That loop can make Mira quieter and never sharper. Peer recall supplies the
missing half: a thread another bot opened, that the author then fixed, on
lines where Mira said nothing, is direct evidence of a check Mira should have
made. On this organisation's history that is 297 of Cursor Bugbot's threads
alone, mostly lifecycle, state-machine and cross-file bugs.

For each missed-and-addressed thread (once; the row remembers it):

1. The change that addressed it is read: the compare diff from the commit the
   bot reviewed to the merged head, cut to that file.
2. The review model is shown the bot's comment and that change — both framed
   as untrusted data, since the comment is a third party's text — plus the
   patterns already learned for the repository, and asked for one "Look for …"
   instruction that transfers to other code, or for why there is none.
3. The pattern joins an equivalent candidate when there is one (the model's
   ``same_as`` pick, else the lexical semantic dedup), and evidence is counted
   in *distinct pull requests*: two bots flagging one bug is one miss.
4. At ``peer_recall.min_support`` pull requests (2) the candidate is accepted
   automatically as :data:`~mira.feedback.retrieval.AUTO_ACTOR`, so the
   automatic-rule TTL and per-repository budget apply to it, and becomes a
   positive rule that retrieval renders under "Also check for".

Nothing here posts anywhere. With ``learning.auto_accept`` off, a candidate
that reaches the threshold waits in the pending queue instead.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader

from mira.feedback.models import LearningCandidate
from mira.models import PRInfo
from mira.quality.models import (
    LEARN_ACTIVE,
    LEARN_COLLECTING,
    LEARN_ERROR,
    LEARN_SKIPPED,
    PeerThread,
)

logger = logging.getLogger(__name__)

SYNTHESIZER_VERSION = "peer-recall-v1"

# Existing patterns shown to the model so it can say "this is #12 again".
# Thirty one-sentence rules are a few hundred tokens.
EXISTING_PATTERNS_SHOWN = 30

# Characters of the fixing diff quoted to the model.
DIFF_LIMIT = 6000

_INACTIVE = {"rejected", "retracted", "superseded"}

_ENV = Environment(
    loader=FileSystemLoader(
        str(Path(__file__).resolve().parents[1] / "llm" / "prompts" / "templates")
    ),
    trim_blocks=True,
    lstrip_blocks=True,
)


@dataclass
class LearnResult:
    """What one repository's positive-learning pass did."""

    examined: int = 0
    skipped: int = 0
    collecting: int = 0
    activated: list[int] = field(default_factory=list)
    reinforced: list[int] = field(default_factory=list)
    evicted: list[int] = field(default_factory=list)
    errors: int = 0


def enabled(config: Any) -> bool:
    """Positive learning runs only when every switch it depends on is on."""
    learning = config.learning
    return bool(
        config.peer_recall.positive_learning
        and learning.feedback_v2
        and learning.learning_synthesis
    )


def evidence_key(row: PeerThread) -> str:
    """The unit of independent support: one pull request, whatever bots flagged it."""
    return f"peer-pr:{row.platform}:{row.owner}/{row.repo}#{row.pr_number}"


def positive_candidates(store: Any) -> list[LearningCandidate]:
    """This synthesiser's live candidates, most recently reinforced first."""
    return [
        candidate
        for candidate in store.list_learning_candidates(limit=2000)
        if candidate.synthesizer_version.startswith("peer-recall-")
        and candidate.status not in _INACTIVE
    ]


def normalize_pattern(text: str) -> str:
    """One sentence that reads as an instruction to look, never as a ban."""
    text = " ".join((text or "").split())[:400].strip()
    if not text:
        return ""
    lowered = text.lower()
    if lowered.startswith(("do not", "don't", "never", "avoid", "ignore", "skip")):
        # A negation is the old loop's job; a "look for" rule worded as one
        # would land in the wrong prompt section and read as its opposite.
        return ""
    if not lowered.startswith(("look for", "check for", "check whether", "check that")):
        text = "Look for " + text[0].lower() + text[1:]
    return text if text.endswith(".") else text + "."


async def fixing_diff(
    provider: Any, scope: PRInfo, row: PeerThread, cache: dict[tuple[int, str], str]
) -> str:
    """This file's part of the change between the bot's commit and the merged head."""
    from mira.quality.peers import file_diff

    if not row.original_commit or not row.head_sha or row.original_commit == row.head_sha:
        return ""
    key = (row.pr_number, row.original_commit)
    if key not in cache:
        pr = PRInfo(
            title="",
            description="",
            base_branch="",
            head_branch="",
            url=row.pr_url,
            number=row.pr_number,
            owner=scope.owner,
            repo=scope.repo,
            platform=scope.platform,
            head_sha=row.head_sha,
        )
        try:
            diff = await provider.get_compare_diff(pr, row.original_commit, row.head_sha)
            cache[key] = diff if isinstance(diff, str) else ""
        except Exception as exc:  # noqa: BLE001 - synthesis can proceed without it
            logger.debug("Fixing diff for %s unavailable: %s", row.id, exc)
            cache[key] = ""
    return file_diff(cache[key], row.path, limit=DIFF_LIMIT)


async def synthesize_pattern(
    llm: Any, row: PeerThread, diff: str, existing: list[LearningCandidate]
) -> dict[str, Any]:
    """Ask the review model for a pattern. ``{}`` when it cannot say."""
    from mira.autofix.redact import redact
    from mira.llm import untrusted
    from mira.llm.tool_schemas import PEER_PATTERN_VERDICTS, SUBMIT_REVIEW_PATTERN_TOOL
    from mira.llm.utils import strip_code_fences, strip_think_blocks

    prompt = _ENV.get_template("peer_pattern.jinja2").render(
        comment_block=untrusted.block("COMMENT", row.body or row.title, redactor=redact),
        path_block=untrusted.block("FILE", row.path, redactor=redact),
        line=max(0, int(row.line or 0)),
        diff_block=untrusted.block("DIFF", diff, redactor=redact) if diff else "",
        existing=[{"id": c.id, "category": c.category, "rule_text": c.rule_text} for c in existing],
    )
    try:
        raw = await llm.complete_with_tools(
            messages=[{"role": "user", "content": prompt}],
            tools=[SUBMIT_REVIEW_PATTERN_TOOL],
            temperature=0.0,
        )
        data = json.loads(strip_think_blocks(strip_code_fences(raw))) if raw else {}
    except Exception as exc:  # noqa: BLE001
        logger.warning("Peer pattern synthesis failed for %s: %s", row.id, exc)
        return {}
    if not isinstance(data, dict) or data.get("verdict") not in PEER_PATTERN_VERDICTS:
        return {}
    return data


def _confidence(value: Any) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.0
    return max(0.0, min(number, 1.0)) if number == number else 0.0


def _public_owner(owner: str) -> str:
    return owner.split("/", 1)[1] if owner.startswith("_") and "/" in owner else owner


def _scope_for(row: PeerThread, requested: str) -> tuple[str, str]:
    """Repository by default; the file's language when the model says it is an idiom."""
    from mira.feedback.synthesis import language_for_path

    language = language_for_path(row.path)
    if requested == "language" and language:
        return "language", language
    return "repo", f"{_public_owner(row.owner)}/{row.repo}"


def _example(row: PeerThread) -> dict[str, Any]:
    return {
        "peer_thread_id": row.id,
        "bot": row.bot,
        "pr_number": row.pr_number,
        "path": row.path,
        "line": row.line,
        "url": row.url,
        "title": row.title[:200],
        "addressed": row.addressed_reason,
    }


def record_evidence(
    store: Any,
    row: PeerThread,
    data: dict[str, Any],
    existing: list[LearningCandidate],
) -> LearningCandidate | None:
    """Add this miss to its pattern's candidate (created if new). None if unusable."""
    from mira.feedback.deduplication import find_equivalent_candidate, semantic_fingerprint
    from mira.feedback.synthesis import language_for_path
    from mira.llm.tool_schemas import FINDING_CATEGORIES

    pattern = normalize_pattern(str(data.get("pattern") or ""))
    if not pattern:
        return None
    category = str(data.get("category") or "bug")
    if category not in FINDING_CATEGORIES:
        category = "bug"
    by_id = {candidate.id: candidate for candidate in existing}
    try:
        same_as = int(data.get("same_as") or 0)
    except (TypeError, ValueError):
        same_as = 0
    equivalent = by_id.get(same_as) or find_equivalent_candidate(
        existing, rule_text=pattern, category=category
    )
    rationale = str(data.get("reason") or "").strip()[:500]
    if equivalent is not None:
        # Joining keeps the candidate's identity, wording and scope: each new
        # miss is evidence for the pattern, not a chance to rewrite it.
        fingerprint = equivalent.semantic_fingerprint
        scope_type, scope_value = equivalent.scope_type, equivalent.scope_value
        rule_text, category = equivalent.rule_text, equivalent.category
        rationale = equivalent.rationale or rationale
    else:
        fingerprint = semantic_fingerprint(pattern, category)
        scope_type, scope_value = _scope_for(row, str(data.get("scope_type") or "repo"))
        rule_text = pattern
    candidate = LearningCandidate(
        id=0,
        semantic_fingerprint=fingerprint,
        rule_text=rule_text,
        rationale=rationale
        or f"{row.bot} caught this on #{row.pr_number}, the author fixed it, Mira missed it.",
        scope_type=scope_type,
        scope_value=scope_value,
        category=category,
        language=language_for_path(row.path),
        confidence=_confidence(data.get("confidence")),
        status="collecting",
        synthesizer_version=SYNTHESIZER_VERSION,
        evidence_ids_json=json.dumps([evidence_key(row)]),
        positive_examples_json=json.dumps([_example(row)], sort_keys=True),
        negative_examples_json="[]",
    )
    stored, _created = store.upsert_learning_candidate(candidate)
    return stored


def enforce_positive_budget(store: Any, limit: int) -> list[int]:
    """Disable the least recently reinforced automatic positive rules past ``limit``."""
    from mira.feedback.retrieval import AUTO_ACTOR, is_positive_rule

    rules = [
        rule
        for rule in store.list_learned_rules("approved")
        if rule.active and rule.created_by == AUTO_ACTOR and is_positive_rule(rule)
    ]
    if len(rules) <= limit:
        return []
    rules.sort(key=lambda rule: (rule.updated_at or 0.0, rule.id))
    evicted = rules[: len(rules) - limit]
    for rule in evicted:
        store.set_learned_rule_active(rule.id, False)
    return [rule.id for rule in evicted]


def activate(store: Any, candidate: LearningCandidate, config: Any, result: LearnResult) -> str:
    """Accept a candidate that reached its support, or reinforce its rule."""
    from mira.feedback.autolearn import enforce_auto_rule_budget
    from mira.feedback.lifecycle import approve_candidate
    from mira.feedback.retrieval import AUTO_ACTOR

    learning = config.learning
    settings = config.peer_recall
    if candidate.status in _INACTIVE:
        # An admin (or a retraction) said no; more misses do not overrule that.
        return LEARN_SKIPPED
    if candidate.evidence_count < settings.min_support:
        return LEARN_COLLECTING
    if candidate.status == "approved":
        rules = [
            rule
            for rule in store.list_learned_rules()
            if rule.origin_candidate_id == candidate.id and rule.active
        ]
        for rule in rules:
            # Touching the rule restarts its expiry clock.
            store.set_learned_rule_active(rule.id, True)
            result.reinforced.append(rule.id)
        return LEARN_ACTIVE
    if not learning.auto_accept:
        if candidate.status == "collecting":
            store.set_learning_candidate_status(candidate.id, "pending")
        return LEARN_COLLECTING
    try:
        rule = approve_candidate(
            store,
            candidate.id,
            actor=AUTO_ACTOR,
            config=learning,
            min_evidence=settings.min_support,
        )
    except (LookupError, ValueError) as exc:
        logger.info("Automatic acceptance of peer candidate #%s failed: %s", candidate.id, exc)
        return LEARN_COLLECTING
    result.activated.append(int(rule.id))
    result.evicted.extend(enforce_positive_budget(store, settings.max_positive_rules_per_repo))
    result.evicted.extend(enforce_auto_rule_budget(store, learning.max_auto_rules_per_repo))
    return LEARN_ACTIVE


async def learn_from_misses(
    provider: Any,
    owner: str,
    repo: str,
    quality_store: Any,
    llm: Any,
    config: Any,
    *,
    platform: str = "github",
) -> LearnResult:
    """One pass over this repository's unprocessed missed-and-addressed threads."""
    from mira.quality.backtest import repo_scope
    from mira.quality.models import LEARN_PENDING

    result = LearnResult()
    if not enabled(config) or llm is None:
        return result
    settings = config.peer_recall
    store = quality_store.index_store
    rows = quality_store.list_peer_threads(
        flagged=False,
        addressed=True,
        learning_state=LEARN_PENDING,
        limit=settings.max_syntheses_per_run,
    )
    scope = repo_scope(owner, repo, platform)
    diffs: dict[tuple[int, str], str] = {}
    for row in rows:
        result.examined += 1
        try:
            state, candidate_id = await _learn_one(
                provider, scope, row, store, llm, config, result, diffs
            )
        except Exception:  # noqa: BLE001 - one thread must not stop the pass
            logger.exception("Peer learning failed for %s", row.id)
            state, candidate_id = LEARN_ERROR, 0
            result.errors += 1
        quality_store.set_peer_learning(row.id, state, candidate_id)
        if state == LEARN_SKIPPED:
            result.skipped += 1
        elif state == LEARN_COLLECTING:
            result.collecting += 1
    return result


async def _learn_one(
    provider: Any,
    scope: PRInfo,
    row: PeerThread,
    store: Any,
    llm: Any,
    config: Any,
    result: LearnResult,
    diffs: dict[tuple[int, str], str],
) -> tuple[str, int]:
    settings = config.peer_recall
    diff = await fixing_diff(provider, scope, row, diffs)
    existing = positive_candidates(store)[:EXISTING_PATTERNS_SHOWN]
    data = await synthesize_pattern(llm, row, diff, existing)
    if not data:
        return LEARN_ERROR, 0
    if (
        data.get("verdict") != "pattern"
        or _confidence(data.get("confidence")) < settings.min_confidence
    ):
        return LEARN_SKIPPED, 0
    candidate = record_evidence(store, row, data, existing)
    if candidate is None:
        return LEARN_SKIPPED, 0
    return activate(store, candidate, config, result), int(candidate.id)
