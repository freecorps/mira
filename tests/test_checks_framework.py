"""Phase 6 — the framework itself: states, budgets, evidence and dedup.

The acceptance criterion these defend is one sentence: *a user must be able to
tell a violation of the project's rules from a failure of Mira's
infrastructure.* So every test here is written as an assertion about which of
the five states came out, never merely "it did not pass" — a test that accepted
`skipped` where `violation` was meant would pass while the framework lied.

The scheduler is driven with checks built in the test rather than with the real
ones, because the question here is what the *runner* does with an answer, not
whether any particular check answers correctly. The native checks have their
own file.
"""

from __future__ import annotations

import asyncio
import time

import pytest

from mira.checks.context import CheckContext, CheckOutcome
from mira.checks.dedupe import deduplicate, duplicate_findings
from mira.checks.models import (
    CheckFinding,
    CheckResult,
    CheckRun,
    CheckRunInputs,
    Evidence,
    SkipReason,
    fingerprint,
    result_key,
    run_key,
)
from mira.checks.policy import resolve_policy
from mira.checks.registry import CheckSpec
from mira.checks.runner import run_checks
from mira.config import ChecksConfig


def _policy(**overrides):
    overrides.setdefault("enabled", True)
    return resolve_policy(ChecksConfig(**overrides), "acme", "app")


def _ctx(policy=None, **overrides) -> CheckContext:
    return CheckContext(
        policy=policy or _policy(),
        owner="acme",
        repo="app",
        pr_number=7,
        pr_url="https://github.com/acme/app/pull/7",
        head_sha="head123",
        **overrides,
    )


def _inputs(**overrides) -> CheckRunInputs:
    base = {
        "platform": "github",
        "owner": "acme",
        "repo": "app",
        "pr_number": 7,
        "pr_url": "https://github.com/acme/app/pull/7",
        "head_sha": "head123",
    }
    base.update(overrides)
    return CheckRunInputs(**base)


def _spec(check_id: str, runner, *, origin="native", version="1") -> CheckSpec:
    return CheckSpec(
        check_id=check_id,
        title=check_id,
        origin=origin,
        version=version,
        run=runner,
    )


def _evidence(path="src/a.py", line=10) -> Evidence:
    return Evidence(path=path, start_line=line, snippet="x = 1", source="diff")


def _finding(signature="s", path="src/a.py", line=10, sources=None) -> CheckFinding:
    return CheckFinding(
        fingerprint=fingerprint(path=path, signature=signature),
        title="a problem",
        detail="something is wrong",
        evidence=[_evidence(path, line)],
        sources=list(sources or ["native.x"]),
    )


async def _run(specs, ctx=None, inputs=None) -> CheckRun:
    ctx = ctx or _ctx()
    return await run_checks_with(specs, ctx, inputs or _inputs())


async def run_checks_with(specs, ctx, inputs, monkeypatch=None) -> CheckRun:
    """Drive `run_checks` with an explicit spec list.

    The registry builds its list from the policy; here the point is the
    scheduler, so the spec list is injected rather than configured into
    existence.
    """
    import mira.checks.runner as runner_module

    original = runner_module.specs_for
    runner_module.specs_for = lambda _policy: list(specs)
    try:
        return await run_checks(ctx, inputs)
    finally:
        runner_module.specs_for = original


# ───────────────────────────────────────────────────────── the five states ──


async def test_a_passing_check_is_recorded_as_a_pass() -> None:
    async def check(_ctx):
        return CheckOutcome.passed("nothing to report", [_evidence()])

    run = await _run([_spec("native.ok", check)])
    result = run.results[0]
    assert result.state == "pass"
    assert result.is_violation is False
    assert result.incomplete is False
    assert result.evidence


async def test_a_violation_with_evidence_is_recorded_as_a_violation() -> None:
    async def check(_ctx):
        return CheckOutcome.violation("one problem", [_finding()])

    run = await _run([_spec("native.bad", check)])
    result = run.results[0]
    assert result.state == "violation"
    assert result.is_violation is True
    assert result.findings[0].evidence[0].path == "src/a.py"


