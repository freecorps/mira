"""Dashboard rules routes"""

from __future__ import annotations

import json
import os

from fastapi import HTTPException, Request, Response

from mira.dashboard import api as _api
from mira.dashboard.api import (
    LearnedRuleActiveInput,
    LearnedRuleInput,
    LearnedRuleModel,
    LearningCandidateInput,
    LearningCandidateModel,
    OrgLearnedRuleModel,
    ReviewContextCreate,
    ReviewContextModel,
    RuleCreate,
    RuleModel,
    _open_store,
    _require_admin,
    router,
)
from mira.index.store import LearnedRuleRow


def _candidate_model(row: dict) -> LearningCandidateModel:
    def parse_list(value: object) -> list:
        if isinstance(value, list):
            return value
        try:
            parsed = json.loads(str(value or "[]"))
        except (TypeError, json.JSONDecodeError):
            return []
        return parsed if isinstance(parsed, list) else []

    evidence_ids = parse_list(row.get("evidence_ids_json"))
    return LearningCandidateModel(
        id=row["id"],
        owner=row["owner"],
        repo=row["repo"],
        rule_text=row["rule_text"],
        rationale=row["rationale"],
        scope_type=row["scope_type"],
        scope_value=row["scope_value"],
        category=row["category"],
        language=row.get("language", ""),
        confidence=row["confidence"],
        status=row["status"],
        synthesizer_version=row["synthesizer_version"],
        evidence_ids=evidence_ids,
        positive_examples=parse_list(row.get("positive_examples_json")),
        negative_examples=parse_list(row.get("negative_examples_json")),
        evidence_count=len(evidence_ids),
        source_finding_id=row.get("source_finding_id"),
        source_feedback_id=row.get("source_feedback_id"),
        created_at=row.get("created_at", 0.0),
        updated_at=row.get("updated_at", 0.0),
    )


def _learned_rule_kwargs(rule: LearnedRuleRow) -> dict:
    return {
        "id": rule.id,
        "rule_text": rule.rule_text,
        "source_signal": rule.source_signal,
        "category": rule.category,
        "path_pattern": rule.path_pattern,
        "sample_count": rule.sample_count,
        "active": rule.active,
        "status": rule.status,
        "created_by": rule.created_by,
        "version": rule.version,
        "scope_type": rule.scope_type,
        "scope_value": rule.scope_value,
        "origin_candidate_id": rule.origin_candidate_id,
        "rationale": rule.rationale,
        "evidence_count": rule.evidence_count,
        "effective_from": rule.effective_from,
        "disabled_at": rule.disabled_at,
        "supersedes_rule_id": rule.supersedes_rule_id,
        "updated_at": rule.updated_at,
    }


@router.get("/api/repos/{owner}/{repo}/context", response_model=list[ReviewContextModel])
def list_context(owner: str, repo: str) -> list[ReviewContextModel]:
    with _open_store(owner, repo) as store:
        entries = store.list_review_context()
        return [
            ReviewContextModel(
                id=e.id,
                title=e.title,
                content=e.content,
                created_at=e.created_at,
                updated_at=e.updated_at,
            )
            for e in entries
        ]


@router.post("/api/repos/{owner}/{repo}/context", response_model=ReviewContextModel)
def create_context(owner: str, repo: str, body: ReviewContextCreate) -> ReviewContextModel:
    with _open_store(owner, repo) as store:
        e = store.upsert_review_context(title=body.title, content=body.content)
        return ReviewContextModel(
            id=e.id,
            title=e.title,
            content=e.content,
            created_at=e.created_at,
            updated_at=e.updated_at,
        )


@router.put("/api/repos/{owner}/{repo}/context/{context_id}", response_model=ReviewContextModel)
def update_context(
    owner: str, repo: str, context_id: int, body: ReviewContextCreate
) -> ReviewContextModel:
    with _open_store(owner, repo) as store:
        existing = store.get_review_context(context_id)
        if not existing:
            raise HTTPException(status_code=404, detail="Context not found")
        e = store.upsert_review_context(
            title=body.title, content=body.content, context_id=context_id
        )
        return ReviewContextModel(
            id=e.id,
            title=e.title,
            content=e.content,
            created_at=e.created_at,
            updated_at=e.updated_at,
        )


