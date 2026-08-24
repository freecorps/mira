"""Dashboard routes for Phase 3 rule evaluation analytics.

Everything here is admin-only. The evaluation history spans every repository
in the install and carries finding titles, PR authors and reviewer reactions --
that is governance data, not per-repo browsing data, so it sits behind the same
gate as rule approval itself.
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from contextlib import contextmanager

from fastapi import HTTPException, Request, Response
from pydantic import BaseModel

from mira.config import load_config
from mira.dashboard.api import _require_admin, router
from mira.feedback import analytics
from mira.feedback.analytics import PlatformResolutionError
from mira.feedback.evaluation import DECISIONS, DETAIL_OUTCOMES, ORIGINS

# Page sizes are capped rather than trusted: the Orange Pi profile is the
# reason this whole feature paginates in the first place.
_MAX_PAGE = 200
_MAX_EXPORT_RULES = 5000
_MAX_EXPORT_EVALUATIONS = 20000

_SORT_KEYS = {"exposures", "negative", "positive", "findings", "last_exposure_at", "rule_id"}
_SUMMARY_DIMENSIONS = {"category", "repo", "owner", "author", "scope_type", "origin", "decision"}


class RuleAnalyticsPage(BaseModel):
    rules: list[dict]
    total: int
    limit: int
    offset: int


class EvaluationPage(BaseModel):
    evaluations: list[dict]
    total: int
    limit: int
    offset: int


class SummaryResponse(BaseModel):
    dimension: str
    buckets: list[dict]


class RegressionResponse(BaseModel):
    suggestions: list[dict]
    min_exposures: int
    negative_rate_threshold: float
    disable_rate_threshold: float


class RegressionAckInput(BaseModel):
    action: str
    note: str = ""


class AuditEventsResponse(BaseModel):
    events: list[dict]


def _page(limit: int, offset: int) -> tuple[int, int]:
    return max(1, min(limit, _MAX_PAGE)), max(0, offset)


def _filters(
    owner: str,
    repo: str,
    category: str,
    origin: str,
    decision: str,
    scope_type: str,
    pr_author: str,
    since: float,
    until: float,
    rule_id: int = 0,
) -> dict:
    if origin and origin not in ORIGINS:
        raise HTTPException(status_code=400, detail=f"origin must be one of {sorted(ORIGINS)}")
    if decision and decision not in DECISIONS:
        raise HTTPException(status_code=400, detail=f"decision must be one of {sorted(DECISIONS)}")
    return {
        "owner": owner,
        "repo": repo,
        "category": category,
        "origin": origin,
        "decision": decision,
        "scope_type": scope_type,
        "pr_author": pr_author,
        "since": since,
        "until": until,
        "rule_id": rule_id,
    }


@contextmanager
def _resolved_platform() -> Iterator[None]:
    """Surface an unreachable repo registry as 503, never as an empty result.

    Analytics exists to make evidence auditable, so "we could not look" must
    never be rendered as "there is nothing".
    """
    try:
        yield
    except PlatformResolutionError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


def _actor(request: Request) -> str:
    user = getattr(request.state, "user", None)
    return str(getattr(user, "username", "") or "")


@router.get("/api/analytics/rules", response_model=RuleAnalyticsPage)
def list_rule_analytics(
    request: Request,
    owner: str = "",
    repo: str = "",
    category: str = "",
    origin: str = "",
    decision: str = "",
    scope_type: str = "",
    pr_author: str = "",
    since: float = 0.0,
    until: float = 0.0,
    sort: str = "exposures",
    order: str = "desc",
    limit: int = 50,
    offset: int = 0,
) -> RuleAnalyticsPage:
    """Per-rule exposures and outcomes, filtered and paginated."""
    _require_admin(request)
    if sort not in _SORT_KEYS:
        raise HTTPException(status_code=400, detail=f"sort must be one of {sorted(_SORT_KEYS)}")
    limit, offset = _page(limit, offset)
    with _resolved_platform():
        rows, total = analytics.list_rule_analytics(
            filters=_filters(
                owner, repo, category, origin, decision, scope_type, pr_author, since, until
            ),
            limit=limit,
            offset=offset,
            sort=sort,
            descending=order != "asc",
        )
    return RuleAnalyticsPage(
        rules=[row.as_dict() for row in rows], total=total, limit=limit, offset=offset
    )


@router.get("/api/analytics/rules/{owner}/{repo}/{rule_id}", response_model=dict)
def rule_analytics_detail(request: Request, owner: str, repo: str, rule_id: int) -> dict:
    """One rule: where it ran, what came back, and whether it helped."""
    _require_admin(request)
    with _resolved_platform():
        rows, _total = analytics.list_rule_analytics(
            filters={"owner": owner, "repo": repo, "rule_id": rule_id}, limit=1, offset=0
        )
    if not rows:
        raise HTTPException(
            status_code=404, detail=f"No recorded evaluations for rule {rule_id} in {owner}/{repo}"
        )
    learning = load_config().learning
    row = rows[0]
    comparison = analytics.compare_activation_periods(
        owner=owner,
        repo=repo,
        rule_id=rule_id,
        config=learning,
        # The evaluations are the audit record. If the rule row was deleted the
        # history must still open, just without a comparison to anchor.
        fallback_scope={
            "category": row.category,
            "scope_type": row.scope_type,
            "scope_value": row.scope_value,
        },
    )
    from mira.feedback.evaluation import detect_regression

    suggestion = None
    if row.origin != "manual":
        suggestion = detect_regression(
            row,
            min_exposures=learning.min_exposures_for_regression,
            negative_rate_threshold=learning.regression_negative_rate,
            disable_rate_threshold=learning.regression_disable_rate,
        )
    return {
        "rule": row.as_dict(),
        "period_comparison": comparison,
        "regression": suggestion.as_dict() if suggestion else None,
        "min_exposures_for_regression": learning.min_exposures_for_regression,
    }


@router.get(
    "/api/analytics/rules/{owner}/{repo}/{rule_id}/evaluations", response_model=EvaluationPage
)
def list_rule_evaluations(
    request: Request,
    owner: str,
    repo: str,
    rule_id: int,
    outcome: str = "",
    decision: str = "",
    pr_author: str = "",
    since: float = 0.0,
    until: float = 0.0,
    limit: int = 50,
    offset: int = 0,
) -> EvaluationPage:
    """The individual evaluations behind a rule's aggregate numbers."""
    _require_admin(request)
    if outcome and outcome not in DETAIL_OUTCOMES:
        raise HTTPException(
            status_code=400, detail=f"outcome must be one of {sorted(DETAIL_OUTCOMES)}"
        )
    limit, offset = _page(limit, offset)
    with _resolved_platform():
        rows, total = analytics.list_rule_evaluations(
            owner=owner,
            repo=repo,
            filters=_filters(
                "", "", "", "", decision, "", pr_author, since, until, rule_id=rule_id
            ),
            limit=limit,
            offset=offset,
            outcome=outcome,
        )
    return EvaluationPage(evaluations=rows, total=total, limit=limit, offset=offset)


