"""Escaped bugs: reverts and hotfixes traced back to the pull request Mira reviewed.

A merged pull request (or a commit pushed straight to the default branch) is a
**fix event** when it is

* a **revert** — a ``Revert …`` title, a ``This reverts commit <sha>`` line,
  or GitHub's ``revert-<N>-…`` branch; or
* a **hotfix** — a hotfix/bug label, a ``hotfix/`` style branch, or a
  ``fixes #N`` reference to an issue labelled as a bug.

Each fix event is linked to the earlier pull request(s) that introduced the
code it changed:

* a revert names its target directly (reverted sha → pull request);
* a hotfix is linked by **blaming** the lines it changed at the commit just
  before it, then mapping each blamed commit to its pull request; where the
  provider cannot blame (Forgejo), by comparing the fix's lines against the
  diffs of recent pull requests Mira reviewed that touched the same file.

Only pull requests Mira actually reviewed are recorded, and each record says
whether Mira had flagged anything on those lines. That yes/no, counted over
time, is a real-world recall figure: of the defects that shipped through Mira,
how many had it pointed at.

Missed ones are, optionally, fed to the learning loop as *pending* candidates
— governed like every other candidate, so nothing reaches a review prompt
until an admin approves it.
"""

from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any

from mira.models import CommitInfo, MergedPullRequest, PRInfo
from mira.quality.config_models import EscapedBugsConfig
from mira.quality.lines import FileLines, changed_lines, lines_near, ranges_overlap, to_ranges
from mira.quality.models import (
    KIND_HOTFIX,
    KIND_REVERT,
    LINK_BLAME,
    LINK_DIFF_OVERLAP,
    LINK_REVERT_BRANCH,
    LINK_REVERT_SHA,
    EscapedBug,
    escaped_bug_id,
)

logger = logging.getLogger(__name__)

REVERT_TITLE = re.compile(r"^\s*revert\b", re.IGNORECASE)
REVERTS_COMMIT = re.compile(r"this reverts commit\s+([0-9a-f]{7,40})", re.IGNORECASE)
REVERT_BRANCH = re.compile(r"^revert-(\d+)-")
FIXES_ISSUE = re.compile(
    r"\b(?:fix(?:es|ed)?|close[sd]?|resolve[sd]?)\s*:?\s+#(\d+)\b", re.IGNORECASE
)
HOTFIX_MESSAGE = re.compile(r"^\s*(?:hotfix|\[hotfix\])\b", re.IGNORECASE)

MISSED_FEEDBACK_KIND = "missed"
SYNTHESIZER_VERSION = "escaped-bug-v1"


@dataclass
class FixEvent:
    """A revert or hotfix, before it has been linked to anything."""

    kind: str
    fix_ref: str
    title: str
    url: str = ""
    fix_pr_number: int = 0
    fix_sha: str = ""
    # The revision just before the fix: what blame runs against.
    base_ref: str = ""
    happened_at: float = 0.0
    reverted_shas: list[str] = field(default_factory=list)
    reverted_prs: list[int] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)


@dataclass
class ScanResult:
    examined: int = 0
    fix_events: int = 0
    recorded: list[EscapedBug] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


# ---------------------------------------------------------------- classify


def _label_hit(labels: list[str], wanted: list[str]) -> str:
    lowered = {w.lower() for w in wanted}
    for label in labels:
        if label.lower() in lowered:
            return label
    return ""


