"""Phase 6 — configuration: validation, three-layer inheritance, kill switch.

Two properties are defended here, and they are the ones a check framework is
worthless without.

**A configuration Mira cannot read fails at load, never at check time.** A bad
path glob, an analyser outside the allowlist, a mode nobody can parse: each one
has no safe runtime interpretation, because ignoring it silently removes a
check somebody believes is running and refusing everything takes the install
down on a typo.

**Inheritance is layered in one direction and resolves to one frozen answer.**
Global, then organisation, then repository. Modes merge so a repository need
not restate twelve settings to change one; lists replace so ``[]`` genuinely
means "none here"; tools merge by name so disabling one analyser does not drop
the others.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from mira.checks.config_models import (
    TOOL_ALLOWLIST,
    ChecksConfig,
    ChecksScopePolicy,
    CheckToolConfig,
    NaturalLanguageCheck,
)
from mira.checks.policy import resolve_policy
from mira.checks.registry import catalog, specs_for
from mira.config import MiraConfig


def _resolve(config: ChecksConfig, owner="acme", repo="app"):
    return resolve_policy(config, owner, repo)


# ────────────────────────────────────────────────────────────── validation ──


def test_an_unknown_analyser_is_refused_at_load() -> None:
    with pytest.raises(ValidationError) as exc:
        ChecksConfig(tools=[{"name": "curl"}])
    assert "allowlisted" in str(exc.value)


@pytest.mark.parametrize("name", sorted(TOOL_ALLOWLIST))
def test_every_allowlisted_analyser_has_an_adapter(name: str) -> None:
    from mira.checks.tools import adapter_for

    adapter = adapter_for(name)
    assert adapter is not None, f"{name} is allowlisted with no adapter to drive it"
    assert adapter.name == name


def test_an_absolute_tool_config_path_is_refused() -> None:
    with pytest.raises(ValidationError):
        ChecksConfig(tools=[{"name": "semgrep", "config_path": "/etc/passwd"}])


def test_a_traversing_tool_config_path_is_refused() -> None:
    with pytest.raises(ValidationError):
        ChecksConfig(tools=[{"name": "semgrep", "config_path": "../../secrets.yml"}])


def test_a_tool_argument_list_must_be_strings() -> None:
    with pytest.raises(ValidationError):
        ChecksConfig(tools=[{"name": "ruff", "args": [1, 2]}])


def test_an_unreadable_mode_is_refused_at_load() -> None:
    with pytest.raises(ValidationError):
        ChecksConfig(modes={"native.tests": "fatal"})


def test_an_unparseable_path_glob_is_refused_at_load() -> None:
    with pytest.raises(ValidationError):
        ChecksConfig(
            natural_language=[
                {"id": "r", "instruction": "x", "paths": ["src/**/["]},
            ]
        )


def test_an_empty_natural_language_instruction_is_refused() -> None:
    with pytest.raises(ValidationError):
        ChecksConfig(natural_language=[{"id": "r", "instruction": "   "}])


def test_a_duplicate_rule_id_is_refused() -> None:
    with pytest.raises(ValidationError) as exc:
        ChecksConfig(
            natural_language=[
                {"id": "r", "instruction": "a"},
                {"id": "r", "instruction": "b"},
            ]
        )
    assert "duplicate" in str(exc.value)


def test_a_rule_id_cannot_impersonate_a_native_check() -> None:
    """`nl.` namespacing is what stops a repository answering for `native.tests`."""
    rule = NaturalLanguageCheck(id="tests", instruction="anything")
    assert rule.check_id == "nl.tests"
    assert rule.check_id not in {spec.check_id for spec in specs_for(_resolve(ChecksConfig()))}


def test_an_invalid_reference_pattern_is_refused() -> None:
    with pytest.raises(ValidationError):
        ChecksConfig(ticket={"reference_patterns": ["ACME-(\\d+"]})


def test_checks_are_off_by_default() -> None:
    config = MiraConfig()
    assert config.checks.enabled is False
    assert config.checks.default_mode == "warning"
    assert _resolve(config.checks).active is False


# ───────────────────────────────────────────────────────────── inheritance ──


def test_an_organisation_entry_applies_to_every_repository_under_it() -> None:
    config = ChecksConfig(
        enabled=True,
        organizations={"acme": ChecksScopePolicy(default_mode="error")},
    )
    assert _resolve(config, "acme", "app").default_mode == "error"
    assert _resolve(config, "acme", "other").default_mode == "error"
    assert _resolve(config, "other", "app").default_mode == "warning"


def test_a_repository_entry_wins_over_its_organisation() -> None:
    config = ChecksConfig(
        enabled=True,
        organizations={"acme": ChecksScopePolicy(default_mode="error")},
        repositories={"acme/app": ChecksScopePolicy(default_mode="warning")},
    )
    assert _resolve(config, "acme", "app").default_mode == "warning"
    assert _resolve(config, "acme", "other").default_mode == "error"


def test_modes_merge_rather_than_replace() -> None:
    """A repository changing one check must not silently drop the others."""
    config = ChecksConfig(
        enabled=True,
        modes={"native.tests": "error", "native.docs": "error"},
        repositories={"acme/app": ChecksScopePolicy(modes={"native.docs": "off"})},
    )
    policy = _resolve(config)
    assert policy.mode_for("native.tests") == "error"
    assert policy.mode_for("native.docs") == "off"


def test_a_list_set_to_empty_genuinely_means_none_here() -> None:
    config = ChecksConfig(
        enabled=True,
        natural_language=[NaturalLanguageCheck(id="r", instruction="a rule")],
        repositories={"acme/app": ChecksScopePolicy(natural_language=[])},
    )
    assert _resolve(config, "acme", "app").natural_language == ()
    assert len(_resolve(config, "acme", "other").natural_language) == 1


def test_an_unset_list_inherits() -> None:
    config = ChecksConfig(
        enabled=True,
        natural_language=[NaturalLanguageCheck(id="r", instruction="a rule")],
        repositories={"acme/app": ChecksScopePolicy(default_mode="error")},
    )
    assert len(_resolve(config, "acme", "app").natural_language) == 1


def test_tools_merge_by_name() -> None:
    config = ChecksConfig(
        enabled=True,
        tools=[CheckToolConfig(name="ruff"), CheckToolConfig(name="gitleaks")],
        repositories={
            "acme/app": ChecksScopePolicy(tools=[CheckToolConfig(name="ruff", enabled=False)])
        },
    )
    policy = _resolve(config)
    names = {tool.name: tool.enabled for tool in policy.tools}
    assert names == {"ruff": False, "gitleaks": True}


def test_a_repository_can_disable_checks_its_organisation_enabled() -> None:
    config = ChecksConfig(
        enabled=True,
        organizations={"acme": ChecksScopePolicy(enabled=True)},
        repositories={"acme/app": ChecksScopePolicy(enabled=False)},
    )
    assert _resolve(config, "acme", "app").active is False
    assert _resolve(config, "acme", "other").active is True


def test_repository_keys_are_case_insensitive() -> None:
    config = ChecksConfig(
        enabled=True, repositories={"ACME/App": ChecksScopePolicy(default_mode="error")}
    )
    assert _resolve(config, "acme", "app").default_mode == "error"


# ───────────────────────────────────────────────────────────── kill switch ──


def test_the_kill_switch_stops_every_repository_at_once() -> None:
    config = ChecksConfig(
        enabled=True,
        kill_switch=True,
        organizations={"acme": ChecksScopePolicy(enabled=True)},
        repositories={"acme/app": ChecksScopePolicy(enabled=True, default_mode="error")},
    )
    policy = _resolve(config, "acme", "app")
    assert policy.active is False
    # Recorded, not merely inferred: a run row must show that a switch did this
    # rather than that somebody rewrote the policy.
    assert policy.killed is True
    assert policy.enabled is False


# ──────────────────────────────────────────────────────────── versioning ──


def test_the_policy_version_moves_when_a_rule_changes() -> None:
    base = _resolve(ChecksConfig(enabled=True))
    changed = _resolve(
        ChecksConfig(
            enabled=True,
            natural_language=[NaturalLanguageCheck(id="r", instruction="a rule")],
        )
    )
    assert base.version != changed.version


def test_the_policy_version_does_not_move_for_announcement_settings() -> None:
    """Turning a comment on must not invalidate results already reached."""
    quiet = _resolve(ChecksConfig(enabled=True, comment=False))
    loud = _resolve(ChecksConfig(enabled=True, comment=True))
    assert quiet.version == loud.version


def test_a_per_check_digest_isolates_one_rule_from_another() -> None:
    """Changing one rule must not invalidate an unrelated check's history."""
    first = _resolve(
        ChecksConfig(
            enabled=True,
            natural_language=[NaturalLanguageCheck(id="r", instruction="first wording")],
        )
    )
    second = _resolve(
        ChecksConfig(
            enabled=True,
            natural_language=[NaturalLanguageCheck(id="r", instruction="second wording")],
        )
    )
    assert first.config_digest_for("nl.r") != second.config_digest_for("nl.r")
    assert first.config_digest_for("native.tests") == second.config_digest_for("native.tests")