@router.get("/api/analytics/summary", response_model=SummaryResponse)
def analytics_summary(
    request: Request,
    dimension: str = "category",
    owner: str = "",
    repo: str = "",
    category: str = "",
    origin: str = "",
    pr_author: str = "",
    since: float = 0.0,
    until: float = 0.0,
    limit: int = 50,
) -> SummaryResponse:
    """Outcome mix grouped by category, repo, owner, author, scope or origin."""
    _require_admin(request)
    if dimension not in _SUMMARY_DIMENSIONS:
        raise HTTPException(
            status_code=400, detail=f"dimension must be one of {sorted(_SUMMARY_DIMENSIONS)}"
        )
    with _resolved_platform():
        buckets = analytics.summarize(
            dimension=dimension,
            filters=_filters(owner, repo, category, origin, "", "", pr_author, since, until),
            limit=max(1, min(limit, _MAX_PAGE)),
        )
    return SummaryResponse(dimension=dimension, buckets=buckets)


@router.get("/api/analytics/regressions", response_model=RegressionResponse)
def list_regressions(
    request: Request, owner: str = "", repo: str = "", limit: int = 100
) -> RegressionResponse:
    """Rules whose evidence says they regressed. Advisory only.

    Nothing is disabled by listing this. Acting on a suggestion is a separate,
    audited admin action.
    """
    _require_admin(request)
    learning = load_config().learning
    with _resolved_platform():
        suggestions = analytics.regression_suggestions(
            filters={"owner": owner, "repo": repo},
            config=learning,
            limit=max(1, min(limit, _MAX_PAGE)),
        )
    return RegressionResponse(
        suggestions=[s.as_dict() for s in suggestions],
        min_exposures=learning.min_exposures_for_regression,
        negative_rate_threshold=learning.regression_negative_rate,
        disable_rate_threshold=learning.regression_disable_rate,
    )


