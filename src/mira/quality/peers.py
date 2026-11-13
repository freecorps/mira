"""Peer recall: what other review bots found on pull requests Mira also reviewed.

Escaped bugs measure recall on defects bad enough to be reverted, which is a
handful a month. The review threads other bots open are a much larger labelled
set that arrives within a day: on this organisation's history Cursor Bugbot
opened 490 threads on pull requests Mira reviewed, Mira had a finding within
15 lines of 87 of them, and the author fixed 297 of the 403 it missed.

For every pull request Mira reviewed that has since closed, this module reads
the threads each configured peer bot opened and records, per thread:

* **whether Mira saw it** — a finding Mira persisted on that pull request, in
  the same file, within ``peer_recall.match_line_tolerance`` lines (15);
* **whether it was addressed** — see :func:`assess_addressed`. The rule is
  deliberately strict, because these threads become positive learning
  evidence and a dismissed one would teach Mira to repeat the noise.

Reading is GitHub-only and read-only. The rows are idempotent: a pull request
is read once after it closes, and reading it again refreshes the same rows.
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any

from mira.models import PeerReviewSnapshot, PeerReviewThread, PRInfo
from mira.quality.config_models import PeerRecallConfig
from mira.quality.lines import ranges_overlap
from mira.quality.models import (
    ADDRESSED_OUTDATED,
    ADDRESSED_REPLY,
    ADDRESSED_RESOLVED,
    PeerThread,
    peer_thread_id,
)

logger = logging.getLogger(__name__)

# Stored per thread so the dashboard and the synthesiser can quote it without
# asking GitHub again. CodeRabbit's bodies run to several kilobytes of
# collapsible sections; the finding itself is at the top.
BODY_LIMIT = 4000

# A reply that says the finding was wrong or will not be acted on. Any match
# anywhere in the thread vetoes "addressed", whatever else the thread says:
# the cost of missing a fix is one fewer example, the cost of counting a
# dismissal is a positive rule that repeats a peer bot's false positive.
_DISMISSED_RE = re.compile(
    r"\b(?:won'?t\s+fix|will\s+not\s+fix|not\s+(?:an?\s+)?(?:issue|bug|problem|concern)"
    r"|false[\s-]+positive|by\s+design|intended|intentional|on\s+purpose|ignore\s+this"
    r"|out\s+of\s+scope|separate\s+pr|follow[\s-]?up"
    r"|falso[\s-]+positivo|n[ãa]o\s+(?:[ée]|era)\s+(?:um\s+)?(?:problema|bug)"
    r"|proposital|de\s+prop[óo]sito|fora\s+do\s+escopo|outro\s+pr)\b",
    re.IGNORECASE,
)

# Reasons a thread was not counted, for the drill-down.
NOT_MERGED = "not_merged"
DISMISSED = "dismissed"
UNVERIFIED = "unverified"


# ----------------------------------------------------------------- reading


_MARKUP_RE = re.compile(r"<!--.*?-->|<[^>]+>|!\[[^\]]*\]\([^)]*\)", re.DOTALL)
_LABEL_LINE_RE = re.compile(
    r"^[_*\s]*[^\w\s]*\s*(?:potential issue|refactor suggestion|nitpick)", re.I
)
_LEADING_RE = re.compile(r"^(?:#+\s*|[-*>]\s+|\*\*|__)+")
_TRAILING_RE = re.compile(r"(?:\*\*|__)+\s*$")
_BUG_PREFIX_RE = re.compile(r"^(?:bug|issue|suggestion|problem)\s*[:\-–]\s*", re.I)


def peer_title(body: str) -> str:
    """The first line of a peer bot's comment that states the finding.

    Bots open with badges, severity labels and HTML comments (CodeRabbit's
    ``_⚠️ Potential issue_ | _🟠 Major_``); those are skipped. A bold line in
    the first few is preferred, since that is where every bot here puts its
    title.
    """
    text = _MARKUP_RE.sub("", body or "")
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    candidates: list[str] = []
    for line in lines[:8]:
        if _LABEL_LINE_RE.match(line) or line.startswith("|"):
            continue
        cleaned = _TRAILING_RE.sub("", _LEADING_RE.sub("", line)).strip(" _*`")
        cleaned = _BUG_PREFIX_RE.sub("", cleaned)
        if len(re.findall(r"[A-Za-zÀ-ÿ]{2,}", cleaned)) < 3:
            continue
        if line.startswith(("**", "__", "#")):
            return cleaned[:200]
        candidates.append(cleaned)
    return (candidates[0] if candidates else (lines[0] if lines else ""))[:200]


# Ordered: the first match wins, so the specific kinds come before "logic".
# ``lifecycle`` is separate because that is what Mira measurably misses.
_CATEGORIES: tuple[tuple[str, re.Pattern[str]], ...] = tuple(
    (name, re.compile(pattern, re.IGNORECASE))
    for name, pattern in (
        (
            "security",
            r"\b(?:security|injection|xss|csrf|ssrf|secrets?|credentials?|unauthori[sz]ed"
            r"|authenticat\w*|authori[sz]ation|permissions?|sanitiz\w*|path traversal)\b",
        ),
        (
            "concurrency",
            r"\b(?:race(?: condition)?|concurren\w*|deadlock|thread[- ]safe\w*|mutex"
            r"|atomic\w*|in parallel)\b",
        ),
        (
            "lifecycle",
            r"\b(?:state machine|lifecycle|stale|transition\w*|cleanup|clean up|teardown"
            r"|idempoten\w*|re-?entr\w*|invalidat\w*|out of sync|never (?:reset|clear)\w*"
            r"|left (?:open|running|pending)|status (?:is|stays|remains))\b",
        ),
        (
            "error-handling",
            r"\b(?:exceptions?|error handling|unhandled|swallow\w*|silently|try/except"
            r"|catch\w*|rejection)\b",
        ),
        ("null-safety", r"\b(?:null|none|undefined|nil|nullable|optional chaining)\b"),
        (
            "resource",
            r"\b(?:leak\w*|not closed|never closed|file handles?|connection pool|dispose\w*)\b",
        ),
        (
            "performance",
            r"\b(?:performance|n\+1|quadratic|o\(n\^?2\)|slow\w*|inefficien\w*|unbounded)\b",
        ),
        (
            "api-contract",
            r"\b(?:breaking change|signature|backward[- ]compat\w*|contract|schema"
            r"|migrations?|callers?)\b",
        ),
        (
            "logic",
            r"\b(?:off[- ]by[- ]one|incorrect\w*|wrong|inverted|logic|always (?:true|false)"
            r"|never (?:true|reached|matches)|bug)\b",
        ),
    )
)

CATEGORIES = (*(name for name, _ in _CATEGORIES), "other")


def peer_category(title: str, body: str) -> str:
    """A coarse category from the thread's words; ``other`` when nothing matches.

    The title is tried first: bodies quote code, and a ``None`` in a code
    sample says nothing about what the finding is.
    """
    for text in (title, (body or "")[:1500]):
        for name, pattern in _CATEGORIES:
            if pattern.search(text or ""):
                return name
    return "other"


def bot_name(login: str, configured: list[str]) -> str:
    """The configured spelling of a login (``cursor[bot]``), for display and filters."""
    wanted = _normal(login)
    for entry in configured:
        if _normal(entry) == wanted:
            return entry
    return login


def _normal(login: str) -> str:
    return (login or "").removesuffix("[bot]").lower()


# --------------------------------------------------------------- matching


def mira_overlap(
    findings: list[dict[str, Any]], path: str, line: int, tolerance: int
) -> tuple[bool, bool, list[str]]:
    """``(near, same_file, finding_ids)`` for one peer thread against Mira's findings."""
    same_file = [f for f in findings if f["path"] == path]
    if line <= 0:
        # A file-level thread: any finding in the file is as near as it gets.
        return bool(same_file), bool(same_file), [f["id"] for f in same_file]
    hits = [
        f
        for f in same_file
        if ranges_overlap(f["start_line"], f["end_line"], line, line, tolerance)
    ]
    return bool(hits), bool(same_file), [f["id"] for f in hits]


