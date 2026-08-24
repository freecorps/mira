"""Turning a decision into something a person can act on.

Two audiences, two renderings. The **public** explanation goes on the pull
request and to the platform status check: it tells the author what happened and
what would change it, in the words of the policy, without quoting the policy's
internals back at them. The **administrative** explanation is the dashboard and
audit view: every factor, every input, every capability, including the ones the
author has no business seeing (who is on an allowlist, what the thresholds are).

Both are generated from the stored decision, never from live state, so the
explanation an operator reads next year is the explanation the gate gave.
"""

from __future__ import annotations

from mira.gate.models import GateDecision, ReasonCode

_STATE_HEADLINE = {
    "approved": "✅ **Merge gate: approved**",
    "would_approve": "🟡 **Merge gate: would approve** (dry run)",
    "not_approved": "🚫 **Merge gate: not approved**",
    "skipped": "⚪ **Merge gate: not applicable**",
    "error": "⚠️ **Merge gate: could not decide**",
}

_STATE_SUMMARY = {
    "approved": "Mira approved this pull request.",
    "would_approve": (
        "Mira would approve this pull request. Nothing was submitted — the gate "
        "is running in shadow mode, or this platform cannot record an approval."
    ),
    "not_approved": "Mira did not approve this pull request.",
    "skipped": "The merge gate does not apply to this pull request.",
    "error": (
        "The merge gate could not complete its evaluation. Nothing was submitted; "
        "an incomplete evaluation never approves."
    ),
}

# Short status-check titles, which most platforms truncate hard.
_STATE_TITLE = {
    "approved": "Approved",
    "would_approve": "Would approve (dry run)",
    "not_approved": "Not approved",
    "skipped": "Not applicable",
    "error": "Could not decide",
}


def status_title(decision: GateDecision) -> str:
    return _STATE_TITLE.get(decision.state, decision.state)


def status_conclusion(decision: GateDecision) -> str:
    """Platform-neutral conclusion for a check run / commit status.

    Only a delivered approval is ever ``success``. Every other state is
    ``neutral`` — including ``would_approve``, because a shadow run that turned
    the merge box green would not be a shadow run.
    """
    if decision.state == "approved":
        return "success"
    if decision.state == "not_approved" and decision.request_changes:
        return "failure"
    return "neutral"


def public_explanation(decision: GateDecision) -> str:
    """Markdown for the pull request. Explains the outcome, not the policy."""
    lines = [
        _STATE_HEADLINE.get(decision.state, f"**Merge gate: {decision.state}**"),
        "",
        _STATE_SUMMARY.get(decision.state, ""),
        "",
    ]

    blocking = [r for r in decision.reasons if r.kind == "block"]
    skipping = [r for r in decision.reasons if r.kind == "skip"]
    notes = [r for r in decision.reasons if r.kind == "info"]

    if blocking:
        lines.append("**Why not:**")
        lines.extend(f"- {reason.message}" for reason in blocking)
        lines.append("")
    if skipping:
        lines.append("**Out of scope because:**")
        lines.extend(f"- {reason.message}" for reason in skipping)
        lines.append("")
    if notes:
        lines.extend(f"> {reason.message}" for reason in notes)
        lines.append("")

    if decision.state != "skipped":
        lines.append(
            f"Risk score **{decision.risk_score}/100** ({decision.risk_band}). "
            f"Policy `{decision.policy_version}`."
        )
        if decision.factors:
            lines.append("")
            lines.append("<details><summary>What went into the score</summary>")
            lines.append("")
            lines.append("| Factor | Points | Detail |")
            lines.append("|---|---:|---|")
            for factor in decision.factors:
                detail = factor.detail.replace("|", "\\|")
                lines.append(f"| {factor.label} | {factor.points} | {detail} |")
            lines.append("")
            lines.append("</details>")

    if decision.state in {"would_approve", "not_approved"}:
        lines.append("")
        lines.append(
            "_The merge gate never merges anything and never replaces a human "
            "review. It only records whether Mira would put its name on this._"
        )
    return "\n".join(lines).strip() + "\n"


