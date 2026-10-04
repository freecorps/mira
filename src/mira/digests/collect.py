"""Asking the provider what landed on a branch.

Two questions, one answer shape. *What landed between these dates?* is a list
of merged pull requests plus any commit pushed to the branch outside one.
*What lies between these two refs?* is the commits the platform's compare
reports, matched back to the pull requests that brought them in.

**Direct commits.** A commit "landed directly" when it sits on the branch's
first-parent chain and no pull request in the window accounts for it. Walking
the first parent is what separates the commits a merge brought in (they hang
off its second parent) from the ones pushed straight to the branch. A
rebase-merged pull request puts every one of its commits on the chain; the
platform names only the last as the merge commit, so the rest are recognised
by being committed within a minute of a merge in the window, which is what a
rebase-merge does to committer dates.

Everything is bounded by the caller's caps, and every cap that cut something
leaves a note saying so.
"""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass, field
from typing import Any

from mira.digests.models import Change
from mira.models import CommitInfo, MergedPullRequest, PRInfo

logger = logging.getLogger(__name__)

# Direct commits whose files are looked up one request each. Past this, a
# commit is listed under "Other" rather than costing another round trip.
MAX_DIRECT_COMMIT_LOOKUPS = 40

# How close a commit's date must be to a merge for it to count as part of a
# rebase-merge.
_REBASE_WINDOW_SECONDS = 60.0

# `Title (#123)`, `Merge pull request #123`, `See merge request group/proj!123`.
_PR_REFERENCE = re.compile(r"(?:\(#(\d+)\)|pull request #(\d+)|merge request \S*!(\d+))", re.I)


@dataclass
class Collected:
    changes: list[Change] = field(default_factory=list)
    branch: str = ""
    notes: list[str] = field(default_factory=list)
    pull_requests: int = 0
    direct_commits: int = 0


def change_from_pr(pr: MergedPullRequest, *, repo: str = "") -> Change:
    return Change(
        kind="pr",
        number=pr.number,
        sha=pr.merge_commit_sha,
        title=pr.title,
        body=pr.body,
        url=pr.url,
        author=pr.author,
        landed_at=pr.merged_at,
        labels=list(pr.labels),
        files=list(pr.files),
        repo=repo,
    )


def change_from_commit(commit: CommitInfo, *, repo: str = "") -> Change:
    message = (commit.message or "").strip()
    title, _, body = message.partition("\n")
    return Change(
        kind="commit",
        sha=commit.sha,
        title=title.strip(),
        body=body.strip(),
        url=commit.url,
        author=commit.author,
        landed_at=commit.date,
        files=list(commit.files),
        repo=repo,
    )


def _chain_head(commits: list[CommitInfo]) -> CommitInfo | None:
    """The tip: a commit nobody in the set names as a parent, newest first."""
    parents = {p for c in commits for p in c.parents}
    tips = [c for c in commits if c.sha not in parents]
    if not tips:
        return commits[0] if commits else None
    return max(tips, key=lambda c: c.date)


def find_direct_commits(
    commits: list[CommitInfo], prs: list[MergedPullRequest]
) -> list[CommitInfo]:
    """Commits on the first-parent chain that no pull request accounts for.

    Order of ``commits`` does not matter (GitHub lists newest first, a compare
    oldest first). A provider that reported no parents at all leaves no chain
    to walk, so every commit is a candidate and the other filters decide.
    """
    if not commits:
        return []
    by_sha = {c.sha: c for c in commits}
    if any(c.parents for c in commits):
        chain: list[CommitInfo] = []
        head = _chain_head(commits)
        seen: set[str] = set()
        cursor = head.sha if head else ""
        while cursor in by_sha and cursor not in seen:
            seen.add(cursor)
            commit = by_sha[cursor]
            chain.append(commit)
            cursor = commit.parents[0] if commit.parents else ""
    else:
        chain = list(commits)

    merge_shas = {pr.merge_commit_sha for pr in prs if pr.merge_commit_sha}
    numbers = {pr.number for pr in prs}
    merge_times = [pr.merged_at for pr in prs if pr.merged_at]
    direct: list[CommitInfo] = []
    for commit in chain:
        if commit.sha in merge_shas or len(commit.parents) > 1:
            continue
        referenced = {
            int(n) for match in _PR_REFERENCE.findall(commit.message or "") for n in match if n
        }
        if referenced & numbers:
            continue
        if commit.date and any(abs(commit.date - t) <= _REBASE_WINDOW_SECONDS for t in merge_times):
            continue
        direct.append(commit)
    return direct


