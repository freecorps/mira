"""GitHub's side of the review queue: requests from webhooks, and a runner."""

from __future__ import annotations

import logging
from typing import Any

from mira.core.commit_status import STATUS_CONTEXT, CommitStatus
from mira.core.review_queue import RunResult
from mira.dashboard.db import ReviewRequest
from mira.platforms.github.auth import GitHubAppAuth
from mira.platforms.queued_review import execute_review_request, pr_info_for, publish_status
from mira.providers import create_provider

logger = logging.getLogger(__name__)

# Bounds on the startup scan for checks left in progress: it costs one call
# per repository and one per open pull request, every boot.
_ORPHAN_SCAN_REPOS = 200
_ORPHAN_SCAN_PRS_PER_REPO = 50


def request_from_pull_request(
    payload: dict[str, Any], pull_request: dict[str, Any], *, reason: str, head_sha: str = ""
) -> ReviewRequest:
    """A review request for one pull request named in a webhook payload."""
    repository = payload.get("repository") or {}
    owner = (repository.get("owner") or {}).get("login", "")
    repo = repository.get("name", "")
    number = int(pull_request.get("number") or 0)
    head = pull_request.get("head") or {}
    base = pull_request.get("base") or {}
    return ReviewRequest(
        platform="github",
        owner=owner,
        repo=repo,
        pr_number=number,
        installation_id=int((payload.get("installation") or {}).get("id") or 0),
        head_sha=head_sha or str(head.get("sha") or ""),
        base_ref=str(base.get("ref") or ""),
        head_ref=str(head.get("ref") or ""),
        pr_title=str(pull_request.get("title") or ""),
        pr_url=f"https://github.com/{owner}/{repo}/pull/{number}",
        is_private=bool(repository.get("private", False)),
        reason=reason,
        actor=str((payload.get("sender") or {}).get("login") or ""),
    )


class GitHubReviewPlatform:
    """Runs queued GitHub reviews with a fresh installation token each time.

    A token is minted per request rather than held: an installation token
    lasts an hour, and a request can wait longer than that in a deep queue.
    """

    def __init__(self, app_auth: GitHubAppAuth, bot_name: str, db: Any = None) -> None:
        self._auth = app_auth
        self._bot_name = bot_name
        self._db = db

    def _app_db(self) -> Any:
        if self._db is not None:
            return self._db
        from mira.dashboard.api import _app_db

        return _app_db

    async def _provider(self, installation_id: int) -> Any:
        token = await self._auth.get_installation_token(installation_id)
        return create_provider("github", token)

    async def run(self, request: ReviewRequest) -> RunResult:
        provider = await self._provider(request.installation_id)
        return await execute_review_request(
            provider,
            request,
            bot_name=self._bot_name,
            bot_identity=await self._auth.get_bot_identity(),
            db=self._app_db(),
        )

    async def publish(self, request: ReviewRequest, status: CommitStatus) -> bool:
        provider = await self._provider(request.installation_id)
        return await publish_status(provider, pr_info_for(request), status)

    async def find_orphans(self) -> list[ReviewRequest]:
        """Open pull requests whose `mira/review` check was left queued or in progress."""
        found: list[ReviewRequest] = []
        repos = [
            r for r in self._app_db().list_repos() if r.platform == "github" and r.installation_id
        ][:_ORPHAN_SCAN_REPOS]
        providers: dict[int, Any] = {}
        for record in repos:
            try:
                provider = providers.get(record.installation_id)
                if provider is None:
                    provider = await self._provider(record.installation_id)
                    providers[record.installation_id] = provider
                pulls = await provider.list_unfinished_review_checks(
                    record.owner,
                    record.repo,
                    context=STATUS_CONTEXT,
                    limit=_ORPHAN_SCAN_PRS_PER_REPO,
                )
            except Exception as exc:  # noqa: BLE001 - one repository is not the scan
                logger.info(
                    "Skipped %s/%s looking for unfinished review checks: %s",
                    record.owner,
                    record.repo,
                    exc,
                )
                continue
            for pull in pulls:
                found.append(
                    ReviewRequest(
                        platform="github",
                        owner=record.owner,
                        repo=record.repo,
                        pr_number=pull.number,
                        installation_id=record.installation_id,
                        head_sha=pull.head_sha,
                        base_ref=pull.base_ref,
                        head_ref=pull.head_ref,
                        pr_title=pull.title,
                        pr_url=pull.url
                        or f"https://github.com/{record.owner}/{record.repo}/pull/{pull.number}",
                        is_private=bool(record.private),
                        reason="recovered",
                        # The check is already on the commit: whatever happens
                        # to this request, it is one the queue must settle.
                        check_state="running",
                    )
                )
        return found
