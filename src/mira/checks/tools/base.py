"""Driving a deterministic analyser, and refusing to guess when it is absent.

Every adapter in this package is the same shape: name the binary, build an
argument list, run it over the changed files in a scratch directory, and turn
its output into findings that point at real lines. What varies is the argv and
the parser; what must not vary is the safety story, so that lives here.

**Nothing from a pull request reaches an argument vector.** The tool name comes
from a closed allowlist checked at config load. Extra arguments come from
deployment configuration as a list. The only values derived from the repository
are *file paths that the diff already contains*, and they are written into a
scratch directory whose root is the working directory, so a path that tried to
escape it never gets materialised in the first place.

**A tool Mira did not run is never a pass.** A missing binary is ``skipped``
with the binary named. A version that does not match the operator's pin is
``skipped`` with both versions named. A crash or a timeout is
``infrastructure_error`` or ``timeout``. Only a tool that ran to completion and
reported findings produces a ``violation``, and only one that ran to completion
and reported none produces a ``pass``.

**The tool's own configuration is the repository's, and only if asked for.** An
adapter passes ``--config <path>`` only when the operator named one, and the
path is validated as repository-relative at config load. A repository cannot
smuggle a rule file into the run by committing one at a name the adapter
happens to look for, because no adapter looks for one.

The scratch directory holds the changed files at the head commit and nothing
else. That is enough for a linter with a config file and is not a checkout: a
rule needing cross-file resolution will find fewer things here than it would in
CI. The honest statement of that limit is in ``docs/pre-merge-checks.md``,
because a check that silently under-reports is worse than one that says what it
looked at.
"""

from __future__ import annotations

import asyncio
import logging
import tempfile
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path

from mira import sandbox
from mira.autofix.redact import redact
from mira.checks.config_models import CheckToolConfig
from mira.checks.context import CheckContext, CheckOutcome, CheckRunner
from mira.checks.models import CheckFinding, Evidence, SkipReason, fingerprint
from mira.gate import paths as gate_paths

logger = logging.getLogger(__name__)

# Bytes of a tool's output kept as evidence. Output is untrusted data and is
# redacted before it is stored.
MAX_OUTPUT_BYTES = 200_000

# Findings kept from one tool. A linter newly pointed at a legacy file reports
# hundreds; a wall of them is not a review comment, it is a denial of service
# against the reader.
MAX_FINDINGS = 20

# Files materialised into the scratch tree. A cap on both count and total
# bytes, because the deployment profile is a small board and the caller has
# already been told what the cap is.
MAX_FILES = 200
MAX_TOTAL_BYTES = 4_000_000

# How far below the check's own budget an analyser's timeout is set.
#
# The subprocess runs on a worker thread through `asyncio.to_thread`, and a
# thread cannot be cancelled. If the runner's `wait_for` fired first, the
# coroutine would be cancelled while the tool was still running: the scratch
# directory would be removed from under it, and the process would live on until
# its own deadline. So the tool's timeout is set *below* the check's, which
# makes `sandbox.run_argv` — which does kill the process group — the one that
# fires, and leaves the runner's ceiling as the backstop it should be.
CANCELLATION_MARGIN_SECONDS = 5.0

# Floor for that subtraction, so a very short check budget still leaves an
# analyser long enough to start and be killed cleanly rather than never running.
MIN_TOOL_SECONDS = 1.0


@dataclass(frozen=True)
class ToolFinding:
    """One thing an analyser reported, before it becomes a check finding."""

    path: str
    line: int
    rule_id: str
    message: str
    severity: str = "warning"
    end_line: int = 0
    snippet: str = ""


