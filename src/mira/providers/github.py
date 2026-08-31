"""GitHub provider using PyGithub."""

from __future__ import annotations

import asyncio
import base64
import itertools
import logging
import os
import re
from typing import Any

import httpx
from github import Github, GithubException
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from mira.autofix.capabilities import (
    GITHUB_CAPABILITIES as GITHUB_AUTOFIX_CAPABILITIES,
)
from mira.autofix.capabilities import (
    AutofixCapabilities,
)
from mira.checks.capabilities import (
    GITHUB_CAPABILITIES as GITHUB_CHECK_CAPABILITIES,
)
from mira.checks.capabilities import (
    CheckCapabilities,
)
from mira.checks.models import mira_status_contexts
from mira.exceptions import ProviderError
from mira.gate.capabilities import GITHUB_CAPABILITIES, GateCapabilities
from mira.gate.codeowners import CODEOWNERS_LOCATIONS
from mira.gate.models import CIState
from mira.models import (
    BotThreadRecord,
    CIJobFailure,
    FileChangeStat,
    FileHistoryEntry,
    HumanReviewComment,
    IssueInfo,
    OpenPRRef,
    PathAuthorship,
    PRInfo,
    ReviewComment,
    ReviewResult,
    UnresolvedThread,
)
from mira.providers._time import iso_to_epoch
from mira.providers.base import BaseProvider

# Shared comment-formatting helpers (re-exported for back-compat — callers and
# tests import these names from this module).
from mira.providers.formatting import (  # noqa: F401
    _CATEGORY_DISPLAY,
    parse_bot_comment_metadata,
)
from mira.providers.formatting import (
    format_comment_body as _format_comment_body,
)
from mira.providers.formatting import (
    format_key_issues as _format_key_issues,
)
from mira.triage.capabilities import (
    GITHUB_CAPABILITIES as GITHUB_TRIAGE_CAPABILITIES,
)
from mira.triage.capabilities import (
    TriageCapabilities,
)

# Every check-run name Mira publishes itself. Filtered out of the CI it reads
# back, so neither the gate nor the pre-merge checks can see their own red
# status and conclude the build is failing.
_OWN_STATUS_CONTEXTS = mira_status_contexts()

# Transient errors worth retrying — network issues and GitHub server errors.
_RETRYABLE = (ConnectionError, TimeoutError, httpx.TransportError, GithubException)

logger = logging.getLogger(__name__)

_retry_transient = retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=2, max=30),
    retry=retry_if_exception_type(_RETRYABLE),
    reraise=True,
)

# GitHub Enterprise: set MIRA_GITHUB_API_URL (and MIRA_GITHUB_GRAPHQL_URL if non-default).
_GITHUB_API_URL = os.environ.get(
    "MIRA_GITHUB_API_URL",
    "https://api.github.com",
).rstrip("/")
_GRAPHQL_URL = os.environ.get(
    "MIRA_GITHUB_GRAPHQL_URL",
    f"{_GITHUB_API_URL}/graphql",
)


def _normalize_login(login: str) -> str:
    """Normalize a GitHub login for comparison.

    GitHub Apps have a quirk: ``viewer.login`` returns ``app[bot]`` while
    review-comment authors are stored as just ``app``.  Strip the ``[bot]``
    suffix and lower-case so both forms match reliably.
    """
    return login.removesuffix("[bot]").lower()


_REVIEW_THREADS_QUERY = """
query($owner: String!, $repo: String!, $number: Int!, $cursor: String) {
  viewer { login }
  repository(owner: $owner, name: $repo) {
    pullRequest(number: $number) {
      reviewThreads(first: 100, after: $cursor) {
        pageInfo {
          hasNextPage
          endCursor
        }
        nodes {
          id
          isResolved
          isOutdated
          comments(first: 1) {
            nodes {
              databaseId
              author { login }
              body
              path
              line
              originalLine
              reactionGroups {
                content
                users(first: 100) { nodes { login } }
              }
            }
          }
        }
      }
    }
  }
}
"""

_RESOLVE_THREAD_MUTATION = """
mutation($threadId: ID!) {
  resolveReviewThread(input: {threadId: $threadId}) {
    thread { id isResolved }
  }
}
"""

_COMMENT_THREAD_QUERY = """
query($owner: String!, $repo: String!, $number: Int!, $cursor: String) {
  repository(owner: $owner, name: $repo) {
    pullRequest(number: $number) {
      reviewThreads(first: 50, after: $cursor) {
        pageInfo { hasNextPage endCursor }
        nodes {
          id
          isResolved
          comments(first: 100) {
            nodes { id }
          }
        }
      }
    }
  }
}
"""

# Matches: https://github.com/owner/repo/pull/123 or owner/repo#123
_PR_URL_PATTERN = re.compile(
    r"(?:https?://github\.com/)?(?P<owner>[^/\s]+)/(?P<repo>[^/\s#]+)(?:/pull/|#)(?P<number>\d+)"
)


def parse_pr_url(pr_url: str) -> tuple[str, str, int]:
    """Parse a PR URL or shorthand into (owner, repo, number)."""
    match = _PR_URL_PATTERN.match(pr_url.strip())
    if not match:
        raise ProviderError(
            f"Cannot parse PR URL: {pr_url}. "
            "Expected format: https://github.com/owner/repo/pull/123 or owner/repo#123"
        )
    return match.group("owner"), match.group("repo"), int(match.group("number"))


def _file_to_diff(f: dict[str, Any]) -> str:
    """Rebuild a unified-diff file section from a GitHub files-API entry.

    The API returns each file's ``patch`` without the ``diff --git`` /
    ``---`` / ``+++`` headers unidiff needs, so reconstruct them from the
    entry's ``filename``/``status``/``previous_filename``.
    """
    path = f["filename"]
    status = f.get("status", "modified")
    patch = f["patch"]
    if status == "renamed":
        prev = f.get("previous_filename") or path
        header = [
            f"diff --git a/{prev} b/{path}",
            f"rename from {prev}",
            f"rename to {path}",
            f"--- a/{prev}",
            f"+++ b/{path}",
        ]
    elif status == "added":
        header = [
            f"diff --git a/{path} b/{path}",
            "new file mode 100644",
            "--- /dev/null",
            f"+++ b/{path}",
        ]
    elif status == "removed":
        header = [
            f"diff --git a/{path} b/{path}",
            "deleted file mode 100644",
            f"--- a/{path}",
            "+++ /dev/null",
        ]
    else:
        header = [f"diff --git a/{path} b/{path}", f"--- a/{path}", f"+++ b/{path}"]
    return "\n".join(header) + "\n" + patch


