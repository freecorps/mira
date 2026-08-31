"""Tests for review verdict decisions (approve / request changes)."""

from __future__ import annotations

import pytest

from mira.config import MiraConfig
from mira.core.verdict import APPROVE, REQUEST_CHANGES, decide_verdict
from mira.models import (
    PRInfo,
    ReviewComment,
    ReviewResult,
    Severity,
    WalkthroughConfidenceScore,
    WalkthroughResult,
)


def _config(**verdict_kwargs) -> MiraConfig:
    cfg = MiraConfig()
    for key, value in verdict_kwargs.items():
        setattr(cfg.review.verdict, key, value)
    return cfg


def _pr(author: str = "alice") -> PRInfo:
    return PRInfo(
        title="t",
        description="d",
        base_branch="main",
        head_branch="feat",
        url="https://github.com/o/r/pull/1",
        number=1,
        owner="o",
        repo="r",
        author=author,
    )


def _comment(severity: Severity) -> ReviewComment:
    return ReviewComment(
        path="a.py",
        line=1,
        end_line=None,
        severity=severity,
        category="bug",
        title="t",
        body="b",
        confidence=0.9,
    )


def _result(*severities: Severity, **kwargs) -> ReviewResult:
    return ReviewResult(
        comments=[_comment(s) for s in severities],
        reviewed_files=kwargs.pop("reviewed_files", 3),
        **kwargs,
    )


def test_mode_off_stays_silent():
    off = _config(mode="off")
    assert decide_verdict(_result(), off, _pr(), "mira") is None
    assert decide_verdict(_result(Severity.BLOCKER), off, _pr(), "mira") is None


def test_approving_is_the_default_and_blocking_is_not():
    """The asymmetry is the whole reason there are three modes.

    An approval adds a signal a human can dismiss; a REQUEST_CHANGES takes the
    merge button away until somebody does. The first is a sensible default, the
    second is a decision a deployment makes on purpose.
    """
    clean = decide_verdict(_result(), _config(), _pr(), "mira")
    assert clean is not None and clean.event == APPROVE
    assert decide_verdict(_result(Severity.BLOCKER), _config(), _pr(), "mira") is None


def test_clean_pr_is_approved():
    verdict = decide_verdict(_result(), _config(mode="approve"), _pr(), "mira")
    assert verdict is not None
    assert verdict.event == APPROVE


def test_suggestions_alone_still_approve():
    verdict = decide_verdict(
        _result(Severity.SUGGESTION, Severity.NITPICK), _config(mode="approve"), _pr(), "mira"
    )
    assert verdict is not None
    assert verdict.event == APPROVE


def test_nitpick_ceiling_rejects_suggestions():
    verdict = decide_verdict(
        _result(Severity.SUGGESTION),
        _config(mode="approve", approve_max_severity="nitpick"),
        _pr(),
        "mira",
    )
    assert verdict is None


@pytest.mark.parametrize("severity", [Severity.BLOCKER, Severity.WARNING])
def test_findings_above_ceiling_request_changes(severity):
    verdict = decide_verdict(_result(severity), _config(mode="request_changes"), _pr(), "mira")
    assert verdict is not None
    assert verdict.event == REQUEST_CHANGES


@pytest.mark.parametrize("severity", [Severity.BLOCKER, Severity.WARNING])
def test_approve_only_mode_never_blocks(severity):
    """`mode: approve` opts into green, not red — findings stay comment-only."""
    assert decide_verdict(_result(severity), _config(mode="approve"), _pr(), "mira") is None


def test_skipped_files_block_approval():
    result = _result(skipped_paths=["big.py"])
    assert decide_verdict(result, _config(mode="approve"), _pr(), "mira") is None


def test_skipped_files_allowed_when_guard_disabled():
    result = _result(skipped_paths=["big.py"])
    verdict = decide_verdict(
        result,
        _config(mode="approve", require_all_files_reviewed=False),
        _pr(),
        "mira",
    )
    assert verdict is not None
    assert verdict.event == APPROVE


def test_skipped_review_is_not_approved():
    result = _result(skipped_reason="PR too large")
    assert decide_verdict(result, _config(mode="approve"), _pr(), "mira") is None