# -------------------------------------------------------------- addressing


def _region_changed(diff_text: str, path: str, line: int, tolerance: int) -> bool:
    """Whether a diff that starts at the thread's commit touches lines near it.

    The thread's ``original_line`` is a position at its own commit, which is
    the *source* side of a diff from there, so hunks are compared by their
    source range. A pure insertion still has a source position.
    """
    from mira.core.diff_parser import parse_diff

    if not diff_text or line <= 0:
        return False
    try:
        patch = parse_diff(diff_text)
    except Exception:  # noqa: BLE001 - an unparseable diff proves nothing
        return False
    start, end = max(1, line - tolerance), line + tolerance
    for file in patch.files:
        if path not in {file.path, file.old_path}:
            continue
        for hunk in file.hunks:
            hunk_start = hunk.source_start
            hunk_end = hunk.source_start + max(hunk.source_length, 1) - 1
            if hunk_start <= end and hunk_end >= start:
                return True
    return False


def file_diff(diff_text: str, path: str, *, limit: int = 6000) -> str:
    """One file's hunks from a multi-file diff, bounded, for the synthesiser."""
    from mira.core.diff_parser import parse_diff

    try:
        patch = parse_diff(diff_text or "")
    except Exception:  # noqa: BLE001
        return ""
    for file in patch.files:
        if path in {file.path, file.old_path}:
            return "\n".join(hunk.content for hunk in file.hunks)[:limit]
    return ""


