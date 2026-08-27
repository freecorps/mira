"""Dashboard routes for the Phase 4 merge gate.

Three permissions live here, and keeping them apart is the point rather than an
implementation detail:

* **Reading a decision** is admin-only. A gate decision quotes PR authors,
  protected paths and CI failures across every repository in the install; that
  is governance data, not per-repo browsing data.
* **Editing the policy** is admin-only, and is the only way policy changes.
  Nothing that arrives from a pull request can reach these endpoints.
* **Overriding a decision** is a *separate* capability layered on top of admin,
  gated by ``gate.override_admins``. "Can administer Mira" and "can move a
  merge decision" are different jobs in most teams, and an install that wants
  them to be the same person simply leaves the list empty.

Every mutating route also passes through the dashboard's origin check, so a
session cookie alone is not enough to move a decision from another site.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from fastapi import HTTPException, Request
from pydantic import BaseModel, Field

from mira.config import GateConfig, load_config
from mira.dashboard.api import _require_admin, router
from mira.gate import history
from mira.gate.explain import admin_explanation, public_explanation, would_have_approved
from mira.gate.history import PlatformResolutionError
from mira.gate.models import GATE_MODES, GATE_STATES
from mira.gate.policy import resolve_policy
from mira.gate.service import OverrideDenied, apply_override

logger = logging.getLogger(__name__)

# Page sizes are capped rather than trusted: the same Orange Pi profile that
# made the history paginate is the one serving this endpoint.
_MAX_PAGE = 200

# Owner/repo arrive from the URL and end up in a filesystem path on the SQLite
# backend, so they are validated as single safe segments — an admin session is
# not a filesystem primitive.
_UNSAFE_SEGMENT = re.compile(r"[/\x00]")
_NAMESPACED_OWNER = re.compile(r"^_(?:github|gitlab|forgejo)/(?P<owner>[^/\x00]+)$")

_SORT_KEYS = {"created_at", "risk_score", "pr_number", "state"}


class GateDecisionPage(BaseModel):
    decisions: list[dict]
    total: int
    limit: int
    offset: int


class GateSummaryResponse(BaseModel):
    buckets: list[dict]
    totals: dict


class GateDecisionDetail(BaseModel):
    decision: dict
    public_explanation: str
    admin_explanation: str
    overrides: list[dict]
    policy: dict


class GateConfigResponse(BaseModel):
    config: dict
    overrides: dict
    effective: dict


class GateConfigUpdate(BaseModel):
    """The gate section of the admin override blob, replaced wholesale.

    Wholesale rather than patched so that clearing a list is expressible: a
    merge would make ``blocked_labels: []`` indistinguishable from "leave it
    alone", and silently keeping an old blocklist is the wrong way to be wrong.
    """

    gate: dict[str, Any] = Field(default_factory=dict)


class GateOverrideInput(BaseModel):
    new_state: str
    reason: str
    # Lets a caller record a genuinely repeated action (revoke → approve →
    # revoke) instead of collapsing onto the first one. Absent, a retried
    # request is idempotent, which is what a flaky network needs.
    nonce: str = ""


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


def _require_gate_override(request: Request, owner: str, repo: str) -> str:
    """Admin, *plus* membership of the gate's override list when one is set.

    Checked before anything is read or written, and it returns the actor rather
    than a bool so a caller cannot forget to record who acted.
    """
    _require_admin(request)
    actor = _actor(request)
    policy = resolve_policy(load_config().gate, owner, repo)
    if not policy.allow_overrides:
        raise HTTPException(status_code=403, detail="Gate overrides are disabled")
    allowed = {name.strip().lower() for name in policy.override_admins if name.strip()}
    if allowed and actor.lower() not in allowed:
        raise HTTPException(
            status_code=403,
            detail="This account is not permitted to override merge gate decisions",
        )
    if not actor:
        # An override with no attributable actor is not an audit record.
        raise HTTPException(status_code=403, detail="Overrides require an identified account")
    return actor


def _filters(
    owner: str,
    repo: str,
    platform: str,
    state: str,
    mode: str,
    pr_number: int,
    pr_author: str,
    risk_band: str,
    since: float,
    until: float,
) -> dict:
    if state and state not in GATE_STATES:
        raise HTTPException(status_code=400, detail=f"state must be one of {sorted(GATE_STATES)}")
    if mode and mode not in GATE_MODES:
        raise HTTPException(status_code=400, detail=f"mode must be one of {sorted(GATE_MODES)}")
    return {
        "owner": owner,
        "repo": repo,
        "platform": platform,
        "state": state,
        "mode": mode,
        "pr_number": pr_number,
        "pr_author": pr_author,
        "risk_band": risk_band,
        "since": since,
        "until": until,
    }


@router.get("/api/gate/decisions", response_model=GateDecisionPage)
def list_gate_decisions(
    request: Request,
    owner: str = "",
    repo: str = "",
    platform: str = "",
    state: str = "",
    mode: str = "",
    pr_number: int = 0,
    pr_author: str = "",
    risk_band: str = "",
    since: float = 0.0,
    until: float = 0.0,
    sort: str = "created_at",
    order: str = "desc",
    limit: int = 50,
    offset: int = 0,
) -> GateDecisionPage:
    """Every recorded decision, filtered and paginated."""
    _require_admin(request)
    if sort not in _SORT_KEYS:
        raise HTTPException(status_code=400, detail=f"sort must be one of {sorted(_SORT_KEYS)}")
    _validate_repo_filters(owner, repo)
    limit, offset = _page(limit, offset)
    with _resolved_platform():
        rows, total = history.list_decisions(
            filters=_filters(
                owner, repo, platform, state, mode, pr_number, pr_author, risk_band, since, until
            ),
            limit=limit,
            offset=offset,
            sort=sort,
            descending=order != "asc",
        )
    return GateDecisionPage(
        decisions=[row.as_dict() for row in rows], total=total, limit=limit, offset=offset
    )


@router.get("/api/gate/summary", response_model=GateSummaryResponse)
def gate_summary(
    request: Request,
    owner: str = "",
    repo: str = "",
    platform: str = "",
    since: float = 0.0,
    until: float = 0.0,
) -> GateSummaryResponse:
    """State counts, and the two numbers a shadow rollout exists to produce.

    ``would_approve`` is the candidate-approval count: how often the gate would
    have put its name on a merge. ``approved`` is how often it actually did.
    Comparing the first against what happened to those pull requests is the
    false-approval measurement, and it is only meaningful because a dry run
    records the same decision it would have acted on.
    """
    _require_admin(request)
    _validate_repo_filters(owner, repo)
    with _resolved_platform():
        buckets = history.summarize(
            filters=_filters(owner, repo, platform, "", "", 0, "", "", since, until)
        )
    totals: dict[str, Any] = dict.fromkeys(GATE_STATES, 0)
    total = 0
    for bucket in buckets:
        totals[bucket["state"]] = totals.get(bucket["state"], 0) + bucket["count"]
        total += bucket["count"]
    totals["total"] = total
    totals["candidate_approvals"] = totals.get("would_approve", 0) + totals.get("approved", 0)
    return GateSummaryResponse(buckets=buckets, totals=totals)


@router.get("/api/gate/decisions/{owner}/{repo}/{decision_id}", response_model=GateDecisionDetail)
def gate_decision_detail(
    request: Request, owner: str, repo: str, decision_id: int
) -> GateDecisionDetail:
    """One decision: what it decided, why, and everything done to it since."""
    _require_admin(request)
    _require_known_repo(owner, repo)
    with _resolved_platform():
        decision = history.get_decision(owner, repo, decision_id)
        if decision is None:
            raise HTTPException(status_code=404, detail="No such gate decision")
        overrides = history.list_overrides(owner, repo, decision_id=decision_id)
    policy = resolve_policy(load_config().gate, _public_owner(owner), repo)
    payload = decision.as_dict()
    payload["would_have_approved"] = would_have_approved(decision)
    return GateDecisionDetail(
        decision=payload,
        public_explanation=public_explanation(decision),
        admin_explanation=admin_explanation(decision),
        overrides=overrides,
        policy=policy.as_dict(),
    )


@router.post("/api/gate/decisions/{owner}/{repo}/{decision_id}/override")
def override_gate_decision(
    owner: str, repo: str, decision_id: int, body: GateOverrideInput, request: Request
) -> dict:
    """Move a decision by hand, recording who, why, from what and to what.

    Authorization is checked *first*, before the decision is even read: an
    account that may not override should not be able to use this endpoint to
    discover whether a decision exists.
    """
    # Authorization strictly first: a repository lookup that 404s before the
    # permission check turns this endpoint into an existence oracle for anyone
    # with a session.
    actor = _require_gate_override(request, _public_owner(owner), repo)
    _require_known_repo(owner, repo)
    with _resolved_platform():
        platform = history.platform_for(owner, repo)
        try:
            result = apply_override(
                owner=owner,
                repo=repo,
                platform=platform,
                decision_id=decision_id,
                actor=actor,
                reason=body.reason,
                new_state=body.new_state,
                nonce=body.nonce,
            )
        except OverrideDenied as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
    logger.info(
        "Gate decision %s in %s/%s overridden to %s by %s",
        decision_id,
        owner,
        repo,
        body.new_state,
        actor,
    )
    return {
        "ok": True,
        "created": result.created,
        "decision": result.decision.as_dict(),
        "override": result.override,
    }


@router.get("/api/gate/config", response_model=GateConfigResponse)
def get_gate_config(request: Request, owner: str = "", repo: str = "") -> GateConfigResponse:
    """The stored gate override, and the policy that actually applies.

    Both, because they answer different questions: the override is what an
    admin typed, the effective policy is what a pull request will meet — and on
    an install with a `mira.yaml` and per-repository entries those are rarely
    the same document.
    """
    _require_admin(request)
    from mira.dashboard.api import _app_db

    stored = (_app_db.get_global_review_overrides() or {}).get("gate", {}) if _app_db else {}
    config = load_config()
    policy = resolve_policy(config.gate, owner, repo)
    return GateConfigResponse(
        config=config.gate.model_dump(),
        overrides=stored,
        effective=policy.as_dict(),
    )


@router.put("/api/gate/config")
def set_gate_config(body: GateConfigUpdate, request: Request) -> dict:
    """Replace the gate section of the admin override blob.

    Validated against the real model before anything is written, so a typo
    fails the request rather than the next pull request. Only the ``gate``
    section is touched — the review and filter overrides an admin set on the
    settings page are read back and rewritten unchanged, so two panels editing
    one blob cannot clobber each other.
    """
    _require_admin(request)
    from pydantic import ValidationError

    try:
        GateConfig.model_validate(body.gate)
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
            status_code=400, detail={"message": f"Invalid gate settings: {exc}"}
        ) from exc

    from mira.dashboard.api import _app_db

    if _app_db is None:  # pragma: no cover - only unconfigured installs
        raise HTTPException(status_code=503, detail="No settings store is configured")
    # One statement rather than read-modify-write: the autofix panel edits a
    # sibling key in the same JSON row, and a save that read the blob before
    # that panel wrote would carry its old section back over the new one.
    _app_db.update_global_review_overrides_section("gate", body.gate or None)
    logger.info("Gate policy updated by %s", _actor(request) or "an admin")
    return {"ok": True, "gate": body.gate}
