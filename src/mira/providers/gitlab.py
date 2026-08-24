"""GitLab provider using the REST v4 API.

Mirrors GitHubProvider but speaks GitLab: merge requests instead of pull
requests, discussions/notes instead of review threads, and position-anchored
inline notes. Authentication is a static group/project access token sent as
``PRIVATE-TOKEN`` (the token's user is who comments are posted as).
"""

from __future__ import annotations

import asyncio
import logging
import re
from contextlib import suppress
from typing import Any
from urllib.parse import quote

import httpx

from mira.exceptions import ProviderError
from mira.gate.capabilities import GITLAB_CAPABILITIES, GateCapabilities
from mira.gate.codeowners import CODEOWNERS_LOCATIONS
from mira.gate.models import STATUS_CONTEXT, CIState
from mira.models import (
    BotThreadRecord,
    FileHistoryEntry,
    HumanReviewComment,
    PRInfo,
    ReviewResult,
    UnresolvedThread,
)
from mira.platforms import profiles
from mira.providers.base import BaseProvider
from mira.providers.formatting import format_comment_body, format_key_issues

logger = logging.getLogger(__name__)

# https://gitlab.com/group/sub/project/-/merge_requests/123  or  group/project!123
_MR_URL_PATTERN = re.compile(
    r"(?:https?://[^/]+/)?(?P<owner>.+?)/(?P<repo>[^/]+?)(?:/-/merge_requests/|!)(?P<number>\d+)"
)


def parse_mr_url(mr_url: str) -> tuple[str, str, int]:
    """Parse an MR URL or shorthand into (owner, repo, iid).

    ``owner`` may contain slashes (nested groups). The project path for the API
    is ``owner/repo``.
    """
    match = _MR_URL_PATTERN.match(mr_url.strip())
    if not match:
        raise ProviderError(
            f"Cannot parse MR URL: {mr_url}. Expected "
            "https://gitlab.com/group/project/-/merge_requests/123 or group/project!123"
        )
    return match.group("owner"), match.group("repo"), int(match.group("number"))


def _build_unified_diff(changes: list[dict[str, Any]]) -> str:
    """Assemble GitLab's per-file ``diff`` fragments into one unified diff.

    GitLab returns each file's ``@@`` hunks without the ``---``/``+++`` headers
    the diff parser needs, so we prepend them (using /dev/null for added or
    deleted files).
    """
    parts: list[str] = []
    for ch in changes:
        diff = ch.get("diff") or ""
        if not diff:
            continue
        old_path = ch.get("old_path", "")
        new_path = ch.get("new_path", "")
        old = "/dev/null" if ch.get("new_file") else f"a/{old_path}"
        new = "/dev/null" if ch.get("deleted_file") else f"b/{new_path}"
        parts.append(f"diff --git a/{old_path} b/{new_path}")
        # Without the file-mode line, a `diff --git` header paired with a
        # /dev/null side makes unidiff emit the file twice (the empty second
        # entry then clobbers the real line ranges and every finding is
        # dropped as "outside the diff").
        if ch.get("new_file"):
            parts.append("new file mode 100644")
        elif ch.get("deleted_file"):
            parts.append("deleted file mode 100644")
        parts.append(f"--- {old}")
        parts.append(f"+++ {new}")
        parts.append(diff if diff.endswith("\n") else diff + "\n")
    return "".join(p if p.endswith("\n") else p + "\n" for p in parts)


