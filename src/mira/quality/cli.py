"""``mira backtest``, ``mira escaped-bugs`` and ``mira peer-recall`` — on the main CLI group."""

from __future__ import annotations

import asyncio
import json
import logging
import sys
from datetime import UTC, datetime
from typing import Any

import click


def _parse_repo(value: str) -> tuple[str, str]:
    owner, _, repo = (value or "").strip().strip("/").rpartition("/")
    if not owner or not repo:
        raise click.BadParameter("expected owner/name (GitLab groups: group/sub/name)")
    return owner, repo


def _parse_since(value: str | None) -> float:
    if not value:
        return 0.0
    try:
        parsed = datetime.fromisoformat(value.strip())
    except ValueError as exc:
        raise click.BadParameter("expected a date such as 2026-07-01") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.timestamp()


def _parse_prs(value: str | None) -> list[int]:
    if not value:
        return []
    out = []
    for part in value.split(","):
        part = part.strip().lstrip("#")
        if not part:
            continue
        if not part.isdigit():
            raise click.BadParameter(f"not a pull request number: {part!r}")
        out.append(int(part))
    return list(dict.fromkeys(out))


def _load(config_path: str | None) -> Any:
    from mira.config import load_config
    from mira.exceptions import MiraError

    try:
        return load_config(config_path)
    except MiraError as exc:
        raise click.ClickException(str(exc)) from exc


def _provider(platform: str, token: str | None) -> Any:
    if not token:
        raise click.UsageError(
            "--token (or GITHUB_TOKEN / MIRA_GIT_TOKEN) is required to read repository history"
        )
    from mira.providers import create_provider, get_available_providers

    try:
        return create_provider(platform, token)
    except ValueError as err:
        available = ", ".join(get_available_providers()) or "(none)"
        raise click.UsageError(f"Unknown platform {platform!r}. Available: {available}") from err


def _logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.INFO if verbose else logging.WARNING,
        format="%(name)s %(levelname)s: %(message)s",
        stream=sys.stderr,
    )


