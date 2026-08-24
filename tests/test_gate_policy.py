"""Phase 4 — the decision itself, with no platform and no store in sight.

The acceptance criterion these defend: *nothing uncertain and nothing protected
can ever come out as an approval*. So the tests are written the same way the
decision function is — as a matrix over the facts, asserting the state and the
reason code, never just "not approved".
"""

from __future__ import annotations

import pytest

from mira.config import GateConfig, GateRepoPolicy, MiraConfig
from mira.gate import codeowners as co
from mira.gate import paths
from mira.gate.capabilities import (
    FORGEJO_CAPABILITIES,
    GITHUB_CAPABILITIES,
    GITLAB_CAPABILITIES,
    NO_CAPABILITIES,
    GateCapabilities,
    for_platform,
    narrow,
)
from mira.gate.decide import decide
from mira.gate.explain import (
    admin_explanation,
    public_explanation,
    status_conclusion,
    would_have_approved,
)
from mira.gate.models import CIState, GateInputs, ReasonCode
from mira.gate.policy import resolve_policy
from mira.gate.risk import score


def _policy(**overrides):
    """An effective policy that would approve a clean PR, plus overrides."""
    overrides.setdefault("mode", "enforce")
    return resolve_policy(GateConfig(**overrides), "acme", "app")


def _clean_inputs(**overrides) -> GateInputs:
    """A pull request with nothing wrong with it."""
    base = {
        "platform": "github",
        "owner": "acme",
        "repo": "app",
        "pr_number": 7,
        "pr_url": "https://github.com/acme/app/pull/7",
        "pr_author": "alice",
        "base_branch": "main",
        "head_branch": "feature",
        "head_sha": "deadbeef",
        "author_association": "MEMBER",
        "changed_paths": ["src/a.py", "src/b.py"],
        "changed_files": 2,
        "added_lines": 20,
        "deleted_lines": 5,
        "ci": CIState(state="success", total=3),
        "review_complete": True,
        "index_ready": True,
        "bot_login": "miracodeai",
    }
    base.update(overrides)
    return GateInputs(**base)


def _codes(decision) -> set[str]:
    return set(decision.reason_codes())


# ─────────────────────────────────────────────────────────── path matching ──


@pytest.mark.parametrize(
    "pattern,path,expected",
    [
        ("*.pem", "secrets/prod.pem", True),
        ("*.pem", "prod.pem", True),
        ("*.pem", "prod.pem.txt", False),
        ("/deploy/*.yaml", "deploy/app.yaml", True),
        ("/deploy/*.yaml", "deploy/nested/app.yaml", False),
        ("infra/**", "infra/main.tf", True),
        ("infra/**", "infra", True),
        ("infra/**", "infrastructure/main.tf", False),
        ("infra/", "infra/a/b/c.tf", True),
        ("**/migrations/**", "app/db/migrations/001.sql", True),
        ("**/migrations/**", "migrations/001.sql", True),
        ("**/migrations/**", "app/migrations", False),
        (".github/workflows/**", ".github/workflows/ci.yml", True),
        ("src/*.py", "src/a/b.py", False),
    ],
)
def test_path_patterns_are_gitignore_shaped(pattern: str, path: str, expected: bool) -> None:
    assert paths.matches(path, pattern) is expected


def test_windows_separators_normalize() -> None:
    assert paths.matches("infra\\main.tf", "infra/**")


def test_invalid_pattern_fails_at_config_load_not_at_decision_time() -> None:
    with pytest.raises(paths.PatternError):
        paths.compile_pattern("bad[")
    with pytest.raises(Exception, match="unterminated character class"):
        GateConfig(protected_paths=["bad["])


def test_match_any_names_the_rule_that_protected_a_file() -> None:
    assert paths.match_any("infra/main.tf", ["docs/**", "infra/**"]) == "infra/**"
    assert paths.match_any("README.md", ["infra/**"]) == ""


# ────────────────────────────────────────────────────────────── CODEOWNERS ──


def test_codeowners_last_match_wins() -> None:
    parsed = co.parse("*       @team\nsrc/api/**  @api-team\n")
    assert parsed.status == "ok"
    assert parsed.owners_for("README.md") == ("@team",)
    assert parsed.owners_for("src/api/routes.py") == ("@api-team",)