def dismissed(thread: PeerReviewThread) -> bool:
    return any(_DISMISSED_RE.search(reply.body or "") for reply in thread.replies)


def assess_addressed(
    thread: PeerReviewThread,
    snapshot: PeerReviewSnapshot,
    *,
    region_changed: bool | None,
) -> tuple[bool, str]:
    """``(addressed, reason)`` for one peer thread. Conservative by design.

    A thread counts as addressed only on a **merged** pull request, with no
    reply in it dismissing the finding (won't fix, false positive, by design,
    out of scope, follow-up …), and with one of:

    * ``reply`` — a reply, from anyone but Mira, claiming the fix ("fixed",
      "addressed in <sha>", "corrigido" …; :func:`autolearn.fix_claimed`);
    * ``resolved`` — the thread is resolved **and** the code within
      ``addressed_line_tolerance`` lines of it changed between the commit the
      bot reviewed and the merged head;
    * ``outdated`` — the thread is outdated **and** that same check holds.

    Resolution alone is not enough: resolving is also how people clear bot
    noise. Outdatedness alone is not either: it only says the diff moved,
    which is what any later push near the line looks like (the evaluation
    analytics exclude it for the same reason). ``region_changed`` is None when
    the history could not be read, and then only a reply can count.
    """
    from mira.feedback.autolearn import fix_claimed

    if snapshot.state != "merged":
        return False, NOT_MERGED
    if dismissed(thread):
        return False, DISMISSED
    viewer = _normal(snapshot.viewer_login)
    for reply in thread.replies:
        if _normal(reply.author) != viewer and fix_claimed(reply.body):
            return True, ADDRESSED_REPLY
    if not (thread.is_resolved or thread.is_outdated):
        return False, ""
    if region_changed is None:
        return False, UNVERIFIED
    if not region_changed:
        return False, ""
    return True, ADDRESSED_RESOLVED if thread.is_resolved else ADDRESSED_OUTDATED


# ---------------------------------------------------------------- collect


@dataclass
class CollectResult:
    """What one repository's collection did."""

    prs_examined: int = 0
    prs_skipped_open: int = 0
    threads: int = 0
    missed_and_addressed: int = 0
    unsupported: bool = False
    errors: list[str] = field(default_factory=list)


def _pr_scope(scope: PRInfo, number: int, *, url: str = "", head_sha: str = "") -> PRInfo:
    return PRInfo(
        title="",
        description="",
        base_branch="",
        head_branch="",
        url=url,
        number=number,
        owner=scope.owner,
        repo=scope.repo,
        platform=scope.platform,
        head_sha=head_sha,
    )


async def _region_checks(
    provider: Any,
    scope: PRInfo,
    snapshot: PeerReviewSnapshot,
    threads: list[PeerReviewThread],
    tolerance: int,
) -> dict[str, bool | None]:
    """Per thread id: did the code near it change between its commit and the merge?

    One compare per distinct commit the threads were opened on, and only for
    threads where the answer could decide anything: resolved or outdated, on
    a merged pull request, not already dismissed.
    """
    changed: dict[str, bool | None] = {}
    diffs: dict[str, str] = {}
    if snapshot.state != "merged" or not snapshot.head_sha:
        return changed
    pr = _pr_scope(scope, snapshot.number, url=snapshot.url, head_sha=snapshot.head_sha)
    for thread in threads:
        if not (thread.is_resolved or thread.is_outdated) or dismissed(thread):
            continue
        base = thread.original_commit
        if not base or base == snapshot.head_sha:
            changed[thread.thread_id] = False if base else None
            continue
        if base not in diffs:
            try:
                diff = await provider.get_compare_diff(pr, base, snapshot.head_sha)
                diffs[base] = diff if isinstance(diff, str) else ""
            except Exception as exc:  # noqa: BLE001 - one unreadable compare
                logger.debug("Compare %s..%s failed: %s", base[:10], snapshot.head_sha[:10], exc)
                diffs[base] = ""
                changed[thread.thread_id] = None
                continue
        if not diffs[base]:
            changed[thread.thread_id] = None
            continue
        changed[thread.thread_id] = _region_changed(
            diffs[base], thread.path, thread.original_line or thread.line, tolerance
        )
    return changed


