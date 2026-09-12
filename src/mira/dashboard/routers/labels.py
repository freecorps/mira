"""Admin-only label workflow editing, copying and side-effect-free preview."""

from typing import Literal

from fastapi import HTTPException, Request
from pydantic import Field

from mira.dashboard import api as _api
from mira.dashboard.api import _require_admin, router
from mira.labels.engine import evaluate, plan
from mira.labels.models import LabelWorkflow, PreviewRequest, PreviewResponse, StrictModel
from mira.labels.presets import presets
from mira.labels.store import load_workflow, scope_key, workflow_revision

Platform = Literal["github", "gitlab", "forgejo"]


class WorkflowSnapshot(StrictModel):
    workflow: LabelWorkflow
    revision: str = Field(pattern=r"^[a-f0-9]{64}$")


class RepoScope(StrictModel):
    platform: Platform = "github"
    owner: str = Field(min_length=1, max_length=500)
    repo: str = Field(min_length=1, max_length=200)


def require_repo(platform: str, owner: str, repo: str) -> None:
    if _api._app_db is None:
        raise HTTPException(503, "The repository registry is unavailable")
    if not _api._app_db.get_repo(owner, repo, platform=platform):
        raise HTTPException(404, "Repository not found")


@router.get("/api/labels/presets")
def label_presets(request: Request) -> list[dict]:
    _require_admin(request)
    return presets()


@router.get("/api/labels/workflow", response_model=WorkflowSnapshot)
def get_label_workflow(
    request: Request, owner: str, repo: str, platform: Platform = "github"
) -> WorkflowSnapshot:
    _require_admin(request)
    require_repo(platform, owner, repo)
    raw = _api._app_db.get_setting("label_workflow:" + scope_key(platform, owner, repo))
    return WorkflowSnapshot(
        workflow=LabelWorkflow.model_validate_json(raw) if raw else LabelWorkflow(),
        revision=workflow_revision(raw),
    )


@router.put("/api/labels/workflow", response_model=WorkflowSnapshot)
def set_label_workflow(
    body: WorkflowSnapshot, request: Request, owner: str, repo: str, platform: Platform = "github"
) -> WorkflowSnapshot:
    _require_admin(request)
    require_repo(platform, owner, repo)
    key = "label_workflow:" + scope_key(platform, owner, repo)
    raw = _api._app_db.get_setting(key)
    previous = LabelWorkflow.model_validate_json(raw) if raw else LabelWorkflow()
    updated = body.workflow.model_dump_json()
    if body.revision != workflow_revision(raw) or not _api._app_db.compare_and_set_setting(
        key, raw, updated
    ):
        raise HTTPException(
            409,
            "These rules were changed by another administrator. Your draft has not been saved. "
            "Copy your changes before reloading to review the latest rules.",
        )
    _api._app_db.record_config_audit(
        section="labels",
        actor=request.state.user.username,
        previous={
            "platform": platform,
            "owner": owner,
            "repo": repo,
            "workflow": previous.model_dump(),
        },
        new={
            "platform": platform,
            "owner": owner,
            "repo": repo,
            "workflow": body.workflow.model_dump(),
        },
    )
    return WorkflowSnapshot(workflow=body.workflow, revision=workflow_revision(updated))


@router.post("/api/labels/preview", response_model=PreviewResponse)
def preview_label_workflow(body: PreviewRequest, request: Request) -> PreviewResponse:
    _require_admin(request)
    return plan(evaluate(body.workflow, body.facts), body.current_labels)


@router.post("/api/labels/copy", response_model=LabelWorkflow)
def copy_label_workflow(body: RepoScope, request: Request) -> LabelWorkflow:
    """Copy into the editor as a disabled draft; saving selects the destination."""
    _require_admin(request)
    require_repo(body.platform, body.owner, body.repo)
    workflow = load_workflow(_api._app_db, body.platform, body.owner, body.repo)
    return workflow.model_copy(update={"enabled": False})