async def _fill_commit_files(
    provider: Any, ref: PRInfo, commits: list[CommitInfo], *, max_files: int
) -> None:
    sem = asyncio.Semaphore(6)

    async def _one(commit: CommitInfo) -> None:
        if commit.files:
            commit.files = commit.files[:max_files]
            return
        async with sem:
            try:
                commit.files = await provider.get_commit_files(ref, commit.sha, limit=max_files)
            except Exception as exc:  # noqa: BLE001 - files only place a change in an area
                logger.debug("Could not read files of %s: %s", commit.sha[:8], exc)

    await asyncio.gather(*[_one(c) for c in commits[:MAX_DIRECT_COMMIT_LOOKUPS]])


async def collect_window(
    provider: Any,
    ref: PRInfo,
    *,
    since: float,
    until: float,
    branch: str = "",
    max_pull_requests: int = 100,
    max_commits: int = 200,
    max_files: int = 100,
    include_direct_commits: bool = True,
    label: str = "",
) -> Collected:
    """Everything that landed on ``branch`` (default: the default branch) in
    ``[since, until)``.

    The pull request listing must succeed — a digest built on a failed listing
    would report a quiet week. The direct-commit half is best effort and says
    so in a note when it could not be read.
    """
    out = Collected()
    branch = branch or await provider.get_default_branch(ref)
    out.branch = branch
    if not branch:
        out.notes.append("The default branch could not be read; pull requests into any branch.")
    prs = await provider.list_landed_pull_requests(
        ref,
        since=since,
        until=until,
        base=branch,
        limit=max_pull_requests,
        max_files=max_files,
    )
    if len(prs) >= max_pull_requests:
        out.notes.append(
            f"Only the {max_pull_requests} most recently merged pull requests were read."
        )
    out.changes = [change_from_pr(pr, repo=label) for pr in prs]
    out.pull_requests = len(prs)

    if include_direct_commits and branch:
        try:
            commits = await provider.list_commits(
                ref, ref=branch, since=since, until=until, limit=max_commits
            )
        except Exception as exc:  # noqa: BLE001 - the pull requests already stand
            logger.info("Could not list commits on %s for the digest: %s", branch, exc)
            out.notes.append("Commits pushed directly to the branch could not be read.")
            commits = []
        if len(commits) >= max_commits:
            out.notes.append(
                f"Only the {max_commits} most recent commits were checked for direct pushes."
            )
        direct = find_direct_commits(commits, prs)
        if direct and max_files > 0:
            await _fill_commit_files(provider, ref, direct, max_files=max_files)
            out.changes.extend(change_from_commit(c, repo=label) for c in direct)
            out.direct_commits = len(direct)
    return out


async def collect_between(
    provider: Any,
    ref: PRInfo,
    *,
    base: str,
    head: str,
    max_pull_requests: int = 100,
    max_commits: int = 250,
    include_direct_commits: bool = True,
) -> Collected:
    """The pull requests and direct commits in ``base..head``.

    The compare names the commits; the pull requests are the ones merged in the
    span of those commits' dates whose merge commit is among them. A provider
    that reports no merge commit for any of them falls back to the date span
    alone, which is right for a linear release branch and says so in a note.
    """
    out = Collected(branch=head)
    commits = await provider.compare_commits(ref, base, head, limit=max_commits)
    if not commits:
        return out
    if len(commits) >= max_commits:
        out.notes.append(f"Only {max_commits} commits of the range were read.")
    dated = [c.date for c in commits if c.date]
    if not dated:
        out.notes.append("The commits in the range carry no dates; pull requests were not matched.")
        prs: list[MergedPullRequest] = []
    else:
        # A pull request's merge happens at or after its merge commit's date;
        # the slack absorbs clock skew between the two.
        prs = await provider.list_landed_pull_requests(
            ref,
            since=min(dated) - 60,
            until=max(dated) + 3600,
            limit=max_pull_requests,
            max_files=0,
        )
        if len(prs) >= max_pull_requests:
            out.notes.append(f"Only {max_pull_requests} pull requests were read.")
        shas = {c.sha for c in commits}
        if any(pr.merge_commit_sha for pr in prs):
            prs = [pr for pr in prs if pr.merge_commit_sha in shas]
        elif prs:
            out.notes.append("Pull requests were matched to the range by date alone.")
    out.changes = [change_from_pr(pr) for pr in prs]
    out.pull_requests = len(prs)
    if include_direct_commits:
        direct = find_direct_commits(commits, prs)
        out.changes.extend(change_from_commit(c) for c in direct)
        out.direct_commits = len(direct)
    return out