async def classify_pull_request(
    provider: Any, scope: PRInfo, merged: MergedPullRequest, config: EscapedBugsConfig
) -> FixEvent | None:
    """A fix event for this merged pull request, or None if it is neither kind."""
    event = FixEvent(
        kind="",
        fix_ref=f"pr:{merged.number}",
        title=merged.title,
        url=merged.url,
        fix_pr_number=merged.number,
        fix_sha=merged.merge_commit_sha or merged.head_sha,
        base_ref=merged.base_sha,
        happened_at=merged.merged_at or time.time(),
    )
    text = f"{merged.title}\n{merged.body}"

    if config.detect_reverts:
        shas = REVERTS_COMMIT.findall(text)
        branch = REVERT_BRANCH.match(merged.head_branch or "")
        if shas or branch or REVERT_TITLE.search(merged.title or ""):
            event.kind = KIND_REVERT
            event.reverted_shas = list(dict.fromkeys(s.lower() for s in shas))
            if branch:
                event.reverted_prs.append(int(branch.group(1)))
            event.reasons.append("revert")
            return event

    if not config.detect_hotfixes:
        return None
    label = _label_hit(merged.labels, config.hotfix_labels)
    if label:
        event.reasons.append(f"label:{label}")
    branch = merged.head_branch or ""
    for prefix in config.hotfix_branch_prefixes:
        if branch.lower().startswith(prefix.lower()):
            event.reasons.append(f"branch:{prefix}")
            break
    if not event.reasons:
        for number in dict.fromkeys(int(n) for n in FIXES_ISSUE.findall(text)):
            try:
                issue = await provider.get_issue(scope, number)
            except Exception as exc:  # noqa: BLE001 - an unreadable issue proves nothing
                logger.debug("Issue #%s unreadable: %s", number, exc)
                continue
            if issue is not None and _label_hit(issue.labels, config.bug_issue_labels):
                event.reasons.append(f"fixes-bug:#{number}")
                break
    if not event.reasons:
        return None
    event.kind = KIND_HOTFIX
    return event


def classify_commit(commit: CommitInfo, config: EscapedBugsConfig) -> FixEvent | None:
    """A fix event for a commit pushed directly to the default branch."""
    message = commit.message or ""
    first = message.strip().splitlines()[0] if message.strip() else ""
    event = FixEvent(
        kind="",
        fix_ref=f"commit:{commit.sha}",
        title=first[:200],
        url=commit.url,
        fix_sha=commit.sha,
        base_ref=commit.parents[0] if commit.parents else "",
        happened_at=commit.date or time.time(),
    )
    if config.detect_reverts and (REVERTS_COMMIT.search(message) or REVERT_TITLE.search(first)):
        event.kind = KIND_REVERT
        event.reverted_shas = list(
            dict.fromkeys(s.lower() for s in REVERTS_COMMIT.findall(message))
        )
        event.reasons.append("revert")
        return event
    if config.detect_hotfixes and HOTFIX_MESSAGE.search(first):
        event.kind = KIND_HOTFIX
        event.reasons.append("message:hotfix")
        return event
    return None


# -------------------------------------------------------------------- link


@dataclass
class _Link:
    original_pr: int
    path: str
    lines: set[int]
    method: str


async def _fix_diff(provider: Any, scope: PRInfo, event: FixEvent) -> str:
    if event.fix_pr_number:
        pr = PRInfo(
            title=event.title,
            description="",
            base_branch="",
            head_branch="",
            url=event.url,
            number=event.fix_pr_number,
            owner=scope.owner,
            repo=scope.repo,
            platform=scope.platform,
        )
        return await provider.get_pr_diff(pr)
    return await provider.get_commit_diff(scope, event.fix_sha)


async def _revert_links(
    provider: Any, scope: PRInfo, event: FixEvent, touched: dict[str, FileLines]
) -> list[_Link]:
    originals: dict[int, str] = {}
    for sha in event.reverted_shas:
        try:
            for number in await provider.get_prs_for_commit(scope, sha):
                originals.setdefault(number, LINK_REVERT_SHA)
        except Exception as exc:  # noqa: BLE001
            logger.debug("Could not map reverted commit %s: %s", sha, exc)
    for number in event.reverted_prs:
        originals.setdefault(number, LINK_REVERT_BRANCH)
    links = []
    for number, method in originals.items():
        for path, entry in touched.items():
            # The revert's old side is the original's new side: the lines
            # the original added are exactly what a revert removes.
            lines = entry.removed or entry.added
            if lines:
                links.append(_Link(number, path, set(lines), method))
    return links


