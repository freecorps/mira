"""Rendering a triage run for the two audiences that read one.

The public rendering goes on the pull request, where the person who opened it
and everyone watching the repository will see it. Three rules shape it.

**Nobody is mentioned.** Every identity is rendered as literal text, never as
``@login``. A suggestion that pings four people has notified three of them
about work they have not agreed to do, on every push, and the fastest way to
have this feature switched off is to make it noisy. The reader picks a name and
requests the review themselves — which is also the moment a human decides,
which is where the decision belongs.

**Mira's failures are labelled as Mira's.** A run that could not read CODEOWNERS
says so in those words, in its own section, and never renders as "no
suggestions". The difference between "there is nobody obvious" and "we could not
look" is the whole reason this phase records the two separately.

**Every name carries its reason.** One line per candidate saying what connects
them to this change, and the evidence behind it. A suggestion nobody can check
is a suggestion nobody should follow.

The admin rendering adds what a public comment should not carry: the excluded
names and why, the score arithmetic, and the unmasked evidence.
"""

from __future__ import annotations

import re

from mira.triage.models import (
    Evidence,
    ExclusionReason,
    ReviewerCandidate,
    TriageRun,
)

# Evidence lines shown per candidate in the public comment. The rest is in the
# dashboard: a pull-request comment is read in three seconds.
PUBLIC_EVIDENCE_PER_CANDIDATE = 3

_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")

_SIGNAL_WORDS = {
    "codeowners": "listed in CODEOWNERS",
    "authored": "has changed these files",
    "reviewed": "has reviewed these files",
}

_EXCLUSION_WORDS = {
    ExclusionReason.AUTHOR: "opened this pull request",
    ExclusionReason.BOT: "is a machine account",
    ExclusionReason.OPTED_OUT: "has opted out of suggestions",
    ExclusionReason.NO_EVIDENCE: "had no evidence behind the name",
    ExclusionReason.BELOW_THRESHOLD: "scored below the floor",
    ExclusionReason.NOT_TOP_RANKED: "ranked below the cut",
}


def _clean(text: str, limit: int = 200) -> str:
    """One line, no control characters, no backticks to escape a code span."""
    flattened = _CONTROL.sub("", str(text or "")).replace("\r", " ").replace("\n", " ")
    return flattened.replace("`", "'")[:limit]


def code(text: str, limit: int = 200) -> str:
    return f"`{_clean(text, limit)}`"


def mask_email(identity: str) -> str:
    """``dana@example.com`` becomes ``d***@example.com``.

    CODEOWNERS is in the repository, so this hides nothing from anybody who can
    read the code. It is about the *comment*: a pull-request thread on a public
    repository is indexed, quoted and mirrored in ways a file in a private
    directory tree is not, and republishing a colleague's address into one to
    make a suggestion slightly prettier is not a trade worth making. The
    dashboard shows the address in full to an admin.
    """
    if "@" not in identity:
        return identity
    local, _, domain = identity.partition("@")
    head = local[:1] if local else ""
    return f"{head}***@{domain}"


def public_identity(candidate: ReviewerCandidate) -> str:
    if candidate.kind == "email":
        return mask_email(candidate.identity)
    return candidate.identity


def _evidence_line(item: Evidence) -> str:
    locator = item.locator
    parts = []
    if locator:
        parts.append(code(locator))
    if item.detail:
        parts.append(_clean(item.detail, 120))
    text = " — ".join(parts) if parts else _clean(item.source or "evidence", 60)
    if item.url:
        return f"{text} ([link]({_clean(item.url, 300)}))"
    return text


def _candidate_lines(candidate: ReviewerCandidate) -> list[str]:
    reasons = ", ".join(
        f"{_SIGNAL_WORDS.get(item.kind, item.kind)} ({_clean(item.detail, 80)})"
        for item in candidate.contributions
    )
    lines = [f"- {code(public_identity(candidate))} — {reasons}"]
    shown = 0
    for contribution in candidate.contributions:
        for item in contribution.evidence:
            if shown >= PUBLIC_EVIDENCE_PER_CANDIDATE:
                break
            lines.append(f"  - {_evidence_line(item)}")
            shown += 1
        if shown >= PUBLIC_EVIDENCE_PER_CANDIDATE:
            break
    return lines


def _degradations(run: TriageRun) -> list[str]:
    """The signals that did not answer, in Mira's own name."""
    lines = []
    for report in run.signals:
        if report.answered:
            continue
        label = {
            "codeowners": "CODEOWNERS",
            "authored": "file history",
            "reviewed": "review history",
        }.get(report.kind, report.kind)
        lines.append(f"- **{label}**: {_clean(report.detail, 300)}")
    return lines