def admin_explanation(decision: GateDecision) -> str:
    """Markdown for the dashboard: everything, including the policy internals."""
    inputs = decision.inputs
    lines = [
        f"### {_STATE_HEADLINE.get(decision.state, decision.state)}",
        "",
        f"- **PR**: `{inputs.owner}/{inputs.repo}#{inputs.pr_number}` "
        f"({inputs.platform}) by `{inputs.pr_author or 'unknown'}`",
        f"- **Head**: `{inputs.head_sha[:12] or 'unknown'}` onto `{inputs.base_branch}`",
        f"- **Mode**: `{decision.mode}` · **Policy**: `{decision.policy_version}`",
        f"- **Risk**: {decision.risk_score}/100 ({decision.risk_band})",
        f"- **Delivery**: `{decision.delivery_state}`"
        + (f" → `{decision.delivery_ref}`" if decision.delivery_ref else "")
        + (f" after {decision.delivery_attempts} attempt(s)" if decision.delivery_attempts else ""),
    ]
    if decision.error:
        lines.append(f"- **Error**: {decision.error}")
    lines.append("")

    if decision.reasons:
        lines.append("**Reasons**")
        lines.append("")
        lines.append("| Kind | Code | Message |")
        lines.append("|---|---|---|")
        for reason in decision.reasons:
            message = reason.message.replace("|", "\\|")
            lines.append(f"| {reason.kind} | `{reason.code}` | {message} |")
        lines.append("")

    if decision.factors:
        lines.append("**Risk factors**")
        lines.append("")
        lines.append("| Code | Factor | Points | Detail |")
        lines.append("|---|---|---:|---|")
        for factor in decision.factors:
            detail = factor.detail.replace("|", "\\|")
            lines.append(f"| `{factor.code}` | {factor.label} | {factor.points} | {detail} |")
        lines.append("")

    lines.append("**Inputs**")
    lines.append("")
    lines.extend(
        [
            f"- Changed files: {inputs.changed_files} "
            f"(+{inputs.added_lines}/-{inputs.deleted_lines}), "
            f"{len(inputs.generated_paths)} generated",
            f"- Protected matches: {', '.join(sorted(inputs.protected_matches)[:10]) or '—'}",
            f"- CODEOWNERS: `{inputs.codeowners_status}`, "
            f"owned: {', '.join(sorted(inputs.codeowner_matches)[:10]) or '—'}",
            f"- CI: `{inputs.ci.state}` over {inputs.ci.total} check(s)",
            f"- Findings: {inputs.open_blockers} blocker(s), {inputs.open_findings} open, "
            f"worst `{inputs.worst_severity or 'none'}`",
            f"- Review complete: {inputs.review_complete}; index ready: {inputs.index_ready}",
            f"- Labels: {', '.join(sorted(inputs.labels)[:10]) or '—'}",
            f"- Author association: `{inputs.author_association}`",
            f"- Human review states: {inputs.human_states or '—'}",
        ]
    )

    if decision.capabilities:
        lines.append("")
        lines.append("**Provider capabilities**")
        lines.append("")
        for key, value in sorted(decision.capabilities.items()):
            if key == "notes":
                continue
            lines.append(f"- `{key}`: {value}")
        for note in decision.capabilities.get("notes") or []:
            lines.append(f"- _{note}_")
    return "\n".join(lines).strip() + "\n"


def one_line(decision: GateDecision) -> str:
    """A log line: state, score, and the reason that decided it."""
    decisive = next(
        (r for r in decision.reasons if r.kind == "block"),
        next((r for r in decision.reasons if r.kind == "skip"), None),
    )
    tail = f" — {decisive.code}: {decisive.message}" if decisive else ""
    return f"{decision.state} (risk {decision.risk_score}/100){tail}"


def would_have_approved(decision: GateDecision) -> bool:
    """Whether this decision counts as a candidate approval in a shadow rollout.

    The measurement that makes a dry run worth running: how many PRs would have
    been approved, so the false-approval rate can be computed against what
    actually happened to them.
    """
    return decision.state in {"approved", "would_approve"} and not any(
        reason.code == ReasonCode.RISK_ABOVE_THRESHOLD for reason in decision.reasons
    )