async def test_an_infrastructure_error_is_never_a_violation() -> None:
    """The distinction the whole phase exists for."""

    async def check(_ctx):
        return CheckOutcome.failed("the model refused")

    run = await _run([_spec("native.broken", check)])
    result = run.results[0]
    assert result.state == "infrastructure_error"
    assert result.is_violation is False
    assert result.incomplete is True
    assert "Mira problem" in result.summary
    assert run.violations == []


async def test_a_raising_check_becomes_an_infrastructure_error_not_a_violation() -> None:
    async def check(_ctx):
        raise RuntimeError("boom")

    run = await _run([_spec("native.raises", check)])
    result = run.results[0]
    assert result.state == "infrastructure_error"
    assert "RuntimeError" in result.error
    assert result.is_violation is False


async def test_a_check_that_overruns_is_a_timeout_not_a_failure_of_the_pull_request() -> None:
    async def check(_ctx):
        await asyncio.sleep(5)
        return CheckOutcome.passed("never reached")

    policy = _policy(check_timeout_seconds=0.05)
    run = await _run([_spec("native.slow", check)], ctx=_ctx(policy))
    result = run.results[0]
    assert result.state == "timeout"
    assert result.is_violation is False
    assert result.incomplete is True


async def test_a_check_that_does_not_apply_is_skipped_and_is_not_incomplete() -> None:
    async def check(_ctx):
        return CheckOutcome.skipped("no migration here", SkipReason.NOT_APPLICABLE)

    run = await _run([_spec("native.na", check)])
    result = run.results[0]
    assert result.state == "skipped"
    assert result.skip_reason == SkipReason.NOT_APPLICABLE
    assert result.incomplete is False


async def test_a_check_skipped_for_a_missing_tool_is_skipped_but_unanswered() -> None:
    """Shown as a skip; still counts as not having answered."""

    async def check(_ctx):
        return CheckOutcome.skipped("ruff is not installed", SkipReason.TOOL_MISSING)

    run = await _run([_spec("tool.ruff", check, origin="tool")])
    result = run.results[0]
    assert result.state == "skipped"
    assert result.incomplete is True
    assert result.is_violation is False


async def test_a_check_that_is_off_is_recorded_rather_than_omitted() -> None:
    """ "Off" and "does not exist in this version" are different facts."""

    async def check(_ctx):
        raise AssertionError("a check in off mode must not run")

    policy = _policy(modes={"native.off": "off"})
    run = await _run([_spec("native.off", check)], ctx=_ctx(policy))
    assert len(run.results) == 1
    assert run.results[0].state == "skipped"
    assert run.results[0].skip_reason == SkipReason.DISABLED


# ────────────────────────────────────────────────────── evidence is required ──


async def test_a_violation_with_no_evidence_is_downgraded_to_a_skip() -> None:
    """A violation nobody can look up is a guess, and guesses are not recorded."""

    async def check(_ctx):
        return CheckOutcome.violation(
            "I am sure something is wrong",
            [CheckFinding(fingerprint="f", title="vague", detail="trust me")],
        )

    run = await _run([_spec("nl.vague", check, origin="natural_language")])
    result = run.results[0]
    assert result.state == "skipped"
    assert result.skip_reason == SkipReason.NO_EVIDENCE
    assert result.findings == []
    assert "I am sure something is wrong" in result.summary


async def test_the_downgrade_still_counts_as_unanswered_for_a_gate() -> None:
    async def check(_ctx):
        return CheckOutcome.violation("no proof", [CheckFinding(fingerprint="f", title="t")])

    policy = _policy(default_mode="error")
    run = await _run([_spec("nl.vague", check, origin="natural_language")], ctx=_ctx(policy))
    assert run.results[0].incomplete is True
    assert run.verdict == "incomplete"


async def test_evidence_is_capped_per_check() -> None:
    async def check(_ctx):
        finding = _finding()
        finding.evidence = [_evidence(line=n) for n in range(50)]
        return CheckOutcome.violation("many", [finding])

    policy = _policy(max_evidence_per_check=3)
    run = await _run([_spec("tool.noisy", check, origin="tool")], ctx=_ctx(policy))
    assert len(run.results[0].findings[0].evidence) == 3


