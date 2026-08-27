"""Does the linked issue say what "done" means?

The narrowest useful version of the question, and deliberately so. This check
does not judge whether the pull request *meets* the acceptance criteria — that
is a review, needs the whole diff and a model, and is available as a
natural-language check for teams that want it. It asks whether anybody wrote
criteria down at all, which is answerable from the issue body alone and is the
failure that actually happens.

Off unless asked for: ``checks.ticket.require_acceptance_criteria`` is
``False`` by default, and the check skips rather than passing when it is. A
skip says "this repository does not ask this question"; a pass would say "the
issue has criteria", and saying that about an issue nobody looked at would be
the kind of quiet lie this phase exists to remove.

Everything the ticket check could not resolve, this check cannot either — and
it does not re-ask. Both read one shared resolution, so a tracker outage
produces one infrastructure error apiece over one failed lookup rather than two
lookups and two differently-worded reports of one cause.
"""

from __future__ import annotations

from mira.checks.context import CheckContext, CheckOutcome
from mira.checks.external.resolve import resolution_for
from mira.checks.external.tickets import parse_acceptance_criteria
from mira.checks.models import CheckFinding, Evidence, SkipReason, fingerprint

VERSION = "1"

CHECK_ID = "context.acceptance_criteria"

# Criteria quoted as evidence. A cap: the point is to show that criteria exist
# and what they look like, not to copy the issue into the pull request.
_MAX_EVIDENCE = 8


async def run(ctx: CheckContext) -> CheckOutcome:
    """Report a linked issue that states no acceptance criteria."""
    settings = ctx.policy.ticket
    if not settings.require_acceptance_criteria:
        return CheckOutcome.skipped(
            "This repository does not require acceptance criteria on the linked issue.",
            SkipReason.DISABLED,
        )

    resolution = await resolution_for(ctx)

    if not resolution.refs:
        return CheckOutcome.skipped(
            "This pull request references no issue, so there is none to read criteria "
            "from. The linked-issue check answers for that.",
            SkipReason.NOT_APPLICABLE,
        )

    if not resolution.found:
        if resolution.lookups_disabled:
            return CheckOutcome.skipped(
                "Ticket lookups are switched off for this deployment, so the linked "
                "issue's body was never read.",
                SkipReason.UNSUPPORTED,
            )
        if resolution.missing and not resolution.unresolved:
            return CheckOutcome.skipped(
                "The referenced issue does not exist, so it states no criteria. The "
                "linked-issue check answers for that.",
                SkipReason.NOT_APPLICABLE,
            )
        reasons = "; ".join(
            f"{label}: {why}" for label, why in sorted(resolution.unresolved.items())
        )
        return CheckOutcome.failed(
            error=reasons or "the tracker could not be reached",
            summary=(
                "Mira could not read the linked issue, so it cannot say whether it "
                "states acceptance criteria. This is a Mira problem, not a problem "
                "with the change."
            ),
        )

    findings: list[CheckFinding] = []
    evidence: list[Evidence] = []
    for label, issue in sorted(resolution.found.items()):
        criteria = parse_acceptance_criteria(issue.body)
        if criteria:
            evidence.extend(
                Evidence(
                    start_line=criterion.line,
                    snippet=criterion.text[:300],
                    detail=f"{label} acceptance criterion",
                    url=issue.url,
                    source="ticket",
                )
                for criterion in criteria[:_MAX_EVIDENCE]
            )
            continue
        findings.append(
            CheckFinding(
                fingerprint=fingerprint(
                    path="", signature=f"issue {label} has no acceptance criteria"
                ),
                title=f"{label} states no acceptance criteria",
                detail=(
                    "The issue body carries no checklist, no criteria section and no "
                    "Given/When/Then lines, so there is nothing written down that says "
                    "what this pull request has to do to be finished."
                ),
                evidence=[
                    Evidence(
                        snippet=issue.title[:200],
                        detail=f"{label}, read for acceptance criteria",
                        url=issue.url,
                        source="ticket",
                    )
                ],
                sources=[CHECK_ID],
            )
        )

    if findings:
        return CheckOutcome.violation(
            summary=f"{len(findings)} linked issue(s) state no acceptance criteria.",
            findings=findings,
        )
    return CheckOutcome.passed(
        summary=f"{len(evidence)} acceptance criterion/criteria found on the linked issue(s).",
        evidence=evidence[:_MAX_EVIDENCE],
    )
