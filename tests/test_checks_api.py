"""Phase 6 — the dashboard surface: authorization, validation and the audit trail.

`test_admin_authz.py` already asserts that every check route rejects a
non-admin, and `test_csrf.py`-style origin coverage is provided by the shared
middleware every mutating route passes through. What is left, and what lives
here, is the part specific to the checks:

* policy edits validated against the real model before anything is stored, so a
  bad analyser name or an unparseable glob fails the request rather than the
  next pull request;
* an audit entry recording who changed the policy, from what and to what,
  because the settings blob only ever holds the current value;
* filters and history that answer the two questions this phase is for — "which
  check is noisy" and "which check could not run".
"""

from __future__ import annotations

import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from mira.checks.models import (
    CheckFinding,
    CheckResult,
    CheckRun,
    CheckRunInputs,
    Evidence,
    SkipReason,
    result_key,
    run_key,
)
from mira.config import ChecksConfig, MiraConfig
from mira.dashboard.routers import checks as check_routes
from mira.index.store import IndexStore


@pytest.fixture(autouse=True)
def isolated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MIRA_INDEX_DIR", str(tmp_path))
    monkeypatch.delenv("DATABASE_URL", raising=False)


def _request(username: str = "admin", is_admin: bool = True) -> SimpleNamespace:
    user = SimpleNamespace(id=1, username=username, is_admin=is_admin)
    return SimpleNamespace(state=SimpleNamespace(user=user))


class _Registry:
    """Stand-in for the dashboard database: repos, settings, audit."""

    def __init__(self) -> None:
        self.overrides: dict = {}
        self.audit: list[dict] = []

    def get_repo_any_platform(self, owner, repo):
        return [SimpleNamespace(platform="github")]

    def get_global_review_overrides(self):
        return dict(self.overrides)

    def update_global_review_overrides_section(self, section, value):
        if value is None:
            self.overrides.pop(section, None)
        else:
            self.overrides[section] = value
        return dict(self.overrides)

    def record_config_audit(self, *, section, actor, previous, new, action="update"):
        self.audit.append(
            {
                "id": len(self.audit) + 1,
                "section": section,
                "actor": actor,
                "action": action,
                "previous": previous or {},
                "new": new or {},
                "created_at": time.time(),
            }
        )

    def list_config_audit(self, *, section="", limit=50, offset=0):
        rows = [row for row in self.audit if not section or row["section"] == section]
        return list(reversed(rows))[offset : offset + limit]


@pytest.fixture
def registry(monkeypatch: pytest.MonkeyPatch) -> _Registry:
    store = _Registry()
    import mira.dashboard.api as api

    monkeypatch.setattr(api, "_app_db", store)
    monkeypatch.setattr(check_routes.history, "platform_for", lambda owner, repo: "github")
    return store


def _seed_run(state: str = "violation", *, head_sha: str = "head123", mode: str = "error"):
    store = IndexStore.open("acme", "app")
    try:
        inputs = CheckRunInputs(
            platform="github",
            owner="acme",
            repo="app",
            pr_number=7,
            pr_url="https://github.com/acme/app/pull/7",
            pr_author="alice",
            head_sha=head_sha,
            changed_paths=["src/a.py"],
            changed_files=1,
        )
        key = run_key(
            platform="github",
            owner="acme",
            repo="app",
            pr_number=7,
            head_sha=head_sha,
            policy_version="checks-v1+abc",
            inputs_digest=inputs.digest,
        )
        result = CheckResult(
            check_id="native.tests",
            title="Tests",
            origin="native",
            mode=mode,
            state=state,
            summary="source changed with no test",
            evidence=[Evidence(path="src/a.py", detail="4 added lines", source="diff")],
            findings=(
                [
                    CheckFinding(
                        fingerprint="fp1",
                        title="Source changed and no test changed with it",
                        evidence=[Evidence(path="src/a.py", start_line=3, source="diff")],
                        sources=["native.tests"],
                    )
                ]
                if state == "violation"
                else []
            ),
            skip_reason=SkipReason.TOOL_MISSING if state == "skipped" else "",
            error="network down" if state == "infrastructure_error" else "",
            duration_seconds=0.25,
            config_digest="cfg1",
            result_key=result_key(run_key_value=key, check_id="native.tests"),
            sources=["native.tests"],
        )
        run = CheckRun(
            run_key=key,
            policy_version="checks-v1+abc",
            inputs=inputs,
            results=[result],
            duration_seconds=0.5,
            created_at=time.time(),
        )
        stored, _ = store.record_check_run(run)
        return stored
    finally:
        store.close()


# ────────────────────────────────────────────────────────── reading a run ──


def test_a_run_lists_with_its_results_duration_and_origin(registry: _Registry) -> None:
    _seed_run()
    page = check_routes.list_check_runs(_request(), owner="acme", repo="app", with_results=True)
    assert page.total == 1
    run = page.runs[0]
    assert run["verdict"] == "violation"
    result = run["results"][0]
    assert result["origin"] == "native"
    assert result["duration_seconds"] == pytest.approx(0.25)
    assert result["evidence"][0]["path"] == "src/a.py"


