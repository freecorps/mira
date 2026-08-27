"""Click CLI for Mira."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import sys
from datetime import UTC

import click

from mira import __version__
from mira.config import load_config
from mira.core.engine import ReviewEngine
from mira.exceptions import MiraError
from mira.llm import create_llm
from mira.models import ReviewResult, Severity
from mira.report import review_result_dict, review_result_text


def _format_text(result: ReviewResult) -> str:
    """Format review result as human-readable text.

    Delegates to `mira.report`, which the local CLI renders with too.
    """
    return review_result_text(result)


def _format_json(result: ReviewResult) -> str:
    """Format review result as JSON.

    Delegates to `mira.report`, which is also what the local CLI emits: two
    formatters for one object would drift, and a CI job written against one
    would misparse the other.
    """
    return json.dumps(review_result_dict(result), indent=2)


@click.group()
@click.version_option(version=__version__, prog_name="mira")
def main() -> None:
    """Mira — AI-powered PR reviewer."""


@main.command()
@click.option("--pr", "pr_url", default=None, help="PR/MR URL (GitHub PR or GitLab MR)")
@click.option("--stdin", "use_stdin", is_flag=True, help="Read diff from stdin")
@click.option("--model", envvar="MIRA_MODEL", default=None, help="LLM model to use")
@click.option("--max-comments", envvar="MIRA_MAX_COMMENTS", type=int, default=None)
@click.option("--confidence", envvar="MIRA_CONFIDENCE_THRESHOLD", type=float, default=None)
@click.option("--token", envvar="MIRA_GIT_TOKEN", default=None, help="Git platform API token")
@click.option(
    "--github-token",
    envvar="GITHUB_TOKEN",
    default=None,
    help="GitHub API token (alias for --token)",
)
@click.option("--dry-run", is_flag=True, help="Don't post review, just print results")
@click.option("--output", "output_format", type=click.Choice(["text", "json"]), default="text")
@click.option("--verbose", is_flag=True, help="Enable verbose logging")
@click.option("--config", "config_path", default=None, help="Path to .mira.yaml")
@click.option(
    "--no-walkthrough",
    is_flag=True,
    help="Skip walkthrough generation. Useful in dry-run loops where only the "
    "inline review is needed and the extra LLM call should be saved.",
)
def review(
    pr_url: str | None,
    use_stdin: bool,
    model: str | None,
    max_comments: int | None,
    confidence: float | None,
    token: str | None,
    github_token: str | None,
    dry_run: bool,
    output_format: str,
    verbose: bool,
    config_path: str | None,
    no_walkthrough: bool,
) -> None:
    """Review a pull request or diff."""
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.WARNING,
        format="%(name)s %(levelname)s: %(message)s",
        stream=sys.stdout,
    )

    if not pr_url and not use_stdin:
        raise click.UsageError("Provide --pr <url> or --stdin")

    overrides: dict[str, object] = {}
    if model:
        overrides["llm.model"] = model
    if max_comments is not None:
        overrides["filter.max_comments"] = max_comments
    if confidence is not None:
        overrides["filter.confidence_threshold"] = confidence
    if no_walkthrough:
        overrides["review.walkthrough"] = False

    try:
        config = load_config(config_path, overrides)
    except MiraError as e:
        raise click.ClickException(str(e)) from e

    from mira.dashboard.models_config import llm_config_for

    llm = create_llm(llm_config_for("review", config.llm))
    indexing_llm = create_llm(llm_config_for("indexing", config.llm))
    security_llm = create_llm(llm_config_for("security", config.llm))

    git_token = token or github_token
    github_provider = None
    if pr_url:
        if not git_token:
            raise click.UsageError(
                "--token (or --github-token / GITHUB_TOKEN / MIRA_GIT_TOKEN) is required for PR review"
            )
        # Infer the platform from the URL shape; fall back to the configured default.
        if "/-/merge_requests/" in pr_url or "gitlab" in pr_url:
            provider_type = "gitlab"
        elif "/pulls/" in pr_url or "forgejo" in pr_url:
            provider_type = "forgejo"
        elif "/pull/" in pr_url or "github" in pr_url:
            provider_type = "github"
        else:
            provider_type = config.provider.type
        from mira.providers import create_provider, get_available_providers

        try:
            github_provider = create_provider(provider_type, git_token)
        except ValueError as err:
            available = ", ".join(get_available_providers()) or "(none)"
            raise click.UsageError(
                f"Unknown provider type {provider_type!r}. Available providers: {available}"
            ) from err

    engine = ReviewEngine(
        config=config,
        llm=llm,
        provider=github_provider,
        dry_run=dry_run,
        indexing_llm=indexing_llm,
        security_llm=security_llm,
    )

    try:
        if use_stdin:
            diff_text = sys.stdin.read()
            result = asyncio.run(engine.review_diff(diff_text))
        else:
            result = asyncio.run(engine.review_pr(pr_url))  # type: ignore[arg-type]
    except MiraError as e:
        raise click.ClickException(str(e)) from e

    if output_format == "json":
        click.echo(_format_json(result))
    else:
        click.echo(_format_text(result))

    # Exit with non-zero if blockers found
    if any(c.severity >= Severity.BLOCKER for c in result.comments):
        sys.exit(1)


@main.command()
@click.option("--host", default="0.0.0.0", help="Host to bind to")
@click.option("--port", envvar="PORT", default=8000, type=int, help="Port to bind to")
@click.option(
    "--app-id",
    envvar="MIRA_GITHUB_APP_ID",
    default=None,
    help="GitHub App ID (enables the GitHub webhook route)",
)
@click.option(
    "--private-key",
    envvar="MIRA_GITHUB_PRIVATE_KEY",
    default=None,
    help="PEM contents or @path/to/key.pem",
)
@click.option(
    "--webhook-secret",
    envvar="MIRA_WEBHOOK_SECRET",
    default=None,
    help="Webhook secret from GitHub App settings",
)
@click.option(
    "--gitlab-token",
    envvar="MIRA_GITLAB_TOKEN",
    default=None,
    help="GitLab group/project access token (enables the GitLab webhook route)",
)
@click.option(
    "--gitlab-webhook-secret",
    envvar="MIRA_GITLAB_WEBHOOK_SECRET",
    default=None,
    help="Secret string configured on the GitLab project webhook (X-Gitlab-Token)",
)
@click.option(
    "--gitlab-base-url",
    envvar="MIRA_GITLAB_API_URL",
    default=None,
    help="GitLab API base for self-managed instances, e.g. https://gitlab.acme.com/api/v4",
)
@click.option(
    "--forgejo-token",
    envvar="MIRA_FORGEJO_TOKEN",
    default=None,
    help="Forgejo access token (enables the Forgejo webhook route)",
)
@click.option(
    "--forgejo-webhook-secret",
    envvar="MIRA_FORGEJO_WEBHOOK_SECRET",
    default=None,
    help="HMAC-SHA256 secret for verifying Forgejo webhook signatures (X-Forgejo-Signature header)",
)
@click.option(
    "--forgejo-base-url",
    envvar="MIRA_FORGEJO_API_URL",
    default=None,
    help="Forgejo API base, e.g. https://forgejo.example.com/api/v1",
)
@click.option(
    "--bot-name",
    envvar="MIRA_BOT_NAME",
    default=None,
    help="Bot @mention name. If unset, auto-detected from the platform's own identity.",
)
@click.option(
    "--config",
    "config_path",
    envvar="MIRA_CONFIG",
    type=click.Path(dir_okay=False),
    default=None,
    help=(
        "Path to a deployment-wide config YAML (model defaults, filter, review). "
        "Per-repo `.mira.yaml` files, when present, deep-merge over these defaults."
    ),
)
@click.option("--verbose", is_flag=True, help="Enable verbose logging")
def serve(
    host: str,
    port: int,
    app_id: str | None,
    private_key: str | None,
    webhook_secret: str | None,
    gitlab_token: str | None,
    gitlab_webhook_secret: str | None,
    gitlab_base_url: str | None,
    forgejo_token: str | None,
    forgejo_webhook_secret: str | None,
    forgejo_base_url: str | None,
    bot_name: str | None,
    config_path: str | None,
    verbose: bool,
) -> None:
    """Run the Mira webhook server for GitHub, GitLab, and/or Forgejo."""
    try:
        import asyncio

        import uvicorn

        from mira.config import set_global_defaults
        from mira.platforms.forgejo.auth import ForgejoTokenAuth
        from mira.platforms.github.auth import GitHubAppAuth
        from mira.platforms.gitlab.auth import GitLabTokenAuth
        from mira.platforms.server import create_app
    except ImportError as exc:
        raise click.ClickException(
            f"Missing dependency: {exc}. Install with: pip install mira-reviewer[serve]"
        ) from exc

    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(name)s %(levelname)s: %(message)s",
        stream=sys.stdout,
    )

    if config_path:
        try:
            set_global_defaults(config_path)
            click.echo(f"Loaded deployment config: {config_path}")
        except Exception as exc:
            raise click.ClickException(f"Invalid --config file: {exc}") from exc

    github_configured = bool(app_id and private_key and webhook_secret)
    gitlab_configured = bool(gitlab_token and gitlab_webhook_secret)
    forgejo_configured = bool(forgejo_token and forgejo_webhook_secret)
    if not github_configured and not gitlab_configured and not forgejo_configured:
        raise click.ClickException(
            "No platform configured. Provide GitHub App creds (--app-id, --private-key, "
            "--webhook-secret) and/or GitLab creds (--gitlab-token, --gitlab-webhook-secret)."
        )

    app_auth = None
    gitlab_auth = None
    forgejo_auth = None

    if github_configured:
        assert private_key is not None
        assert app_id is not None
        if private_key.startswith("@"):
            key_path = private_key[1:]
            try:
                with open(key_path) as f:
                    private_key = f.read()
            except FileNotFoundError:
                raise click.ClickException(f"Private key file not found: {key_path}") from None
        app_auth = GitHubAppAuth(app_id=app_id, private_key=private_key)

    if gitlab_configured:
        assert gitlab_token is not None
        gitlab_auth = GitLabTokenAuth(gitlab_token, gitlab_base_url or "https://gitlab.com/api/v4")

    if forgejo_configured:
        assert forgejo_token is not None
        forgejo_auth = ForgejoTokenAuth(
            forgejo_token, forgejo_base_url or "https://codeberg.org/api/v1"
        )

    # Auto-detect the bot @mention from whichever platform's own identity when
    # the user didn't override it. Falls back to "miracodeai" on a lookup blip.
    if not bot_name:
        identity_auth = app_auth or gitlab_auth or forgejo_auth
        if identity_auth is not None:
            bot_name = asyncio.run(identity_auth.get_bot_identity()) or "miracodeai"
            click.echo(f"Detected bot @mention: @{bot_name}")
        else:
            bot_name = "miracodeai"

    # Persist the resolved name so the dashboard UI can show the real handle.
    try:
        from mira.dashboard.api import _app_db

        _app_db.set_setting("bot_name", bot_name)
    except Exception as exc:
        click.echo(f"Warning: could not persist bot_name for the dashboard: {exc}")

    app = create_app(
        app_auth=app_auth,
        webhook_secret=webhook_secret,
        bot_name=bot_name,
        gitlab_auth=gitlab_auth,
        gitlab_webhook_secret=gitlab_webhook_secret,
        forgejo_auth=forgejo_auth,
        forgejo_webhook_secret=forgejo_webhook_secret,
    )

    platforms = ", ".join(
        p
        for p, on in [
            ("GitHub", github_configured),
            ("GitLab", gitlab_configured),
            ("Forgejo", forgejo_configured),
        ]
        if on
    )
    click.echo(f"Starting Mira webhook server ({platforms}) on {host}:{port}")
    uvicorn.run(app, host=host, port=port)


@main.command("backfill-contributors")
@click.option(
    "--repo",
    "repo_spec",
    default=None,
    help="owner/repo to backfill. Omit to backfill every registered repo.",
)
@click.option("--app-id", envvar="MIRA_GITHUB_APP_ID", required=True, help="GitHub App ID")
@click.option(
    "--private-key",
    envvar="MIRA_GITHUB_PRIVATE_KEY",
    required=True,
    help="PEM contents or @path/to/key.pem",
)
@click.option(
    "--since",
    default=None,
    help="Only events on/after this ISO date (e.g. 2024-01-01). Enables an incremental top-up.",
)
@click.option(
    "--no-commits",
    is_flag=True,
    help="Skip the per-commit phase (much lighter on the GitHub API).",
)
@click.option("--verbose", is_flag=True, help="Enable verbose logging")
def backfill_contributors(
    repo_spec: str | None,
    app_id: str,
    private_key: str,
    since: str | None,
    no_commits: bool,
    verbose: bool,
) -> None:
    """Backfill historical contributor activity (PRs, reviews, commits) from GitHub."""
    import asyncio
    from datetime import datetime

    from mira.platforms.github.auth import GitHubAppAuth
    from mira.platforms.github.contributor_backfill import (
        backfill_all_repos,
        backfill_repo_contributions,
    )

    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(name)s %(levelname)s: %(message)s",
        stream=sys.stdout,
    )

    if private_key.startswith("@"):
        key_path = private_key[1:]
        try:
            with open(key_path) as f:
                private_key = f.read()
        except FileNotFoundError:
            raise click.ClickException(f"Private key file not found: {key_path}") from None

    app_auth = GitHubAppAuth(app_id=app_id, private_key=private_key)

    since_epoch: float | None = None
    if since:
        try:
            since_epoch = datetime.fromisoformat(since).replace(tzinfo=UTC).timestamp()
        except ValueError:
            raise click.ClickException("--since must be an ISO date, e.g. 2024-01-01") from None

    include_commits = not no_commits
    if repo_spec:
        if "/" not in repo_spec:
            raise click.UsageError("--repo must be in the form owner/repo")
        owner, repo = repo_spec.split("/", 1)
        counts = asyncio.run(
            backfill_repo_contributions(
                owner, repo, app_auth, since=since_epoch, include_commits=include_commits
            )
        )
        click.echo(f"Backfilled {owner}/{repo}: {counts}")
    else:
        totals = asyncio.run(
            backfill_all_repos(app_auth, since=since_epoch, include_commits=include_commits)
        )
        click.echo(f"Backfill complete: {totals}")


@main.command("autofix-worker")
@click.option("--app-id", envvar="MIRA_GITHUB_APP_ID", default=None, help="GitHub App ID")
@click.option(
    "--private-key",
    envvar="MIRA_GITHUB_PRIVATE_KEY",
    default=None,
    help="PEM contents or @path/to/key.pem",
)
@click.option("--gitlab-token", envvar="MIRA_GITLAB_TOKEN", default=None)
@click.option("--gitlab-base-url", envvar="MIRA_GITLAB_BASE_URL", default=None)
@click.option("--forgejo-token", envvar="MIRA_FORGEJO_TOKEN", default=None)
@click.option("--forgejo-base-url", envvar="MIRA_FORGEJO_BASE_URL", default=None)
@click.option("--config", "config_path", default=None, help="Path to .mira.yaml")
@click.option(
    "--once",
    is_flag=True,
    help="Run a single poll and exit. For cron-style scheduling and for smoke tests.",
)
@click.option("--verbose", is_flag=True, help="Enable verbose logging")
def autofix_worker(
    app_id: str | None,
    private_key: str | None,
    gitlab_token: str | None,
    gitlab_base_url: str | None,
    forgejo_token: str | None,
    forgejo_base_url: str | None,
    config_path: str | None,
    once: bool,
    verbose: bool,
) -> None:
    """Run the autofix queue's worker as its own process.

    Only needed when `autofix.inline_worker` is false. The default deployment
    runs this loop inside `mira serve`, because one container is the profile
    this project targets and a second process is a second thing to supervise.

    Safe to run alongside the inline worker and alongside other copies of
    itself: jobs are handed out by a database lease, so two workers cannot take
    the same one and a worker that dies simply lets its lease expire.
    """
    import asyncio

    from mira.autofix.runtime import provider_factory
    from mira.autofix.worker import AutofixWorker
    from mira.platforms.auth import ForgejoTokenAuth, GitLabTokenAuth
    from mira.platforms.github.auth import GitHubAppAuth

    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(name)s %(levelname)s: %(message)s",
        stream=sys.stdout,
    )

    config = load_config(config_path)
    if config.autofix.mode == "off":
        raise click.ClickException("autofix.mode is 'off' — nothing would ever be claimed")
    if config.autofix.kill_switch:
        raise click.ClickException("autofix.kill_switch is on — the worker would claim nothing")

    auths: dict[str, object] = {}
    if app_id and private_key:
        if private_key.startswith("@"):
            key_path = private_key[1:]
            try:
                with open(key_path) as handle:
                    private_key = handle.read()
            except FileNotFoundError:
                raise click.ClickException(f"Private key file not found: {key_path}") from None
        auths["github"] = GitHubAppAuth(app_id=app_id, private_key=private_key)
    if gitlab_token:
        auths["gitlab"] = GitLabTokenAuth(
            gitlab_token, gitlab_base_url or "https://gitlab.com/api/v4"
        )
    if forgejo_token:
        auths["forgejo"] = ForgejoTokenAuth(
            forgejo_token, forgejo_base_url or "https://codeberg.org/api/v1"
        )
    if not auths:
        raise click.ClickException(
            "No platform credentials were supplied; the worker would claim jobs it cannot run"
        )

    worker = AutofixWorker(provider_factory=provider_factory(auths), config=config)
    click.echo(f"Autofix worker {worker.identity} starting ({', '.join(sorted(auths))})")
    if once:
        ran = asyncio.run(worker.poll_once(config=config))
        click.echo("Ran one job." if ran else "Nothing to run.")
        return
    try:
        asyncio.run(worker.run_forever())
    except KeyboardInterrupt:
        click.echo("Stopped.")


@main.group("local")
def local_group() -> None:
    """Review a change in this checkout, without a pull request.

    The same engine, configuration, retrieval and pre-merge checks the server
    runs — reading the working tree, the index or a commit range instead of a
    pull request. Read-only: nothing is staged, committed, posted or recorded.
    """


def _print_exit_codes(ctx: click.Context, _param: object, value: bool) -> None:
    if not value or ctx.resilient_parsing:
        return
    from mira.local.output import exit_code_table

    click.echo(exit_code_table())
    ctx.exit(0)


@local_group.command("review")
@click.option(
    "--path",
    "repo_path",
    default=".",
    type=click.Path(file_okay=False),
    help="Directory inside the repository to review. Defaults to the current one.",
)
@click.option(
    "--staged",
    "staged",
    is_flag=True,
    help="Review what is staged (the index) rather than the whole working tree.",
)
@click.option(
    "--range",
    "range_spec",
    default=None,
    metavar="<base>..<head>",
    help=(
        "Review a commit range. `a..b` compares the two commits; `a...b` compares "
        "b against their merge base, which is what a pull request shows."
    ),
)
@click.option(
    "--include-untracked",
    is_flag=True,
    help=(
        "Also review files git does not track yet. Off by default: an untracked "
        "file has never been through a commit and may be anything."
    ),
)
@click.option(
    "--repo",
    "stated_slug",
    default=None,
    metavar="OWNER/REPO",
    help=(
        "Name the repository explicitly, when the remote does not say. Decides "
        "which index, learned rules and per-repository policy apply."
    ),
)
@click.option(
    "--platform",
    "stated_platform",
    type=click.Choice(["github", "gitlab", "forgejo"]),
    default=None,
    help="Platform for --repo, when the remote's host does not imply one.",
)
@click.option(
    "--remote",
    default=None,
    help="Which git remote names this repository. Defaults to origin, then the first one.",
)
@click.option("--model", envvar="MIRA_MODEL", default=None, help="LLM model to use")
@click.option("--max-comments", envvar="MIRA_MAX_COMMENTS", type=int, default=None)
@click.option("--confidence", envvar="MIRA_CONFIDENCE_THRESHOLD", type=float, default=None)
@click.option("--no-walkthrough", is_flag=True, help="Skip the walkthrough, saving one LLM call.")
@click.option("--no-checks", is_flag=True, help="Skip the pre-merge checks for this run.")
@click.option(
    "--fail-on",
    type=click.Choice(["blocker", "warning", "suggestion", "nitpick", "never"]),
    default="blocker",
    show_default=True,
    help="Lowest severity that makes the command exit 1.",
)
@click.option(
    "--fail-on-incomplete-checks",
    is_flag=True,
    help=(
        "Also exit 1 when a blocking check could not answer. Off by default: "
        "locally that usually means an analyser is not installed on this machine."
    ),
)
@click.option("--output", "output_format", type=click.Choice(["text", "json"]), default="text")
@click.option(
    "--config",
    "config_path",
    envvar="MIRA_CONFIG",
    type=click.Path(dir_okay=False),
    default=None,
    help=(
        "Deployment-wide defaults, as `mira serve --config` takes them. The "
        "repository's own .mira.yaml still deep-merges over these."
    ),
)
@click.option(
    "--explain-exit-codes",
    is_flag=True,
    is_eager=True,
    expose_value=False,
    callback=_print_exit_codes,
    help="Print the exit-code table and exit.",
)
@click.option("--verbose", is_flag=True, help="Enable verbose logging (on stderr)")
def local_review(
    repo_path: str,
    staged: bool,
    range_spec: str | None,
    include_untracked: bool,
    stated_slug: str | None,
    stated_platform: str | None,
    remote: str | None,
    model: str | None,
    max_comments: int | None,
    confidence: float | None,
    no_walkthrough: bool,
    no_checks: bool,
    fail_on: str,
    fail_on_incomplete_checks: bool,
    output_format: str,
    config_path: str | None,
    verbose: bool,
) -> None:
    """Review the working tree, the index, or a commit range."""
    from mira.local import output as local_output
    from mira.local import run as local_run
    from mira.local.repo import MODE_RANGE, MODE_STAGED, MODE_WORKING_TREE

    # A review body is model output and can contain any character. On a console
    # whose encoding is not UTF-8 - the default on Windows - writing one raises
    # UnicodeEncodeError, and a tool that crashes while printing its own report
    # is worse than one that prints a substitution character. JSON output is
    # unaffected either way: it is escaped to ASCII before it gets here.
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            with contextlib.suppress(ValueError, OSError):  # pragma: no cover
                reconfigure(errors="replace")

    # Logging goes to stderr, always. `--output json` writes one document to
    # stdout and a caller pipes it into a parser; a log line in the middle of it
    # would be a parse error that looks like a Mira bug.
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.WARNING,
        format="%(name)s %(levelname)s: %(message)s",
        stream=sys.stderr,
    )

    if staged and range_spec:
        raise click.UsageError("--staged and --range review different things; pick one.")
    mode = MODE_RANGE if range_spec else (MODE_STAGED if staged else MODE_WORKING_TREE)
    if include_untracked and mode != MODE_WORKING_TREE:
        raise click.UsageError(
            "--include-untracked only applies to a working-tree review: an "
            "untracked file is neither staged nor in any commit."
        )
    if stated_platform and not stated_slug:
        raise click.UsageError("--platform needs --repo, which is what it describes.")

    overrides: dict[str, object] = {}
    if model:
        overrides["llm.model"] = model
    if max_comments is not None:
        overrides["filter.max_comments"] = max_comments
    if confidence is not None:
        overrides["filter.confidence_threshold"] = confidence
    if no_walkthrough:
        overrides["review.walkthrough"] = False

    try:
        review = local_run.prepare(
            path=repo_path,
            mode=mode,
            range_spec=range_spec or "",
            include_untracked=include_untracked,
            deployment_config=config_path,
            overrides=overrides,
            remote=remote or "",
            stated_slug=stated_slug or "",
            stated_platform=stated_platform or "",
        )
        review.fail_on = fail_on
        review.fail_on_incomplete_checks = fail_on_incomplete_checks
        review = asyncio.run(local_run.execute(review, run_checks_too=not no_checks))
    except local_run.LocalReviewError as exc:
        click.echo(str(exc), err=True)
        sys.exit(int(exc.code))
    except KeyboardInterrupt:  # pragma: no cover - depends on a real signal
        click.echo("Interrupted.", err=True)
        sys.exit(int(local_run.ExitCode.INTERRUPTED))

    if output_format == "json":
        click.echo(local_output.to_json(review))
    else:
        click.echo(local_output.to_text(review))
    sys.exit(int(review.exit_code()))
