"""Phase 4 — the dashboard surface: authorization, CSRF, and the audit trail.

`test_admin_authz.py` already asserts that every gate route rejects a non-admin.
What is left, and what lives here, is the part that is specific to the gate:

* a *separate* override permission layered on top of admin, so administering
  Mira and moving a merge decision are not the same power;
* the origin check, because a session cookie alone must not let another site
  approve a pull request;
* policy edits that are validated before they are stored, and that cannot be
  reached from anything a pull request contains.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from mira.config import GateConfig, MiraConfig
from mira.dashboard.routers import gate as gate_routes
from mira.gate.models import CIState, GateDecision, GateInputs, Reason, RiskFactor
from mira.index.store import IndexStore


@pytest.fixture(autouse=True)
def isolated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MIRA_INDEX_DIR", str(tmp_path))
    monkeypatch.delenv("DATABASE_URL", raising=False)


def _section_writer(stored: dict):
    """Stand-in for `AppDatabase.update_global_review_overrides_section`.

    The real one replaces a section in one statement so two panels saving at
    once cannot carry each other's old section back; the stub only has to have
    the same observable effect.
    """

    def _update(section: str, value: dict | None) -> dict:
        if value is None:
            stored.pop(section, None)
        else:
            stored[section] = value
        return dict(stored)

    return _update


def _request(username: str = "admin", is_admin: bool = True) -> SimpleNamespace:
    user = SimpleNamespace(id=1, username=username, is_admin=is_admin)
    return SimpleNamespace(state=SimpleNamespace(user=user))


def _seed_decision(state: str = "would_approve", **overrides) -> GateDecision:
    store = IndexStore.open("acme", "app")
    try:
        inputs = GateInputs(
            platform="github",
            owner="acme",
            repo="app",
            pr_number=7,
            pr_url="https://github.com/acme/app/pull/7",
            pr_author="alice",
            base_branch="main",
            head_sha="head123",
            ci=CIState(state="success", total=1),
            **overrides,
        )
        decision = GateDecision(
            decision_key="key-1",
            state=state,
            mode="shadow",
            risk_score=8,
            risk_band="low",
            policy_version="gate-v1+abc",
            inputs=inputs,
            reasons=[Reason("eligible", "Eligible with risk score 8", "info")],
            factors=[RiskFactor("size_files", "Files changed", 2, "2 files")],
        )
        stored, _ = store.record_gate_decision(decision)
        return stored
    finally:
        store.close()


@pytest.fixture
def known_repo(monkeypatch: pytest.MonkeyPatch) -> None:
    """The repo registry says acme/app exists on GitHub."""
    registry = SimpleNamespace(
        get_repo_any_platform=lambda owner, repo: [SimpleNamespace(platform="github")],
        get_global_review_overrides=dict,
        set_global_review_overrides=lambda overrides: None,
        update_global_review_overrides_section=lambda section, value: {},
    )
    import mira.dashboard.api as api

    monkeypatch.setattr(api, "_app_db", registry)
    monkeypatch.setattr(gate_routes.history, "platform_for", lambda owner, repo: "github")


# ─────────────────────────────────────────────────── the override permission ──


def test_an_admin_not_on_the_override_list_cannot_move_a_decision(
    known_repo: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        gate_routes,
        "load_config",
        lambda: MiraConfig(gate=GateConfig(mode="shadow", override_admins=["release-manager"])),
    )
    decision = _seed_decision()
    with pytest.raises(HTTPException) as exc:
        gate_routes.override_gate_decision(
            owner="acme",
            repo="app",
            decision_id=decision.id,
            body=gate_routes.GateOverrideInput(new_state="not_approved", reason="because"),
            request=_request("someone-else"),
        )
    assert exc.value.status_code == 403
    assert "not permitted" in str(exc.value.detail)


def test_an_admin_on_the_override_list_can_move_a_decision(
    known_repo: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        gate_routes,
        "load_config",
        lambda: MiraConfig(gate=GateConfig(mode="shadow", override_admins=["release-manager"])),
    )
    decision = _seed_decision()
    result = gate_routes.override_gate_decision(
        owner="acme",
        repo="app",
        decision_id=decision.id,
        body=gate_routes.GateOverrideInput(
            new_state="not_approved", reason="held for the release train"
        ),
        request=_request("release-manager"),
    )
    assert result["ok"] is True
    assert result["decision"]["state"] == "not_approved"
    assert result["override"]["actor"] == "release-manager"
    assert result["override"]["previous_state"] == "would_approve"


def test_an_empty_override_list_means_every_admin(
    known_repo: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        gate_routes, "load_config", lambda: MiraConfig(gate=GateConfig(mode="shadow"))
    )
    decision = _seed_decision()
    result = gate_routes.override_gate_decision(
        owner="acme",
        repo="app",
        decision_id=decision.id,
        body=gate_routes.GateOverrideInput(new_state="not_approved", reason="r"),
        request=_request("admin"),
    )
    assert result["ok"] is True


def test_overrides_can_be_switched_off_entirely(
    known_repo: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        gate_routes,
        "load_config",
        lambda: MiraConfig(gate=GateConfig(mode="shadow", allow_overrides=False)),
    )
    decision = _seed_decision()
    with pytest.raises(HTTPException) as exc:
        gate_routes.override_gate_decision(
            owner="acme",
            repo="app",
            decision_id=decision.id,
            body=gate_routes.GateOverrideInput(new_state="not_approved", reason="r"),
            request=_request(),
        )
    assert exc.value.status_code == 403


def test_authorization_is_checked_before_the_repository_is_looked_up(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Otherwise the endpoint is an existence oracle for anyone with a session."""
    monkeypatch.setattr(
        gate_routes,
        "load_config",
        lambda: MiraConfig(gate=GateConfig(mode="shadow", override_admins=["only-me"])),
    )
    with pytest.raises(HTTPException) as exc:
        gate_routes.override_gate_decision(
            owner="does-not-exist",
            repo="nope",
            decision_id=1,
            body=gate_routes.GateOverrideInput(new_state="not_approved", reason="r"),
            request=_request("intruder"),
        )
    assert exc.value.status_code == 403


