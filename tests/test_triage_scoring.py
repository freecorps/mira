"""Phase 7C — the ranking, which is arithmetic and has to stay that way.

Nothing here touches a provider, a store or a model. The same signals under the
same policy rank the same way, in the same order, on any machine — which is
what makes a suggestion something a person can argue with.

The exclusions are the part worth reading. They are checked in a fixed order,
and every one of them is *recorded* rather than applied silently: "Dana owns
three of these files but opened the pull request" is a more useful thing to
show than an empty list, and it is the only way anybody can debug a ranking
that surprised them.
"""

from __future__ import annotations

from mira.triage.config_models import TriageConfig, TriageWeights
from mira.triage.history import Touch
from mira.triage.models import Evidence, ExclusionReason
from mira.triage.policy import EffectiveTriagePolicy, resolve_policy
from mira.triage.scoring import kind_of, normalize, rank

NOW = 1_800_000_000.0
DAY = 86_400.0


def _policy(**overrides: object) -> EffectiveTriagePolicy:
    return resolve_policy(TriageConfig(enabled=True, **overrides), "acme", "app")  # type: ignore[arg-type]


def _owns(*paths: str) -> list[Evidence]:
    return [Evidence(path=path, line=3, source="codeowners") for path in paths]


def _touched(identity: str, *paths: str, age_days: float = 1.0) -> list[Touch]:
    return [
        Touch(
            identity=identity,
            path=path,
            at=NOW - age_days * DAY,
            evidence=Evidence(path=path, source="commit", at=NOW - age_days * DAY),
        )
        for path in paths
    ]


def test_identities_are_compared_normalized_and_shown_as_written() -> None:
    assert normalize("@Dana") == "dana"
    assert kind_of("@acme/platform") == "team"
    assert kind_of("dana@acme.example") == "email"
    assert kind_of("dana") == "user"


def test_owning_more_of_the_change_outranks_owning_less() -> None:
    candidates, _ = rank(
        policy=_policy(),
        owners={"@dana": _owns("a.py", "b.py", "c.py"), "@sam": _owns("a.py")},
        authored={},
        reviewed={},
        now=NOW,
    )
    assert [c.identity for c in candidates] == ["dana", "sam"]
    assert candidates[0].score == 9.0
    assert candidates[0].contributions[0].detail == "owns 3 of the changed file(s)"


def test_declared_ownership_outranks_observed_history() -> None:
    """When the repository states an owner, the statement beats the inference."""
    candidates, _ = rank(
        policy=_policy(),
        owners={"@dana": _owns("a.py")},
        authored={"sam": _touched("sam", "a.py")},
        reviewed={},
        now=NOW,
    )
    assert candidates[0].identity == "dana"


def test_two_signals_beat_one_at_the_same_strength() -> None:
    candidates, _ = rank(
        policy=_policy(weights=TriageWeights(codeowners=1.0, authored=1.0, reviewed=1.0)),
        owners={"@dana": _owns("a.py")},
        authored={"dana": _touched("dana", "a.py"), "sam": _touched("sam", "a.py")},
        reviewed={},
        now=NOW,
    )
    assert candidates[0].identity == "dana"
    assert candidates[0].signals == ["codeowners", "authored"]


def test_older_history_is_worth_less_than_newer() -> None:
    candidates, _ = rank(
        policy=_policy(codeowners=False),
        owners={},
        authored={
            "recent": _touched("recent", "a.py", age_days=1),
            "older": _touched("older", "a.py", age_days=20),
        },
        reviewed={},
        now=NOW,
    )
    assert [c.identity for c in candidates] == ["recent", "older"]
    assert candidates[0].score > candidates[1].score


def test_one_file_touched_at_the_edge_of_the_window_is_not_a_recommendation() -> None:
    """The floor sits just under one recent authorship, on purpose.

    A file you changed yesterday is enough to be suggested. The same file
    changed most of a window ago has decayed to a fifth of that, and a single
    six-month-old commit is a fact rather than a recommendation.
    """
    candidates, excluded = rank(
        policy=_policy(codeowners=False),
        owners={},
        authored={"ancient": _touched("ancient", "a.py", age_days=170)},
        reviewed={},
        now=NOW,
    )
    assert candidates == []
    assert excluded[0].reason == ExclusionReason.BELOW_THRESHOLD


def test_the_author_is_never_suggested_and_the_reason_is_recorded() -> None:
    candidates, excluded = rank(
        policy=_policy(),
        owners={"@dana": _owns("a.py", "b.py")},
        authored={},
        reviewed={},
        pr_author="Dana",
        now=NOW,
    )
    assert candidates == []
    assert [(e.identity, e.reason) for e in excluded] == [("dana", ExclusionReason.AUTHOR)]


def test_a_machine_account_is_dropped() -> None:
    _, excluded = rank(
        policy=_policy(),
        owners={"@dependabot[bot]": _owns("a.py")},
        authored={},
        reviewed={},
        now=NOW,
    )
    assert excluded[0].reason == ExclusionReason.BOT


