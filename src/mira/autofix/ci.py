"""Watching what CI says about a fix, and answering it at most N times.

A fix that Mira validated and published still has to survive the repository's
own build, and the build is the only reviewer here that runs the real test
suite. So a red CI run on a fix's pull request is the signal worth acting on —
and the one signal most likely to turn into an expensive loop if nobody bounds
it.

Two things keep it bounded.

**The count lives on the job row.** ``ci_attempts`` against ``max_ci_attempts``
survives a restart, a redelivered webhook and a second worker. A limit held in
memory would reset on every deploy, which is the same as having no limit on the
install where it matters.

**The default is one.** One regeneration, then the job stops with a reason
saying a human should look. A fix that CI rejects twice is not a fix that needs
a third model call.

Discovered by sweeping rather than only by webhook. GitHub reports check
completions; GitLab and Forgejo report pipelines and statuses through events
Mira does not currently subscribe to. A sweep over the small number of
published jobs asks all three the same question through the same
``get_ci_state`` the merge gate already uses, so the retry loop behaves
identically on every platform instead of working properly on one.
"""

from __future__ import annotations

import logging
from typing import Any

from mira.autofix.models import AutofixJob
from mira.autofix.policy import resolve_policy
from mira.autofix.worker import retry_after_ci
from mira.config import MiraConfig, load_config
from mira.gate.models import CIState

logger = logging.getLogger(__name__)

# Published jobs examined per sweep. A cap, not a page: the sweep comes back
# around, and a repository with a hundred open fixes has a bigger problem than
# a slow retry.
_SWEEP_LIMIT = 50


def _summarize(state: CIState) -> str:
    """What to tell the model, in one short paragraph of untrusted data.

    Names only. A CI *log* is the most attacker-reachable text in the whole
    pipeline — anybody who can open a pull request can print anything they like
    into it — and there is nothing in a log body that the next generation
    attempt needs that the failing check's name does not already say.
    """
    failing = ", ".join(sorted(state.failing)[:20]) or "unnamed check(s)"
    return (
        f"The fix's own pull request has {len(state.failing)} failing check(s): {failing}. "
        f"{state.total} check(s) ran in total."
    )


def ci_rejected(job: AutofixJob, state: CIState) -> bool:
    """Whether CI has actually turned this published fix down.

    Only an outright failure counts. `pending` means CI has not finished and
    the next sweep will ask again; `unknown` means nobody could say, and
    regenerating a perfectly good fix because a status endpoint blinked is a
    worse failure than waiting.

    Deliberately says nothing about the retry budget. Whether there is one left
    is a different question, and answering both here is how "we stopped
    retrying" turns into a job that looks like CI passed.
    """
    if job.state != "opened" or not job.child_pr_number:
        return False
    return state.state == "failure" and bool(state.failing)


def needs_retry(job: AutofixJob, state: CIState) -> bool:
    """Whether this job should be regenerated *now*: red CI, and budget left."""
    return ci_rejected(job, state) and job.ci_attempts < job.max_ci_attempts


async def recheck_job(
    provider: Any,
    job: AutofixJob,
    *,
    config: MiraConfig | None = None,
) -> AutofixJob:
    """Read the fix pull request's CI and act on what it says.

    A red run with budget left is re-queued. A red run with the budget spent is
    *recorded* as such — the job stops, and it says on its own row that CI
    rejected it and that a human should look. Stopping silently would leave an
    `opened` job that reads exactly like one CI was happy with.
    """
    config = config or load_config()
    child = _child_pr_info(job)
    try:
        state = await provider.get_ci_state(child)
    except Exception as exc:  # noqa: BLE001 - an unreadable CI is not a red CI
        logger.debug("Could not read CI for autofix job %s: %s", job.job_key, exc)
        return job
    if not ci_rejected(job, state):
        return job
    if job.ci_attempts >= job.max_ci_attempts:
        logger.info(
            "CI rejected autofix %s and its retry budget is spent (%d)",
            job.job_key,
            job.max_ci_attempts,
        )
    else:
        logger.info(
            "CI rejected autofix %s (%s); regenerating",
            job.job_key,
            ", ".join(sorted(state.failing)),
        )
    # `retry_after_ci` owns both outcomes, so the budget is checked in exactly
    # one place and the two paths cannot drift apart.
    return await retry_after_ci(job, ci_summary=_summarize(state), config=config)


def _child_pr_info(job: AutofixJob) -> Any:
    """A PRInfo pointing at the *fix's* pull request, not the original.

    Built rather than fetched: `get_ci_state` needs the owner, repo, number and
    head sha, and fetching the whole pull request to read four fields would put
    an API call in a sweep that runs on a schedule.
    """
    from mira.models import PRInfo

    return PRInfo(
        title="",
        description="",
        base_branch=job.head_branch,
        head_branch=job.branch_name,
        url=job.child_pr_url,
        number=job.child_pr_number,
        owner=job.owner,
        repo=job.repo,
        head_sha=job.commit_sha,
        platform=job.platform,
    )


async def sweep(
    *,
    provider_factory: Any,
    store: Any,
    config: MiraConfig | None = None,
) -> int:
    """Check every published fix whose retry budget is not spent.

    Returns how many jobs were re-queued. Cheap when there is nothing to do:
    the filter is a single indexed query, and a store with no published jobs
    makes no platform calls at all.
    """
    config = config or load_config()
    jobs = store.list_autofix_jobs({"state": "opened"}, limit=_SWEEP_LIMIT)
    requeued = 0
    for job in jobs:
        if not job.child_pr_number or job.ci_attempts >= job.max_ci_attempts:
            continue
        if not resolve_policy(config.autofix, job.owner, job.repo).writing:
            # The repository stopped allowing writes since this fix landed.
            # Regenerating would produce a patch there is nowhere to put.
            continue
        try:
            provider = provider_factory(job)
            if hasattr(provider, "__await__"):
                provider = await provider
            updated = await recheck_job(provider, job, config=config)
        except Exception as exc:  # noqa: BLE001 - one bad job is not the sweep
            logger.debug("Autofix CI sweep failed on %s: %s", job.job_key, exc)
            continue
        if updated.state == "queued":
            requeued += 1
    return requeued
