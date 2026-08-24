"""Org-wide evaluation analytics over whichever backend is configured.

Postgres holds every repository in one database, so a filter and a `LIMIT`
answer any question directly. SQLite keeps one file per repository, so the
same question means visiting each file and merging. Both paths aggregate
*inside* SQL and only ever carry one page of already-reduced rows in memory --
the Orange Pi profile can't afford to materialize a review history.
"""

from __future__ import annotations

import csv
import io
import json
import logging
import os
import time
from contextlib import contextmanager
from typing import Any

from mira.config import LearningConfig, load_config
from mira.feedback.evaluation import (
    RegressionSuggestion,
    RuleAnalyticsRow,
    detect_regression,
)

logger = logging.getLogger(__name__)

DAY_SECONDS = 86400.0

# Rules per repository pulled before merging in the SQLite path. Rules are
# governed artifacts -- a repo has tens of them, not thousands -- so this is a
# safety valve against a pathological index, not a routine truncation.
_PER_REPO_RULE_CAP = 500


def _postgres_url() -> str:
    url = os.environ.get("DATABASE_URL", "")
    return url if url.startswith(("postgresql://", "postgres://")) else ""


def _repo_targets(owner: str = "", repo: str = "") -> list[tuple[str, str, str]]:
    """(platform, owner, repo) for every SQLite repo store, optionally filtered."""
    from mira.index.store import _INDEX_DIR, _iter_repo_dbs

    index_dir = os.environ.get("MIRA_INDEX_DIR", _INDEX_DIR)
    targets = []
    for platform, db_owner, db_repo, _path in _iter_repo_dbs(index_dir):
        public_owner = db_owner.split("/", 1)[1] if db_owner.startswith("_") else db_owner
        if owner and owner not in (db_owner, public_owner):
            continue
        if repo and repo != db_repo:
            continue
        targets.append((platform, db_owner, db_repo))
    return targets


@contextmanager
def open_analytics_store(owner: str, repo: str, platform: str = "github") -> Any:
    """Open the right store for one repository and always close it."""
    from mira.index.store import IndexStore

    store = IndexStore.open(owner, repo, platform=platform)
    try:
        yield store
    finally:
        store.close()


def _merged_sort_key(row: RuleAnalyticsRow, sort: str) -> Any:
    if sort == "last_exposure_at":
        return row.last_exposure_at
    if sort == "negative":
        return row.counts.negative
    if sort == "positive":
        return row.counts.positive
    if sort == "findings":
        return row.counts.findings
    if sort == "rule_id":
        return row.rule_id
    return row.counts.exposures


def list_rule_analytics(
    *,
    filters: dict[str, Any] | None = None,
    limit: int = 50,
    offset: int = 0,
    sort: str = "exposures",
    descending: bool = True,
) -> tuple[list[RuleAnalyticsRow], int]:
    """One page of per-rule analytics plus the total row count."""
    active = dict(filters or {})
    owner = str(active.get("owner") or "")
    repo = str(active.get("repo") or "")

    if _postgres_url():
        # A single shared database: the store's filters already span every
        # repository, so ask it for exactly the page requested.
        with open_analytics_store(owner or "", repo or "") as store:
            rows = store.aggregate_rule_analytics(
                active, limit=limit, offset=offset, sort=sort, descending=descending
            )
            total = store.count_rule_analytics(active)
        return rows, total

    merged: list[RuleAnalyticsRow] = []
    for platform, db_owner, db_repo in _repo_targets(owner, repo):
        try:
            with open_analytics_store(db_owner, db_repo, platform) as store:
                merged.extend(
                    store.aggregate_rule_analytics(
                        active, limit=_PER_REPO_RULE_CAP, offset=0, sort=sort, descending=descending
                    )
                )
        except Exception:
            logger.exception("Rule analytics failed for %s/%s", db_owner, db_repo)
    merged.sort(key=lambda row: (_merged_sort_key(row, sort), row.rule_id), reverse=descending)
    return merged[offset : offset + limit], len(merged)


def list_rule_evaluations(
    *,
    owner: str,
    repo: str,
    filters: dict[str, Any] | None = None,
    limit: int = 50,
    offset: int = 0,
    outcome: str = "",
) -> tuple[list[dict], int]:
    """The evaluations behind an aggregate, for one repository, paginated."""
    active = dict(filters or {})
    active["owner"] = owner
    active["repo"] = repo
    platform = _platform_for(owner, repo)
    with open_analytics_store(owner, repo, platform) as store:
        rows = store.list_rule_evaluations(active, limit=limit, offset=offset, outcome=outcome)
        total = store.count_rule_evaluations(active, outcome=outcome)
    return rows, total


def _platform_for(owner: str, repo: str) -> str:
    if _postgres_url():
        return "github"
    for platform, db_owner, db_repo in _repo_targets(owner, repo):
        if db_repo == repo:
            return platform
        _ = db_owner
    return "github"