def test_codeowners_explicit_unset_rule_is_honoured() -> None:
    parsed = co.parse("*  @team\ndocs/**\n")
    assert parsed.owners_for("docs/guide.md") == ()
    assert parsed.owned_paths(["docs/guide.md", "src/a.py"]) == ["src/a.py"]


def test_codeowners_gitlab_sections_are_skipped_not_rejected() -> None:
    parsed = co.parse("[Backend][2]\nsrc/**  @backend\n^[Optional]\ndocs/**  @docs\n")
    assert parsed.status == "ok"
    assert parsed.owners_for("src/a.py") == ("@backend",)


def test_codeowners_that_cannot_be_parsed_is_unreadable() -> None:
    parsed = co.parse("src/**  not-an-owner\n")
    assert parsed.status == "unreadable"
    assert "not-an-owner" in parsed.error


def test_codeowners_accepts_emails_and_teams() -> None:
    parsed = co.parse("src/**  @org/team  dev@example.com\n")
    assert parsed.status == "ok"
    assert parsed.owners_for("src/a.py") == ("@org/team", "dev@example.com")


# ────────────────────────────────────────────────────── policy resolution ──


def test_kill_switch_disables_every_repository() -> None:
    config = GateConfig(
        mode="enforce",
        kill_switch=True,
        repositories={"acme/app": GateRepoPolicy(mode="enforce", enabled=True)},
    )
    policy = resolve_policy(config, "acme", "app")
    assert policy.mode == "off"
    assert policy.enabled is False
    assert policy.active is False


def test_per_repo_policy_overrides_and_inherits() -> None:
    config = GateConfig(
        mode="shadow",
        max_changed_files=20,
        blocked_labels=["hold"],
        repositories={
            "ACME/App": GateRepoPolicy(mode="enforce", max_changed_files=3, blocked_labels=[])
        },
    )
    policy = resolve_policy(config, "acme", "app")
    assert policy.mode == "enforce"
    assert policy.max_changed_files == 3
    # An explicitly empty list overrides; it is not read as "inherit".
    assert policy.blocked_labels == ()
    other = resolve_policy(config, "acme", "other")
    assert other.mode == "shadow"
    assert other.blocked_labels == ("hold",)


def test_extra_protected_paths_add_to_the_effective_list() -> None:
    config = GateConfig(
        protected_paths=["infra/**"],
        extra_protected_paths=["deploy/**"],
        repositories={"acme/app": GateRepoPolicy(extra_protected_paths=["charts/**"])},
    )
    policy = resolve_policy(config, "acme", "app")
    assert policy.protected_paths == ("infra/**", "deploy/**", "charts/**")


def test_policy_version_changes_when_policy_changes() -> None:
    first = resolve_policy(GateConfig(mode="shadow"), "acme", "app")
    second = resolve_policy(GateConfig(mode="shadow", risk_threshold=5), "acme", "app")
    cosmetic = resolve_policy(GateConfig(mode="shadow", comment=True), "acme", "app")
    assert first.version != second.version
    # How a decision is *announced* is not part of what it decided.
    assert first.version == cosmetic.version


def test_the_shipped_default_is_off() -> None:
    assert MiraConfig().gate.mode == "off"
    assert MiraConfig().gate.kill_switch is False
    assert resolve_policy(MiraConfig().gate, "acme", "app").active is False


# ────────────────────────────────────────────────────────────────── risk ──


def test_a_clean_small_pr_scores_zero() -> None:
    total, factors = score(_clean_inputs(), GateConfig().weights)
    assert total == 0
    assert factors == []


def test_risk_is_deterministic_and_integral() -> None:
    inputs = _clean_inputs(
        changed_files=40, added_lines=900, deleted_lines=300, open_warnings=3, open_findings=3
    )
    first = score(inputs, GateConfig().weights)
    second = score(inputs, GateConfig().weights)
    assert first == second
    assert isinstance(first[0], int)
    assert all(isinstance(factor.points, int) for factor in first[1])


def test_every_factor_carries_its_own_explanation() -> None:
    inputs = _clean_inputs(
        changed_files=30,
        added_lines=2000,
        protected_matches=["infra/main.tf"],
        ci=CIState(state="failure", failing=["build"]),
        author_association="FIRST_TIME_CONTRIBUTOR",
        review_skipped_paths=["src/huge.py"],
        index_ready=False,
        open_warnings=2,
        open_security=1,
        open_findings=3,
    )
    total, factors = score(inputs, GateConfig().weights)
    assert total == 100  # clamped
    codes = {factor.code for factor in factors}
    assert {
        "size_files",
        "size_lines",
        "protected_path",
        "ci_not_success",
        "first_time_contributor",
        "unreviewed_paths",
        "index_not_ready",
        "warning_findings",
        "security_findings",
    } <= codes
    assert all(factor.detail for factor in factors)