@router.delete("/api/repos/{owner}/{repo}/context/{context_id}")
def delete_context(owner: str, repo: str, context_id: int) -> dict:
    with _open_store(owner, repo) as store:
        store.delete_review_context(context_id)
        return {"ok": True}


@router.get(
    "/api/repos/{owner}/{repo}/learned-rules",
    response_model=list[LearnedRuleModel],
)
def list_repo_learned_rules(owner: str, repo: str) -> list[LearnedRuleModel]:
    """Active learned rules synthesized from feedback signals on this repo."""
    with _open_store(owner, repo) as store:
        # Keep this CRUD endpoint repository-local. Organization rules from a
        # sibling repository may affect reviews here, but must be edited using
        # their owning repository ID from the org-wide endpoint.
        rules = [rule for rule in store.list_learned_rules("approved") if rule.active]
        return [LearnedRuleModel(**_learned_rule_kwargs(rule)) for rule in rules]


@router.get("/api/learned-rules", response_model=list[OrgLearnedRuleModel])
def list_org_learned_rules(limit: int = 500, status: str = "") -> list[OrgLearnedRuleModel]:
    """Learned rules across every repo in the org.

    `status` filters by approval state ('pending'|'approved'|'rejected');
    empty returns all so admins can manage the full set.
    """
    db_url = os.environ.get("DATABASE_URL", "")
    capped = max(1, min(limit, 2000))
    status_filter = status or None
    if db_url.startswith("postgresql://") or db_url.startswith("postgres://"):
        from mira.index.pg_store import list_learned_rules_org_wide

        rows = list_learned_rules_org_wide(db_url, limit=capped, status=status_filter)
    else:
        from mira.index.store import list_learned_rules_org_wide_sqlite

        rows = list_learned_rules_org_wide_sqlite(limit=capped, status=status_filter)
    return [
        OrgLearnedRuleModel(
            id=r.get("id", 0),
            owner=r["owner"],
            repo=r["repo"],
            rule_text=r["rule_text"],
            source_signal=r["source_signal"],
            category=r["category"],
            path_pattern=r["path_pattern"],
            sample_count=r["sample_count"],
            active=r.get("active", True),
            status=r.get("status", "approved"),
            created_by=r.get("created_by", ""),
            version=r.get("version", 1),
            scope_type=r.get("scope_type", "repo"),
            scope_value=r.get("scope_value", ""),
            origin_candidate_id=r.get("origin_candidate_id"),
            rationale=r.get("rationale", ""),
            evidence_count=r.get("evidence_count", r.get("sample_count", 0)),
            effective_from=r.get("effective_from", 0.0),
            disabled_at=r.get("disabled_at"),
            supersedes_rule_id=r.get("supersedes_rule_id"),
            updated_at=r["updated_at"] or 0.0,
        )
        for r in rows
    ]


# ── Learnings approval queue + CRUD (admin only) ───────────────────────────
# Auto-synthesized learnings land 'pending' and must be approved by an admin
# before they influence reviews. Admins can also author/edit/delete rules.


@router.get("/api/learning-candidates", response_model=list[LearningCandidateModel])
def list_learning_candidates(limit: int = 500, status: str = "") -> list[LearningCandidateModel]:
    db_url = os.environ.get("DATABASE_URL", "")
    capped = max(1, min(limit, 2000))
    status_filter = status or None
    if db_url.startswith("postgresql://") or db_url.startswith("postgres://"):
        from mira.index.pg_store import list_learning_candidates_org_wide

        rows = list_learning_candidates_org_wide(db_url, limit=capped, status=status_filter)
    else:
        from mira.index.store import list_learning_candidates_org_wide_sqlite

        rows = list_learning_candidates_org_wide_sqlite(limit=capped, status=status_filter)
    return [_candidate_model(row) for row in rows]


@router.get(
    "/api/learning-candidates/{owner}/{repo}/{candidate_id}",
    response_model=LearningCandidateModel,
)
def get_learning_candidate(owner: str, repo: str, candidate_id: int) -> LearningCandidateModel:
    with _open_store(owner, repo) as store:
        candidate = store.get_learning_candidate(candidate_id)
    if candidate is None:
        raise HTTPException(status_code=404, detail="Learning candidate not found")
    return _candidate_model({**candidate.__dict__, "owner": owner, "repo": repo})


