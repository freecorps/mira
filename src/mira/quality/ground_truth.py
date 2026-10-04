"""What actually happened to a pull request's lines after it was reviewed.

Three independent sources, each imperfect, each recorded with its kind so a
report reader can weigh them:

* **Human review comments** left on the pull request — a person thought those
  lines worth a remark. Positive evidence.
* **Fix and revert commits** that touched the same lines within a window after
  the merge — the strongest evidence that something there was wrong.
* **Feedback Mira already holds** for its own findings on that pull request —
  a thumbs-up or a resolved thread is positive at that spot, a thumbs-down or
  a dismissal is negative (the only negative evidence there is).

Nothing here guesses when a source cannot answer: a provider that cannot
search history produces a note, not an empty list that reads as "no fixes".
"""

from __future__ import annotations

import logging
import re
from typing import Any

from mira.models import MergedPullRequest, PRInfo
from mira.quality.lines import changed_lines, to_ranges
from mira.quality.models import (
    SIGNAL_FEEDBACK_NEGATIVE,
    SIGNAL_FEEDBACK_POSITIVE,
    SIGNAL_FIX_COMMIT,
    SIGNAL_HUMAN_COMMENT,
    SIGNAL_REVERT,
    Signal,
)

logger = logging.getLogger(__name__)

REVERT_MESSAGE = re.compile(r"^\s*revert\b|this reverts commit", re.IGNORECASE | re.MULTILINE)
FIX_MESSAGE = re.compile(
    r"\b(fix(e[sd])?|bug(fix)?|hotfix|regression|broken|crash(es|ed)?|patch(es|ed)?)\b",
    re.IGNORECASE,
)

# Review comments that carry no claim about the code.
_TRIVIAL_COMMENTS = {
    "lgtm",
    "+1",
    "👍",
    "thanks",
    "thank you",
    "nice",
    "done",
    "fixed",
    "ok",
    "agreed",
}


def classify_commit_message(message: str) -> str:
    """``revert``, ``fix_commit`` or ``""`` for a commit message."""
    first_line = (message or "").strip()
    if REVERT_MESSAGE.search(first_line):
        return SIGNAL_REVERT
    if FIX_MESSAGE.search(first_line.splitlines()[0] if first_line else ""):
        return SIGNAL_FIX_COMMIT
    return ""


def _is_trivial(body: str) -> bool:
    text = (body or "").strip().lower().rstrip("!.")
    return len(text) < 8 or text in _TRIVIAL_COMMENTS


async def human_comment_signals(
    provider: Any, pr_info: PRInfo, bot_login: str, notes: list[str]
) -> list[Signal]:
    try:
        comments = await provider.get_human_review_comments(pr_info, bot_login)
    except Exception as exc:  # noqa: BLE001 - recorded as a note, not as "none"
        notes.append(f"human review comments unavailable: {exc}")
        return []
    out = []
    author = (pr_info.author or "").lower()
    for comment in comments:
        if not comment.path or not comment.line or _is_trivial(comment.body):
            continue
        if author and (comment.author or "").lower() == author:
            # The author answering a reviewer is not evidence about the code.
            continue
        out.append(
            Signal(
                kind=SIGNAL_HUMAN_COMMENT,
                path=comment.path,
                start=int(comment.line),
                end=int(comment.line),
                ref=comment.author,
                detail=comment.body[:200],
            )
        )
    return out


