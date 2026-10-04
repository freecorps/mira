"""Building, storing and delivering digests; building release notes.

The entry points the CLI, the dashboard and the scheduler share:

* :func:`build_digest` — collect, group, summarise. Pure apart from the
  provider and model calls it is handed.
* :func:`publish_digest` — store it and send it to the outbound webhooks (and
  email, when configured). Never raises.
* :func:`run_scheduled` — one scheduler tick: if a boundary has passed that no
  process has run yet, claim it and build every configured digest for it. A
  run in which every digest failed hands the claim back for the next tick.
* :func:`build_release_notes_for` — release notes for a repository.
"""

from __future__ import annotations

import logging
import os
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from mira.config import DigestsConfig, MiraConfig
from mira.digests import schedule
from mira.digests.areas import attach_context, group_by_area
from mira.digests.collect import collect_between, collect_window
from mira.digests.models import Digest, day
from mira.digests.release_notes import ReleaseNotes, build_release_notes
from mira.digests.render import render_markdown, render_text, webhook_payload
from mira.digests.summarize import fallback_overview, summarize
from mira.providers.base import repository_ref

logger = logging.getLogger(__name__)

#: The settings key holding the last schedule boundary a digest run claimed.
LAST_BOUNDARY_KEY = "digests:last_boundary"


@dataclass
class DigestTarget:
    """One repository to read, with the provider that can read it."""

    provider: Any
    platform: str
    owner: str
    repo: str
    branch: str = ""
    # Area name -> one-line description, usually from the repository index.
    context: Callable[[str], str] | None = None


def index_context(platform: str, owner: str, repo: str) -> Callable[[str], str] | None:
    """Directory summaries from the repository index, for path-named areas.

    Read once, up front, into a closure: the store is opened and closed here
    rather than held across model calls. ``None`` when the repository has no
    index — on SQLite that is checked without creating one.
    """
    from mira.index.store import IndexStore

    db_url = os.environ.get("DATABASE_URL", "")
    if not db_url.startswith(("postgresql://", "postgres://")) and not os.path.exists(
        IndexStore.db_path_for(owner, repo, platform)
    ):
        return None
    cache: dict[str, str] = {}

    def lookup(name: str) -> str:
        if not name.endswith("/"):
            return ""
        if name not in cache:
            store = None
            try:
                store = IndexStore.open(owner, repo, platform=platform)
                found = store.get_directory_summary(name.rstrip("/"))
                cache[name] = found.summary if found else ""
            except Exception as exc:  # noqa: BLE001 - context only helps the model
                logger.debug("No index context for %s in %s/%s: %s", name, owner, repo, exc)
                cache[name] = ""
            finally:
                if store is not None:
                    store.close()
        return cache[name]

    return lookup


async def build_digest(
    targets: list[DigestTarget],
    *,
    since: float,
    until: float,
    cfg: DigestsConfig,
    llm: Any = None,
    org: bool = False,
) -> Digest:
    """Collect what landed in ``[since, until)`` and summarise it per area.

    One target is a repository digest. Several (or ``org=True``) is an org-wide
    digest: areas are prefixed with the repository name, and a repository that
    cannot be read is a note rather than a failure. A single repository that
    cannot be read raises — there is nothing else to deliver.
    """
    if not targets:
        raise ValueError("a digest needs at least one repository")
    org = org or len(targets) > 1
    first = targets[0]
    digest = Digest(
        platform=first.platform,
        owner=first.owner,
        repo="" if org else first.repo,
        period_start=since,
        period_end=until,
        generated_at=time.time(),
    )
    changes = []
    for target in targets:
        ref = repository_ref(target.platform, target.owner, target.repo)
        try:
            collected = await collect_window(
                target.provider,
                ref,
                since=since,
                until=until,
                branch=target.branch,
                max_pull_requests=cfg.max_pull_requests,
                max_commits=cfg.max_commits,
                max_files=cfg.max_files_per_change,
                include_direct_commits=cfg.include_direct_commits,
                label=target.repo if org else "",
            )
        except Exception as exc:
            if not org:
                raise
            logger.warning("Digest: could not read %s/%s: %s", target.owner, target.repo, exc)
            digest.notes.append(f"{target.repo} could not be read.")
            continue
        changes.extend(collected.changes)
        digest.pull_requests += collected.pull_requests
        digest.direct_commits += collected.direct_commits
        prefix = f"{target.repo}: " if org else ""
        digest.notes.extend(prefix + note for note in collected.notes)
        if not org:
            digest.branch = collected.branch

    digest.areas = group_by_area(changes, cfg.areas, depth=cfg.area_depth, prefix_repo=org)
    if not org:
        attach_context(digest.areas, first.context)
    if llm is not None and cfg.use_llm and not digest.is_empty:
        digest.llm_used = await summarize(llm, digest, max_chars=cfg.max_input_chars)
    if not digest.overview:
        digest.overview = fallback_overview(digest)
    return digest