def test_forcing_an_approval_needs_its_own_opt_in(
    known_repo: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        gate_routes, "load_config", lambda: MiraConfig(gate=GateConfig(mode="shadow"))
    )
    decision = _seed_decision()
    with pytest.raises(HTTPException) as exc:
        gate_routes.override_gate_decision(
            owner="acme",
            repo="app",
            decision_id=decision.id,
            body=gate_routes.GateOverrideInput(new_state="approved", reason="ship it"),
            request=_request(),
        )
    assert exc.value.status_code == 400
    assert "allow_approval_override" in str(exc.value.detail)


def test_an_override_must_carry_a_reason(known_repo: None, monkeypatch) -> None:
    monkeypatch.setattr(
        gate_routes, "load_config", lambda: MiraConfig(gate=GateConfig(mode="shadow"))
    )
    decision = _seed_decision()
    with pytest.raises(HTTPException) as exc:
        gate_routes.override_gate_decision(
            owner="acme",
            repo="app",
            decision_id=decision.id,
            body=gate_routes.GateOverrideInput(new_state="not_approved", reason=""),
            request=_request(),
        )
    assert exc.value.status_code == 400


def test_an_unknown_state_is_rejected(known_repo: None, monkeypatch) -> None:
    monkeypatch.setattr(
        gate_routes, "load_config", lambda: MiraConfig(gate=GateConfig(mode="shadow"))
    )
    decision = _seed_decision()
    with pytest.raises(HTTPException) as exc:
        gate_routes.override_gate_decision(
            owner="acme",
            repo="app",
            decision_id=decision.id,
            body=gate_routes.GateOverrideInput(new_state="skipped", reason="r"),
            request=_request(),
        )
    assert exc.value.status_code == 400


# ───────────────────────────────────────────────────────────── reading back ──