def build_rows(
    scope: PRInfo,
    snapshot: PeerReviewSnapshot,
    findings: list[dict[str, Any]],
    config: PeerRecallConfig,
    changed: dict[str, bool | None],
) -> list[PeerThread]:
    """One :class:`PeerThread` per peer thread on a closed pull request."""
    closed_at = snapshot.merged_at or snapshot.closed_at or time.time()
    rows: list[PeerThread] = []
    for thread in snapshot.threads:
        line = thread.line or thread.original_line
        near, same_file, ids = mira_overlap(
            findings, thread.path, line, config.match_line_tolerance
        )
        addressed, reason = assess_addressed(
            thread, snapshot, region_changed=changed.get(thread.thread_id)
        )
        title = peer_title(thread.body)
        rows.append(
            PeerThread(
                id=peer_thread_id(
                    scope.platform, scope.owner, scope.repo, snapshot.number, thread.thread_id
                ),
                platform=scope.platform,
                owner=scope.owner,
                repo=scope.repo,
                pr_number=snapshot.number,
                bot=bot_name(thread.author, config.bots),
                path=thread.path,
                line=line,
                thread_id=thread.thread_id,
                url=thread.url,
                pr_url=snapshot.url,
                pr_state=snapshot.state,
                pr_closed_at=closed_at,
                title=title,
                category=peer_category(title, thread.body),
                body=(thread.body or "")[:BODY_LIMIT],
                original_commit=thread.original_commit,
                head_sha=snapshot.head_sha,
                addressed=addressed,
                addressed_reason=reason,
                mira_flagged=near,
                mira_flagged_file=same_file,
                finding_ids=ids,
                thread_created_at=thread.created_at,
            )
        )
    return rows


async def collect_pull_request(
    provider: Any,
    scope: PRInfo,
    number: int,
    quality_store: Any,
    config: PeerRecallConfig,
) -> tuple[str, list[PeerThread]]:
    """Read and record one pull request. Returns ``(state, rows)``; ``"unsupported"``
    when the platform cannot list peer threads."""
    snapshot = await provider.get_peer_review_threads(_pr_scope(scope, number), config.bots)
    if snapshot is None:
        return "unsupported", []
    if snapshot.state == "open":
        return "open", []
    changed = await _region_checks(
        provider, scope, snapshot, snapshot.threads, config.addressed_line_tolerance
    )
    rows = build_rows(scope, snapshot, quality_store.findings_for_pr(number), config, changed)
    for row in rows:
        quality_store.upsert_peer_thread(row)
    quality_store.record_peer_scan(
        scope.platform,
        number,
        pr_state=snapshot.state,
        threads=len(rows),
        owner=scope.owner,
        repo=scope.repo,
    )
    return snapshot.state, rows


async def collect_repository(
    provider: Any,
    owner: str,
    repo: str,
    quality_store: Any,
    config: PeerRecallConfig,
    *,
    platform: str = "github",
    rescan: bool = False,
    now: float | None = None,
) -> CollectResult:
    """Read every closed pull request Mira reviewed in the lookback window.

    A pull request already read after it closed is skipped unless ``rescan``;
    at most ``max_prs_per_run`` are read (an open one counts: it was asked).
    Never raises for one pull request; the error is noted and the rest run.
    """
    from mira.quality.backtest import repo_scope

    result = CollectResult()
    if platform != "github":
        result.unsupported = True
        return result
    scope = repo_scope(owner, repo, platform)
    since = (now or time.time()) - config.lookback_days * 86400
    # The cap bounds GitHub reads, not the candidate list: on a busy repository
    # the most recent reviews are mostly still open, and capping the list
    # instead would starve the older, closed ones that are actually ready.
    numbers = quality_store.reviewed_pr_numbers(since=since, limit=10_000)
    reads = 0
    for number in numbers:
        if not rescan and quality_store.peer_scanned(platform, number):
            continue
        if reads >= config.max_prs_per_run:
            break
        reads += 1
        try:
            state, rows = await collect_pull_request(provider, scope, number, quality_store, config)
        except Exception as exc:  # noqa: BLE001 - one pull request must not stop the rest
            logger.warning("Peer recall: %s/%s#%s unreadable: %s", owner, repo, number, exc)
            result.errors.append(f"#{number}: {exc}")
            continue
        if state == "unsupported":
            result.unsupported = True
            break
        if state == "open":
            result.prs_skipped_open += 1
            continue
        result.prs_examined += 1
        result.threads += len(rows)
        result.missed_and_addressed += sum(1 for row in rows if row.missed_and_addressed)
    return result