# ─────────────────────────────────────────────────────────────── the verdict ──


async def test_a_warning_mode_violation_never_blocks() -> None:
    async def check(_ctx):
        return CheckOutcome.violation("a problem", [_finding()])

    policy = _policy(default_mode="warning")
    run = await _run([_spec("native.bad", check)], ctx=_ctx(policy))
    assert run.results[0].state == "violation"
    assert run.results[0].blocking is False
    assert run.verdict == "pass"


async def test_an_error_mode_violation_blocks() -> None:
    async def check(_ctx):
        return CheckOutcome.violation("a problem", [_finding()])

    policy = _policy(modes={"native.bad": "error"})
    run = await _run([_spec("native.bad", check)], ctx=_ctx(policy))
    assert run.verdict == "violation"


async def test_an_error_mode_infrastructure_failure_is_incomplete_not_a_violation() -> None:
    async def check(_ctx):
        return CheckOutcome.failed("network down")

    policy = _policy(modes={"context.ci": "error"})
    run = await _run([_spec("context.ci", check, origin="context")], ctx=_ctx(policy))
    assert run.verdict == "incomplete"
    assert run.violations == []


async def test_a_real_violation_is_reported_ahead_of_an_incomplete_check() -> None:
    """Both are true; the actionable one is the one worth leading with."""

    async def bad(_ctx):
        return CheckOutcome.violation("a problem", [_finding()])

    async def broken(_ctx):
        return CheckOutcome.failed("network down")

    policy = _policy(default_mode="error")
    run = await _run(
        [_spec("native.bad", bad), _spec("context.ci", broken, origin="context")],
        ctx=_ctx(policy),
    )
    assert run.verdict == "violation"


async def test_a_run_with_no_results_is_not_run() -> None:
    run = CheckRun(run_key="k", inputs=_inputs())
    assert run.verdict == "not_run"


# ─────────────────────────────────────────────── budget, timeout, concurrency ──


async def test_concurrency_is_capped_at_the_configured_limit() -> None:
    """The Orange Pi profile is the reason this number exists at all."""
    live = 0
    peak = 0

    async def check(_ctx):
        nonlocal live, peak
        live += 1
        peak = max(peak, live)
        await asyncio.sleep(0.05)
        live -= 1
        return CheckOutcome.passed("ok", [_evidence()])

    policy = _policy(max_concurrency=2)
    specs = [_spec(f"native.c{n}", check) for n in range(8)]
    run = await _run(specs, ctx=_ctx(policy))
    assert peak <= 2
    assert all(result.state == "pass" for result in run.results)


async def test_the_run_budget_skips_the_checks_it_never_reached() -> None:
    async def slow(_ctx):
        await asyncio.sleep(0.3)
        return CheckOutcome.passed("ok", [_evidence()])

    policy = _policy(max_concurrency=1, total_timeout_seconds=0.2, check_timeout_seconds=10)
    specs = [_spec(f"native.b{n}", slow) for n in range(4)]
    run = await _run(specs, ctx=_ctx(policy))
    skipped = [r for r in run.results if r.skip_reason == SkipReason.BUDGET_EXHAUSTED]
    assert skipped, "a run that spent its budget must say so"
    # And a budget-exhausted skip is unanswered, not a pass.
    assert all(result.incomplete for result in skipped)


async def test_a_slow_check_does_not_stop_the_others_from_answering() -> None:
    async def slow(_ctx):
        await asyncio.sleep(5)
        return CheckOutcome.passed("never")

    async def fast(_ctx):
        return CheckOutcome.passed("ok", [_evidence()])

    policy = _policy(max_concurrency=4, check_timeout_seconds=0.05)
    run = await _run([_spec("native.slow", slow), _spec("native.fast", fast)], ctx=_ctx(policy))
    by_id = {result.check_id: result for result in run.results}
    assert by_id["native.slow"].state == "timeout"
    assert by_id["native.fast"].state == "pass"