def test_a_decision_detail_carries_both_explanations(known_repo: None) -> None:
    decision = _seed_decision()
    detail = gate_routes.gate_decision_detail(
        request=_request(), owner="acme", repo="app", decision_id=decision.id
    )
    assert detail.decision["state"] == "would_approve"
    assert detail.decision["would_have_approved"] is True
    assert "Merge gate" in detail.public_explanation
    assert "Risk factors" in detail.admin_explanation
    assert detail.policy["mode"] in {"off", "shadow", "enforce"}


def test_the_summary_counts_candidate_approvals(known_repo: None) -> None:
    _seed_decision()
    summary = gate_routes.gate_summary(request=_request())
    assert summary.totals["would_approve"] == 1
    assert summary.totals["candidate_approvals"] == 1
    assert summary.totals["total"] == 1


def test_a_missing_decision_is_a_404(known_repo: None) -> None:
    with pytest.raises(HTTPException) as exc:
        gate_routes.gate_decision_detail(
            request=_request(), owner="acme", repo="app", decision_id=999
        )
    assert exc.value.status_code == 404


def test_repository_identifiers_cannot_traverse(known_repo: None) -> None:
    for owner, repo in (("..", "app"), ("acme", ".."), ("a/b", "app")):
        with pytest.raises(HTTPException) as exc:
            gate_routes.gate_decision_detail(
                request=_request(), owner=owner, repo=repo, decision_id=1
            )
        assert exc.value.status_code == 400


def test_list_filters_reject_unknown_states(known_repo: None) -> None:
    with pytest.raises(HTTPException) as exc:
        gate_routes.list_gate_decisions(request=_request(), state="approved-ish")
    assert exc.value.status_code == 400


# ────────────────────────────────────────────────────────────── policy edits ──


def test_a_policy_edit_is_validated_before_it_is_stored(monkeypatch) -> None:
    written: dict = {}
    registry = SimpleNamespace(
        get_global_review_overrides=lambda: {"review": {"walkthrough": False}},
        set_global_review_overrides=lambda overrides: written.update(overrides),
        update_global_review_overrides_section=_section_writer(written),
    )
    import mira.dashboard.api as api

    monkeypatch.setattr(api, "_app_db", registry)

    with pytest.raises(HTTPException) as exc:
        gate_routes.set_gate_config(
            body=gate_routes.GateConfigUpdate(gate={"mode": "always"}), request=_request()
        )
    assert exc.value.status_code == 400
    assert written == {}


def test_a_policy_edit_leaves_the_other_sections_alone(monkeypatch) -> None:
    written: dict = {"review": {"walkthrough": False}}
    registry = SimpleNamespace(
        get_global_review_overrides=lambda: dict(written),
        update_global_review_overrides_section=_section_writer(written),
    )
    import mira.dashboard.api as api

    monkeypatch.setattr(api, "_app_db", registry)

    result = gate_routes.set_gate_config(
        body=gate_routes.GateConfigUpdate(gate={"mode": "shadow", "kill_switch": False}),
        request=_request(),
    )
    assert result["ok"] is True
    assert written["gate"]["mode"] == "shadow"
    assert written["review"] == {"walkthrough": False}


def test_an_unreadable_protected_path_pattern_fails_the_edit(monkeypatch) -> None:
    registry = SimpleNamespace(
        get_global_review_overrides=dict,
        set_global_review_overrides=lambda overrides: None,
        update_global_review_overrides_section=lambda section, value: {},
    )
    import mira.dashboard.api as api

    monkeypatch.setattr(api, "_app_db", registry)
    with pytest.raises(HTTPException) as exc:
        gate_routes.set_gate_config(
            body=gate_routes.GateConfigUpdate(gate={"protected_paths": ["bad["]}),
            request=_request(),
        )
    assert exc.value.status_code == 400


# ───────────────────────────────────────────────────────────────────── CSRF ──