@router.put(
    "/api/learning-candidates/{owner}/{repo}/{candidate_id}",
    response_model=LearningCandidateModel,
)
def update_learning_candidate(
    owner: str,
    repo: str,
    candidate_id: int,
    body: LearningCandidateInput,
    request: Request,
) -> LearningCandidateModel:
    _require_admin(request)
    from mira.config import load_config
    from mira.feedback.lifecycle import update_candidate

    try:
        with _open_store(owner, repo) as store:
            candidate = update_candidate(
                store,
                candidate_id,
                rule_text=body.rule_text,
                rationale=body.rationale,
                scope_type=body.scope_type,
                scope_value=body.scope_value,
                category=body.category,
                language=body.language,
                config=load_config().learning,
            )
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return _candidate_model({**candidate.__dict__, "owner": owner, "repo": repo})


@router.post("/api/learning-candidates/{owner}/{repo}/{candidate_id}/approve")
def approve_learning_candidate(owner: str, repo: str, candidate_id: int, request: Request) -> dict:
    _require_admin(request)
    user = request.state.user
    from mira.config import load_config
    from mira.feedback.lifecycle import approve_candidate

    try:
        with _open_store(owner, repo) as store:
            rule = approve_candidate(
                store,
                candidate_id,
                actor=user.username,
                config=load_config().learning,
            )
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"ok": True, "rule_id": rule.id if rule else None}


@router.post("/api/learning-candidates/{owner}/{repo}/{candidate_id}/reject")
def reject_learning_candidate(owner: str, repo: str, candidate_id: int, request: Request) -> dict:
    _require_admin(request)
    from mira.feedback.lifecycle import reject_candidate

    try:
        with _open_store(owner, repo) as store:
            reject_candidate(store, candidate_id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"ok": True}


@router.get("/api/learned-rules/{owner}/{repo}/export.yaml")
def export_learned_rules(owner: str, repo: str, request: Request) -> Response:
    _require_admin(request)
    from mira.feedback.serialization import export_rules_yaml

    with _open_store(owner, repo) as store:
        raw = export_rules_yaml(store, owner, repo)
    return Response(
        content=raw,
        media_type="application/yaml",
        headers={"Content-Disposition": f'attachment; filename="{owner}-{repo}-mira-rules.yaml"'},
    )


@router.post("/api/learned-rules/{owner}/{repo}/import.yaml")
async def import_learned_rules(owner: str, repo: str, request: Request) -> dict:
    _require_admin(request)
    user = request.state.user
    from mira.feedback.serialization import import_rules_yaml

    try:
        raw = (await request.body()).decode("utf-8")
        with _open_store(owner, repo) as store:
            created = import_rules_yaml(store, raw, actor=user.username)
    except (UnicodeDecodeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"ok": True, "imported": len(created)}


@router.get(
    "/api/learned-rules/{owner}/{repo}/{rule_id}",
    response_model=OrgLearnedRuleModel,
)
def get_learned_rule_detail(
    owner: str, repo: str, rule_id: int, request: Request
) -> OrgLearnedRuleModel:
    """Single learned rule — backs the edit page. Readable by any authenticated
    user (so a creator can load their own pending rule to edit)."""
    user = getattr(request.state, "user", None)
    if not user:
        raise HTTPException(status_code=401, detail="Not authenticated")
    is_admin = bool(getattr(user, "is_admin", False))
    username = getattr(user, "username", "") if user else ""
    with _open_store(owner, repo) as store:
        r = store.get_learned_rule(rule_id)
    if not r:
        raise HTTPException(status_code=404, detail="Learning not found")
    if r.status != "approved" and not is_admin and r.created_by != username:
        raise HTTPException(status_code=403, detail="Not allowed to view this learning")
    return OrgLearnedRuleModel(owner=owner, repo=repo, **_learned_rule_kwargs(r))


@router.post("/api/learned-rules/{owner}/{repo}/{rule_id}/approve")
def approve_learned_rule(owner: str, repo: str, rule_id: int, request: Request) -> dict:
    _require_admin(request)
    with _open_store(owner, repo) as store:
        store.set_learned_rule_status(rule_id, "approved")
    return {"ok": True}


@router.post("/api/learned-rules/{owner}/{repo}/{rule_id}/reject")
def reject_learned_rule(owner: str, repo: str, rule_id: int, request: Request) -> dict:
    _require_admin(request)
    with _open_store(owner, repo) as store:
        store.set_learned_rule_status(rule_id, "rejected")
    return {"ok": True}


