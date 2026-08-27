"""The four analysers Mira drives as subprocesses.

Each one is an argv and a parser. The interesting decisions are the same for
all of them and are made in :mod:`mira.checks.tools.base`; what is decided here
is narrower, and worth stating because a wrong answer looks like a working
check:

* **Which exit code means "found something".** Every linter here uses a
  non-zero exit for findings, which is not an error — and treating it as one
  would turn every objection into an infrastructure failure that says nothing.
  Every *other* non-zero code is a failure, because a tool that exited 2 has
  not told us what it concluded.
* **Which paths are worth passing.** A Python linter given a ``.ts`` file
  wastes a process; a secret scanner given only ``.py`` files misses the
  ``.env`` somebody committed. So ruff and ESLint filter by extension and
  gitleaks does not.
* **Whether a ruleset is required.** Semgrep and ESLint refuse to run without
  one, so an install that names none gets a *skip* that says so. Running them
  against whatever default happens to exist would be enforcing rules nobody
  reviewed.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from mira.checks.tools.base import SubprocessTool, ToolFinding

logger = logging.getLogger(__name__)


def _relative(path: str, workspace: Path) -> str:
    """A workspace-relative path, whatever shape the tool reported.

    ESLint reports absolute paths and semgrep reports whatever it was given.
    A finding whose path does not match the repository's own spelling cannot be
    linked, cannot be deduplicated against another producer's finding, and
    reads as a file the reader does not have.
    """
    candidate = (path or "").replace("\\", "/")
    root = str(workspace.resolve()).replace("\\", "/").rstrip("/")
    if candidate.startswith(root):
        candidate = candidate[len(root) :]
    return candidate.lstrip("./").lstrip("/")


def _severity(raw: str) -> str:
    """Map a tool's own severity vocabulary onto Mira's advisory one."""
    value = (raw or "").strip().lower()
    if value in {"error", "critical", "high", "blocker", "2"}:
        return "blocker"
    if value in {"warning", "warn", "medium", "moderate", "1"}:
        return "warning"
    return "suggestion"


class SemgrepTool(SubprocessTool):
    """Pattern-based static analysis over the changed files."""

    name = "semgrep"
    title = "Semgrep"
    version = "1"
    description = "Semgrep rules, run over the files this pull request changed."
    binary = "semgrep"
    requires_ruleset = True
    # Semgrep exits 0 whether or not it found something and reports findings
    # in its JSON — unless `--error` is passed, when findings become exit 1.
    # Both are accepted so an operator who adds `--error` to `args` does not
    # silently turn every finding into an infrastructure error. A fatal problem
    # exits 2 or higher, which is neither and is reported as one.
    clean_exit_codes = (0,)
    finding_exit_codes = (1,)

    def argv(self, *, files: list[str], config_file: str, extra: list[str]) -> list[str]:
        argv = [self.binary, "--json", "--quiet", "--disable-version-check", "--metrics=off"]
        if config_file:
            argv += ["--config", config_file]
        argv += extra
        return [*argv, "--", *files]

    def parse(self, stdout: str, stderr: str, *, workspace: Path) -> list[ToolFinding]:
        data = json.loads(stdout or "{}")
        findings: list[ToolFinding] = []
        for item in data.get("results") or []:
            extra_data = item.get("extra") or {}
            start = item.get("start") or {}
            end = item.get("end") or {}
            findings.append(
                ToolFinding(
                    path=_relative(str(item.get("path") or ""), workspace),
                    line=int(start.get("line") or 0),
                    end_line=int(end.get("line") or 0),
                    rule_id=str(item.get("check_id") or ""),
                    message=str(extra_data.get("message") or ""),
                    severity=_severity(str(extra_data.get("severity") or "")),
                    snippet=str(extra_data.get("lines") or "")[:400],
                )
            )
        return findings


class RuffTool(SubprocessTool):
    """Ruff over the changed Python files."""

    name = "ruff"
    title = "Ruff"
    version = "1"
    description = "Ruff lint rules, run over the Python files this pull request changed."
    binary = "ruff"
    extensions = (".py", ".pyi")
    clean_exit_codes = (0,)
    finding_exit_codes = (1,)

    def argv(self, *, files: list[str], config_file: str, extra: list[str]) -> list[str]:
        argv = [self.binary, "check", "--output-format", "json", "--no-cache", "--quiet"]
        if config_file:
            argv += ["--config", config_file]
        argv += extra
        return [*argv, "--", *files]

    def parse(self, stdout: str, stderr: str, *, workspace: Path) -> list[ToolFinding]:
        data = json.loads(stdout or "[]")
        findings: list[ToolFinding] = []
        for item in data if isinstance(data, list) else []:
            location = item.get("location") or {}
            end_location = item.get("end_location") or {}
            findings.append(
                ToolFinding(
                    path=_relative(str(item.get("filename") or ""), workspace),
                    line=int(location.get("row") or 0),
                    end_line=int(end_location.get("row") or 0),
                    rule_id=str(item.get("code") or ""),
                    message=str(item.get("message") or ""),
                    severity="warning",
                )
            )
        return findings


class EslintTool(SubprocessTool):
    """ESLint over the changed JavaScript and TypeScript files."""

    name = "eslint"
    title = "ESLint"
    version = "1"
    description = "ESLint rules, run over the JS/TS files this pull request changed."
    binary = "eslint"
    extensions = (".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx", ".vue", ".svelte")
    requires_ruleset = True
    clean_exit_codes = (0,)
    finding_exit_codes = (1,)

    def argv(self, *, files: list[str], config_file: str, extra: list[str]) -> list[str]:
        argv = [self.binary, "--format", "json", "--no-color"]
        if config_file:
            argv += ["--config", config_file]
        argv += extra
        return [*argv, "--", *files]

    def parse(self, stdout: str, stderr: str, *, workspace: Path) -> list[ToolFinding]:
        data = json.loads(stdout or "[]")
        findings: list[ToolFinding] = []
        for entry in data if isinstance(data, list) else []:
            path = _relative(str(entry.get("filePath") or ""), workspace)
            for message in entry.get("messages") or []:
                findings.append(
                    ToolFinding(
                        path=path,
                        line=int(message.get("line") or 0),
                        end_line=int(message.get("endLine") or 0),
                        rule_id=str(message.get("ruleId") or "eslint"),
                        message=str(message.get("message") or ""),
                        severity=_severity(str(message.get("severity") or "")),
                    )
                )
        return findings


class GitleaksTool(SubprocessTool):
    """Gitleaks over the changed files, looking for committed credentials.

    Runs with ``--no-git`` against the scratch tree rather than against a
    checkout, because there is no checkout: the workspace holds the files this
    pull request changed at the head commit, which is exactly the set worth
    scanning. History is the background poller's job, not a pre-merge check's.
    """

    name = "gitleaks"
    title = "Gitleaks"
    version = "1"
    description = "Gitleaks, looking for credentials in the files this pull request changed."
    binary = "gitleaks"
    clean_exit_codes = (0,)
    finding_exit_codes = (1,)

    #: Report written into the workspace and read back before it is cleaned up.
    REPORT = "mira-gitleaks-report.json"

    def argv(self, *, files: list[str], config_file: str, extra: list[str]) -> list[str]:
        argv = [
            self.binary,
            "detect",
            "--no-git",
            "--source",
            ".",
            "--report-format",
            "json",
            "--report-path",
            self.REPORT,
            "--exit-code",
            "1",
            "--redact",
        ]
        if config_file:
            argv += ["--config", config_file]
        return [*argv, *extra]

    def parse(self, stdout: str, stderr: str, *, workspace: Path) -> list[ToolFinding]:
        report = workspace / self.REPORT
        raw = report.read_text(encoding="utf-8") if report.exists() else (stdout or "[]")
        data = json.loads(raw or "[]")
        findings: list[ToolFinding] = []
        for item in data if isinstance(data, list) else []:
            findings.append(
                ToolFinding(
                    path=_relative(str(item.get("File") or ""), workspace),
                    line=int(item.get("StartLine") or 0),
                    end_line=int(item.get("EndLine") or 0),
                    rule_id=str(item.get("RuleID") or "gitleaks"),
                    message=str(item.get("Description") or "a credential-shaped secret"),
                    severity="blocker",
                    # `--redact` above means gitleaks has already replaced the
                    # matched secret. Quoting the match without it would put the
                    # credential in Mira's database and in a pull-request
                    # comment, which is the one outcome worse than missing it.
                    snippet=str(item.get("Match") or "")[:200],
                )
            )
        return findings


ADAPTERS = (SemgrepTool, RuffTool, EslintTool, GitleaksTool)
