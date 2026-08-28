"""Triaging one pull request: gather, rank, persist, announce.

The order never varies, and each step is bounded by what the one before it
established.

1. **Resolve the policy.** Triage off for this repository means nothing is
   fetched and nothing is written. A repository that has not opted in must not
   have its contributors' names collected into a run row.
2. **Gather.** The changed files (from the review that just ran, when there was
   one), then ownership at the base and history for the hottest paths — the two
   in parallel, under one budget.
3. **Rank**, in :mod:`mira.triage.scoring`, which is pure arithmetic: no
   provider, no store, no model. That is what makes a suggestion reproducible.
4. **Persist** before announcing. A suggestion nobody can audit is a suggestion
   nobody should have to argue with.
5. **Announce**, if the policy asks and the run has something to say.

Two things this module deliberately does not do.

**It never requests a review.** There is no provider call here that assigns,
requests or adds anybody, and the capability table has no flag that would let
one appear. The output is a comment a human reads.

**It never blocks anything.** No status is published, so no branch protection
rule can be built on a suggestion, and the merge gate does not read triage at
all. A ranking that could hold up a merge would need to be right; a ranking
that can only be read needs to be useful, which is a much better contract for
something built on inference.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from typing import Any

from mira.config import MiraConfig, load_config
from mira.core.diff_parser import parse_diff
from mira.models import FileChangeStat
from mira.triage import capabilities as caps
from mira.triage import history as history_signal
from mira.triage import load as load_signal
from mira.triage import ownership as ownership_signal
from mira.triage.classify import classify
from mira.triage.explain import one_line, public_explanation
from mira.triage.models import COMMENT_MARKER, TriageInputs, TriageRun, run_key
from mira.triage.policy import EffectiveTriagePolicy, resolve_policy
from mira.triage.scoring import rank

logger = logging.getLogger(__name__)


class TriageUnavailable(Exception):
    """The facts every signal depends on could not be read.

    Raised rather than returning an empty change list, because an empty change
    list is a *fact* — "this pull request touches nothing" — and a failed fetch
    is not. Ranking on the first when the truth is the second would produce a
    confident "nobody to suggest" from a network error.
    """


class ReviewSignal:
    """What the review pass already knows, handed over rather than re-fetched."""

    def __init__(
        self,
        *,
        diff_text: str = "",
        changes: list[FileChangeStat] | None = None,
        review_id: int = 0,
    ) -> None:
        self.diff_text = diff_text
        self.changes = changes
        self.review_id = review_id


def _open_store(owner: str, repo: str, platform: str) -> Any:
    from mira.index.store import IndexStore

    return IndexStore.open(owner, repo, platform=platform)


async def gather_changes(
    provider: Any, pr_info: Any, signal: ReviewSignal | None = None
) -> list[FileChangeStat]:
    """The changed files, from the review's own parse where there was one."""
    signal = signal or ReviewSignal()
    if signal.changes is not None and signal.changes:
        return list(signal.changes)

    if signal.diff_text:
        try:
            patch_set = parse_diff(signal.diff_text)
        except Exception as exc:  # noqa: BLE001 - fall through to the provider
            logger.debug("Triage could not parse the handed-over diff: %s", exc)
        else:
            if patch_set.files:
                return [
                    FileChangeStat(
                        path=file_diff.path,
                        added_lines=file_diff.added_lines,
                        deleted_lines=file_diff.deleted_lines,
                    )
                    for file_diff in patch_set.files
                ]

    if provider is None:
        raise TriageUnavailable("no provider was attached, so the changed files are unknown")
    try:
        return list(await provider.get_pr_change_stats(pr_info))
    except Exception as exc:  # noqa: BLE001 - one failure mode, named
        raise TriageUnavailable(f"the changed files could not be read: {exc}") from exc


def _inputs_for(
    pr_info: Any,
    changes: list[FileChangeStat],
    *,
    ownership_ref: str = "",
    review_id: int = 0,
) -> TriageInputs:
    return TriageInputs(
        platform=str(getattr(pr_info, "platform", "github") or "github"),
        owner=pr_info.owner,
        repo=pr_info.repo,
        pr_number=int(getattr(pr_info, "number", 0) or 0),
        pr_url=str(getattr(pr_info, "url", "") or ""),
        pr_author=str(getattr(pr_info, "author", "") or ""),
        pr_title=str(getattr(pr_info, "title", "") or ""),
        base_branch=str(getattr(pr_info, "base_branch", "") or ""),
        base_sha=str(getattr(pr_info, "base_sha", "") or ""),
        head_sha=str(getattr(pr_info, "head_sha", "") or ""),
        draft=bool(getattr(pr_info, "draft", False)),
        changed_paths=[change.path for change in changes],
        changed_files=len(changes),
        added_lines=sum(change.added_lines for change in changes),
        deleted_lines=sum(change.deleted_lines for change in changes),
        ownership_ref=ownership_ref,
        review_id=int(review_id),
    )


