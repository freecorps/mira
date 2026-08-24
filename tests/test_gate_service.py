"""Phase 4 — the gate end to end, against fake providers.

What these defend is the boundary the phase exists for: *a dry run never
approves anything, an unfinished evaluation never approves anything, and a
platform that cannot approve is never reported as having approved.*

The providers here are deliberately thin and rude — they raise, they refuse,
they hang — because every one of those is a real Tuesday and each has to land
on the same answer.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from mira.config import GateConfig, GateRepoPolicy, MiraConfig
from mira.exceptions import ProviderError
from mira.feedback.models import ReviewFinding
from mira.gate import service as gate_service
from mira.gate.capabilities import (
    FORGEJO_CAPABILITIES,
    GITHUB_CAPABILITIES,
    GITLAB_CAPABILITIES,
    NO_CAPABILITIES,
    GateCapabilities,
)
from mira.gate.models import CIState, ReasonCode, delivery_key
from mira.gate.service import OverrideDenied, apply_override
from mira.index.store import IndexStore


@pytest.fixture(autouse=True)
def isolated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MIRA_INDEX_DIR", str(tmp_path))
    monkeypatch.delenv("DATABASE_URL", raising=False)
    IndexStore.open("acme", "app").close()
    # The index-readiness probe reads the repo registry; a repo that is ready
    # is the uninteresting case, so it is the default here.
    monkeypatch.setattr(
        gate_service, "_index_ready", lambda owner, repo, platform: True, raising=True
    )


def _pr(**overrides) -> SimpleNamespace:
    base = {
        "owner": "acme",
        "repo": "app",
        "number": 7,
        "url": "https://github.com/acme/app/pull/7",
        "title": "Add a thing",
        "description": "",
        "base_branch": "main",
        "head_branch": "feature",
        "base_sha": "base",
        "head_sha": "head123",
        "platform": "github",
        "author": "alice",
        "draft": False,
    }
    base.update(overrides)
    return SimpleNamespace(**base)


class FakeProvider:
    """A provider that behaves, until a test asks it not to."""

    def __init__(
        self,
        *,
        capabilities: GateCapabilities = GITHUB_CAPABILITIES,
        labels: list[str] | None = None,
        association: str = "MEMBER",
        ci: CIState | None = None,
        review_states: dict[str, str] | None = None,
        codeowners: tuple[str, str] = ("", ""),
        verdict_result: bool = True,
        raise_on: str = "",
        status_raises: bool = False,
    ) -> None:
        self._capabilities = capabilities
        self._labels = labels or []
        self._association = association
        self._ci = ci or CIState(state="success", total=2)
        self._review_states = review_states or {}
        self._codeowners = codeowners
        self._verdict_result = verdict_result
        self._raise_on = raise_on
        self._status_raises = status_raises
        self.verdicts: list[tuple[str, str]] = []
        self.statuses: list[dict] = []
        self.comments: list[str] = []

    def _maybe_raise(self, name: str) -> None:
        if self._raise_on == name:
            raise ProviderError(f"{name} is unavailable")

    def gate_capabilities(self) -> GateCapabilities:
        return self._capabilities

    async def get_pr_labels(self, pr_info):
        self._maybe_raise("labels")
        return list(self._labels)

    async def get_author_association(self, pr_info):
        self._maybe_raise("association")
        return self._association

    async def get_ci_state(self, pr_info):
        self._maybe_raise("ci")
        return self._ci

    async def get_review_states(self, pr_info):
        self._maybe_raise("review_states")
        return dict(self._review_states)

    async def get_pr_change_stats(self, pr_info):
        self._maybe_raise("change_stats")
        return ["src/a.py", "src/b.py"], 20, 4

    async def get_codeowners(self, pr_info):
        self._maybe_raise("codeowners")
        return self._codeowners

    async def submit_verdict(self, pr_info, event, body):
        self._maybe_raise("verdict")
        self.verdicts.append((event, body))
        return self._verdict_result

    async def publish_gate_status(self, pr_info, *, context, conclusion, title, summary, **kw):
        if self._status_raises:
            raise ProviderError("checks:write is not granted")
        self.statuses.append({"context": context, "conclusion": conclusion, "title": title})
        return "check-1"

    async def find_bot_comment(self, pr_info, marker):
        return None

    async def post_comment(self, pr_info, body):
        self.comments.append(body)

    async def update_comment(self, pr_info, comment_id, body):
        self.comments.append(body)


def _config(**gate_overrides) -> MiraConfig:
    return MiraConfig(gate=GateConfig(**gate_overrides))


def _decisions() -> list:
    store = IndexStore.open("acme", "app")
    try:
        return store.list_gate_decisions()
    finally:
        store.close()


async def _evaluate(provider, config, **kwargs):
    return await gate_service.evaluate(
        provider, _pr(**kwargs.pop("pr", {})), config=config, bot_name="miracodeai", **kwargs
    )


# ───────────────────────────────────────────────────────── the safe default ──


async def test_enforce_is_not_the_default_and_nothing_is_submitted() -> None:
    provider = FakeProvider()
    decision = await _evaluate(provider, MiraConfig())
    assert decision.state == "skipped"
    assert ReasonCode.GATE_OFF in decision.reason_codes()
    assert provider.verdicts == []
    assert provider.statuses == []


async def test_an_inactive_gate_fetches_nothing_from_the_platform() -> None:
    """Cost matters: an install that never turned the gate on must not pay."""
    provider = FakeProvider(raise_on="labels")
    decision = await _evaluate(provider, MiraConfig())
    # If any provider read had happened, `raise_on="labels"` would have made
    # this an `error` decision instead of a clean skip.
    assert decision.state == "skipped"


async def test_the_kill_switch_beats_every_per_repo_override() -> None:
    provider = FakeProvider()
    config = _config(
        mode="enforce",
        kill_switch=True,
        repositories={"acme/app": GateRepoPolicy(mode="enforce", enabled=True)},
    )
    decision = await _evaluate(provider, config)
    assert decision.state == "skipped"
    assert {ReasonCode.KILL_SWITCH, ReasonCode.REPO_DISABLED} & set(decision.reason_codes()) or (
        ReasonCode.REPO_DISABLED in decision.reason_codes()
    )
    assert provider.verdicts == []


# ─────────────────────────────────────────────────────────────── dry run ──


async def test_shadow_mode_records_a_decision_and_approves_nothing() -> None:
    provider = FakeProvider()
    decision = await _evaluate(provider, _config(mode="shadow"))
    assert decision.state == "would_approve"
    assert provider.verdicts == []
    # It still explains itself, on the PR and in the record.
    assert provider.statuses[0]["conclusion"] == "neutral"
    assert "would approve" in provider.statuses[0]["title"].lower()
    assert len(_decisions()) == 1


async def test_shadow_mode_explains_a_refusal_too() -> None:
    provider = FakeProvider(ci=CIState(state="failure", failing=["build"]))
    decision = await _evaluate(provider, _config(mode="shadow"))
    assert decision.state == "not_approved"
    assert ReasonCode.CI_FAILING in decision.reason_codes()
    assert provider.verdicts == []
    assert "CI is failing" in gate_service.explain(decision)


async def test_a_dry_run_review_delivers_nothing_at_all() -> None:
    provider = FakeProvider()
    decision = await gate_service.evaluate(
        provider,
        _pr(),
        config=_config(mode="enforce"),
        bot_name="miracodeai",
        deliver_side_effects=False,
    )
    assert decision.state == "would_approve"
    assert provider.verdicts == []
    assert provider.statuses == []


# ──────────────────────────────────────────────────────────────── enforce ──


async def test_enforce_approves_only_after_the_platform_confirms() -> None:
    provider = FakeProvider()
    decision = await _evaluate(provider, _config(mode="enforce"))
    assert decision.state == "approved"
    assert provider.verdicts[0][0] == "APPROVE"
    assert _decisions()[0].state == "approved"


async def test_a_refused_approval_stays_would_approve() -> None:
    provider = FakeProvider(verdict_result=False)
    decision = await _evaluate(provider, _config(mode="enforce"))
    assert decision.state == "would_approve"
    assert decision.delivery_state == "failed"
    assert ReasonCode.APPROVAL_REFUSED in decision.reason_codes()


async def test_a_failed_approval_call_stays_would_approve() -> None:
    provider = FakeProvider(raise_on="verdict")
    decision = await _evaluate(provider, _config(mode="enforce"))
    assert decision.state == "would_approve"
    assert decision.delivery_state == "failed"
    assert _decisions()[0].state == "would_approve"


async def test_request_changes_is_submitted_only_when_configured() -> None:
    store = IndexStore.open("acme", "app")
    store.close()
    quiet = FakeProvider()
    await _evaluate(quiet, _config(mode="enforce"))
    assert [event for event, _ in quiet.verdicts] == ["APPROVE"]


# ─────────────────────────────────────────────────────── provider degrading ──


# Reads everything the policy needs, but cannot record an approval — a token
# with reduced scopes, or a GitLab project whose tier has approvals switched off.
_READ_ONLY = GateCapabilities(
    provider="github",
    can_approve=False,
    can_publish_status=True,
    can_read_ci=True,
    can_read_association=True,
    can_read_labels=True,
)


@pytest.mark.parametrize(
    "capabilities,expected_state",
    [
        (GITHUB_CAPABILITIES, "approved"),
        (GITLAB_CAPABILITIES, "approved"),
        (FORGEJO_CAPABILITIES, "approved"),
        (_READ_ONLY, "would_approve"),
    ],
)
async def test_each_provider_either_approves_or_says_it_cannot(
    capabilities: GateCapabilities, expected_state: str
) -> None:
    provider = FakeProvider(capabilities=capabilities)
    decision = await _evaluate(provider, _config(mode="enforce"))
    assert decision.state == expected_state
    if expected_state == "would_approve":
        assert ReasonCode.PROVIDER_CANNOT_APPROVE in decision.reason_codes()
        assert provider.verdicts == []


async def test_a_provider_that_declares_nothing_never_approves() -> None:
    """The floor: no capabilities means no approval, by any route."""
    provider = FakeProvider(capabilities=NO_CAPABILITIES)
    decision = await _evaluate(provider, _config(mode="enforce"))
    assert decision.state != "approved"
    assert provider.verdicts == []


async def test_a_provider_that_cannot_read_labels_blocks_a_label_policy() -> None:
    """The policy asks a question this provider cannot answer."""
    provider = FakeProvider(
        capabilities=GateCapabilities(provider="github", can_approve=True, can_read_labels=False)
    )
    decision = await _evaluate(provider, _config(mode="enforce", blocked_labels=["hold"]))
    assert decision.state == "error"
    assert provider.verdicts == []


async def test_a_provider_that_cannot_read_ci_never_clears_the_ci_requirement() -> None:
    provider = FakeProvider(
        capabilities=GateCapabilities(
            provider="github", can_approve=True, can_read_labels=True, can_read_ci=False
        )
    )
    decision = await _evaluate(provider, _config(mode="enforce"))
    assert decision.state == "not_approved"
    assert ReasonCode.CI_UNKNOWN in decision.reason_codes()


async def test_a_provider_that_cannot_read_association_never_clears_it() -> None:
    provider = FakeProvider(
        capabilities=GateCapabilities(
            provider="github",
            can_approve=True,
            can_read_labels=True,
            can_read_ci=True,
            can_read_association=False,
        )
    )
    decision = await _evaluate(provider, _config(mode="enforce"))
    assert decision.state == "not_approved"
    assert ReasonCode.AUTHOR_ASSOCIATION_UNKNOWN in decision.reason_codes()


# ──────────────────────────────────────────────────────────────── failures ──


@pytest.mark.parametrize(
    "failing", ["labels", "association", "ci", "review_states", "change_stats"]
)
async def test_any_unreadable_input_is_an_error_and_never_an_approval(failing: str) -> None:
    provider = FakeProvider(raise_on=failing)
    decision = await _evaluate(provider, _config(mode="enforce"))
    assert decision.state == "error"
    assert decision.error
    assert provider.verdicts == []
    assert _decisions()[0].state == "error"


async def test_a_timeout_is_an_error_and_never_an_approval() -> None:
    class SlowProvider(FakeProvider):
        async def get_ci_state(self, pr_info):
            await asyncio.sleep(5)
            return CIState(state="success")

    provider = SlowProvider()
    decision = await _evaluate(provider, _config(mode="enforce", timeout_seconds=0.05))
    assert decision.state == "error"
    assert ReasonCode.EVALUATION_TIMEOUT in decision.reason_codes()
    assert provider.verdicts == []


async def test_an_llm_or_review_failure_never_approves() -> None:
    provider = FakeProvider()
    decision = await gate_service.evaluate(
        provider,
        _pr(),
        config=_config(mode="enforce"),
        bot_name="miracodeai",
        signal=gate_service.ReviewSignal(
            changed_paths=["src/a.py"],
            review_failed="the review model returned no parseable response",
        ),
    )
    assert decision.state == "not_approved"
    assert ReasonCode.REVIEW_FAILED in decision.reason_codes()
    assert provider.verdicts == []


async def test_an_unindexed_repository_never_approves(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(gate_service, "_index_ready", lambda owner, repo, platform: False)
    provider = FakeProvider()
    decision = await _evaluate(provider, _config(mode="enforce"))
    assert decision.state == "not_approved"
    assert ReasonCode.INDEX_NOT_READY in decision.reason_codes()
    assert provider.verdicts == []


async def test_a_failed_status_publish_does_not_undo_an_approval() -> None:
    provider = FakeProvider(status_raises=True)
    decision = await _evaluate(provider, _config(mode="enforce"))
    assert decision.state == "approved"
    assert provider.verdicts[0][0] == "APPROVE"


# ───────────────────────────────────────────── idempotency and concurrency ──


async def test_a_redelivered_webhook_approves_once() -> None:
    provider = FakeProvider()
    first = await _evaluate(provider, _config(mode="enforce"))
    second = await _evaluate(provider, _config(mode="enforce"))
    assert first.state == "approved"
    assert second.state == "approved"
    assert len(provider.verdicts) == 1
    assert len(_decisions()) == 1


async def test_a_second_decision_over_the_same_commit_approves_once() -> None:
    """CI going green makes a new decision, not a second approval."""
    pending = FakeProvider(ci=CIState(state="pending", pending=["build"]))
    first = await _evaluate(pending, _config(mode="enforce"))
    assert first.state == "not_approved"

    green = FakeProvider()
    second = await _evaluate(green, _config(mode="enforce"))
    assert second.state == "approved"
    assert len(green.verdicts) == 1

    # A third evaluation, over the same commit, must not approve again.
    third_provider = FakeProvider()
    third = await _evaluate(third_provider, _config(mode="enforce"))
    assert third.state == "approved"
    assert third_provider.verdicts == []
    assert len(_decisions()) == 2


async def test_concurrent_evaluations_produce_one_approval() -> None:
    providers = [FakeProvider() for _ in range(4)]
    results = await asyncio.gather(
        *(_evaluate(provider, _config(mode="enforce")) for provider in providers)
    )
    submitted = sum(len(provider.verdicts) for provider in providers)
    assert submitted == 1
    assert all(result.state == "approved" for result in results)


async def test_the_delivery_claim_is_recorded_for_the_audit() -> None:
    provider = FakeProvider()
    await _evaluate(provider, _config(mode="enforce"))
    store = IndexStore.open("acme", "app")
    try:
        record = store.get_gate_delivery(
            delivery_key(
                platform="github",
                owner="acme",
                repo="app",
                pr_number=7,
                head_sha="head123",
                kind="approval",
            )
        )
    finally:
        store.close()
    assert record["state"] == "delivered"
    assert record["attempts"] == 1


# ──────────────────────────────────────────────────────────────── overrides ──


async def _one_decision(mode: str = "shadow", **provider_kwargs):
    provider = FakeProvider(**provider_kwargs)
    await _evaluate(provider, _config(mode=mode))
    return _decisions()[0]


async def test_an_override_records_the_full_trail() -> None:
    decision = await _one_decision()
    result = apply_override(
        owner="acme",
        repo="app",
        platform="github",
        decision_id=decision.id,
        actor="admin",
        reason="Released after a manual review",
        new_state="not_approved",
        config=_config(mode="shadow"),
    )
    assert result.created is True
    assert result.decision.state == "not_approved"
    assert result.override["actor"] == "admin"
    assert result.override["previous_state"] == "would_approve"
    assert result.override["new_state"] == "not_approved"


async def test_an_override_needs_a_reason() -> None:
    decision = await _one_decision()
    with pytest.raises(OverrideDenied, match="reason"):
        apply_override(
            owner="acme",
            repo="app",
            platform="github",
            decision_id=decision.id,
            actor="admin",
            reason="   ",
            new_state="not_approved",
            config=_config(mode="shadow"),
        )


async def test_forcing_an_approval_is_its_own_opt_in() -> None:
    decision = await _one_decision()
    with pytest.raises(OverrideDenied, match="allow_approval_override"):
        apply_override(
            owner="acme",
            repo="app",
            platform="github",
            decision_id=decision.id,
            actor="admin",
            reason="ship it",
            new_state="approved",
            config=_config(mode="shadow"),
        )


async def test_no_override_can_approve_past_a_hard_veto() -> None:
    # A human asking for changes is not an opinion the gate formed and an
    # admin can wave off — it is one of the reasons this phase exists.
    blocked = await _one_decision(mode="shadow", review_states={"carol": "CHANGES_REQUESTED"})
    with pytest.raises(OverrideDenied, match="cannot be overridden"):
        apply_override(
            owner="acme",
            repo="app",
            platform="github",
            decision_id=blocked.id,
            actor="admin",
            reason="ship it",
            new_state="approved",
            config=_config(mode="shadow", allow_approval_override=True),
        )


async def test_overrides_can_be_disabled_entirely() -> None:
    decision = await _one_decision()
    with pytest.raises(OverrideDenied, match="disabled"):
        apply_override(
            owner="acme",
            repo="app",
            platform="github",
            decision_id=decision.id,
            actor="admin",
            reason="no",
            new_state="not_approved",
            config=_config(mode="shadow", allow_overrides=False),
        )


async def test_an_override_never_touches_the_platform() -> None:
    """Administering Mira and approving a pull request stay separate powers."""
    provider = FakeProvider()
    await _evaluate(provider, _config(mode="shadow"))
    decision = _decisions()[0]
    apply_override(
        owner="acme",
        repo="app",
        platform="github",
        decision_id=decision.id,
        actor="admin",
        reason="recorded by hand",
        new_state="approved",
        config=_config(mode="shadow", allow_approval_override=True),
    )
    assert provider.verdicts == []


# ──────────────────────────────────────────────────────── the engine's hook ──


async def test_the_engine_hands_the_gate_what_the_review_already_knows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No second diff fetch: the gate runs on the review's own facts."""
    from mira.core.engine import ReviewEngine
    from mira.models import ReviewComment, ReviewResult, Severity

    captured: dict = {}

    async def _fake_evaluate(provider, pr_info, *, config, bot_name, signal, deliver_side_effects):
        captured["signal"] = signal
        captured["deliver"] = deliver_side_effects
        return SimpleNamespace(state="would_approve")

    provider = FakeProvider(raise_on="change_stats")
    engine = ReviewEngine(
        config=_config(mode="shadow"),
        llm=SimpleNamespace(),
        provider=provider,
        bot_name="miracodeai",
    )
    engine._review_event_id = 42
    result = ReviewResult(
        comments=[
            ReviewComment(
                path="src/a.py",
                line=1,
                end_line=None,
                severity=Severity.WARNING,
                category="security",
                title="t",
                body="b",
                confidence=0.9,
            )
        ],
        total_paths=["src/a.py", "src/b.py"],
        skipped_paths=["src/huge.py"],
    )
    diff = "--- a/src/a.py\n+++ b/src/a.py\n@@ -1 +1,2 @@\n+added\n-removed\n"

    monkeypatch.setattr(gate_service, "evaluate", _fake_evaluate)
    await engine._run_merge_gate(_pr(), result, diff)

    signal = captured["signal"]
    assert signal.changed_paths == ["src/a.py", "src/b.py"]
    assert (signal.added_lines, signal.deleted_lines) == (1, 1)
    assert signal.open_warnings == 1
    assert signal.open_security == 1
    assert signal.open_findings == 1
    assert signal.worst_severity == "warning"
    assert signal.review_complete is False
    assert signal.skipped_paths == ["src/huge.py"]
    assert signal.review_id == 42
    assert captured["deliver"] is True


