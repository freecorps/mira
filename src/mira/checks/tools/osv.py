"""Known vulnerabilities in the dependencies this pull request adds.

The one adapter here that is not a subprocess, and the one that had to be
written carefully for a reason that has nothing to do with subprocesses: Mira
already scans PR-added dependencies against OSV.dev during the review, and
already polls every indexed repository hourly. A second implementation of that
would eventually disagree with the first, and the disagreement would show up as
a pull request being told about a CVE by one half of Mira and not the other.

So this check does not implement anything. It calls
:func:`mira.security.pr_scan.scan_manifest_changes` — the same function the
review pass calls, over the same parsed manifests, against the same OSV client
— and re-renders its answer as check findings. The review keeps posting inline
comments exactly as it did; this makes the same facts available to a gate, to
the dashboard's check history, and to deduplication against whatever else found
them.

Two consequences worth stating plainly:

* **Nothing about the existing OSV analysis changes.** Turning this check on
  adds a second *view*, not a second scan of record, and turning it off leaves
  the review pass exactly where it was.
* **A vulnerability reported here and inline is one finding.** The finding
  fingerprint is the manifest path, the line the dependency was added on, and
  the package — so the run's deduplication pass folds this together with
  anything else that found it, and the reader sees it once with both sources
  named.
"""

from __future__ import annotations

import logging

from mira.checks.config_models import CheckToolConfig
from mira.checks.context import CheckContext, CheckOutcome
from mira.checks.models import Evidence, SkipReason
from mira.checks.tools.base import ToolAdapter, ToolFinding
from mira.models import FileDiff

logger = logging.getLogger(__name__)

# Manifest files this check knows how to read. Taken from the same parser the
# indexer uses, so "which files are manifests" has one answer in this codebase.
_MANIFEST_HINTS = (
    "package.json",
    "requirements.txt",
    "pyproject.toml",
    "pipfile",
    "go.mod",
    "cargo.toml",
    "composer.json",
    "gemfile",
    "build.gradle",
    "pom.xml",
)


class _ContextFetcher:
    """A ``SourceFetcher`` over the run's own cached file reads.

    The review pass hands ``scan_manifest_changes`` a provider-backed fetcher;
    this hands it the check context's, so a manifest already read by another
    check is not fetched a second time.
    """

    def __init__(self, ctx: CheckContext) -> None:
        self._ctx = ctx

    async def fetch(self, path: str) -> str | None:
        return await self._ctx.file_content(path) or None


class OsvTool(ToolAdapter):
    """OSV.dev advisories for dependencies this pull request adds or bumps."""

    name = "osv"
    title = "Known vulnerabilities (OSV.dev)"
    version = "1"
    description = (
        "OSV.dev advisories for the dependencies this pull request adds or bumps, "
        "using the same scan the review already runs."
    )
    binary = ""

    def _manifests(self, ctx: CheckContext, config: CheckToolConfig) -> list[FileDiff]:
        selected = set(self.select(ctx, config))
        return [
            file_diff
            for file_diff in ctx.patch_set.files
            if file_diff.path in selected
            and file_diff.path.replace("\\", "/").lower().rsplit("/", 1)[-1] in _MANIFEST_HINTS
        ]

    async def analyse(self, ctx: CheckContext, config: CheckToolConfig) -> CheckOutcome:
        from mira.security.pr_scan import scan_manifest_changes

        manifests = self._manifests(ctx, config)
        if not manifests:
            return CheckOutcome.skipped(
                "This pull request changes no dependency manifest.",
                SkipReason.NOT_APPLICABLE,
            )

        timeout = config.timeout_seconds or min(30.0, max(5.0, ctx.remaining))
        try:
            comments = await scan_manifest_changes(
                manifests, _ContextFetcher(ctx), timeout_s=timeout
            )
        except Exception as exc:  # noqa: BLE001 - an unreachable advisory feed is ignorance
            return CheckOutcome.failed(
                error=f"{type(exc).__name__}: {exc}",
                summary=(
                    "OSV.dev could not be reached, so Mira does not know whether these "
                    "dependencies carry advisories. This is a Mira problem, not a "
                    "problem with the change."
                ),
            )

        if not comments:
            return CheckOutcome.passed(
                summary=(
                    f"No OSV.dev advisory affects the dependencies changed in "
                    f"{len(manifests)} manifest(s)."
                ),
                evidence=[
                    Evidence(
                        path=file_diff.path,
                        detail="manifest scanned against OSV.dev",
                        source="tool:osv",
                    )
                    for file_diff in manifests[:10]
                ],
            )

        results = [
            ToolFinding(
                path=comment.path,
                line=comment.line,
                # The package identity, not the advisory text: a manifest that
                # gains a second CVE for the same package is the same finding,
                # and a fingerprint keyed on prose would churn every time an
                # advisory summary was reworded upstream.
                rule_id=comment.title,
                message=comment.body,
                severity="blocker" if comment.severity.name == "BLOCKER" else "warning",
                snippet=comment.existing_code,
            )
            for comment in comments
        ]
        return CheckOutcome.violation(
            summary=(
                f"{len(results)} dependency change(s) introduce packages with known advisories."
            ),
            findings=self.to_findings(results),
        )