async def _blame_links(
    provider: Any,
    scope: PRInfo,
    event: FixEvent,
    entry: FileLines,
    sha_cache: dict[str, list[int]],
) -> list[_Link]:
    ranges = await provider.get_blame(scope, entry.old_path or entry.path, event.base_ref)
    per_sha: dict[str, set[int]] = {}
    for blame in ranges:
        hit = {line for line in entry.removed if blame.start <= line <= blame.end}
        if hit and blame.sha:
            per_sha.setdefault(blame.sha, set()).update(hit)
    links: list[_Link] = []
    for sha, lines in per_sha.items():
        if sha not in sha_cache:
            try:
                sha_cache[sha] = await provider.get_prs_for_commit(scope, sha)
            except Exception as exc:  # noqa: BLE001
                logger.debug("Could not map blamed commit %s: %s", sha, exc)
                sha_cache[sha] = []
        for number in sha_cache[sha]:
            links.append(_Link(number, entry.path, lines, LINK_BLAME))
    return links


async def _overlap_links(
    provider: Any,
    scope: PRInfo,
    event: FixEvent,
    entry: FileLines,
    quality_store: Any,
    config: EscapedBugsConfig,
    diff_cache: dict[int, dict[str, FileLines]],
) -> list[_Link]:
    since = event.happened_at - config.window_days * 86400
    candidates = quality_store.reviewed_prs_touching(
        entry.path, since=since, limit=config.max_original_prs * 2
    )
    links: list[_Link] = []
    for number in candidates:
        if number == event.fix_pr_number:
            continue
        if number not in diff_cache:
            pr = PRInfo(
                title="",
                description="",
                base_branch="",
                head_branch="",
                url="",
                number=number,
                owner=scope.owner,
                repo=scope.repo,
                platform=scope.platform,
            )
            try:
                diff_cache[number] = changed_lines(await provider.get_pr_diff(pr))
            except Exception as exc:  # noqa: BLE001
                logger.debug("Diff of #%s unavailable: %s", number, exc)
                diff_cache[number] = {}
        theirs = diff_cache[number].get(entry.old_path or entry.path) or diff_cache[number].get(
            entry.path
        )
        if theirs is None:
            continue
        hit = {
            line
            for line in entry.removed
            if lines_near(line, line, theirs.added, config.line_tolerance)
        }
        if hit:
            links.append(_Link(number, entry.path, hit, LINK_DIFF_OVERLAP))
    return links


async def link_fix_event(
    provider: Any,
    scope: PRInfo,
    event: FixEvent,
    quality_store: Any,
    config: EscapedBugsConfig,
) -> list[_Link]:
    """The (original PR, path, lines) each fix event points back to."""
    try:
        touched = changed_lines(await _fix_diff(provider, scope, event))
    except Exception as exc:  # noqa: BLE001
        logger.info("Diff of %s unavailable, cannot link it: %s", event.fix_ref, exc)
        return []
    # Biggest changes first, so the cap keeps the files that matter.
    files = sorted(touched.values(), key=lambda f: len(f.removed), reverse=True)
    files = files[: config.max_files]
    if event.kind == KIND_REVERT:
        return await _revert_links(provider, scope, event, {f.path: f for f in files})

    links: list[_Link] = []
    sha_cache: dict[str, list[int]] = {}
    diff_cache: dict[int, dict[str, FileLines]] = {}
    can_blame = bool(event.base_ref)
    for entry in files:
        if not entry.removed:
            continue
        if can_blame:
            try:
                links.extend(await _blame_links(provider, scope, event, entry, sha_cache))
                continue
            except NotImplementedError:
                can_blame = False
            except Exception as exc:  # noqa: BLE001 - one unreadable file
                logger.debug("Blame of %s failed: %s", entry.path, exc)
                continue
        links.extend(
            await _overlap_links(provider, scope, event, entry, quality_store, config, diff_cache)
        )
    return links


# ------------------------------------------------------------------ record


def _flagged(
    findings: list[dict[str, Any]], path: str, lines: set[int], tolerance: int
) -> tuple[bool, bool, list[str]]:
    same_file = [f for f in findings if f["path"] == path]
    hits = [
        f
        for f in same_file
        if any(
            ranges_overlap(f["start_line"], f["end_line"], start, end, tolerance)
            for start, end in to_ranges(lines)
        )
    ]
    return bool(hits), bool(same_file), [f["id"] for f in hits]


