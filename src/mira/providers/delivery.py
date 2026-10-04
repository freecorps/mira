"""Delivery-analytics reads for the three providers.

The change-frequency heatmap needs commit history with per-file line counts,
and DORA needs release dates and a pull request's first commit. None of that
is review-critical, so it lives here as one mixin per platform instead of in
the provider modules: each method is best-effort, bounded, and returns what it
could read.

Bounded matters more than complete. Most platforms list commits without their
files, so churn costs one request per commit; ``max_commits`` caps that, the
listing stops as soon as it passes ``since``, and merge commits are skipped —
their diff against the first parent repeats the work of the commits they merge
and would count every change twice.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from typing import Any
from urllib.parse import quote

import httpx

from mira.models import CommitChurn, FileChangeStat, PRInfo, ReleaseRef
from mira.providers._time import iso_to_epoch

logger = logging.getLogger(__name__)

# Concurrent per-commit requests. Small on purpose: this runs in the
# background after a merge, and nothing is waiting on it.
_CONCURRENCY = 4
_PAGE = 100


def _iso(epoch: float) -> str:
    return datetime.fromtimestamp(max(0.0, epoch), tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def diff_line_counts(diff_text: str) -> list[FileChangeStat]:
    """Per-file added/deleted counts from a unified diff."""
    from mira.core.diff_parser import parse_diff

    try:
        patch_set = parse_diff(diff_text or "")
    except Exception:  # noqa: BLE001 - a diff we cannot parse counts as no lines
        return []
    return [
        FileChangeStat(path=f.path, added_lines=f.added_lines, deleted_lines=f.deleted_lines)
        for f in patch_set.files
    ]


def fragment_line_counts(fragment: str) -> tuple[int, int]:
    """Added/deleted lines in a header-less hunk fragment (GitLab's ``diff``)."""
    added = deleted = 0
    for line in (fragment or "").splitlines():
        if line.startswith("+++") or line.startswith("---"):
            continue
        if line.startswith("+"):
            added += 1
        elif line.startswith("-"):
            deleted += 1
    return added, deleted


async def _gather_bounded(items: list[Any], fn: Any) -> list[Any]:
    sem = asyncio.Semaphore(_CONCURRENCY)

    async def _one(item: Any) -> Any:
        async with sem:
            try:
                return await fn(item)
            except Exception as exc:  # noqa: BLE001 - one bad commit is not fatal
                logger.debug("Delivery analytics read failed for %s: %s", item, exc)
                return None

    return [r for r in await asyncio.gather(*[_one(i) for i in items]) if r is not None]


# ───────────────────────────────────────────────────────────── GitHub ──


class GitHubDeliveryMixin:
    """GitHub REST: commits, releases, PR commits."""

    _token: str

    def _delivery_base(self, pr_info: PRInfo) -> str:
        from mira.providers.github import _GITHUB_API_URL

        return f"{_GITHUB_API_URL}/repos/{pr_info.owner}/{pr_info.repo}"

    def _delivery_headers(self) -> dict[str, str]:
        return {
            "Authorization": f"token {self._token}",
            "Accept": "application/vnd.github.v3+json",
        }

    async def get_commit_churn(
        self,
        pr_info: PRInfo,
        *,
        since: float,
        ref: str = "",
        max_commits: int = 100,
    ) -> list[CommitChurn]:
        branch = ref or pr_info.base_branch
        base = self._delivery_base(pr_info)
        headers = self._delivery_headers()
        listed: list[dict[str, Any]] = []
        async with httpx.AsyncClient(timeout=30) as client:
            page = 1
            while len(listed) < max_commits:
                params: dict[str, Any] = {"since": _iso(since), "per_page": _PAGE, "page": page}
                if branch:
                    params["sha"] = branch
                resp = await client.get(f"{base}/commits", headers=headers, params=params)
                if resp.status_code != 200:
                    break
                data = resp.json() or []
                listed.extend(c for c in data if len(c.get("parents") or []) <= 1)
                if len(data) < _PAGE:
                    break
                page += 1

            async def _detail(item: dict[str, Any]) -> CommitChurn | None:
                sha = str(item.get("sha") or "")
                resp = await client.get(f"{base}/commits/{sha}", headers=headers)
                if resp.status_code != 200:
                    return None
                body = resp.json() or {}
                commit = body.get("commit") or {}
                at = iso_to_epoch(
                    ((commit.get("committer") or {}).get("date"))
                    or ((commit.get("author") or {}).get("date"))
                    or ""
                )
                return CommitChurn(
                    sha=sha,
                    at=at,
                    message=str(commit.get("message") or "").split("\n", 1)[0][:200],
                    files=[
                        FileChangeStat(
                            path=str(f.get("filename") or ""),
                            added_lines=int(f.get("additions") or 0),
                            deleted_lines=int(f.get("deletions") or 0),
                        )
                        for f in body.get("files") or []
                        if f.get("filename")
                    ],
                )

            return await _gather_bounded(listed[:max_commits], _detail)

    async def list_deployment_releases(
        self, pr_info: PRInfo, *, since: float = 0.0
    ) -> list[ReleaseRef]:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.get(
                f"{self._delivery_base(pr_info)}/releases",
                headers=self._delivery_headers(),
                params={"per_page": _PAGE},
            )
        if resp.status_code != 200:
            return []
        out: list[ReleaseRef] = []
        for item in resp.json() or []:
            if item.get("draft"):
                continue
            at = iso_to_epoch(item.get("published_at") or item.get("created_at") or "")
            if at and at >= since:
                out.append(
                    ReleaseRef(
                        tag=str(item.get("tag_name") or ""),
                        at=at,
                        url=str(item.get("html_url") or ""),
                    )
                )
        return out

    async def get_pr_first_commit_at(self, pr_info: PRInfo) -> float:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.get(
                f"{self._delivery_base(pr_info)}/pulls/{pr_info.number}/commits",
                headers=self._delivery_headers(),
                params={"per_page": _PAGE},
            )
        if resp.status_code != 200:
            return 0.0
        times = [
            iso_to_epoch(((c.get("commit") or {}).get("author") or {}).get("date") or "")
            for c in resp.json() or []
        ]
        times = [t for t in times if t > 0]
        return min(times) if times else 0.0


# ───────────────────────────────────────────────────────────── GitLab ──


class GitLabDeliveryMixin:
    """GitLab REST v4: repository commits + per-commit diff, releases, MR commits."""

    _token: str

    def _project(self, pr_info: PRInfo) -> str:  # pragma: no cover - provided by the provider
        raise NotImplementedError

    async def get_commit_churn(
        self,
        pr_info: PRInfo,
        *,
        since: float,
        ref: str = "",
        max_commits: int = 100,
    ) -> list[CommitChurn]:
        branch = ref or pr_info.base_branch
        project = self._project(pr_info)
        headers = {"PRIVATE-TOKEN": self._token}
        listed: list[dict[str, Any]] = []
        async with httpx.AsyncClient(timeout=30) as client:
            page = 1
            while len(listed) < max_commits:
                params: dict[str, Any] = {"since": _iso(since), "per_page": _PAGE, "page": page}
                if branch:
                    params["ref_name"] = branch
                resp = await client.get(
                    f"{project}/repository/commits", headers=headers, params=params
                )
                if resp.status_code != 200:
                    break
                data = resp.json() or []
                listed.extend(c for c in data if len(c.get("parent_ids") or []) <= 1)
                if len(data) < _PAGE:
                    break
                page += 1

            async def _detail(item: dict[str, Any]) -> CommitChurn | None:
                sha = str(item.get("id") or "")
                resp = await client.get(
                    f"{project}/repository/commits/{quote(sha, safe='')}/diff",
                    headers=headers,
                    params={"per_page": _PAGE},
                )
                if resp.status_code != 200:
                    return None
                files: list[FileChangeStat] = []
                for change in resp.json() or []:
                    path = str(change.get("new_path") or change.get("old_path") or "")
                    if not path:
                        continue
                    added, deleted = fragment_line_counts(str(change.get("diff") or ""))
                    files.append(
                        FileChangeStat(path=path, added_lines=added, deleted_lines=deleted)
                    )
                return CommitChurn(
                    sha=sha,
                    at=iso_to_epoch(item.get("committed_date") or item.get("created_at") or ""),
                    message=str(item.get("title") or "")[:200],
                    files=files,
                )

            return await _gather_bounded(listed[:max_commits], _detail)

    async def list_deployment_releases(
        self, pr_info: PRInfo, *, since: float = 0.0
    ) -> list[ReleaseRef]:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.get(
                f"{self._project(pr_info)}/releases",
                headers={"PRIVATE-TOKEN": self._token},
                params={"per_page": _PAGE},
            )
        if resp.status_code != 200:
            return []
        out: list[ReleaseRef] = []
        for item in resp.json() or []:
            at = iso_to_epoch(item.get("released_at") or item.get("created_at") or "")
            if at and at >= since:
                links = item.get("_links") or {}
                out.append(
                    ReleaseRef(
                        tag=str(item.get("tag_name") or ""),
                        at=at,
                        url=str(links.get("self") or ""),
                    )
                )
        return out

    async def get_pr_first_commit_at(self, pr_info: PRInfo) -> float:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.get(
                f"{self._project(pr_info)}/merge_requests/{pr_info.number}/commits",
                headers={"PRIVATE-TOKEN": self._token},
                params={"per_page": _PAGE},
            )
        if resp.status_code != 200:
            return 0.0
        times = [
            iso_to_epoch(c.get("authored_date") or c.get("created_at") or "")
            for c in resp.json() or []
        ]
        times = [t for t in times if t > 0]
        return min(times) if times else 0.0


# ──────────────────────────────────────────────────────────── Forgejo ──


class ForgejoDeliveryMixin:
    """Forgejo / Gitea API v1: commits + raw commit diff, releases, PR commits."""

    _token: str

    def _repo(self, pr_info: PRInfo) -> str:  # pragma: no cover - provided by the provider
        raise NotImplementedError

    async def get_commit_churn(
        self,
        pr_info: PRInfo,
        *,
        since: float,
        ref: str = "",
        max_commits: int = 100,
    ) -> list[CommitChurn]:
        branch = ref or pr_info.base_branch
        base = self._repo(pr_info)
        headers = {"Authorization": f"token {self._token}"}
        listed: list[dict[str, Any]] = []
        limit = 50
        async with httpx.AsyncClient(timeout=30) as client:
            page = 1
            done = False
            while not done and len(listed) < max_commits:
                params: dict[str, Any] = {
                    "limit": limit,
                    "page": page,
                    "since": _iso(since),
                    "stat": "false",
                    "verification": "false",
                }
                if branch:
                    params["sha"] = branch
                resp = await client.get(f"{base}/commits", headers=headers, params=params)
                if resp.status_code != 200:
                    break
                data = resp.json() or []
                for item in data:
                    at = iso_to_epoch(
                        (((item.get("commit") or {}).get("committer") or {}).get("date")) or ""
                    )
                    # Older Forgejo ignores `since`; stop at the window's edge.
                    if at and at < since:
                        done = True
                        break
                    if len(item.get("parents") or []) <= 1:
                        listed.append(item)
                if len(data) < limit:
                    break
                page += 1

            async def _detail(item: dict[str, Any]) -> CommitChurn | None:
                sha = str(item.get("sha") or "")
                commit = item.get("commit") or {}
                at = iso_to_epoch(((commit.get("committer") or {}).get("date")) or "")
                files: list[FileChangeStat] = []
                resp = await client.get(
                    f"{base}/git/commits/{quote(sha, safe='')}.diff", headers=headers
                )
                if resp.status_code == 200:
                    files = diff_line_counts(resp.text)
                if not files:
                    # The listing names the files even when the diff is
                    # unavailable; a touch without line counts still counts.
                    files = [
                        FileChangeStat(path=str(f.get("filename") or ""))
                        for f in item.get("files") or []
                        if f.get("filename")
                    ]
                return CommitChurn(
                    sha=sha,
                    at=at,
                    message=str(commit.get("message") or "").split("\n", 1)[0][:200],
                    files=files,
                )

            return await _gather_bounded(listed[:max_commits], _detail)

    async def list_deployment_releases(
        self, pr_info: PRInfo, *, since: float = 0.0
    ) -> list[ReleaseRef]:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.get(
                f"{self._repo(pr_info)}/releases",
                headers={"Authorization": f"token {self._token}"},
                params={"limit": 50},
            )
        if resp.status_code != 200:
            return []
        out: list[ReleaseRef] = []
        for item in resp.json() or []:
            if item.get("draft"):
                continue
            at = iso_to_epoch(item.get("published_at") or item.get("created_at") or "")
            if at and at >= since:
                out.append(
                    ReleaseRef(
                        tag=str(item.get("tag_name") or ""),
                        at=at,
                        url=str(item.get("html_url") or ""),
                    )
                )
        return out

    async def get_pr_first_commit_at(self, pr_info: PRInfo) -> float:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.get(
                f"{self._repo(pr_info)}/pulls/{pr_info.number}/commits",
                headers={"Authorization": f"token {self._token}"},
                params={"limit": 50, "stat": "false", "verification": "false", "files": "false"},
            )
        if resp.status_code != 200:
            return 0.0
        times = [
            iso_to_epoch(((c.get("commit") or {}).get("author") or {}).get("date") or "")
            for c in resp.json() or []
        ]
        times = [t for t in times if t > 0]
        return min(times) if times else 0.0
