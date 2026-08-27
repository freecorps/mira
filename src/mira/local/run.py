"""Wiring a local review: same engine, same configuration, no side effects.

This module is deliberately thin. Everything that decides what a finding is
lives in :mod:`mira.core.engine`; everything that decides whether a check
objects lives in :mod:`mira.checks`. What is decided here is the small set of
questions that only exist locally — which repository, which diff, where the
code is allowed to go, and what the process should exit with.

Two constraints shape it.

**Nothing is constructed that could write.** No provider is built, so there is
no client that could post a comment, submit a verdict or publish a status. The
engine is additionally run with ``dry_run=True``: the flag is redundant given a
null provider, and it is set anyway, because a future change that gives the
local surface a provider for reading should not silently acquire the ability to
write.

**A run that could not happen says so.** An unreachable model endpoint, an
unreadable repository and a clean tree are three different outcomes with three
different exit codes, and none of them is "no issues found".
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from mira.checks.models import CheckRun
from mira.config import MiraConfig
from mira.core.diff_parser import parse_diff
from mira.exceptions import MiraError
from mira.local.checks import local_pr_info, run_local_checks
from mira.local.exit_codes import ExitCode
from mira.local.gitcmd import GitError, find_repo_root, run_git
from mira.local.guard import (
    Destination,
    DestinationRefused,
    apply_deployment_defaults,
    check_destinations,
    load_repo_config,
    repo_config_path,
)
from mira.local.repo import (
    MODE_RANGE,
    MODE_WORKING_TREE,
    ChangedEntry,
    LocalDiff,
    RepoIdentity,
    identify_repo,
    resolve_diff,
)
from mira.models import ReviewResult, Severity

logger = logging.getLogger(__name__)

#: ``--fail-on`` accepts a severity name or the word below, which means the
#: exit code never reports findings. Useful for a hook that wants the report
#: and lets the human decide.
FAIL_ON_NEVER = "never"


class LocalReviewError(Exception):
    """Something stopped the review. Carries the exit code it should produce."""

    def __init__(self, message: str, code: ExitCode) -> None:
        super().__init__(message)
        self.code = code


@dataclass
class LocalReview:
    """Everything one local review produced, before it is rendered."""

    identity: RepoIdentity
    diff: LocalDiff
    config: MiraConfig
    destinations: list[Destination] = field(default_factory=list)
    result: ReviewResult | None = None
    checks: CheckRun | None = None
    notes: list[str] = field(default_factory=list)
    fail_on: str = Severity.BLOCKER.name.lower()
    fail_on_incomplete_checks: bool = False

    @property
    def comments(self) -> list:
        return list(self.result.comments) if self.result else []

    def counts(self) -> dict[str, int]:
        tally = {severity.name.lower(): 0 for severity in Severity}
        for comment in self.comments:
            tally[comment.severity.name.lower()] += 1
        tally["total"] = len(self.comments)
        return tally

    @property
    def blocking_check_violations(self) -> list:
        if self.checks is None:
            return []
        return [result for result in self.checks.blocking_results if result.is_violation]

    @property
    def unanswered_checks(self) -> list[str]:
        """Every way this run failed to produce an answer it was asked for.

        Two of them, and the second is the one that is easy to lose. A blocking
        check that ran and could not conclude is the obvious case. A run that
        never *started* — an unreadable diff, a broken policy — is the other,
        and it has no results at all, so a check that only walked
        ``blocking_results`` would report an empty list and let the strictest
        flag in the tool pass precisely when the checks were least able to
        answer.
        """
        if self.checks is None:
            return []
        reasons = [
            f"{result.check_id}: {result.state}"
            for result in self.checks.blocking_results
            if result.incomplete
        ]
        if self.checks.error:
            reasons.append(f"the run did not start: {self.checks.error}")
        return reasons

    def exit_code(self) -> ExitCode:
        """What the process should exit with.

        Only findings and check *violations* produce
        :data:`~mira.local.exit_codes.ExitCode.FINDINGS`. A check that could not
        answer does not, unless the caller asked for it: locally, the usual
        reason a check cannot answer is that the deployment's analyser is not
        installed on this machine, and failing a developer's pre-commit hook for
        that would teach them to pass ``--no-verify``. The merge gate, which is
        the thing that actually protects the branch, still fails closed on the
        same condition — that is its job and this is not it.
        """
        if self.fail_on != FAIL_ON_NEVER:
            threshold = Severity.from_str(self.fail_on)
            if any(comment.severity >= threshold for comment in self.comments):
                return ExitCode.FINDINGS
        if self.blocking_check_violations:
            return ExitCode.FINDINGS
        if self.fail_on_incomplete_checks and self.unanswered_checks:
            return ExitCode.FINDINGS
        return ExitCode.OK


def _commit_subject(repo_root: Path, sha: str) -> str:
    """The head commit's subject line, used as the review's title.

    The review prompt takes a title, and on a pull request it is the one the
    author wrote. A commit range has the same thing available; the working tree
    does not, and passes nothing rather than something invented.
    """
    if not sha:
        return ""
    result = run_git(repo_root, "show", "-s", "--format=%s", sha)
    return result.stdout.strip() if result.ok else ""


def _annotate_entries(diff: LocalDiff, result: ReviewResult | None) -> None:
    """Record what the review actually looked at, per changed path.

    A report that lists what changed without saying which parts were read is the
    kind of output that gets trusted for the wrong reason — "Mira found nothing
    in the lockfile" is only meaningful if Mira read the lockfile.
    """
    binary: set[str] = set()
    try:
        for file_diff in parse_diff(diff.diff_text).files:
            if file_diff.is_binary:
                binary.add(file_diff.path)
    except Exception as exc:  # noqa: BLE001 - annotation is never worth failing over
        logger.debug("Could not classify binary files in the local diff: %s", exc)

    reviewed = set(result.reviewed_paths) if result else set()
    skipped = set(result.skipped_paths) if result else set()

    annotated: list[ChangedEntry] = []
    for entry in diff.entries:
        reason = entry.excluded_reason
        if entry.submodule:
            reason = reason or "submodule pointer, not code"
        elif entry.path in binary:
            reason = reason or "binary file"
        elif entry.path in skipped:
            reason = reason or "skipped: size or priority budget"
        elif result is not None and entry.path not in reviewed:
            reason = reason or "excluded by the repository's file filters"
        annotated.append(
            replace(
                entry,
                binary=entry.path in binary,
                reviewed=entry.path in reviewed,
                excluded_reason="" if entry.path in reviewed else reason,
            )
        )
    diff.entries = annotated


def prepare(
    *,
    path: str | Path,
    mode: str = MODE_WORKING_TREE,
    range_spec: str = "",
    include_untracked: bool = False,
    deployment_config: str | Path | None = None,
    overrides: dict[str, Any] | None = None,
    remote: str = "",
    stated_slug: str = "",
    stated_platform: str = "",
) -> LocalReview:
    """Resolve the repository, the configuration, the diff and the destination.

    Everything before a single byte of source is *sent* anywhere. The diff is
    resolved before the destination is checked, and in that order on purpose:
    the trusted destination lives in ``.mira.yaml`` as committed at the base of
    the review, so the guard needs the base the diff resolution worked out.
    Reading a diff into this process sends nothing; the guard still runs before
    a client is constructed.

    Raises :class:`LocalReviewError` carrying the exit code the failure
    deserves.
    """
    try:
        repo_root = find_repo_root(path)
    except GitError as exc:
        raise LocalReviewError(str(exc), ExitCode.GIT) from exc

    if deployment_config:
        try:
            apply_deployment_defaults(deployment_config)
        except Exception as exc:  # noqa: BLE001 - surfaced as a config failure
            raise LocalReviewError(f"--config could not be loaded: {exc}", ExitCode.CONFIG) from exc

    try:
        config = load_repo_config(repo_root, overrides)
    except MiraError as exc:
        raise LocalReviewError(str(exc), ExitCode.CONFIG) from exc

    try:
        identity = identify_repo(
            repo_root,
            fallback_platform=config.provider.type,
            remote=remote,
            stated_slug=stated_slug,
            stated_platform=stated_platform,
        )
    except GitError as exc:
        raise LocalReviewError(str(exc), ExitCode.GIT) from exc

    try:
        diff = resolve_diff(
            repo_root,
            mode=mode,
            range_spec=range_spec,
            include_untracked=include_untracked,
        )
    except ValueError as exc:
        raise LocalReviewError(str(exc), ExitCode.USAGE) from exc
    except GitError as exc:
        raise LocalReviewError(str(exc), ExitCode.GIT) from exc

    try:
        destinations = check_destinations(repo_root, effective=config, base_rev=diff.base_sha)
    except DestinationRefused as exc:
        raise LocalReviewError(str(exc), ExitCode.CONFIG) from exc
    except MiraError as exc:
        # An unusable baseline is not a reason to fall back to the working
        # tree's answer: with no trusted destination there is nothing to
        # compare against, and the whole point is to not send the code then.
        raise LocalReviewError(
            f"The destination configured at the base of this review could not be read: {exc}",
            ExitCode.CONFIG,
        ) from exc

    review = LocalReview(identity=identity, diff=diff, config=config, destinations=destinations)
    review.notes.extend(diff.notes)
    if not identity.known:
        review.notes.append(
            "This checkout has no remote Mira could name, so no indexed context, "
            "no learned rules and no per-repository policy were applied. "
            "Pass --repo owner/repo to point at them."
        )
    if repo_config_path(repo_root) is None:
        review.notes.append(
            "This repository has no .mira.yaml, so the review ran under the deployment's defaults."
        )
    return review


async def execute(review: LocalReview, *, run_checks_too: bool = True) -> LocalReview:
    """Run the review (and, when the repository asks for them, the checks).

    Raises :class:`LocalReviewError` with
    :data:`~mira.local.exit_codes.ExitCode.ENGINE` when the review could not
    complete — an unreachable model endpoint is the common case, and reporting
    it as "no issues found" would be the worst possible lie for this tool to
    tell.
    """
    if review.diff.is_empty:
        review.notes.append("There was nothing to review in this comparison.")
        return review

    from mira.dashboard.models_config import llm_config_for
    from mira.llm import create_llm

    config = review.config
    try:
        llm = create_llm(llm_config_for("review", config.llm))
        indexing_llm = create_llm(llm_config_for("indexing", config.llm))
        security_llm = create_llm(llm_config_for("security", config.llm))
    except MiraError as exc:
        raise LocalReviewError(str(exc), ExitCode.CONFIG) from exc

    from mira.core.engine import ReviewEngine

    engine = ReviewEngine(
        config=config,
        llm=llm,
        # No provider at all. There is no client here that could write to a
        # forge, which is a stronger statement than "we did not call one".
        provider=None,
        dry_run=True,
        indexing_llm=indexing_llm,
        security_llm=security_llm,
    )

    scope = local_pr_info(review.identity, review.diff) if review.identity.known else None
    title = (
        _commit_subject(review.identity.root, review.diff.head_sha)
        if review.diff.mode == MODE_RANGE
        else ""
    )

    try:
        review.result = await engine.review_diff(
            review.diff.diff_text, repo_scope=scope, title=title
        )
    except MiraError as exc:
        raise LocalReviewError(
            f"The review could not complete: {exc.safe_message}", ExitCode.ENGINE
        ) from exc
    except Exception as exc:  # noqa: BLE001 - every failure is a non-answer
        raise LocalReviewError(
            f"The review could not complete: {type(exc).__name__}: {exc}", ExitCode.ENGINE
        ) from exc

    if scope is not None and getattr(engine, "_index_was_empty", False):
        review.notes.append(
            f"{review.identity.slug} has no index on this machine, so the review "
            "ran without repository context. Index it from the dashboard for "
            "the same context the server has."
        )

    if run_checks_too:
        run, note = await run_local_checks(
            config=config, identity=review.identity, diff=review.diff
        )
        review.checks = run
        if note:
            review.notes.append(note)

    _annotate_entries(review.diff, review.result)
    return review