async def test_a_gate_failure_never_discards_a_published_review() -> None:
    from mira.core.engine import ReviewEngine
    from mira.models import ReviewResult

    engine = ReviewEngine(
        config=_config(mode="shadow"),
        llm=SimpleNamespace(),
        provider=FakeProvider(raise_on="labels"),
        bot_name="miracodeai",
    )
    # Must not raise: the review has already been posted by this point.
    await engine._run_merge_gate(_pr(), ReviewResult(), "")


async def test_both_entry_paths_score_the_same_pull_request_alike() -> None:
    """A gate woken by a finished CI run must not score a PR as spotless.

    The review path hands its own finding counts over; the CI-recheck path has
    only the store. Both have to land on the same score, or the same PR gets
    two different answers depending on what woke the gate up.
    """
    store = IndexStore.open("acme", "app")
    try:
        store.save_review_finding(
            ReviewFinding(
                id="f1",
                fingerprint="fp-f1",
                review_id=0,
                platform="github",
                owner="acme",
                repo="app",
                pr_number=7,
                pr_url="https://github.com/acme/app/pull/7",
                base_sha="base",
                head_sha="head123",
                path="src/a.py",
                start_line=1,
                end_line=1,
                symbol="",
                category="security",
                severity="warning",
                confidence=0.9,
                title="Unsafe call",
                body="",
                suggestion="",
                detector="llm",
                prompt_model="test",
                state="open",
            )
        )
    finally:
        store.close()

    from_review = await gate_service.evaluate(
        FakeProvider(),
        _pr(),
        config=_config(mode="shadow"),
        bot_name="miracodeai",
        signal=gate_service.ReviewSignal(
            changed_paths=["src/a.py", "src/b.py"],
            added_lines=20,
            deleted_lines=4,
            open_warnings=1,
            open_security=1,
            open_findings=1,
            worst_severity="warning",
        ),
    )
    # The recheck path passes no signal at all.
    from_recheck = await _evaluate(FakeProvider(), _config(mode="shadow"))

    assert from_review.risk_score == from_recheck.risk_score
    assert from_review.decision_key == from_recheck.decision_key
    assert {factor.code for factor in from_recheck.factors} >= {
        "warning_findings",
        "security_findings",
    }


async def test_a_dry_run_review_is_still_scored_on_its_own_findings() -> None:
    """A dry run never persists findings, so the store would say "clean"."""
    decision = await gate_service.evaluate(
        FakeProvider(),
        _pr(),
        config=_config(mode="shadow"),
        bot_name="miracodeai",
        signal=gate_service.ReviewSignal(
            changed_paths=["src/a.py"],
            open_blockers=1,
            open_findings=1,
            worst_severity="blocker",
        ),
        deliver_side_effects=False,
    )
    assert decision.state == "not_approved"
    assert ReasonCode.OPEN_BLOCKER in decision.reason_codes()
