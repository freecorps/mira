"""Phase 7C — what the comment says, and what it refuses to say.

Three rules, all of them about the reader rather than the ranking.

*Nobody is mentioned.* Every identity is rendered as literal text. A suggestion
that pings four people has notified three of them about work they never agreed
to, on every push, which is the fastest way to have the feature switched off.

*Mira's failures are labelled as Mira's.* A run that could not read what it
ranks on says so in its own name and never renders as "no suggestions".

*Nothing from the repository is rendered as markup.* Paths come out of a diff,
so they go inside a code span with backticks and control characters stripped —
a filename cannot close the span it is written in.
"""

from __future__ import annotations

from mira.triage.explain import (
    admin_explanation,
    mask_email,
    one_line,
    public_explanation,
)
from mira.triage.models import (
    Classification,
    Evidence,
    Exclusion,
    ReviewerCandidate,
    SignalContribution,
    SignalReport,
    TriageInputs,
    TriageRun,
)


def _run(**overrides: object) -> TriageRun:
    inputs = TriageInputs(
        owner="acme",
        repo="app",
        pr_number=7,
        pr_author="kit",
        head_sha="head222",
        ownership_ref="base111",
    )
    run = TriageRun(
        run_key="k",
        policy_version="triage-v1+abc",
        inputs=inputs,
        classification=Classification(
            size="s", changed_files=2, changed_lines=40, areas=["src/mira"], kinds=["code"]
        ),
        candidates=[
            ReviewerCandidate(
                identity="dana",
                score=3.0,
                contributions=[
                    SignalContribution(
                        kind="codeowners",
                        raw=1,
                        weight=3.0,
                        score=3.0,
                        detail="owns 1 of the changed file(s)",
                        evidence=[
                            Evidence(
                                path="src/mira/app.py",
                                line=4,
                                detail=".github/CODEOWNERS:4 — src/",
                                source="codeowners",
                            )
                        ],
                    )
                ],
            )
        ],
        signals=[SignalReport(kind="codeowners", status="available", candidates=1)],
        excluded=[Exclusion(identity="kit", reason="author", detail="opened this")],
    )
    for key, value in overrides.items():
        setattr(run, key, value)
    return run


def test_nobody_is_mentioned() -> None:
    body = public_explanation(_run())
    assert "`dana`" in body
    assert "@dana" not in body


def test_the_comment_says_it_is_not_a_request() -> None:
    assert "not a review request" in public_explanation(_run())


def test_every_name_carries_its_reason_and_its_evidence() -> None:
    body = public_explanation(_run())
    assert "listed in CODEOWNERS" in body
    assert "src/mira/app.py:4" in body


def test_nobody_to_suggest_reads_as_an_answer() -> None:
    run = _run(candidates=[])
    body = public_explanation(run)
    assert run.status == "no_candidates"
    assert "found nobody to suggest" in body
    assert "not a failure" in body


def test_a_failure_reads_as_mira_s_and_never_as_nobody() -> None:
    run = _run(
        candidates=[],
        signals=[SignalReport(kind="codeowners", status="unavailable", detail="502 from the API")],
    )
    body = public_explanation(run)
    assert run.status == "unavailable"
    assert "could not work out who to suggest" in body
    assert "problem with Mira" in body
    assert "502 from the API" in body
    assert "nobody to suggest" not in body


def test_a_short_list_admits_the_signal_that_failed() -> None:
    run = _run(
        signals=[
            SignalReport(kind="codeowners", status="available", candidates=1),
            SignalReport(kind="authored", status="unavailable", detail="rate limited"),
        ]
    )
    body = public_explanation(run)
    assert run.status == "ok"
    assert "may be short" in body
    assert "rate limited" in body


def test_an_email_owner_is_masked_in_public_and_whole_for_an_admin() -> None:
    """The address is in the repository already; a pull-request thread is not
    the repository, and republishing it there buys nothing."""
    run = _run(
        candidates=[
            ReviewerCandidate(
                identity="dana@acme.example",
                kind="email",
                score=3.0,
                contributions=[
                    SignalContribution(
                        kind="codeowners",
                        raw=1,
                        weight=3.0,
                        score=3.0,
                        evidence=[Evidence(path="docs/a.md", line=2, source="codeowners")],
                    )
                ],
            )
        ]
    )
    assert "d***@acme.example" in public_explanation(run)
    assert "dana@acme.example" not in public_explanation(run)
    assert "dana@acme.example" in admin_explanation(run)


def test_masking_leaves_a_login_alone() -> None:
    assert mask_email("dana") == "dana"
    assert mask_email("dana@x.example") == "d***@x.example"


def test_a_path_cannot_close_the_code_span_it_is_written_in() -> None:
    run = _run(
        candidates=[
            ReviewerCandidate(
                identity="dana",
                score=3.0,
                contributions=[
                    SignalContribution(
                        kind="codeowners",
                        raw=1,
                        weight=3.0,
                        score=3.0,
                        evidence=[
                            Evidence(
                                path="src/`echo`\n## Injected heading",
                                line=1,
                                source="codeowners",
                            )
                        ],
                    )
                ],
            )
        ]
    )
    body = public_explanation(run)
    # The text survives — it is a filename, and hiding it would be worse — but
    # it cannot be markup: the newline is flattened, so nothing it contains
    # begins a line, and its backticks are neutralised, so it cannot end the
    # code span it sits inside.
    assert "\n## Injected heading" not in body
    assert "`echo`" not in body
    assert [line for line in body.splitlines() if line.startswith("#")] == [
        "### Reviewer suggestions"
    ]


def test_the_admin_view_shows_the_arithmetic_and_everyone_dropped() -> None:
    body = admin_explanation(_run())
    assert "codeowners 1×3=3" in body
    assert "kit" in body
    assert "opened this pull request" in body
    assert "ownership read at: `base111`" in body


def test_the_admin_view_says_when_nobody_was_dropped() -> None:
    body = admin_explanation(_run(excluded=[]))
    assert "nobody was dropped" in body


def test_the_log_line_says_what_happened_in_one_sentence() -> None:
    assert "acme/app#7 triage ok: dana" in one_line(_run())
    degraded = _run(signals=[SignalReport(kind="codeowners", status="unavailable", detail="x")])
    assert "(degraded)" in one_line(degraded)


def test_a_run_that_never_started_says_so() -> None:
    run = _run(candidates=[], signals=[])
    assert run.status == "not_run"
    assert "did not run" in public_explanation(run)


def test_a_note_is_shown_without_being_dressed_up_as_a_finding() -> None:
    run = _run(notes=["Review load could not be read, so nobody was dampened."])
    body = public_explanation(run)
    assert "Review load could not be read" in body
