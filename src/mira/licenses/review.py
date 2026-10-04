"""Review-time license policy: flag PR-added dependencies whose license breaks it.

Same shape as :mod:`mira.security.pr_scan`: read each changed manifest at the
pull request's head, keep the packages named on an added line (added or
bumped), and anchor a finding to that line. The license comes from
:func:`mira.licenses.lookup.resolve_licenses`. No model is called, and nothing
here can fail the review: any error is a log line and no findings.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from mira.index.manifests import _is_lockfile_path, parse_manifest
from mira.licenses.lookup import PackageRef, resolve_licenses
from mira.licenses.policy import UNKNOWN, Policy, evaluate
from mira.models import FileChangeType, FileDiff, ReviewComment, Severity
from mira.security.pr_scan import _added_line_hits

if TYPE_CHECKING:
    from mira.config import LicensesConfig
    from mira.index.context import SourceFetcher

logger = logging.getLogger(__name__)

SOURCE_PASS = "licenses"


@dataclass
class _Candidate:
    ref: PackageRef
    path: str
    line: int
    text: str
    from_lockfile: bool


async def check_manifest_licenses(
    manifest_files: list[FileDiff],
    fetcher: SourceFetcher | None,
    config: LicensesConfig,
    *,
    store: Any = None,
    client: Any = None,
) -> list[ReviewComment]:
    """Findings for PR-added packages whose license violates ``config``. Never raises."""
    try:
        return await _check(manifest_files, fetcher, config, store=store, client=client)
    except Exception as exc:  # noqa: BLE001 - a license check must never cost the review
        logger.warning("License check failed, continuing without: %s", exc)
        return []


async def _check(
    manifest_files: list[FileDiff],
    fetcher: SourceFetcher | None,
    config: LicensesConfig,
    *,
    store: Any,
    client: Any,
) -> list[ReviewComment]:
    policy = Policy.from_config(config)
    if not config.enabled or not policy.active or not manifest_files or fetcher is None:
        return []
    ignored = {n.lower() for n in config.ignore_packages}

    # (kind, lowercased name) → the best anchor for it. A package added to
    # package.json and resolved in package-lock.json is one finding, placed
    # where a person added it, with the lockfile's version and license.
    found: dict[tuple[str, str], _Candidate] = {}
    for f in manifest_files:
        if f.change_type == FileChangeType.DELETED:
            continue
        try:
            content = await fetcher.fetch(f.path)
        except Exception as exc:  # noqa: BLE001
            logger.debug("License check: %s unavailable: %s", f.path, exc)
            continue
        if not content:
            continue
        lockfile = _is_lockfile_path(f.path)
        for pkg in parse_manifest(f.path, content):
            if pkg.kind == "docker" or pkg.name.lower() in ignored:
                continue
            if config.ignore_dev and pkg.is_dev:
                continue
            hits = _added_line_hits(f, pkg.name)
            if not hits:
                continue
            key = (pkg.kind, pkg.name.lower())
            ref = PackageRef(pkg.kind, pkg.name, pkg.version, pkg.license)
            current = found.get(key)
            if current is None:
                found[key] = _Candidate(ref, f.path, hits[0][0], hits[0][1], lockfile)
                continue
            # Keep the manifest's anchor, take the lockfile's facts.
            if lockfile and not current.from_lockfile:
                current.ref = ref
            elif current.from_lockfile and not lockfile:
                found[key] = _Candidate(
                    PackageRef(
                        pkg.kind,
                        pkg.name,
                        current.ref.version or pkg.version,
                        current.ref.recorded or pkg.license,
                    ),
                    f.path,
                    hits[0][0],
                    hits[0][1],
                    False,
                )
    if not found:
        return []

    candidates = list(found.values())
    licenses = await resolve_licenses(
        [c.ref for c in candidates], config, store, lookup=config.lookup, client=client
    )

    violations: list[tuple[_Candidate, Any]] = []
    for c in candidates:
        info = licenses.get(c.ref.key)
        verdict = evaluate(info.expression if info else "", policy)
        if verdict.violates(policy.fail_on_unknown):
            violations.append((c, verdict))
    if not violations:
        return []

    severity = Severity.BLOCKER if config.severity == "blocker" else Severity.WARNING
    shown = violations[: config.max_comments]
    comments: list[ReviewComment] = []
    for i, (c, verdict) in enumerate(shown):
        name = c.ref.name
        version = c.ref.key[2] or c.ref.version
        label = f"{name}@{version}" if version else name
        if verdict.status == UNKNOWN:
            title = f"Unknown license for {name}"
            body = (
                f"Mira could not determine the license of `{label}`, added by this pull "
                "request, and this repository's license policy sets "
                "`licenses.fail_on_unknown`."
            )
        else:
            title = f"License not allowed: {name} ({verdict.expression})"
            reasons = "; ".join(verdict.reasons) or "it is not allowed"
            body = (
                f"`{label}` is licensed under **{verdict.expression}**, which this "
                f"repository's license policy does not allow: {reasons}."
            )
        body += (
            "\n\nReplace it with a package under an allowed license, or — if the "
            "license has been cleared — add it to `licenses.ignore_packages`."
        )
        if i == len(shown) - 1 and len(violations) > len(shown):
            body += (
                f"\n\n…and {len(violations) - len(shown)} more package(s) with the same problem."
            )
        comments.append(
            ReviewComment(
                path=c.path,
                line=c.line,
                end_line=None,
                severity=severity,
                category="license",
                title=title,
                body=body,
                confidence=1.0,
                agent_prompt=(
                    f"In {c.path}, the dependency {name} has the license "
                    f"{verdict.expression or 'unknown'}, which the repository's license "
                    "policy does not allow. Replace it with an alternative under an allowed "
                    "license and update any associated lockfile entries."
                ),
                existing_code=c.text,
                source_pass=SOURCE_PASS,
            )
        )
    return comments
