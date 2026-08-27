"""Running the checks for one pull request: gather, run, persist, announce.

The order is load-bearing and never varies.

1. **Resolve the policy.** If checks are off for this repository, stop here.
   Nothing is fetched and nothing is written — an install that never turned
   checks on must not pay for them, and a repository that opted out must not
   have its pull-request data copied into a run row.
2. **Gather inputs.** The diff, the changed files and the labels, once, so
   every check reasons about the same facts. Anything unreadable here produces
   a run that records the failure and reports nothing about the change.
3. **Run**, in :mod:`mira.checks.runner`, which is pure: no store, no platform.
4. **Persist** before announcing. A run that was announced but never recorded
   is a verdict nobody can audit.
5. **Announce**, if the policy asks for it and the provider can.

Steps 4 and 5 are the only ones with side effects, and neither of them can turn
a result into something it was not: the announcement renders what was stored,
and a failure to announce is logged rather than folded back into the verdict.

The relationship with the merge gate is one-directional and worth stating.
Checks never approve, never request changes and never merge anything. They
produce a verdict; the gate reads it as one input among several and applies its
own fail-closed rules. A check in ``warning`` mode is invisible to the gate by
construction, because ``CheckResult.blocking`` is false for it.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from mira.checks import capabilities as caps
from mira.checks.context import CheckContext
from mira.checks.explain import (
    one_line,
    public_explanation,
    status_conclusion,
    status_title,
)
from mira.checks.models import (
    COMMENT_MARKER,
    STATUS_CONTEXT,
    CheckRun,
    CheckRunInputs,
    run_key,
)
from mira.checks.policy import EffectiveChecksPolicy, resolve_policy
from mira.checks.runner import run_checks
from mira.config import MiraConfig, load_config
from mira.core.diff_parser import parse_diff
from mira.models import FileChangeStat, PatchSet

logger = logging.getLogger(__name__)


class ChecksUnavailable(Exception):
    """An input every check depends on could not be read.

    Raised rather than returning a partial context, so there is no code path
    where a missing fact silently reads as a benign one — a diff that failed to
    fetch must not become "this pull request changes nothing", which would pass
    the tests check, the docs check and the migrations check at once.
    """


class ReviewSignal:
    """What the review pass already knows, passed in rather than re-fetched.

    Checks usually run right after a review that has already fetched the diff
    and parsed it. Re-deriving that would cost a second diff fetch per pull
    request on a device that has none to spare.
    """

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


def _llm_factory(config: MiraConfig) -> Any:
    """A callable that builds a review-tier client when one is asked for.

    A factory rather than a client, so a run whose policy carries no
    natural-language rule and no CI summarisation never constructs one — which
    on the reference deployment is most runs. A deployment with no usable model
    configuration fails when the client is *used*, and the check that used it
    records an infrastructure error naming the failure, which is the right
    place for it: one rule cannot answer, and nothing else about the run
    changes.
    """

    def _build() -> Any:
        from mira.dashboard.models_config import llm_config_for
        from mira.llm import create_llm

        return create_llm(llm_config_for("review", config.llm))

    return _build


async def gather_context(
    provider: Any,
    pr_info: Any,
    policy: EffectiveChecksPolicy,
    *,
    config: MiraConfig,
    signal: ReviewSignal | None = None,
) -> tuple[CheckContext, CheckRunInputs]:
    """Collect every fact the checks share. Raises on anything unreadable."""
    signal = signal or ReviewSignal()
    capability = caps.for_provider(provider)

    diff_text = signal.diff_text
    if not diff_text and provider is not None:
        try:
            diff_text = await provider.get_pr_diff(pr_info)
        except Exception as exc:  # noqa: BLE001 - re-raised as one failure mode
            raise ChecksUnavailable(f"the diff could not be read: {exc}") from exc

    patch_set: PatchSet = parse_diff(diff_text or "")

    changes = list(signal.changes) if signal.changes is not None else []
    if not changes:
        changes = [
            FileChangeStat(
                path=file_diff.path,
                added_lines=file_diff.added_lines,
                deleted_lines=file_diff.deleted_lines,
            )
            for file_diff in patch_set.files
        ]
    if not changes and provider is not None:
        # An empty parse is not proof of an empty pull request — a binary-only
        # or very large diff can produce one — so the platform is asked before
        # every path-shaped check is handed an empty list to agree with.
        try:
            changes = list(await provider.get_pr_change_stats(pr_info))
        except Exception as exc:  # noqa: BLE001
            raise ChecksUnavailable(f"the changed files could not be read: {exc}") from exc

    labels: list[str] = []
    if provider is not None:
        try:
            labels = list(await provider.get_pr_labels(pr_info))
        except Exception as exc:  # noqa: BLE001 - labels only narrow scope
            # A missing label list can only make a check *apply* where an
            # exemption would have excused it, so it degrades rather than
            # failing the run. The ticket check is the only consumer, and
            # over-applying it is the conservative direction.
            logger.debug("Checks could not read labels for %s: %s", pr_info.url, exc)

    platform = getattr(pr_info, "platform", "github")
    inputs = CheckRunInputs(
        platform=platform,
        owner=pr_info.owner,
        repo=pr_info.repo,
        pr_number=pr_info.number,
        pr_url=pr_info.url,
        pr_author=getattr(pr_info, "author", "") or "",
        pr_title=getattr(pr_info, "title", "") or "",
        base_branch=getattr(pr_info, "base_branch", "") or "",
        head_branch=getattr(pr_info, "head_branch", "") or "",
        head_sha=getattr(pr_info, "head_sha", "") or "",
        draft=bool(getattr(pr_info, "draft", False)),
        changed_paths=[change.path for change in changes],
        changed_files=len(changes),
        added_lines=sum(change.added_lines for change in changes),
        deleted_lines=sum(change.deleted_lines for change in changes),
        review_id=signal.review_id,
    )

    ctx = CheckContext(
        policy=policy,
        platform=platform,
        owner=inputs.owner,
        repo=inputs.repo,
        pr_number=inputs.pr_number,
        pr_url=inputs.pr_url,
        pr_author=inputs.pr_author,
        pr_title=inputs.pr_title,
        pr_body=getattr(pr_info, "description", "") or "",
        base_branch=inputs.base_branch,
        head_branch=inputs.head_branch,
        head_sha=inputs.head_sha,
        draft=inputs.draft,
        labels=labels,
        changes=changes,
        patch_set=patch_set,
        diff_text=diff_text or "",
        provider=provider,
        pr_info=pr_info,
        llm_factory=_llm_factory(config),
    )
    logger.debug(
        "Checks on %s: %d changed file(s), provider %s",
        inputs.pr_url,
        len(changes),
        capability.provider,
    )
    return ctx, inputs


def _failed_run(pr_info: Any, policy: EffectiveChecksPolicy, message: str) -> CheckRun:
    """A run that records why nothing ran, and reports nothing about the change.

    Given its own inputs snapshot rather than a partial one, so an audit can
    never mistake "these are the facts we checked" for "these are the facts we
    managed to fetch before it broke".
    """
    inputs = CheckRunInputs(
        platform=getattr(pr_info, "platform", "github"),
        owner=pr_info.owner,
        repo=pr_info.repo,
        pr_number=pr_info.number,
        pr_url=pr_info.url,
        pr_author=getattr(pr_info, "author", "") or "",
        pr_title=getattr(pr_info, "title", "") or "",
        head_sha=getattr(pr_info, "head_sha", "") or "",
    )
    return CheckRun(
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


def _persist(run: CheckRun) -> tuple[CheckRun, bool]:
    """Write the run, tolerating a store that is unavailable.

    A run that cannot be recorded is still *returned* — the caller may be about
    to render it — but the error is set, so nothing downstream treats it as an
    auditable result.
    """
    inputs = run.inputs
    try:
        store = _open_store(inputs.owner, inputs.repo, inputs.platform)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Checks could not open their store for %s: %s", inputs.pr_url, exc)
        run.error = run.error or f"run not recorded: {exc}"
        return run, False
    try:
        return store.record_check_run(run)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Checks could not record a run for %s: %s", inputs.pr_url, exc)
        run.error = run.error or f"run not recorded: {exc}"
        return run, False
    finally:
        store.close()


async def announce(
    provider: Any, pr_info: Any, run: CheckRun, policy: EffectiveChecksPolicy
) -> None:
    """Publish the run where the policy asks and the provider can.

    Best effort, and deliberately so: the run is already recorded, and a
    provider that refuses a status must not turn a completed set of checks into
    an error. Every failure is logged with the channel that failed.
    """
    capability = caps.for_provider(provider)

    if policy.publish_status and capability.can_publish_status:
        try:
            await provider.publish_checks_status(
                pr_info,
                context=STATUS_CONTEXT,
                conclusion=status_conclusion(run),
                title=status_title(run),
                summary=public_explanation(run)[:60_000],
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("Could not publish the check status on %s: %s", pr_info.url, exc)

    if policy.comment and provider is not None:
        body = f"{COMMENT_MARKER}\n{public_explanation(run)}"
        try:
            existing = await provider.find_bot_comment(pr_info, COMMENT_MARKER)
            if existing:
                await provider.update_comment(pr_info, existing, body)
            else:
                await provider.post_comment(pr_info, body)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Could not post the check summary on %s: %s", pr_info.url, exc)


async def evaluate(
    provider: Any,
    pr_info: Any,
    *,
    config: MiraConfig | None = None,
    signal: ReviewSignal | None = None,
    announce_result: bool = True,
) -> CheckRun:
    """Run every check for one pull request and record the run. Never raises."""
    config = config or load_config()
    policy = resolve_policy(config.checks, pr_info.owner, pr_info.repo)

    if not policy.active:
        # Nothing fetched, nothing written. The caller gets an empty run whose
        # verdict is `not_run`, which no gate reads as permission.
        return CheckRun(
            policy_version=policy.version,
            inputs=CheckRunInputs(
                platform=getattr(pr_info, "platform", "github"),
                owner=pr_info.owner,
                repo=pr_info.repo,
                pr_number=pr_info.number,
                pr_url=pr_info.url,
                head_sha=getattr(pr_info, "head_sha", "") or "",
            ),
        )

    started = time.monotonic()
    try:
        ctx, inputs = await gather_context(provider, pr_info, policy, config=config, signal=signal)
    except ChecksUnavailable as exc:
        run = _failed_run(pr_info, policy, str(exc))
        _persist(run)
        logger.warning("Pre-merge checks could not start on %s: %s", pr_info.url, exc)
        return run
    except Exception as exc:  # noqa: BLE001 - every failure is a non-answer
        run = _failed_run(pr_info, policy, f"{type(exc).__name__}: {exc}")
        _persist(run)
        logger.warning("Pre-merge checks could not start on %s: %s", pr_info.url, exc)
        return run

    run = await run_checks(ctx, inputs)
    run.duration_seconds = round(time.monotonic() - started, 4)
    stored, created = _persist(run)
    logger.info("Pre-merge checks on %s: %s", pr_info.url, one_line(stored))

    if announce_result and provider is not None:
        await announce(provider, pr_info, stored, policy)
    return stored


def latest_verdict(owner: str, repo: str, platform: str, pr_number: int, head_sha: str) -> str:
    """The verdict of the newest run for this commit, for the merge gate.

    Returns ``not_run`` when there is none, when the store cannot be reached,
    or when the only runs are against an older commit. All three are the same
    thing to a gate — no evidence that the checks passed on *this* commit —
    and the gate's own rules decide what that costs.
    """
    try:
        store = _open_store(owner, repo, platform)
    except Exception as exc:  # noqa: BLE001
        logger.debug("Could not open the check store for %s/%s: %s", owner, repo, exc)
        return "not_run"
    try:
        run = store.latest_check_run(pr_number=pr_number, head_sha=head_sha)
    except Exception as exc:  # noqa: BLE001
        logger.debug("Could not read the latest check run for %s/%s: %s", owner, repo, exc)
        return "not_run"
    finally:
        store.close()
    return run.verdict if run else "not_run"