@router.post("/api/analytics/regressions/{owner}/{repo}/{rule_id}/ack", response_model=dict)
def acknowledge_regression(
    request: Request, owner: str, repo: str, rule_id: int, body: RegressionAckInput
) -> dict:
    """Record what an admin decided about a regression suggestion.

    This writes an audit entry and nothing else. Disabling or versioning the
    rule stays a deliberate call against the existing rule endpoints, so the
    trail always shows a human made it.
    """
    _require_admin(request)
    allowed = {"accepted", "dismissed", "deferred"}
    if body.action not in allowed:
        raise HTTPException(status_code=400, detail=f"action must be one of {sorted(allowed)}")
    with _resolved_platform():
        event_id = _record_ack(request, owner, repo, rule_id, body)
    return {"ok": True, "event_id": event_id}


def _record_ack(
    request: Request, owner: str, repo: str, rule_id: int, body: RegressionAckInput
) -> int:
    return analytics.record_audit_event(
        owner=owner,
        repo=repo,
        event_type=f"regression_{body.action}",
        rule_id=rule_id,
        actor=_actor(request),
        summary=f"Regression suggestion {body.action} for rule {rule_id}",
        detail={"note": body.note, "acknowledged_at": time.time()},
    )


@router.get("/api/analytics/audit", response_model=AuditEventsResponse)
def list_audit_events(
    request: Request,
    owner: str = "",
    repo: str = "",
    rule_id: int = 0,
    limit: int = 100,
    offset: int = 0,
) -> AuditEventsResponse:
    _require_admin(request)
    limit, offset = _page(limit, offset)
    with _resolved_platform():
        events = analytics.list_audit_events(
            owner=owner, repo=repo, rule_id=rule_id, limit=limit, offset=offset
        )
    return AuditEventsResponse(events=events)


def _export_response(body: str, media_type: str, filename: str) -> Response:
    return Response(
        content=body,
        media_type=media_type,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/api/analytics/export")
def export_rule_analytics(
    request: Request,
    fmt: str = "json",
    owner: str = "",
    repo: str = "",
    category: str = "",
    origin: str = "",
    pr_author: str = "",
    since: float = 0.0,
    until: float = 0.0,
    limit: int = 1000,
) -> Response:
    """Export the rule-level analytics table as CSV or JSON."""
    _require_admin(request)
    if fmt not in {"csv", "json"}:
        raise HTTPException(status_code=400, detail="fmt must be 'csv' or 'json'")
    with _resolved_platform():
        body, media_type = analytics.export_rule_analytics(
            filters=_filters(owner, repo, category, origin, "", "", pr_author, since, until),
            fmt=fmt,
            limit=max(1, min(limit, _MAX_EXPORT_RULES)),
        )
    return _export_response(body, media_type, f"mira-rule-analytics.{fmt}")


@router.get("/api/analytics/rules/{owner}/{repo}/{rule_id}/export")
def export_rule_evaluations(
    request: Request,
    owner: str,
    repo: str,
    rule_id: int,
    fmt: str = "json",
    outcome: str = "",
    limit: int = 5000,
) -> Response:
    """Export the evidence rows behind one rule, so a number can be checked."""
    _require_admin(request)
    if fmt not in {"csv", "json"}:
        raise HTTPException(status_code=400, detail="fmt must be 'csv' or 'json'")
    if outcome and outcome not in DETAIL_OUTCOMES:
        raise HTTPException(
            status_code=400, detail=f"outcome must be one of {sorted(DETAIL_OUTCOMES)}"
        )
    with _resolved_platform():
        body, media_type = analytics.export_rule_evaluations(
            owner=owner,
            repo=repo,
            filters={"rule_id": rule_id},
            fmt=fmt,
            limit=max(1, min(limit, _MAX_EXPORT_EVALUATIONS)),
            outcome=outcome,
        )
    return _export_response(body, media_type, f"mira-rule-{rule_id}-evaluations.{fmt}")
