"""Phase 4 — the events that make a gate decision stale, and what they cost.

CI finishing and a label moving are the two things that change a gate decision
without changing a line of code, so the gate listens for them. It costs no LLM
call, which is what makes that affordable — but it must still be free when the
gate is off, and it must not turn a redelivered webhook into a second approval.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import BackgroundTasks

from mira.config import GateConfig, MiraConfig
from mira.gate.models import STATUS_CONTEXT
from mira.platforms import handlers
from mira.platforms.github import webhook
from mira.platforms.github.webhook import (
    dispatch_github_event,
    handle_gate_pr_event,
    handle_gate_recheck,
)


@pytest.fixture(autouse=True)
def isolated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MIRA_INDEX_DIR", str(tmp_path))
    monkeypatch.delenv("DATABASE_URL", raising=False)


def _auth() -> AsyncMock:
    auth = AsyncMock()
    auth.get_bot_identity = AsyncMock(return_value="mira-bot")
    return auth


def _repository() -> dict:
    return {"owner": {"login": "acme"}, "name": "app", "full_name": "acme/app"}


@pytest.mark.parametrize("event", ["check_suite", "check_run"])
async def test_a_finished_check_suite_queues_a_gate_recheck(event: str) -> None:
    tasks = BackgroundTasks()
    payload = {
        "action": "completed",
        event: {"pull_requests": [{"number": 7}]},
        "repository": _repository(),
        "sender": {"login": "ci-bot"},
    }
    result = await dispatch_github_event(event, payload, _auth(), "mira-bot", tasks)
    assert result == "processing"
    assert len(tasks.tasks) == 1


@pytest.mark.parametrize("action", ["labeled", "unlabeled", "ready_for_review"])
async def test_a_label_or_draft_change_queues_a_gate_recheck(action: str) -> None:
    tasks = BackgroundTasks()
    payload = {
        "action": action,
        "pull_request": {"number": 7, "body": "", "labels": []},
        "repository": _repository(),
        "sender": {"login": "alice"},
    }
    result = await dispatch_github_event("pull_request", payload, _auth(), "mira-bot", tasks)
    assert result == "processing"
    assert [task.func for task in tasks.tasks].count(handle_gate_pr_event) == 1


async def test_mira_s_own_label_change_does_not_loop() -> None:
    tasks = BackgroundTasks()
    payload = {
        "action": "labeled",
        "pull_request": {"number": 7, "body": "", "labels": []},
        "repository": _repository(),
        "sender": {"login": "mira-bot[bot]"},
    }
    result = await dispatch_github_event("pull_request", payload, _auth(), "mira-bot", tasks)
    assert result != "processing"


async def test_a_recheck_costs_nothing_when_the_gate_is_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The policy is resolved before the provider is ever touched."""
    monkeypatch.setattr(handlers, "load_config", lambda: MiraConfig())
    provider = SimpleNamespace(
        get_pr_info=AsyncMock(side_effect=AssertionError("the provider was consulted"))
    )
    await handlers.run_gate_evaluation(
        provider, "acme", "app", 7, "https://github.com/acme/app/pull/7", "mira-bot"
    )
    provider.get_pr_info.assert_not_called()


async def test_a_recheck_failure_never_propagates(monkeypatch: pytest.MonkeyPatch) -> None:
    """A stale gate decision is recoverable; a crashed webhook handler is not."""
    monkeypatch.setattr(handlers, "load_config", lambda: MiraConfig(gate=GateConfig(mode="shadow")))
    provider = SimpleNamespace(get_pr_info=AsyncMock(side_effect=RuntimeError("API is down")))
    await handlers.run_gate_evaluation(
        provider, "acme", "app", 7, "https://github.com/acme/app/pull/7", "mira-bot"
    )
    provider.get_pr_info.assert_awaited()


async def test_a_check_suite_with_no_pull_requests_does_nothing() -> None:
    auth = _auth()
    await handle_gate_recheck(
        {"check_suite": {"pull_requests": []}, "repository": _repository()}, auth, "mira-bot"
    )
    auth.get_installation_token.assert_not_called()


# ───────────────────────────────── regressions from the pre-merge review ──


async def test_a_check_suite_recheck_costs_no_token_when_the_gate_is_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The policy is resolved before an installation token is minted.

    An install that never turned the gate on would otherwise pay an API call
    for every check suite that finishes anywhere it is installed.
    """
    monkeypatch.setattr(webhook, "load_config", lambda: MiraConfig())
    auth = _auth()
    await handle_gate_recheck(
        {
            "check_suite": {"pull_requests": [{"number": 7}]},
            "repository": _repository(),
            "installation": {"id": 1},
        },
        auth,
        "mira-bot",
    )
    auth.get_installation_token.assert_not_called()


async def test_a_label_recheck_costs_no_token_when_the_gate_is_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(webhook, "load_config", lambda: MiraConfig())
    auth = _auth()
    await handle_gate_pr_event(
        {
            "pull_request": {"number": 7},
            "repository": _repository(),
            "installation": {"id": 1},
        },
        auth,
        "mira-bot",
    )
    auth.get_installation_token.assert_not_called()


async def test_an_unreadable_policy_is_not_active(monkeypatch: pytest.MonkeyPatch) -> None:
    def _explode() -> MiraConfig:
        raise RuntimeError("the settings store is unreachable")

    monkeypatch.setattr(webhook, "load_config", _explode)
    assert webhook._gate_is_active("acme", "app") is False


async def test_the_gates_own_check_run_does_not_wake_the_gate() -> None:
    """Publishing a check run delivers a webhook straight back to the app.

    Without this filter the gate wakes itself: a retryable delivery
    republishes, the republish arrives as an event, the inputs have not moved
    so the decision key is the same, and it republishes again.
    """
    tasks = BackgroundTasks()
    payload = {
        "action": "completed",
        "check_run": {"name": STATUS_CONTEXT, "pull_requests": [{"number": 7}]},
        "repository": _repository(),
        "sender": {"login": "mira-bot[bot]"},
    }
    result = await dispatch_github_event("check_run", payload, _auth(), "mira-bot", tasks)
    assert result != "processing"
    assert tasks.tasks == []


async def test_a_check_suite_from_the_gates_own_app_does_not_wake_it() -> None:
    tasks = BackgroundTasks()
    payload = {
        "action": "completed",
        "check_suite": {"app": {"slug": "mira-bot"}, "pull_requests": [{"number": 7}]},
        "repository": _repository(),
        "sender": {"login": "mira-bot[bot]"},
    }
    result = await dispatch_github_event("check_suite", payload, _auth(), "mira-bot", tasks)
    assert result != "processing"
    assert tasks.tasks == []


async def test_a_real_check_run_still_wakes_the_gate() -> None:
    tasks = BackgroundTasks()
    payload = {
        "action": "completed",
        "check_run": {
            "name": "build",
            "app": {"slug": "github-actions"},
            "pull_requests": [{"number": 7}],
        },
        "repository": _repository(),
        "sender": {"login": "github-actions[bot]"},
    }
    result = await dispatch_github_event("check_run", payload, _auth(), "mira-bot", tasks)
    assert result == "processing"
    assert len(tasks.tasks) == 1
