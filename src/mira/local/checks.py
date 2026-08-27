"""Running the pre-merge checks against a local change.

The same framework the server runs, with three differences, each of which is a
consequence of there being no pull request rather than a different opinion
about the code.

**Nothing is recorded.** :func:`mira.checks.service.evaluate` gathers, runs,
*persists* and announces. Here only the first two happen: the run is built with
:func:`mira.checks.service.gather_context` and executed by the pure
:func:`mira.checks.runner.run_checks`, and the result is returned rather than
written. A developer running this in a save-loop must not fill the repository's
check history with runs against commits that do not exist, and the merge gate
must never read a local run as evidence about a real pull request.

**Files come from disk, not from the platform.** With no provider, the shared
context's file reader returns ``""`` for everything — which for a linter means
being handed empty files and reporting nothing, and "nothing" from a linter
reads as a pass. So the context is given a reader that resolves content from
the work tree, the index or a commit, matching whichever mode is being
reviewed.

**Checks that are about a pull request are switched off rather than answered.**
``native.title_description`` asks whether the description explains the change;
locally there is no description, and a check that objected to its absence would
fail every local run for a fact that is not about the code. The same goes for
the ticket, acceptance-criteria and CI context checks, which need the platform.
They are forced to ``off``, which the framework records as skipped — never as a
pass — and the reason is reported.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import replace
from pathlib import Path
from typing import Any

from mira.checks.context import MAX_FILE_BYTES
from mira.checks.models import CheckRun
from mira.checks.policy import EffectiveChecksPolicy, resolve_policy
from mira.checks.registry import (
    CONTEXT_ACCEPTANCE_CRITERIA,
    CONTEXT_CI,
    CONTEXT_TICKET,
    NATIVE_TITLE_DESCRIPTION,
)
from mira.checks.runner import run_checks
from mira.checks.service import (
    ChecksUnavailable,
    ReviewSignal,
    failed_run,
    gather_context,
)
from mira.config import MiraConfig
from mira.local.gitcmd import GitError, run_git
from mira.local.repo import MODE_RANGE, MODE_STAGED, LocalDiff, RepoIdentity
from mira.models import PRInfo

logger = logging.getLogger(__name__)

#: Checks whose subject is the pull request, not the change. Forced off for a
#: local run, because the honest local answer to "does the description explain
#: this?" is that there is no description to read.
PULL_REQUEST_ONLY_CHECKS: tuple[str, ...] = (
    NATIVE_TITLE_DESCRIPTION,
    CONTEXT_TICKET,
    CONTEXT_ACCEPTANCE_CRITERIA,
    CONTEXT_CI,
)


def local_policy(config: MiraConfig, identity: RepoIdentity) -> EffectiveChecksPolicy:
    """The repository's own check policy, narrowed for a run with no pull request."""
    policy = resolve_policy(config.checks, identity.owner, identity.repo)
    if not policy.active:
        return policy
    forced = tuple((check_id, "off") for check_id in PULL_REQUEST_ONLY_CHECKS)
    # Appended, so `dict(modes)` resolves these last and they win over an
    # operator's own entry. That is deliberate: a repository that set
    # `native.title_description: error` is describing pull requests, and a local
    # run cannot satisfy it however it is configured.
    return replace(
        policy,
        modes=(*policy.modes, *forced),
        # A local run announces nothing anywhere, and the two flags below are
        # the only things in the policy that could reach a platform.
        publish_status=False,
        comment=False,
    )


def _read_worktree_file(repo_root: Path, path: str) -> str:
    """Read a file from the work tree, or return "" — never following a link out.

    Two things this must not do, and one it cannot fully do.

    **It must not read outside the repository.** Git tracks symlinks, so a
    branch can add ``leak -> /home/you/.ssh/id_rsa`` and it arrives here as an
    ordinary changed path. ``Path.is_file()`` follows links, so the naive read
    hands a host secret to whatever analyser or model the checks are
    configured with. The candidate is therefore resolved and required to stay
    beneath the resolved repository root, and a symlink is refused outright:
    what git stores for one is the target *path*, not the target's contents, so
    reading through it would misreport the change even when it stays inside.

    **It must not read an unbounded amount.** The shared check context caps a
    file body at ``MAX_FILE_BYTES``, but it caps what the reader returned — so
    a reader that slurped a 400 MB file first has already spent the memory. One
    byte past the cap is read, so the caller's truncation still sees an
    over-long body and behaves exactly as it would with the whole thing.

    What it cannot close is the window between the checks and the open. That is
    a race an attacker would have to win on the developer's own machine, while
    the developer runs a review of the tree that attacker already controls; the
    containment check is what keeps a *committed* symlink from working, which
    is the reachable half.
    """
    root = repo_root.resolve()
    target = root / path
    if target.is_symlink():
        return ""
    try:
        resolved = target.resolve(strict=True)
        resolved.relative_to(root)
    except (OSError, ValueError):
        return ""
    if not resolved.is_file():
        return ""
    with open(resolved, "rb") as handle:
        raw = handle.read(MAX_FILE_BYTES + 1)
    return raw.decode("utf-8", errors="replace")