async def record_links(
    provider: Any,
    scope: PRInfo,
    event: FixEvent,
    links: list[_Link],
    quality_store: Any,
    config: EscapedBugsConfig,
    *,
    learning_config: Any = None,
) -> list[EscapedBug]:
    """Persist one escaped bug per (original PR, path); return the new ones."""
    by_key: dict[tuple[int, str], _Link] = {}
    for link in links:
        if link.original_pr == event.fix_pr_number or link.original_pr <= 0:
            continue
        key = (link.original_pr, link.path)
        if key in by_key:
            by_key[key].lines |= link.lines
        else:
            by_key[key] = _Link(link.original_pr, link.path, set(link.lines), link.method)

    originals = list(dict.fromkeys(pr for pr, _ in by_key))[: config.max_original_prs]
    earliest = event.happened_at - config.window_days * 86400
    recorded: list[EscapedBug] = []
    for number in originals:
        if not quality_store.was_reviewed(number):
            logger.debug("%s → #%s: not reviewed by Mira, skipped", event.fix_ref, number)
            continue
        try:
            original = await provider.get_landed_pull_request(scope, number)
        except Exception as exc:  # noqa: BLE001
            logger.debug("Original #%s unreadable: %s", number, exc)
            original = None
        merged_at = original.merged_at if original else 0.0
        if merged_at and (merged_at < earliest or merged_at > event.happened_at):
            continue
        findings = quality_store.findings_for_pr(number)
        for (pr, path), link in by_key.items():
            if pr != number:
                continue
            flagged, flagged_file, ids = _flagged(findings, path, link.lines, config.line_tolerance)
            lines = sorted(link.lines)
            bug = EscapedBug(
                id=escaped_bug_id(scope.platform, scope.owner, scope.repo, event.fix_ref, pr, path),
                platform=scope.platform,
                owner=scope.owner,
                repo=scope.repo,
                kind=event.kind,
                fix_ref=event.fix_ref,
                original_pr_number=pr,
                path=path,
                line_start=lines[0] if lines else 0,
                line_end=lines[-1] if lines else 0,
                fix_pr_number=event.fix_pr_number,
                fix_sha=event.fix_sha,
                fix_title=event.title,
                fix_url=event.url,
                original_pr_url=original.url if original else "",
                original_merged_at=merged_at,
                flagged=flagged,
                flagged_file=flagged_file,
                finding_ids=ids,
                link_method=link.method,
                detail={"reasons": event.reasons, "line_ranges": to_ranges(link.lines)},
            )
            if not quality_store.record_escaped_bug(bug):
                continue
            if not flagged and config.feed_learning:
                try:
                    bug.learning_candidate_id = feed_learning(
                        quality_store, bug, learning_config=learning_config
                    )
                except Exception:  # noqa: BLE001 - the escape is already durable
                    logger.exception("Could not feed escaped bug %s to learning", bug.id)
            recorded.append(bug)
    return recorded


