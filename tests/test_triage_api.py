"""Phase 7C — the dashboard surface: validation, the audit trail, and what it shows.

`test_admin_authz.py` already asserts that every triage route rejects a
non-admin, including the read routes — a triage run is a record of people, and
the endpoint that answers "who gets suggested most" is the last one to leave
open. What is left, and what lives here, is specific to triage:

* a policy edit validated against the real model before anything is stored, so
  an unusable opt-out entry fails the request rather than silently failing to
  match a person for the next six months;
* an audit entry naming who changed it, from what, to what;
* the run detail carrying both renderings, because the public one is what the
  contributor saw and the admin one is the arithmetic behind it.
"""

from __future__ import annotations

import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from mira.dashboard.routers import triage as triage_routes
from mira.index.store import IndexStore
from mira.triage.models import (
    Classification,
    Evidence,
    Exclusion,
    ReviewerCandidate,
    SignalContribution,
    SignalReport,
    TriageInputs,
    TriageRun,
    run_key,
)


@pytest.fixture(autouse=True)
def isolated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MIRA_INDEX_DIR", str(tmp_path))
    monkeypatch.delenv("DATABASE_URL", raising=False)


def _request(username: str = "admin", is_admin: bool = True) -> SimpleNamespace:
    user = SimpleNamespace(id=1, username=username, is_admin=is_admin)
    return SimpleNamespace(state=SimpleNamespace(user=user))


class _Registry:
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
    monkeypatch.setattr(triage_routes.queries, "platform_for", lambda owner, repo: "github")
    return store


def _seed_run(*, status: str = "ok", head_sha: str = "head222") -> TriageRun:
    store = IndexStore.open("acme", "app")
    try:
        inputs = TriageInputs(
            platform="github",
            owner="acme",
            repo="app",
            pr_number=7,
            pr_url="https://github.com/acme/app/pull/7",
            pr_author="kit",
            base_sha="base111",
            head_sha=head_sha,
            ownership_ref="base111",
            changed_paths=["src/app.py"],
            changed_files=1,
        )
        candidates = (
            [
                ReviewerCandidate(
                    identity="dana",
                    score=3.0,
                    contributions=[
                        SignalContribution(
                            kind="codeowners",
                            raw=1,
                            weight=3.0,
                            score=3.0,
                            detail="owns 1 of the changed file(s)",
                            evidence=[Evidence(path="src/app.py", line=2, source="codeowners")],
                        )
                    ],
                )
            ]
            if status == "ok"
            else []
        )
        signals = [
            SignalReport(
                kind="codeowners",
                status="unavailable" if status == "unavailable" else "available",
                detail="502 from the API" if status == "unavailable" else "one owner",
                candidates=len(candidates),
            )
        ]
        run = TriageRun(
            run_key=run_key(
                platform="github",
                owner="acme",
                repo="app",
                pr_number=7,
                head_sha=head_sha,
                policy_version="triage-v1+abc",
                inputs_digest=inputs.digest,
            ),
            policy_version="triage-v1+abc",
            inputs=inputs,
            classification=Classification(
                size="s", changed_files=1, changed_lines=4, areas=["src"], kinds=["code"]
            ),
            candidates=candidates,
            signals=signals,
            excluded=[Exclusion(identity="kit", reason="author", detail="opened this")],
            created_at=time.time(),
        )
        stored, _ = store.record_triage_run(run)
        return stored
    finally:
        store.close()


# ─────────────────────────────────────────────────────────── reading runs ──


def test_a_run_lists_with_its_status_and_candidates(registry: _Registry) -> None:
    _seed_run()
    page = triage_routes.list_triage_runs(_request(), owner="acme", repo="app")
    assert page.total == 1
    row = page.runs[0]
    assert row["status"] == "ok"
    assert [c["identity"] for c in row["candidates"]] == ["dana"]
    assert row["classification"]["size"] == "s"


def test_the_degraded_filter_selects_the_runs_that_are_about_mira(
    registry: _Registry,
) -> None:
    _seed_run(status="unavailable", head_sha="headAAA")
    _seed_run(status="ok", head_sha="headBBB")
    page = triage_routes.list_triage_runs(_request(), owner="acme", repo="app", degraded=True)
    assert page.total == 1
    assert page.runs[0]["status"] == "unavailable"


def test_a_run_can_be_found_by_the_person_it_named(registry: _Registry) -> None:
    _seed_run()
    assert triage_routes.list_triage_runs(_request(), identity="@Dana").total == 1
    assert triage_routes.list_triage_runs(_request(), identity="dan").total == 0


def test_an_unknown_status_filter_is_rejected(registry: _Registry) -> None:
    with pytest.raises(HTTPException) as exc:
        triage_routes.list_triage_runs(_request(), status="probably")
    assert exc.value.status_code == 400


def test_an_unknown_sort_key_is_rejected(registry: _Registry) -> None:
    with pytest.raises(HTTPException) as exc:
        triage_routes.list_triage_runs(_request(), sort="score")
    assert exc.value.status_code == 400


def test_a_traversal_attempt_in_the_repository_name_is_rejected(
    registry: _Registry,
) -> None:
    with pytest.raises(HTTPException) as exc:
        triage_routes.list_triage_runs(_request(), owner="../../etc", repo="passwd")
    assert exc.value.status_code == 400