def content_reader_for(repo_root: Path, diff: LocalDiff) -> Callable[[str], Awaitable[str]]:
    """A reader returning each path's content *as the reviewed change leaves it*.

    Which is a different source in each mode, and getting it wrong would have a
    check quoting a version of the file nobody is proposing:

    * a working-tree review reads the file on disk, including edits that are
      not staged;
    * a staged review reads the index — ``git show :path`` — because the point
      of reviewing the index is to see what a commit would contain;
    * a range review reads the range's head commit.

    The two git-backed modes are bounded by ``max_output_bytes`` for the same
    reason the work-tree read is: a blob is whatever somebody committed.
    """
    mode = diff.mode
    head = diff.head_sha

    async def read(path: str) -> str:
        try:
            if mode == MODE_RANGE:
                spec = f"{head}:{path}"
            elif mode == MODE_STAGED:
                spec = f":{path}"
            else:
                return _read_worktree_file(repo_root, path)
            result = run_git(repo_root, "show", spec, max_output_bytes=MAX_FILE_BYTES + 1)
            return result.stdout if result.ok else ""
        except (GitError, OSError) as exc:
            logger.debug("Local check could not read %s: %s", path, exc)
            return ""

    return read


def local_pr_info(identity: RepoIdentity, diff: LocalDiff) -> PRInfo:
    """A ``PRInfo`` describing the local change, with nothing invented.

    ``number`` is zero and the URL names the comparison rather than a web page,
    so anything that renders this cannot present a local run as a pull request.
    The title and description are empty on purpose: the checks that would read
    them are switched off, and supplying a plausible-looking stand-in would
    make them answer about a commit message instead.
    """
    slug = identity.slug or identity.root.name
    return PRInfo(
        title="",
        description="",
        base_branch=diff.base_label,
        head_branch=identity.branch or diff.head_label,
        url=f"local:{slug}#{diff.comparison}",
        number=0,
        owner=identity.owner,
        repo=identity.repo,
        base_sha=diff.base_sha,
        head_sha=diff.head_sha,
        platform=identity.platform,
    )


def _unstarted(pr_info: Any, policy: EffectiveChecksPolicy | None, message: str) -> CheckRun:
    """A run that records why the checks never started.

    Built with the framework's own helper so the shape is the one the dashboard
    and the gate already understand: no results, an error, and therefore the
    verdict ``incomplete`` rather than ``not_run``. The distinction is the whole
    point — ``not_run`` means nothing was asked, and something was.

    ``policy`` may be None when the failure happened while resolving it; an
    empty policy is then recorded, which is honest about what was in force.
    """
    return failed_run(
        pr_info
        or PRInfo(
            title="",
            description="",
            base_branch="",
            head_branch="",
            url="",
            number=0,
            owner="",
            repo="",
        ),
        policy or EffectiveChecksPolicy(),
        message,
    )


async def run_local_checks(
    *,
    config: MiraConfig,
    identity: RepoIdentity,
    diff: LocalDiff,
) -> tuple[CheckRun | None, str]:
    """Run the checks for a local change. Returns ``(run, note)``; never raises.

    ``run`` is None only when the repository has checks switched off — the same
    "nothing was asked, so nothing is owed" the server applies. Every other
    outcome, *including a run that could not start*, returns a ``CheckRun``:
    returning None for a failure would drop the run out of the exit decision
    entirely, and ``--fail-on-incomplete-checks`` would then pass precisely
    when the checks were least able to answer. A failed run carries its error
    and reports the verdict ``incomplete``, which is what it is.

    ``note`` is a sentence for the report when something is worth saying.

    Policy resolution is inside the guarded section rather than in front of it,
    so this function's promise not to raise covers the whole of it: a
    configuration bug in the policy layer must not take down a review that has
    already completed.
    """
    pr_info = None
    policy = None
    try:
        policy = local_policy(config, identity)
        if not policy.active:
            return None, ""

        pr_info = local_pr_info(identity, diff)
        ctx, inputs = await gather_context(
            None,
            pr_info,
            policy,
            config=config,
            signal=ReviewSignal(diff_text=diff.diff_text),
        )
    except ChecksUnavailable as exc:
        return _unstarted(pr_info, policy, str(exc)), f"Pre-merge checks did not run: {exc}"
    except Exception as exc:  # noqa: BLE001 - a local run never fails the review
        logger.warning("Local checks could not start: %s", exc)
        message = f"{type(exc).__name__}: {exc}"
        return _unstarted(pr_info, policy, message), f"Pre-merge checks did not run: {message}"

    ctx.content_reader = content_reader_for(identity.root, diff)

    try:
        run = await run_checks(ctx, inputs)
    except Exception as exc:  # noqa: BLE001 - same reasoning as the server's
        logger.warning("Local checks failed: %s", exc)
        message = f"{type(exc).__name__}: {exc}"
        return _unstarted(pr_info, policy, message), f"Pre-merge checks failed: {message}"

    note = (
        "Checks about the pull request itself ("
        + ", ".join(PULL_REQUEST_ONLY_CHECKS)
        + ") were switched off: a local review has no pull request to read."
    )
    return run, note