def feed_learning(quality_store: Any, bug: EscapedBug, *, learning_config: Any = None) -> int:
    """Record a missed-finding example and a *pending* learning candidate.

    Returns the candidate id (0 when learning is switched off). The candidate
    is a positive example — "look for this here" — where the existing
    synthesis only ever produces negative ones from disagreement. It starts
    pending, scoped to the exact path, and changes nothing until approved.
    """
    from mira.feedback.deduplication import semantic_fingerprint
    from mira.feedback.models import FeedbackEventV2, LearningCandidate
    from mira.feedback.synthesis import language_for_path

    if learning_config is None:
        from mira.config import load_config

        learning_config = load_config().learning
    if not (learning_config.feedback_v2 and learning_config.learning_synthesis):
        return 0

    store = quality_store.index_store
    verb = "reverted" if bug.kind == KIND_REVERT else "hotfixed"
    fix = f"#{bug.fix_pr_number}" if bug.fix_pr_number else bug.fix_sha[:10]
    event, _created = store.record_feedback_v2(
        FeedbackEventV2(
            id=0,
            finding_id=None,
            kind=MISSED_FEEDBACK_KIND,
            actor="mira-escaped-bugs",
            actor_role="system",
            raw_text=bug.fix_title,
            rationale=(
                f"Code merged in #{bug.original_pr_number} was {verb} by {fix}; "
                "Mira reviewed it and did not flag these lines."
            ),
            platform=bug.platform,
            source_event_id=f"escaped:{bug.id}",
            head_sha=bug.fix_sha,
            thread_state="",
            provenance_complete=False,
            audit_json=json.dumps(bug.to_dict(), sort_keys=True, default=str),
        )
    )
    rule_text = (
        f"Review changes to {bug.path} with extra care for defects: code merged here in "
        f"#{bug.original_pr_number} was later {verb} ({fix}: {bug.fix_title[:120]}). "
        "Flag similar mistakes when the diff touches the same logic."
    )
    example = {
        "feedback_id": event.id,
        "escaped_bug_id": bug.id,
        "path": bug.path,
        "lines": [bug.line_start, bug.line_end],
        "original_pr": bug.original_pr_number,
        "fix": fix,
        "fix_title": bug.fix_title,
        "kind": bug.kind,
    }
    candidate = LearningCandidate(
        id=0,
        semantic_fingerprint=semantic_fingerprint(rule_text, "bug"),
        rule_text=rule_text,
        rationale=f"Escaped bug: Mira did not flag lines later {verb}.",
        scope_type="path",
        scope_value=bug.path,
        category="bug",
        language=language_for_path(bug.path),
        confidence=0.5,
        status="pending",
        synthesizer_version=SYNTHESIZER_VERSION,
        evidence_ids_json=json.dumps([event.id]),
        positive_examples_json=json.dumps([example], sort_keys=True),
        negative_examples_json="[]",
        source_finding_id=None,
        source_feedback_id=event.id or None,
    )
    stored, _ = store.upsert_learning_candidate(candidate)
    candidate_id = int(getattr(stored, "id", 0) or 0)
    if candidate_id:
        quality_store.set_escaped_bug_candidate(bug.id, candidate_id)
    return candidate_id


# ------------------------------------------------------------- entry points


def _scope(owner: str, repo: str, platform: str) -> PRInfo:
    from mira.quality.backtest import repo_scope

    return repo_scope(owner, repo, platform)


async def process_fix_event(
    provider: Any,
    scope: PRInfo,
    event: FixEvent,
    quality_store: Any,
    config: EscapedBugsConfig,
    *,
    learning_config: Any = None,
) -> list[EscapedBug]:
    links = await link_fix_event(provider, scope, event, quality_store, config)
    if not links:
        return []
    return await record_links(
        provider, scope, event, links, quality_store, config, learning_config=learning_config
    )


async def process_merged_pull_request(
    provider: Any,
    pr_info: PRInfo,
    *,
    platform: str = "github",
    config: Any = None,
) -> list[EscapedBug]:
    """Webhook entry point: a pull request just merged. Never raises."""
    from mira.quality.readonly import ReadOnlyProvider
    from mira.quality.store import open_quality_store

    try:
        if config is None:
            from mira.config import load_config

            config = load_config()
        settings: EscapedBugsConfig = config.escaped_bugs
        if not settings.tracks(pr_info.owner, pr_info.repo):
            return []
        reader = ReadOnlyProvider(provider)
        scope = _scope(pr_info.owner, pr_info.repo, platform)
        merged = await reader.get_landed_pull_request(scope, pr_info.number)
        if merged is None:
            merged = MergedPullRequest(
                number=pr_info.number,
                title=pr_info.title,
                url=pr_info.url,
                body=pr_info.description,
                author=pr_info.author,
                base_branch=pr_info.base_branch,
                head_branch=pr_info.head_branch,
                base_sha=pr_info.base_sha,
                head_sha=pr_info.head_sha,
                merged_at=time.time(),
            )
        event = await classify_pull_request(reader, scope, merged, settings)
        if event is None:
            return []
        with open_quality_store(pr_info.owner, pr_info.repo, platform) as quality_store:
            recorded = await process_fix_event(
                reader, scope, event, quality_store, settings, learning_config=config.learning
            )
        if recorded:
            logger.info(
                "Escaped bug(s) from %s on %s/%s: %d recorded",
                event.fix_ref,
                pr_info.owner,
                pr_info.repo,
                len(recorded),
            )
        return recorded
    except Exception:  # noqa: BLE001 - a webhook side task must never raise
        logger.exception("Escaped-bug detection failed for %s", pr_info.url)
        return []


