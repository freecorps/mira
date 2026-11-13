"""One review's verification: a workspace, its tool evidence, and the findings it checks.

The review engine meets this module in four places, and only these:

1. :meth:`VerifySession.maybe_start` right after the repository snapshot starts
   downloading. Preparation — isolation probe, workspace, setup, tool commands —
   runs in the background alongside the walkthrough and context building.
2. :meth:`VerifySession.prompt_block` when a review part writes its prompt. It
   waits for the tools only until ``evidence_wait_seconds`` after the start of
   the review; past that the part goes without the block.
3. :meth:`VerifySession.verify` and :meth:`VerifySession.tool_findings` after
   the self-critique, bounded by ``budget_seconds``.
4. :meth:`VerifySession.aclose` when the review ends, however it ends.

Every blocking step runs on a worker thread and is tracked, because a thread
cannot be cancelled: the workspace is removed only once nothing is running in
it. When the review ends first, removal happens in the background as soon as
the last command reaches its own timeout — the review is not held for it.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Callable
from typing import Any, TypeVar

from mira.models import FindingVerification, ReviewComment
from mira.verify import evidence as evidence_mod
from mira.verify import reproduce
from mira.verify.isolation import Isolation, resolve_isolation
from mira.verify.policy import VerifyPolicy, resolve_verify_policy
from mira.verify.workspace import Workspace, WorkspaceError

logger = logging.getLogger(__name__)

T = TypeVar("T")

# How long the snapshot is waited for, beyond what the review itself waits.
_SNAPSHOT_WAIT_SECONDS = 180.0

# Cleanups still waiting on a command to time out. Held so the event loop does
# not collect the task before it has removed its workspace.
_PENDING_CLEANUPS: set[asyncio.Task] = set()


class VerifySession:
    """Everything verification does for one review."""

    def __init__(
        self,
        policy: VerifyPolicy,
        *,
        source: Any,
        diff_text: str,
        covered_by_checks: set[str] | None = None,
    ) -> None:
        from mira.autofix.finishing import added_lines_by_path

        self.policy = policy
        self._source = source
        self.added_lines = added_lines_by_path(diff_text)
        self.changed_paths = sorted(self.added_lines)
        self.covered_by_checks = {name.lower() for name in covered_by_checks or set()}
        self.started_at = time.monotonic()
        self.isolation: Isolation | None = None
        self.workspace: Workspace | None = None
        self.tree: list[str] = []
        self.evidence = evidence_mod.ToolEvidence()
        self.setup_log: list[dict] = []
        self.unavailable = ""
        self._task: asyncio.Task | None = None
        self._threads: set[asyncio.Task] = set()
        self._harness: dict[str, reproduce.HarnessTrust] = {}
        self._harness_lock = asyncio.Lock()
        self._closed = False

    # ── Lifecycle ───────────────────────────────────────────────────────

    @classmethod
    def maybe_start(
        cls, config: Any, pr_info: Any, source: Any, diff_text: str
    ) -> VerifySession | None:
        """A started session when this repository has verification configured, else None."""
        if pr_info is None or source is None:
            return None
        policy = resolve_verify_policy(config.review.verify, pr_info.owner, pr_info.repo)
        if not policy.active:
            return None
        session = cls(
            policy,
            source=source,
            diff_text=diff_text,
            covered_by_checks=_checks_tools(config, pr_info),
        )
        if not session.changed_paths:
            return None
        session._task = asyncio.create_task(session._prepare())
        return session

    async def _in_thread(self, fn: Callable[[], T]) -> T:
        """Run blocking work on a thread the session can wait for at cleanup."""
        task = asyncio.ensure_future(asyncio.to_thread(fn))
        self._threads.add(task)
        task.add_done_callback(self._threads.discard)
        return await asyncio.shield(task)

    async def _prepare(self) -> Workspace | None:
        """Isolation, workspace, setup, tool evidence. Never raises; None when unavailable."""
        policy = self.policy
        try:
            self.isolation = await self._in_thread(
                lambda: resolve_isolation(policy.isolation, policy.extra_read_only_paths)
            )
            if self.isolation is None:
                self.unavailable = (
                    f"no working sandbox for isolation {policy.isolation!r} on this host "
                    "(install bubblewrap on Linux)"
                )
                logger.warning("Verification skipped: %s", self.unavailable)
                return None
            snapshot = await self._source.snapshot(max_wait=_SNAPSHOT_WAIT_SECONDS)
            if snapshot is None or not getattr(snapshot, "files", None):
                self.unavailable = "the repository snapshot of the head commit is unavailable"
                return None
            files = dict(snapshot.files)
            self.tree = sorted(getattr(snapshot, "paths", None) or files)
            isolation = self.isolation

            def _create() -> Workspace:
                workspace = Workspace.create(
                    files,
                    isolation,
                    max_bytes=policy.max_workspace_mb * 1024 * 1024,
                    memory_limit_mb=policy.memory_limit_mb,
                    cpu_seconds=policy.cpu_seconds,
                )
                # Set from the thread: a review cancelled while this runs must
                # still find the workspace to delete.
                self.workspace = workspace
                return workspace

            workspace = await self._in_thread(_create)
            if self._closed:
                return None
            await self._setup(workspace)
            await self._collect_evidence(workspace)
            return workspace
        except WorkspaceError as exc:
            self.unavailable = str(exc)
            logger.info("Verification workspace not prepared: %s", exc)
            return None
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - verification must never cost the review
            self.unavailable = f"preparation failed: {exc}"[:300]
            logger.warning("Verification preparation failed: %s", exc)
            return None

    async def _setup(self, workspace: Workspace) -> None:
        """The repository's install steps, in order, stopping at the first that fails."""
        deadline = time.monotonic() + self.policy.setup_timeout_seconds
        for step in self.policy.setup:
            remaining = deadline - time.monotonic()
            if remaining < 1 or self._closed:
                self.setup_log.append({"name": step.label, "status": "timeout"})
                return
            timeout = min(step.timeout_seconds or remaining, remaining)
            outcome = await self._in_thread(
                lambda step=step, timeout=timeout: workspace.run(
                    list(step.command),
                    timeout_seconds=timeout,
                    network=self.policy.setup_network,
                    max_output_bytes=20_000,
                )
            )
            ok = outcome.status == "ok" and outcome.exit_code == 0
            entry = {
                "name": step.label,
                "status": "ok" if ok else outcome.status,
                "exit_code": outcome.exit_code,
                "seconds": round(outcome.duration_seconds, 2),
            }
            self.setup_log.append(entry)
            if not ok:
                logger.info(
                    "Verification setup step %s failed (%s, exit %s)",
                    step.label,
                    outcome.status,
                    outcome.exit_code,
                )
                return

    def _related_tests(self) -> list[str]:
        from mira.autofix.finishing import detect_test_layout, related_tests
        from mira.checks.native import paths as path_kinds

        layout = detect_test_layout(self.tree)
        found: list[str] = []
        for path in self.changed_paths:
            if path_kinds.is_test(path):
                found.append(path)
            else:
                found.extend(related_tests(path, layout, limit=2))
        return list(dict.fromkeys(found))

    async def _collect_evidence(self, workspace: Workspace) -> None:
        if not self.policy.commands:
            return
        present = [p for p in self.changed_paths if workspace.exists(p)]
        tests = [p for p in self._related_tests() if workspace.exists(p)]

        async def _one(command: Any) -> evidence_mod.CommandEvidence:
            timeout = command.timeout_seconds or self.policy.command_timeout_seconds
            return await self._in_thread(
                lambda: evidence_mod.run_command(
                    workspace,
                    command,
                    changed=present,
                    added_lines=self.added_lines,
                    related_tests=tests,
                    timeout_seconds=timeout,
                )
            )

        results = await asyncio.gather(*(_one(c) for c in self.policy.commands))
        self.evidence = evidence_mod.ToolEvidence(commands=list(results))

    async def _ready(self, deadline: float) -> Workspace | None:
        """The prepared workspace, waiting for it no later than ``deadline``."""
        task = self._task
        if task is None:
            return None
        remaining = deadline - time.monotonic()
        if not task.done():
            if remaining <= 0:
                return None
            try:
                await asyncio.wait_for(asyncio.shield(task), remaining)
            except TimeoutError:
                return None
        if task.cancelled() or task.exception() is not None:
            return None
        return task.result()

    async def aclose(self) -> None:
        """End the session: stop preparing, and delete the workspace once nothing runs in it."""
        self._closed = True
        task, self._task = self._task, None
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        running = set(self._threads)

        async def _cleanup() -> None:
            if running:
                await asyncio.gather(*running, return_exceptions=True)
            workspace, self.workspace = self.workspace, None
            if workspace is not None:
                await asyncio.to_thread(workspace.cleanup)

        if not running:
            await _cleanup()
            return
        cleanup = asyncio.create_task(_cleanup())
        _PENDING_CLEANUPS.add(cleanup)
        cleanup.add_done_callback(_PENDING_CLEANUPS.discard)

    # ── Tool evidence ───────────────────────────────────────────────────

    async def prompt_block(self, paths: list[str]) -> str:
        """The "Tool output on changed lines" block for one review part, or ""."""
        if not self.policy.commands:
            return ""
        deadline = self.started_at + self.policy.evidence_wait_seconds
        if await self._ready(deadline) is None:
            return ""
        return self.evidence.render(set(paths), self.policy.max_evidence_chars)

    def tool_findings(self, comments: list[ReviewComment]) -> list[ReviewComment]:
        """Diagnostics on changed lines as findings, for tools nothing else posts."""
        if self.policy.max_tool_findings <= 0:
            return []
        return evidence_mod.diagnostics_to_findings(
            self.evidence,
            covered_by_checks=self.covered_by_checks,
            existing=comments,
            limit=self.policy.max_tool_findings,
        )

    # ── Finding verification ────────────────────────────────────────────

    async def _harness_trust(self, runner: Any, like: str, deadline: float) -> Any:
        changed = reproduce.harness_changes(self.changed_paths)
        if changed:
            return reproduce.HarnessTrust(
                False, f"this pull request changes the test harness ({', '.join(changed[:3])})"
            )
        async with self._harness_lock:
            cached = self._harness.get(runner.label)
            if cached is None:
                workspace = self.workspace
                if workspace is None:
                    return reproduce.HarnessTrust(False, "the workspace is gone")
                cached = await self._in_thread(
                    lambda: reproduce.check_canary(
                        workspace,
                        runner,
                        like=like,
                        timeout_seconds=self.policy.test_timeout_seconds,
                        deadline=deadline,
                    )
                )
                self._harness[runner.label] = cached
            return cached

    async def verify(
        self, comments: list[ReviewComment], llm: Any, audit: list[dict] | None = None
    ) -> list[ReviewComment]:
        """Run reproduction tests for the strongest findings and apply what they show.

        Returns the comments to keep, in order. Never raises: a verification
        that could not happen leaves every comment as it was.
        """
        policy = self.policy
        if not policy.reproducing or not comments:
            self._audit_summary(audit, attempted=[])
            return comments
        chosen = reproduce.select(comments, policy)
        if not chosen:
            self._audit_summary(audit, attempted=[])
            return comments
        deadline = time.monotonic() + policy.budget_seconds
        workspace = await self._ready(deadline)
        attempts = [
            reproduce.Attempt(comment=c, runner=policy.runner_for(c.path))  # type: ignore[arg-type]
            for c in chosen
        ]
        if workspace is None:
            reason = self.unavailable or "the workspace was not ready within the budget"
            for attempt in attempts:
                attempt.verification = FindingVerification(
                    outcome="inconclusive", reason=reason, runner=attempt.runner.label
                )
        else:
            await self._run_attempts(attempts, workspace, llm, deadline)

        by_id = {id(a.comment): a for a in attempts}
        kept: list[ReviewComment] = []
        for comment in comments:
            attempt = by_id.get(id(comment))
            if attempt is None or attempt.verification is None:
                kept.append(comment)
                continue
            keep = reproduce.apply_outcome(comment, attempt.verification)
            if audit is not None:
                audit.append(_audit_entry(comment, attempt, dropped=not keep))
            if keep:
                kept.append(comment)
            else:
                logger.info(
                    "Verification dropped %s:%d — plausible, and its test passed",
                    comment.path,
                    comment.line,
                )
        self._audit_summary(audit, attempted=attempts)
        return kept

    async def _run_attempts(
        self,
        attempts: list[reproduce.Attempt],
        workspace: Workspace,
        llm: Any,
        deadline: float,
    ) -> None:
        from mira.autofix.finishing import detect_test_layout

        layout = detect_test_layout(self.tree)
        semaphore = asyncio.Semaphore(self.policy.max_concurrent)

        async def _one(attempt: reproduce.Attempt) -> None:
            async with semaphore:
                try:
                    attempt.verification = await reproduce.verify_one(
                        attempt,
                        llm=llm,
                        workspace=workspace,
                        layout=layout,
                        policy=self.policy,
                        deadline=deadline,
                        harness=lambda like, runner=attempt.runner: self._harness_trust(
                            runner, like, deadline
                        ),
                    )
                except Exception as exc:  # noqa: BLE001 - one broken attempt costs itself
                    logger.warning("Verification of %s failed: %s", attempt.comment.path, exc)
                    attempt.verification = FindingVerification(
                        outcome="inconclusive",
                        reason="verification failed",
                        runner=attempt.runner.label,
                    )

        # The per-step deadlines bound every await below; this is the backstop
        # for a model call or a thread that somehow outlives them.
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(
                asyncio.gather(*(_one(a) for a in attempts)),
                max(1.0, deadline - time.monotonic()) + 30,
            )
        for attempt in attempts:
            if attempt.verification is None:
                attempt.verification = FindingVerification(
                    outcome="inconclusive",
                    reason="the verification budget ran out",
                    runner=attempt.runner.label,
                )

    def _audit_summary(self, audit: list[dict] | None, *, attempted: list) -> None:
        if audit is None:
            return
        outcomes: dict[str, int] = {}
        for attempt in attempted:
            if attempt.verification is not None:
                key = attempt.verification.outcome
                outcomes[key] = outcomes.get(key, 0) + 1
        audit.append(
            {
                "stage": "verify_summary",
                "isolation": self.isolation.mode if self.isolation else "",
                "unavailable": self.unavailable,
                "setup": self.setup_log,
                "tools": evidence_mod.summarise(self.evidence),
                "attempted": len(attempted),
                "outcomes": outcomes,
                "seconds": round(time.monotonic() - self.started_at, 1),
            }
        )


def _audit_entry(comment: ReviewComment, attempt: reproduce.Attempt, *, dropped: bool) -> dict:
    v = attempt.verification
    assert v is not None
    return {
        "stage": "verify",
        "path": comment.path,
        "line": comment.line,
        "title": comment.title,
        "severity": comment.severity.name,
        "category": comment.category,
        "confidence": comment.confidence,
        "outcome": v.outcome,
        "reason": f"{v.outcome}: {v.reason}" if dropped else v.reason,
        "dropped": dropped,
        "critique_grade": comment.critique_grade,
        "original_severity": v.original_severity,
        "runner": v.runner,
        "seconds": v.duration_seconds,
    }


def _checks_tools(config: Any, pr_info: Any) -> set[str]:
    """Analysers the pre-merge checks run (and post) for this repository."""
    try:
        from mira.checks.policy import resolve_policy as resolve_checks_policy

        checks = resolve_checks_policy(config.checks, pr_info.owner, pr_info.repo)
    except Exception:  # noqa: BLE001 - no checks policy is no checks
        return set()
    if not checks.active:
        return set()
    return {tool.name for tool in checks.tools if tool.enabled}
