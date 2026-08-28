"""Phase 7C — the policy, and the questions it is the only answer to.

Three layers resolve into one frozen answer, a kill switch beats all three, and
an opt-out entry works however the person who wrote it spelled their name. The
last one is not a nicety: an opt-out that silently fails to match is a person
who asked not to be named and keeps being named.
"""

from __future__ import annotations

import dataclasses

import pytest
from pydantic import ValidationError

from mira.config import MiraConfig
from mira.triage.config_models import TriageConfig, TriageScopePolicy, TriageWeights
from mira.triage.policy import resolve_policy


def test_triage_is_off_until_somebody_turns_it_on() -> None:
    policy = resolve_policy(MiraConfig().triage, "acme", "app")
    assert policy.enabled is False
    assert policy.active is False


def test_an_organisation_setting_reaches_its_repositories() -> None:
    config = TriageConfig(
        enabled=False,
        organizations={"acme": TriageScopePolicy(enabled=True, max_suggestions=2)},
    )
    policy = resolve_policy(config, "acme", "app")
    assert policy.active is True
    assert policy.max_suggestions == 2
    # A repository in another organisation is untouched by it.
    assert resolve_policy(config, "other", "app").active is False


def test_a_repository_overrides_its_organisation() -> None:
    config = TriageConfig(
        enabled=True,
        max_suggestions=5,
        organizations={"acme": TriageScopePolicy(max_suggestions=3)},
        repositories={"acme/app": TriageScopePolicy(max_suggestions=1, history=False)},
    )
    policy = resolve_policy(config, "acme", "app")
    assert policy.max_suggestions == 1
    assert policy.history is False
    assert policy.signals_enabled == ("codeowners",)
    # A sibling repository still gets the organisation's answer.
    assert resolve_policy(config, "acme", "other").max_suggestions == 3


def test_an_empty_list_means_empty_here_not_inherit() -> None:
    """The sentinel every policy in this codebase shares.

    ``None`` inherits and ``[]`` empties. A repository that clears an opt-out
    list means it, and a merge that quietly kept the inherited entries would
    make the setting unwritable.
    """
    config = TriageConfig(
        enabled=True,
        exclude=["dana"],
        organizations={"acme": TriageScopePolicy()},
        repositories={"acme/app": TriageScopePolicy(exclude=[])},
    )
    assert resolve_policy(config, "acme", "other").exclude == ("dana",)
    assert resolve_policy(config, "acme", "app").exclude == ()


def test_the_kill_switch_beats_every_layer() -> None:
    config = TriageConfig(
        enabled=True,
        kill_switch=True,
        organizations={"acme": TriageScopePolicy(enabled=True)},
        repositories={"acme/app": TriageScopePolicy(enabled=True)},
    )
    policy = resolve_policy(config, "acme", "app")
    assert policy.active is False
    # Recorded, not inferred: a run row has to be able to say that a switch —
    # not a policy edit — made triage inert.
    assert policy.killed is True


def test_an_opt_out_matches_however_it_was_written() -> None:
    policy = resolve_policy(TriageConfig(enabled=True, exclude=["@Dana"]), "acme", "app")
    assert policy.excluded("dana") is True
    assert policy.excluded("@dana") is True
    assert policy.excluded("DANA") is True
    assert policy.excluded("dana-ops") is False


def test_a_machine_account_is_named_by_convention_or_by_hand() -> None:
    policy = resolve_policy(TriageConfig(enabled=True, bots=["release-train"]), "acme", "app")
    assert policy.is_bot("dependabot[bot]") is True
    assert policy.is_bot("release-train") is True
    # No substring heuristic: `robot-oncall` is a team of people, and excluding
    # a human for having the wrong name is worse than including one machine.
    assert policy.is_bot("robot-oncall") is False


def test_bot_exclusion_can_be_switched_off_entirely() -> None:
    policy = resolve_policy(TriageConfig(enabled=True, exclude_bots=False), "acme", "app")
    assert policy.is_bot("dependabot[bot]") is False


def test_the_version_changes_when_the_ranking_would() -> None:
    base = resolve_policy(TriageConfig(enabled=True), "acme", "app")
    heavier = resolve_policy(
        TriageConfig(enabled=True, weights=TriageWeights(codeowners=9.0)), "acme", "app"
    )
    assert base.version != heavier.version

    # Publishing settings do not change who should review the code, so they do
    # not invalidate results that were already correctly produced.
    quiet = resolve_policy(TriageConfig(enabled=True, comment=False), "acme", "app")
    assert quiet.digest == base.digest


def test_an_unusable_opt_out_entry_fails_the_config_load() -> None:
    """A pattern that can never match is a startup error, not a silent no-op."""
    with pytest.raises(ValidationError) as exc:
        TriageConfig(exclude=["not a login!"])
    assert "recognizable login" in str(exc.value)


def test_a_repository_key_has_to_name_a_repository() -> None:
    with pytest.raises(ValidationError):
        TriageConfig(repositories={"acme": TriageScopePolicy()})


def test_weights_are_bounded() -> None:
    with pytest.raises(ValidationError):
        TriageWeights(codeowners=-1.0)


def test_the_policy_is_frozen() -> None:
    """Every run quotes it, so it must not be mutable between rank and record."""
    policy = resolve_policy(TriageConfig(enabled=True), "acme", "app")
    with pytest.raises(dataclasses.FrozenInstanceError):
        policy.max_suggestions = 9  # type: ignore[misc]