def test_the_detail_carries_what_the_contributor_saw_and_the_arithmetic(
    registry: _Registry,
) -> None:
    stored = _seed_run()
    detail = triage_routes.triage_run_detail(_request(), "acme", "app", stored.run_id)
    assert "Reviewer suggestions" in detail.public_explanation
    # The public rendering never mentions anybody.
    assert "@dana" not in detail.public_explanation
    assert "`dana`" in detail.public_explanation
    # The admin rendering shows the score and everyone who was dropped.
    assert "codeowners 1×3=3" in detail.admin_explanation
    assert "kit" in detail.admin_explanation
    assert detail.policy["version"].startswith("triage-v1")


def test_a_missing_run_is_a_404(registry: _Registry) -> None:
    with pytest.raises(HTTPException) as exc:
        triage_routes.triage_run_detail(_request(), "acme", "app", 999)
    assert exc.value.status_code == 404


def test_the_suggestion_summary_counts_by_identity(registry: _Registry) -> None:
    _seed_run(head_sha="headAAA")
    _seed_run(head_sha="headBBB")
    summary = triage_routes.triage_suggestions(_request(), owner="acme", repo="app")
    assert summary.totals == {"identities": 1, "suggestions": 2}
    assert summary.identities[0]["identity"] == "dana"


# ─────────────────────────────────────────────────────────────── the policy ──


def test_the_policy_endpoint_shows_the_override_and_what_applies(
    registry: _Registry,
) -> None:
    response = triage_routes.get_triage_config(_request())
    assert response.overrides == {}
    assert response.effective["enabled"] is False


def test_the_policy_endpoint_also_says_what_would_apply_without_the_override(
    registry: _Registry,
) -> None:
    """The panel has to be able to hand a field back to inheritance.

    Comparing an edit against the *resolved* policy compares each field to
    itself, so nothing ever looks changed and a stored value can never be
    removed. `inherited` is the same resolution with the database layer left
    out, which is what a field returns to when its override is deleted.
    """
    triage_routes.set_triage_config(
        triage_routes.TriageConfigUpdate(triage={"enabled": True, "max_suggestions": 9}),
        _request(),
    )
    response = triage_routes.get_triage_config(_request())
    assert response.config["max_suggestions"] == 9
    assert response.effective["enabled"] is True
    assert response.inherited["max_suggestions"] == 3
    assert response.inherited["enabled"] is False


def test_a_policy_edit_is_validated_before_it_is_stored(registry: _Registry) -> None:
    with pytest.raises(HTTPException) as exc:
        triage_routes.set_triage_config(
            triage_routes.TriageConfigUpdate(triage={"exclude": ["not a login!"]}),
            _request(),
        )
    assert exc.value.status_code == 400
    assert "exclude" in str(exc.value.detail)
    assert registry.overrides == {}


def test_an_out_of_range_weight_is_rejected(registry: _Registry) -> None:
    with pytest.raises(HTTPException) as exc:
        triage_routes.set_triage_config(
            triage_routes.TriageConfigUpdate(triage={"weights": {"codeowners": -3}}),
            _request(),
        )
    assert exc.value.status_code == 400


def test_a_policy_edit_is_stored_and_audited(registry: _Registry) -> None:
    triage_routes.set_triage_config(
        triage_routes.TriageConfigUpdate(triage={"enabled": True, "max_suggestions": 2}),
        _request(username="ada"),
    )
    assert registry.overrides["triage"]["max_suggestions"] == 2
    entry = registry.audit[-1]
    assert entry["section"] == "triage"
    assert entry["actor"] == "ada"
    assert entry["previous"] == {}
    assert entry["new"]["max_suggestions"] == 2


def test_the_audit_entry_records_what_was_replaced_not_what_replaced_it(
    registry: _Registry,
) -> None:
    triage_routes.set_triage_config(
        triage_routes.TriageConfigUpdate(triage={"max_suggestions": 2}), _request()
    )
    triage_routes.set_triage_config(
        triage_routes.TriageConfigUpdate(triage={"max_suggestions": 5}), _request()
    )
    entry = registry.audit[-1]
    assert entry["previous"]["max_suggestions"] == 2
    assert entry["new"]["max_suggestions"] == 5


def test_clearing_the_policy_is_recorded_as_a_clear(registry: _Registry) -> None:
    triage_routes.set_triage_config(
        triage_routes.TriageConfigUpdate(triage={"enabled": True}), _request()
    )
    triage_routes.set_triage_config(triage_routes.TriageConfigUpdate(triage={}), _request())
    assert "triage" not in registry.overrides
    assert registry.audit[-1]["action"] == "clear"


def test_only_the_triage_section_is_touched(registry: _Registry) -> None:
    """Two admins on two panels must not clobber each other's settings."""
    registry.overrides["checks"] = {"enabled": True}
    triage_routes.set_triage_config(
        triage_routes.TriageConfigUpdate(triage={"enabled": True}), _request()
    )
    assert registry.overrides["checks"] == {"enabled": True}


def test_the_audit_trail_is_readable_and_scoped_to_this_section(
    registry: _Registry,
) -> None:
    registry.record_config_audit(
        section="checks", actor="someone", previous={}, new={"enabled": True}
    )
    triage_routes.set_triage_config(
        triage_routes.TriageConfigUpdate(triage={"enabled": True}), _request(username="ada")
    )
    page = triage_routes.triage_config_audit(_request())
    assert [entry["section"] for entry in page.entries] == ["triage"]
