"""Does this pull request name an issue, and does that issue exist?

Two questions, one check, and the order they are answered in is what keeps the
result honest.

*Does it name one?* Answerable offline, always. A pull request with no
reference anywhere in its title, body or branch is a violation when the policy
requires one — and a skip when the policy does not, because a deployment that
never asked for ticket discipline should not be told about it on every pull
request.

*Does the named one exist?* Answerable only if somebody can be asked. A tracker
that says "no such issue" produces a violation naming the reference. A tracker
that cannot be reached produces an infrastructure error, and the message says
in as many words that this is Mira's problem and not the author's. Those two
outcomes are one HTTP status apart and could not be further apart in meaning,
which is why the adapter contract makes the provider state which one it means
rather than letting this check guess from an exception type.

An exempt label takes the pull request out of scope entirely: a repository that
labels dependency bumps ``dependencies`` has said those do not need a ticket,
and the check records that as a skip rather than as a pass it did not earn.
"""

from __future__ import annotations

from mira.checks.context import CheckContext, CheckOutcome
from mira.checks.external.resolve import resolution_for
from mira.checks.models import CheckFinding, Evidence, SkipReason, fingerprint

VERSION = "1"

CHECK_ID = "context.ticket"


def _reference_evidence(ref, detail: str) -> Evidence:  # type: ignore[no-untyped-def]
    return Evidence(
        start_line=ref.line,
        snippet=ref.raw,
        detail=f"{detail} (found in the {ref.found_in})",
        url=ref.url,
        source="pr",
    )


async def run(ctx: CheckContext) -> CheckOutcome:
    """Report a missing reference, a reference to nothing, or neither."""
    settings = ctx.policy.ticket

    exempt = {label.strip().lower() for label in settings.exempt_labels if label.strip()}
    matched = sorted({label for label in ctx.labels if label.lower() in exempt})
    if matched:
        return CheckOutcome.skipped(
            f"Labelled {', '.join(matched)}, which this repository exempts from needing a ticket.",
            SkipReason.OUT_OF_SCOPE,
        )

    resolution = await resolution_for(ctx)

    if not resolution.refs:
        if not settings.require_reference:
            return CheckOutcome.skipped(
                "This pull request references no issue, and this repository does not require one.",
                SkipReason.NOT_APPLICABLE,
            )
        finding = CheckFinding(
            fingerprint=fingerprint(path="", signature="no ticket reference"),
            title="This pull request references no issue",
            detail=(
                "Nothing in the title, the description or the branch name looks like an "
                "issue reference. Add one — `#123`, `owner/repo#123`, a link to the "
                "issue, or a key matching this repository's own pattern — or apply one "
                "of the labels this repository exempts."
            ),
            evidence=[
                Evidence(
                    snippet=(ctx.pr_title or "")[:200],
                    detail="pull request title, searched for a reference",
                    url=ctx.pr_url,
                    source="pr",
                )
            ],
            sources=[CHECK_ID],
        )
        return CheckOutcome.violation(
            summary="No issue reference in the title, description or branch name.",
            findings=[finding],
        )

    if resolution.missing:
        # A definite absence outranks an unreachable tracker: it is a fact
        # about the pull request and it is actionable, where the other is
        # neither. Anything unresolved is named in the summary so the reader
        # knows the answer is not the whole picture.
        findings = [
            CheckFinding(
                fingerprint=fingerprint(path="", signature=f"ticket {ref.label} does not exist"),
                title=f"{ref.label} does not exist",
                detail=(
                    f"The tracker was asked about {ref.label} and answered that there is "
                    "no such issue. A reference to nothing is worse than no reference: it "
                    "reads as context that a reviewer can go and find."
                ),
                evidence=[_reference_evidence(ref, "reference to a missing issue")],
                sources=[CHECK_ID],
            )
            for ref in resolution.missing
        ]
        summary = f"{len(findings)} referenced issue(s) do not exist."
        if resolution.unresolved:
            summary += (
                f" {len(resolution.unresolved)} more could not be checked at all: "
                + ", ".join(sorted(resolution.unresolved))
                + "."
            )
        return CheckOutcome.violation(summary=summary, findings=findings)

    if resolution.found and resolution.unresolved:
        # Some references resolved and some could not be asked about. Passing
        # on the ones that worked would let a partially checked pull request
        # satisfy the gate — and the half nobody could reach is exactly the
        # half a check exists to be sure about.
        reasons = "; ".join(
            f"{label}: {why}" for label, why in sorted(resolution.unresolved.items())
        )
        return CheckOutcome.failed(
            error=reasons,
            summary=(
                f"{len(resolution.found)} of this pull request's references resolved and "
                f"{len(resolution.unresolved)} could not be checked, so Mira cannot say "
                "whether they all exist. This is a Mira problem, not a problem with the "
                "change."
            ),
        )

    if resolution.found:
        issue = resolution.first_found()
        return CheckOutcome.passed(
            summary=(
                f"References {', '.join(sorted(resolution.found))}"
                + (f" — {issue.title}" if issue and issue.title else "")
                + "."
            ),
            evidence=[
                Evidence(
                    snippet=found.title[:200],
                    detail=f"{label} ({found.state or 'state unknown'})",
                    url=found.url,
                    source="ticket",
                )
                for label, found in sorted(resolution.found.items())
            ],
        )

    # Every reference is present and none of them could be checked. Whether
    # that is a pass or an error depends on *why*, and the two reasons are not
    # the same fact about the world.
    references = [
        _reference_evidence(ref, "reference found, existence not verified")
        for ref in resolution.refs
    ]
    if resolution.lookups_disabled:
        return CheckOutcome.passed(
            summary=(
                f"References {', '.join(ref.label for ref in resolution.refs)}. "
                "Ticket lookups are switched off for this deployment, so Mira confirmed "
                "the reference is there and not that the issue exists."
            ),
            evidence=references,
        )
    reasons = "; ".join(f"{label}: {why}" for label, why in sorted(resolution.unresolved.items()))
    return CheckOutcome.failed(
        error=reasons or "the tracker could not be reached",
        summary=(
            f"This pull request references {', '.join(ref.label for ref in resolution.refs)}, "
            "and Mira could not reach the tracker to confirm it. This is a Mira problem, "
            "not a problem with the change."
        ),
    )
