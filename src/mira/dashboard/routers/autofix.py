"""Dashboard routes for Phase 5 assisted correction.

Everything here is admin-only, and for the same reason the gate's routes are: a
job row quotes a finding, a diff, a model's reasoning and a validation
transcript across every repository in the install. That is governance data.

Two capabilities are separated on purpose:

* **Reading** a job, its attempts and the policy is plain admin.
* **Cancelling** a job is admin *plus* membership of ``autofix.cancel_admins``
  when one is set — the same shape as the gate's override list, and for the
  same reason: "can administer Mira" and "can stop a maintainer's fix" are
  different jobs in most teams.

Every mutating route also passes through the dashboard's origin check, so a
session cookie alone is not enough to stop somebody's work from another site.

There is deliberately no route that *starts* a fix. A fix is requested from the
pull request, by an account whose write permission the platform confirmed —
routing it through a dashboard session would make "can log into Mira" and "can
commit to this repository" the same permission, which is the one collapse this
phase exists to prevent.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from fastapi import HTTPException, Request
from pydantic import BaseModel, Field

from mira.autofix import history
from mira.autofix.capabilities import for_platform
from mira.autofix.models import FIX_MODES, JOB_STATES
from mira.autofix.policy import resolve_policy
from mira.autofix.worker import cancel_job
from mira.config import AutofixConfig, load_config
from mira.dashboard.api import _require_admin, router
from mira.gate.history import PlatformResolutionError

logger = logging.getLogger(__name__)

# Page sizes are capped rather than trusted: the same small-board profile that
# made the gate history paginate is the one serving this endpoint.
_MAX_PAGE = 200

# Owner/repo arrive from the URL and end up in a filesystem path on the SQLite
# backend, so they are validated as single safe segments — an admin session is
# not a filesystem primitive.
_UNSAFE_SEGMENT = re.compile(r"[/\x00]")
_NAMESPACED_OWNER = re.compile(r"^_(?:github|gitlab|forgejo)/(?P<owner>[^/\x00]+)$")

_SORT_KEYS = {"created_at", "updated_at", "pr_number", "state", "attempts"}


class AutofixJobPage(BaseModel):
    jobs: list[dict]
    total: int
    limit: int
    offset: int


class AutofixSummaryResponse(BaseModel):
    buckets: list[dict]
    totals: dict


class AutofixJobDetail(BaseModel):
    job: dict
    attempts: list[dict]
    policy: dict
    capabilities: dict


class AutofixConfigResponse(BaseModel):
    config: dict
    overrides: dict
    effective: dict
    handoff_adapters: list[str]


class AutofixConfigUpdate(BaseModel):
    """The autofix section of the admin override blob, replaced wholesale.

    Wholesale rather than patched so that clearing a list is expressible: a
    merge would make ``allowed_requesters: []`` indistinguishable from "leave it
    alone", and silently keeping an old allowlist is the wrong way to be wrong.
    """

    autofix: dict[str, Any] = Field(default_factory=dict)


def _unknown_keys(payload: dict[str, Any]) -> list[str]:
    """Keys in an override that `AutofixConfig` does not define, outermost only.

    Outermost only on purpose: the nested models are validated by pydantic in
    the usual way, and what this catches is the case pydantic is *quiet* about
    — a misspelt top-level section that would be dropped on load, leaving an
    admin looking at a saved form that changed nothing.
    """
    known = set(AutofixConfig.model_fields)
    return sorted(key for key in payload if key not in known)


class AutofixCancelInput(BaseModel):
    reason: str = ""


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


def _require_cancel(request: Request, owner: str, repo: str) -> str:
    """Admin, *plus* membership of the cancel list when one is set.

    Checked before anything is read or written, and it returns the actor rather
    than a bool so a caller cannot forget to record who acted.
    """
    _require_admin(request)
    actor = _actor(request)
    config = load_config()
    allowed = {name.strip().lower() for name in config.autofix.cancel_admins if name.strip()}
    if allowed and actor.lower() not in allowed:
        raise HTTPException(
            status_code=403,
            detail="This account is not permitted to cancel autofix jobs",
        )
    if not actor:
        # A cancellation with no attributable actor is not an audit record.
        raise HTTPException(status_code=403, detail="Cancelling requires an identified account")
    return actor


def _filters(
    owner: str,
    repo: str,
    platform: str,
    state: str,
    mode: str,
    pr_number: int,
    requested_by: str,
    finding_id: str,
    request_id_value: str,
    since: float,
    until: float,
) -> dict:
    if state and state not in JOB_STATES:
        raise HTTPException(status_code=400, detail=f"state must be one of {sorted(JOB_STATES)}")
    if mode and mode not in FIX_MODES:
        raise HTTPException(status_code=400, detail=f"mode must be one of {sorted(FIX_MODES)}")
    return {
        "owner": owner,
        "repo": repo,
        "platform": platform,
        "state": state,
        "mode": mode,
        "pr_number": pr_number,
        "requested_by": requested_by,
        "finding_id": finding_id,
        "request_id": request_id_value,
        "since": since,
        "until": until,
    }


@router.get("/api/autofix/jobs", response_model=AutofixJobPage)
def list_autofix_jobs(
    request: Request,
    owner: str = "",
    repo: str = "",
    platform: str = "",
    state: str = "",
    mode: str = "",
    pr_number: int = 0,
    requested_by: str = "",
    finding_id: str = "",
    request_id: str = "",
    since: float = 0.0,
    until: float = 0.0,
    sort: str = "created_at",
    order: str = "desc",
    limit: int = 50,
    offset: int = 0,
) -> AutofixJobPage:
    """Every recorded job, filtered and paginated."""
    _require_admin(request)
    if sort not in _SORT_KEYS:
        raise HTTPException(status_code=400, detail=f"sort must be one of {sorted(_SORT_KEYS)}")
    _validate_repo_filters(owner, repo)
    limit, offset = _page(limit, offset)
    with _resolved_platform():
        rows, total = history.list_jobs(
            filters=_filters(
                owner,
                repo,
                platform,
                state,
                mode,
                pr_number,
                requested_by,
                finding_id,
                request_id,
                since,
                until,
            ),
            limit=limit,
            offset=offset,
            sort=sort,
            descending=order != "asc",
        )
    return AutofixJobPage(
        jobs=[row.as_dict() for row in rows], total=total, limit=limit, offset=offset
    )


@router.get("/api/autofix/summary", response_model=AutofixSummaryResponse)
def autofix_summary(
    request: Request,
    owner: str = "",
    repo: str = "",
    platform: str = "",
    since: float = 0.0,
    until: float = 0.0,
) -> AutofixSummaryResponse:
    """State counts, plus the two numbers a rollout is watched through.

    ``opened`` is how many fixes produced something reviewable. ``dead_letter``
    is how many gave up. Their ratio is the honest measure of whether autofix
    is earning its keep on this install — and both are counted from the same
    rows whether the deployment is in ``suggest`` or in ``on``, so a dry run
    predicts what turning it on will do.
    """
    _require_admin(request)
    _validate_repo_filters(owner, repo)
    with _resolved_platform():
        buckets = history.summarize(
            filters=_filters(owner, repo, platform, "", "", 0, "", "", "", since, until)
        )
    totals: dict[str, Any] = dict.fromkeys(JOB_STATES, 0)
    total = 0
    opened = 0
    for bucket in buckets:
        totals[bucket["state"]] = totals.get(bucket["state"], 0) + bucket["count"]
        total += bucket["count"]
        opened += bucket["opened"]
    totals["total"] = total
    totals["published"] = opened
    return AutofixSummaryResponse(buckets=buckets, totals=totals)


@router.get("/api/autofix/jobs/{owner}/{repo}/{job_id}", response_model=AutofixJobDetail)
def autofix_job_detail(request: Request, owner: str, repo: str, job_id: int) -> AutofixJobDetail:
    """One job: what it did, every attempt it made, and under what policy."""
    _require_admin(request)
    _require_known_repo(owner, repo)
    with _resolved_platform():
        job = history.get_job(owner, repo, job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="No such autofix job")
        attempts = history.list_attempts(owner, repo, job_key=job.job_key)
    policy = resolve_policy(load_config().autofix, _public_owner(owner), repo)
    return AutofixJobDetail(
        job=job.as_dict(),
        attempts=[attempt.as_dict() for attempt in attempts],
        policy=policy.as_dict(),
        capabilities=for_platform(job.platform).as_dict(),
    )


@router.post("/api/autofix/jobs/{owner}/{repo}/{job_id}/cancel")
def cancel_autofix_job(
    owner: str, repo: str, job_id: int, body: AutofixCancelInput, request: Request
) -> dict:
    """Stop a job by hand, recording who stopped it and why.

    Authorization is checked *first*, before the job is even read: an account
    that may not cancel should not be able to use this endpoint to discover
    whether a job exists.

    Cancelling does not reach through to the platform. A job that already
    opened a pull request stays ``opened`` and its pull request stays open —
    closing somebody's pull request is not what "cancel" means, and a
    cancellation that could would make this endpoint a deletion primitive.
    """
    actor = _require_cancel(request, _public_owner(owner), repo)
    _require_known_repo(owner, repo)
    with _resolved_platform():
        job = history.get_job(owner, repo, job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="No such autofix job")
        platform = history.platform_for(owner, repo)
        updated = cancel_job(
            owner=owner,
            repo=repo,
            platform=platform,
            job_key=job.job_key,
            actor=actor,
            reason=(body.reason or "cancelled from the dashboard").strip()[:500],
        )
    logger.info("Autofix job %s in %s/%s cancelled by %s", job_id, owner, repo, actor)
    return {
        "ok": True,
        "cancelled": bool(updated and updated.state == "cancelled"),
        "job": (updated or job).as_dict(),
    }


@router.get("/api/autofix/config", response_model=AutofixConfigResponse)
def get_autofix_config(request: Request, owner: str = "", repo: str = "") -> AutofixConfigResponse:
    """The stored autofix override, and the policy that actually applies.

    Both, because they answer different questions: the override is what an
    admin typed, the effective policy is what a `fix` request will meet — and
    on an install with a `mira.yaml` and per-repository entries those are rarely
    the same document.
    """
    _require_admin(request)
    from mira.autofix import handoff
    from mira.dashboard.api import _app_db

    stored = (_app_db.get_global_review_overrides() or {}).get("autofix", {}) if _app_db else {}
    config = load_config()
    policy = resolve_policy(config.autofix, owner, repo)
    return AutofixConfigResponse(
        config=config.autofix.model_dump(),
        overrides=stored,
        effective=policy.as_dict(),
        handoff_adapters=handoff.available(),
    )


@router.put("/api/autofix/config")
def set_autofix_config(body: AutofixConfigUpdate, request: Request) -> dict:
    """Replace the autofix section of the admin override blob.

    Validated against the real model before anything is written, so a typo
    fails the request rather than the next fix request. Only the ``autofix``
    section is touched, and it is replaced in a single statement, so two panels
    editing one blob cannot clobber each other.

    Two things this route deliberately will not do.

    It will not accept a ``validation`` section. Those entries are argv lists
    that a worker executes as the Mira service account, and ``["/bin/sh", "-c",
    "…"]`` is a perfectly well-formed argv list. Storing commands as lists stops
    a *pull request* from injecting one; it does nothing about an admin session
    that types one in. Validation commands come from deployment configuration —
    the file, the environment — where changing them means having the host.

    It will not accept a key the model does not define. Pydantic ignores unknown
    fields by default, so ``{"mod": "on"}`` would validate, persist, change
    nothing, and report success — which is the worst of the three outcomes.
    """
    _require_admin(request)
    from pydantic import ValidationError

    if "validation" in body.autofix:
        raise HTTPException(
            status_code=400,
            detail={
                "field": "validation",
                "message": (
                    "Validation commands are deployment configuration and cannot be "
                    "set from the dashboard"
                ),
            },
        )
    unknown = _unknown_keys(body.autofix)
    if unknown:
        raise HTTPException(
            status_code=400,
            detail={
                "field": unknown[0],
                "message": f"Unknown autofix setting(s): {', '.join(unknown)}",
            },
        )
    try:
        AutofixConfig.model_validate(body.autofix)
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
            status_code=400, detail={"message": f"Invalid autofix settings: {exc}"}
        ) from exc

    from mira.dashboard.api import _app_db

    if _app_db is None:  # pragma: no cover - only unconfigured installs
        raise HTTPException(status_code=503, detail="No settings store is configured")
    _app_db.update_global_review_overrides_section("autofix", body.autofix or None)
    logger.info("Autofix policy updated by %s", _actor(request) or "an admin")
    return {"ok": True, "autofix": body.autofix}