@click.command("backtest")
@click.option("--repo", "repository", required=True, help="owner/name to replay")
@click.option(
    "--platform",
    type=click.Choice(["github", "gitlab", "forgejo"]),
    default=None,
    help="Hosting platform (default: provider.type from the config)",
)
@click.option("--prs", "prs", default=None, help="Comma-separated merged PR numbers, e.g. 12,15")
@click.option("--since", default=None, help="Replay PRs merged on or after this date (YYYY-MM-DD)")
@click.option("--limit", type=int, default=10, show_default=True, help="PRs to replay")
@click.option("--config", "config_path", default=None, help="Configuration A (.mira.yaml)")
@click.option("--config-b", "config_b_path", default=None, help="Configuration B, for an A/B run")
@click.option("--token", envvar="MIRA_GIT_TOKEN", default=None, help="Git platform API token")
@click.option("--github-token", envvar="GITHUB_TOKEN", default=None, help="Alias for --token")
@click.option("--bot-name", envvar="MIRA_BOT_NAME", default="miracodeai", show_default=True)
@click.option("--max-prs", type=int, default=None, help="Override backtest.max_prs")
@click.option("--max-concurrency", type=int, default=None, help="Override backtest.max_concurrency")
@click.option(
    "--max-cost", type=float, default=None, help="Override backtest.max_estimated_cost_usd"
)
@click.option(
    "--confirm",
    "--yes",
    "confirm",
    is_flag=True,
    help="Actually run the reviews. Without it, only the plan and cost estimate are printed.",
)
@click.option(
    "--format", "output_format", type=click.Choice(["markdown", "json"]), default="markdown"
)
@click.option("--output", "output_path", default=None, help="Write the report to this file")
@click.option("--no-save", is_flag=True, help="Do not store the run for the dashboard")
@click.option("--verbose", is_flag=True)
def backtest(
    repository: str,
    platform: str | None,
    prs: str | None,
    since: str | None,
    limit: int,
    config_path: str | None,
    config_b_path: str | None,
    token: str | None,
    github_token: str | None,
    bot_name: str,
    max_prs: int | None,
    max_concurrency: int | None,
    max_cost: float | None,
    confirm: bool,
    output_format: str,
    output_path: str | None,
    no_save: bool,
    verbose: bool,
) -> None:
    """Replay Mira over merged pull requests without posting anything.

    Prints a plan with a cost estimate first; pass --confirm to run it. Each
    PR's findings are scored against later human review comments, fix/revert
    commits on the same lines and recorded feedback. With --config-b, both
    configurations review every PR and the report compares them.
    """
    from mira.quality.backtest import BacktestRunner, Variant
    from mira.quality.report import render_json, render_markdown, render_plan

    _logging(verbose)
    owner, repo = _parse_repo(repository)
    numbers = _parse_prs(prs)
    since_ts = _parse_since(since)

    config_a = _load(config_path)
    variants = [Variant("A", config_path or "default", config_a)]
    if config_b_path:
        variants.append(Variant("B", config_b_path, _load(config_b_path)))

    limits = config_a.backtest.model_copy()
    if max_prs is not None:
        limits.max_prs = max(1, max_prs)
    if max_concurrency is not None:
        limits.max_concurrency = max(1, max_concurrency)
    if max_cost is not None:
        limits.max_estimated_cost_usd = max(0.0, max_cost)
    if no_save:
        limits.store_results = False

    resolved_platform = platform or config_a.provider.type or "github"
    provider = _provider(resolved_platform, token or github_token)

    async def _go() -> tuple[Any, Any]:
        from contextlib import nullcontext

        from mira.quality.store import open_quality_store

        store_cm = (
            open_quality_store(owner, repo, resolved_platform)
            if limits.store_results
            else nullcontext(None)
        )
        with store_cm as quality_store:
            runner = BacktestRunner(
                provider,
                variants,
                owner=owner,
                repo=repo,
                platform=resolved_platform,
                limits=limits,
                bot_login=bot_name,
                quality_store=quality_store,
            )
            plan = await runner.plan(
                numbers=numbers or None, since=since_ts, limit=len(numbers) or limit
            )
            click.echo(render_plan(plan, variants, limits.max_estimated_cost_usd), err=True)
            if not plan.prs:
                raise click.ClickException("No merged pull requests matched.")
            if runner.over_budget(plan):
                raise click.ClickException(
                    f"Estimated ${plan.estimated_cost_usd:.2f} exceeds the "
                    f"${limits.max_estimated_cost_usd:.2f} limit. Replay fewer PRs or raise "
                    "--max-cost / backtest.max_estimated_cost_usd."
                )
            if not confirm:
                click.echo("\nNothing was run. Re-run with --confirm to review these.", err=True)
                return plan, None

            def _progress(result: Any) -> None:
                state = result.error or result.skipped_reason or "ok"
                click.echo(
                    f"  #{result.pr_number} [{result.variant}] {result.score.findings} finding(s), "
                    f"${result.cost_usd:.4f}, {result.latency_ms / 1000:.1f}s — {state}",
                    err=True,
                )

            run = await runner.run(plan, on_result=_progress)
            return plan, run

    _plan, run = asyncio.run(_go())
    if run is None:
        return
    report = render_json(run) if output_format == "json" else render_markdown(run)
    if output_path:
        with open(output_path, "w", encoding="utf-8") as handle:
            handle.write(report)
        click.echo(f"Report written to {output_path}", err=True)
    else:
        click.echo(report)
    if any(r.blocked_writes for r in run.results):
        # Should be impossible; loud if it ever is not.
        click.echo("WARNING: the read-only provider blocked write attempts.", err=True)
        sys.exit(2)


@click.group("escaped-bugs")
def escaped_bugs_group() -> None:
    """Track reverts and hotfixes of pull requests Mira reviewed."""


