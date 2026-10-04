"""DORA metrics and cycle time: the computation, the pull_requests columns and
deployments table behind it, merge-time enrichment, webhook capture and the API."""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from mira.analytics import collect
from mira.analytics.dora import compute_dora, is_default_branch_merge, is_failure_fix
from mira.config import DoraConfig, MiraConfig
from mira.dashboard import api
from mira.dashboard.db import AppDatabase
from mira.models import PRInfo, ReleaseRef
from mira.platforms.github import webhook as gh_webhook

DAY = 86400.0
HOUR = 3600.0
NOW = 1_800_000_000.0


@pytest.fixture
def db(tmp_path: Path) -> AppDatabase:
    return AppDatabase(url=str(tmp_path / "app.db"), admin_password="admin")


@pytest.fixture
def patched_api(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AppDatabase:
    monkeypatch.setenv("MIRA_INDEX_DIR", str(tmp_path))
    d = AppDatabase(url=str(tmp_path / "app.db"), admin_password="admin")
    monkeypatch.setattr(api, "_app_db", d)
    return d


def _admin() -> SimpleNamespace:
    return SimpleNamespace(state=SimpleNamespace(user=SimpleNamespace(is_admin=True)))


def _viewer() -> SimpleNamespace:
    return SimpleNamespace(state=SimpleNamespace(user=SimpleNamespace(is_admin=False)))


def _row(number: int, merged_at: float, **kw: Any) -> dict[str, Any]:
    row = {
        "owner": "o",
        "repo": "r",
        "number": number,
        "title": f"PR {number}",
        "labels": [],
        "base_branch": "main",
        "default_branch": "main",
        "created_at": merged_at - 10 * HOUR,
        "first_commit_at": 0.0,
        "first_review_at": 0.0,
        "first_approval_at": 0.0,
        "merged_at": merged_at,
        "url": "",
    }
    row.update(kw)
    return row


# ── classification ──


def test_failure_fix_by_title_prefix_or_label() -> None:
    prefixes, labels = ["revert", "hotfix"], ["hotfix", "incident"]
    assert is_failure_fix({"title": 'Revert "Add cache"', "labels": []}, prefixes, labels)
    assert is_failure_fix({"title": "hotfix: null deref", "labels": []}, prefixes, labels)
    assert is_failure_fix({"title": "[Hotfix] login", "labels": []}, prefixes, labels)
    assert is_failure_fix({"title": "Fix login", "labels": ["Incident"]}, prefixes, labels)
    assert not is_failure_fix({"title": "Add revert button", "labels": []}, prefixes, labels)


def test_default_branch_merge_counts_unknowns() -> None:
    assert is_default_branch_merge({"base_branch": "main", "default_branch": "main"})
    assert not is_default_branch_merge({"base_branch": "feature", "default_branch": "main"})
    assert is_default_branch_merge({"base_branch": "", "default_branch": "main"})
    assert is_default_branch_merge({"base_branch": "main", "default_branch": ""})


# ── computation ──


def test_deployment_frequency_counts_default_branch_merges() -> None:
    rows = [_row(i, NOW - i * DAY - 1) for i in range(7)]  # 7 merges in 7 days
    rows.append(_row(99, NOW - DAY, base_branch="feature"))  # not a deployment
    rows.append(_row(100, NOW - 10 * DAY))  # previous window
    report = compute_dora(rows, [], days=7, now=NOW)
    assert report.current.deployments == 7
    assert report.current.deployments_per_day == pytest.approx(1.0)
    assert report.current.deployment_band == "elite"
    assert report.previous.deployments == 1
    assert len(report.series) == 7
    assert sum(b.deployments for b in report.series) == 7


def test_releases_mode_counts_deployments_table() -> None:
    rows = [_row(1, NOW - DAY)]
    deployments = [
        {"deployed_at": NOW - 2 * DAY},
        {"deployed_at": NOW - 40 * DAY},
    ]
    report = compute_dora(rows, deployments, days=30, deployment_source="releases", now=NOW)
    assert report.current.deployments == 1
    assert report.deployment_source == "releases"
    assert report.current.deployment_band == "medium"  # 1 in 30 days


def test_lead_time_prefers_first_commit_and_falls_back_to_created() -> None:
    rows = [
        _row(1, NOW - DAY, first_commit_at=NOW - DAY - 4 * HOUR),
        _row(2, NOW - DAY, created_at=NOW - DAY - 2 * HOUR),
        _row(3, NOW - DAY, first_commit_at=NOW - DAY - 6 * HOUR),
    ]
    report = compute_dora(rows, [], days=7, now=NOW)
    assert report.current.lead_time_secs == pytest.approx(4 * HOUR)
    assert report.current.lead_time_band == "elite"


def test_change_failure_rate_and_mttr_from_revert() -> None:
    rows = [
        _row(1, NOW - 3 * DAY, title="Add cache layer"),
        _row(2, NOW - 3 * DAY + 2 * HOUR, title='Revert "Add cache layer"'),
        _row(3, NOW - 2 * DAY),
        _row(4, NOW - DAY, title="hotfix: login", created_at=NOW - DAY - 30 * 60),
    ]
    report = compute_dora(rows, [], days=7, title_prefixes=["revert", "hotfix"], now=NOW)
    cur = report.current
    assert cur.failures == 2
    assert cur.change_failure_rate == pytest.approx(0.5)
    assert cur.change_failure_band == "low"
    # Revert: 2h from culprit merge; hotfix: 30m open → merge. Median = 75m.
    assert cur.mttr_secs == pytest.approx(75 * 60)
    assert cur.mttr_band == "high"
    restore = {f["number"]: f["restore_secs"] for f in report.failures}
    assert restore[2] == pytest.approx(2 * HOUR)


def test_cycle_time_breakdown_medians() -> None:
    created = NOW - 5 * DAY
    rows = [
        _row(
            1,
            created + 10 * HOUR,
            created_at=created,
            first_commit_at=created - 2 * HOUR,
            first_review_at=created + 1 * HOUR,
            first_approval_at=created + 4 * HOUR,
        ),
        _row(
            2,
            created + 30 * HOUR,
            created_at=created,
            first_review_at=created + 3 * HOUR,
            first_approval_at=created + 8 * HOUR,
        ),
    ]
    cycle = compute_dora(rows, [], days=7, now=NOW).current.cycle
    assert cycle.prs == 2
    assert cycle.time_to_first_review_secs == pytest.approx(2 * HOUR)
    assert cycle.time_to_approval_secs == pytest.approx(6 * HOUR)
    assert cycle.time_to_merge_secs == pytest.approx(20 * HOUR)
    assert cycle.coding_time_secs == pytest.approx(2 * HOUR)
    assert cycle.total_cycle_secs == pytest.approx(21 * HOUR)  # (12h + 30h) / 2


def test_empty_data_yields_no_metrics() -> None:
    report = compute_dora([], [], days=30, now=NOW)
    assert report.current.deployments == 0
    assert report.current.lead_time_secs is None
    assert report.current.change_failure_rate is None
    assert report.current.mttr_secs is None
    assert report.bucket_secs == DAY
    assert compute_dora([], [], days=90, now=NOW).bucket_secs == 7 * DAY


def test_config_validates_deployment_source() -> None:
    assert MiraConfig().analytics.dora.deployment_source == "merges"
    assert DoraConfig(deployment_source="releases").deployment_source == "releases"
    with pytest.raises(ValueError):
        DoraConfig(deployment_source="deploys")


# ── storage ──


def test_upsert_records_dora_columns_and_keeps_earliest_first_commit(db: AppDatabase) -> None:
    db.upsert_pull_request(
        "o",
        "r",
        1,
        title="t",
        state="merged",
        created_at=100,
        merged_at=500,
        base_branch="main",
        default_branch="main",
        labels=["Hotfix", "bug"],
        first_commit_at=50,
    )
    # A later sparse upsert keeps labels/branches and the earlier first commit.
    db.upsert_pull_request("o", "r", 1, state="merged", merged_at=500, first_commit_at=80)
    [row] = db.get_delivery_rows(0)
    assert row["labels"] == ["bug", "hotfix"]
    assert row["base_branch"] == "main" and row["default_branch"] == "main"
    assert row["first_commit_at"] == 50
    # An explicit (even empty) label list replaces them.
    db.upsert_pull_request("o", "r", 1, state="merged", merged_at=500, labels=[])
    assert db.get_delivery_rows(0)[0]["labels"] == []


def test_first_approval_keeps_earliest(db: AppDatabase) -> None:
    db.upsert_pull_request("o", "r", 1, state="merged", created_at=1, merged_at=900)
    db.set_pr_first_approval("o", "r", 1, 500)
    db.set_pr_first_approval("o", "r", 1, 300)
    db.set_pr_first_approval("o", "r", 1, 800)
    assert db.get_delivery_rows(0)[0]["first_approval_at"] == 300


def test_delivery_rows_filter_by_repo_and_window(db: AppDatabase) -> None:
    db.upsert_pull_request("o", "a", 1, state="merged", merged_at=100)
    db.upsert_pull_request("o", "b", 2, state="merged", merged_at=200)
    db.upsert_pull_request("o", "b", 3, state="open")
    assert [r["number"] for r in db.get_delivery_rows(0)] == [1, 2]
    assert [r["number"] for r in db.get_delivery_rows(0, owner="o", repo="b")] == [2]
    assert [r["number"] for r in db.get_delivery_rows(150)] == [2]


def test_deployments_are_idempotent(db: AppDatabase) -> None:
    db.record_deployment("o", "r", "v1", 100.0, url="u")
    db.record_deployment("o", "r", "v1", 100.0)
    db.record_deployment("o", "r", "", 100.0)  # ignored
    db.record_deployment("o", "x", "v1", 200.0)
    deps = db.get_deployments(0, owner="o", repo="r")
    assert len(deps) == 1 and deps[0]["url"] == "u"
    assert len(db.get_deployments(0)) == 2


def test_sqlite_migration_adds_dora_columns(tmp_path: Path) -> None:
    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE pull_requests (owner TEXT NOT NULL, repo TEXT NOT NULL, "
        "number INTEGER NOT NULL, author TEXT NOT NULL DEFAULT '', title TEXT NOT NULL DEFAULT '', "
        "url TEXT NOT NULL DEFAULT '', state TEXT NOT NULL DEFAULT 'open', "
        "draft INTEGER NOT NULL DEFAULT 0, created_at REAL NOT NULL DEFAULT 0, "
        "updated_at REAL NOT NULL DEFAULT 0, first_review_at REAL NOT NULL DEFAULT 0, "
        "merged_at REAL NOT NULL DEFAULT 0, closed_at REAL NOT NULL DEFAULT 0, "
        "PRIMARY KEY (owner, repo, number))"
    )
    conn.execute("INSERT INTO pull_requests (owner, repo, number, merged_at) VALUES ('o','r',1,5)")
    conn.commit()
    conn.close()
    migrated = AppDatabase(url=str(path), admin_password="admin")
    [row] = migrated.get_delivery_rows(0)
    assert row["labels"] == [] and row["first_approval_at"] == 0.0


# ── merge-time enrichment ──


class _Provider:
    def __init__(self, *, labels: Any = ("hotfix",), fail: bool = False) -> None:
        self.labels = labels
        self.fail = fail

    async def get_pr_first_commit_at(self, pr_info: PRInfo) -> float:
        if self.fail:
            raise RuntimeError("boom")
        return 1000.0

    async def get_default_branch(self, pr_info: PRInfo) -> str:
        return "main"

    async def get_pr_labels(self, pr_info: PRInfo) -> list[str]:
        if self.fail:
            raise RuntimeError("boom")
        return list(self.labels)

    async def list_deployment_releases(self, pr_info: PRInfo, *, since: float = 0.0) -> list:
        return [ReleaseRef(tag="v9", at=time.time() - DAY, url="rel")]


def _pr_info(platform: str = "gitlab") -> PRInfo:
    return PRInfo(
        title="hotfix: crash",
        description="",
        base_branch="main",
        head_branch="fix",
        url="https://gitlab.example/o/r/-/merge_requests/4",
        number=4,
        owner="o",
        repo="r",
        platform=platform,
        author="dev",
    )


@pytest.mark.asyncio
async def test_merge_enrichment_creates_row_for_platforms_without_lifecycle(
    db: AppDatabase,
) -> None:
    await collect.record_merge_delivery(_Provider(), _pr_info(), config=MiraConfig(), app_db=db)
    [row] = db.get_delivery_rows(0)
    assert row["title"] == "hotfix: crash"
    assert row["labels"] == ["hotfix"]
    assert row["first_commit_at"] == 1000.0
    assert row["base_branch"] == "main" and row["default_branch"] == "main"
    assert db.get_deployments(0) == []  # merges mode: no release sync


@pytest.mark.asyncio
async def test_merge_enrichment_degrades_and_syncs_releases(db: AppDatabase) -> None:
    db.upsert_pull_request("o", "r", 4, state="open", labels=["keep"])
    cfg = MiraConfig()
    cfg.analytics.dora.deployment_source = "releases"
    await collect.record_merge_delivery(_Provider(fail=True), _pr_info(), config=cfg, app_db=db)
    [row] = db.get_delivery_rows(0)
    assert row["labels"] == ["keep"]  # unreadable labels leave stored ones alone
    assert row["first_commit_at"] == 0.0
    assert [d["ref"] for d in db.get_deployments(0)] == ["v9"]


@pytest.mark.asyncio
async def test_merge_enrichment_is_config_gated(db: AppDatabase) -> None:
    cfg = MiraConfig()
    cfg.analytics.dora.enabled = False
    await collect.record_merge_delivery(_Provider(), _pr_info(), config=cfg, app_db=db)
    assert db.get_delivery_rows(0) == []


# ── GitHub webhook capture ──


def test_github_lifecycle_records_branches_and_labels(
    patched_api: AppDatabase, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(gh_webhook, "_get_app_db", lambda: patched_api)
    payload = {
        "repository": {"name": "r", "owner": {"login": "o"}, "default_branch": "main"},
        "pull_request": {
            "number": 5,
            "title": "Revert x",
            "state": "closed",
            "merged": True,
            "created_at": "2026-01-01T00:00:00Z",
            "merged_at": "2026-01-02T00:00:00Z",
            "base": {"ref": "main"},
            "labels": [{"name": "Revert"}],
            "user": {"login": "dev"},
        },
    }
    gh_webhook._record_pr_lifecycle(payload)
    [row] = patched_api.get_delivery_rows(0)
    assert row["base_branch"] == "main" and row["default_branch"] == "main"
    assert row["labels"] == ["revert"]


def test_github_release_event_records_deployment(
    patched_api: AppDatabase, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(gh_webhook, "_get_app_db", lambda: patched_api)
    payload = {
        "action": "published",
        "repository": {"name": "r", "owner": {"login": "o"}},
        "release": {"tag_name": "v1.2", "published_at": "2026-01-02T00:00:00Z", "html_url": "u"},
    }
    gh_webhook._record_release(payload)
    gh_webhook._record_release({**payload, "release": {**payload["release"], "draft": True}})
    [dep] = patched_api.get_deployments(0)
    assert dep["ref"] == "v1.2" and dep["kind"] == "release"


# ── API ──


def test_dora_route_is_admin_only(patched_api: AppDatabase) -> None:
    from fastapi import HTTPException

    from mira.dashboard.routers import delivery

    with pytest.raises(HTTPException) as exc:
        delivery.review_dora(_viewer(), days=30)
    assert exc.value.status_code == 403


def test_dora_route_reports_current_previous_and_repos(patched_api: AppDatabase) -> None:
    from mira.dashboard.routers import delivery

    now = time.time()
    for i, repo in enumerate(("a", "a", "b")):
        patched_api.upsert_pull_request(
            "o",
            repo,
            i + 1,
            title="hotfix: x" if i == 2 else f"PR {i}",
            state="merged",
            created_at=now - 2 * DAY,
            merged_at=now - DAY,
        )
    resp = delivery.review_dora(_admin(), days=7)
    assert resp.enabled is True
    assert resp.current["deployments"] == 3
    assert resp.current["failures"] == 1
    assert resp.previous["deployments"] == 0
    assert resp.repos == ["o/a", "o/b"]
    one = delivery.review_dora(_admin(), days=7, repo="o/a")
    assert one.current["deployments"] == 2
    assert one.repos == ["o/a", "o/b"]

    from fastapi import HTTPException

    with pytest.raises(HTTPException):
        delivery.review_dora(_admin(), days=7, repo="nope")


@pytest.mark.asyncio
async def test_github_merge_enrichment_keeps_the_webhook_merge_time(db: AppDatabase) -> None:
    db.upsert_pull_request("o", "r", 4, state="merged", created_at=100, merged_at=500)
    await collect.record_merge_delivery(
        _Provider(), _pr_info("github"), config=MiraConfig(), app_db=db
    )
    [row] = db.get_delivery_rows(0)
    assert row["merged_at"] == 500
    assert row["labels"] == ["hotfix"]