async def fix_commit_signals(
    provider: Any,
    pr_info: PRInfo,
    merged: MergedPullRequest | None,
    diff_text: str,
    *,
    window_days: int,
    max_files: int,
    max_commits: int,
    notes: list[str],
) -> list[Signal]:
    """Lines later fix/revert commits touched in files this pull request changed."""
    if merged is None or not merged.merged_at or max_files <= 0:
        return []
    files = changed_lines(diff_text)
    # Largest changes first: they are where a later fix is most likely to land,
    # and the cap is what keeps a 200-file PR from costing 200 history reads.
    ranked = sorted(files.values(), key=lambda f: len(f.added) + len(f.removed), reverse=True)
    own = {s for s in (merged.merge_commit_sha, merged.head_sha) if s}
    since = merged.merged_at + 1
    until = merged.merged_at + window_days * 86400
    diff_cache: dict[str, str] = {}
    out: list[Signal] = []
    for entry in ranked[:max_files]:
        if not entry.added:
            continue  # deleted file: nothing of this PR's left to fix
        try:
            commits = await provider.list_path_commits(
                pr_info,
                entry.path,
                since=since,
                until=until,
                ref=merged.base_branch or pr_info.base_branch,
                limit=max_commits,
            )
        except NotImplementedError:
            notes.append("provider cannot search file history; fix commits not considered")
            return out
        except Exception as exc:  # noqa: BLE001
            notes.append(f"history of {entry.path} unavailable: {exc}")
            continue
        for commit in commits:
            if commit.sha in own or any(commit.sha.startswith(s) for s in own):
                continue
            kind = classify_commit_message(commit.message)
            if not kind:
                continue
            if commit.sha not in diff_cache:
                try:
                    diff_cache[commit.sha] = await provider.get_commit_diff(pr_info, commit.sha)
                except Exception as exc:  # noqa: BLE001
                    notes.append(f"diff of {commit.sha[:10]} unavailable: {exc}")
                    diff_cache[commit.sha] = ""
            touched = changed_lines(diff_cache[commit.sha]).get(entry.path)
            if touched is None:
                continue
            for start, end in to_ranges(touched.removed):
                out.append(
                    Signal(
                        kind=kind,
                        path=entry.path,
                        start=start,
                        end=end,
                        ref=commit.sha,
                        detail=commit.message.strip().splitlines()[0][:200]
                        if commit.message.strip()
                        else "",
                    )
                )
    return out


def feedback_signals(quality_store: Any, pr_number: int) -> list[Signal]:
    """Signals from feedback recorded against Mira's own findings on this PR."""
    if quality_store is None:
        return []
    from mira.feedback.evaluation import NEGATIVE_KINDS, POSITIVE_KINDS

    findings = {f["id"]: f for f in quality_store.findings_for_pr(pr_number)}
    if not findings:
        return []
    try:
        events = quality_store.index_store.list_feedback_v2(pr_number=pr_number, limit=2000)
    except Exception as exc:  # noqa: BLE001
        logger.debug("Feedback unavailable for PR %s: %s", pr_number, exc)
        return []
    kinds: dict[str, set[str]] = {}
    for event in events:
        if event.finding_id in findings:
            kinds.setdefault(event.finding_id, set()).add(event.kind)
    out: list[Signal] = []
    for finding_id, seen in kinds.items():
        finding = findings[finding_id]
        positive = bool(seen & set(POSITIVE_KINDS))
        negative = bool(seen & set(NEGATIVE_KINDS))
        if not positive and not negative:
            continue
        out.append(
            Signal(
                kind=SIGNAL_FEEDBACK_POSITIVE if positive else SIGNAL_FEEDBACK_NEGATIVE,
                path=finding["path"],
                start=finding["start_line"],
                end=finding["end_line"],
                positive=positive,
                ref=finding_id,
                detail=",".join(sorted(seen)),
            )
        )
    return out


def dedupe(signals: list[Signal]) -> list[Signal]:
    seen: set[tuple[str, str, int, int, bool]] = set()
    out = []
    for signal in signals:
        key = (signal.kind, signal.path, signal.start, signal.end, signal.positive)
        if key in seen:
            continue
        seen.add(key)
        out.append(signal)
    return out


async def collect_signals(
    provider: Any,
    pr_info: PRInfo,
    merged: MergedPullRequest | None,
    diff_text: str,
    *,
    bot_login: str,
    window_days: int,
    max_files: int,
    max_commits: int,
    quality_store: Any = None,
) -> tuple[list[Signal], list[str]]:
    """All ground-truth signals for one pull request, plus notes on what was missing."""
    notes: list[str] = []
    signals: list[Signal] = []
    signals.extend(await human_comment_signals(provider, pr_info, bot_login, notes))
    signals.extend(
        await fix_commit_signals(
            provider,
            pr_info,
            merged,
            diff_text,
            window_days=window_days,
            max_files=max_files,
            max_commits=max_commits,
            notes=notes,
        )
    )
    try:
        signals.extend(feedback_signals(quality_store, pr_info.number))
    except Exception as exc:  # noqa: BLE001
        notes.append(f"stored feedback unavailable: {exc}")
    return dedupe(signals), notes