@escaped_bugs_group.command("scan")
@click.option("--repo", "repository", required=True, help="owner/name to scan")
@click.option("--platform", type=click.Choice(["github", "gitlab", "forgejo"]), default=None)
@click.option("--since", default=None, help="Only PRs merged on or after this date (YYYY-MM-DD)")
@click.option("--limit", type=int, default=100, show_default=True, help="Merged PRs to examine")
@click.option("--config", "config_path", default=None, help="Path to .mira.yaml")
@click.option("--token", envvar="MIRA_GIT_TOKEN", default=None, help="Git platform API token")
@click.option("--github-token", envvar="GITHUB_TOKEN", default=None, help="Alias for --token")
@click.option("--output", "output_format", type=click.Choice(["text", "json"]), default="text")
@click.option("--verbose", is_flag=True)
def escaped_bugs_scan(
    repository: str,
    platform: str | None,
    since: str | None,
    limit: int,
    config_path: str | None,
    token: str | None,
    github_token: str | None,
    output_format: str,
    verbose: bool,
) -> None:
    """Classify merged PRs as reverts/hotfixes and link them to reviewed PRs.

    Records what it finds (idempotently) for the dashboard. Running the
    command is the opt-in; `escaped_bugs.enabled` only governs the webhooks.
    """
    from mira.quality.escaped import scan_history
    from mira.quality.store import open_quality_store

    _logging(verbose)
    owner, repo = _parse_repo(repository)
    config = _load(config_path)
    resolved_platform = platform or config.provider.type or "github"
    provider = _provider(resolved_platform, token or github_token)

    async def _go() -> tuple[Any, dict[str, int]]:
        with open_quality_store(owner, repo, resolved_platform) as store:
            result = await scan_history(
                provider,
                owner,
                repo,
                platform=resolved_platform,
                since=_parse_since(since),
                limit=max(1, limit),
                config=config,
                quality_store=store,
            )
            return result, store.escaped_bug_counts()

    result, counts = asyncio.run(_go())
    recall = counts["caught"] / counts["total"] if counts["total"] else None
    if output_format == "json":
        click.echo(
            json.dumps(
                {
                    "examined": result.examined,
                    "fix_events": result.fix_events,
                    "recorded": [bug.to_dict() for bug in result.recorded],
                    "counts": counts,
                    "recall": recall,
                },
                indent=2,
            )
        )
        return
    click.echo(
        f"Examined {result.examined} merged PR(s); {result.fix_events} revert/hotfix; "
        f"{len(result.recorded)} new escaped bug record(s)."
    )
    for bug in result.recorded:
        state = "flagged" if bug.flagged else "MISSED"
        click.echo(
            f"  {bug.kind:<7} {bug.fix_ref:<14} → #{bug.original_pr_number} "
            f"{bug.path}:{bug.line_start}-{bug.line_end} [{state}, {bug.link_method}]"
        )
    shown = "—" if recall is None else f"{recall * 100:.0f}%"
    click.echo(
        f"Real-world recall for {owner}/{repo}: {counts['caught']} caught / "
        f"{counts['total']} escaped incidents ({shown})"
    )


@click.group("peer-recall")
def peer_recall_group() -> None:
    """Measure Mira's recall against other review bots, and learn from what it missed."""


@peer_recall_group.command("scan")
@click.option("--repo", "repository", required=True, help="owner/name to read")
@click.option("--config", "config_path", default=None, help="Path to .mira.yaml")
@click.option("--token", envvar="MIRA_GIT_TOKEN", default=None, help="GitHub API token")
@click.option("--github-token", envvar="GITHUB_TOKEN", default=None, help="Alias for --token")
@click.option("--rescan", is_flag=True, help="Read pull requests already read after closing")
@click.option(
    "--no-learn", is_flag=True, help="Only collect; do not synthesise positive rules from misses"
)
@click.option("--verbose", is_flag=True)
def peer_recall_scan(
    repository: str,
    config_path: str | None,
    token: str | None,
    github_token: str | None,
    rescan: bool,
    no_learn: bool,
    verbose: bool,
) -> None:
    """Read peer-bot threads on closed PRs Mira reviewed, record them, learn from misses.

    Only reads from GitHub. With learning on (`peer_recall.positive_learning`)
    each missed-and-addressed thread costs one review-model call, at most
    `peer_recall.max_syntheses_per_run` per run.
    """
    from mira.quality.peer_learning import enabled
    from mira.quality.peer_runtime import review_llm, run_repository

    _logging(verbose)
    owner, repo = _parse_repo(repository)
    config = _load(config_path)
    provider = _provider("github", token or github_token)
    learn = not no_learn and enabled(config)
    llm = None
    if learn:
        try:
            llm = review_llm(config)
        except Exception as exc:  # noqa: BLE001 — collecting needs no model
            # As the scheduler does: no review model means collect without
            # learning, not no scan at all.
            click.echo(f"Warning: review model unavailable, collecting only: {exc}", err=True)
            learn = False
    outcome = asyncio.run(
        run_repository(provider, owner, repo, config=config, llm=llm, rescan=rescan, learn=learn)
    )
    if outcome.skipped:
        click.echo(f"Skipped: {outcome.skipped}")
        return
    click.echo(
        f"Read {outcome.prs_examined} closed PR(s): {outcome.threads} peer thread(s), "
        f"{outcome.missed_and_addressed} missed by Mira and addressed by the author."
    )
    if learn:
        rules = ", ".join(f"#{rule}" for rule in outcome.rules_activated) or "none"
        click.echo(f"Synthesised from {outcome.learned} miss(es); rules activated: {rules}.")
    if outcome.error:
        click.echo(f"Error: {outcome.error}", err=True)