async def test_each_result_carries_its_own_duration() -> None:
    async def check(_ctx):
        await asyncio.sleep(0.02)
        return CheckOutcome.passed("ok", [_evidence()])

    run = await _run([_spec("native.timed", check)])
    assert run.results[0].duration_seconds >= 0.01
    assert run.duration_seconds >= run.results[0].duration_seconds


# ──────────────────────────────────────────────────────────────── dedup ──


def test_two_producers_that_found_the_same_thing_produce_one_finding() -> None:
    tool = CheckResult(
        check_id="tool.gitleaks",
        origin="tool",
        state="violation",
        mode="warning",
        findings=[
            CheckFinding(
                fingerprint=fingerprint(path="app/config.py", signature="hardcoded key"),
                title="gitleaks: generic-api-key",
                detail="a credential-shaped secret",
                evidence=[Evidence(path="app/config.py", start_line=14, source="tool:gitleaks")],
                sources=["tool.gitleaks"],
            )
        ],
        sources=["tool.gitleaks"],
    )
    model = CheckResult(
        check_id="nl.no-secrets",
        origin="natural_language",
        state="violation",
        mode="warning",
        findings=[
            CheckFinding(
                # Same file, same words, a line apart — which is exactly how
                # two producers describe one problem.
                fingerprint=fingerprint(path="app/config.py", signature="hardcoded key"),
                title="nl.no-secrets: app/config.py",
                detail="this commits an API key",
                evidence=[
                    Evidence(path="app/config.py", start_line=15, source="llm", snippet="KEY = ..")
                ],
                sources=["nl.no-secrets"],
            )
        ],
        sources=["nl.no-secrets"],
    )

    results = deduplicate([tool, model])
    all_findings = [f for r in results for f in r.findings]
    assert len(all_findings) == 1, "one problem must appear once"

    merged = all_findings[0]
    assert sorted(merged.sources) == ["nl.no-secrets", "tool.gitleaks"]
    # Both evidences survive: that is the whole reason to run two producers.
    assert {item.source for item in merged.evidence} == {"tool:gitleaks", "llm"}
    assert "this commits an API key" in merged.detail
    assert duplicate_findings(results) == [merged]


def test_the_producer_that_owns_a_merged_finding_is_deterministic() -> None:
    """Otherwise two identical runs attribute the same finding differently."""

    def _pair():
        native = CheckResult(
            check_id="native.migrations",
            origin="native",
            state="violation",
            mode="warning",
            findings=[_finding(signature="drops a column", path="db/1.sql", line=3)],
            sources=["native.migrations"],
        )
        tool = CheckResult(
            check_id="tool.semgrep",
            origin="tool",
            state="violation",
            mode="warning",
            findings=[_finding(signature="drops a column", path="db/1.sql", line=3)],
            sources=["tool.semgrep"],
        )
        return native, tool

    first = deduplicate(list(_pair()))
    # Same inputs, opposite order in: the owner must not change.
    second = deduplicate(list(reversed(_pair())))
    owner_of = lambda results: next(r.check_id for r in results if r.findings)  # noqa: E731
    assert owner_of(first) == owner_of(second) == "native.migrations"


def test_a_producer_whose_finding_was_merged_keeps_its_own_state() -> None:
    """It really did find something; rewriting it to `pass` would be a lie."""
    native = CheckResult(
        check_id="native.migrations",
        origin="native",
        state="violation",
        mode="warning",
        summary="1 schema concern.",
        findings=[_finding(signature="drops a column", path="db/1.sql", line=3)],
        sources=["native.migrations"],
    )
    tool = CheckResult(
        check_id="tool.semgrep",
        origin="tool",
        state="violation",
        mode="warning",
        summary="semgrep reported 1 finding.",
        findings=[_finding(signature="drops a column", path="db/1.sql", line=3)],
        sources=["tool.semgrep"],
    )
    results = deduplicate([native, tool])
    semgrep = next(r for r in results if r.check_id == "tool.semgrep")
    assert semgrep.state == "violation"
    assert semgrep.findings == []
    assert "native.migrations" in semgrep.summary


