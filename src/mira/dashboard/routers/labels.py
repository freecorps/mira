"""Admin-only label workflow editing, copying and side-effect-free preview."""

from typing import Literal

from fastapi import HTTPException, Request
from pydantic import Field

from mira.dashboard import api as _api
from mira.dashboard.api import _require_admin, router
from mira.labels.engine import evaluate, plan
from mira.labels.models import LabelWorkflow, PreviewRequest, PreviewResponse, StrictModel
from mira.labels.presets import presets
from mira.labels.store import load_workflow, save_workflow


class RepoScope(StrictModel):
    platform: Literal["github", "gitlab", "forgejo"] = "github"
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


@router.get("/api/labels/workflow", response_model=LabelWorkflow)
def get_label_workflow(
    request: Request, owner: str, repo: str, platform: str = "github"
) -> LabelWorkflow:
    _require_admin(request)
    require_repo(platform, owner, repo)
    return load_workflow(_api._app_db, platform, owner, repo)


@router.put("/api/labels/workflow", response_model=LabelWorkflow)
def set_label_workflow(
    body: LabelWorkflow, request: Request, owner: str, repo: str, platform: str = "github"
) -> LabelWorkflow:
    _require_admin(request)
    require_repo(platform, owner, repo)
    previous = load_workflow(_api._app_db, platform, owner, repo)
    save_workflow(_api._app_db, platform, owner, repo, body)
    _api._app_db.record_config_audit(
        section="labels",
        actor=request.state.user.username,
        previous={
            "platform": platform,
            "owner": owner,
            "repo": repo,
            "workflow": previous.model_dump(),
        },
        new={"platform": platform, "owner": owner, "repo": repo, "workflow": body.model_dump()},
    )
    return body


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