@pytest.mark.parametrize("author", ["mira", "mira[bot]", "MIRA"])
def test_never_approves_own_pr(author):
    assert decide_verdict(_result(), _config(mode="approve"), _pr(author), "mira") is None


def test_never_approves_over_a_human_requesting_changes():
    verdict = decide_verdict(
        _result(),
        _config(mode="approve"),
        _pr(),
        "mira",
        human_states={"bob": "CHANGES_REQUESTED"},
    )
    assert verdict is None


def test_human_approval_does_not_block():
    verdict = decide_verdict(
        _result(), _config(mode="approve"), _pr(), "mira", human_states={"bob": "APPROVED"}
    )
    assert verdict is not None
    assert verdict.event == APPROVE


def test_other_bots_do_not_block_approval():
    verdict = decide_verdict(
        _result(),
        _config(mode="approve"),
        _pr(),
        "mira",
        human_states={"other-review[bot]": "CHANGES_REQUESTED"},
    )
    assert verdict is not None
    assert verdict.event == APPROVE


def test_request_changes_body_counts_findings():
    verdict = decide_verdict(
        _result(Severity.BLOCKER, Severity.WARNING, Severity.WARNING),
        _config(mode="request_changes"),
        _pr(),
        "mira",
    )
    assert verdict is not None
    assert "1 blocker" in verdict.body
    assert "2 warnings" in verdict.body


def _scored(score: int, *severities: Severity) -> ReviewResult:
    result = _result(*severities)
    result.walkthrough = WalkthroughResult(
        confidence_score=WalkthroughConfidenceScore(score=score, label="Needs review", reason="r")
    )
    return result


def test_a_low_confidence_review_is_not_approved_even_with_no_findings():
    """The second question the ceiling cannot ask.

    "I found nothing" and "I understood this well enough for that to mean
    something" are different claims, and a model that rated its own read of the
    change 2/5 has already answered the second one.
    """
    assert decide_verdict(_scored(2), _config(mode="approve"), _pr(), "mira") is None


def test_a_confident_review_is_approved_and_says_the_score():
    verdict = decide_verdict(_scored(5), _config(mode="approve"), _pr(), "mira")
    assert verdict is not None
    assert verdict.event == APPROVE
    assert "5/5" in verdict.body


def test_the_floor_is_the_configured_one():
    lenient = _config(mode="approve", approve_min_confidence=3)
    assert decide_verdict(_scored(3), lenient, _pr(), "mira") is not None
    assert decide_verdict(_scored(2), lenient, _pr(), "mira") is None


def test_the_floor_can_be_switched_off():
    verdict = decide_verdict(
        _scored(1), _config(mode="approve", approve_min_confidence=0), _pr(), "mira"
    )
    assert verdict is not None
    assert verdict.event == APPROVE


def test_a_review_with_no_score_is_judged_on_severity_alone():
    """Turning the walkthrough off must not silently stop every approval.

    The floor is evidence Mira uses when it has it. Treating "no score" as a
    failing score would change behaviour for installs that never opted into
    anything, in the name of caution they did not ask for.
    """
    verdict = decide_verdict(_result(), _config(mode="approve"), _pr(), "mira")
    assert verdict is not None
    assert verdict.event == APPROVE
    assert "confidence" not in verdict.body.lower()


def test_the_floor_never_turns_into_a_request_for_changes():
    """A low score is a reason to say nothing, not a reason to block.

    Blocking on it would let one number the model wrote about itself hold a
    merge — which is exactly the authority this verdict does not have.
    """
    assert decide_verdict(_scored(1), _config(mode="request_changes"), _pr(), "mira") is None


def test_invalid_config_rejected():
    """Validation happens where config actually comes from — construction."""
    with pytest.raises(ValueError):
        MiraConfig(review={"verdict": {"mode": "yolo"}})
    with pytest.raises(ValueError):
        MiraConfig(review={"verdict": {"approve_max_severity": "catastrophic"}})


def test_valid_config_accepted():
    cfg = MiraConfig(
        review={"verdict": {"mode": "request_changes", "approve_max_severity": "nitpick"}}
    )
    assert cfg.review.verdict.mode == "request_changes"
    assert cfg.review.verdict.approve_max_severity == "nitpick"
