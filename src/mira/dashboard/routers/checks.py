"""Dashboard routes for the Phase 6 pre-merge checks.

Three permissions live here, and keeping them apart is the point rather than an
implementation detail:

* **Reading a run** is admin-only. A check result quotes diff lines, CI output
  and ticket titles across every repository in the install; that is governance
  data, not per-repo browsing data.
* **Editing the policy** is admin-only, and is the only way policy changes.
  Nothing that arrives from a pull request can reach these endpoints — which is
  the same statement :mod:`mira.checks.config_models` makes about its own
  values, enforced here at the other end.
* **Reading the audit trail** is admin-only and is deliberately a separate
  endpoint from the policy itself. "What is the policy now" and "who changed it
  and to what" are different questions, and the second one is the one asked
  after something has gone wrong.

Every mutating route passes through the dashboard's origin check, so a session
cookie alone is not enough to change a policy from another site.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from fastapi import HTTPException, Request
from pydantic import BaseModel, Field

from mira.checks import history
from mira.checks.config_models import ChecksConfig
from mira.checks.explain import admin_explanation, public_explanation
from mira.checks.history import PlatformResolutionError
from mira.checks.models import CHECK_MODES, CHECK_ORIGINS, CHECK_STATES, RUN_VERDICTS
from mira.checks.policy import resolve_policy
from mira.checks.registry import catalog
from mira.config import load_config
from mira.dashboard.api import _require_admin, router

logger = logging.getLogger(__name__)

# Page sizes are capped rather than trusted: the same Orange Pi profile that
# made the history paginate is the one serving this endpoint.
_MAX_PAGE = 200

# Owner/repo arrive from the URL and end up in a filesystem path on the SQLite
# backend, so they are validated as single safe segments — an admin session is
# not a filesystem primitive.
_UNSAFE_SEGMENT = re.compile(r"[/\x00]")
_NAMESPACED_OWNER = re.compile(r"^_(?:github|gitlab|forgejo)/(?P<owner>[^/\x00]+)$")

_RUN_SORT_KEYS = {"created_at", "pr_number", "verdict", "duration_seconds"}
_RESULT_SORT_KEYS = {"created_at", "check_id", "state", "duration_seconds"}

# The settings-blob section these routes own. Named once, because it is both
# the key written and the audit trail's filter.
_SECTION = "checks"


class CheckRunPage(BaseModel):
    runs: list[dict]
    total: int
    limit: int
    offset: int


class CheckResultPage(BaseModel):
    results: list[dict]
    total: int
    limit: int
    offset: int


class CheckRunDetail(BaseModel):
    run: dict
    public_explanation: str
    admin_explanation: str
    policy: dict


class CheckSummaryResponse(BaseModel):
    buckets: list[dict]
    totals: dict


class CheckCatalogResponse(BaseModel):
    checks: list[dict]
    policy: dict


class ChecksConfigResponse(BaseModel):
    config: dict
    overrides: dict
    effective: dict


class ChecksConfigUpdate(BaseModel):
    """The checks section of the admin override blob, replaced wholesale.

    Wholesale rather than patched so that clearing a list is expressible: a
    merge would make ``natural_language: []`` indistinguishable from "leave it
    alone", and silently keeping a rule an admin deleted is the wrong way to be
    wrong.
    """

    checks: dict[str, Any] = Field(default_factory=dict)


class ChecksAuditPage(BaseModel):
    entries: list[dict]
    limit: int
    offset: int


def _page(limit: int, offset: int) -> tuple[int, int]:
    return max(1, min(limit, _MAX_PAGE)), max(0, offset)


def _safe_segment(value: str) -> bool:
    return bool(value) and value not in {".", ".."} and not _UNSAFE_SEGMENT.search(value)


def _public_owner(owner: str) -> str:
    match = _NAMESPACED_OWNER.match(owner)
    return match.group("owner") if match else owner


def _require_known_repo(owner: str, repo: str) -> None:
    """Reject traversal and unknown repositories before touching a store."""
    if not _safe_segment(repo) or not (_safe_segment(owner) or _NAMESPACED_OWNER.match(owner)):
        raise HTTPException(status_code=400, detail="Invalid repository identifier")

    from mira.dashboard.api import _app_db

    if _app_db is None:  # pragma: no cover - only unconfigured installs
        return
    try:
        records = _app_db.get_repo_any_platform(_public_owner(owner), repo)
    except Exception as exc:
        raise HTTPException(
            status_code=503, detail="The repository registry is unavailable"
        ) from exc
    if not records:
        raise HTTPException(status_code=404, detail=f"Repo {owner}/{repo} not found")


def _validate_repo_filters(owner: str, repo: str) -> None:
    for value, label in ((owner, "owner"), (repo, "repo")):
        if value and not (
            _safe_segment(value) or (label == "owner" and _NAMESPACED_OWNER.match(value))
        ):
            raise HTTPException(status_code=400, detail=f"Invalid {label}")
    if owner and repo:
        _require_known_repo(owner, repo)


@contextmanager
def _resolved_platform() -> Iterator[None]:
    """Surface an unreachable repo registry as 503, never as an empty result."""
    try:
        yield
    except PlatformResolutionError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


def _actor(request: Request) -> str:
    user = getattr(request.state, "user", None)
    return str(getattr(user, "username", "") or "")


def _one_of(value: str, allowed: tuple[str, ...], label: str) -> str:
    if value and value not in allowed:
        raise HTTPException(status_code=400, detail=f"{label} must be one of {sorted(allowed)}")
    return value


@router.get("/api/checks/runs", response_model=CheckRunPage)
def list_check_runs(
    request: Request,
    owner: str = "",
    repo: str = "",
    platform: str = "",
    verdict: str = "",
    pr_number: int = 0,
    pr_author: str = "",
    head_sha: str = "",
    since: float = 0.0,
    until: float = 0.0,
    sort: str = "created_at",
    order: str = "desc",
    limit: int = 50,
    offset: int = 0,
    with_results: bool = False,
) -> CheckRunPage:
    """Every recorded run, filtered and paginated."""
    _require_admin(request)
    if sort not in _RUN_SORT_KEYS:
        raise HTTPException(status_code=400, detail=f"sort must be one of {sorted(_RUN_SORT_KEYS)}")
    _one_of(verdict, RUN_VERDICTS, "verdict")
    _validate_repo_filters(owner, repo)
    limit, offset = _page(limit, offset)
    with _resolved_platform():
        rows, total = history.list_runs(
            filters={
                "owner": owner,
                "repo": repo,
                "platform": platform,
                "verdict": verdict,
                "pr_number": pr_number,
                "pr_author": pr_author,
                "head_sha": head_sha,
                "since": since,
                "until": until,
            },
            limit=limit,
            offset=offset,
            sort=sort,
            descending=order != "asc",
            with_results=with_results,
        )
    return CheckRunPage(
        runs=[row.as_dict() for row in rows], total=total, limit=limit, offset=offset
    )


@router.get("/api/checks/results", response_model=CheckResultPage)
def list_check_results(
    request: Request,
    owner: str = "",
    repo: str = "",
    platform: str = "",
    check_id: str = "",
    origin: str = "",
    state: str = "",
    mode: str = "",
    pr_number: int = 0,
    head_sha: str = "",
    blocking: bool = False,
    incomplete: bool = False,
    since: float = 0.0,
    until: float = 0.0,
    sort: str = "created_at",
    order: str = "desc",
    limit: int = 100,
    offset: int = 0,
) -> CheckResultPage:
    """One check's history: every time it ran, what it said, how long it took.

    The ``incomplete`` filter is the one an operator reaches for first after an
    incident: it selects exactly the results that were *not* statements about a
    pull request, which is the set a noisy-check investigation must exclude and
    an infrastructure investigation must start from.
    """
    _require_admin(request)
    if sort not in _RESULT_SORT_KEYS:
        raise HTTPException(
            status_code=400, detail=f"sort must be one of {sorted(_RESULT_SORT_KEYS)}"
        )
    _one_of(state, CHECK_STATES, "state")
    _one_of(mode, CHECK_MODES, "mode")
    _one_of(origin, CHECK_ORIGINS, "origin")
    _validate_repo_filters(owner, repo)
    limit, offset = _page(limit, offset)
    with _resolved_platform():
        rows, total = history.list_results(
            filters={
                "owner": owner,
                "repo": repo,
                "platform": platform,
                "check_id": check_id,
                "origin": origin,
                "state": state,
                "mode": mode,
                "pr_number": pr_number,
                "head_sha": head_sha,
                "blocking": blocking,
                "incomplete": incomplete,
                "since": since,
                "until": until,
            },
            limit=limit,
            offset=offset,
            sort=sort,
            descending=order != "asc",
        )
    return CheckResultPage(
        results=[row.as_dict() for row in rows], total=total, limit=limit, offset=offset
    )


@router.get("/api/checks/summary", response_model=CheckSummaryResponse)
def checks_summary(
    request: Request,
    owner: str = "",
    repo: str = "",
    platform: str = "",
    since: float = 0.0,
    until: float = 0.0,
) -> CheckSummaryResponse:
    """Counts by check and state, plus the one ratio worth watching.

    ``inconclusive`` against ``total`` is the framework's own health number:
    how often a check said nothing about a pull request because Mira could not
    answer. A rollout where that climbs has an infrastructure problem, and no
    per-check violation count would show it.
    """
    _require_admin(request)
    _validate_repo_filters(owner, repo)
    with _resolved_platform():
        buckets = history.summarize(
            filters={
                "owner": owner,
                "repo": repo,
                "platform": platform,
                "since": since,
                "until": until,
            }
        )
    totals: dict[str, Any] = dict.fromkeys(CHECK_STATES, 0)
    total = 0
    for bucket in buckets:
        totals[bucket["state"]] = totals.get(bucket["state"], 0) + bucket["count"]
        total += bucket["count"]
    totals["total"] = total
    totals["inconclusive"] = totals.get("infrastructure_error", 0) + totals.get("timeout", 0)
    return CheckSummaryResponse(buckets=buckets, totals=totals)


@router.get("/api/checks/runs/{owner}/{repo}/{run_id}", response_model=CheckRunDetail)
def check_run_detail(request: Request, owner: str, repo: str, run_id: int) -> CheckRunDetail:
    """One run: every check, its evidence, its origin and its duration."""
    _require_admin(request)
    _require_known_repo(owner, repo)
    with _resolved_platform():
        run = history.get_run(owner, repo, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="No such check run")
    policy = resolve_policy(load_config().checks, _public_owner(owner), repo)
    return CheckRunDetail(
        run=run.as_dict(),
        public_explanation=public_explanation(run),
        admin_explanation=admin_explanation(run),
        policy=policy.as_dict(),
    )


@router.get("/api/checks/catalog", response_model=CheckCatalogResponse)
def checks_catalog(request: Request, owner: str = "", repo: str = "") -> CheckCatalogResponse:
    """Every check that would run for a repository, with its version and mode.

    Answers the coverage question the run history cannot: a check that never
    appears in a result row might be off, might not apply, or might not exist
    in this version of Mira, and only the catalog distinguishes the third.
    """
    _require_admin(request)
    _validate_repo_filters(owner, repo)
    policy = resolve_policy(load_config().checks, owner, repo)
    return CheckCatalogResponse(checks=catalog(policy), policy=policy.as_dict())


@router.get("/api/checks/config", response_model=ChecksConfigResponse)
def get_checks_config(request: Request, owner: str = "", repo: str = "") -> ChecksConfigResponse:
    """The stored override, and the policy that actually applies.

    Both, because they answer different questions: the override is what an
    admin typed, the effective policy is what a pull request will meet — and on
    an install with a ``mira.yaml``, organisation entries and per-repository
    entries those are rarely the same document.
    """
    _require_admin(request)
    _validate_repo_filters(owner, repo)
    from mira.dashboard.api import _app_db

    stored = (_app_db.get_global_review_overrides() or {}).get(_SECTION, {}) if _app_db else {}
    config = load_config()
    policy = resolve_policy(config.checks, owner, repo)
    return ChecksConfigResponse(
        config=config.checks.model_dump(),
        overrides=stored,
        effective=policy.as_dict(),
    )


@router.put("/api/checks/config")
def set_checks_config(body: ChecksConfigUpdate, request: Request) -> dict:
    """Replace the checks section of the admin override blob.

    Validated against the real model before anything is written, so a malformed
    natural-language rule or an analyser outside the allowlist fails the
    request rather than the next pull request.

    Only the ``checks`` section is touched — the review, gate and autofix
    overrides an admin set on other panels are left alone by writing through
    the one-statement section update rather than a read-modify-write, so two
    panels editing one blob cannot clobber each other.
    """
    _require_admin(request)
    from pydantic import ValidationError

    try:
        ChecksConfig.model_validate(body.checks)
    except ValidationError as exc:
        first = exc.errors()[0]
        raise HTTPException(
            status_code=400,
            detail={
                "field": ".".join(str(part) for part in first.get("loc", ())),
                "message": first.get("msg", "invalid value"),
            },
        ) from exc
    except Exception as exc:
        raise HTTPException(
            status_code=400, detail={"message": f"Invalid check settings: {exc}"}
        ) from exc

    from mira.dashboard.api import _app_db

    if _app_db is None:  # pragma: no cover - only unconfigured installs
        raise HTTPException(status_code=503, detail="No settings store is configured")

    actor = _actor(request) or "an admin"
    # Read the previous value *before* writing it, so the audit entry records
    # what was replaced rather than what replaced it. The window between the
    # two is the same window every settings panel already has, and the entry
    # naming both halves is what makes a policy change reviewable at all.
    previous = (_app_db.get_global_review_overrides() or {}).get(_SECTION, {})
    _app_db.update_global_review_overrides_section(_SECTION, body.checks or None)
    _app_db.record_config_audit(
        section=_SECTION,
        actor=actor,
        previous=previous,
        new=body.checks or {},
        action="update" if body.checks else "clear",
    )
    logger.info("Check policy updated by %s", actor)
    return {"ok": True, "checks": body.checks}


@router.get("/api/checks/config/audit", response_model=ChecksAuditPage)
def checks_config_audit(
    request: Request, limit: int = 50, offset: int = 0, section: str = _SECTION
) -> ChecksAuditPage:
    """Who changed the policy, when, from what and to what.

    Append-only and separate from the policy itself, because the blob only ever
    carries the current value: a policy loosened for an afternoon and tightened
    again leaves no trace in it, and that is precisely the change somebody
    comes looking for.
    """
    _require_admin(request)
    from mira.dashboard.api import _app_db

    if _app_db is None:  # pragma: no cover - only unconfigured installs
        return ChecksAuditPage(entries=[], limit=limit, offset=offset)
    limit, offset = _page(limit, offset)
    entries = _app_db.list_config_audit(section=section, limit=limit, offset=offset)
    return ChecksAuditPage(entries=entries, limit=limit, offset=offset)
