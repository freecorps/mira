"""``mira sbom`` and ``mira licenses``: the package inventory from the command line.

Both read what indexing stored (``DATABASE_URL``'s Postgres, else the SQLite
files under ``MIRA_INDEX_DIR``); neither calls a model. Registry lookups for
missing licenses happen only with ``--lookup-licenses`` and go through the
same guarded client as the review-time check.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from contextlib import contextmanager
from typing import Any

import click


def _split_repo(value: str) -> tuple[str, str]:
    owner, _, repo = value.strip().strip("/").rpartition("/")
    if not owner or not repo:
        raise click.UsageError(f"--repo must be owner/name, got {value!r}")
    return owner, repo


@contextmanager
def _open_existing_store(owner: str, repo: str, platform: str) -> Any:
    """The repository's index store, without creating an empty one by asking."""
    from mira.index.store import IndexStore

    db_url = os.environ.get("DATABASE_URL", "")
    if not db_url.startswith(("postgresql://", "postgres://")) and not os.path.exists(
        IndexStore.db_path_for(owner, repo, platform)
    ):
        raise click.ClickException(
            f"{owner}/{repo} has not been indexed here — index it first (the dashboard's "
            "Index button, or a review on the server), then export."
        )
    store = IndexStore.open(owner, repo, platform=platform)
    try:
        yield store
    finally:
        store.close()


def _load(config_path: str | None) -> Any:
    from mira.config import load_config

    return load_config(config_path).licenses


@click.command("sbom")
@click.option("--repo", "repo_name", default=None, help="Repository as owner/name.")
@click.option("--platform", default="github", show_default=True, help="github, gitlab, forgejo…")
@click.option("--all", "org", is_flag=True, help="Every indexed repository in one document.")
@click.option(
    "--format",
    "fmt",
    type=click.Choice(["cyclonedx", "spdx"]),
    default="cyclonedx",
    show_default=True,
)
@click.option(
    "--spec-version",
    type=click.Choice(["1.5", "1.6"]),
    default="1.6",
    show_default=True,
    help="CycloneDX specification version (SPDX is always 2.3).",
)
@click.option("--output", "-o", default="-", help="File to write; '-' for stdout.")
@click.option(
    "--lookup-licenses",
    is_flag=True,
    help="Ask package registries for licenses the lockfiles and cache do not know.",
)
@click.option("--config", "config_path", default=None, help="Path to .mira.yaml")
def sbom_command(
    repo_name: str | None,
    platform: str,
    org: bool,
    fmt: str,
    spec_version: str,
    output: str,
    lookup_licenses: bool,
    config_path: str | None,
) -> None:
    """Export a software bill of materials (CycloneDX or SPDX JSON)."""
    from mira.sbom import export_org, export_repo

    if bool(repo_name) == org:
        raise click.UsageError("Pass exactly one of --repo owner/name or --all")
    config = _load(config_path)
    if org:
        doc = asyncio.run(
            export_org(fmt, spec_version=spec_version, config=config, lookup=lookup_licenses)
        )
    else:
        owner, repo = _split_repo(repo_name or "")
        from mira.sbom.inventory import repo_label

        with _open_existing_store(owner, repo, platform) as store:
            doc = asyncio.run(
                export_repo(
                    store,
                    repo_label(owner, repo, platform),
                    fmt,
                    spec_version=spec_version,
                    config=config,
                    lookup=lookup_licenses,
                )
            )
    text = json.dumps(doc, indent=2) + "\n"
    if output == "-":
        sys.stdout.write(text)
    else:
        with open(output, "w", encoding="utf-8") as fh:
            fh.write(text)
        count = len(doc.get("components") or doc.get("packages") or [])
        click.echo(f"Wrote {fmt} SBOM with {count} entries to {output}", err=True)


@click.command("licenses")
@click.option("--repo", "repo_name", required=True, help="Repository as owner/name.")
@click.option("--platform", default="github", show_default=True)
@click.option(
    "--lookup-licenses",
    is_flag=True,
    help="Ask package registries for licenses the lockfiles and cache do not know.",
)
@click.option("--output", "output_format", type=click.Choice(["text", "json"]), default="text")
@click.option("--config", "config_path", default=None, help="Path to .mira.yaml")
def licenses_command(
    repo_name: str,
    platform: str,
    lookup_licenses: bool,
    output_format: str,
    config_path: str | None,
) -> None:
    """List the repository's dependency licenses and check them against `licenses`.

    Exits 1 when the policy in the configuration (`licenses.allow`, `deny`,
    `fail_on_unknown`) is broken by any dependency, so it can gate a CI job.
    """
    from mira.licenses.policy import Policy, evaluate
    from mira.sbom.inventory import build_repo_inventory, repo_label

    config = _load(config_path)
    policy = Policy.from_config(config)
    owner, repo = _split_repo(repo_name)
    with _open_existing_store(owner, repo, platform) as store:
        inv = asyncio.run(
            build_repo_inventory(
                store, repo_label(owner, repo, platform), config, lookup=lookup_licenses
            )
        )
    ignored = {n.lower() for n in config.ignore_packages}
    rows: list[dict[str, Any]] = []
    violations = 0
    for c in inv.components:
        if c.kind == "docker":
            continue
        verdict = evaluate(c.license_expression, policy)
        skipped = c.name.lower() in ignored or (config.ignore_dev and c.dev)
        bad = policy.active and not skipped and verdict.violates(policy.fail_on_unknown)
        violations += int(bad)
        rows.append(
            {
                "kind": c.kind,
                "name": c.name,
                "version": c.version or c.constraint,
                "scope": c.scope,
                "dev": c.dev,
                "license": c.license_expression,
                "source": c.license.source if c.license else "",
                "status": "ignored" if skipped else verdict.status,
                "violation": bad,
                "reasons": verdict.reasons if bad else [],
            }
        )
    if output_format == "json":
        click.echo(json.dumps({"repository": inv.name, "packages": rows}, indent=2))
    else:
        for r in rows:
            mark = "x" if r["violation"] else " "
            lic = r["license"] or "unknown"
            click.echo(
                f"[{mark}] {r['kind']:<8} {r['name']} {r['version']}  {lic}"
                + (f"  ({'; '.join(r['reasons'])})" if r["reasons"] else "")
            )
        unknown = sum(1 for r in rows if not r["license"])
        click.echo(
            f"\n{len(rows)} package(s), {unknown} with an unknown license, "
            f"{violations} violating the policy."
        )
    if violations:
        sys.exit(1)