class GitLabProvider(BaseProvider):
    """GitLab code hosting provider (REST v4)."""

    def __init__(self, token: str) -> None:
        if not token:
            raise ProviderError("GitLab token is required")
        self._token = token
        self._api = profiles.resolve("gitlab")["api_url"] or "https://gitlab.com/api/v4"
        self._username: str | None = None

    # ── low-level HTTP ──────────────────────────────────────────────

    def _project(self, pr_info: PRInfo) -> str:
        pid = quote(f"{pr_info.owner}/{pr_info.repo}", safe="")
        return f"{self._api}/projects/{pid}"

    def _mr(self, pr_info: PRInfo) -> str:
        return f"{self._project(pr_info)}/merge_requests/{pr_info.number}"

    async def _request(
        self, method: str, url: str, *, ok: tuple[int, ...] = (200, 201), **kw: Any
    ) -> httpx.Response:
        headers = {"PRIVATE-TOKEN": self._token, **kw.pop("headers", {})}
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.request(method, url, headers=headers, **kw)
        if resp.status_code not in ok:
            raise ProviderError(f"GitLab {method} {url} → {resp.status_code}: {resp.text[:300]}")
        return resp

    async def _paginate(self, url: str) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        next_url: str | None = url + ("&" if "?" in url else "?") + "per_page=100"
        async with httpx.AsyncClient(timeout=30) as client:
            while next_url:
                resp = await client.get(next_url, headers={"PRIVATE-TOKEN": self._token})
                resp.raise_for_status()
                out.extend(resp.json())
                next_url = _next_link(resp.headers.get("link", ""))
        return out

    async def _self_username(self) -> str:
        """The token user's own username — who comments are actually posted as.

        Group/project access tokens post as a bot user (``project_<id>_bot_…``)
        whose name has nothing to do with MIRA_BOT_NAME, so thread dedup and
        auto-resolution must match on this, not the configured display name.
        """
        if self._username is None:
            try:
                resp = await self._request("GET", f"{self._api}/user")
                self._username = (resp.json() or {}).get("username", "") or ""
            except Exception as exc:
                logger.warning("Failed to resolve GitLab bot identity: %s", exc)
                self._username = ""
        return self._username

    # ── PR (MR) read ────────────────────────────────────────────────

    async def get_pr_info(self, pr_url: str) -> PRInfo:
        owner, repo, iid = parse_mr_url(pr_url)
        pid = quote(f"{owner}/{repo}", safe="")
        try:
            resp = await self._request("GET", f"{self._api}/projects/{pid}/merge_requests/{iid}")
            mr = resp.json()
        except ProviderError:
            raise
        except Exception as e:
            raise ProviderError(f"Failed to fetch MR info: {e}") from e
        return PRInfo(
            title=mr.get("title") or "",
            description=mr.get("description") or "",
            base_branch=mr.get("target_branch") or "",
            head_branch=mr.get("source_branch") or "",
            url=mr.get("web_url") or pr_url,
            number=iid,
            owner=owner,
            repo=repo,
            base_sha=(mr.get("diff_refs") or {}).get("base_sha") or "",
            head_sha=mr.get("sha") or "",
            platform="gitlab",
            author=((mr.get("author") or {}).get("username")) or "",
            author_avatar_url=((mr.get("author") or {}).get("avatar_url")) or "",
            draft=bool(mr.get("draft") or mr.get("work_in_progress") or False),
        )

    async def _changes(self, pr_info: PRInfo) -> dict[str, Any]:
        resp = await self._request("GET", f"{self._mr(pr_info)}/changes")
        return resp.json()

    async def get_pr_diff(self, pr_info: PRInfo) -> str:
        try:
            data = await self._changes(pr_info)
        except ProviderError:
            raise
        except Exception as e:
            raise ProviderError(f"Failed to fetch MR diff: {e}") from e
        return _build_unified_diff(data.get("changes", []))

    async def get_compare_diff(self, pr_info: PRInfo, base_sha: str, head_sha: str) -> str:
        """Diff between two commits, for round 2+ incremental reviews."""
        if base_sha == head_sha or not base_sha or not head_sha:
            return ""
        url = f"{self._project(pr_info)}/repository/compare?from={base_sha}&to={head_sha}"
        try:
            resp = await self._request("GET", url)
            return _build_unified_diff(resp.json().get("diffs", []))
        except Exception as e:
            raise ProviderError(f"Failed to fetch compare diff: {e}") from e

    async def get_file_content(self, pr_info: PRInfo, path: str, ref: str) -> str:
        """Raw file content at a ref — used to verify a thread's fix landed."""
        url = (
            f"{self._project(pr_info)}/repository/files/"
            f"{quote(path, safe='')}/raw?ref={quote(ref, safe='')}"
        )
        try:
            resp = await self._request("GET", url, ok=(200, 404))
        except ProviderError as exc:
            logger.warning("Failed to fetch %s@%s: %s", path, ref, exc)
            return ""
        return resp.text if resp.status_code == 200 else ""

    async def get_repo_tree(self, pr_info: PRInfo, ref: str) -> list[str]:
        """Every file path in the repo at a ref, for JIT cross-file context."""
        url = (
            f"{self._project(pr_info)}/repository/tree"
            f"?recursive=true&pagination=keyset&ref={quote(ref, safe='')}"
        )
        try:
            items = await self._paginate(url)
        except Exception as exc:
            logger.debug("Failed to fetch repo tree: %s", exc)
            return []
        return [it["path"] for it in items if it.get("type") == "blob"]

    async def get_file_history(
        self, pr_info: PRInfo, paths: list[str], max_per_file: int = 5
    ) -> dict[str, list[FileHistoryEntry]]:
        """Recent commits per file (most-recent first), for "why does this exist?" context."""
        if not paths:
            return {}

        sem = asyncio.Semaphore(8)
        base = f"{self._project(pr_info)}/repository/commits"
        headers = {"PRIVATE-TOKEN": self._token}

        async def _fetch_one(
            client: httpx.AsyncClient, path: str
        ) -> tuple[str, list[FileHistoryEntry]]:
            async with sem:
                try:
                    resp = await client.get(
                        base,
                        headers=headers,
                        params={
                            "path": path,
                            "ref_name": pr_info.head_branch,
                            "per_page": max_per_file,
                        },
                    )
                    if resp.status_code != 200:
                        return path, []
                    data = resp.json()
                except Exception as exc:
                    logger.debug("File history fetch failed for %s: %s", path, exc)
                    return path, []

            entries: list[FileHistoryEntry] = []
            for item in data[:max_per_file]:
                message = (item.get("message") or item.get("title") or "").strip()
                entries.append(
                    FileHistoryEntry(
                        sha=str(item.get("short_id") or item.get("id", ""))[:8],
                        message=message.split("\n\n", 1)[0][:300],
                        author=str(item.get("author_name", "")),
                        date=str(item.get("authored_date", "")),
                    )
                )
            return path, entries

        async with httpx.AsyncClient(timeout=30) as client:
            results = await asyncio.gather(*[_fetch_one(client, p) for p in paths])
        return {path: hist for path, hist in results if hist}

    # ── posting ─────────────────────────────────────────────────────

    async def post_review(
        self, pr_info: PRInfo, result: ReviewResult, bot_name: str = "miracodeai"
    ) -> list[int]:
        if not result.comments:
            return []
        try:
            data = await self._changes(pr_info)
        except Exception as e:
            raise ProviderError(f"Failed to load MR diff_refs: {e}") from e
        diff_refs = data.get("diff_refs") or {}

        posted = 0
        posted_ids = [0] * len(result.comments)
        for index, comment in enumerate(result.comments):
            body = format_comment_body(comment, bot_name=bot_name)
            line = (
                comment.end_line
                if comment.end_line and comment.end_line > comment.line
                else comment.line
            )
            position = {
                "position[position_type]": "text",
                "position[base_sha]": diff_refs.get("base_sha", ""),
                "position[start_sha]": diff_refs.get("start_sha", ""),
                "position[head_sha]": diff_refs.get("head_sha", ""),
                "position[old_path]": comment.path,
                "position[new_path]": comment.path,
                "position[new_line]": str(line),
            }
            try:
                response = await self._request(
                    "POST",
                    f"{self._mr(pr_info)}/discussions",
                    data={"body": body, **position},
                )
                discussion = response.json() or {}
                comment.platform_thread_id = str(discussion.get("id") or "")
                notes = discussion.get("notes") or []
                if notes:
                    posted_ids[index] = int(notes[0].get("id") or 0)
                posted += 1
            except ProviderError as exc:
                # GitLab 400s when a position lands on an unchanged/context line.
                # Fall back to a plain note so the finding still surfaces.
                logger.warning(
                    "Inline note failed for %s:%s (%s); posting as a note", comment.path, line, exc
                )
                note = f"**`{comment.path}:{line}`**\n\n{body}"
                try:
                    await self.post_comment(pr_info, note)
                    posted += 1
                except ProviderError:
                    logger.warning("Plain-note fallback also failed for %s:%s", comment.path, line)

        review_body = ""
        if result.summary:
            review_body = f"**Mira Review Summary**\n\n{result.summary}"
        if result.key_issues:
            review_body += format_key_issues(result.key_issues)
        if review_body:
            try:
                await self.post_comment(pr_info, review_body)
            except ProviderError as exc:
                logger.warning("Failed to post MR summary note: %s", exc)

        logger.info("GitLab: posted %d/%d review notes", posted, len(result.comments))
        return posted_ids

    async def post_comment(self, pr_info: PRInfo, body: str) -> None:
        await self._request("POST", f"{self._mr(pr_info)}/notes", data={"body": body})

    async def find_bot_comment(self, pr_info: PRInfo, marker: str) -> int | None:
        try:
            notes = await self._paginate(f"{self._mr(pr_info)}/notes")
        except Exception as e:
            raise ProviderError(f"Failed to list MR notes: {e}") from e
        for note in notes:
            if marker in (note.get("body") or ""):
                return int(note["id"])
        return None

    async def update_comment(self, pr_info: PRInfo, comment_id: int, body: str) -> None:
        await self._request("PUT", f"{self._mr(pr_info)}/notes/{comment_id}", data={"body": body})

    async def get_comment_body(self, pr_info: PRInfo, comment_id: int) -> str:
        """Fetch an MR note's body by id. Best-effort."""
        try:
            resp = await self._request("GET", f"{self._mr(pr_info)}/notes/{comment_id}")
            return (resp.json().get("body") or "")[:1500]
        except Exception:
            return ""

    async def get_discussion_root_body(self, pr_info: PRInfo, discussion_id: str) -> str:
        """First note of a discussion — the bot's original comment. Best-effort.

        GitLab's note webhook carries the discussion id but not the parent note
        id, so this gives the thread-reply classifier the original suggestion as
        context (parity with GitHub's ``in_reply_to_id`` lookup).
        """
        try:
            resp = await self._request("GET", f"{self._mr(pr_info)}/discussions/{discussion_id}")
            notes = resp.json().get("notes", [])
            return (notes[0].get("body") or "")[:1500] if notes else ""
        except Exception:
            return ""

    async def reply_to_review_comment(self, pr_info: PRInfo, comment_id: int, body: str) -> None:
        discussion_id = await self.get_thread_id_for_comment(str(comment_id), pr_info)
        if not discussion_id:
            # No discussion for this note — fall back to a plain MR note.
            await self.post_comment(pr_info, body)
            return
        await self._request(
            "POST",
            f"{self._mr(pr_info)}/discussions/{discussion_id}/notes",
            data={"body": body},
        )

    # ── discussions / threads ───────────────────────────────────────

    async def _discussions(self, pr_info: PRInfo) -> list[dict[str, Any]]:
        return await self._paginate(f"{self._mr(pr_info)}/discussions")

    @staticmethod
    def _first_inline_note(discussion: dict[str, Any]) -> dict[str, Any] | None:
        for note in discussion.get("notes", []):
            if note.get("type") == "DiffNote" or note.get("position"):
                return note
        return None

    async def get_all_bot_threads(
        self, pr_info: PRInfo, bot_login: str | None = None
    ) -> list[BotThreadRecord]:
        try:
            discussions = await self._discussions(pr_info)
        except Exception as e:
            raise ProviderError(f"Failed to fetch MR discussions: {e}") from e
        bot_identities = {n for n in (await self._self_username(), bot_login) if n}
        records: list[BotThreadRecord] = []
        for disc in discussions:
            note = self._first_inline_note(disc)
            if note is None:
                continue
            author = (note.get("author") or {}).get("username", "")
            if bot_identities and author not in bot_identities:
                continue
            pos = note.get("position") or {}
            records.append(
                BotThreadRecord(
                    thread_id=str(disc["id"]),
                    path=pos.get("new_path") or pos.get("old_path") or "",
                    line=int(pos.get("new_line") or pos.get("old_line") or 0),
                    body=note.get("body") or "",
                    is_resolved=bool(note.get("resolved")),
                )
            )
        return records

    async def get_unresolved_bot_threads(
        self, pr_info: PRInfo, bot_login: str | None = None
    ) -> list[UnresolvedThread]:
        threads = await self.get_all_bot_threads(pr_info, bot_login)
        return [
            UnresolvedThread(thread_id=t.thread_id, path=t.path, line=t.line, body=t.body)
            for t in threads
            if not t.is_resolved
        ]

    async def resolve_threads(self, pr_info: PRInfo, thread_ids: list[str]) -> int:
        resolved = 0
        for tid in thread_ids:
            try:
                await self._request(
                    "PUT", f"{self._mr(pr_info)}/discussions/{tid}", data={"resolved": "true"}
                )
                resolved += 1
            except ProviderError as exc:
                logger.warning("Failed to resolve discussion %s: %s", tid, exc)
        return resolved

    async def resolve_outdated_review_threads(self, pr_info: PRInfo) -> int:
        # GitLab's REST API doesn't flag a discussion as "outdated", and
        # resolving every unresolved bot thread would also clear still-valid
        # ones. We rely on the engine's already-posted dedup instead, so this
        # is intentionally a no-op for v1.
        return 0

    async def get_thread_id_for_comment(self, comment_node_id: str, pr_info: PRInfo) -> str | None:
        try:
            discussions = await self._discussions(pr_info)
        except Exception:
            return None
        for disc in discussions:
            for note in disc.get("notes", []):
                if str(note.get("id")) == str(comment_node_id):
                    return str(disc["id"])
        return None

    async def get_human_review_comments(
        self, pr_info: PRInfo, bot_login: str
    ) -> list[HumanReviewComment]:
        try:
            discussions = await self._discussions(pr_info)
        except Exception as e:
            raise ProviderError(f"Failed to fetch human review comments: {e}") from e
        bot_identities = {n for n in (await self._self_username(), bot_login) if n}
        out: list[HumanReviewComment] = []
        for disc in discussions:
            for note in disc.get("notes", []):
                pos = note.get("position")
                author = (note.get("author") or {}).get("username", "")
                if not pos or author in bot_identities or note.get("system"):
                    continue
                out.append(
                    HumanReviewComment(
                        path=pos.get("new_path") or pos.get("old_path") or "",
                        line=int(pos.get("new_line") or pos.get("old_line") or 0),
                        body=note.get("body") or "",
                        author=author,
                    )
                )
        return out

    # ── labels ──────────────────────────────────────────────────────

    async def add_label(self, pr_info: PRInfo, label: str) -> None:
        await self._request("PUT", self._mr(pr_info), data={"add_labels": label})

    async def remove_label(self, pr_info: PRInfo, label: str) -> None:
        await self._request("PUT", self._mr(pr_info), data={"remove_labels": label})

    # ── Merge gate (Phase 4) ──

    def gate_capabilities(self) -> GateCapabilities:
        return GITLAB_CAPABILITIES

    async def submit_verdict(self, pr_info: PRInfo, event: str, body: str) -> bool:
        """Approve a merge request; report anything else as unsupported.

        GitLab has approvals but no REQUEST_CHANGES review event. Rather than
        approximate one with a comment that no merge rule reads, this returns
        False so the gate records `would_approve`/`not_approved` honestly and
        the findings stay where they already are — in the review comments.

        Approvals are a tiered feature and can be turned off per project, so a
        refusal is expected, not exceptional: it degrades the decision instead
        of failing the review.
        """
        if event != "APPROVE":
            logger.info("GitLab has no %s review event; leaving the findings as MR comments", event)
            return False
        try:
            await self._request("POST", f"{self._mr(pr_info)}/approve", ok=(201, 200))
        except Exception as exc:  # noqa: BLE001 - a refused approval is a downgrade
            logger.warning("GitLab refused the approval on %s: %s", pr_info.url, exc)
            return False
        if body:
            with suppress(Exception):
                await self.post_comment(pr_info, body)
        return True

    async def get_review_states(self, pr_info: PRInfo) -> dict[str, str]:
        """Approvals as review states. GitLab has no "changes requested"."""
        try:
            resp = await self._request("GET", f"{self._mr(pr_info)}/approvals")
        except Exception as exc:
            # Not "{}": that reads as "nobody objected".
            logger.debug("Could not read MR approvals for %s: %s", pr_info.url, exc)
            raise ProviderError(f"Failed to read MR approvals: {exc}") from exc
        data = resp.json() or {}
        states: dict[str, str] = {}
        for entry in data.get("approved_by") or []:
            username = ((entry or {}).get("user") or {}).get("username", "")
            if username:
                states[username] = "APPROVED"
        return states

    # Pipeline states that have not reached a verdict yet.
    _PENDING_PIPELINE_STATES = frozenset(
        {
            "created",
            "waiting_for_resource",
            "preparing",
            "pending",
            "running",
            "scheduled",
        }
    )

    async def _foreign_commit_status(self, pr_info: PRInfo) -> bool | None:
        """Is anything other than the gate recorded against the head commit?

        ``True`` — yes, so the pipeline describes real work.
        ``False`` — only the gate's own status is here, so there is no CI.
        ``None`` — nothing at all is recorded, which is what a merged-results
        pipeline looks like from the head commit: it belongs to the merge-ref
        commit instead. The caller then trusts the pipeline.

        Deliberately one page and one question. Answering a boolean by walking
        every retried job of a large pipeline would cost several sequential
        requests on every gate evaluation.
        """
        sha = pr_info.head_sha
        if not sha:
            return None
        url = (
            f"{self._project(pr_info)}/repository/commits/{quote(sha, safe='')}"
            "/statuses?per_page=100"
        )
        resp = await self._request("GET", url, ok=(200, 404))
        if resp.status_code == 404:
            return None
        entries = resp.json() or []
        if not entries:
            return None
        return any(str((entry or {}).get("name") or "") != STATUS_CONTEXT for entry in entries)

    async def get_ci_state(self, pr_info: PRInfo) -> CIState:
        """The merge request's head pipeline, mapped onto the gate vocabulary.

        The pipeline is authoritative because it is the only view that gets the
        awkward cases right: a run blocked on a manual gate reports `manual`
        rather than leaving its downstream jobs looking pending forever, and a
        merged-results pipeline is found even though it belongs to the
        merge-ref commit rather than to the head SHA.

        It has one blind spot, and it is the one that matters here. An external
        commit status posted through the API *joins* the pipeline — including
        the status the gate itself publishes — so on a project with no CI the
        gate would create a green pipeline and read it back as passing on the
        next pass. Hence the second call, which asks only whether anything
        other than the gate is recorded against this commit.

        A merge request with no pipeline reports "none", which is not success —
        the gate cannot vouch for a commit that nothing ever built, and says so
        the same way it does for a pipeline it could not read.
        """
        try:
            resp = await self._request("GET", self._mr(pr_info))
        except Exception as exc:  # noqa: BLE001
            logger.warning("Could not read MR pipeline for %s: %s", pr_info.url, exc)
            return CIState(state="unknown")
        pipeline = (resp.json() or {}).get("head_pipeline") or {}
        if not pipeline:
            return CIState(state="none", total=0)

        try:
            foreign = await self._foreign_commit_status(pr_info)
        except Exception as exc:  # noqa: BLE001 - unknown, never assumed green
            logger.warning("Could not read commit statuses for %s: %s", pr_info.url, exc)
            return CIState(state="unknown")
        if foreign is False:
            # The pipeline exists because the gate posted into it.
            return CIState(state="none", total=0)

        status = str(pipeline.get("status") or "").lower()
        name = f"pipeline #{pipeline.get('id', '?')}"
        if status in {"success", "manual"}:
            # `manual` means every automatic job passed and a human gate remains.
            return CIState(state="success", total=1)
        if status in self._PENDING_PIPELINE_STATES:
            return CIState(state="pending", total=1, pending=[name])
        if status in {"failed", "canceled"}:
            return CIState(state="failure", total=1, failing=[f"{name} ({status})"])
        if status == "skipped":
            # `[ci skip]`, or every job excluded by `rules`. Nothing failed and
            # nothing ran, which is the same standing as a commit with no
            # pipeline: not green, and not a failure to report as one.
            return CIState(state="none", total=0)
        return CIState(state="unknown", total=1)

    async def get_pr_labels(self, pr_info: PRInfo) -> list[str]:
        try:
            resp = await self._request("GET", self._mr(pr_info))
        except Exception as exc:
            # An empty list would read as "no blocking label present".
            logger.warning("Could not read MR labels for %s: %s", pr_info.url, exc)
            raise ProviderError(f"Failed to read MR labels: {exc}") from exc
        return [str(label) for label in (resp.json() or {}).get("labels") or []]

    async def get_author_association(self, pr_info: PRInfo) -> str:
        """Project membership mapped onto GitHub's association vocabulary.

        GitLab access levels are numeric; anything below Developer cannot push,
        so it is reported as a plain contributor and never clears the default
        association allowlist.
        """
        if not pr_info.author:
            return "UNKNOWN"
        url = f"{self._project(pr_info)}/members/all?query={quote(pr_info.author, safe='')}"
        try:
            resp = await self._request("GET", url)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Could not read MR author association for %s: %s", pr_info.url, exc)
            return "UNKNOWN"
        members = resp.json() or []
        level = 0
        for member in members:
            if str((member or {}).get("username", "")).lower() == pr_info.author.lower():
                level = max(level, int((member or {}).get("access_level", 0) or 0))
        if level >= 50:
            return "OWNER"
        if level >= 40:
            return "MEMBER"
        if level >= 30:
            return "COLLABORATOR"
        if level > 0:
            return "CONTRIBUTOR"
        return "NONE"

    async def get_codeowners(self, pr_info: PRInfo) -> tuple[str, str]:
        """First CODEOWNERS found at the head ref, or ``("", "")`` if none.

        Deliberately not routed through `get_file_content`, which logs a failed
        fetch and returns "". "There are no owners" and "we could not check"
        have to reach the gate as different answers — the first is safe to
        approve past and the second is not — so a real failure raises.
        """
        ref = pr_info.head_sha or pr_info.head_branch
        for candidate in CODEOWNERS_LOCATIONS["gitlab"]:
            url = (
                f"{self._project(pr_info)}/repository/files/"
                f"{quote(candidate, safe='')}/raw?ref={quote(ref, safe='')}"
            )
            try:
                resp = await self._request("GET", url, ok=(200, 404))
            except Exception as exc:  # noqa: BLE001 - surfaced, never swallowed
                raise ProviderError(f"Failed to read CODEOWNERS: {exc}") from exc
            if resp.status_code == 200 and resp.text:
                return candidate, resp.text
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
        """Publish the decision as a commit status on the head SHA.

        GitLab has no check runs; a commit status is the closest equivalent and
        is keyed by `name`, so re-publishing after a retry updates in place.
        The state is always `success` and the verdict travels in the
        description. GitLab has no blocking review event, so a red status would
        claim a power this provider does not have; and because an external
        status joins the head pipeline, posting one would corrupt the very
        signal `get_ci_state` reads back. The status is an explanation here, not
        a verdict — which is also why a dry run is not a lie: it says "would
        approve" in the words next to the tick.
        """
        sha = pr_info.head_sha
        if not sha:
            return ""
        # Always `success`. GitLab has no blocking review event, so a red status
        # would claim a power this provider does not have — and an external
        # status *joins* the head pipeline, so posting `failed` would turn the
        # pipeline red and the gate would read its own verdict back as a failing
        # build on the next pass, forever, on that commit. The verdict travels
        # in the description, where it cannot break anything.
        state = "success"
        params: dict[str, Any] = {
            "state": state,
            "name": context or STATUS_CONTEXT,
            "description": title[:255],
        }
        if target_url:
            params["target_url"] = target_url
        url = f"{self._project(pr_info)}/statuses/{quote(sha, safe='')}"
        try:
            resp = await self._request("POST", url, params=params, ok=(200, 201))
        except Exception as exc:  # noqa: BLE001
            raise ProviderError(f"Failed to publish gate status: {exc}") from exc
        return str((resp.json() or {}).get("id", "") or "")


def _next_link(link_header: str) -> str | None:
    """Extract the rel="next" URL from a Link header."""
    if not link_header:
        return None
    for part in link_header.split(","):
        if 'rel="next"' in part:
            return part.split(";")[0].strip().strip("<>")
    return None
