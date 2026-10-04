"""Putting it together: bumps → repositories → notes → summary, inside one budget.

:func:`collect_dependency_updates` never raises and never runs past
``timeout_seconds``. Each stage keeps what it finished when the budget runs
out — a bump whose notes did not arrive is still listed, with the reason — so
a slow registry costs that bump its notes, not the review its section, and
never the review.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import replace
from typing import TYPE_CHECKING, Any

import httpx

from mira.dependency_updates.bumps import ManifestReader, detect_bumps
from mira.dependency_updates.fetch import (
    CACHE_TTL_SECONDS,
    Budget,
    Fetcher,
    PinnedTransport,
    allowed_hosts,
    fetch_release_notes,
    resolve_repo,
)
from mira.dependency_updates.models import DependencyUpdate
from mira.dependency_updates.summarize import summarize

#: Where `mira.security.osv` sends its queries.
OSV_HOST = "api.osv.dev"

if TYPE_CHECKING:
    from mira.config import DependencyUpdatesConfig
    from mira.models import FileDiff, PRInfo

logger = logging.getLogger(__name__)

#: Share of the budget the HTTP lookups may use; the rest is the summary's.
_FETCH_SHARE = 0.6

# bump key → (expires_at, finished update). Only finished answers are cached —
# "summarised" or "looked, there are none" — never a timeout.
_RESULTS: dict[tuple[str, str, str, str], tuple[float, DependencyUpdate]] = {}
_RESULTS_MAX = 256

_STATUS_TIMED_OUT = "Release notes did not arrive in time"
_STATUS_NO_REPO = "No GitHub repository in the registry metadata"
_STATUS_NO_NOTES = "No release notes found between these versions"
_STATUS_OVER_LIMIT = "Not looked up: more bumps than this review looks up"
_STATUS_NOT_SUMMARIZED = "Release notes found but not summarised"


def reset_cache() -> None:
    _RESULTS.clear()


def provider_reader(provider: Any, pr_info: PRInfo) -> ManifestReader:
    """Read a manifest at the pull request's base or head through the platform API."""

    async def read(path: str, side: str) -> str | None:
        ref = (
            (pr_info.base_sha or pr_info.base_branch)
            if side == "base"
            else (pr_info.head_sha or pr_info.head_branch)
        )
        if not ref:
            return None
        try:
            content = await provider.get_file_content(pr_info, path, ref)
        except Exception as exc:
            logger.debug("Dependency updates: %s@%s unavailable: %s", path, ref, exc)
            return None
        return content if isinstance(content, str) else None

    return read


def _cached(update: DependencyUpdate) -> bool:
    hit = _RESULTS.get(update.bump.key)
    if not hit or hit[0] <= time.monotonic():
        return False
    done = hit[1]
    update.source_repo = done.source_repo
    update.notes_url = done.notes_url
    update.releases = list(done.releases)
    update.breaking_changes = list(done.breaking_changes)
    update.deprecations = list(done.deprecations)
    update.notable = list(done.notable)
    update.status = done.status
    update.summarized = done.summarized
    return True


def _remember(update: DependencyUpdate) -> None:
    if len(_RESULTS) >= _RESULTS_MAX:
        _RESULTS.pop(next(iter(_RESULTS)))
    snapshot = replace(update, vulns_fixed=[], vulns_in_new=[])
    _RESULTS[update.bump.key] = (time.monotonic() + CACHE_TTL_SECONDS, snapshot)


async def _fill_notes(fetcher: Fetcher, update: DependencyUpdate) -> None:
    update.status = _STATUS_TIMED_OUT
    repo = await resolve_repo(fetcher, update.bump)
    if repo is None:
        update.status = _STATUS_NO_REPO if not fetcher.budget.exhausted else _STATUS_TIMED_OUT
        return
    owner, name = repo
    update.source_repo = f"{owner}/{name}"
    update.notes_url = f"https://github.com/{owner}/{name}/releases"
    releases, url = await fetch_release_notes(fetcher, owner, name, update.bump)
    update.releases = releases
    if url:
        update.notes_url = url
    if releases:
        update.status = _STATUS_NOT_SUMMARIZED
    elif not fetcher.budget.exhausted:
        update.status = _STATUS_NO_NOTES