@pytest.fixture
def csrf_app(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> FastAPI:
    """A real app with the dashboard's auth middleware in front of the routes."""
    monkeypatch.setenv("MIRA_INDEX_DIR", str(tmp_path))
    from mira.dashboard.auth import AuthMiddleware, create_auth_router
    from mira.dashboard.db import AppDatabase

    db = AppDatabase(url="", admin_password="admin")
    app = FastAPI()

    @app.post("/api/gate/decisions/acme/app/1/override")
    def _override() -> dict:  # pragma: no cover - reached only when CSRF passes
        return {"ok": True}

    app.add_middleware(AuthMiddleware, db=db)
    app.include_router(create_auth_router(db))
    return app


def _login(client: TestClient) -> None:
    resp = client.post("/api/auth/login", json={"username": "admin", "password": "admin"})
    assert resp.status_code == 200


def test_a_cross_site_override_is_refused(csrf_app: FastAPI) -> None:
    client = TestClient(csrf_app, base_url="http://testserver")
    _login(client)
    resp = client.post(
        "/api/gate/decisions/acme/app/1/override",
        json={"new_state": "approved", "reason": "r"},
        headers={"Origin": "https://evil.example"},
    )
    assert resp.status_code == 403
    assert "origin" in resp.json()["error"].lower()


def test_a_same_site_override_passes_the_origin_check(csrf_app: FastAPI) -> None:
    client = TestClient(csrf_app, base_url="http://testserver")
    _login(client)
    resp = client.post(
        "/api/gate/decisions/acme/app/1/override",
        json={"new_state": "not_approved", "reason": "r"},
        headers={"Origin": "http://testserver"},
    )
    assert resp.status_code == 200


def test_an_unauthenticated_override_is_refused(csrf_app: FastAPI) -> None:
    client = TestClient(csrf_app, base_url="http://testserver")
    resp = client.post(
        "/api/gate/decisions/acme/app/1/override",
        json={"new_state": "approved", "reason": "r"},
        headers={"Origin": "http://testserver"},
    )
    assert resp.status_code == 401


# ───────────────────────────────── regressions from the pre-merge review ──


def test_a_policy_edit_preserves_gate_keys_the_caller_resent(monkeypatch) -> None:
    """The endpoint replaces the `gate` section wholesale, and says so.

    Wholesale is what makes an empty list expressible — a merge would render
    `blocked_labels: []` indistinguishable from "leave it alone". The contract
    is therefore that the caller resends what it wants kept, and the dashboard
    panel does exactly that by spreading the loaded overrides under its form
    values. This asserts the server half: everything sent is stored, and only
    the `gate` section is touched.
    """
    # One dict standing in for the stored blob: the route replaces its `gate`
    # key without reading the rest, so the rest has to already be in there for
    # "only the gate section moved" to mean anything.
    written: dict = {
        "review": {"walkthrough": False},
        "gate": {"mode": "shadow", "repositories": {"acme/app": {"mode": "off"}}},
    }
    registry = SimpleNamespace(
        get_global_review_overrides=lambda: dict(written),
        update_global_review_overrides_section=_section_writer(written),
    )
    import mira.dashboard.api as api

    monkeypatch.setattr(api, "_app_db", registry)

    gate_routes.set_gate_config(
        body=gate_routes.GateConfigUpdate(
            gate={
                "mode": "enforce",
                "repositories": {"acme/app": {"mode": "off"}},
                "blocked_labels": [],
            }
        ),
        request=_request(),
    )
    assert written["gate"]["mode"] == "enforce"
    assert written["gate"]["repositories"] == {"acme/app": {"mode": "off"}}
    # An explicitly empty list survives as empty rather than being merged away.
    assert written["gate"]["blocked_labels"] == []
    assert written["review"] == {"walkthrough": False}


def test_an_empty_gate_edit_clears_the_section(monkeypatch) -> None:
    written: dict = {"gate": {"mode": "enforce"}, "filter": {}}
    registry = SimpleNamespace(
        get_global_review_overrides=lambda: dict(written),
        update_global_review_overrides_section=_section_writer(written),
    )
    import mira.dashboard.api as api

    monkeypatch.setattr(api, "_app_db", registry)

    gate_routes.set_gate_config(body=gate_routes.GateConfigUpdate(gate={}), request=_request())
    assert "gate" not in written
    assert "filter" in written