def test_a_run_detail_separates_what_it_found_from_what_it_could_not_answer(
    registry: _Registry, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(check_routes, "load_config", lambda: MiraConfig())
    run = _seed_run("infrastructure_error")
    detail = check_routes.check_run_detail(_request(), "acme", "app", run.id)
    assert "What Mira could not answer" in detail.public_explanation
    assert "What the checks found" not in detail.public_explanation
    assert "native.tests" in detail.admin_explanation
    assert detail.policy["version"]


def test_an_unknown_run_is_a_404(registry: _Registry, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(check_routes, "load_config", lambda: MiraConfig())
    with pytest.raises(HTTPException) as exc:
        check_routes.check_run_detail(_request(), "acme", "app", 999)
    assert exc.value.status_code == 404


def test_a_traversing_repository_identifier_is_refused(registry: _Registry) -> None:
    with pytest.raises(HTTPException) as exc:
        check_routes.check_run_detail(_request(), "..", "app", 1)
    assert exc.value.status_code == 400


def test_results_filter_by_state(registry: _Registry) -> None:
    _seed_run("violation", head_sha="a")
    _seed_run("pass", head_sha="b")
    violations = check_routes.list_check_results(
        _request(), owner="acme", repo="app", state="violation"
    )
    assert violations.total == 1
    assert violations.results[0]["state"] == "violation"


def test_results_filter_by_incompleteness(registry: _Registry) -> None:
    """The filter an operator reaches for after an incident."""
    _seed_run("violation", head_sha="a")
    _seed_run("skipped", head_sha="b")
    _seed_run("infrastructure_error", head_sha="c")
    page = check_routes.list_check_results(_request(), owner="acme", repo="app", incomplete=True)
    assert page.total == 2
    assert {row["state"] for row in page.results} == {"skipped", "infrastructure_error"}


def test_an_unknown_state_filter_is_refused_rather_than_ignored(registry: _Registry) -> None:
    with pytest.raises(HTTPException) as exc:
        check_routes.list_check_results(_request(), state="broken")
    assert exc.value.status_code == 400


def test_an_unknown_sort_column_is_refused(registry: _Registry) -> None:
    with pytest.raises(HTTPException) as exc:
        check_routes.list_check_runs(_request(), sort="1; DROP TABLE check_runs")
    assert exc.value.status_code == 400


def test_the_summary_counts_the_inconclusive_separately(registry: _Registry) -> None:
    """The framework's own health number, and no violation count would show it."""
    _seed_run("violation", head_sha="a")
    _seed_run("infrastructure_error", head_sha="b")
    _seed_run("timeout", head_sha="c")
    summary = check_routes.checks_summary(_request(), owner="acme", repo="app")
    assert summary.totals["violation"] == 1
    assert summary.totals["inconclusive"] == 2
    assert summary.totals["total"] == 3


def test_the_catalog_answers_the_coverage_question(
    registry: _Registry, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        check_routes,
        "load_config",
        lambda: MiraConfig(checks=ChecksConfig(enabled=True, modes={"native.tests": "error"})),
    )
    catalog = check_routes.checks_catalog(_request(), owner="acme", repo="app")
    ids = {entry["check_id"]: entry for entry in catalog.checks}
    assert ids["native.tests"]["mode"] == "error"
    assert ids["native.docs"]["mode"] == "warning"
    assert all(entry["version"] for entry in catalog.checks)


# ──────────────────────────────────────────────────────────── the policy ──


def test_a_valid_policy_is_stored(registry: _Registry, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(check_routes, "load_config", lambda: MiraConfig())
    body = check_routes.ChecksConfigUpdate(
        checks={"enabled": True, "default_mode": "warning", "modes": {"native.tests": "error"}}
    )
    result = check_routes.set_checks_config(body, _request("release-manager"))
    assert result["ok"] is True
    assert registry.overrides["checks"]["modes"]["native.tests"] == "error"


def test_an_analyser_outside_the_allowlist_fails_the_request(
    registry: _Registry, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Not the next pull request."""
    monkeypatch.setattr(check_routes, "load_config", lambda: MiraConfig())
    body = check_routes.ChecksConfigUpdate(checks={"tools": [{"name": "curl"}]})
    with pytest.raises(HTTPException) as exc:
        check_routes.set_checks_config(body, _request())
    assert exc.value.status_code == 400
    assert "tools" in str(exc.value.detail)
    assert "checks" not in registry.overrides


def test_an_unparseable_glob_fails_the_request(
    registry: _Registry, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(check_routes, "load_config", lambda: MiraConfig())
    body = check_routes.ChecksConfigUpdate(
        checks={"natural_language": [{"id": "r", "instruction": "x", "paths": ["src/**/["]}]}
    )
    with pytest.raises(HTTPException) as exc:
        check_routes.set_checks_config(body, _request())
    assert exc.value.status_code == 400


def test_saving_the_check_panel_leaves_a_sibling_section_alone(
    registry: _Registry, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(check_routes, "load_config", lambda: MiraConfig())
    registry.overrides["gate"] = {"mode": "shadow"}
    check_routes.set_checks_config(
        check_routes.ChecksConfigUpdate(checks={"enabled": True}), _request()
    )
    assert registry.overrides["gate"] == {"mode": "shadow"}


def test_the_effective_policy_is_returned_next_to_what_the_admin_typed(
    registry: _Registry, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        check_routes, "load_config", lambda: MiraConfig(checks=ChecksConfig(enabled=True))
    )
    registry.overrides["checks"] = {"enabled": True}
    response = check_routes.get_checks_config(_request(), owner="acme", repo="app")
    assert response.overrides == {"enabled": True}
    assert response.effective["enabled"] is True
    assert response.effective["version"].startswith("checks-v1+")


# ──────────────────────────────────────────────────────────────── the audit ──


def test_a_policy_change_is_recorded_with_who_what_and_from_what(
    registry: _Registry, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(check_routes, "load_config", lambda: MiraConfig())
    check_routes.set_checks_config(
        check_routes.ChecksConfigUpdate(checks={"enabled": True, "default_mode": "warning"}),
        _request("alice"),
    )
    check_routes.set_checks_config(
        check_routes.ChecksConfigUpdate(checks={"enabled": True, "default_mode": "error"}),
        _request("bob"),
    )

    page = check_routes.checks_config_audit(_request())
    assert [entry["actor"] for entry in page.entries] == ["bob", "alice"]
    latest = page.entries[0]
    assert latest["previous"]["default_mode"] == "warning"
    assert latest["new"]["default_mode"] == "error"


def test_a_policy_loosened_and_tightened_again_leaves_a_trace(
    registry: _Registry, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The blob only holds the current value; this is the whole point."""
    monkeypatch.setattr(check_routes, "load_config", lambda: MiraConfig())
    for mode, actor in (("error", "alice"), ("off", "bob"), ("error", "alice")):
        check_routes.set_checks_config(
            check_routes.ChecksConfigUpdate(checks={"modes": {"native.tests": mode}}),
            _request(actor),
        )
    page = check_routes.checks_config_audit(_request())
    assert [entry["new"]["modes"]["native.tests"] for entry in page.entries] == [
        "error",
        "off",
        "error",
    ]
    # And the current value alone would say nothing happened.
    assert registry.overrides["checks"]["modes"]["native.tests"] == "error"


def test_a_failed_edit_is_not_audited(registry: _Registry, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(check_routes, "load_config", lambda: MiraConfig())
    with pytest.raises(HTTPException):
        check_routes.set_checks_config(
            check_routes.ChecksConfigUpdate(checks={"tools": [{"name": "curl"}]}), _request()
        )
    assert registry.audit == []


# ─────────────────────────────────────────────────────────────────── CSRF ──


def test_a_session_cookie_alone_cannot_change_the_policy_from_another_site(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The origin check, exercised through the real middleware.

    A policy edit is the most valuable thing a check-framework endpoint does:
    turning a blocking check off is one request, and a cookie that a browser
    attaches automatically must not be enough to make it from somewhere else.
    """
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from mira.dashboard.auth import AuthMiddleware
    from mira.dashboard.db import AppDatabase

    monkeypatch.setenv("MIRA_INDEX_DIR", str(tmp_path))
    app = FastAPI()
    db = AppDatabase(url="", admin_password="pw")
    app.state.auth_db = db
    app.add_middleware(AuthMiddleware, db=db)

    @app.put("/api/checks/config")
    def _config() -> dict:  # pragma: no cover - only reached on the allowed path
        return {"ok": True}

    client = TestClient(app)
    admin = db.authenticate("admin", "pw")
    client.cookies.set("mira_session", db.create_session(admin.id))

    payload = {"checks": {"enabled": False}}
    cross_site = client.put(
        "/api/checks/config", json=payload, headers={"Origin": "https://attacker.example"}
    )
    assert cross_site.status_code == 403

    same_site = client.put(
        "/api/checks/config", json=payload, headers={"Origin": "http://testserver"}
    )
    assert same_site.status_code == 200


def test_the_real_settings_store_records_and_reads_the_audit_trail(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The stub above stands in for this; here is the thing it stands in for."""
    from mira.dashboard.db import AppDatabase

    monkeypatch.setenv("MIRA_INDEX_DIR", str(tmp_path))
    db = AppDatabase(url="", admin_password="pw")
    try:
        db.record_config_audit(
            section="checks",
            actor="alice",
            previous={"default_mode": "warning"},
            new={"default_mode": "error"},
        )
        db.record_config_audit(section="gate", actor="bob", previous={}, new={"mode": "shadow"})

        entries = db.list_config_audit(section="checks")
        assert len(entries) == 1
        assert entries[0]["actor"] == "alice"
        assert entries[0]["previous"] == {"default_mode": "warning"}
        assert entries[0]["new"] == {"default_mode": "error"}
        assert len(db.list_config_audit()) == 2
    finally:
        db.close()


def test_checks_is_a_writable_settings_section() -> None:
    """A section the panel writes has to be in the closed set the store allows."""
    from mira.dashboard.db import AppDatabase

    assert "checks" in AppDatabase._OVERRIDE_SECTIONS