def test_distinct_problems_on_one_line_are_not_folded_together() -> None:
    one = CheckResult(
        check_id="tool.ruff",
        origin="tool",
        state="violation",
        mode="warning",
        findings=[_finding(signature="unused import", path="a.py", line=1)],
        sources=["tool.ruff"],
    )
    two = CheckResult(
        check_id="tool.semgrep",
        origin="tool",
        state="violation",
        mode="warning",
        findings=[_finding(signature="sql injection", path="a.py", line=1)],
        sources=["tool.semgrep"],
    )
    results = deduplicate([one, two])
    assert sum(len(r.findings) for r in results) == 2


async def test_dedup_runs_inside_the_scheduler() -> None:
    async def tool(_ctx):
        return CheckOutcome.violation("found it", [_finding(sources=["tool.x"])])

    async def model(_ctx):
        return CheckOutcome.violation("found it too", [_finding(sources=["nl.y"])])

    run = await _run(
        [
            _spec("tool.x", tool, origin="tool"),
            _spec("nl.y", model, origin="natural_language"),
        ]
    )
    assert len(run.findings) == 1
    assert len(run.findings[0].sources) == 2


# ──────────────────────────────────────────────────────────────── identity ──


def test_the_run_key_is_stable_across_triggers_and_moves_with_the_facts() -> None:
    def key(**overrides):
        base = {
            "platform": "github",
            "owner": "acme",
            "repo": "app",
            "pr_number": 7,
            "head_sha": "abc",
            "policy_version": "checks-v1+deadbeef",
            "inputs_digest": "d1",
        }
        base.update(overrides)
        return run_key(**base)

    assert key() == key(), "the same facts are the same run"
    assert key() != key(head_sha="def"), "a new commit is a new run"
    assert key() != key(inputs_digest="d2"), "different facts are a different run"
    assert key() != key(policy_version="checks-v1+other"), "a new policy is a new run"


def test_the_inputs_digest_ignores_the_review_that_triggered_it() -> None:
    """A retried review is not a different world to check."""
    first = _inputs(review_id=1)
    second = _inputs(review_id=2)
    assert first.digest == second.digest


def test_a_result_key_is_one_per_check_per_run() -> None:
    key = run_key(
        platform="github",
        owner="acme",
        repo="app",
        pr_number=7,
        head_sha="abc",
        policy_version="v",
    )
    assert result_key(run_key_value=key, check_id="native.tests") == result_key(
        run_key_value=key, check_id="native.tests"
    )
    assert result_key(run_key_value=key, check_id="native.tests") != result_key(
        run_key_value=key, check_id="native.docs"
    )


async def test_two_identical_runs_produce_identical_result_keys() -> None:
    """Reproducibility, stated as the property the store depends on."""

    async def check(_ctx):
        return CheckOutcome.passed("ok", [_evidence()])

    specs = [_spec("native.a", check), _spec("native.b", check)]
    first = await _run(specs)
    second = await _run(specs)
    assert first.run_key == second.run_key
    assert [r.result_key for r in first.results] == [r.result_key for r in second.results]


# ────────────────────────────────────────────────────────── shared context ──


async def test_the_shared_helper_computes_once_for_the_whole_run() -> None:
    calls = 0

    async def expensive():
        nonlocal calls
        calls += 1
        return "answer"

    ctx = _ctx()
    results = await asyncio.gather(*(ctx.shared("k", expensive) for _ in range(5)))
    assert results == ["answer"] * 5
    assert calls == 1


async def test_a_shared_failure_is_shared_rather_than_retried() -> None:
    """One outage produces one error, not one per interested check."""
    calls = 0

    async def failing():
        nonlocal calls
        calls += 1
        raise RuntimeError("tracker down")

    ctx = _ctx()
    for _ in range(3):
        with pytest.raises(RuntimeError):
            await ctx.shared("k", failing)
    assert calls == 1


async def test_the_context_remaining_budget_counts_down() -> None:
    ctx = _ctx()
    ctx.deadline = time.monotonic() + 1.0
    assert 0 < ctx.remaining <= 1.0
    ctx.deadline = time.monotonic() - 1.0
    assert ctx.remaining == 0.0