def classification_line(run: TriageRun) -> str:
    classification = run.classification
    kinds = ", ".join(classification.kinds[:4]) or "unclassified"
    areas = ", ".join(code(area) for area in classification.areas[:3])
    line = (
        f"**{classification.size}** · {classification.changed_files} file(s), "
        f"{classification.changed_lines} line(s) · {kinds}"
    )
    return f"{line} · {areas}" if areas else line


def public_explanation(run: TriageRun) -> str:
    """What goes on the pull request."""
    status = run.status
    lines = ["### Reviewer suggestions", "", classification_line(run), ""]

    if status == "ok":
        lines.append("Who is closest to these files, and why:")
        lines.append("")
        for candidate in run.candidates:
            lines.extend(_candidate_lines(candidate))
        lines.append("")
        lines.append("_A suggestion, not a review request — Mira does not assign reviewers._")
    elif status == "no_candidates":
        lines.append(
            "Mira looked at who owns and who has worked on these files and found "
            "nobody to suggest. That is the answer, not a failure: on a small "
            "repository the only person with history here is often the one who "
            "opened this."
        )
    elif status == "not_run":
        lines.append("Reviewer triage did not run for this pull request.")
    else:
        lines.append(
            "**Mira could not work out who to suggest.** This is a problem with "
            "Mira, not with this pull request, and it is not a statement that "
            "nobody is available:"
        )
        lines.append("")
        lines.extend(_degradations(run) or [f"- {_clean(run.error, 300)}"])

    if status == "ok" and run.degraded:
        lines.append("")
        lines.append("Some of what Mira reads was unavailable, so this list may be short:")
        lines.append("")
        lines.extend(_degradations(run))

    for note in run.notes:
        lines.append("")
        lines.append(f"_{_clean(note, 300)}_")

    return "\n".join(lines).strip()


def admin_explanation(run: TriageRun) -> str:
    """What the dashboard shows: the arithmetic, and everyone who was dropped."""
    inputs = run.inputs
    lines = [
        f"**{inputs.owner}/{inputs.repo}#{inputs.pr_number}** — status `{run.status}`",
        "",
        f"- policy: `{run.policy_version}`",
        f"- head: `{_clean(inputs.head_sha, 40) or 'unknown'}`",
        f"- ownership read at: `{_clean(inputs.ownership_ref, 40) or 'not read'}`",
        f"- duration: {run.duration_seconds:.3f}s over {run.attempts} attempt(s)",
        "",
        f"**Classification** — {classification_line(run)}",
        "",
        "**Signals**",
    ]
    for report in run.signals:
        lines.append(
            f"- `{report.kind}` — `{report.status}`, {report.candidates} candidate(s): "
            f"{_clean(report.detail, 300)}"
        )

    lines.extend(["", "**Candidates**"])
    if not run.candidates:
        lines.append("- none")
    for index, candidate in enumerate(run.candidates, start=1):
        breakdown = " + ".join(
            f"{item.kind} {item.raw:g}×{item.weight:g}={item.score:g}"
            for item in candidate.contributions
        )
        penalty = (
            f" − load {candidate.load_penalty:g} ({candidate.open_reviews} open)"
            if candidate.load_penalty
            else ""
        )
        lines.append(
            f"{index}. `{_clean(candidate.identity, 100)}` ({candidate.kind}) — "
            f"{candidate.score:g} = {breakdown}{penalty}"
        )
        for item in candidate.evidence:
            lines.append(f"   - {_evidence_line(item)}")

    lines.extend(["", "**Not suggested**"])
    if not run.excluded:
        lines.append("- nobody was dropped")
    for exclusion in run.excluded:
        why = _EXCLUSION_WORDS.get(exclusion.reason, exclusion.reason)
        detail = f" — {_clean(exclusion.detail, 200)}" if exclusion.detail else ""
        lines.append(f"- `{_clean(exclusion.identity, 100)}`: {why}{detail}")

    if run.notes:
        lines.extend(["", "**Notes**"])
        lines.extend(f"- {_clean(note, 300)}" for note in run.notes)
    if run.error:
        lines.extend(["", f"**Error** — {_clean(run.error, 500)}"])
    return "\n".join(lines)


def one_line(run: TriageRun) -> str:
    """The log line: what happened, in one sentence."""
    inputs = run.inputs
    who = ", ".join(run.suggested) or "nobody"
    degraded = " (degraded)" if run.degraded else ""
    return (
        f"{inputs.owner}/{inputs.repo}#{inputs.pr_number} triage {run.status}{degraded}: "
        f"{who} in {run.duration_seconds:.2f}s"
    )