def test_generated_files_do_not_inflate_the_size_score() -> None:
    weights = GateConfig().weights
    plain = _clean_inputs(changed_files=12, changed_paths=["a"] * 12)
    with_lock = _clean_inputs(
        changed_files=12,
        changed_paths=["a"] * 12,
        generated_paths=["uv.lock", "package-lock.json", "go.sum", "yarn.lock"],
    )
    assert score(with_lock, weights)[0] < score(plain, weights)[0]


# ─────────────────────────────────────────────── the eligibility matrix ──


@pytest.mark.parametrize(
    "overrides,policy_kwargs,expected_code",
    [
        ({"draft": True}, {}, ReasonCode.PR_DRAFT),
        ({"pr_author": "miracodeai[bot]"}, {}, ReasonCode.SELF_AUTHORED),
        (
            {"base_branch": "release"},
            {"allowed_base_branches": ["main"]},
            ReasonCode.BASE_BRANCH_OUT_OF_SCOPE,
        ),
        ({}, {"allowed_authors": ["bob"]}, ReasonCode.AUTHOR_NOT_IN_ALLOWLIST),
        ({}, {"required_labels": ["ready"]}, ReasonCode.MISSING_REQUIRED_LABEL),
        (
            {"changed_paths": ["uv.lock"], "changed_files": 1, "generated_paths": ["uv.lock"]},
            {},
            ReasonCode.GENERATED_ONLY_DIFF,
        ),
        ({"human_states": {"carol": "APPROVED"}}, {}, ReasonCode.HUMAN_ALREADY_APPROVED),
    ],
)
def test_out_of_scope_prs_are_skipped_with_a_reason(
    overrides: dict, policy_kwargs: dict, expected_code: str
) -> None:
    decision = decide(
        _clean_inputs(**overrides), _policy(**policy_kwargs), capabilities=GITHUB_CAPABILITIES
    )
    assert decision.state == "skipped"
    assert expected_code in _codes(decision)


@pytest.mark.parametrize(
    "overrides,policy_kwargs,expected_code",
    [
        ({"labels": ["do-not-merge"]}, {}, ReasonCode.BLOCKED_LABEL),
        ({}, {"blocked_base_branches": ["main"]}, ReasonCode.BLOCKED_BASE_BRANCH),
        ({}, {"blocked_authors": ["alice"]}, ReasonCode.AUTHOR_BLOCKED),
        ({"author_association": "UNKNOWN"}, {}, ReasonCode.AUTHOR_ASSOCIATION_UNKNOWN),
        (
            {"author_association": "FIRST_TIME_CONTRIBUTOR"},
            {},
            ReasonCode.AUTHOR_ASSOCIATION_INSUFFICIENT,
        ),
        ({"changed_files": 99}, {}, ReasonCode.PR_TOO_MANY_FILES),
        ({"added_lines": 9000}, {}, ReasonCode.PR_TOO_MANY_LINES),
        ({"protected_matches": [".github/workflows/ci.yml"]}, {}, ReasonCode.PROTECTED_PATH),
        ({"ci": CIState(state="failure", failing=["build"])}, {}, ReasonCode.CI_FAILING),
        ({"ci": CIState(state="pending", pending=["build"])}, {}, ReasonCode.CI_PENDING),
        ({"ci": CIState(state="unknown")}, {}, ReasonCode.CI_UNKNOWN),
        ({"ci": CIState(state="none")}, {}, ReasonCode.CI_UNKNOWN),
        (
            {"review_complete": False, "review_skipped_paths": ["src/huge.py"]},
            {},
            ReasonCode.REVIEW_INCOMPLETE,
        ),
        ({"review_failed": "LLM timed out"}, {}, ReasonCode.REVIEW_FAILED),
        ({"index_ready": False}, {}, ReasonCode.INDEX_NOT_READY),
        ({"open_blockers": 1}, {}, ReasonCode.OPEN_BLOCKER),
        ({"worst_severity": "warning"}, {}, ReasonCode.SEVERITY_ABOVE_CEILING),
        ({"human_states": {"carol": "CHANGES_REQUESTED"}}, {}, ReasonCode.HUMAN_CHANGES_REQUESTED),
    ],
)
def test_disqualifying_facts_never_approve(
    overrides: dict, policy_kwargs: dict, expected_code: str
) -> None:
    decision = decide(
        _clean_inputs(**overrides), _policy(**policy_kwargs), capabilities=GITHUB_CAPABILITIES
    )
    assert decision.state == "not_approved"
    assert expected_code in _codes(decision)


