"""Label automation listens independently of LLM review filters and switches."""

from __future__ import annotations

import logging
from typing import Any

from mira.labels.service import reconcile
from mira.labels.store import load_workflow

logger = logging.getLogger(__name__)


def schedule_labels(platform: str, event: str, payload: dict, auth: Any, tasks: Any) -> None:
    """Called only after the platform verified the webhook's signature/token.

    Label-only events are deliberately excluded: outputs are never inputs.
    """
    from mira.platforms.index_handlers import _get_app_db

    if platform == "gitlab":
        if event != "Merge Request Hook":
            return
        attrs = payload.get("object_attributes", {})
        action = attrs.get("action")
        changes = payload.get("changes") or {}
        if action not in {"open", "reopen", "update"}:
            return
        if (
            action == "update"
            and not attrs.get("oldrev")
            and not set(changes)
            & {
                "title",
                "description",
                "target_branch",
                "source_branch",
                "draft",
                "work_in_progress",
            }
        ):
            return
        if attrs.get("state") in {"closed", "merged"}:
            return
        full_name = payload.get("project", {}).get("path_with_namespace", "")
        owner, _, repo = full_name.rpartition("/")
        number = attrs.get("iid")
        url = (
            attrs.get("url")
            or f"{payload.get('project', {}).get('web_url', '')}/-/merge_requests/{number}"
        )
    else:
        if event != "pull_request" or payload.get("action") not in {
            "opened",
            "reopened",
            "synchronize",
            "synchronized",
            "edited",
            "ready_for_review",
            "converted_to_draft",
        }:
            return
        pr = payload.get("pull_request", {})
        if pr.get("state") == "closed" or pr.get("merged"):
            return
        repository = payload.get("repository", {})
        full_name = (
            repository.get("full_name")
            or f"{repository.get('owner', {}).get('login', '')}/{repository.get('name', '')}"
        )
        owner, _, repo = full_name.rpartition("/")
        number = pr.get("number")
        url = pr.get("html_url") or f"https://github.com/{owner}/{repo}/pull/{number}"
    if not owner or not repo or not number or not url:
        return
    try:
        db = _get_app_db()
        if db is None or not load_workflow(db, platform, owner, repo).enabled:
            return
        tasks.add_task(
            run_labels,
            platform,
            auth,
            payload.get("installation", {}).get("id", 0),
            owner,
            repo,
            number,
            url,
        )
    except Exception:
        logger.exception("Could not schedule labels for %s/%s", owner, repo)


async def run_labels(
    platform: str, auth: Any, installation_id: int, owner: str, repo: str, number: int, url: str
) -> None:
    from mira.platforms.index_handlers import _get_app_db
    from mira.providers import create_provider

    try:
        token = (
            await auth.get_installation_token(installation_id)
            if platform == "github"
            else await auth.get_token()
        )
        await reconcile(
            create_provider(platform, token), _get_app_db(), platform, owner, repo, number, url
        )
    except Exception:
        # Label failures are visible in Mira logs, but never prevent a review.
        logger.exception("Label automation failed for %s/%s#%s (%s)", owner, repo, number, platform)