def summarize(
    *,
    dimension: str = "category",
    filters: dict[str, Any] | None = None,
    limit: int = 50,
) -> list[dict]:
    """Outcome mix grouped by one dimension, merged across repositories."""
    active = dict(filters or {})
    owner = str(active.get("owner") or "")
    repo = str(active.get("repo") or "")

    if _postgres_url():
        with open_analytics_store(owner or "", repo or "") as store:
            return store.rule_analytics_summary(active, dimension=dimension, limit=limit)

    accumulator: dict[str, dict] = {}
    numeric_keys: set[str] = set()
    for platform, db_owner, db_repo in _repo_targets(owner, repo):
        try:
            with open_analytics_store(db_owner, db_repo, platform) as store:
                buckets = store.rule_analytics_summary(active, dimension=dimension, limit=limit)
        except Exception:
            logger.exception("Analytics summary failed for %s/%s", db_owner, db_repo)
            continue
        for bucket in buckets:
            name = bucket["bucket"]
            target = accumulator.setdefault(name, {"bucket": name})
            for key, value in bucket.items():
                if key == "bucket" or not isinstance(value, int):
                    continue
                numeric_keys.add(key)
                target[key] = target.get(key, 0) + value

    merged = list(accumulator.values())
    for bucket in merged:
        _recompute_rates(bucket)
        for key in numeric_keys:
            bucket.setdefault(key, 0)
    merged.sort(key=lambda b: b.get("exposures", 0), reverse=True)
    return merged[:limit]


def _recompute_rates(bucket: dict) -> None:
    """Rates never survive a sum -- recompute them from the merged counts."""
    positive = bucket.get("positive", 0)
    negative = bucket.get("negative", 0)
    findings = bucket.get("findings", 0)
    decisive = positive + negative
    bucket["observed"] = positive + negative + bucket.get("neutral", 0)
    bucket["acceptance_rate"] = positive / decisive if decisive else None
    bucket["negative_rate"] = negative / decisive if decisive else None
    bucket["addressed_rate"] = bucket.get("addressed", 0) / findings if findings else None


def compare_activation_periods(
    *,
    owner: str,
    repo: str,
    rule_id: int,
    window_days: int | None = None,
    config: LearningConfig | None = None,
) -> dict:
    """Compare the rule's scope before and after the rule went live.

    The comparison deliberately measures findings *in the rule's scope*, not
    the rule's own exposures: before activation the rule had none, so an
    exposure-based comparison would compare a number against zero and prove
    nothing about whether reviews improved.
    """
    learning = config or load_config().learning
    days = window_days or learning.evaluation_window_days
    window = days * DAY_SECONDS
    platform = _platform_for(owner, repo)
    now = time.time()

    with open_analytics_store(owner, repo, platform) as store:
        rule = store.get_learned_rule(rule_id)
        if rule is None:
            raise LookupError(f"rule {rule_id} not found in {owner}/{repo}")
        pivot = float(rule.effective_from or rule.created_at or 0.0)
        if not pivot:
            return {
                "rule_id": rule_id,
                "owner": owner,
                "repo": repo,
                "window_days": days,
                "activated_at": None,
                # Without an activation timestamp there is no honest pivot to
                # split on, and inventing one would fabricate a comparison.
                "comparable": False,
                "reason": "rule has no recorded activation timestamp",
                "before": None,
                "after": None,
            }
        scope_args = {
            "owner": owner,
            "repo": repo,
            "category": rule.category or "",
            "scope_type": rule.scope_type,
            "scope_value": rule.scope_value or rule.path_pattern or "",
        }
        before = store.rule_period_stats(**scope_args, start=pivot - window, end=pivot)
        after = store.rule_period_stats(**scope_args, start=pivot, end=min(pivot + window, now))

    return {
        "rule_id": rule_id,
        "owner": owner,
        "repo": repo,
        "window_days": days,
        "activated_at": pivot,
        # The "after" window is still filling until a full period has passed;
        # say so rather than letting a partial window read as a verdict.
        "comparable": now >= pivot + window,
        "reason": "" if now >= pivot + window else "after-window is still accumulating",
        "scope": scope_args,
        "before": before,
        "after": after,
        "delta": _period_delta(before, after),
    }


def _period_delta(before: dict, after: dict) -> dict:
    def diff(key: str) -> float | None:
        left, right = before.get(key), after.get(key)
        if left is None or right is None:
            return None
        return right - left

    return {
        "findings": after.get("findings", 0) - before.get("findings", 0),
        "negative": after.get("negative", 0) - before.get("negative", 0),
        "positive": after.get("positive", 0) - before.get("positive", 0),
        "acceptance_rate": diff("acceptance_rate"),
        "addressed_rate": diff("addressed_rate"),
        "negative_rate": diff("negative_rate"),
    }