def _key_for(inputs: TriageInputs, policy: EffectiveTriagePolicy) -> str:
    return run_key(
        platform=inputs.platform,
        owner=inputs.owner,
        repo=inputs.repo,
        pr_number=inputs.pr_number,
        head_sha=inputs.head_sha,
        policy_version=policy.version,
        inputs_digest=inputs.digest,
    )


def failed_run(pr_info: Any, policy: EffectiveTriagePolicy, message: str) -> TriageRun:
    """A run that records why there is no suggestion, and suggests nobody.

    Public because the same shape is needed by anything that runs the ranker
    without a full pull request behind it: a failure has to arrive as
    ``unavailable``, never as an absence and never as ``no_candidates``.
    """
    inputs = _inputs_for(pr_info, [])
    return TriageRun(
        run_key=run_key(
            platform=inputs.platform,
            owner=inputs.owner,
            repo=inputs.repo,
            pr_number=inputs.pr_number,
            head_sha=inputs.head_sha,
            policy_version=policy.version,
            inputs_digest=f"error:{message[:60]}",
        ),
        policy_version=policy.version,
        inputs=inputs,
        error=message,
    )


def _persist(run: TriageRun) -> tuple[TriageRun, bool]:
    """Write the run, tolerating a store that is unavailable."""
    inputs = run.inputs
    try:
        store = _open_store(inputs.owner, inputs.repo, inputs.platform)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Triage could not open its store for %s: %s", inputs.pr_url, exc)
        run.error = run.error or f"run not recorded: {exc}"
        return run, False
    try:
        return store.record_triage_run(run)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Triage could not record a run for %s: %s", inputs.pr_url, exc)
        run.error = run.error or f"run not recorded: {exc}"
        return run, False
    finally:
        store.close()


async def announce(
    provider: Any, pr_info: Any, run: TriageRun, policy: EffectiveTriagePolicy
) -> None:
    """Publish the suggestion where the policy asks. Best effort, and quiet.

    A comment is *created* only for a run that has something to suggest. Every
    other status updates a comment that already exists and creates none — a
    repository where triage can never find anybody should not collect a comment
    on every pull request saying so, and a suggestion that has gone stale after
    a force-push should not be left standing.

    Drafts are recorded and not announced. A draft is not asking for a reviewer
    yet; marking it ready re-runs this and the comment appears then.
    """
    if not policy.comment or provider is None:
        return
    if run.inputs.draft:
        return

    body = f"{COMMENT_MARKER}\n{public_explanation(run)}"
    try:
        existing = await provider.find_bot_comment(pr_info, COMMENT_MARKER)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Could not look for the triage comment on %s: %s", pr_info.url, exc)
        return

    try:
        if existing:
            await provider.update_comment(pr_info, existing, body)
        elif run.status == "ok":
            await provider.post_comment(pr_info, body)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Could not publish the triage comment on %s: %s", pr_info.url, exc)


async def _gather_signals(
    provider: Any,
    pr_info: Any,
    changes: list[FileChangeStat],
    *,
    policy: EffectiveTriagePolicy,
    capability: caps.TriageCapabilities,
    store: Any,
    platform: str,
    now: float,
) -> tuple[ownership_signal.OwnershipOutcome, history_signal.HistoryOutcome]:
    """Read ownership and history in parallel, under one budget."""
    ref = ownership_signal.base_ref(pr_info)
    paths = [change.path for change in changes]

    ownership_task = ownership_signal.gather(
        provider,
        pr_info,
        paths,
        enabled=policy.codeowners,
        can_read=capability.can_read_ownership,
    )
    history_task = history_signal.gather(
        provider,
        pr_info,
        changes,
        store=store,
        enabled=policy.history,
        can_attribute_commits=capability.can_attribute_commits,
        window_days=policy.history_days,
        max_paths=policy.history_max_paths,
        max_per_path=policy.history_max_per_path,
        refresh_hours=policy.history_refresh_hours,
        ref=ref,
        platform=platform,
        now=now,
    )
    return await asyncio.gather(ownership_task, history_task)


def _timed_out(
    policy: EffectiveTriagePolicy,
) -> tuple[ownership_signal.OwnershipOutcome, history_signal.HistoryOutcome]:
    """What the signals report when the run's budget ran out.

    Every one of them reports ``unavailable``, which makes the run
    ``unavailable`` rather than ``no_candidates``. A budget that expired is a
    question left unanswered, and the reader is told so in those words.
    """
    from mira.triage.models import SignalReport

    detail = f"the {policy.budget_seconds:g}s triage budget ran out before this signal answered"
    return (
        ownership_signal.OwnershipOutcome(
            report=SignalReport(kind="codeowners", status="unavailable", detail=detail)
        ),
        history_signal.HistoryOutcome(
            authored_report=SignalReport(kind="authored", status="unavailable", detail=detail),
            reviewed_report=SignalReport(kind="reviewed", status="unavailable", detail=detail),
        ),
    )


