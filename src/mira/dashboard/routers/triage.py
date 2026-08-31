"""Dashboard routes for Phase 7C triage and reviewer suggestion.

Every route here is admin-only, and that is a stronger statement than it is on
most of the other panels. A triage run is a record of *people*: who owns which
files, who has worked on them, who was suggested and who was passed over and
why. That is governance data about colleagues, not per-repository browsing
data, and the one endpoint that would be most tempting to open up — "who gets
suggested most" — is exactly the one that should not be.

Editing the policy is admin-only too, and is the only way the policy changes.
Nothing that arrives from a pull request reaches these endpoints, which is the
same statement :mod:`mira.triage.config_models` makes at the other end. Every
mutating route passes through the dashboard's origin check, so a session cookie
alone is not enough to change a policy from another site — and every change is
recorded in the same append-only audit trail the other panels write to.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from fastapi import HTTPException, Request
from pydantic import BaseModel, Field

from mira.config import load_config
from mira.dashboard.api import _require_admin, router
from mira.triage import queries
from mira.triage.config_models import TriageConfig
from mira.triage.explain import admin_explanation, public_explanation
from mira.triage.models import TRIAGE_STATUSES
from mira.triage.policy import resolve_policy
from mira.triage.queries import PlatformResolutionError

logger = logging.getLogger(__name__)

_MAX_PAGE = 200

# Owner and repo arrive from the URL and become part of a filesystem path on
# the SQLite backend, so they are validated as single safe segments. An admin
# session is not a filesystem primitive.
_UNSAFE_SEGMENT = re.compile(r"[/\x00]")
_NAMESPACED_OWNER = re.compile(r"^_(?:github|gitlab|forgejo)/(?P<owner>[^/\x00]+)$")

_RUN_SORT_KEYS = {"created_at", "pr_number", "status", "duration_seconds"}

# The settings-blob section these routes own. Named once: it is both the key
# written and the audit trail's filter.
_SECTION = "triage"


class TriageRunPage(BaseModel):
    runs: list[dict]
    total: int
    limit: int
    offset: int


class TriageRunDetail(BaseModel):
    run: dict
    public_explanation: str
    admin_explanation: str
    policy: dict


class TriageSuggestionSummary(BaseModel):
    identities: list[dict]
    totals: dict


class TriageConfigResponse(BaseModel):
    config: dict
    overrides: dict
    effective: dict
    # What would apply if the stored override were removed. The panel needs it
    # as the baseline for "did the admin change this?": comparing against
    # `config`, which already has the override folded in, makes handing a field
    # back to its `mira.yaml` value indistinguishable from setting it, so an
    # override could never be cleared one field at a time.
    inherited: dict


class TriageConfigUpdate(BaseModel):
    """The triage section of the admin override blob, replaced wholesale.

    Wholesale rather than patched, so clearing a list is expressible: under a
    merge, ``exclude: []`` would be indistinguishable from "leave it alone",
    and quietly keeping somebody on an opt-out list they were removed from is
    the wrong way to be wrong. Quietly keeping them *on* it is the right way,
    which is why the shape is the same as the check panel's rather than being
    argued about again here.
    """

    triage: dict[str, Any] = Field(default_factory=dict)


class TriageAuditPage(BaseModel):
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


@router.get("/api/triage/runs", response_model=TriageRunPage)
def list_triage_runs(
    request: Request,
    owner: str = "",
    repo: str = "",
    platform: str = "",
    status: str = "",
    pr_number: int = 0,
    pr_author: str = "",
    head_sha: str = "",
    identity: str = "",
    degraded: bool = False,
    since: float = 0.0,
    until: float = 0.0,
    sort: str = "created_at",
    order: str = "desc",
    limit: int = 50,
    offset: int = 0,
) -> TriageRunPage:
    """Every recorded run, filtered and paginated.

    ``degraded`` is the filter an operator reaches for first: it selects the
    runs where a signal did not answer, which is the set that says something
    about Mira rather than about the repository.
    """
    _require_admin(request)
    if sort not in _RUN_SORT_KEYS:
        raise HTTPException(status_code=400, detail=f"sort must be one of {sorted(_RUN_SORT_KEYS)}")
    if status and status not in TRIAGE_STATUSES:
        raise HTTPException(
            status_code=400, detail=f"status must be one of {sorted(TRIAGE_STATUSES)}"
        )
    _validate_repo_filters(owner, repo)
    limit, offset = _page(limit, offset)
    with _resolved_platform():
        rows, total = queries.list_runs(
            filters={
                "owner": owner,
                "repo": repo,
                "platform": platform,
                "status": status,
                "pr_number": pr_number,
                "pr_author": pr_author,
                "head_sha": head_sha,
                "identity": (identity or "").strip().lstrip("@").lower(),
                "degraded": degraded,
                "since": since,
                "until": until,
            },
            limit=limit,
            offset=offset,
            sort=sort,
            descending=order != "asc",
        )
    return TriageRunPage(
        runs=[row.as_dict() for row in rows], total=total, limit=limit, offset=offset
    )


@router.get("/api/triage/suggestions", response_model=TriageSuggestionSummary)
def triage_suggestions(
    request: Request,
    owner: str = "",
    repo: str = "",
    platform: str = "",
    since: float = 0.0,
    until: float = 0.0,
) -> TriageSuggestionSummary:
    """How often each identity has been suggested, and how highly.

    The question to ask before turning suggestions on anywhere else: is this
    naming the same two people over and over, or spreading across the team.
    """
    _require_admin(request)
    _validate_repo_filters(owner, repo)
    with _resolved_platform():
        rows = queries.summarize_candidates(
            filters={
                "owner": owner,
                "repo": repo,
                "platform": platform,
                "since": since,
                "until": until,
            }
        )
    suggestions = sum(int(row.get("count") or 0) for row in rows)
    return TriageSuggestionSummary(
        identities=rows,
        totals={"identities": len(rows), "suggestions": suggestions},
    )


@router.get("/api/triage/runs/{owner}/{repo}/{run_id}", response_model=TriageRunDetail)
def triage_run_detail(request: Request, owner: str, repo: str, run_id: int) -> TriageRunDetail:
    """One run: the classification, every candidate's arithmetic, everyone dropped."""
    _require_admin(request)
    _require_known_repo(owner, repo)
    with _resolved_platform():
        run = queries.get_run(owner, repo, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="No such triage run")
    policy = resolve_policy(load_config().triage, _public_owner(owner), repo)
    return TriageRunDetail(
        run=run.as_dict(),
        public_explanation=public_explanation(run),
        admin_explanation=admin_explanation(run),
        policy=policy.as_dict(),
    )


