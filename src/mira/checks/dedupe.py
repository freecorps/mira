"""One problem, one entry, both sources named.

Running a deterministic analyser and a language model over the same diff means
they will sometimes find the same thing. Reporting it twice is not a cosmetic
problem: a reader who sees "hardcoded credential at config.py:14" from gitleaks
and "a secret is committed here" from a rule, as two separate entries, has to
work out that they are one issue before they can act on either — and the count
at the top of the run is wrong, which is the number people actually read.

The merge keeps *both* evidences. That is the point of running two producers:
gitleaks knows the rule id and the exact span, the model knows why it matters
in this file. Dropping either one to make a tidy record would throw away the
half the reader wanted.

**What counts as the same problem** is the finding fingerprint from
:mod:`mira.checks.models`: the path, a five-line bucket, and the normalised
description. Coarse on the line number because two producers rarely agree to
the line; keyed on the description because two producers *do* usually use the
same nouns, and because a fingerprint keyed only on position would fold two
genuinely different problems on one line into one.

**Which producer owns the merged finding** is decided by a fixed precedence,
not by iteration order: native, then tool, then natural language, then external
context, breaking ties on the check id. A deterministic winner is what makes a
run reproducible — if it depended on which check happened to finish first, two
identical runs could attribute the same finding to different checks, and the
history would show a finding moving between checks for no reason.

**A producer whose finding was merged away keeps its own state.** It really did
find something, and rewriting its result to ``pass`` would be a lie that
happens to make the summary tidier. It records the fingerprint it contributed
instead, so the dashboard can show "also found by ruff" on the surviving entry
and "1 finding, merged into native.migrations" on the other.
"""

from __future__ import annotations

import logging

from mira.checks.models import CheckFinding, CheckResult

logger = logging.getLogger(__name__)

# Lower sorts first and therefore wins ownership. Native checks lead because
# they are the ones whose wording a team has read; a tool's rule id and a
# model's prose are both more useful hanging off that than owning the entry.
_ORIGIN_PRECEDENCE = {
    "native": 0,
    "tool": 1,
    "natural_language": 2,
    "context": 3,
}


def _rank(result: CheckResult) -> tuple[int, str]:
    return (_ORIGIN_PRECEDENCE.get(result.origin, 9), result.check_id)


def _merge(primary: CheckFinding, other: CheckFinding) -> CheckFinding:
    """Fold ``other`` into ``primary``, keeping every distinct evidence item.

    Evidence is deduplicated on its own content, so a producer that quoted the
    identical line does not add a second copy of it — while one that quoted a
    different span, or added a rule id, does.
    """
    seen = {(item.path, item.start_line, item.snippet) for item in primary.evidence}
    for item in other.evidence:
        key = (item.path, item.start_line, item.snippet)
        if key in seen:
            continue
        seen.add(key)
        primary.evidence.append(item)
    for source in other.sources:
        if source not in primary.sources:
            primary.sources.append(source)
    # The other producer's own words, kept as a second paragraph rather than
    # discarded: two descriptions of one problem is usually two useful halves.
    if other.detail and other.detail not in primary.detail:
        primary.detail = f"{primary.detail}\n\nAlso reported by {other.sources[0]}: {other.detail}"
    # Severity is advisory, and the more severe reading is the one worth
    # showing: a reader who dismisses it should do so knowing what was claimed.
    order = {"blocker": 3, "warning": 2, "suggestion": 1}
    if order.get(other.severity, 0) > order.get(primary.severity, 0):
        primary.severity = other.severity
    return primary


def deduplicate(results: list[CheckResult]) -> list[CheckResult]:
    """Fold identical findings together across every check in one run.

    Edits the results in place and returns the same list, in the order it was
    given — that order is the registry's, and the dashboard renders by it. In
    place because these results are the run's own, built moments earlier and
    not yet persisted; copying them would leave two versions of one run in
    memory and a real chance of writing the wrong one.
    """
    owner: dict[str, tuple[CheckResult, CheckFinding]] = {}
    merged_away: dict[str, list[str]] = {}

    for result in sorted(results, key=_rank):
        kept: list[CheckFinding] = []
        for finding in result.findings:
            key = finding.fingerprint
            if not key:
                kept.append(finding)
                continue
            existing = owner.get(key)
            if existing is None:
                owner[key] = (result, finding)
                kept.append(finding)
                continue
            _merge(existing[1], finding)
            merged_away.setdefault(result.check_id, []).append(key)
            logger.debug(
                "Folded %s's finding %s into %s", result.check_id, key[:8], existing[0].check_id
            )
        result.findings = kept

    for result in results:
        folded = merged_away.get(result.check_id) or []
        if not folded:
            continue
        owners = sorted({owner[key][0].check_id for key in folded if key in owner})
        note = (
            f" {len(folded)} of them {'was' if len(folded) == 1 else 'were'} also found by "
            f"{', '.join(owners)} and {'is' if len(folded) == 1 else 'are'} reported there."
        )
        if note not in result.summary:
            result.summary = (result.summary or "").rstrip() + note

    return results


def duplicate_findings(results: list[CheckResult]) -> list[CheckFinding]:
    """The findings more than one producer reported. For tests and the API."""
    return [finding for result in results for finding in result.findings if finding.deduplicated]
