"""Markdown and JSON renderings of a backtest run."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

from mira.quality.models import BacktestRun, VariantSummary


def _pct(value: float | None) -> str:
    return "—" if value is None else f"{value * 100:.0f}%"


def _usd(value: float) -> str:
    return f"${value:.4f}" if value < 1 else f"${value:.2f}"


def _when(epoch: float) -> str:
    if not epoch:
        return "—"
    return datetime.fromtimestamp(epoch, tz=UTC).strftime("%Y-%m-%d %H:%M UTC")


def _delta(b: float | None, a: float | None) -> str:
    if a is None or b is None:
        return "—"
    return f"{(b - a) * 100:+.0f} pts"


def render_json(run: BacktestRun) -> str:
    return json.dumps(run.to_dict(), indent=2, sort_keys=False)


def _summary_row(s: VariantSummary) -> str:
    sc = s.score
    return (
        f"| {s.variant} | `{s.config_label or 'default'}` | {s.prs} | {s.prs_with_ground_truth} "
        f"| {sc.findings} | {sc.tp} | {sc.fp} | {sc.unlabelled} | {_pct(sc.precision)} "
        f"| {_pct(sc.labelled_precision)} | {_pct(sc.recall)} "
        f"| {s.prompt_tokens + s.completion_tokens:,} | {_usd(s.cost_usd)} "
        f"| {s.mean_latency_ms / 1000:.1f}s | {s.errors} |"
    )


def render_markdown(run: BacktestRun) -> str:
    lines: list[str] = [
        f"# Mira backtest — {run.owner}/{run.repo}",
        "",
        f"Run `{run.id}` · {run.status} · {len({r.pr_number for r in run.results})} pull "
        f"request(s) · started {_when(run.created_at)} · estimated "
        f"{_usd(run.estimated_cost_usd)}, spent {_usd(run.total_cost_usd)}",
        "",
        "Dry run: nothing was posted. Findings are scored against human review comments, "
        "later fix/revert commits on the same lines, and feedback recorded on Mira's own "
        "findings. *Strict* precision counts findings with no evidence either way as misses; "
        "*labelled* precision only counts findings the evidence spoke about.",
        "",
        "## Summary",
        "",
        "| Variant | Config | PRs | With ground truth | Findings | TP | FP | Unlabelled "
        "| Precision (strict) | Precision (labelled) | Recall | Tokens | Cost | Mean latency "
        "| Errors |",
        "|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    summaries = run.summaries()
    lines.extend(_summary_row(s) for s in summaries)

    by_name = {s.variant: s for s in summaries}
    if "A" in by_name and "B" in by_name:
        a, b = by_name["A"], by_name["B"]
        lines += [
            "",
            "### A/B comparison (B − A)",
            "",
            "| Metric | A | B | Δ |",
            "|---|---:|---:|---:|",
            f"| Precision (strict) | {_pct(a.score.precision)} | {_pct(b.score.precision)} "
            f"| {_delta(b.score.precision, a.score.precision)} |",
            f"| Precision (labelled) | {_pct(a.score.labelled_precision)} "
            f"| {_pct(b.score.labelled_precision)} "
            f"| {_delta(b.score.labelled_precision, a.score.labelled_precision)} |",
            f"| Recall | {_pct(a.score.recall)} | {_pct(b.score.recall)} "
            f"| {_delta(b.score.recall, a.score.recall)} |",
            f"| Findings | {a.score.findings} | {b.score.findings} "
            f"| {b.score.findings - a.score.findings:+d} |",
            f"| Cost | {_usd(a.cost_usd)} | {_usd(b.cost_usd)} | {b.cost_usd - a.cost_usd:+.4f} |",
            f"| Mean latency | {a.mean_latency_ms / 1000:.1f}s | {b.mean_latency_ms / 1000:.1f}s "
            f"| {(b.mean_latency_ms - a.mean_latency_ms) / 1000:+.1f}s |",
        ]

    lines += [
        "",
        "## Per pull request",
        "",
        "| PR | Variant | Findings | TP | FP | Signals | Caught | Precision | Recall | Tokens "
        "| Cost | Latency | Status |",
        "|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|",
    ]
    for r in run.results:
        sc = r.score
        status = r.error or r.skipped_reason or ("blocked writes!" if r.blocked_writes else "ok")
        title = r.pr_title.replace("|", "\\|")[:60]
        lines.append(
            f"| [#{r.pr_number}]({r.pr_url}) {title} | {r.variant} | {sc.findings} | {sc.tp} "
            f"| {sc.fp} | {sc.positives} | {sc.positives_caught} | {_pct(sc.precision)} "
            f"| {_pct(sc.recall)} | {r.prompt_tokens + r.completion_tokens:,} | {_usd(r.cost_usd)} "
            f"| {r.latency_ms / 1000:.1f}s | {status.replace('|', '/')} |"
        )

    findings_rows = [(r, f) for r in run.results for f in r.findings]
    if findings_rows:
        lines += ["", "## Findings", ""]
        for r, f in findings_rows:
            matched = f" ({', '.join(f.matched)})" if f.matched else ""
            lines.append(
                f"- **#{r.pr_number} [{r.variant}]** `{f.path}:{f.line}` {f.severity} "
                f"{f.category}: {f.title} — *{f.label}*{matched}"
            )

    if run.notes:
        lines += ["", "## Notes", ""]
        lines.extend(f"- {note}" for note in run.notes)
    return "\n".join(lines) + "\n"


def render_plan(plan: Any, variants: list[Any], limit_usd: float) -> str:
    """The pre-run summary printed before anything is spent."""
    lines = [
        f"Backtest plan: {len(plan.prs)} pull request(s) × {len(variants)} configuration(s)",
        "",
    ]
    for entry in plan.prs:
        est = ", ".join(f"{k}: {_usd(v)}" for k, v in entry.estimated_cost_usd.items())
        problem = f"  [{entry.error}]" if entry.error else ""
        lines.append(
            f"  #{entry.merged.number:<6} {entry.merged.title[:60]:<60} "
            f"{len(entry.diff_text):>8,} chars  {est}{problem}"
        )
    lines += [
        "",
        f"Estimated total: {_usd(plan.estimated_cost_usd)} (limit {_usd(limit_usd)})",
    ]
    lines.extend(f"Note: {note}" for note in plan.notes)
    return "\n".join(lines)