@router.patch("/api/learned-rules/{owner}/{repo}/{rule_id}/active")
def set_learned_rule_active(
    owner: str, repo: str, rule_id: int, body: LearnedRuleActiveInput, request: Request
) -> dict:
    _require_admin(request)
    with _open_store(owner, repo) as store:
        store.set_learned_rule_active(rule_id, body.active)
    return {"ok": True}


@router.post("/api/learned-rules/{owner}/{repo}", response_model=LearnedRuleModel)
def create_learned_rule(
    owner: str, repo: str, body: LearnedRuleInput, request: Request
) -> LearnedRuleModel:
    # Anyone authenticated may author a learning; admins' land approved, while
    # everyone else's go to the pending queue for an admin to approve.
    user = getattr(request.state, "user", None)
    if not user:
        raise HTTPException(status_code=401, detail="Not authenticated")
    is_admin = bool(getattr(user, "is_admin", False))
    from mira.feedback.deduplication import semantic_fingerprint
    from mira.feedback.lifecycle import validate_rule_scope, validate_rule_text

    scope_type = "path" if body.path_pattern and body.scope_type == "repo" else body.scope_type
    scope_value = body.scope_value or body.path_pattern
    if not scope_value and scope_type == "repo":
        scope_value = f"{owner}/{repo}"
    elif not scope_value and scope_type == "org":
        scope_value = owner
    try:
        validate_rule_text(body.rule_text)
        validate_rule_scope(scope_type, scope_value)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    with _open_store(owner, repo) as store:
        r = store.create_learned_rule(
            rule_text=body.rule_text,
            category=body.category,
            path_pattern=body.path_pattern,
            status="approved" if is_admin else "pending",
            created_by=getattr(user, "username", "") if user else "",
            scope_type=scope_type,
            scope_value=scope_value,
            rationale=body.rationale,
            semantic_fingerprint=semantic_fingerprint(body.rule_text, body.category),
        )
    return LearnedRuleModel(**_learned_rule_kwargs(r))


@router.put("/api/learned-rules/{owner}/{repo}/{rule_id}")
def update_learned_rule(
    owner: str, repo: str, rule_id: int, body: LearnedRuleInput, request: Request
) -> dict:
    # Admins may edit any rule; a non-admin may edit only their own rule while
    # it's still pending approval.
    user = getattr(request.state, "user", None)
    if not user:
        raise HTTPException(status_code=401, detail="Not authenticated")
    is_admin = bool(getattr(user, "is_admin", False))
    username = getattr(user, "username", "") if user else ""
    with _open_store(owner, repo) as store:
        existing = store.get_learned_rule(rule_id)
        if not existing:
            raise HTTPException(status_code=404, detail="Learning not found")
        if not (is_admin or (existing.created_by == username and existing.status == "pending")):
            raise HTTPException(status_code=403, detail="Not allowed to edit this learning")
        scope_type = "path" if body.path_pattern and body.scope_type == "repo" else body.scope_type
        scope_value = body.scope_value or body.path_pattern
        if not scope_value and scope_type == "repo":
            scope_value = f"{owner}/{repo}"
        elif not scope_value and scope_type == "org":
            scope_value = owner
        if existing.status == "approved":
            from mira.config import load_config
            from mira.feedback.lifecycle import version_rule

            try:
                replacement = version_rule(
                    store,
                    rule_id,
                    rule_text=body.rule_text,
                    category=body.category,
                    scope_type=scope_type,
                    scope_value=scope_value,
                    rationale=body.rationale,
                    actor=username,
                    config=load_config().learning,
                )
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
            return {"ok": True, "rule_id": replacement.id, "version": replacement.version}
        from mira.feedback.deduplication import semantic_fingerprint
        from mira.feedback.lifecycle import validate_rule_scope, validate_rule_text

        try:
            validate_rule_text(body.rule_text)
            validate_rule_scope(scope_type, scope_value)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        store.update_learned_rule(
            rule_id,
            body.rule_text,
            body.category,
            body.path_pattern,
            scope_type=scope_type,
            scope_value=scope_value,
            rationale=body.rationale,
            semantic_fingerprint=semantic_fingerprint(body.rule_text, body.category),
        )
    return {"ok": True, "rule_id": rule_id}


