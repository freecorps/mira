"""Scope-aware retrieval and deterministic precedence for learned rules."""

from __future__ import annotations

import time
from fnmatch import fnmatch
from typing import Any

# Who approved a rule the automatic loop accepted. Kept here, next to the
# retrieval that treats those rules differently, so both sides agree on it.
AUTO_ACTOR = "mira-auto"

_SPECIFICITY = {
    "symbol": 0,
    "path": 1,
    "language": 2,
    "repo": 3,
    "org": 4,
}


def _public_owner(owner: str) -> str:
    """Strip the internal platform namespace used by non-GitHub stores."""
    if owner.startswith("_") and "/" in owner:
        return owner.split("/", 1)[1]
    return owner


def _matches(
    rule: Any,
    paths: list[str],
    languages: list[str],
    symbols: list[str],
    owner: str,
    repo: str,
) -> bool:
    if rule.scope_type == "path":
        pattern = rule.scope_value or rule.path_pattern
        return any(path == pattern or fnmatch(path, pattern) for path in paths)
    if rule.scope_type == "language":
        return rule.scope_value.lower() in {language.lower() for language in languages}
    if rule.scope_type == "symbol":
        return rule.scope_value in set(symbols)
    if rule.scope_type == "repo":
        public_owner = _public_owner(owner)
        return not rule.scope_value or rule.scope_value in {
            repo,
            f"{owner}/{repo}",
            f"{public_owner}/{repo}",
        }
    if rule.scope_type == "org":
        return not rule.scope_value or rule.scope_value in {owner, _public_owner(owner)}
    # Unknown/legacy scope types fail closed. In particular, a category cannot
    # be matched before the model has inferred a finding category.
    return False


def retrieve_rules(
    store: Any,
    *,
    paths: list[str],
    languages: list[str],
    symbols: list[str] | None = None,
    limit: int = 10,
    auto_rule_ttl_days: int | None = None,
    now: float | None = None,
) -> list[Any]:
    owner = getattr(store, "_owner", "")
    repo = getattr(store, "_repo", "")
    rules = [
        rule
        for rule in store.list_active_learned_rules()
        if _matches(rule, paths, languages, symbols or [], owner, repo)
        and not is_expired_auto_rule(rule, auto_rule_ttl_days, now)
    ]
    rules.sort(
        key=lambda rule: (
            0 if rule.source_signal == "manual" else 1,
            _SPECIFICITY.get(rule.scope_type, 99),
            -rule.version,
            -rule.evidence_count,
            -rule.sample_count,
        )
    )
    return rules[:limit]


def is_expired_auto_rule(rule: Any, ttl_days: int | None, now: float | None = None) -> bool:
    """An automatically accepted rule nothing has reinforced within its TTL.

    No human approved these rules, so they must keep earning their place: new
    evidence touches ``updated_at`` and restarts the clock. Rules a person
    approved or wrote never expire here.
    """
    if not ttl_days or getattr(rule, "created_by", "") != AUTO_ACTOR:
        return False
    last = getattr(rule, "updated_at", 0.0) or getattr(rule, "effective_from", 0.0) or 0.0
    if not last:
        return False
    return (now or time.time()) - last > ttl_days * 86400


def render_rule(rule: Any) -> str:
    scope = f"{rule.scope_type}={rule.scope_value}" if rule.scope_value else rule.scope_type
    return f"[{scope}; v{rule.version}] {rule.rule_text}"