async def process_pushed_commits(
    provider: Any,
    owner: str,
    repo: str,
    commits: list[dict[str, Any]],
    *,
    platform: str = "github",
    config: Any = None,
) -> list[EscapedBug]:
    """Webhook entry point: commits pushed to the default branch. Never raises.

    Only commits whose message marks them as a revert or hotfix cost a request;
    the rest are classified from the payload alone.
    """
    from mira.quality.readonly import ReadOnlyProvider
    from mira.quality.store import open_quality_store

    try:
        if config is None:
            from mira.config import load_config

            config = load_config()
        settings: EscapedBugsConfig = config.escaped_bugs
        if not settings.tracks(owner, repo):
            return []
        reader = ReadOnlyProvider(provider)
        scope = _scope(owner, repo, platform)
        events: list[FixEvent] = []
        for raw in commits[:50]:
            sha = str(raw.get("id") or raw.get("sha") or "")
            message = str(raw.get("message") or "")
            if not sha or classify_commit(CommitInfo(sha=sha, message=message), settings) is None:
                continue
            # The payload has no parents; ask once, for the commits that matter.
            commit = await reader.get_commit(scope, sha)
            if commit is None:
                continue
            # A merge commit for a pull request is handled by the merge event.
            if len(commit.parents) > 1 or await reader.get_prs_for_commit(scope, sha):
                continue
            event = classify_commit(commit, settings)
            if event is not None:
                events.append(event)
        if not events:
            return []
        recorded: list[EscapedBug] = []
        with open_quality_store(owner, repo, platform) as quality_store:
            for event in events:
                recorded.extend(
                    await process_fix_event(
                        reader,
                        scope,
                        event,
                        quality_store,
                        settings,
                        learning_config=config.learning,
                    )
                )
        return recorded
    except Exception:  # noqa: BLE001
        logger.exception("Escaped-bug detection failed for a push to %s/%s", owner, repo)
        return []


async def scan_history(
    provider: Any,
    owner: str,
    repo: str,
    *,
    platform: str = "github",
    since: float = 0.0,
    limit: int = 100,
    config: Any = None,
    quality_store: Any = None,
) -> ScanResult:
    """CLI entry point: classify and link every merged pull request in a window.

    Unlike the webhook path this does not require ``escaped_bugs.enabled`` —
    running the command is the opt-in — but it uses the same limits.
    """
    from mira.quality.readonly import ReadOnlyProvider
    from mira.quality.store import open_quality_store

    if config is None:
        from mira.config import load_config

        config = load_config()
    settings: EscapedBugsConfig = config.escaped_bugs
    reader = ReadOnlyProvider(provider)
    scope = _scope(owner, repo, platform)
    result = ScanResult()
    merged_prs = await reader.list_landed_pull_requests(
        scope, since=since, limit=limit, max_files=0
    )
    # Oldest first, so a hotfix is linked after the pull request it fixes is known.
    merged_prs.sort(key=lambda pr: pr.merged_at)

    async def _run(store: Any) -> None:
        for merged in merged_prs:
            result.examined += 1
            event = await classify_pull_request(reader, scope, merged, settings)
            if event is None:
                continue
            result.fix_events += 1
            result.recorded.extend(
                await process_fix_event(
                    reader, scope, event, store, settings, learning_config=config.learning
                )
            )

    if quality_store is not None:
        await _run(quality_store)
    else:
        with open_quality_store(owner, repo, platform) as store:
            await _run(store)
    return result