def test_the_matrix_reports_every_problem_not_just_the_first() -> None:
    decision = decide(
        _clean_inputs(
            labels=["do-not-merge"],
            ci=CIState(state="failure", failing=["build"]),
            open_blockers=2,
            protected_matches=["infra/main.tf"],
        ),
        _policy(),
        capabilities=GITHUB_CAPABILITIES,
    )
    assert {
        ReasonCode.BLOCKED_LABEL,
        ReasonCode.CI_FAILING,
        ReasonCode.OPEN_BLOCKER,
        ReasonCode.PROTECTED_PATH,
    } <= _codes(decision)


def test_a_protected_path_is_never_approved_however_clean_the_pr() -> None:
    decision = decide(
        _clean_inputs(protected_matches=[".github/workflows/ci.yml"]),
        # Everything else relaxed as far as the policy allows.
        _policy(risk_threshold=100, require_ci_success=False, require_index_ready=False),
        capabilities=GITHUB_CAPABILITIES,
    )
    assert decision.state == "not_approved"
    assert ReasonCode.PROTECTED_PATH in _codes(decision)
    assert decision.hard_vetoes


def test_an_open_blocker_outranks_a_perfect_score() -> None:
    decision = decide(
        _clean_inputs(open_blockers=1),
        _policy(risk_threshold=100),
        capabilities=GITHUB_CAPABILITIES,
    )
    assert decision.state == "not_approved"
    assert ReasonCode.OPEN_BLOCKER in _codes(decision)


def test_risk_above_the_threshold_blocks_on_its_own() -> None:
    decision = decide(
        _clean_inputs(changed_files=18, added_lines=400),
        _policy(risk_threshold=1),
        capabilities=GITHUB_CAPABILITIES,
    )
    assert decision.state == "not_approved"
    assert ReasonCode.RISK_ABOVE_THRESHOLD in _codes(decision)


def test_risk_is_scored_even_for_a_disqualified_pr() -> None:
    """A shadow rollout has to be able to tune the threshold from every PR."""
    decision = decide(
        _clean_inputs(labels=["do-not-merge"], changed_files=30),
        _policy(),
        capabilities=GITHUB_CAPABILITIES,
    )
    assert decision.state == "not_approved"
    assert decision.risk_score > 0
    assert decision.factors


# ─────────────────────────────────────────────────────────── CODEOWNERS mode ──


def test_codeowners_block_mode_refuses_an_owned_path() -> None:
    decision = decide(
        _clean_inputs(codeowner_matches=["src/api/routes.py"], codeowners_status="ok"),
        _policy(codeowners="block"),
        capabilities=GITHUB_CAPABILITIES,
    )
    assert decision.state == "not_approved"
    assert ReasonCode.CODEOWNERS_PATH in _codes(decision)


def test_codeowners_risk_mode_scores_but_does_not_disqualify() -> None:
    decision = decide(
        _clean_inputs(codeowner_matches=["src/api/routes.py"], codeowners_status="ok"),
        _policy(codeowners="risk", risk_threshold=100),
        capabilities=GITHUB_CAPABILITIES,
    )
    assert decision.state == "would_approve"
    assert any(factor.code == "codeowner_path" for factor in decision.factors)


def test_an_unreadable_codeowners_is_conservative() -> None:
    decision = decide(
        _clean_inputs(codeowners_status="unreadable"),
        _policy(codeowners="block"),
        capabilities=GITHUB_CAPABILITIES,
    )
    assert decision.state == "not_approved"
    assert ReasonCode.CODEOWNERS_UNREADABLE in _codes(decision)