async def evaluate(
    provider: Any,
    pr_info: Any,
    *,
    config: MiraConfig | None = None,
    signal: ReviewSignal | None = None,
    announce_result: bool = True,
) -> TriageRun:
    """Triage one pull request and record the run. Never raises."""
    config = config or load_config()
    policy = resolve_policy(config.triage, pr_info.owner, pr_info.repo)

    if not policy.active:
        # Nothing fetched, nothing written, nobody's name recorded.
        return TriageRun(policy_version=policy.version, inputs=_inputs_for(pr_info, []))

    started = time.monotonic()
    now = time.time()
    platform = str(getattr(pr_info, "platform", "github") or "github")
    capability = caps.for_provider(provider)

    try:
        changes = await gather_changes(provider, pr_info, signal)
    except TriageUnavailable as exc:
        run = failed_run(pr_info, policy, str(exc))
        _persist(run)
        logger.warning("Triage could not start on %s: %s", pr_info.url, exc)
        return run
    except Exception as exc:  # noqa: BLE001 - every failure is a non-answer
        run = failed_run(pr_info, policy, f"{type(exc).__name__}: {exc}")
        _persist(run)
        logger.warning("Triage could not start on %s: %s", pr_info.url, exc)
        return run

    store: Any = None
    notes: list[str] = []
    try:
        store = _open_store(pr_info.owner, pr_info.repo, platform)
    except Exception as exc:  # noqa: BLE001 - history degrades, ownership does not
        logger.debug("Triage could not open a store for %s: %s", pr_info.url, exc)

    try:
        try:
            ownership, history = await asyncio.wait_for(
                _gather_signals(
                    provider,
                    pr_info,
                    changes,
                    policy=policy,
                    capability=capability,
                    store=store,
                    platform=platform,
                    now=now,
                ),
                timeout=policy.budget_seconds,
            )
        except TimeoutError:
            logger.warning(
                "Triage ran out of budget on %s after %.1fs", pr_info.url, policy.budget_seconds
            )
            ownership, history = _timed_out(policy)
    finally:
        if store is not None:
            # Closing is best effort: the rows are already written, and a
            # handle that will not close is not a reason to fail a caller.
            with contextlib.suppress(Exception):
                store.close()

    review_load = load_signal.current(
        owner=pr_info.owner, repo=pr_info.repo, pr_number=int(getattr(pr_info, "number", 0) or 0)
    )
    if not review_load.available:
        notes.append(
            "Review load could not be read, so nobody's score was dampened for being "
            f"busy ({review_load.detail})."
        )

    candidates, excluded = rank(
        policy=policy,
        owners=ownership.owners,
        authored=history.authored,
        reviewed=history.reviewed,
        pr_author=str(getattr(pr_info, "author", "") or ""),
        load=review_load.counts,
        now=now,
    )

    inputs = _inputs_for(
        pr_info,
        changes,
        ownership_ref=ownership.ref,
        review_id=(signal.review_id if signal else 0),
    )
    run = TriageRun(
        run_key=_key_for(inputs, policy),
        policy_version=policy.version,
        inputs=inputs,
        classification=classify(changes),
        candidates=candidates,
        signals=[ownership.report, *history.reports],
        excluded=excluded,
        notes=notes,
        duration_seconds=round(time.monotonic() - started, 4),
    )

    stored, _created = _persist(run)
    logger.info("Triage on %s: %s", pr_info.url, one_line(stored))

    if announce_result and provider is not None:
        await announce(provider, pr_info, stored, policy)
    return stored


def latest_for(
    owner: str, repo: str, platform: str, pr_number: int, head_sha: str = ""
) -> TriageRun | None:
    """The newest recorded run for a pull request, or ``None``.

    ``None`` for "there is none", "the store could not be opened" and "the only
    runs are against an older commit" alike. Every caller of this is a reader,
    and a reader shown a suggestion computed from a different set of files
    would be shown names for code nobody attributed to them.
    """
    try:
        store = _open_store(owner, repo, platform)
    except Exception as exc:  # noqa: BLE001
        logger.debug("Could not open the triage store for %s/%s: %s", owner, repo, exc)
        return None
    try:
        return store.latest_triage_run(pr_number=pr_number, head_sha=head_sha)
    except Exception as exc:  # noqa: BLE001
        logger.debug("Could not read the latest triage run for %s/%s: %s", owner, repo, exc)
        return None
    finally:
        store.close()