def _pct(value: float | None) -> str:
    return "—" if value is None else f"{value * 100:.0f}%"


@peer_recall_group.command("report")
@click.option("--repo", "repository", default=None, help="owner/name; default every repository")
@click.option("--since", default=None, help="PRs closed on or after this date (YYYY-MM-DD)")
@click.option("--bot", default="", help="Only this peer bot, e.g. cursor[bot]")
@click.option(
    "--missed",
    "show_missed",
    type=int,
    default=10,
    show_default=True,
    help="Missed-and-addressed threads to list",
)
@click.option("--output", "output_format", type=click.Choice(["text", "json"]), default="text")
def peer_recall_report(
    repository: str | None, since: str | None, bot: str, show_missed: int, output_format: str
) -> None:
    """Print recall vs each peer bot, by repository and by category, from stored rows."""
    from mira.quality import history

    owner, repo = _parse_repo(repository) if repository else ("", "")
    since_ts = _parse_since(since)
    summary = history.peer_recall_summary(owner=owner, repo=repo, bot=bot, since=since_ts)
    missed = history.list_missed_peer_threads(
        owner=owner, repo=repo, bot=bot, since=since_ts, limit=max(0, show_missed)
    )
    if output_format == "json":
        click.echo(json.dumps({**summary, "missed": missed}, indent=2, default=str))
        return
    totals = summary["totals"]
    if not totals["threads"]:
        click.echo("No peer threads recorded. Run `mira peer-recall scan --repo owner/name`.")
        return
    click.echo(
        f"Recall on addressed peer findings: {totals['addressed_flagged']} / "
        f"{totals['addressed']} ({_pct(totals['recall'])}); "
        f"overlap on all threads {totals['flagged']} / {totals['threads']} "
        f"({_pct(totals['overlap'])})."
    )
    click.echo("")
    click.echo(f"{'bot':<30} {'threads':>7} {'addressed':>9} {'missed':>7} {'recall':>7}")
    for row in summary["bots"]:
        click.echo(
            f"{row['bot']:<30} {row['threads']:>7} {row['addressed']:>9} "
            f"{row['missed_addressed']:>7} {_pct(row['recall']):>7}"
        )
    click.echo("")
    click.echo(f"{'category':<30} {'threads':>7} {'addressed':>9} {'missed':>7} {'recall':>7}")
    for row in summary["categories"]:
        click.echo(
            f"{row['category']:<30} {row['threads']:>7} {row['addressed']:>9} "
            f"{row['missed_addressed']:>7} {_pct(row['recall']):>7}"
        )
    if not owner and len({(r["owner"], r["repo"]) for r in summary["repos"]}) > 1:
        click.echo("")
        click.echo(f"{'repository / bot':<50} {'addressed':>9} {'missed':>7} {'recall':>7}")
        for row in summary["repos"]:
            name = f"{row['owner']}/{row['repo']} {row['bot']}"
            click.echo(
                f"{name:<50} {row['addressed']:>9} {row['missed_addressed']:>7} "
                f"{_pct(row['recall']):>7}"
            )
    if missed:
        click.echo("")
        click.echo("Missed and addressed (newest first):")
        for item in missed:
            click.echo(
                f"  {item['owner']}/{item['repo']}#{item['pr_number']} {item['bot']} "
                f"{item['path']}:{item['line']} [{item['category']}] {item['title'][:80]}"
            )
            if item["url"]:
                click.echo(f"    {item['url']}")


def register(main: click.Group) -> None:
    main.add_command(backtest)
    main.add_command(escaped_bugs_group)
    main.add_command(peer_recall_group)