def test_codeowners_off_does_not_consult_ownership() -> None:
    """With the integration off, ownership is never read, so it never scores.

    `gather_inputs` leaves `codeowner_matches` empty and the status at
    `not_checked`; the decision is then indistinguishable from one on a
    repository with no CODEOWNERS at all, which is the point of "off".
    """
    decision = decide(
        _clean_inputs(codeowners_status="not_checked"),
        _policy(codeowners="off"),
        capabilities=GITHUB_CAPABILITIES,
    )
    assert decision.state == "would_approve"
    assert not any(factor.code == "codeowner_path" for factor in decision.factors)


# ───────────────────────────────────────────────────── modes and capabilities ──


def test_shadow_mode_never_produces_an_approval() -> None:
    decision = decide(_clean_inputs(), _policy(mode="shadow"), capabilities=GITHUB_CAPABILITIES)
    assert decision.state == "would_approve"
    assert ReasonCode.SHADOW_MODE in _codes(decision)


def test_off_mode_skips_without_reading_the_pr() -> None:
    decision = decide(_clean_inputs(labels=["do-not-merge"]), _policy(mode="off"))
    assert decision.state == "skipped"
    assert _codes(decision) == {ReasonCode.GATE_OFF}


def test_enforce_stops_at_would_approve_until_the_platform_confirms() -> None:
    """`decide` never returns `approved`; only a delivered approval does."""
    decision = decide(_clean_inputs(), _policy(mode="enforce"), capabilities=GITHUB_CAPABILITIES)
    assert decision.state == "would_approve"
    assert decision.delivery_state == "pending"


def test_a_provider_that_cannot_approve_degrades_explicitly() -> None:
    decision = decide(_clean_inputs(), _policy(mode="enforce"), capabilities=NO_CAPABILITIES)
    assert decision.state == "would_approve"
    assert ReasonCode.PROVIDER_CANNOT_APPROVE in _codes(decision)


def test_gitlab_cannot_request_changes_and_says_so() -> None:
    decision = decide(
        _clean_inputs(open_blockers=1),
        _policy(mode="enforce", request_changes_on_blockers=True),
        capabilities=GITLAB_CAPABILITIES,
    )
    assert decision.state == "not_approved"
    assert decision.request_changes is False


def test_request_changes_fires_only_for_blockers_in_enforce_mode() -> None:
    enforcing = decide(
        _clean_inputs(open_blockers=1),
        _policy(mode="enforce", request_changes_on_blockers=True),
        capabilities=GITHUB_CAPABILITIES,
    )
    assert enforcing.request_changes is True

    shadow = decide(
        _clean_inputs(open_blockers=1),
        _policy(mode="shadow", request_changes_on_blockers=True),
        capabilities=GITHUB_CAPABILITIES,
    )
    assert shadow.request_changes is False

    warning_only = decide(
        _clean_inputs(worst_severity="warning"),
        _policy(mode="enforce", request_changes_on_blockers=True),
        capabilities=GITHUB_CAPABILITIES,
    )
    assert warning_only.request_changes is False


def test_request_changes_never_overwrites_a_human_review() -> None:
    for state in ("APPROVED", "CHANGES_REQUESTED"):
        decision = decide(
            _clean_inputs(open_blockers=1, human_states={"carol": state}),
            _policy(mode="enforce", request_changes_on_blockers=True),
            capabilities=GITHUB_CAPABILITIES,
        )
        assert decision.request_changes is False


def test_capabilities_can_narrow_but_never_widen() -> None:
    liar = GateCapabilities(provider="gitlab", can_approve=True, can_request_changes=True)
    assert narrow(liar, for_platform("gitlab")).can_request_changes is False
    modest = GateCapabilities(provider="github", can_approve=False, can_publish_status=True)
    narrowed = narrow(modest, GITHUB_CAPABILITIES)
    assert narrowed.can_approve is False
    assert narrowed.can_publish_status is True
    assert FORGEJO_CAPABILITIES.can_request_changes is True


# ───────────────────────────────────────────────────────────── explanations ──


def test_the_dry_run_explains_both_directions() -> None:
    approving = decide(_clean_inputs(), _policy(mode="shadow"), capabilities=GITHUB_CAPABILITIES)
    text = public_explanation(approving)
    assert "would approve" in text.lower()
    assert "shadow mode" in text.lower()

    refusing = decide(
        _clean_inputs(open_blockers=2, ci=CIState(state="failure", failing=["build"])),
        _policy(mode="shadow"),
        capabilities=GITHUB_CAPABILITIES,
    )
    refusal = public_explanation(refusing)
    assert "Why not:" in refusal
    assert "blocker finding(s) are still open" in refusal
    assert "CI is failing" in refusal


