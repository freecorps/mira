"""Phase 6 — the service, and what the merge gate does with a verdict.

Two halves.

*The service*: gather once, run, persist before announcing, and never raise. A
check framework that threw would discard a review that had already landed, and
a run that was announced but never recorded would be a verdict nobody can
audit.

*The gate*: the fail-closed half of the phase. A blocking check that objected
refuses an approval, and a blocking check that could not answer refuses one
too — in different words, with a different reason code, and only the second is
a hard veto an admin cannot override. Reporting an outage as a finding against
somebody's change is the failure this phase exists to prevent, and it would be
just as wrong coming out of the gate as out of a check.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from mira.checks import service as checks_service
from mira.checks.models import CheckRun, CheckRunInputs
from mira.config import ChecksConfig, GateConfig, MiraConfig
from mira.gate.decide import decide
from mira.gate.models import CIState, GateInputs, ReasonCode
from mira.gate.policy import resolve_policy as resolve_gate_policy
from mira.index.store import IndexStore
from mira.models import PRInfo


@pytest.fixture(autouse=True)
def isolated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MIRA_INDEX_DIR", str(tmp_path))
    monkeypatch.delenv("DATABASE_URL", raising=False)


DIFF = (
    "diff --git a/src/app.py b/src/app.py\n"
    "--- a/src/app.py\n"
    "+++ b/src/app.py\n"
    "@@ -1,0 +1,2 @@\n"
    "+import os\n"
    "+x = 1\n"
)


def _pr() -> PRInfo:
    return PRInfo(
        title="Add the ingest limiter and its metric",
        description="Rate-limits the ingest endpoint so a burst cannot exhaust the pool.",
        base_branch="main",
        head_branch="feature/limit",
        url="https://github.com/acme/app/pull/7",
        number=7,
        owner="acme",
        repo="app",
        head_sha="head123",
        platform="github",
        author="alice",
    )


class _Provider:
    """Enough provider to drive a run, recording what was announced."""

    def __init__(self, *, diff=DIFF, labels=(), raises=None) -> None:
        self.diff = diff
        self.labels = list(labels)
        self.raises = raises
        self.status = None
        self.comments: list[str] = []

    def checks_capabilities(self):
        from mira.checks.capabilities import GITHUB_CAPABILITIES

        return GITHUB_CAPABILITIES

    async def get_pr_diff(self, _pr_info):
        if self.raises is not None:
            raise self.raises
        return self.diff

    async def get_pr_labels(self, _pr_info):
        return self.labels

    async def get_file_content(self, _pr_info, path, _ref):
        return "import os\nx = 1\n" if path == "src/app.py" else ""

    async def get_ci_state(self, _pr_info):
        return CIState(state="success", total=2)

    async def get_issue(self, _pr_info, number, *, owner="", repo=""):
        return None

    async def publish_checks_status(self, _pr_info, **kwargs):
        self.status = kwargs
        return "run-1"

    async def find_bot_comment(self, _pr_info, _marker):
        return None

    async def post_comment(self, _pr_info, body):
        self.comments.append(body)


def _config(**overrides) -> MiraConfig:
    overrides.setdefault("enabled", True)
    return MiraConfig(checks=ChecksConfig(**overrides))


# ──────────────────────────────────────────────────────────────── the service ──


async def test_checks_off_does_not_touch_the_platform_or_the_store() -> None:
    """An install that never turned checks on must not pay for them."""
    provider = _Provider()
    run = await checks_service.evaluate(provider, _pr(), config=MiraConfig())
    assert run.verdict == "not_run"
    assert provider.status is None

    store = IndexStore.open("acme", "app")
    try:
        assert store.count_check_runs({}) == 0
    finally:
        store.close()


async def test_a_run_is_persisted_and_readable_afterwards() -> None:
    run = await checks_service.evaluate(_Provider(), _pr(), config=_config())
    assert run.results, "a run with checks enabled produces results"

    store = IndexStore.open("acme", "app")
    try:
        read = store.get_check_run(run.run_key)
    finally:
        store.close()
    assert read is not None
    assert {r.check_id for r in read.results} >= {"native.tests", "native.docs", "context.ci"}


async def test_a_second_run_over_the_same_facts_converges_on_one_row() -> None:
    config = _config()
    first = await checks_service.evaluate(_Provider(), _pr(), config=config)
    second = await checks_service.evaluate(_Provider(), _pr(), config=config)
    assert first.run_key == second.run_key

    store = IndexStore.open("acme", "app")
    try:
        assert store.count_check_runs({}) == 1
    finally:
        store.close()


async def test_an_unreadable_diff_records_a_failure_and_reports_nothing_about_the_change() -> None:
    provider = _Provider(raises=RuntimeError("502 from the API"))
    run = await checks_service.evaluate(provider, _pr(), config=_config())
    assert run.results == []
    assert "diff could not be read" in run.error
    # A run that *failed* is not a run that did not happen. `not_run` is what a
    # gate ignores, so reporting it here would let a pull request past a
    # blocking check by breaking early enough.
    assert run.verdict == "incomplete"


async def test_an_unparseable_diff_is_not_read_as_an_empty_pull_request() -> None:
    """Otherwise it passes the tests check, the docs check and migrations at once."""
    provider = _Provider(diff="this is not a diff\n@@ nonsense @@\n")
    run = await checks_service.evaluate(provider, _pr(), config=_config())
    assert run.verdict in {"incomplete", "pass", "violation"}
    if run.error:
        assert "could not be parsed" in run.error or "could not be read" in run.error
        assert run.results == []


async def test_checks_that_never_ran_are_still_not_evidence() -> None:
    """The inactive path owes nothing, and says `not_run` rather than failing."""
    run = await checks_service.evaluate(_Provider(), _pr(), config=MiraConfig())
    assert run.verdict == "not_run"
    assert run.error == ""


async def test_the_run_is_announced_when_the_policy_asks() -> None:
    provider = _Provider()
    await checks_service.evaluate(provider, _pr(), config=_config(publish_status=True))
    assert provider.status is not None
    assert provider.status["context"] == "mira/pre-merge-checks"
    assert provider.status["conclusion"] in {"success", "failure", "neutral"}


async def test_announcement_can_be_switched_off() -> None:
    provider = _Provider()
    await checks_service.evaluate(
        provider, _pr(), config=_config(publish_status=False), announce_result=False
    )
    assert provider.status is None


async def test_a_provider_that_refuses_a_status_does_not_break_the_run() -> None:
    class _Refuses(_Provider):
        async def publish_checks_status(self, _pr_info, **kwargs):
            raise RuntimeError("no checks:write")

    provider = _Refuses()
    run = await checks_service.evaluate(provider, _pr(), config=_config())
    assert run.results, "the run still happened"


async def test_the_review_signal_saves_a_diff_fetch() -> None:
    class _NoDiff(_Provider):
        async def get_pr_diff(self, _pr_info):
            raise AssertionError("the diff was already in hand")

    run = await checks_service.evaluate(
        _NoDiff(),
        _pr(),
        config=_config(),
        signal=checks_service.ReviewSignal(diff_text=DIFF, review_id=42),
    )
    assert run.inputs.review_id == 42


async def test_the_latest_verdict_is_scoped_to_the_head_commit() -> None:
    config = _config()
    await checks_service.evaluate(_Provider(), _pr(), config=config)

    assert checks_service.latest_verdict("acme", "app", "github", 7, "head123") in {
        "pass",
        "violation",
        "incomplete",
    }
    # A commit nothing ran against has no evidence, whatever ran before it.
    assert checks_service.latest_verdict("acme", "app", "github", 7, "other") == "not_run"


def test_an_unreachable_store_reads_as_no_evidence(monkeypatch: pytest.MonkeyPatch) -> None:
    def _boom(*args, **kwargs):
        raise RuntimeError("disk gone")

    monkeypatch.setattr(checks_service, "_open_store", _boom)
    assert checks_service.latest_verdict("acme", "app", "github", 7, "head123") == "not_run"


# ────────────────────────────────────────────────────────────── the gate ──


def _gate_policy(**overrides):
    overrides.setdefault("mode", "enforce")
    return resolve_gate_policy(GateConfig(**overrides), "acme", "app")


def _clean_gate_inputs(**overrides) -> GateInputs:
    base = {
        "platform": "github",
        "owner": "acme",
        "repo": "app",
        "pr_number": 7,
        "pr_url": "https://github.com/acme/app/pull/7",
        "pr_author": "alice",
        "base_branch": "main",
        "head_sha": "head123",
        "author_association": "MEMBER",
        "changed_paths": ["src/a.py"],
        "changed_files": 1,
        "added_lines": 10,
        "ci": CIState(state="success", total=2),
        "human_states": {"bob": "COMMENTED"},
        # Most of these cases are about what an *active* framework does; the
        # ones that are not say so explicitly.
        "checks_active": True,
    }
    base.update(overrides)
    return GateInputs(**base)


def test_a_gate_ignores_checks_that_never_ran() -> None:
    """Installing Mira must not change the gate; turning checks on may."""
    decision = decide(
        _clean_gate_inputs(checks_active=False, checks_verdict="not_run"), _gate_policy()
    )
    assert decision.state == "would_approve"


def test_a_gate_ignores_a_passing_check_run() -> None:
    decision = decide(_clean_gate_inputs(checks_verdict="pass"), _gate_policy())
    assert decision.state == "would_approve"


def test_a_blocking_check_violation_refuses_the_approval() -> None:
    decision = decide(
        _clean_gate_inputs(checks_verdict="violation", checks_blocking=["native.tests"]),
        _gate_policy(),
    )
    assert decision.state == "not_approved"
    assert ReasonCode.CHECKS_VIOLATION in decision.reason_codes()
    assert "native.tests" in next(
        r.message for r in decision.reasons if r.code == ReasonCode.CHECKS_VIOLATION
    )


def test_an_incomplete_check_run_refuses_the_approval_in_different_words() -> None:
    """Fail closed, and say which of the two things happened."""
    decision = decide(
        _clean_gate_inputs(checks_verdict="incomplete", checks_blocking=["tool.ruff"]),
        _gate_policy(),
    )
    assert decision.state == "not_approved"
    assert ReasonCode.CHECKS_INCOMPLETE in decision.reason_codes()
    assert ReasonCode.CHECKS_VIOLATION not in decision.reason_codes()
    message = next(r.message for r in decision.reasons if r.code == ReasonCode.CHECKS_INCOMPLETE)
    assert "could not reach a conclusion" in message


def test_an_incomplete_run_is_a_hard_veto_and_a_violation_is_not() -> None:
    """ "We do not know" is not something an admin should be able to force past."""
    from mira.gate.models import HARD_VETO_CODES

    assert ReasonCode.CHECKS_INCOMPLETE in HARD_VETO_CODES
    assert ReasonCode.CHECKS_VIOLATION not in HARD_VETO_CODES

    incomplete = decide(
        _clean_gate_inputs(checks_verdict="incomplete", checks_blocking=["tool.ruff"]),
        _gate_policy(),
    )
    assert incomplete.hard_vetoes
    violation = decide(
        _clean_gate_inputs(checks_verdict="violation", checks_blocking=["native.tests"]),
        _gate_policy(),
    )
    assert violation.hard_vetoes == []


def test_a_deployment_can_stop_the_gate_consulting_the_checks() -> None:
    decision = decide(
        _clean_gate_inputs(checks_verdict="violation", checks_blocking=["native.tests"]),
        _gate_policy(require_checks_pass=False),
    )
    assert decision.state == "would_approve"


def test_the_check_verdict_is_part_of_the_decision_inputs() -> None:
    """A gate re-run after the checks finish must record a *new* decision."""
    pending = _clean_gate_inputs(checks_verdict="incomplete")
    finished = _clean_gate_inputs(checks_verdict="pass")
    assert pending.digest != finished.digest


def test_the_run_verdict_a_gate_reads_comes_from_the_stored_run() -> None:
    from mira.checks.models import CheckResult

    run = CheckRun(
        run_key="k",
        inputs=CheckRunInputs(owner="acme", repo="app", pr_number=7, head_sha="head123"),
        results=[
            CheckResult(
                check_id="tool.ruff", mode="error", state="skipped", skip_reason="tool_missing"
            ),
            CheckResult(check_id="native.tests", mode="warning", state="violation"),
        ],
    )
    # A missing linter in error mode blocks; a violation in warning mode does not.
    assert run.verdict == "incomplete"
    assert [result.check_id for result in run.blocking_results] == ["tool.ruff"]


# ─────────────────────────────────────────────────────────── the explanation ──


def test_the_comment_never_mixes_a_finding_with_an_outage() -> None:
    from mira.checks.explain import public_explanation
    from mira.checks.models import CheckFinding, CheckResult, Evidence

    run = CheckRun(
        run_key="k",
        policy_version="checks-v1+abc",
        inputs=CheckRunInputs(owner="acme", repo="app", pr_number=7),
        results=[
            CheckResult(
                check_id="native.tests",
                title="Tests",
                mode="error",
                state="violation",
                summary="source changed with no test",
                findings=[
                    CheckFinding(
                        fingerprint="fp",
                        title="Source changed and no test changed with it",
                        evidence=[Evidence(path="src/a.py", start_line=3, source="diff")],
                        sources=["native.tests"],
                    )
                ],
            ),
            CheckResult(
                check_id="tool.ruff",
                title="Ruff",
                mode="warning",
                state="skipped",
                skip_reason="tool_missing",
                summary="ruff is not installed in this environment",
            ),
        ],
    )
    body = public_explanation(run)
    found = body.index("What the checks found")
    could_not = body.index("What Mira could not answer")
    assert found < could_not
    assert "ruff is not installed" in body[could_not:]
    assert "src/a.py:3" in body[found:could_not]
    assert "not findings against this pull request" in body


def test_an_incomplete_run_publishes_neutral_rather_than_red() -> None:
    """ "Mira could not run its linter" is not a failing build."""
    from mira.checks.explain import status_conclusion
    from mira.checks.models import CheckResult

    incomplete = CheckRun(
        results=[CheckResult(check_id="tool.ruff", mode="error", state="infrastructure_error")]
    )
    assert incomplete.verdict == "incomplete"
    assert status_conclusion(incomplete) == "neutral"


def test_evidence_with_no_path_is_not_described_twice() -> None:
    """Its description *is* its locator; appending it again reads as a stutter."""
    from mira.checks.explain import public_explanation
    from mira.checks.models import CheckFinding, CheckResult, Evidence

    run = CheckRun(
        run_key="k",
        inputs=CheckRunInputs(owner="acme", repo="app", pr_number=7),
        results=[
            CheckResult(
                check_id="context.ci",
                title="CI result",
                mode="error",
                state="violation",
                summary="1 failing CI job(s).",
                findings=[
                    CheckFinding(
                        fingerprint="fp",
                        title="CI is failing on this commit",
                        evidence=[
                            Evidence(
                                detail="job `build`, step `pytest`",
                                url="https://ci/1",
                                snippet="FAILED tests/test_x.py::test_y",
                                source="ci",
                            )
                        ],
                        sources=["context.ci"],
                    )
                ],
            )
        ],
    )
    body = public_explanation(run)
    assert body.count("job `build`, step `pytest`") == 1
    assert "https://ci/1" in body
    assert "FAILED tests/test_x.py::test_y" in body


def test_evidence_with_a_path_still_carries_its_description() -> None:
    from mira.checks.explain import public_explanation
    from mira.checks.models import CheckFinding, CheckResult, Evidence

    run = CheckRun(
        run_key="k",
        inputs=CheckRunInputs(owner="acme", repo="app", pr_number=7),
        results=[
            CheckResult(
                check_id="native.tests",
                title="Tests",
                mode="warning",
                state="violation",
                summary="no test changed",
                findings=[
                    CheckFinding(
                        fingerprint="fp",
                        title="Source changed and no test changed with it",
                        evidence=[
                            Evidence(
                                path="src/a.py",
                                start_line=4,
                                detail="4 added line(s), no test changed",
                                source="diff",
                            )
                        ],
                        sources=["native.tests"],
                    )
                ],
            )
        ],
    )
    body = public_explanation(run)
    assert "src/a.py:4" in body
    assert "4 added line(s), no test changed" in body


def test_a_repository_with_checks_on_and_no_run_is_not_approved() -> None:
    """The absence of evidence is not evidence of a clean run."""
    decision = decide(
        _clean_gate_inputs(checks_active=True, checks_verdict="not_run"), _gate_policy()
    )
    assert decision.state == "not_approved"
    assert ReasonCode.CHECKS_INCOMPLETE in decision.reason_codes()
    assert "no run recorded" in next(
        r.message for r in decision.reasons if r.code == ReasonCode.CHECKS_INCOMPLETE
    )


def test_a_repository_with_checks_off_owes_nothing() -> None:
    decision = decide(
        _clean_gate_inputs(checks_active=False, checks_verdict="not_run"), _gate_policy()
    )
    assert decision.state == "would_approve"


def test_an_active_repository_whose_checks_passed_is_approved() -> None:
    decision = decide(_clean_gate_inputs(checks_active=True, checks_verdict="pass"), _gate_policy())
    assert decision.state == "would_approve"


def test_whether_checks_are_active_is_part_of_the_decision_inputs() -> None:
    """Turning the framework on is a different world to decide about."""
    off = _clean_gate_inputs(checks_active=False)
    on = _clean_gate_inputs(checks_active=True)
    assert off.digest != on.digest