async def publish_digest(
    digest: Digest,
    *,
    cfg: DigestsConfig,
    db: Any = None,
    deliver: bool = True,
) -> int:
    """Store the digest and deliver it. Returns the stored id (0 if not stored).

    Never raises: storing and delivering are separate, so a database hiccup
    still delivers and a dead webhook still stores.
    """
    markdown = render_markdown(digest)
    digest_id = 0
    if db is not None:
        try:
            digest_id = db.save_digest(
                kind="digest",
                platform=digest.platform,
                owner=digest.owner,
                repo=digest.repo,
                period_start=digest.period_start,
                period_end=digest.period_end,
                title=digest.title,
                markdown=markdown,
                data=digest.to_dict(),
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("Could not store the digest for %s: %s", digest.scope_name, exc)
    if not deliver:
        return digest_id
    from mira.outbound_webhooks import DIGEST_READY, dispatch_event

    await dispatch_event(DIGEST_READY, webhook_payload(digest, digest_id=digest_id))
    if cfg.email.recipients:
        from mira.digests.delivery import send_email

        await send_email(digest.title, render_text(digest), cfg.email.recipients)
    return digest_id


# ── Scheduling ─────────────────────────────────────────────────────────────

ProviderFor = Callable[[str, str, str], Awaitable[Any]]


def scheduled_repositories(cfg: DigestsConfig, db: Any) -> list[tuple[str, str, str]]:
    """``(platform, owner, repo)`` for every repository a scheduled run covers."""
    if cfg.repositories:
        out = []
        for key in cfg.repositories:
            platform, _, slug = key.partition(":")
            owner, _, repo = slug.partition("/")
            out.append((platform, owner, repo))
        return out
    if db is None:
        return []
    return [(r.platform or "github", r.owner, r.repo) for r in db.list_repos()]


def _claim(db: Any, boundary_ts: float) -> tuple[bool, str | None]:
    current = db.get_setting(LAST_BOUNDARY_KEY)
    try:
        if current is not None and float(current) >= boundary_ts:
            return False, current
    except ValueError:
        pass
    return bool(db.compare_and_set_setting(LAST_BOUNDARY_KEY, current, repr(boundary_ts))), current


def claim_boundary(db: Any, boundary_ts: float) -> bool:
    """Take the boundary for this process. False when it was already run.

    A compare-and-set on the settings table, so two processes (or a restart
    mid-tick) cannot both deliver the same week. The claim comes before the
    work, so a digest is delivered at most once; see :func:`run_scheduled`
    for when a claim is handed back.
    """
    return _claim(db, boundary_ts)[0]


def release_boundary(db: Any, boundary_ts: float, previous: str | None) -> bool:
    """Hand a claimed boundary back so the next tick runs it again.

    Only undoes this process's own claim: if anything moved the setting on
    since, it is left alone.
    """
    try:
        return bool(
            db.compare_and_set_setting(LAST_BOUNDARY_KEY, repr(boundary_ts), previous or "0")
        )
    except Exception as exc:  # noqa: BLE001 - the claim stands; the period is skipped
        logger.warning("Could not release the digest boundary: %s", exc)
        return False


async def run_scheduled(
    config: MiraConfig,
    *,
    db: Any,
    provider_for: ProviderFor,
    llm: Any = None,
    now: datetime | None = None,
) -> list[int]:
    """One scheduler tick. Returns the ids of the digests it stored.

    The boundary is claimed before any work, so a digest that was delivered is
    never delivered again. When nothing in the run succeeded — every
    repository failed (no provider, or the digest could not be built) or the
    run raised — the claim is handed back and the next tick retries the
    period. A run where some repositories succeeded keeps the claim: the
    failed ones miss that period rather than the others being sent twice.
    """
    cfg = config.digests
    if not cfg.enabled or db is None:
        return []
    boundary = schedule.latest_boundary(now or datetime.now(tz=UTC), cfg)
    claimed, previous = _claim(db, boundary.timestamp())
    if not claimed:
        return []
    try:
        stored, attempted, succeeded = await _run_period(
            cfg, boundary, db=db, provider_for=provider_for, llm=llm
        )
    except BaseException:
        release_boundary(db, boundary.timestamp(), previous)
        raise
    if attempted and not succeeded:
        logger.warning("Every digest in this run failed; the period will be retried")
        release_boundary(db, boundary.timestamp(), previous)
    return stored


async def _run_period(
    cfg: DigestsConfig,
    boundary: datetime,
    *,
    db: Any,
    provider_for: ProviderFor,
    llm: Any,
) -> tuple[list[int], int, int]:
    """Build and publish every digest for the period ending at ``boundary``.

    Returns the stored ids, how many digests were attempted, and how many
    succeeded (built, whether published or skipped as empty).
    """
    since, until = schedule.period_ending(boundary, cfg)
    logger.info("Digest run for %s to %s", day(since), day(until))

    repos = scheduled_repositories(cfg, db)
    groups: dict[tuple[str, str, str], list[tuple[str, str, str]]] = {}
    for platform, owner, repo in repos:
        key = (platform, owner, "" if cfg.scope == "org" else repo)
        groups.setdefault(key, []).append((platform, owner, repo))

    stored: list[int] = []
    attempted = succeeded = 0
    for (platform, owner, _), members in groups.items():
        attempted += 1
        targets: list[DigestTarget] = []
        for _platform, _owner, repo in members:
            try:
                provider = await provider_for(platform, owner, repo)
            except Exception as exc:  # noqa: BLE001 - one repository, not the run
                logger.warning("Digest: no provider for %s/%s: %s", owner, repo, exc)
                continue
            targets.append(
                DigestTarget(
                    provider=provider,
                    platform=platform,
                    owner=owner,
                    repo=repo,
                    context=index_context(platform, owner, repo) if cfg.scope == "repo" else None,
                )
            )
        if not targets:
            continue
        try:
            digest = await build_digest(
                targets, since=since, until=until, cfg=cfg, llm=llm, org=cfg.scope == "org"
            )
        except Exception as exc:  # noqa: BLE001 - the next repository still gets one
            logger.warning("Digest for %s/%s failed: %s", owner, members[0][2], exc)
            continue
        succeeded += 1
        if digest.is_empty and cfg.skip_empty:
            logger.info("Digest for %s: nothing landed, skipped", digest.scope_name)
            continue
        stored.append(await publish_digest(digest, cfg=cfg, db=db))
    return stored, attempted, succeeded


# ── Release notes ──────────────────────────────────────────────────────────


async def build_release_notes_for(
    provider: Any,
    *,
    platform: str,
    owner: str,
    repo: str,
    cfg: DigestsConfig,
    from_ref: str = "",
    to_ref: str = "",
    since: float = 0.0,
    until: float = 0.0,
    llm: Any = None,
) -> ReleaseNotes:
    """Release notes for ``from_ref..to_ref``, or for what merged since a date.

    ``to_ref`` defaults to the default branch. Exactly one of ``from_ref`` and
    ``since`` must be given.
    """
    if bool(from_ref) == bool(since):
        raise ValueError("give either a starting ref or a starting date, not both")
    ref = repository_ref(platform, owner, repo)
    head = to_ref or await provider.get_default_branch(ref)
    if not head:
        raise ValueError("the default branch could not be read; pass --to")
    if from_ref:
        collected = await collect_between(
            provider,
            ref,
            base=from_ref,
            head=head,
            max_pull_requests=cfg.max_pull_requests,
            max_commits=max(cfg.max_commits, 250),
            include_direct_commits=cfg.include_direct_commits,
        )
        title = f"{owner}/{repo}: {from_ref}...{head}"
    else:
        collected = await collect_window(
            provider,
            ref,
            since=since,
            until=until or time.time(),
            branch=head,
            max_pull_requests=cfg.max_pull_requests,
            max_commits=cfg.max_commits,
            max_files=0,
            include_direct_commits=cfg.include_direct_commits,
        )
        title = f"{owner}/{repo}: changes on {head} since {day(since)}"
    return await build_release_notes(
        collected.changes,
        title=title,
        cfg=cfg.release_notes,
        llm=llm if cfg.use_llm else None,
        max_chars=cfg.max_input_chars,
        notes=collected.notes,
    )