def test_the_admin_explanation_shows_the_policy_internals() -> None:
    decision = decide(
        _clean_inputs(changed_files=30), _policy(mode="shadow"), capabilities=GITHUB_CAPABILITIES
    )
    text = admin_explanation(decision)
    assert "Risk factors" in text
    assert "size_files" in text
    assert decision.policy_version in text
    assert "Provider capabilities" in text


def test_only_a_delivered_approval_is_a_green_status() -> None:
    would = decide(_clean_inputs(), _policy(mode="shadow"), capabilities=GITHUB_CAPABILITIES)
    assert status_conclusion(would) == "neutral"
    would.state = "approved"
    assert status_conclusion(would) == "success"


def test_candidate_approvals_are_countable_for_the_dry_run() -> None:
    approving = decide(_clean_inputs(), _policy(mode="shadow"), capabilities=GITHUB_CAPABILITIES)
    refusing = decide(
        _clean_inputs(open_blockers=1), _policy(mode="shadow"), capabilities=GITHUB_CAPABILITIES
    )
    assert would_have_approved(approving) is True
    assert would_have_approved(refusing) is False


# ─────────────────────────────────────────────────────────── decision keys ──


def test_the_same_facts_and_policy_produce_the_same_key() -> None:
    first = decide(_clean_inputs(), _policy(), capabilities=GITHUB_CAPABILITIES)
    second = decide(_clean_inputs(), _policy(), capabilities=GITHUB_CAPABILITIES)
    assert first.decision_key == second.decision_key


def test_changed_facts_produce_a_new_decision() -> None:
    pending = decide(
        _clean_inputs(ci=CIState(state="pending", pending=["build"])),
        _policy(),
        capabilities=GITHUB_CAPABILITIES,
    )
    green = decide(_clean_inputs(), _policy(), capabilities=GITHUB_CAPABILITIES)
    assert pending.decision_key != green.decision_key


def test_a_changed_policy_produces_a_new_decision() -> None:
    lenient = decide(_clean_inputs(), _policy(risk_threshold=50), capabilities=GITHUB_CAPABILITIES)
    strict = decide(_clean_inputs(), _policy(risk_threshold=5), capabilities=GITHUB_CAPABILITIES)
    assert lenient.decision_key != strict.decision_key


def test_a_retried_review_does_not_look_like_a_new_world() -> None:
    """`review_id` changes on a retry; the facts do not."""
    first = decide(_clean_inputs(review_id=1), _policy(), capabilities=GITHUB_CAPABILITIES)
    retry = decide(_clean_inputs(review_id=2), _policy(), capabilities=GITHUB_CAPABILITIES)
    assert first.decision_key == retry.decision_key


# ───────────────────────────────── regressions from the pre-merge review ──


def test_an_empty_generated_list_means_empty() -> None:
    """`[]` is a statement, not a request to fall back to the built-ins.

    The same sentinel `protected_paths` uses: `null` inherits, `[]` is empty.
    A repository that declares nothing generated must not have `uv.lock`
    silently discounted from its size budget.
    """
    inherited = resolve_policy(GateConfig(mode="shadow"), "acme", "app")
    assert "uv.lock" in inherited.generated_paths

    explicit = resolve_policy(GateConfig(mode="shadow", generated_paths=[]), "acme", "app")
    assert explicit.generated_paths == ()

    custom = resolve_policy(GateConfig(mode="shadow", generated_paths=["vendor/**"]), "acme", "app")
    assert custom.generated_paths == ("vendor/**",)


def test_generated_lines_are_discounted_from_the_size_score() -> None:
    weights = GateConfig().weights
    lockfile = _clean_inputs(
        changed_files=2,
        changed_paths=["src/a.py", "uv.lock"],
        generated_paths=["uv.lock"],
        added_lines=4000,
        deleted_lines=3800,
        generated_lines=7794,
    )
    hand_written = _clean_inputs(
        changed_files=2,
        changed_paths=["src/a.py", "src/b.py"],
        added_lines=4000,
        deleted_lines=3800,
    )
    assert score(lockfile, weights)[0] < score(hand_written, weights)[0]