def test_somebody_who_opted_out_is_dropped_however_they_are_named() -> None:
    _, excluded = rank(
        policy=_policy(exclude=["dana"]),
        owners={"@Dana": _owns("a.py")},
        authored={},
        reviewed={},
        now=NOW,
    )
    assert excluded[0].reason == ExclusionReason.OPTED_OUT


def test_an_opt_out_beats_every_signal() -> None:
    candidates, _ = rank(
        policy=_policy(exclude=["dana"]),
        owners={"@dana": _owns("a.py", "b.py", "c.py")},
        authored={"dana": _touched("dana", "a.py", "b.py")},
        reviewed={"dana": _touched("dana", "a.py")},
        now=NOW,
    )
    assert candidates == []


def test_a_name_with_no_evidence_is_not_suggested() -> None:
    """Defensive: an unevidenced name is a bug upstream, and the answer to a
    bug is to drop the name, not to publish an accusation nobody can check."""
    candidates, excluded = rank(
        policy=_policy(),
        owners={"@ghost": []},
        authored={},
        reviewed={},
        now=NOW,
    )
    assert candidates == []
    # An empty evidence list produces no contribution at all, so the name never
    # enters the ranking in the first place.
    assert excluded == []


def test_being_busy_dampens_a_score_without_disqualifying_anybody() -> None:
    candidates, _ = rank(
        policy=_policy(load_penalty=1.0),
        owners={"@dana": _owns("a.py", "b.py"), "@sam": _owns("a.py", "b.py")},
        authored={},
        reviewed={},
        load={"dana": 3},
        now=NOW,
    )
    assert [c.identity for c in candidates] == ["sam", "dana"]
    dana = next(c for c in candidates if c.identity == "dana")
    assert dana.open_reviews == 3
    assert dana.load_penalty == 3.0
    # Still suggested, just second — the most qualified reviewer is still the
    # most qualified reviewer when they are busy.
    assert dana.score == 3.0


def test_a_score_can_be_dampened_to_zero_but_not_below() -> None:
    candidates, excluded = rank(
        policy=_policy(load_penalty=10.0, min_score=0.0),
        owners={"@dana": _owns("a.py")},
        authored={},
        reviewed={},
        load={"dana": 5},
        now=NOW,
    )
    assert candidates[0].score == 0.0


def test_a_weak_signal_falls_below_the_floor_and_says_so() -> None:
    candidates, excluded = rank(
        policy=_policy(codeowners=False, min_score=2.0),
        owners={},
        authored={"sam": _touched("sam", "a.py", age_days=170)},
        reviewed={},
        now=NOW,
    )
    assert candidates == []
    assert excluded[0].reason == ExclusionReason.BELOW_THRESHOLD
    assert "below the 2 floor" in excluded[0].detail


def test_being_fourth_is_recorded_rather_than_vanishing() -> None:
    owners = {
        f"@r{index}": _owns(*[f"{letter}.py" for letter in "abcd"[: index + 1]])
        for index in range(4)
    }
    candidates, excluded = rank(
        policy=_policy(max_suggestions=2), owners=owners, authored={}, reviewed={}, now=NOW
    )
    assert len(candidates) == 2
    cut = [e for e in excluded if e.reason == ExclusionReason.NOT_TOP_RANKED]
    assert len(cut) == 2
    assert "outside the top 2" in cut[0].detail


def test_a_team_is_never_excluded_for_being_the_author() -> None:
    """Mira does not resolve team membership and will not guess at it."""
    candidates, _ = rank(
        policy=_policy(),
        owners={"@acme/platform": _owns("a.py")},
        authored={},
        reviewed={},
        pr_author="dana",
        now=NOW,
    )
    assert [c.identity for c in candidates] == ["acme/platform"]
    assert candidates[0].kind == "team"


def test_a_team_carries_no_review_load() -> None:
    candidates, _ = rank(
        policy=_policy(load_penalty=1.0),
        owners={"@acme/platform": _owns("a.py")},
        authored={},
        reviewed={},
        load={"acme/platform": 9},
        now=NOW,
    )
    assert candidates[0].load_penalty == 0.0


def test_the_same_inputs_always_produce_the_same_order() -> None:
    owners = {"@dana": _owns("a.py"), "@sam": _owns("b.py"), "@ari": _owns("c.py")}
    first, _ = rank(policy=_policy(), owners=owners, authored={}, reviewed={}, now=NOW)
    second, _ = rank(
        policy=_policy(),
        owners=dict(reversed(list(owners.items()))),
        authored={},
        reviewed={},
        now=NOW,
    )
    assert [c.identity for c in first] == [c.identity for c in second]


def test_every_candidate_carries_the_evidence_that_produced_it() -> None:
    candidates, _ = rank(
        policy=_policy(),
        owners={"@dana": _owns("a.py")},
        authored={"dana": _touched("dana", "b.py")},
        reviewed={},
        now=NOW,
    )
    evidence = candidates[0].evidence
    assert {item.source for item in evidence} == {"codeowners", "commit"}
    assert all(item.path for item in evidence)