class ToolAdapter(ABC):
    """One deterministic analyser Mira knows how to drive."""

    #: Matches the allowlisted name in configuration.
    name: str = ""
    title: str = ""
    #: Adapter version, bumped when the argv or the parser changes. Persisted
    #: with every result, so a result is attributable to the logic that made it.
    version: str = "1"
    description: str = ""
    #: Executable looked up on ``PATH``.
    binary: str = ""
    #: File extensions this analyser has anything to say about. Empty means
    #: "every changed file", which is right for a secret scanner and wrong for
    #: a Python linter.
    extensions: tuple[str, ...] = ()

    @abstractmethod
    async def analyse(self, ctx: CheckContext, config: CheckToolConfig) -> CheckOutcome:
        """Run the analyser and turn its answer into a check outcome."""

    def runner(self, config: CheckToolConfig) -> CheckRunner:
        """Bind this adapter to one repository's configuration for the scheduler."""

        async def _run(ctx: CheckContext) -> CheckOutcome:
            return await self.analyse(ctx, config)

        return _run

    # ── Shared helpers ───────────────────────────────────────────────────

    def select(self, ctx: CheckContext, config: CheckToolConfig) -> list[str]:
        """Changed paths this analyser should look at, after every filter."""
        paths = sorted(ctx.changed_paths)
        if config.paths:
            paths = sorted(gate_paths.select(paths, list(config.paths)))
        if self.extensions:
            paths = [path for path in paths if path.lower().endswith(self.extensions)]
        return paths[:MAX_FILES]

    async def materialize(self, ctx: CheckContext, paths: list[str], root: Path) -> list[str]:
        """Write the changed files into a scratch tree at their own paths.

        Returns the paths that were actually written. Re-checks containment
        even though the paths came from a parsed diff: this function writes to
        a filesystem, and a filesystem write is not the place to rely on a
        caller having done its job.
        """
        written: list[str] = []
        total = 0
        resolved_root = root.resolve()
        for path in paths:
            content = await ctx.file_content(path)
            if not content:
                continue
            total += len(content)
            if total > MAX_TOTAL_BYTES:
                break
            target = (resolved_root / path).resolve()
            if not target.is_relative_to(resolved_root):
                logger.debug("Refusing to materialise %s outside the workspace", path)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8", newline="")
            written.append(path)
        return written

    async def fetch_config_file(
        self, ctx: CheckContext, config: CheckToolConfig, root: Path
    ) -> str:
        """Copy the operator's named config file into the scratch tree.

        Returns the relative path written, or "" when there is none or it could
        not be read. The name comes from deployment configuration and was
        validated as repository-relative at load; its *contents* come from the
        repository, which is fine — a rule file a team commits is a file that
        team reviews, and the analyser is the thing that interprets it.
        """
        if not config.config_path:
            return ""
        content = await ctx.file_content(config.config_path)
        if not content:
            return ""
        target = (root.resolve() / config.config_path).resolve()
        if not target.is_relative_to(root.resolve()):  # pragma: no cover - validated at load
            return ""
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8", newline="")
        return config.config_path

    async def check_version(self, config: CheckToolConfig, workspace: Path) -> str:
        """ "" when the version is acceptable, otherwise why it is not.

        A pin is a promise that the rules being enforced are the ones somebody
        reviewed. An analyser whose version does not match it is not run: a
        newer one can add rules nobody agreed to and a older one can silently
        stop enforcing rules a team relies on, and neither is a difference a
        pull request should discover.
        """
        if not config.require_version:
            return ""
        outcome = await asyncio.to_thread(
            sandbox.run_argv,
            [self.binary, "--version"],
            cwd=workspace,
            timeout_seconds=30.0,
        )
        if outcome.status == "missing":
            return f"{self.binary} is not installed in this environment"
        if not outcome.ran:
            return f"{self.binary} --version {outcome.status}: {outcome.detail}"
        reported = f"{outcome.stdout} {outcome.stderr}".strip()
        if config.require_version not in reported:
            return (
                f"this repository pins {self.binary} to {config.require_version!r} and the "
                f"installed one reports {reported.splitlines()[0][:120] if reported else 'nothing'!r}"
            )
        return ""

    def to_findings(self, results: list[ToolFinding]) -> list[CheckFinding]:
        """Turn the analyser's own output into findings with real evidence."""
        findings: list[CheckFinding] = []
        for item in results[:MAX_FINDINGS]:
            evidence = Evidence(
                path=item.path,
                start_line=item.line,
                end_line=item.end_line,
                snippet=redact(item.snippet)[:400],
                detail=redact(item.message)[:400],
                source=f"tool:{self.name}",
            )
            findings.append(
                CheckFinding(
                    fingerprint=fingerprint(
                        path=item.path,
                        # The rule id is the stable half. Including the message
                        # would mean a tool that reworded a diagnostic looked
                        # like it had found something new — and would stop the
                        # finding matching a model's description of the same
                        # problem, which is what dedup exists to catch.
                        signature=item.rule_id or item.message,
                    ),
                    title=f"{self.name}: {item.rule_id or 'finding'}",
                    detail=redact(item.message)[:1_000],
                    severity=item.severity,
                    evidence=[evidence],
                    sources=[f"tool.{self.name}"],
                )
            )
        return findings


