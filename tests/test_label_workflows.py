"""Boundary changes, graph safety, repository isolation and idempotent writes."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError

from mira.dashboard.db import AppDatabase
from mira.labels.engine import evaluate, matches, plan
from mira.labels.models import Condition, LabelWorkflow, PRFacts
from mira.labels.presets import presets, simple_preset, size_preset
from mira.labels.service import reconcile
from mira.labels.store import load_workflow, save_workflow
from mira.labels.webhooks import schedule_labels
from mira.models import FileChangeStat, PRInfo


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setenv("MIRA_INDEX_DIR", str(tmp_path))
    store = AppDatabase(url="", admin_password="test-password")
    monkeypatch.setattr("mira.dashboard.api._app_db", store)
    store.register_repo("acme", "app", platform="github")
    store.register_repo("acme", "app", platform="gitlab")
    store.register_repo("acme", "other", platform="github")
    yield store
    store.close()


@pytest.mark.parametrize(
    "lines,label",
    [
        (0, "XS"),
        (10, "XS"),
        (11, "S"),
        (100, "S"),
        (101, "M"),
        (500, "M"),
        (501, "L"),
        (1000, "L"),
        (1001, "XL"),
        (999999, "XL"),
    ],
)
def test_size_boundaries(lines, label):
    result = evaluate(size_preset(), PRFacts(additions=lines))
    assert [item.name for item in result.labels] == [f"size/{label}"]


def test_size_includes_deletions_and_removes_only_managed_labels():
    result = plan(
        evaluate(size_preset(), PRFacts(additions=250, deletions=251)), ["size/M", "bug", "manual"]
    )
    assert result.add == ["size/L"]
    assert result.remove == ["size/M"]


@pytest.mark.parametrize(
    "field,op,value,facts,expected",
    [
        ("author", "eq", "@Alice", {"author": "alice"}, True),
        ("author", "ne", "alice", {"author": "ALICE"}, False),
        ("title", "contains", "fix", {"title": "fix: login"}, True),
        ("head_branch", "starts_with", "feat/", {"head_branch": "feat/ui"}, True),
        ("files", "glob", "docs/**", {"files": ["src/a.py", "docs/api/start.md"]}, True),
        ("files", "ne", "secret.txt", {"files": ["src/a.py", "secret.txt"]}, False),
        ("draft", "eq", True, {"draft": True}, True),
        ("total_lines", "gt", 500, {"additions": 500}, False),
        ("changed_files", "gte", 3, {"changed_files": 3}, True),
    ],
)
def test_conditions(field, op, value, facts, expected):
    assert matches(Condition(field=field, operator=op, value=value), PRFacts(**facts)) is expected


@pytest.mark.parametrize(
    "condition",
    [
        {"field": "total_lines", "operator": "lte", "value": True},
        {"field": "total_lines", "operator": "glob", "value": 1},
        {"field": "author", "operator": "gt", "value": "alice"},
        {"field": "draft", "operator": "eq", "value": "true"},
        {"field": "files", "operator": "glob", "value": ""},
        {"field": "title", "operator": "regex", "value": "(a+)+"},
    ],
)
def test_invalid_conditions_rejected(condition):
    with pytest.raises(ValidationError):
        Condition.model_validate(condition)


@pytest.mark.parametrize(
    "mutation",
    [
        "cycle",
        "orphan",
        "dangling",
        "duplicate_id",
        "wrong_branch",
        "label_output",
        "duplicate_label",
        "unknown_field",
    ],
)
def test_invalid_graph_rejected(mutation):
    graph = size_preset().model_dump()
    if mutation == "cycle":
        graph["edges"].append(
            {"id": "cycle", "source": "limit-S", "target": "limit-XS", "branch": "true"}
        )
    elif mutation == "orphan":
        graph["edges"].pop(0)
    elif mutation == "dangling":
        graph["edges"][0]["target"] = "missing"
    elif mutation == "duplicate_id":
        graph["nodes"].append(graph["nodes"][0])
    elif mutation == "wrong_branch":
        graph["edges"][0]["branch"] = "true"
    elif mutation == "label_output":
        graph["edges"].append(
            {"id": "label-out", "source": "size-M", "target": "size-L", "branch": "next"}
        )
    elif mutation == "duplicate_label":
        graph["nodes"][2]["action"]["name"] = "size/M"
    else:
        graph["execute_code"] = "print('no')"
    with pytest.raises(ValidationError):
        LabelWorkflow.model_validate(graph)


def test_alternative_paths_deduplicate_label():
    graph = simple_preset("author", "eq", "alice", "team").model_dump()
    graph["nodes"].append(
        {
            "id": "title",
            "kind": "condition",
            "condition": {"field": "title", "operator": "contains", "value": "fix"},
        }
    )
    graph["edges"].extend(
        [
            {"id": "to-title", "source": "start", "target": "title"},
            {"id": "title-label", "source": "title", "target": "label", "branch": "true"},
        ]
    )
    workflow = LabelWorkflow.model_validate(graph)
    for facts in [
        PRFacts(author="alice"),
        PRFacts(title="fix: bug"),
        PRFacts(author="alice", title="fix"),
    ]:
        assert len(evaluate(workflow, facts).labels) == 1
    assert not evaluate(workflow, PRFacts()).labels


def test_every_preset_is_a_valid_disabled_draft():
    for preset in presets():
        assert not LabelWorkflow.model_validate(preset["workflow"]).enabled


def test_workflows_isolate_repo_and_platform_and_survive_reload(db):
    workflow = size_preset()
    save_workflow(db, "github", "acme", "app", workflow)
    assert load_workflow(db, "github", "acme", "app") == workflow
    assert not load_workflow(db, "gitlab", "acme", "app").nodes
    assert not load_workflow(db, "github", "acme", "other").nodes


class FakeProvider:
    def __init__(self):
        self.pr = PRInfo(
            title="PR",
            description="",
            base_branch="main",
            head_branch="topic",
            url="https://github.com/acme/app/pull/7",
            owner="acme",
            repo="app",
            number=7,
            head_sha="abc",
            author="alice",
        )
        self.lines = 500
        self.labels = {"bug"}
        self.mutations = []
        self.fail_add = False
        self.fail_stats = False
        self.get_info_calls = 0

    async def get_pr_info(self, url):
        self.get_info_calls += 1
        return replace(self.pr)

    async def get_label_change_stats(self, pr):
        if self.fail_stats:
            raise RuntimeError("provider unavailable")
        return [FileChangeStat("app.py", self.lines, 0)]

    async def get_pr_labels(self, pr):
        return sorted(self.labels)

    async def ensure_label(self, pr, name, color, description):
        self.mutations.append(("ensure", name))

    async def add_label(self, pr, name):
        if self.fail_add:
            raise RuntimeError("permission denied")
        self.labels.add(name)
        self.mutations.append(("add", name))

    async def remove_label(self, pr, name):
        self.labels.discard(name)
        self.mutations.append(("remove", name))


async def run(provider, db):
    return await reconcile(provider, db, "github", "acme", "app", 7, provider.pr.url)


def activate(db, workflow=None):
    workflow = workflow or size_preset()
    workflow.enabled = True
    save_workflow(db, "github", "acme", "app", workflow)


async def test_500_to_501_and_back_is_idempotent(db):
    activate(db)
    provider = FakeProvider()
    await run(provider, db)
    assert provider.labels == {"bug", "size/M"}
    provider.lines = 501
    await run(provider, db)
    assert provider.labels == {"bug", "size/L"}
    assert provider.mutations[-2:] == [("remove", "size/M"), ("add", "size/L")]
    provider.lines = 500
    await run(provider, db)
    assert provider.labels == {"bug", "size/M"}
    provider.mutations.clear()
    await run(provider, db)
    assert provider.mutations == []


async def test_disabled_workflow_makes_no_provider_calls(db):
    provider = FakeProvider()
    assert await run(provider, db) == {"status": "disabled"}
    assert provider.get_info_calls == 0


async def test_renamed_rule_cleans_up_previous_label(db):
    activate(db)
    provider = FakeProvider()
    await run(provider, db)
    workflow = size_preset()
    next(node for node in workflow.nodes if node.id == "size-M").action.name = "mediana"
    activate(db, workflow)
    await run(provider, db)
    assert provider.labels == {"bug", "mediana"}


async def test_add_only_action_keeps_label_when_unmatched(db):
    workflow = simple_preset("author", "eq", "alice", "team")
    workflow.nodes[-1].action.mode = "add"
    activate(db, workflow)
    provider = FakeProvider()
    await run(provider, db)
    provider.pr.author = "bob"
    await run(provider, db)
    assert provider.labels == {"bug", "team"}


async def test_failed_stats_never_remove_current_label(db):
    activate(db)
    provider = FakeProvider()
    provider.labels.add("size/L")
    provider.fail_stats = True
    with pytest.raises(RuntimeError):
        await run(provider, db)
    assert provider.labels == {"bug", "size/L"}
    assert not provider.mutations


async def test_failed_write_can_be_retried(db):
    activate(db)
    provider = FakeProvider()
    provider.labels.add("size/L")
    provider.fail_add = True
    with pytest.raises(RuntimeError):
        await run(provider, db)
    provider.fail_add = False
    await run(provider, db)
    assert provider.labels == {"bug", "size/M"}


async def test_changed_head_refetches_facts(db):
    activate(db)
    provider = FakeProvider()
    original = provider.get_pr_info

    async def changed(url):
        if provider.get_info_calls == 1:
            provider.pr.head_sha = "new"
            provider.lines = 501
        return await original(url)

    provider.get_pr_info = changed
    await run(provider, db)
    assert provider.labels == {"bug", "size/L"}


async def test_concurrent_events_wait_and_fetch_latest_data(db):
    activate(db)
    provider = FakeProvider()
    entered, release = asyncio.Event(), asyncio.Event()
    original = provider.add_label

    async def delayed(pr, name):
        entered.set()
        await release.wait()
        await original(pr, name)

    provider.add_label = delayed
    first = asyncio.create_task(run(provider, db))
    await entered.wait()
    provider.lines = 501
    second = asyncio.create_task(run(provider, db))
    release.set()
    await asyncio.gather(first, second)
    assert provider.labels == {"bug", "size/L"}


@pytest.fixture
def client(db):
    from mira.dashboard.api import router

    app = FastAPI()

    @app.middleware("http")
    async def user(request, call_next):
        request.state.user = SimpleNamespace(
            username="admin", is_admin=request.headers.get("X-Test-Admin", "true") == "true"
        )
        return await call_next(request)

    app.include_router(router)
    return TestClient(app)


def test_api_save_preview_copy_and_isolation(client, db):
    query = "?platform=github&owner=acme&repo=app"
    workflow = size_preset().model_dump()
    workflow["enabled"] = True
    assert client.put("/api/labels/workflow" + query, json=workflow).status_code == 200
    assert client.get("/api/labels/workflow" + query).json() == workflow
    preview = client.post(
        "/api/labels/preview",
        json={
            "workflow": workflow,
            "facts": {"additions": 501},
            "current_labels": ["size/M", "bug"],
        },
    )
    assert preview.json()["add"] == ["size/L"]
    assert preview.json()["remove"] == ["size/M"]
    copied = client.post(
        "/api/labels/copy", json={"platform": "github", "owner": "acme", "repo": "app"}
    ).json()
    assert not copied["enabled"]
    assert copied["nodes"] == workflow["nodes"]
    copied["nodes"][2]["action"]["name"] = "tiny"
    assert (
        client.put(
            "/api/labels/workflow?platform=github&owner=acme&repo=other", json=copied
        ).status_code
        == 200
    )
    assert client.get("/api/labels/workflow" + query).json() == workflow
    assert len(db.list_config_audit(section="labels")) == 2


@pytest.mark.parametrize(
    "method,path,body",
    [
        ("get", "/api/labels/presets", None),
        ("get", "/api/labels/workflow?owner=acme&repo=app", None),
        ("put", "/api/labels/workflow?owner=acme&repo=app", {}),
        ("post", "/api/labels/copy", {"owner": "acme", "repo": "app"}),
        ("post", "/api/labels/preview", {"workflow": {}, "facts": {}}),
    ],
)
def test_all_label_routes_require_admin(client, method, path, body):
    kwargs = {"headers": {"X-Test-Admin": "false"}}
    if body is not None:
        kwargs["json"] = body
    assert getattr(client, method)(path, **kwargs).status_code == 403


def test_api_rejects_invalid_and_unknown_repos(client):
    assert client.get("/api/labels/workflow?owner=acme&repo=missing").status_code == 404
    graph = size_preset().model_dump()
    graph["edges"] = []
    assert client.put("/api/labels/workflow?owner=acme&repo=app", json=graph).status_code == 422


@pytest.mark.parametrize(
    "action,expected",
    [
        ("opened", 1),
        ("synchronize", 1),
        ("edited", 1),
        ("ready_for_review", 1),
        ("labeled", 0),
        ("unlabeled", 0),
        ("closed", 0),
    ],
)
def test_github_label_events_do_not_loop(db, action, expected):
    activate(db)
    tasks = MagicMock()
    payload = {
        "action": action,
        "repository": {"full_name": "acme/app"},
        "pull_request": {"number": 7},
        "installation": {"id": 123},
    }
    schedule_labels("github", "pull_request", payload, MagicMock(), tasks)
    assert tasks.add_task.call_count == expected


async def test_dispatch_labels_even_when_push_reviews_disabled(db, monkeypatch):
    from mira.config import MiraConfig
    from mira.platforms.github.webhook import dispatch_github_event

    config = MiraConfig()
    config.review.review_on_synchronize = False
    monkeypatch.setattr("mira.platforms.github.webhook.load_config", lambda: config)
    activate(db)
    tasks = MagicMock()
    payload = {
        "action": "synchronize",
        "repository": {"full_name": "acme/app"},
        "pull_request": {"number": 7},
        "sender": {"login": "alice"},
    }
    await dispatch_github_event("pull_request", payload, AsyncMock(), "mira", tasks)
    assert any(call.args[0].__name__ == "run_labels" for call in tasks.add_task.call_args_list)