@router.get("/api/triage/config", response_model=TriageConfigResponse)
def get_triage_config(request: Request, owner: str = "", repo: str = "") -> TriageConfigResponse:
    """The stored override, and the policy that actually applies."""
    _require_admin(request)
    _validate_repo_filters(owner, repo)
    from mira.dashboard.api import _app_db

    stored = (_app_db.get_global_review_overrides() or {}).get(_SECTION, {}) if _app_db else {}
    config = load_config()
    inherited = load_config(use_db_overrides=False)
    # `_public_owner`, because a non-GitHub repository reaches these routes
    # under the namespaced owner `IndexStore.open` uses (`_gitlab/acme`), while
    # `triage.organizations` and `triage.repositories` are keyed on the plain
    # one. Resolving with the namespaced spelling would silently report the
    # global policy as the effective one.
    policy = resolve_policy(config.triage, _public_owner(owner), repo)
    return TriageConfigResponse(
        config=config.triage.model_dump(),
        overrides=stored,
        effective=policy.as_dict(),
        inherited=inherited.triage.model_dump(),
    )


@router.put("/api/triage/config")
def set_triage_config(body: TriageConfigUpdate, request: Request) -> dict:
    """Replace the triage section of the admin override blob.

    Validated against the real model before anything is written, so a
    malformed opt-out entry or an out-of-range weight fails the request rather
    than the next pull request.

    Only the ``triage`` section is touched: the write goes through the
    one-statement section update rather than a read-modify-write, so two panels
    editing the one settings blob cannot clobber each other.
    """
    _require_admin(request)
    from pydantic import ValidationError

    try:
        TriageConfig.model_validate(body.triage)
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
            status_code=400, detail={"message": f"Invalid triage settings: {exc}"}
        ) from exc

    from mira.dashboard.api import _app_db

    if _app_db is None:  # pragma: no cover - only unconfigured installs
        raise HTTPException(status_code=503, detail="No settings store is configured")

    actor = _actor(request) or "an admin"
    # Read the previous value before writing, so the audit entry records what
    # was replaced rather than what replaced it.
    previous = (_app_db.get_global_review_overrides() or {}).get(_SECTION, {})
    _app_db.update_global_review_overrides_section(_SECTION, body.triage or None)
    _app_db.record_config_audit(
        section=_SECTION,
        actor=actor,
        previous=previous,
        new=body.triage or {},
        action="update" if body.triage else "clear",
    )
    logger.info("Triage policy updated by %s", actor)
    return {"ok": True, "triage": body.triage}


@router.get("/api/triage/config/audit", response_model=TriageAuditPage)
def triage_config_audit(request: Request, limit: int = 50, offset: int = 0) -> TriageAuditPage:
    """Who changed the policy, when, from what and to what.

    Separate from the policy itself and append-only, because the blob only
    carries the current value: an opt-out removed for an afternoon and put back
    leaves no trace in it, and that is precisely the change somebody comes
    looking for.
    """
    _require_admin(request)
    from mira.dashboard.api import _app_db

    if _app_db is None:  # pragma: no cover - only unconfigured installs
        return TriageAuditPage(entries=[], limit=limit, offset=offset)
    limit, offset = _page(limit, offset)
    entries = _app_db.list_config_audit(section=_SECTION, limit=limit, offset=offset)
    return TriageAuditPage(entries=entries, limit=limit, offset=offset)