def regression_suggestions(
    *,
    filters: dict[str, Any] | None = None,
    config: LearningConfig | None = None,
    limit: int = 100,
) -> list[RegressionSuggestion]:
    """Rules whose evidence says they got worse. Advisory only.

    Nothing is disabled here. Phase 3 stops at the suggestion; acting on it is
    an explicit admin decision that lands in the audit log.
    """
    learning = config or load_config().learning
    rows, _total = list_rule_analytics(
        filters=filters, limit=limit, offset=0, sort="negative", descending=True
    )
    suggestions = []
    for row in rows:
        # A rule a human wrote is theirs to keep. Mira reports its numbers but
        # does not propose retiring someone else's deliberate decision.
        if row.origin == "manual":
            continue
        suggestion = detect_regression(
            row,
            min_exposures=learning.min_exposures_for_regression,
            negative_rate_threshold=learning.regression_negative_rate,
            disable_rate_threshold=learning.regression_disable_rate,
        )
        if suggestion is not None:
            suggestions.append(suggestion)
    return suggestions


def record_audit_event(
    *,
    owner: str,
    repo: str,
    event_type: str,
    rule_id: int = 0,
    actor: str = "",
    summary: str = "",
    detail: dict | None = None,
) -> int:
    platform = _platform_for(owner, repo)
    with open_analytics_store(owner, repo, platform) as store:
        return int(
            store.record_learning_audit_event(
                event_type=event_type,
                rule_id=rule_id,
                actor=actor,
                summary=summary,
                detail=detail,
            )
        )


def list_audit_events(
    *,
    owner: str = "",
    repo: str = "",
    rule_id: int = 0,
    limit: int = 100,
    offset: int = 0,
) -> list[dict]:
    if _postgres_url():
        with open_analytics_store(owner or "", repo or "") as store:
            return store.list_learning_audit_events(rule_id=rule_id, limit=limit, offset=offset)

    events: list[dict] = []
    for platform, db_owner, db_repo in _repo_targets(owner, repo):
        try:
            with open_analytics_store(db_owner, db_repo, platform) as store:
                events.extend(
                    store.list_learning_audit_events(
                        rule_id=rule_id, limit=limit + offset, offset=0
                    )
                )
        except Exception:
            logger.exception("Audit listing failed for %s/%s", db_owner, db_repo)
    events.sort(key=lambda e: e.get("created_at", 0.0), reverse=True)
    return events[offset : offset + limit]


_EXPORT_RULE_COLUMNS = (
    "rule_id",
    "owner",
    "repo",
    "platform",
    "origin",
    "version",
    "status",
    "active",
    "category",
    "scope_type",
    "scope_value",
    "rule_text",
    "exposures",
    "review_exposures",
    "findings",
    "observed",
    "positive",
    "negative",
    "neutral",
    "unobserved",
    "addressed",
    "thumbs_up",
    "thumbs_down",
    "reply_agree",
    "reply_disagree",
    "repeated_false_positives",
    "acceptance_rate",
    "addressed_rate",
    "negative_rate",
    "first_exposure_at",
    "last_exposure_at",
)

_EXPORT_EVALUATION_COLUMNS = (
    "id",
    "evaluation_key",
    "review_id",
    "rule_id",
    "rule_version",
    "rule_origin",
    "decision",
    "scope_type",
    "scope_value",
    "category",
    "outcome",
    "addressed",
    "finding_id",
    "finding_title",
    "finding_path",
    "finding_line",
    "finding_severity",
    "finding_state",
    "owner",
    "repo",
    "platform",
    "pr_number",
    "pr_author",
    "pr_url",
    "head_sha",
    "thumbs_up",
    "thumbs_down",
    "reply_agree",
    "reply_disagree",
    "created_at",
)


def rows_to_csv(rows: list[dict], columns: tuple[str, ...]) -> str:
    """Render export rows as CSV with a stable column order."""
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=list(columns), extrasaction="ignore")
    writer.writeheader()
    for row in rows:
        writer.writerow({column: _csv_value(row.get(column)) for column in columns})
    return buffer.getvalue()


def _csv_value(value: Any) -> Any:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (dict, list)):
        return json.dumps(value, sort_keys=True)
    return value


def export_rule_analytics(
    *,
    filters: dict[str, Any] | None = None,
    fmt: str = "json",
    limit: int = 1000,
) -> tuple[str, str]:
    """Export the rule-level table. Returns (body, media_type)."""
    rows, _total = list_rule_analytics(filters=filters, limit=limit, offset=0)
    payload = [row.as_dict() for row in rows]
    if fmt == "csv":
        return rows_to_csv(payload, _EXPORT_RULE_COLUMNS), "text/csv"
    return json.dumps({"rules": payload}, indent=2, sort_keys=True), "application/json"


def export_rule_evaluations(
    *,
    owner: str,
    repo: str,
    filters: dict[str, Any] | None = None,
    fmt: str = "json",
    limit: int = 5000,
    outcome: str = "",
) -> tuple[str, str]:
    """Export the evidence rows behind a rule. Returns (body, media_type)."""
    rows, _total = list_rule_evaluations(
        owner=owner, repo=repo, filters=filters, limit=limit, offset=0, outcome=outcome
    )
    if fmt == "csv":
        return rows_to_csv(rows, _EXPORT_EVALUATION_COLUMNS), "text/csv"
    return json.dumps({"evaluations": rows}, indent=2, sort_keys=True), "application/json"
