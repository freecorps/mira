"""Recording which rules a review was exposed to, and what they decided.

This runs *after* the review has been posted, so a failure here can change the
analytics but never the review. When ``learning.evaluation_analytics`` is off
nothing in this module is reached at all.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from fnmatch import fnmatch
from typing import Any

from mira.feedback.evaluation import RuleEvaluation, evaluation_key, origin_for_rule

logger = logging.getLogger(__name__)


@dataclass
class ExposedRule:
    """A rule that was placed in front of one review."""

    rule_id: int
    version: int
    origin: str
    scope_type: str
    scope_value: str
    category: str
    rule_text: str
    decision: str = "instruction"


def exposed_rules_from_rows(rows: list[Any]) -> list[ExposedRule]:
    """Snapshot the rules retrieval selected, before anything can edit them."""
    exposed: list[ExposedRule] = []
    for row in rows:
        exposed.append(
            ExposedRule(
                rule_id=int(getattr(row, "id", 0) or 0),
                version=int(getattr(row, "version", 1) or 1),
                origin=origin_for_rule(row),
                scope_type=str(getattr(row, "scope_type", "repo") or "repo"),
                scope_value=str(getattr(row, "scope_value", "") or ""),
                category=str(getattr(row, "category", "") or ""),
                rule_text=str(getattr(row, "rule_text", "") or ""),
            )
        )
    return exposed


@dataclass
class ReviewScope:
    """Per-file facts attribution needs, mirroring what retrieval matched on.

    Retrieval selects a language- or symbol-scoped rule when *any* changed file
    matches. Attribution has to be narrower: only the findings in a file that
    actually carries that language or symbol belong to the rule. Without this,
    a symbol-scoped rule collects feedback from every unrelated finding in the
    review and its acceptance and regression numbers stop meaning anything.
    """

    languages: dict[str, str] = field(default_factory=dict)
    symbols: dict[str, set[str]] = field(default_factory=dict)


def _rule_covers_finding(rule: ExposedRule, path: str, category: str, scope: ReviewScope) -> bool:
    """Whether a produced finding falls inside the rule's declared scope.

    Attribution is scope-based and deliberately mechanical. We cannot know
    which prompt line the model actually leaned on, so we record the honest
    thing: the rule was in the prompt, and this finding is in its scope. The
    drill-down shows exactly that, so nobody has to trust a guess.

    Language and symbol scopes fail closed when we have no metadata for the
    file. Losing a link is recoverable -- the review-scoped exposure is still
    recorded, so the rule never loses the fact that it ran -- while a wrong
    link silently corrupts the rule's score.
    """
    if rule.category and category and rule.category != category:
        return False
    if rule.scope_type == "path" and rule.scope_value:
        return path == rule.scope_value or fnmatch(path, rule.scope_value)
    if rule.scope_type == "language" and rule.scope_value:
        language = scope.languages.get(path, "")
        return bool(language) and language.lower() == rule.scope_value.lower()
    if rule.scope_type == "symbol" and rule.scope_value:
        return rule.scope_value in scope.symbols.get(path, set())
    # repo/org scopes genuinely cover every path in the review; the category
    # check above is what narrows them.
    return True


def build_rule_evaluations(
    exposed: list[ExposedRule],
    *,
    platform: str,
    owner: str,
    repo: str,
    pr_number: int,
    pr_author: str,
    head_sha: str,
    findings: list[Any],
    review_id: int = 0,
    scope: ReviewScope | None = None,
) -> list[RuleEvaluation]:
    """Turn one review's exposures into the rows to persist.

    Produces a review-scoped row per rule (the rule was in play even if it
    produced nothing) plus one row per finding the rule's scope covers.
    """
    review_scope = scope or ReviewScope()
    evaluations: list[RuleEvaluation] = []
    for rule in exposed:
        if not rule.rule_id:
            continue
        # Annotated so the heterogeneous values survive the ** expansion;
        # mypy would otherwise widen them all to `object`.
        common: dict[str, Any] = {
            "review_id": review_id,
            "rule_id": rule.rule_id,
            "rule_version": rule.version,
            "rule_origin": rule.origin,
            "scope_type": rule.scope_type,
            "scope_value": rule.scope_value,
            "category": rule.category,
            "decision": rule.decision,
            "platform": platform,
            "owner": owner,
            "repo": repo,
            "pr_number": pr_number,
            "pr_author": pr_author,
            "head_sha": head_sha,
        }
        evaluations.append(
            RuleEvaluation(
                evaluation_key=evaluation_key(
                    platform=platform,
                    owner=owner,
                    repo=repo,
                    pr_number=pr_number,
                    head_sha=head_sha,
                    rule_id=rule.rule_id,
                    rule_version=rule.version,
                    decision=rule.decision,
                    finding_id=None,
                ),
                finding_id=None,
                detail_json=json.dumps({"rule_text": rule.rule_text[:500]}, sort_keys=True),
                **common,
            )
        )
        for finding in findings:
            finding_id = str(getattr(finding, "finding_id", "") or "")
            if not finding_id:
                continue
            path = str(getattr(finding, "path", "") or "")
            category = str(getattr(finding, "category", "") or "")
            if not _rule_covers_finding(rule, path, category, review_scope):
                continue
            evaluations.append(
                RuleEvaluation(
                    evaluation_key=evaluation_key(
                        platform=platform,
                        owner=owner,
                        repo=repo,
                        pr_number=pr_number,
                        head_sha=head_sha,
                        rule_id=rule.rule_id,
                        rule_version=rule.version,
                        decision=rule.decision,
                        finding_id=finding_id,
                    ),
                    finding_id=finding_id,
                    detail_json=json.dumps({"path": path, "category": category}, sort_keys=True),
                    **common,
                )
            )
    return evaluations


def record_review_exposures(
    store: Any,
    exposed: list[ExposedRule],
    *,
    platform: str,
    owner: str,
    repo: str,
    pr_number: int,
    pr_author: str,
    head_sha: str,
    findings: list[Any],
    review_id: int = 0,
    scope: ReviewScope | None = None,
) -> int:
    """Persist the exposures for one review. Never raises.

    Analytics must not be able to break a review that has already been
    published, so every failure here is logged and swallowed.
    """
    if not exposed:
        return 0
    try:
        evaluations = build_rule_evaluations(
            exposed,
            platform=platform,
            owner=owner,
            repo=repo,
            pr_number=pr_number,
            pr_author=pr_author,
            head_sha=head_sha,
            findings=findings,
            review_id=review_id,
            scope=scope,
        )
        return int(store.record_rule_evaluations(evaluations))
    except Exception:
        logger.exception("Failed to record rule evaluations for %s/%s#%s", owner, repo, pr_number)
        return 0