def test_the_catalog_reports_every_check_with_its_mode_and_version() -> None:
    config = ChecksConfig(
        enabled=True,
        modes={"native.tests": "error"},
        tools=[CheckToolConfig(name="ruff")],
        natural_language=[NaturalLanguageCheck(id="r", instruction="a rule", title="A rule")],
    )
    entries = {entry["check_id"]: entry for entry in catalog(_resolve(config))}
    assert entries["native.tests"]["mode"] == "error"
    assert entries["native.docs"]["mode"] == "warning"
    assert entries["tool.ruff"]["origin"] == "tool"
    assert entries["nl.r"]["origin"] == "natural_language"
    assert all(entry["version"] for entry in entries.values())


def test_a_disabled_tool_is_not_registered_as_a_check() -> None:
    config = ChecksConfig(enabled=True, tools=[CheckToolConfig(name="ruff", enabled=False)])
    ids = {spec.check_id for spec in specs_for(_resolve(config))}
    assert "tool.ruff" not in ids


def test_the_spec_order_is_stable() -> None:
    """Dedup ownership depends on it, so two identical runs must agree."""
    config = ChecksConfig(
        enabled=True,
        tools=[CheckToolConfig(name="ruff"), CheckToolConfig(name="gitleaks")],
        natural_language=[
            NaturalLanguageCheck(id="b", instruction="b"),
            NaturalLanguageCheck(id="a", instruction="a"),
        ],
    )
    policy = _resolve(config)
    first = [spec.check_id for spec in specs_for(policy)]
    second = [spec.check_id for spec in specs_for(policy)]
    assert first == second