@router.delete("/api/learned-rules/{owner}/{repo}/{rule_id}")
def delete_learned_rule(owner: str, repo: str, rule_id: int, request: Request) -> dict:
    _require_admin(request)
    with _open_store(owner, repo) as store:
        store.delete_learned_rule(rule_id)
    return {"ok": True}


@router.get("/api/repos/{owner}/{repo}/rules", response_model=list[RuleModel])
def list_repo_rules(owner: str, repo: str) -> list[RuleModel]:
    with _open_store(owner, repo) as store:
        entries = store.list_review_context()
        return [
            RuleModel(
                id=e.id,
                title=e.title,
                content=e.content,
                enabled=True,
                created_at=e.created_at,
                updated_at=e.updated_at,
            )
            for e in entries
        ]


@router.post("/api/repos/{owner}/{repo}/rules", response_model=RuleModel)
def create_repo_rule(owner: str, repo: str, body: RuleCreate) -> RuleModel:
    with _open_store(owner, repo) as store:
        e = store.upsert_review_context(title=body.title, content=body.content)
        return RuleModel(
            id=e.id,
            title=e.title,
            content=e.content,
            enabled=True,
            created_at=e.created_at,
            updated_at=e.updated_at,
        )


@router.put("/api/repos/{owner}/{repo}/rules/{rule_id}", response_model=RuleModel)
def update_repo_rule(owner: str, repo: str, rule_id: int, body: RuleCreate) -> RuleModel:
    with _open_store(owner, repo) as store:
        existing = store.get_review_context(rule_id)
        if not existing:
            raise HTTPException(status_code=404, detail="Rule not found")
        e = store.upsert_review_context(title=body.title, content=body.content, context_id=rule_id)
        return RuleModel(
            id=e.id,
            title=e.title,
            content=e.content,
            enabled=True,
            created_at=e.created_at,
            updated_at=e.updated_at,
        )


@router.delete("/api/repos/{owner}/{repo}/rules/{rule_id}")
def delete_repo_rule(owner: str, repo: str, rule_id: int) -> dict:
    with _open_store(owner, repo) as store:
        store.delete_review_context(rule_id)
        return {"ok": True}


@router.get("/api/rules/global", response_model=list[RuleModel])
def list_global_rules() -> list[RuleModel]:
    rules = _api._app_db.list_global_rules()
    return [
        RuleModel(
            id=r.id,
            title=r.title,
            content=r.content,
            enabled=r.enabled,
            created_at=r.created_at,
            updated_at=r.updated_at,
        )
        for r in rules
    ]


@router.post("/api/rules/global", response_model=RuleModel)
def create_global_rule(body: RuleCreate) -> RuleModel:
    r = _api._app_db.upsert_global_rule(title=body.title, content=body.content)
    return RuleModel(
        id=r.id,
        title=r.title,
        content=r.content,
        enabled=r.enabled,
        created_at=r.created_at,
        updated_at=r.updated_at,
    )


@router.put("/api/rules/global/{rule_id}", response_model=RuleModel)
def update_global_rule(rule_id: int, body: RuleCreate) -> RuleModel:
    existing = _api._app_db.get_global_rule(rule_id)
    if not existing:
        raise HTTPException(status_code=404, detail="Rule not found")
    r = _api._app_db.upsert_global_rule(title=body.title, content=body.content, rule_id=rule_id)
    return RuleModel(
        id=r.id,
        title=r.title,
        content=r.content,
        enabled=r.enabled,
        created_at=r.created_at,
        updated_at=r.updated_at,
    )


@router.delete("/api/rules/global/{rule_id}")
def delete_global_rule(rule_id: int) -> dict:
    _api._app_db.delete_global_rule(rule_id)
    return {"ok": True}


@router.patch("/api/rules/global/{rule_id}/toggle", response_model=RuleModel)
def toggle_global_rule(rule_id: int) -> RuleModel:
    r = _api._app_db.toggle_global_rule(rule_id)
    if not r:
        raise HTTPException(status_code=404, detail="Rule not found")
    return RuleModel(
        id=r.id,
        title=r.title,
        content=r.content,
        enabled=r.enabled,
        created_at=r.created_at,
        updated_at=r.updated_at,
    )
