"""Versionable YAML import/export for governed learned rules."""

from __future__ import annotations

from typing import Any

import yaml  # type: ignore[import-untyped]

from mira.feedback.deduplication import semantic_fingerprint
from mira.feedback.lifecycle import validate_rule_scope

FORMAT_VERSION = 1


def _export_scope_value(rule: Any, owner: str, repo: str) -> str:
    if rule.scope_value:
        return rule.scope_value
    if rule.scope_type == "repo":
        return f"{owner}/{repo}"
    if rule.scope_type == "org":
        return owner
    return ""


def export_rules_yaml(store: Any, owner: str, repo: str) -> str:
    rules = [rule for rule in store.list_learned_rules() if rule.status == "approved"]
    payload = {
        "version": FORMAT_VERSION,
        "repository": f"{owner}/{repo}",
        "rules": [
            {
                "rule": rule.rule_text,
                "rationale": rule.rationale,
                "category": rule.category,
                "scope": {
                    "type": rule.scope_type,
                    "value": _export_scope_value(rule, owner, repo),
                },
                "version": rule.version,
                "active": rule.active,
                "evidence_count": rule.evidence_count,
            }
            for rule in rules
        ],
    }
    return yaml.safe_dump(payload, sort_keys=False, allow_unicode=True)


def import_rules_yaml(store: Any, raw: str, *, actor: str = "yaml-import") -> list[Any]:
    try:
        payload = yaml.safe_load(raw) or {}
    except yaml.YAMLError as exc:
        raise ValueError("Invalid learning rules YAML") from exc
    if not isinstance(payload, dict) or payload.get("version") != FORMAT_VERSION:
        raise ValueError(f"Unsupported learning YAML version; expected {FORMAT_VERSION}")
    entries = payload.get("rules")
    if not isinstance(entries, list):
        raise ValueError("YAML must contain a 'rules' list")
    normalized: list[dict[str, Any]] = []
    for entry in entries:
        if not isinstance(entry, dict):
            raise ValueError("Every rule must be a mapping")
        rule_text = str(entry.get("rule") or "").strip()
        if not rule_text:
            raise ValueError("Every rule needs non-empty 'rule' text")
        scope = entry.get("scope") or {}
        if not isinstance(scope, dict):
            raise ValueError("Rule scope must be a mapping")
        scope_type = str(scope.get("type") or "repo")
        scope_value = str(scope.get("value") or "")
        validate_rule_scope(scope_type, scope_value)
        category = str(entry.get("category") or "other")
        active = entry.get("active", True)
        if not isinstance(active, bool):
            raise ValueError("Rule 'active' must be a boolean")
        try:
            version = max(1, int(entry.get("version") or 1))
            evidence_count = max(0, int(entry.get("evidence_count") or 0))
        except (TypeError, ValueError) as exc:
            raise ValueError("Rule version and evidence_count must be integers") from exc
        # Fingerprints are derived server-side so a hand-edited YAML rule
        # cannot retain stale identity or impersonate a different rule.
        fingerprint = semantic_fingerprint(rule_text, category)
        normalized.append(
            {
                "rule_text": rule_text,
                "category": category,
                "scope_type": scope_type,
                "scope_value": scope_value,
                "active": active,
                "version": version,
                "rationale": str(entry.get("rationale") or ""),
                "evidence_count": evidence_count,
                "fingerprint": fingerprint,
            }
        )

    # Validation above completes before the first write, preventing a malformed
    # later entry from leaving a partially imported ruleset.
    created: list[Any] = []
    existing_rules = store.list_learned_rules()
    for entry in normalized:
        duplicate = next(
            (
                rule
                for rule in existing_rules
                if rule.status == "approved"
                and (
                    rule.semantic_fingerprint or semantic_fingerprint(rule.rule_text, rule.category)
                )
                == entry["fingerprint"]
                and rule.scope_type == entry["scope_type"]
                and rule.scope_value == entry["scope_value"]
            ),
            None,
        )
        if duplicate is not None:
            desired_active = entry["active"]
            if duplicate.active != desired_active:
                store.set_learned_rule_active(duplicate.id, desired_active)
            continue
        created.append(
            store.create_learned_rule(
                rule_text=entry["rule_text"],
                category=entry["category"],
                path_pattern=(entry["scope_value"] if entry["scope_type"] == "path" else ""),
                source_signal="manual",
                status="approved",
                active=entry["active"],
                created_by=actor,
                version=entry["version"],
                scope_type=entry["scope_type"],
                scope_value=entry["scope_value"],
                rationale=entry["rationale"],
                evidence_count=entry["evidence_count"],
                semantic_fingerprint=entry["fingerprint"],
            )
        )
        existing_rules.append(created[-1])
    return created
