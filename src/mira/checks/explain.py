"""Saying what a check run found, to a human who did not run it.

Every renderer here obeys the same rule, and it is the phase's acceptance
criterion written as a formatting constraint: **a reader must be able to tell a
problem with their pull request from a problem with Mira without knowing
anything about Mira.**

So violations and non-violations are never mixed into one list. Violations get
the first section, with their evidence. Everything Mira could not answer gets
its own section, headed in plain words, saying that it is Mira's problem.
Skips that simply did not apply are folded into a single quiet line, because a
reader does not need five sentences explaining that the migration check found
no migration.

Nothing rendered here is ever parsed back. Evidence quotes repository text,
which means it quotes whatever the pull request's author wrote, so it is fenced
and truncated on the way out and no downstream code reads a rendered comment to
recover a decision.
"""

from __future__ import annotations

from mira.checks.models import CheckResult, CheckRun, SkipReason

# Characters of one evidence snippet in a rendered comment. Evidence is already
# bounded when it is stored; this is the second, tighter bound for a surface
# where a hundred long lines is a wall rather than a report.
_SNIPPET = 240

_STATE_ICON = {
    "pass": "✅",
    "violation": "❌",
    "infrastructure_error": "⚠️",
    "timeout": "⏱️",
    "skipped": "➖",
}

_VERDICT_LINE = {
    "pass": "Every blocking check answered, and none of them objected.",
    "violation": "A blocking check found a problem with this pull request.",
    "incomplete": (
        "A blocking check could not answer. This is not a finding against this pull "
        "request — it is Mira reporting that it does not know."
    ),
    "not_run": "No check ran for this pull request.",
}


def one_line(run: CheckRun) -> str:
    """A log line: the verdict and the state tally."""
    counts = run.counts()
    tally = ", ".join(f"{state}={counts[state]}" for state in sorted(counts) if counts[state])
    return f"{run.verdict} ({tally or 'nothing ran'}) in {run.duration_seconds:.2f}s"


def _fence(text: str) -> str:
    """A snippet, fenced so repository text cannot restructure the comment."""
    cleaned = (text or "").strip()
    if not cleaned:
        return ""
    if len(cleaned) > _SNIPPET:
        cleaned = cleaned[:_SNIPPET] + " …"
    # A snippet containing a fence would otherwise close ours and continue as
    # markdown, which is how a diff line ends up rendering as a heading.
    cleaned = cleaned.replace("```", "``​`")
    return f"\n```\n{cleaned}\n```\n"


def _evidence_lines(result: CheckResult) -> list[str]:
    lines: list[str] = []
    for finding in result.findings:
        lines.append(f"- **{finding.title}**")
        if finding.detail:
            lines.append(f"  {finding.detail.splitlines()[0]}")
        if finding.deduplicated:
            lines.append(f"  _Also found by: {', '.join(sorted(finding.sources))}._")
        for item in finding.evidence:
            locator = item.locator or item.detail or "evidence"
            link = f"[{locator}]({item.url})" if item.url else f"`{locator}`"
            lines.append(f"  - {link}{f' — {item.detail}' if item.detail else ''}")
            snippet = _fence(item.snippet)
            if snippet:
                lines.append("    " + snippet.strip().replace("\n", "\n    "))
    return lines