async def _fill_vulns(updates: list[DependencyUpdate], timeout: float) -> None:
    from mira.security.osv import PackageQuery, query_batch

    queries = []
    for u in updates:
        b = u.bump
        queries += [PackageQuery(b.kind, b.name, b.old), PackageQuery(b.kind, b.name, b.new)]
    results = await query_batch(queries, timeout_s=timeout)
    for u in updates:
        b = u.bump
        old = {v.cve_id: v for v in results.get((b.kind, b.name, b.old), [])}
        new = {v.cve_id: v for v in results.get((b.kind, b.name, b.new), [])}
        u.vulns_fixed = [(i, v.severity, v.advisory_url) for i, v in old.items() if i not in new]
        u.vulns_in_new = [(i, v.severity, v.advisory_url) for i, v in new.items()]


async def collect_dependency_updates(
    files: list[FileDiff],
    read: ManifestReader,
    config: DependencyUpdatesConfig,
    llm: Any,
    *,
    osv_scan: bool = True,
) -> list[DependencyUpdate]:
    """Every version bump in ``files``, with what its release notes say. Never raises."""
    try:
        return await _collect(files, read, config, llm, osv_scan=osv_scan)
    except Exception as exc:  # noqa: BLE001 - this feature must never cost the review
        logger.warning("Dependency updates failed, continuing without: %s", exc)
        return []


async def _collect(
    files: list[FileDiff],
    read: ManifestReader,
    config: DependencyUpdatesConfig,
    llm: Any,
    *,
    osv_scan: bool,
) -> list[DependencyUpdate]:
    if not config.enabled:
        return []
    hosts = allowed_hosts(config.allowed_hosts)
    if not hosts:
        return []
    started = time.monotonic()
    budget = Budget(config.timeout_seconds, config.max_bytes)
    try:
        bumps = await asyncio.wait_for(detect_bumps(files, read), timeout=budget.time_left())
    except TimeoutError:
        logger.info("Dependency updates: reading the manifests timed out")
        return []
    if not bumps:
        return []

    updates = [DependencyUpdate(bump=b) for b in bumps]
    looked_up = updates[: config.max_packages]
    for u in updates[config.max_packages :]:
        u.status = _STATUS_OVER_LIMIT
    to_fetch = [u for u in looked_up if not _cached(u)]

    token = os.environ.get(config.github_token_env, "") if config.github_token_env else ""
    fetch_window = max(0.0, config.timeout_seconds * _FETCH_SHARE - (time.monotonic() - started))
    async with httpx.AsyncClient(follow_redirects=False, transport=PinnedTransport()) as client:
        fetcher = Fetcher(
            client,
            hosts=hosts,
            budget=budget,
            request_timeout=config.request_timeout_seconds,
            github_token=token,
        )
        jobs = [asyncio.create_task(_fill_notes(fetcher, u)) for u in to_fetch]
        # OSV is a host like any other: only when the allowlist names it, so an
        # install that narrowed `allowed_hosts` contacts nothing else.
        if osv_scan and OSV_HOST in hosts:
            jobs.append(
                asyncio.create_task(
                    _fill_vulns(updates, min(config.request_timeout_seconds * 2, fetch_window))
                )
            )
        if jobs:
            done, pending = await asyncio.wait(jobs, timeout=fetch_window)
            for task in pending:
                task.cancel()
            for task in done:
                if not task.cancelled() and task.exception() is not None:
                    logger.debug("Dependency updates: lookup failed: %s", task.exception())
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)

    pending_summary = [u for u in to_fetch if u.releases]
    if pending_summary and llm is not None and budget.time_left() > 0.5:
        try:
            await asyncio.wait_for(summarize(llm, pending_summary), timeout=budget.time_left())
        except Exception as exc:  # noqa: BLE001 - the links are still worth listing
            logger.info("Dependency updates: summarising release notes failed: %s", exc)
    for u in to_fetch:
        if u.summarized:
            u.status = ""
        if u.summarized or u.status in (_STATUS_NO_NOTES, _STATUS_NO_REPO):
            _remember(u)

    logger.info(
        "Dependency updates: %d bump(s), %d with notes, %d summarised in %.1fs",
        len(updates),
        sum(1 for u in updates if u.releases),
        sum(1 for u in updates if u.summarized),
        time.monotonic() - started,
    )
    return updates
