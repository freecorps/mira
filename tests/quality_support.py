"""A history-capable fake provider for the backtest and escaped-bug tests.

Reads answer from dictionaries; every write is recorded in ``writes`` so a
test can assert that nothing was posted.
"""

from __future__ import annotations

from typing import Any

from mira.models import (
    BlameRange,
    CommitInfo,
    HumanReviewComment,
    IssueInfo,
    MergedPullRequest,
    PRInfo,
    ReviewResult,
)
from mira.providers.base import BaseProvider


class FakeHistoryProvider(BaseProvider):
    def __init__(self, token: str = "t", **data: Any) -> None:
        self.merged: dict[int, MergedPullRequest] = data.get("merged", {})
        self.diffs: dict[int, str] = data.get("diffs", {})
        self.commit_diffs: dict[str, str] = data.get("commit_diffs", {})
        self.commits: dict[str, CommitInfo] = data.get("commits", {})
        self.path_commits: dict[str, list[CommitInfo]] = data.get("path_commits", {})
        self.prs_for_commit: dict[str, list[int]] = data.get("prs_for_commit", {})
        self.blame: dict[str, list[BlameRange]] | None = data.get("blame")
        self.human_comments: dict[int, list[HumanReviewComment]] = data.get("human_comments", {})
        self.issues: dict[int, IssueInfo] = data.get("issues", {})
        self.writes: list[str] = []
        self.reads: list[str] = []

    # -- reads -------------------------------------------------------------

    async def get_pr_info(self, pr_url: str) -> PRInfo:
        number = int(pr_url.rstrip("/").rsplit("/", 1)[-1])
        m = self.merged[number]
        return PRInfo(
            title=m.title,
            description=m.body,
            base_branch=m.base_branch,
            head_branch=m.head_branch,
            url=m.url,
            number=number,
            owner="acme",
            repo="app",
            base_sha=m.base_sha,
            head_sha=m.head_sha,
            author=m.author,
        )

    async def get_pr_diff(self, pr_info: PRInfo) -> str:
        self.reads.append(f"diff:{pr_info.number}")
        return self.diffs.get(pr_info.number, "")

    async def get_human_review_comments(
        self, pr_info: PRInfo, bot_login: str
    ) -> list[HumanReviewComment]:
        return list(self.human_comments.get(pr_info.number, []))

    async def list_landed_pull_requests(
        self,
        pr_info: PRInfo,
        *,
        since: float,
        until: float = 0.0,
        base: str = "",
        limit: int = 100,
        max_files: int = 100,
    ) -> list[MergedPullRequest]:
        items = [m for m in self.merged.values() if not since or m.merged_at >= since]
        items.sort(key=lambda m: m.merged_at, reverse=True)
        return items[:limit]

    async def get_landed_pull_request(
        self, pr_info: PRInfo, number: int
    ) -> MergedPullRequest | None:
        return self.merged.get(number)

    async def get_commit(self, pr_info: PRInfo, sha: str) -> CommitInfo | None:
        return self.commits.get(sha)

    async def get_commit_diff(self, pr_info: PRInfo, sha: str) -> str:
        return self.commit_diffs.get(sha, "")

    async def list_path_commits(
        self,
        pr_info: PRInfo,
        path: str,
        *,
        since: float = 0.0,
        until: float = 0.0,
        ref: str = "",
        limit: int = 20,
    ) -> list[CommitInfo]:
        return [
            c
            for c in self.path_commits.get(path, [])
            if (not since or c.date >= since) and (not until or c.date <= until)
        ][:limit]

    async def get_prs_for_commit(self, pr_info: PRInfo, sha: str) -> list[int]:
        return list(self.prs_for_commit.get(sha, []))

    async def get_blame(self, pr_info: PRInfo, path: str, ref: str) -> list[BlameRange]:
        if self.blame is None:
            raise NotImplementedError("no blame here")
        return list(self.blame.get(path, []))

    async def get_issue(
        self, pr_info: PRInfo, number: int, *, owner: str = "", repo: str = ""
    ) -> IssueInfo | None:
        return self.issues.get(number)

    # -- writes (recorded, never expected) ----------------------------------

    async def post_review(self, pr_info: PRInfo, result: ReviewResult, bot_name: str = "") -> list:
        self.writes.append("post_review")
        return []

    async def post_comment(self, pr_info: PRInfo, body: str) -> None:
        self.writes.append("post_comment")

    async def find_bot_comment(self, pr_info: PRInfo, marker: str) -> int | None:
        return None

    async def update_comment(self, pr_info: PRInfo, comment_id: int, body: str) -> None:
        self.writes.append("update_comment")

    async def resolve_outdated_review_threads(self, pr_info: PRInfo) -> int:
        self.writes.append("resolve_outdated_review_threads")
        return 0

    async def submit_verdict(self, pr_info: PRInfo, event: str, body: str) -> bool:
        self.writes.append("submit_verdict")
        return True

    async def publish_review_status(self, pr_info: PRInfo, **kwargs: Any) -> str:
        self.writes.append("publish_review_status")
        return "x"

    async def add_label(self, pr_info: PRInfo, label: str) -> None:
        self.writes.append("add_label")


DAY = 86400.0


def merged_pr(number: int, *, title: str = "", merged_at: float = 0.0, **kw: Any):
    return MergedPullRequest(
        number=number,
        title=title or f"PR {number}",
        url=f"https://github.com/acme/app/pull/{number}",
        base_branch=kw.pop("base_branch", "main"),
        head_branch=kw.pop("head_branch", f"feature-{number}"),
        base_sha=kw.pop("base_sha", f"base{number}"),
        head_sha=kw.pop("head_sha", f"head{number}"),
        merge_commit_sha=kw.pop("merge_commit_sha", f"merge{number}"),
        merged_at=merged_at,
        **kw,
    )


def file_diff(path: str, old_start: int, removed: list[str], new_start: int, added: list[str]):
    """A one-hunk unified diff for ``path``."""
    lines = [
        f"diff --git a/{path} b/{path}",
        f"--- a/{path}",
        f"+++ b/{path}",
        f"@@ -{old_start},{len(removed)} +{new_start},{len(added)} @@",
    ]
    lines += [f"-{line}" for line in removed]
    lines += [f"+{line}" for line in added]
    return "\n".join(lines) + "\n"