def public_explanation(run: CheckRun) -> str:
    """The comment Mira posts on the pull request.

    Structured so the first thing a reader sees is whichever of the two
    sections is non-empty — and so that a reader whose pull request is fine but
    whose CI check timed out is told that in those words rather than being left
    to infer it from a yellow icon.
    """
    lines: list[str] = ["## Mira pre-merge checks", "", _VERDICT_LINE.get(run.verdict, ""), ""]

    violations = [result for result in run.results if result.is_violation]
    unanswered = [
        result
        for result in run.results
        if result.incomplete or result.state in {"infrastructure_error", "timeout"}
    ]
    passed = [result for result in run.results if result.state == "pass"]
    quiet = [
        result for result in run.results if result.state == "skipped" and not result.incomplete
    ]

    if violations:
        lines.append("### What the checks found")
        lines.append("")
        for result in violations:
            blocking = " — **blocks merge**" if result.blocking else ""
            lines.append(f"**{_STATE_ICON['violation']} {result.title}**{blocking}")
            lines.append("")
            lines.append(result.summary)
            lines.extend(_evidence_lines(result))
            lines.append("")

    if unanswered:
        lines.append("### What Mira could not answer")
        lines.append("")
        lines.append(
            "These are not findings against this pull request. Each one is a check "
            "that did not reach a conclusion, and the reason is Mira's side of the "
            "line, not yours."
        )
        lines.append("")
        for result in unanswered:
            icon = _STATE_ICON.get(result.state, "⚠️")
            blocking = " — **blocks merge**" if result.blocking else ""
            lines.append(f"- {icon} **{result.title}**{blocking}: {result.summary}")
        lines.append("")

    if passed:
        names = ", ".join(f"`{result.check_id}`" for result in passed)
        lines.append(f"<details><summary>{len(passed)} check(s) passed</summary>")
        lines.append("")
        lines.append(names)
        lines.append("")
        lines.append("</details>")
        lines.append("")

    if quiet:
        off = sum(1 for result in quiet if result.skip_reason == SkipReason.DISABLED)
        not_applicable = len(quiet) - off
        parts = []
        if not_applicable:
            parts.append(f"{not_applicable} did not apply to this change")
        if off:
            parts.append(f"{off} are switched off for this repository")
        lines.append(f"_{len(quiet)} check(s) skipped: {'; '.join(parts)}._")
        lines.append("")

    lines.append(f"_Policy `{run.policy_version}`, {run.duration_seconds:.1f}s._")
    return "\n".join(line for line in lines if line is not None).strip()


def admin_explanation(run: CheckRun) -> str:
    """The dashboard's fuller version: every check, with its own timing.

    Includes the checks that passed and the ones that did not apply, because
    the question an operator asks here is "what actually ran" — and the answer
    is only useful if the silent ones are in it.
    """
    lines = [
        f"Verdict: {run.verdict}",
        f"Policy: {run.policy_version}",
        f"Duration: {run.duration_seconds:.2f}s",
        "",
    ]
    for result in run.results:
        icon = _STATE_ICON.get(result.state, "•")
        extra = ""
        if result.skip_reason:
            extra = f" [{result.skip_reason}]"
        elif result.error:
            extra = f" [{result.error[:120]}]"
        lines.append(
            f"{icon} {result.check_id} v{result.check_version} "
            f"({result.origin}, {result.mode}) — {result.state}{extra} "
            f"in {result.duration_seconds:.2f}s"
        )
        if result.summary:
            lines.append(f"    {result.summary.splitlines()[0][:200]}")
    return "\n".join(lines)


def status_conclusion(run: CheckRun) -> str:
    """The check-run conclusion to publish: success, failure or neutral.

    ``neutral`` for an incomplete run rather than ``failure``, deliberately.
    The published status is a statement to everyone reading the pull request,
    and "Mira could not run its linter" is not a failing build. The gate is
    where incompleteness costs something, and it costs it by refusing to
    approve — not by putting a red cross next to somebody's change.
    """
    if run.verdict == "violation":
        return "failure"
    if run.verdict == "pass":
        return "success"
    return "neutral"


def status_title(run: CheckRun) -> str:
    counts = run.counts()
    if run.verdict == "violation":
        blocking = sum(1 for result in run.results if result.blocking and result.is_violation)
        return f"{blocking or counts['violation']} check(s) found a problem"
    if run.verdict == "incomplete":
        return "Some checks could not answer"
    if run.verdict == "pass":
        return f"{counts['pass']} check(s) passed"
    return "No checks ran"