class GitHubProvider(BaseProvider):
    """GitHub code hosting provider."""

    def __init__(self, token: str) -> None:
        if not token:
            raise ProviderError("GitHub token is required")
        self._github = Github(token)
        self._token = token

    async def get_pr_info(self, pr_url: str) -> PRInfo:
        owner, repo, number = parse_pr_url(pr_url)

        @_retry_transient
        def _fetch() -> PRInfo:
            gh_repo = self._github.get_repo(f"{owner}/{repo}")
            pr = gh_repo.get_pull(number)
            user = pr.user
            return PRInfo(
                title=pr.title or "",
                description=pr.body or "",
                base_branch=pr.base.ref,
                head_branch=pr.head.ref,
                url=pr.html_url,
                number=pr.number,
                owner=owner,
                repo=repo,
                base_sha=pr.base.sha or "",
                head_sha=pr.head.sha or "",
                author=(user.login or "") if user else "",
                author_avatar_url=(user.avatar_url or "") if user else "",
                draft=bool(getattr(pr, "draft", False)),
            )

        try:
            return await asyncio.to_thread(_fetch)
        except ProviderError:
            raise
        except Exception as e:
            raise ProviderError(f"Failed to fetch PR info: {e}") from e

    async def get_pr_diff(self, pr_info: PRInfo) -> str:
        diff_url = f"{_GITHUB_API_URL}/repos/{pr_info.owner}/{pr_info.repo}/pulls/{pr_info.number}"
        headers = {
            "Authorization": f"token {self._token}",
            "Accept": "application/vnd.github.v3.diff",
        }

        @_retry_transient
        async def _fetch_diff() -> str:
            async with httpx.AsyncClient() as client:
                resp = await client.get(diff_url, headers=headers, follow_redirects=True)
                if resp.status_code == 406:
                    # GitHub 406s once a PR diff exceeds its ~20,000-line cap.
                    # Fall back to per-file patches from the files API.
                    logger.info(
                        "PR diff too large for single fetch (406); falling back to per-file patches"
                    )
                    return await self._fetch_files_diff(
                        client,
                        f"{_GITHUB_API_URL}/repos/{pr_info.owner}/{pr_info.repo}/pulls/{pr_info.number}/files",
                        pr_info,
                    )
                resp.raise_for_status()
                return resp.text

        try:
            return await _fetch_diff()
        except ProviderError:
            raise
        except Exception as e:
            raise ProviderError(f"Failed to fetch PR diff: {e}") from e

    async def _fetch_files_diff(
        self, client: httpx.AsyncClient, files_url: str, pr_info: PRInfo
    ) -> str:
        """Synthesize a unified diff from per-file patches (files API).

        Fallback for when the .diff media type 406s (diff over GitHub's
        ~20,000-line cap). Paginates up to GitHub's 3000-file ceiling; files
        too large for an individual patch arrive without one and are skipped
        with a warning.
        """
        headers = {
            "Authorization": f"token {self._token}",
            "Accept": "application/vnd.github+json",
        }
        files: list[dict[str, Any]] = []
        page = 1
        while True:
            resp = await client.get(
                files_url,
                headers=headers,
                params={"per_page": 100, "page": page},
                follow_redirects=True,
            )
            resp.raise_for_status()
            batch = resp.json()
            files.extend(batch)
            if len(batch) < 100 or len(files) >= 3000:
                break
            page += 1

        parts: list[str] = []
        skipped: list[str] = []
        for f in files:
            if f.get("patch"):
                parts.append(_file_to_diff(f))
            else:
                skipped.append(f.get("filename", "?"))
        if skipped:
            logger.warning(
                "Per-file diff fallback: %d file(s) too large for individual patches, skipped in %s: %s",
                len(skipped),
                pr_info.url,
                ", ".join(skipped[:10]) + ("…" if len(skipped) > 10 else ""),
            )
        return "\n\n".join(parts)

    async def get_compare_diff(
        self,
        pr_info: PRInfo,
        base_sha: str,
        head_sha: str,
    ) -> str:
        """Fetch a unified diff between two commits via GitHub's compare API.

        Used by round 2+ reviews so we only review what's been pushed since
        the last review (``last_reviewed_sha``..``current_head_sha``) rather
        than re-flagging every file in the PR.

        Returns an empty string if the two SHAs are identical (nothing new
        to review).
        """
        if base_sha == head_sha or not base_sha or not head_sha:
            return ""
        url = (
            f"{_GITHUB_API_URL}/repos/{pr_info.owner}/{pr_info.repo}"
            f"/compare/{base_sha}...{head_sha}"
        )
        headers = {
            "Authorization": f"token {self._token}",
            "Accept": "application/vnd.github.v3.diff",
        }

        @_retry_transient
        async def _fetch() -> str:
            async with httpx.AsyncClient() as client:
                resp = await client.get(url, headers=headers, follow_redirects=True)
                resp.raise_for_status()
                return resp.text

        try:
            return await _fetch()
        except Exception as e:
            raise ProviderError(f"Failed to fetch compare diff: {e}") from e

    async def list_open_prs(
        self,
        owner: str,
        repo: str,
        limit: int = 20,
    ) -> list[OpenPRRef]:
        """List the most recently updated open PRs in a repo.

        Returns lightweight refs (no diff fetched) used by cross-PR overlap
        detection to decide which PRs are worth comparing in depth. Capped at
        ``limit`` to bound the work on busy repos.
        """

        @_retry_transient
        def _fetch() -> list[OpenPRRef]:
            gh_repo = self._github.get_repo(f"{owner}/{repo}")
            pulls = gh_repo.get_pulls(state="open", sort="updated", direction="desc")
            out: list[OpenPRRef] = []
            for pr in itertools.islice(pulls, limit):
                out.append(
                    OpenPRRef(
                        number=pr.number,
                        title=pr.title or "",
                        body=pr.body or "",
                        head_sha=pr.head.sha or "",
                        author=(pr.user.login if pr.user else ""),
                        draft=bool(pr.draft),
                        base_ref=pr.base.ref if pr.base else "",
                        head_ref=pr.head.ref if pr.head else "",
                        url=pr.html_url,
                    )
                )
            return out

        try:
            return await asyncio.to_thread(_fetch)
        except Exception as e:
            raise ProviderError(f"Failed to list open PRs: {e}") from e

    async def get_pr_files(
        self,
        owner: str,
        repo: str,
        number: int,
        limit: int = 300,
    ) -> list[str]:
        """Return the file paths changed by a PR (filenames only).

        Far cheaper than fetching the full diff — used as the fallback when a
        candidate PR has no cached fingerprint (or it's stale). Returns an empty
        list if the PR has vanished (closed/merged mid-review).
        """

        @_retry_transient
        def _fetch() -> list[str]:
            gh_repo = self._github.get_repo(f"{owner}/{repo}")
            pr = gh_repo.get_pull(number)
            return [f.filename for f in itertools.islice(pr.get_files(), limit)]

        try:
            return await asyncio.to_thread(_fetch)
        except GithubException as e:
            if getattr(e, "status", None) == 404:
                return []
            raise ProviderError(f"Failed to fetch PR files: {e}") from e
        except Exception as e:
            raise ProviderError(f"Failed to fetch PR files: {e}") from e

    async def post_review(
        self,
        pr_info: PRInfo,
        result: ReviewResult,
        bot_name: str = "miracodeai",
    ) -> list[int]:
        if not result.comments:
            return []

        # The line GitHub anchors a comment to (the end line for multi-line).
        def _anchor(c: ReviewComment) -> int:
            return c.end_line if (c.end_line and c.end_line > c.line) else c.line

        review_comments: list[dict[str, str | int]] = []
        for comment in result.comments:
            body = _format_comment_body(comment, bot_name=bot_name)
            rc: dict[str, str | int] = {
                "path": comment.path,
                "body": body,
            }
            if comment.end_line and comment.end_line > comment.line:
                rc["start_line"] = comment.line
                rc["line"] = comment.end_line
            else:
                rc["line"] = comment.line

            review_comments.append(rc)

        review_body = ""
        if result.summary:
            review_body = f"**Mira Review Summary**\n\n{result.summary}"
        if result.key_issues:
            review_body += _format_key_issues(result.key_issues)

        @_retry_transient
        def _post() -> list[int]:
            gh_repo = self._github.get_repo(f"{pr_info.owner}/{pr_info.repo}")
            pr = gh_repo.get_pull(pr_info.number)

            commits = list(pr.get_commits())
            if not commits:
                raise ProviderError("PR has no commits")
            latest_commit = commits[-1]

            # GitHub comment IDs aligned to result.comments (0 = unknown).
            ids = [0] * len(result.comments)

            try:
                review = pr.create_review(
                    commit=latest_commit,
                    body=review_body,
                    event="COMMENT",
                    comments=review_comments,  # type: ignore[arg-type]
                )
                # Map the posted comments back to ours by (path, anchored line)
                # so human replies can later link to the exact comment.
                try:
                    by_loc: dict[tuple[str, int], int] = {}
                    for posted_c in review.get_comments():
                        ln = posted_c.line
                        if ln is None:
                            ln = getattr(posted_c, "original_line", 0) or 0
                        by_loc[(posted_c.path, ln)] = posted_c.id
                    for i, c in enumerate(result.comments):
                        ids[i] = by_loc.get((c.path, _anchor(c)), 0)
                except Exception:
                    logger.debug("Could not map review comment IDs", exc_info=True)
                return ids
            except GithubException as exc:
                if exc.status != 422:
                    raise
                logger.warning(
                    "Batch review failed (422: %s), falling back to individual comments",
                    exc.data,
                )

            # Per-comment fallback via /comments — looser 422 validation
            # than /reviews. Inlines aren't grouped under a review object,
            # but they still show up on the PR.
            posted = 0
            for i, rc in enumerate(review_comments):
                try:
                    kwargs: dict = {
                        "body": rc["body"],
                        "commit": latest_commit,
                        "path": rc["path"],
                    }
                    if "line" in rc:
                        kwargs["line"] = rc["line"]
                    if "start_line" in rc:
                        kwargs["start_line"] = rc["start_line"]
                    logger.info(
                        "POST /comments commit=%s path=%s line=%s body_len=%d",
                        getattr(latest_commit, "sha", "?")[:8],
                        kwargs.get("path"),
                        kwargs.get("line"),
                        len(kwargs.get("body", "")),
                    )
                    created = pr.create_review_comment(**kwargs)
                    ids[i] = getattr(created, "id", 0) or 0
                    posted += 1
                except GithubException as exc:
                    if exc.status == 422:
                        logger.warning(
                            "Skipping comment on %s:%s — 422 from GitHub: %s",
                            rc.get("path"),
                            rc.get("line"),
                            exc.data,
                        )
                    else:
                        raise

            # If every inline failed, post the summary alone so the review still shows up.
            if posted == 0 and review_body:
                try:
                    pr.create_review(
                        commit=latest_commit,
                        body=review_body,
                        event="COMMENT",
                        comments=[],
                    )
                except GithubException as exc:
                    logger.warning(
                        "Summary-only fallback also failed (%s): %s",
                        exc.status,
                        exc.data,
                    )

            logger.info("Individual fallback: posted %d/%d comments", posted, len(review_comments))
            return ids

        try:
            return await asyncio.to_thread(_post)
        except ProviderError:
            raise
        except Exception as e:
            raise ProviderError(f"Failed to post review: {e}") from e

    async def submit_verdict(self, pr_info: PRInfo, event: str, body: str) -> bool:
        @_retry_transient
        def _submit() -> bool:
            gh_repo = self._github.get_repo(f"{pr_info.owner}/{pr_info.repo}")
            pr = gh_repo.get_pull(pr_info.number)
            try:
                # No `commit=` — GitHub anchors the verdict to the current head,
                # which is what we just reviewed. Only the latest review from
                # each reviewer counts, so an APPROVE here supersedes an earlier
                # REQUEST_CHANGES without needing the dismissal API.
                pr.create_review(body=body, event=event)
                return True
            except GithubException as exc:
                if exc.status == 422:
                    # GitHub refused the verdict — most often "can not approve
                    # your own pull request". The review itself already landed,
                    # so this is a downgrade to comment-only, not a failure.
                    logger.warning("GitHub refused %s on %s: %s", event, pr_info.url, exc.data)
                    return False
                raise

        try:
            return await asyncio.to_thread(_submit)
        except Exception as e:
            logger.warning("Failed to submit %s verdict on %s: %s", event, pr_info.url, e)
            return False

    async def get_review_states(self, pr_info: PRInfo) -> dict[str, str]:
        @_retry_transient
        def _states() -> dict[str, str]:
            gh_repo = self._github.get_repo(f"{pr_info.owner}/{pr_info.repo}")
            pr = gh_repo.get_pull(pr_info.number)
            latest: dict[str, str] = {}
            for review in pr.get_reviews():
                login = getattr(review.user, "login", "") or ""
                if not login:
                    continue
                state = (review.state or "").upper()
                if state == "DISMISSED":
                    # A dismissed review no longer counts against the PR.
                    latest.pop(login, None)
                    continue
                if state in {"COMMENTED", "PENDING"}:
                    # Neither changes a reviewer's standing on GitHub.
                    continue
                latest[login] = state
            return latest

        try:
            return await asyncio.to_thread(_states)
        except Exception as e:
            # Raised rather than returned as "{}": an empty mapping reads as
            # "nobody objected", and the merge gate would approve over a human
            # who did. Callers that only want a best-effort answer (the review
            # verdict) already treat a failure here as a reason to stay quiet.
            logger.debug("Could not read review states for %s: %s", pr_info.url, e)
            raise ProviderError(f"Failed to read review states: {e}") from e

    async def post_comment(self, pr_info: PRInfo, body: str) -> None:
        @_retry_transient
        def _post_comment() -> None:
            gh_repo = self._github.get_repo(f"{pr_info.owner}/{pr_info.repo}")
            issue = gh_repo.get_issue(pr_info.number)
            issue.create_comment(body)

        try:
            await asyncio.to_thread(_post_comment)
        except ProviderError:
            raise
        except Exception as e:
            raise ProviderError(f"Failed to post comment: {e}") from e

    async def find_bot_comment(self, pr_info: PRInfo, marker: str) -> int | None:
        @_retry_transient
        def _find() -> int | None:
            gh_repo = self._github.get_repo(f"{pr_info.owner}/{pr_info.repo}")
            issue = gh_repo.get_issue(pr_info.number)
            for comment in issue.get_comments():
                if marker in comment.body:
                    return comment.id
            return None

        try:
            return await asyncio.to_thread(_find)
        except ProviderError:
            raise
        except Exception as e:
            raise ProviderError(f"Failed to find bot comment: {e}") from e

    async def update_comment(self, pr_info: PRInfo, comment_id: int, body: str) -> None:
        @_retry_transient
        def _update() -> None:
            gh_repo = self._github.get_repo(f"{pr_info.owner}/{pr_info.repo}")
            issue = gh_repo.get_issue(pr_info.number)
            comment = issue.get_comment(comment_id)
            comment.edit(body)

        try:
            await asyncio.to_thread(_update)
        except ProviderError:
            raise
        except Exception as e:
            raise ProviderError(f"Failed to update comment: {e}") from e

    async def reply_to_review_comment(self, pr_info: PRInfo, comment_id: int, body: str) -> None:
        """Post a reply to an existing review (line) comment, threading it.

        Issue (PR-level) comments use ``post_comment``. Review comments are
        line-anchored and threaded; replying needs the PR-comments-replies
        REST endpoint, not ``create_comment``.
        """

        @_retry_transient
        def _reply() -> None:
            gh_repo = self._github.get_repo(f"{pr_info.owner}/{pr_info.repo}")
            pr = gh_repo.get_pull(pr_info.number)
            pr.create_review_comment_reply(comment_id, body)

        try:
            await asyncio.to_thread(_reply)
        except ProviderError:
            raise
        except Exception as e:
            raise ProviderError(f"Failed to reply to review comment: {e}") from e

    async def get_comment_body(self, pr_info: PRInfo, comment_id: int) -> str:
        """Fetch a review (line) comment's body by id. Best-effort."""

        def _fetch() -> str:
            gh_repo = self._github.get_repo(f"{pr_info.owner}/{pr_info.repo}")
            pr = gh_repo.get_pull(pr_info.number)
            return (pr.get_review_comment(comment_id).body or "")[:1500]

        try:
            return await asyncio.to_thread(_fetch)
        except Exception:
            return ""

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=30),
        retry=retry_if_exception_type((httpx.TransportError, ConnectionError, TimeoutError)),
        reraise=True,
    )
    async def _graphql_request(self, query: str, variables: dict[str, Any]) -> dict[str, Any]:
        """Execute a GraphQL request against the GitHub API."""
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                _GRAPHQL_URL,
                json={"query": query, "variables": variables},
                headers={
                    "Authorization": f"bearer {self._token}",
                    "Content-Type": "application/json",
                },
            )
            resp.raise_for_status()
            data = resp.json()
            if "errors" in data:
                raise ProviderError(f"GraphQL errors: {data['errors']}")
            result: dict[str, Any] = data["data"]
            return result

    async def resolve_outdated_review_threads(self, pr_info: PRInfo) -> int:
        @_retry_transient
        async def _resolve() -> int:
            bot_login: str | None = None
            thread_ids: list[str] = []
            total_unresolved = 0
            cursor: str | None = None

            while True:
                variables: dict[str, Any] = {
                    "owner": pr_info.owner,
                    "repo": pr_info.repo,
                    "number": pr_info.number,
                    "cursor": cursor,
                }
                data = await self._graphql_request(_REVIEW_THREADS_QUERY, variables)

                if bot_login is None:
                    bot_login = data["viewer"]["login"]

                threads = data["repository"]["pullRequest"]["reviewThreads"]
                for node in threads["nodes"]:
                    if node["isResolved"]:
                        continue
                    comments = node["comments"]["nodes"]
                    if not comments:
                        continue
                    author = comments[0].get("author")
                    if author is None:
                        continue
                    if _normalize_login(author["login"]) == _normalize_login(bot_login):
                        total_unresolved += 1
                        if node["isOutdated"]:
                            thread_ids.append(node["id"])

                page_info = threads["pageInfo"]
                if not page_info["hasNextPage"]:
                    break
                cursor = page_info["endCursor"]

            logger.debug(
                "Brute-force resolver (viewer=%s): %d unresolved bot thread(s), "
                "%d outdated to resolve",
                bot_login,
                total_unresolved,
                len(thread_ids),
            )

            for thread_id in thread_ids:
                await self._graphql_request(_RESOLVE_THREAD_MUTATION, {"threadId": thread_id})

            return len(thread_ids)

        try:
            return await _resolve()
        except ProviderError:
            raise
        except Exception as e:
            raise ProviderError(f"Failed to resolve outdated review threads: {e}") from e

    async def get_unresolved_bot_threads(
        self, pr_info: PRInfo, bot_login: str | None = None
    ) -> list[UnresolvedThread]:
        """Fetch all unresolved review threads authored by the bot.

        If *bot_login* is ``None`` the authenticated user (viewer) is used,
        which is the reliable way to match the GitHub App's own comments.
        """
        threads: list[UnresolvedThread] = []
        viewer_login: str | None = None
        cursor: str | None = None

        while True:
            variables: dict[str, Any] = {
                "owner": pr_info.owner,
                "repo": pr_info.repo,
                "number": pr_info.number,
                "cursor": cursor,
            }
            try:
                data = await self._graphql_request(_REVIEW_THREADS_QUERY, variables)
            except ProviderError:
                raise
            except Exception as e:
                raise ProviderError(f"Failed to fetch review threads: {e}") from e

            if viewer_login is None:
                viewer_login = data["viewer"]["login"]

            effective_login = bot_login or viewer_login

            rt = data["repository"]["pullRequest"]["reviewThreads"]
            total_nodes = len(rt["nodes"])
            skipped_resolved = 0
            skipped_no_comments = 0
            skipped_author = 0

            for node in rt["nodes"]:
                if node["isResolved"]:
                    skipped_resolved += 1
                    continue
                comments = node["comments"]["nodes"]
                if not comments:
                    skipped_no_comments += 1
                    continue
                first = comments[0]
                author = (first.get("author") or {}).get("login", "")
                if _normalize_login(author) != _normalize_login(effective_login):
                    skipped_author += 1
                    logger.info(
                        "Skipping thread %s: author %r != %r",
                        node["id"],
                        author,
                        effective_login,
                    )
                    continue
                threads.append(
                    UnresolvedThread(
                        thread_id=node["id"],
                        path=first.get("path", ""),
                        line=first.get("line") or first.get("originalLine") or 0,
                        body=first.get("body", ""),
                        is_outdated=bool(node["isOutdated"]),
                    )
                )

            logger.info(
                "Page: %d nodes, %d resolved, %d no comments, %d wrong author, %d matched",
                total_nodes,
                skipped_resolved,
                skipped_no_comments,
                skipped_author,
                total_nodes - skipped_resolved - skipped_no_comments - skipped_author,
            )

            if rt["pageInfo"]["hasNextPage"]:
                cursor = rt["pageInfo"]["endCursor"]
            else:
                break

        logger.info(
            "get_unresolved_bot_threads (viewer=%s, match=%s): "
            "found %d thread(s) for PR %s (%d outdated)",
            viewer_login,
            effective_login,
            len(threads),
            pr_info.url,
            sum(1 for t in threads if t.is_outdated),
        )
        return threads

    async def add_label(self, pr_info: PRInfo, label: str) -> None:
        @_retry_transient
        def _add() -> None:
            gh_repo = self._github.get_repo(f"{pr_info.owner}/{pr_info.repo}")
            issue = gh_repo.get_issue(pr_info.number)
            issue.add_to_labels(label)

        try:
            await asyncio.to_thread(_add)
        except ProviderError:
            raise
        except Exception as e:
            raise ProviderError(f"Failed to add label: {e}") from e

    async def remove_label(self, pr_info: PRInfo, label: str) -> None:
        @_retry_transient
        def _remove() -> None:
            gh_repo = self._github.get_repo(f"{pr_info.owner}/{pr_info.repo}")
            issue = gh_repo.get_issue(pr_info.number)
            try:
                issue.remove_from_labels(label)
            except GithubException as exc:
                if exc.status == 404:
                    return
                raise

        try:
            await asyncio.to_thread(_remove)
        except ProviderError:
            raise
        except Exception as e:
            raise ProviderError(f"Failed to remove label: {e}") from e

    async def get_repo_tree(self, pr_info: PRInfo, ref: str) -> list[str]:
        """List every blob (file) path in the repo at a given ref.

        Used by JIT cross-file context: we fetch the tree once, then
        check which import-resolution candidates actually exist before
        spending API calls fetching their contents. One API call → up to
        thousands of paths in response.
        """
        url = f"{_GITHUB_API_URL}/repos/{pr_info.owner}/{pr_info.repo}/git/trees/{ref}?recursive=1"
        headers = {
            "Authorization": f"token {self._token}",
            "Accept": "application/vnd.github+json",
        }

        @_retry_transient
        async def _fetch() -> list[str]:
            async with httpx.AsyncClient() as client:
                resp = await client.get(url, headers=headers, follow_redirects=True)
                resp.raise_for_status()
                data = resp.json()
            return [item["path"] for item in data.get("tree", []) if item.get("type") == "blob"]

        try:
            return await _fetch()
        except Exception as exc:
            logger.debug("Failed to fetch repo tree: %s", exc)
            return []

    async def get_file_content(self, pr_info: PRInfo, path: str, ref: str) -> str:
        """Fetch file content at a specific ref via the REST API."""
        url = f"{_GITHUB_API_URL}/repos/{pr_info.owner}/{pr_info.repo}/contents/{path}"
        headers = {
            "Authorization": f"token {self._token}",
            "Accept": "application/vnd.github.v3+json",
        }

        @_retry_transient
        async def _fetch() -> str:
            async with httpx.AsyncClient() as client:
                resp = await client.get(
                    url, headers=headers, params={"ref": ref}, follow_redirects=True
                )
                resp.raise_for_status()
                data = resp.json()
                content = data.get("content", "")
                return base64.b64decode(content).decode("utf-8")

        try:
            return await _fetch()
        except ProviderError:
            raise
        except Exception as e:
            raise ProviderError(f"Failed to fetch file content: {e}") from e

    async def resolve_threads(self, pr_info: PRInfo, thread_ids: list[str]) -> int:
        """Resolve review threads by ID. Returns count of successfully resolved."""
        resolved = 0
        for tid in thread_ids:
            try:
                await self._graphql_request(_RESOLVE_THREAD_MUTATION, {"threadId": tid})
                resolved += 1
            except Exception as exc:
                logger.warning(
                    "Failed to resolve thread %s on PR %s: %s",
                    tid,
                    pr_info.url,
                    exc,
                )
        if resolved < len(thread_ids):
            logger.warning(
                "Resolved %d/%d threads on PR %s (%d failed)",
                resolved,
                len(thread_ids),
                pr_info.url,
                len(thread_ids) - resolved,
            )
        return resolved

    async def get_thread_id_for_comment(
        self,
        comment_node_id: str,
        pr_info: PRInfo,
    ) -> str | None:
        """Look up the review thread containing ``comment_node_id``.

        GitHub's GraphQL schema doesn't expose ``pullRequestReviewThread``
        directly on a ``PullRequestReviewComment``, so we paginate the PR's
        ``reviewThreads`` connection and match by comment node ID. Returns
        the thread's GraphQL ID (suitable for ``resolveReviewThread``), or
        ``None`` if no matching thread is found or it's already resolved.
        """
        cursor: str | None = None
        while True:
            try:
                data = await self._graphql_request(
                    _COMMENT_THREAD_QUERY,
                    {
                        "owner": pr_info.owner,
                        "repo": pr_info.repo,
                        "number": pr_info.number,
                        "cursor": cursor,
                    },
                )
            except Exception as exc:
                logger.warning(
                    "Failed to look up thread for comment %s: %s",
                    comment_node_id,
                    exc,
                )
                return None

            pr_data = (data.get("repository") or {}).get("pullRequest") or {}
            connection = pr_data.get("reviewThreads") or {
                "nodes": [],
                "pageInfo": {"hasNextPage": False},
            }
            for thread in connection.get("nodes") or []:
                comment_ids = {c["id"] for c in thread.get("comments", {}).get("nodes", [])}
                if comment_node_id in comment_ids:
                    if thread.get("isResolved"):
                        return None
                    thread_id: str = thread["id"]
                    return thread_id

            page = connection.get("pageInfo", {})
            if not page.get("hasNextPage"):
                return None
            cursor = page.get("endCursor")
            if cursor is None:
                logger.warning(
                    "hasNextPage=True but endCursor is None for comment %s; stopping pagination",
                    comment_node_id,
                )
                return None

    async def get_all_bot_threads(
        self, pr_info: PRInfo, bot_login: str | None = None
    ) -> list[BotThreadRecord]:
        """Fetch all bot-authored review threads on a PR (resolved and unresolved)."""
        threads: list[BotThreadRecord] = []
        viewer_login: str | None = None
        cursor: str | None = None

        while True:
            variables: dict[str, Any] = {
                "owner": pr_info.owner,
                "repo": pr_info.repo,
                "number": pr_info.number,
                "cursor": cursor,
            }
            try:
                data = await self._graphql_request(_REVIEW_THREADS_QUERY, variables)
            except ProviderError:
                raise
            except Exception as e:
                raise ProviderError(f"Failed to fetch review threads: {e}") from e

            if viewer_login is None:
                viewer_login = data["viewer"]["login"]

            effective_login = bot_login or viewer_login
            rt = data["repository"]["pullRequest"]["reviewThreads"]

            for node in rt["nodes"]:
                comments = node["comments"]["nodes"]
                if not comments:
                    continue
                first = comments[0]
                author = (first.get("author") or {}).get("login", "")
                if _normalize_login(author) != _normalize_login(effective_login):
                    continue
                reaction_groups = {
                    group.get("content", ""): [
                        user.get("login", "")
                        for user in (group.get("users") or {}).get("nodes", [])
                        if user.get("login")
                    ]
                    for group in first.get("reactionGroups") or []
                }
                threads.append(
                    BotThreadRecord(
                        thread_id=node["id"],
                        path=first.get("path", ""),
                        line=first.get("line") or first.get("originalLine") or 0,
                        body=first.get("body", ""),
                        is_resolved=bool(node["isResolved"]),
                        is_outdated=bool(node["isOutdated"]),
                        platform_comment_id=int(first.get("databaseId") or 0),
                        positive_reactors=reaction_groups.get("THUMBS_UP", []),
                        negative_reactors=reaction_groups.get("THUMBS_DOWN", []),
                    )
                )

            if rt["pageInfo"]["hasNextPage"]:
                cursor = rt["pageInfo"]["endCursor"]
            else:
                break

        logger.info(
            "get_all_bot_threads: %d thread(s) on PR %s (%d resolved)",
            len(threads),
            pr_info.url,
            sum(1 for t in threads if t.is_resolved),
        )
        return threads

    async def get_file_history(
        self,
        pr_info: PRInfo,
        paths: list[str],
        max_per_file: int = 5,
    ) -> dict[str, list[FileHistoryEntry]]:
        """Fetch recent commit history per file.

        Returns ``{path: [FileHistoryEntry, ...]}`` ordered most-recent first,
        capped at ``max_per_file`` per path. Used to give the review LLM
        context for "why does this code exist?" before it suggests deletion.

        Concurrency-bounded so a PR touching 50 files doesn't blow the rate
        limit; uses a small semaphore.
        """
        if not paths:
            return {}

        sem = asyncio.Semaphore(8)
        headers = {
            "Authorization": f"token {self._token}",
            "Accept": "application/vnd.github.v3+json",
        }
        base = f"{_GITHUB_API_URL}/repos/{pr_info.owner}/{pr_info.repo}/commits"

        async def _fetch_one(
            client: httpx.AsyncClient, path: str
        ) -> tuple[str, list[FileHistoryEntry]]:
            async with sem:
                try:
                    resp = await client.get(
                        base,
                        headers=headers,
                        params={"path": path, "per_page": max_per_file},
                    )
                    if resp.status_code != 200:
                        return path, []
                    data = resp.json()
                except Exception as exc:
                    logger.debug("File history fetch failed for %s: %s", path, exc)
                    return path, []

            entries: list[FileHistoryEntry] = []
            for item in data[:max_per_file]:
                commit = item.get("commit") or {}
                author = commit.get("author") or {}
                message = (commit.get("message") or "").strip()
                short_message = message.split("\n\n", 1)[0][:300]
                entries.append(
                    FileHistoryEntry(
                        sha=str(item.get("sha", ""))[:8],
                        message=short_message,
                        author=str(author.get("name", "")),
                        date=str(author.get("date", "")),
                    )
                )
            return path, entries

        async with httpx.AsyncClient(timeout=30) as client:
            results = await asyncio.gather(
                *[_fetch_one(client, p) for p in paths],
                return_exceptions=False,
            )

        return {path: hist for path, hist in results if hist}

    def triage_capabilities(self) -> TriageCapabilities:
        return GITHUB_TRIAGE_CAPABILITIES

    async def get_path_authors(
        self,
        pr_info: PRInfo,
        paths: list[str],
        *,
        ref: str = "",
        max_per_path: int = 20,
    ) -> dict[str, list[PathAuthorship]]:
        """Who the platform says has committed to each path, at ``ref``.

        ``author.login`` — the account GitHub resolved the commit to — rather
        than ``commit.author.name``, which is a string the committer wrote. A
        commit GitHub could not resolve yields an entry with an empty login,
        which the caller drops: an unattributed commit must not become an
        unverified name in a suggestion.

        Bounded the same way :meth:`get_file_history` is, with the same small
        semaphore: a pull request touching many files must not turn into a rate
        limit incident on a feature nobody is blocked on.

        **A failed lookup raises.** GitHub answers a path with no commits with
        an empty 200, so an empty result here is a fact; returning the same
        thing for a rate limit or a 502 would make an outage indistinguishable
        from "nobody has touched this file" — and the caller would cache that
        non-answer for the whole refresh interval and report `no_candidates`
        on the strength of it. Unlike :meth:`get_file_history`, whose result
        only enriches a prompt, this one is ranked on.
        """
        if not paths:
            return {}

        commit_ref = ref or pr_info.base_sha or pr_info.base_branch
        if not commit_ref:
            return {}

        sem = asyncio.Semaphore(8)
        headers = {
            "Authorization": f"token {self._token}",
            "Accept": "application/vnd.github.v3+json",
        }
        base = f"{_GITHUB_API_URL}/repos/{pr_info.owner}/{pr_info.repo}/commits"

        async def _fetch_one(
            client: httpx.AsyncClient, path: str
        ) -> tuple[str, list[PathAuthorship]]:
            async with sem:
                try:
                    resp = await client.get(
                        base,
                        headers=headers,
                        params={"path": path, "sha": commit_ref, "per_page": max_per_path},
                    )
                    if resp.status_code != 200:
                        raise ProviderError(
                            f"GitHub returned HTTP {resp.status_code} for the history of {path}"
                        )
                    data = resp.json()
                except ProviderError:
                    raise
                except Exception as exc:
                    raise ProviderError(f"Failed to read the history of {path}: {exc}") from exc

            entries: list[PathAuthorship] = []
            for item in (data or [])[:max_per_path]:
                author = item.get("author") or {}
                commit = item.get("commit") or {}
                entries.append(
                    PathAuthorship(
                        path=path,
                        login=str(author.get("login") or ""),
                        sha=str(item.get("sha", ""))[:12],
                        url=str(item.get("html_url") or ""),
                        at=iso_to_epoch(((commit.get("author") or {}).get("date")) or ""),
                    )
                )
            return path, entries

        async with httpx.AsyncClient(timeout=30) as client:
            results = await asyncio.gather(
                *[_fetch_one(client, p) for p in paths], return_exceptions=False
            )
        return {path: entries for path, entries in results if entries}

    async def get_human_review_comments(
        self, pr_info: PRInfo, bot_login: str
    ) -> list[HumanReviewComment]:
        """Fetch all non-bot review comments (line-level) on a PR."""
        bot_norm = _normalize_login(bot_login)

        @_retry_transient
        def _fetch() -> list[HumanReviewComment]:
            gh_repo = self._github.get_repo(f"{pr_info.owner}/{pr_info.repo}")
            pr = gh_repo.get_pull(pr_info.number)
            results: list[HumanReviewComment] = []
            for c in pr.get_review_comments():
                author = c.user.login if c.user else ""
                if _normalize_login(author) == bot_norm:
                    continue
                results.append(
                    HumanReviewComment(
                        path=c.path or "",
                        line=(c.line or c.original_line or 0),
                        body=c.body or "",
                        author=author,
                    )
                )
            return results

        try:
            return await asyncio.to_thread(_fetch)
        except Exception as e:
            raise ProviderError(f"Failed to fetch human review comments: {e}") from e

    # ── Merge gate (Phase 4) ──

    def gate_capabilities(self) -> GateCapabilities:
        return GITHUB_CAPABILITIES

    async def get_ci_state(self, pr_info: PRInfo) -> CIState:
        """Combined check runs *and* legacy commit statuses on the head commit.

        Both surfaces matter: Actions reports check runs while most third-party
        CI still posts commit statuses, and a repository using only the latter
        would otherwise look like it has no CI at all.

        A run that has not concluded counts as pending, and anything that is
        neither success nor neutral/skipped counts as failing. Unrecognized
        conclusions are treated as failures on purpose: the gate must never
        approve on a check outcome it does not understand.
        """

        @_retry_transient
        def _fetch() -> CIState:
            gh_repo = self._github.get_repo(f"{pr_info.owner}/{pr_info.repo}")
            sha = pr_info.head_sha or gh_repo.get_pull(pr_info.number).head.sha
            commit = gh_repo.get_commit(sha)
            failing: list[str] = []
            pending: list[str] = []
            total = 0
            for run in commit.get_check_runs():
                name = run.name or "check"
                if name in _OWN_STATUS_CONTEXTS:
                    # One of Mira's own published check runs. Counting it would
                    # let Mira read its own verdict back as a failing build.
                    continue
                total += 1
                if (run.status or "") != "completed":
                    pending.append(name)
                    continue
                conclusion = (run.conclusion or "").lower()
                if conclusion in {"success", "neutral", "skipped"}:
                    continue
                failing.append(name)
            seen_contexts: set[str] = set()
            for status in commit.get_statuses():
                # Chronological, newest first, per context — only the first
                # entry for a context describes its current state.
                context = status.context or "status"
                if context in seen_contexts or context in _OWN_STATUS_CONTEXTS:
                    continue
                seen_contexts.add(context)
                total += 1
                state = (status.state or "").lower()
                if state == "pending":
                    pending.append(context)
                elif state != "success":
                    failing.append(context)
            if pending:
                return CIState(state="pending", total=total, failing=failing, pending=pending)
            if failing:
                return CIState(state="failure", total=total, failing=failing, pending=pending)
            if total == 0:
                return CIState(state="none", total=0)
            return CIState(state="success", total=total)

        try:
            return await asyncio.to_thread(_fetch)
        except Exception as e:
            logger.warning("Could not read CI state for %s: %s", pr_info.url, e)
            return CIState(state="unknown")

    async def get_pr_labels(self, pr_info: PRInfo) -> list[str]:
        @_retry_transient
        def _fetch() -> list[str]:
            gh_repo = self._github.get_repo(f"{pr_info.owner}/{pr_info.repo}")
            issue = gh_repo.get_issue(pr_info.number)
            return [label.name for label in issue.get_labels() if label.name]

        try:
            return await asyncio.to_thread(_fetch)
        except Exception as e:
            # An empty list would read as "no blocking label present".
            logger.warning("Could not read labels for %s: %s", pr_info.url, e)
            raise ProviderError(f"Failed to read labels: {e}") from e

    async def get_author_association(self, pr_info: PRInfo) -> str:
        @_retry_transient
        def _fetch() -> str:
            gh_repo = self._github.get_repo(f"{pr_info.owner}/{pr_info.repo}")
            pr = gh_repo.get_pull(pr_info.number)
            association = (getattr(pr, "raw_data", None) or {}).get("author_association", "")
            return str(association or "").upper() or "UNKNOWN"

        try:
            return await asyncio.to_thread(_fetch)
        except Exception as e:
            logger.warning("Could not read author association for %s: %s", pr_info.url, e)
            return "UNKNOWN"

    async def get_pr_change_stats(self, pr_info: PRInfo) -> list[FileChangeStat]:
        """Per-file counts from the files API — no diff body, no 406 size cliff.

        Lists every file the PR touches, deletions and binaries included: the
        merge gate asks this to find protected paths, not to decide what to
        review.
        """

        @_retry_transient
        def _fetch() -> list[FileChangeStat]:
            gh_repo = self._github.get_repo(f"{pr_info.owner}/{pr_info.repo}")
            pr = gh_repo.get_pull(pr_info.number)
            return [
                FileChangeStat(
                    path=file.filename,
                    added_lines=int(getattr(file, "additions", 0) or 0),
                    deleted_lines=int(getattr(file, "deletions", 0) or 0),
                )
                for file in pr.get_files()
            ]

        try:
            return await asyncio.to_thread(_fetch)
        except Exception as e:
            logger.warning("Could not read change stats for %s: %s", pr_info.url, e)
            raise ProviderError(f"Failed to read change stats: {e}") from e

    async def get_codeowners(self, pr_info: PRInfo, ref: str = "") -> tuple[str, str]:
        """First CODEOWNERS found at ``ref``, in GitHub's own order.

        Raises when the lookup itself fails: "we could not check" and "there
        are no owners" have to reach the gate as different answers, because
        only the second one is safe to approve past.

        ``ref`` defaults to the head, which is what the gate reads. Reviewer
        triage passes the base, because a branch that could add itself an owner
        would otherwise be nominating its own reviewer.
        """
        ref = ref or pr_info.head_sha or pr_info.head_branch
        last_error: Exception | None = None
        for candidate in CODEOWNERS_LOCATIONS["github"]:
            try:
                content = await self.get_file_content(pr_info, candidate, ref)
            except Exception as exc:  # noqa: BLE001 - try the next known path
                last_error = exc
                continue
            if content:
                return candidate, content
        if last_error is not None:
            raise ProviderError(f"Failed to read CODEOWNERS: {last_error}") from last_error
        return "", ""

    async def publish_gate_status(
        self,
        pr_info: PRInfo,
        *,
        context: str,
        conclusion: str,
        title: str,
        summary: str,
        target_url: str = "",
    ) -> str:
        """Publish the decision as a check run on the head commit.

        Check runs are keyed by name and GitHub surfaces the latest run for a
        name, so re-publishing after a retry replaces rather than appends.
        Requires `checks:write`; without it this raises and the caller records
        a failed delivery instead of pretending the status went out.
        """
        conclusion_map = {"success": "success", "failure": "failure", "neutral": "neutral"}

        @_retry_transient
        def _publish() -> str:
            gh_repo = self._github.get_repo(f"{pr_info.owner}/{pr_info.repo}")
            sha = pr_info.head_sha or gh_repo.get_pull(pr_info.number).head.sha
            extra = {"details_url": target_url} if target_url else {}
            run = gh_repo.create_check_run(
                name=context,
                head_sha=sha,
                status="completed",
                conclusion=conclusion_map.get(conclusion, "neutral"),
                output={"title": title[:255], "summary": summary[:65535]},
                **extra,
            )
            return str(getattr(run, "id", "") or "")

        try:
            return await asyncio.to_thread(_publish)
        except Exception as e:
            logger.warning("Could not publish gate status on %s: %s", pr_info.url, e)
            raise ProviderError(f"Failed to publish gate status: {e}") from e

    # ── Pre-merge checks (Phase 6) ──

    def checks_capabilities(self) -> CheckCapabilities:
        return GITHUB_CHECK_CAPABILITIES

    async def get_issue(
        self, pr_info: PRInfo, number: int, *, owner: str = "", repo: str = ""
    ) -> IssueInfo | None:
        """One issue, or None when GitHub says there is no such issue.

        A 404 is the answer "it does not exist" and is the only failure turned
        into ``None``. Everything else — a rate limit, a revoked token, a 500 —
        propagates, because a check that reported those as a missing issue
        would blame a pull request for an outage.

        Pull requests are issues on GitHub and this deliberately does not
        filter them out: a repository whose convention is to reference a
        tracking pull request is referencing something that exists, which is
        all this method claims to establish.
        """
        target_owner = owner or pr_info.owner
        target_repo = repo or pr_info.repo

        @_retry_transient
        def _fetch() -> IssueInfo | None:
            gh_repo = self._github.get_repo(f"{target_owner}/{target_repo}")
            try:
                issue = gh_repo.get_issue(number=int(number))
            except GithubException as exc:
                if getattr(exc, "status", 0) == 404:
                    return None
                raise
            return IssueInfo(
                number=int(getattr(issue, "number", number) or number),
                title=str(getattr(issue, "title", "") or ""),
                body=str(getattr(issue, "body", "") or ""),
                state=str(getattr(issue, "state", "") or ""),
                url=str(getattr(issue, "html_url", "") or ""),
                labels=[str(getattr(label, "name", "") or "") for label in (issue.labels or [])],
                owner=target_owner,
                repo=target_repo,
            )

        return await asyncio.to_thread(_fetch)

    async def get_ci_failures(
        self, pr_info: PRInfo, *, max_jobs: int = 3, max_log_bytes: int = 16_000
    ) -> list[CIJobFailure]:
        """Failing check runs and statuses on the head commit, with their output.

        The evidence is the check run's own ``output`` — the summary and text
        the action published — rather than the raw Actions log archive. That is
        a deliberate trade: the archive is a zip fetched over a redirect,
        typically megabytes, and on the deployment profile Mira targets the
        cost of downloading it dwarfs the value of the extra lines. What an
        action chose to publish is what it wanted a reader to see.

        A failing check run that published no output is returned with
        ``log_unavailable`` set, so the check can say "this job failed and told
        us nothing" instead of quoting an empty string as evidence.
        """

        @_retry_transient
        def _fetch() -> list[CIJobFailure]:
            gh_repo = self._github.get_repo(f"{pr_info.owner}/{pr_info.repo}")
            sha = pr_info.head_sha or gh_repo.get_pull(pr_info.number).head.sha
            commit = gh_repo.get_commit(sha)
            failures: list[CIJobFailure] = []
            for run in commit.get_check_runs():
                if len(failures) >= max_jobs:
                    break
                name = run.name or "check"
                if name in _OWN_STATUS_CONTEXTS:
                    continue
                if (run.status or "") != "completed":
                    continue
                conclusion = (run.conclusion or "").lower()
                if conclusion in {"success", "neutral", "skipped", ""}:
                    continue
                output = getattr(run, "output", None)
                pieces = [
                    str(getattr(output, "title", "") or ""),
                    str(getattr(output, "summary", "") or ""),
                    str(getattr(output, "text", "") or ""),
                ]
                excerpt = "\n".join(piece for piece in pieces if piece).strip()
                failures.append(
                    CIJobFailure(
                        name=name,
                        job_id=str(getattr(run, "id", "") or ""),
                        conclusion=conclusion,
                        url=str(getattr(run, "html_url", "") or ""),
                        step=str(getattr(output, "title", "") or ""),
                        # From the end: a build failure is at the bottom.
                        excerpt=excerpt[-max_log_bytes:],
                        log_unavailable=not excerpt,
                    )
                )
            seen: set[str] = set()
            for status in commit.get_statuses():
                if len(failures) >= max_jobs:
                    break
                context = status.context or "status"
                if context in seen or context in _OWN_STATUS_CONTEXTS:
                    continue
                seen.add(context)
                if (status.state or "").lower() in {"success", "pending", ""}:
                    continue
                description = str(getattr(status, "description", "") or "")
                failures.append(
                    CIJobFailure(
                        name=context,
                        conclusion=str(status.state or "failure").lower(),
                        url=str(getattr(status, "target_url", "") or ""),
                        excerpt=description[-max_log_bytes:],
                        log_unavailable=not description,
                    )
                )
            return failures

        return await asyncio.to_thread(_fetch)

    async def publish_checks_status(
        self,
        pr_info: PRInfo,
        *,
        context: str,
        conclusion: str,
        title: str,
        summary: str,
        target_url: str = "",
    ) -> str:
        return await self.publish_gate_status(
            pr_info,
            context=context,
            conclusion=conclusion,
            title=title,
            summary=summary,
            target_url=target_url,
        )

    # ── Assisted correction (Phase 5) ──

    def autofix_capabilities(self) -> AutofixCapabilities:
        return GITHUB_AUTOFIX_CAPABILITIES

    async def get_actor_permission(self, pr_info: PRInfo, login: str) -> str:
        """The account's permission on the repository, in GitHub's own words.

        Raises nothing: an account with no relationship to the repository 404s,
        which is `none`, and any other failure is `unknown`. Both refuse — the
        distinction exists so the refusal can say which happened.
        """

        @_retry_transient
        def _fetch() -> str:
            gh_repo = self._github.get_repo(f"{pr_info.owner}/{pr_info.repo}")
            try:
                level = gh_repo.get_collaborator_permission(login)
            except GithubException as exc:
                if exc.status == 404:
                    return "none"
                raise
            return str(level or "none").lower()

        try:
            return await asyncio.to_thread(_fetch)
        except Exception as exc:  # noqa: BLE001 - unreadable is a refusal, not a crash
            logger.warning("Could not read %s's permission on %s: %s", login, pr_info.url, exc)
            return "unknown"

    async def get_default_branch(self, pr_info: PRInfo) -> str:
        @_retry_transient
        def _fetch() -> str:
            gh_repo = self._github.get_repo(f"{pr_info.owner}/{pr_info.repo}")
            return str(gh_repo.default_branch or "")

        try:
            return await asyncio.to_thread(_fetch)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Could not read the default branch of %s: %s", pr_info.url, exc)
            return ""

    async def get_branch_head(self, pr_info: PRInfo, branch: str) -> str:
        """Tip sha of ``branch``, or "" when it does not exist.

        A 404 is a real answer here — "no such branch" is what the caller is
        asking about — so it is not raised.
        """

        @_retry_transient
        def _fetch() -> str:
            gh_repo = self._github.get_repo(f"{pr_info.owner}/{pr_info.repo}")
            try:
                ref = gh_repo.get_git_ref(f"heads/{branch}")
            except GithubException as exc:
                if exc.status == 404:
                    return ""
                raise
            return str(getattr(getattr(ref, "object", None), "sha", "") or "")

        try:
            return await asyncio.to_thread(_fetch)
        except Exception as e:
            raise ProviderError(f"Failed to read branch {branch}: {e}") from e

    async def create_branch(self, pr_info: PRInfo, branch: str, from_sha: str) -> None:
        """Create ``refs/heads/<branch>`` at ``from_sha``.

        `create_git_ref` only creates. There is no update call here and no
        `force` argument anywhere in this class: a branch that already exists
        makes this raise, and the caller adopts it rather than resetting it.
        """

        @_retry_transient
        def _create() -> None:
            gh_repo = self._github.get_repo(f"{pr_info.owner}/{pr_info.repo}")
            sha = from_sha or gh_repo.get_pull(pr_info.number).head.sha
            gh_repo.create_git_ref(ref=f"refs/heads/{branch}", sha=sha)

        try:
            await asyncio.to_thread(_create)
        except Exception as e:
            raise ProviderError(f"Failed to create branch {branch}: {e}") from e

    async def files_match(self, pr_info: PRInfo, branch: str, files: dict[str, str]) -> bool:
        """Whether every path already holds exactly this content on ``branch``."""

        def _check() -> bool:
            gh_repo = self._github.get_repo(f"{pr_info.owner}/{pr_info.repo}")
            for path, content in files.items():
                try:
                    blob = gh_repo.get_contents(path, ref=branch)
                except GithubException:
                    return False
                if isinstance(blob, list):
                    return False
                existing = base64.b64decode(blob.content or "").decode("utf-8", "replace")
                if existing != content:
                    return False
            return True

        try:
            return await asyncio.to_thread(_check)
        except Exception as exc:  # noqa: BLE001 - any doubt means "commit it"
            logger.debug("Could not compare %s against %s: %s", branch, pr_info.url, exc)
            return False

    async def commit_files(
        self, pr_info: PRInfo, branch: str, files: dict[str, str], message: str
    ) -> str:
        """One commit carrying every changed file, fast-forward from the branch head.

        Built through the git data API — blob, tree, commit, ref update — rather
        than one contents-API call per file, so a multi-file fix is one commit
        and one atomic ref move rather than a half-applied sequence somebody has
        to reason about.

        The ref update is a plain (non-forced) update from the branch's current
        head, so a concurrent push makes it fail rather than lose that push.
        """

        @_retry_transient
        def _commit() -> str:
            from github.InputGitTreeElement import InputGitTreeElement

            gh_repo = self._github.get_repo(f"{pr_info.owner}/{pr_info.repo}")
            ref = gh_repo.get_git_ref(f"heads/{branch}")
            parent = gh_repo.get_git_commit(ref.object.sha)
            base_tree = gh_repo.get_git_tree(parent.tree.sha, recursive=True)
            # Carry each path's existing mode forward. A tree element is a
            # whole entry, not a content patch, so writing `100644` over a
            # `100755` file silently removes its executable bit — a change the
            # fix never proposed, does not appear in the rendered diff, and
            # stops a script from running. Genuinely new paths get `100644`.
            modes = {
                str(element.path): str(element.mode or "100644")
                for element in (base_tree.tree or [])
                if str(element.type or "") == "blob"
            }
            elements = [
                InputGitTreeElement(
                    path=path,
                    mode=modes.get(path, "100644"),
                    type="blob",
                    content=content,
                )
                for path, content in sorted(files.items())
            ]
            tree = gh_repo.create_git_tree(elements, base_tree)
            commit = gh_repo.create_git_commit(message, tree, [parent])
            # `force=False` is the default and is passed explicitly: this is the
            # one call in Mira that could rewrite history, and a reader should
            # not have to know the default to be sure it does not.
            ref.edit(sha=commit.sha, force=False)
            return str(commit.sha)

        try:
            return await asyncio.to_thread(_commit)
        except Exception as e:
            raise ProviderError(f"Failed to commit to {branch}: {e}") from e

    async def create_pull_request(
        self, pr_info: PRInfo, *, head: str, base: str, title: str, body: str
    ) -> tuple[int, str]:
        @_retry_transient
        def _open() -> tuple[int, str]:
            gh_repo = self._github.get_repo(f"{pr_info.owner}/{pr_info.repo}")
            created = gh_repo.create_pull(title=title, body=body, head=head, base=base)
            return int(created.number), str(created.html_url or "")

        try:
            return await asyncio.to_thread(_open)
        except Exception as e:
            raise ProviderError(f"Failed to open a pull request from {head}: {e}") from e

    async def find_open_pull_request(self, pr_info: PRInfo, head: str) -> tuple[int, str] | None:
        def _find() -> tuple[int, str] | None:
            gh_repo = self._github.get_repo(f"{pr_info.owner}/{pr_info.repo}")
            for candidate in gh_repo.get_pulls(state="open", head=f"{pr_info.owner}:{head}"):
                return int(candidate.number), str(candidate.html_url or "")
            return None

        try:
            return await asyncio.to_thread(_find)
        except Exception as e:
            # `None` means "there is no pull request from this branch", and the
            # publisher opens one on that answer — after the branch and commit
            # already exist. A lookup that failed is not that answer, and
            # returning it as one is how a retry opens a second pull request.
            raise ProviderError(f"Failed to look for an open pull request from {head}: {e}") from e

    async def pr_head_is_fork(self, pr_info: PRInfo) -> bool:
        """Whether the head branch lives in a different repository.

        Anything that cannot be determined answers True. A commit onto a fork's
        branch is a cross-repository write nobody authorized, and "we could not
        tell" is not a reason to make one.
        """

        def _check() -> bool:
            gh_repo = self._github.get_repo(f"{pr_info.owner}/{pr_info.repo}")
            pull = gh_repo.get_pull(pr_info.number)
            head_repo = getattr(pull.head, "repo", None)
            if head_repo is None:
                return True
            full_name = str(head_repo.full_name or "").lower()
            return full_name != f"{pr_info.owner}/{pr_info.repo}".lower()

        try:
            return await asyncio.to_thread(_check)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Could not tell whether %s comes from a fork: %s", pr_info.url, exc)
            return True

    async def get_review_inline_comments(self, pr_info: PRInfo, review_id: int) -> list[str]:
        """Return the bodies of the inline comments that belong to one review
        (matched via ``pull_request_review_id``). Used to classify whether an
        approval was a substantive review or a rubber-stamp."""

        @_retry_transient
        def _fetch() -> list[str]:
            gh_repo = self._github.get_repo(f"{pr_info.owner}/{pr_info.repo}")
            pr = gh_repo.get_pull(pr_info.number)
            return [
                c.body or ""
                for c in pr.get_review_comments()
                if getattr(c, "pull_request_review_id", None) == review_id
            ]

        try:
            return await asyncio.to_thread(_fetch)
        except Exception as e:
            raise ProviderError(f"Failed to fetch review inline comments: {e}") from e