class SubprocessTool(ToolAdapter):
    """An analyser that is a binary on ``PATH``.

    Subclasses supply ``argv`` and ``parse``. Everything else — the scratch
    tree, the version pin, the timeout, the rlimits, and the four ways running
    a command can fail without saying anything about the code — is here.
    """

    #: Exit codes that mean "ran fine, found nothing". Most linters use 0 for
    #: clean and 1 for "found something", which is not an error.
    clean_exit_codes: tuple[int, ...] = (0,)
    #: Exit codes that mean "ran fine, found something".
    finding_exit_codes: tuple[int, ...] = (1,)
    memory_limit_mb: int = 1024
    cpu_seconds: int = 120

    #: Flags that count as "the operator named a ruleset" when they appear in
    #: ``args``. Consulted only by adapters that set ``requires_ruleset``.
    ruleset_flags: tuple[str, ...] = ("--config", "-c", "--rules")

    def _names_ruleset(self, extra: list[str]) -> bool:
        return any(
            arg == flag or arg.startswith(f"{flag}=")
            for arg in extra
            for flag in self.ruleset_flags
        )

    @abstractmethod
    def argv(self, *, files: list[str], config_file: str, extra: list[str]) -> list[str]:
        """The argument list, built from adapter constants and configuration."""

    #: Whether this analyser refuses to run without a ruleset. Semgrep and
    #: ESLint do; ruff and gitleaks have working defaults. An adapter that
    #: needs one and was given none is *skipped* with the setting named, never
    #: run with whatever ruleset happens to be lying around.
    requires_ruleset: bool = False

    @abstractmethod
    def parse(self, stdout: str, stderr: str, *, workspace: Path) -> list[ToolFinding]:
        """Turn this analyser's output into findings. Never raises.

        Given the workspace because some analysers report absolute paths and a
        finding has to point at a repository-relative one to be navigable.
        """

    async def analyse(self, ctx: CheckContext, config: CheckToolConfig) -> CheckOutcome:
        started = time.monotonic()
        files = self.select(ctx, config)
        if not files:
            return CheckOutcome.skipped(
                f"No changed file is one {self.name} looks at.",
                SkipReason.NOT_APPLICABLE,
            )

        # Clamped under the check's own ceiling even when an operator
        # configured a longer one: a tool allowed to outlive its check is a
        # tool the runner will try to cancel and cannot.
        budget = min(ctx.policy.check_timeout_seconds, max(MIN_TOOL_SECONDS, ctx.remaining))
        ceiling = max(MIN_TOOL_SECONDS, budget - CANCELLATION_MARGIN_SECONDS)
        timeout = min(config.timeout_seconds or ceiling, ceiling)

        with tempfile.TemporaryDirectory(prefix=f"mira-check-{self.name}-") as scratch:
            workspace = Path(scratch)
            try:
                written = await self.materialize(ctx, files, workspace)
            except OSError as exc:
                return CheckOutcome.failed(
                    error=f"the workspace could not be prepared: {exc}",
                    summary=(
                        f"Mira could not prepare a workspace for {self.name}, so it did "
                        "not run. This is a Mira problem, not a problem with the change."
                    ),
                )
            if not written:
                return CheckOutcome.failed(
                    error="none of the changed files could be read at the head commit",
                    summary=(
                        f"Mira could not read the changed files, so {self.name} had "
                        "nothing to analyse. This is a Mira problem, not a problem with "
                        "the change."
                    ),
                )

            mismatch = await self.check_version(config, workspace)
            if mismatch:
                return CheckOutcome.skipped(
                    f"{self.name} did not run: {mismatch}.",
                    SkipReason.TOOL_MISSING,
                )

            config_file = await self.fetch_config_file(ctx, config, workspace)
            extra_args = list(config.args)
            if self.requires_ruleset and not config_file and not self._names_ruleset(extra_args):
                return CheckOutcome.skipped(
                    f"{self.name} did not run: it needs a ruleset and this repository "
                    "names none. Set `config_path` on this tool's entry, or pass the "
                    "ruleset in `args`. Reported as skipped rather than passed, because "
                    "an analyser that never ran found nothing for the wrong reason.",
                    SkipReason.TOOL_MISSING,
                )
            argv = self.argv(files=written, config_file=config_file, extra=extra_args)
            outcome = await asyncio.to_thread(
                sandbox.run_argv,
                argv,
                cwd=workspace,
                timeout_seconds=timeout,
                memory_limit_mb=self.memory_limit_mb,
                cpu_seconds=self.cpu_seconds,
                max_output_bytes=MAX_OUTPUT_BYTES,
            )
            results: list[ToolFinding] | None = None
            parse_error = ""
            if outcome.ran and (
                outcome.exit_code in self.clean_exit_codes
                or outcome.exit_code in self.finding_exit_codes
            ):
                try:
                    results = self.parse(outcome.stdout, outcome.stderr, workspace=workspace)
                except Exception as exc:  # noqa: BLE001 - an unparseable report proves nothing
                    logger.warning("Could not parse %s output: %s", self.name, exc)
                    parse_error = str(exc)

        note = (
            " (CPU and memory limits are unavailable on this platform)" if outcome.unbounded else ""
        )

        if outcome.status == "missing":
            return CheckOutcome.skipped(
                f"{self.name} did not run: {self.binary} is not installed in this "
                "environment. Install it in the image, or turn this check off — it is "
                "reported as skipped rather than passed so nobody reads its silence as "
                "a clean result.",
                SkipReason.TOOL_MISSING,
            )
        if outcome.status == "timeout":
            return CheckOutcome(
                state="timeout",
                summary=(
                    f"{self.name} was still running after {timeout:g}s and was stopped, "
                    f"so it reached no conclusion about this change{note}."
                ),
                error=outcome.detail,
            )
        if not outcome.ran:
            return CheckOutcome.failed(
                error=outcome.detail,
                summary=(
                    f"{self.name} could not be run, so it says nothing about this "
                    "change. This is a Mira problem, not a problem with the change."
                ),
            )

        if (
            outcome.exit_code not in self.clean_exit_codes
            and outcome.exit_code not in self.finding_exit_codes
        ):
            return CheckOutcome.failed(
                error=f"{self.binary} exited {outcome.exit_code}: "
                + redact(outcome.stderr or outcome.stdout)[:1_000],
                summary=(
                    f"{self.name} exited {outcome.exit_code}, which is neither a clean "
                    "run nor a run that found something, so Mira does not know what it "
                    "concluded. This is a Mira problem, not a problem with the change."
                ),
            )

        if results is None:
            return CheckOutcome.failed(
                error=f"could not parse {self.name} output: {parse_error}",
                summary=(
                    f"{self.name} ran and Mira could not read its report, so the result "
                    "is unknown. This is a Mira problem, not a problem with the change."
                ),
            )

        duration = time.monotonic() - started
        logger.debug("%s analysed %d file(s) in %.2fs", self.name, len(written), duration)

        if not results:
            return CheckOutcome.passed(
                summary=f"{self.name} found nothing in {len(written)} changed file(s){note}.",
                evidence=[
                    Evidence(
                        path=path, detail=f"analysed by {self.name}", source=f"tool:{self.name}"
                    )
                    for path in written[:10]
                ],
            )

        findings = self.to_findings(results)
        extra = len(results) - len(findings)
        summary = f"{self.name} reported {len(results)} finding(s)."
        if extra > 0:
            summary += f" Showing {len(findings)}; {extra} more were reported and not listed."
        return CheckOutcome.violation(summary=summary + note, findings=findings)
