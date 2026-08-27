"""Phase 5 — `@mira fix` arriving from GitHub, GitLab and Forgejo.

Three webhook layers funnel into one platform-neutral handler, so what these
check is the funnelling: that `fix` is routed before the reply classifier that
would otherwise treat it as conversation, that the finding is resolved from the
hidden marker rather than from where the comment sits, and that the same
command produces the same request on all three platforms.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest
from fastapi import BackgroundTasks

from mira.autofix.commands import handle_fix_command
from mira.autofix.models import ReasonCode
from mira.autofix.service import RequestOutcome
from mira.feedback.provenance import finding_marker
from mira.index.store import IndexStore
from mira.platforms.github.webhook import dispatch_github_event, handle_fix_request


@pytest.fixture(autouse=True)
def isolated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MIRA_INDEX_DIR", str(tmp_path))
    monkeypatch.delenv("DATABASE_URL", raising=False)
    IndexStore.open("acme", "app").close()


def _auth() -> AsyncMock:
    auth = AsyncMock()
    auth.get_bot_identity = AsyncMock(return_value="mira-bot")
    auth.get_installation_token = AsyncMock(return_value="tok")
    auth.get_token = AsyncMock(return_value="tok")
    return auth


def _repository() -> dict:
    return {"owner": {"login": "acme"}, "name": "app", "full_name": "acme/app"}


FINDING_ID = "11111111-1111-4111-8111-111111111111"
BOT_COMMENT = f"{finding_marker(FINDING_ID)}\n**Division by zero**\n\nGuard the divisor."


class RecordingProvider:
    """Answers what the funnel needs and records what reached the platform."""

    def __init__(self, original: str = BOT_COMMENT) -> None:
        self._original = original
        self.comments: list[str] = []
        self.replies: list[str] = []

    async def get_pr_info(self, pr_url: str) -> SimpleNamespace:
        return SimpleNamespace(
            owner="acme",
            repo="app",
            number=7,
            url=pr_url,
            title="A pull request",
            description="",
            base_branch="main",
            head_branch="feature",
            base_sha="base",
            head_sha="head",
            platform="github",
            author="alice",
            draft=False,
        )

    async def get_comment_body(self, pr_info: Any, comment_id: int) -> str:
        return self._original

    async def get_discussion_root_body(self, pr_info: Any, discussion_id: str) -> str:
        return self._original

    async def post_comment(self, pr_info: Any, body: str) -> None:
        self.comments.append(body)

    async def reply_to_review_comment(self, pr_info: Any, comment_id: int, body: str) -> None:
        self.replies.append(body)


# ── GitHub dispatch ──────────────────────────────────────────────────────────


async def test_fix_on_a_review_comment_is_routed_before_the_reply_classifier() -> None:
    """`fix` writes. It must never fall through to free-form conversation."""
    tasks = BackgroundTasks()
    payload = {
        "action": "created",
        "repository": _repository(),
        "pull_request": {"number": 7},
        "comment": {
            "id": 5,
            "node_id": "n5",
            "body": "@mira fix",
            "user": {"login": "alice", "type": "User"},
            "in_reply_to_id": 4,
        },
    }
    status = await dispatch_github_event(
        "pull_request_review_comment", payload, _auth(), "mira", tasks
    )
    assert status == "processing"
    assert tasks.tasks[0].func is handle_fix_request
    assert tasks.tasks[0].kwargs == {"inline": True}


async def test_fix_all_on_the_pull_request_is_routed_to_the_fix_handler() -> None:
    tasks = BackgroundTasks()
    payload = {
        "action": "created",
        "repository": _repository(),
        "issue": {"number": 7, "pull_request": {}, "title": "A pull request"},
        "comment": {"id": 5, "body": "@mira fix all", "user": {"login": "alice", "type": "User"}},
    }
    status = await dispatch_github_event("issue_comment", payload, _auth(), "mira", tasks)
    assert status == "processing"
    assert tasks.tasks[0].func is handle_fix_request
    assert tasks.tasks[0].kwargs == {"inline": False}


async def test_an_ordinary_reply_still_reaches_the_reply_classifier() -> None:
    from mira.platforms.github.webhook import handle_thread_reject

    tasks = BackgroundTasks()
    payload = {
        "action": "created",
        "repository": _repository(),
        "pull_request": {"number": 7},
        "comment": {
            "id": 5,
            "node_id": "n5",
            "body": "@mira why is this a problem?",
            "user": {"login": "alice", "type": "User"},
            "in_reply_to_id": 4,
        },
    }
    await dispatch_github_event("pull_request_review_comment", payload, _auth(), "mira", tasks)
    assert tasks.tasks[0].func is handle_thread_reject


async def test_the_bots_own_fix_comment_is_ignored() -> None:
    tasks = BackgroundTasks()
    payload = {
        "action": "created",
        "repository": _repository(),
        "issue": {"number": 7, "pull_request": {}},
        "comment": {
            "id": 5,
            "body": "@mira fix all",
            "user": {"login": "mira[bot]", "type": "Bot"},
        },
    }
    status = await dispatch_github_event("issue_comment", payload, _auth(), "mira", tasks)
    assert status == "ignored"
    assert tasks.tasks == []


async def test_fix_on_the_pull_request_body_asks_for_a_finding(monkeypatch) -> None:
    """`fix` with nothing to point at names no finding, and says so."""
    provider = RecordingProvider()
    monkeypatch.setattr(
        "mira.providers.create_provider", lambda platform, token: provider, raising=False
    )
    import mira.platforms.github.webhook as gh

    monkeypatch.setattr(gh, "create_provider", lambda platform, token: provider)
    payload = {
        "repository": _repository(),
        "issue": {"number": 7, "pull_request": {}},
        "comment": {"id": 5, "body": "@mira fix", "user": {"login": "alice"}},
        "installation": {"id": 1},
    }
    await handle_fix_request(payload, _auth(), "mira", inline=False)
    assert provider.comments
    assert "reply to one of my review comments" in provider.comments[0]


# ── the platform-neutral handler ─────────────────────────────────────────────


async def test_the_finding_comes_from_the_marker_not_from_the_line(monkeypatch) -> None:
    captured: dict[str, Any] = {}

    async def fake_request(provider, pr_info, request, *, config=None):  # noqa: ANN001
        captured["finding_id"] = request.finding_id
        captured["kind"] = request.kind
        captured["actor"] = request.actor
        return RequestOutcome()

    monkeypatch.setattr("mira.autofix.commands.request_fix", fake_request)
    provider = RecordingProvider()
    await handle_fix_command(
        provider,
        await provider.get_pr_info("https://github.com/acme/app/pull/7"),
        actor="alice",
        kind="single",
        original_body=BOT_COMMENT,
    )
    assert captured == {"finding_id": FINDING_ID, "kind": "single", "actor": "alice"}


async def test_a_comment_with_no_marker_produces_a_clear_refusal(monkeypatch) -> None:
    async def fake_request(provider, pr_info, request, *, config=None):  # noqa: ANN001
        return RequestOutcome()

    monkeypatch.setattr("mira.autofix.commands.request_fix", fake_request)
    provider = RecordingProvider(original="just a person talking")
    outcome = await handle_fix_command(
        provider,
        await provider.get_pr_info("https://github.com/acme/app/pull/7"),
        actor="alice",
        kind="single",
        original_body="just a person talking",
    )
    assert outcome.reasons[0].code == ReasonCode.FINDING_NOT_FOUND
    assert provider.comments


async def test_the_reply_threads_under_the_comment_when_it_can(monkeypatch) -> None:
    async def fake_request(provider, pr_info, request, *, config=None):  # noqa: ANN001
        return RequestOutcome()

    monkeypatch.setattr("mira.autofix.commands.request_fix", fake_request)
    provider = RecordingProvider()
    pr_info = await provider.get_pr_info("https://github.com/acme/app/pull/7")

    async def reply(body: str) -> None:
        await provider.reply_to_review_comment(pr_info, 5, body)

    await handle_fix_command(
        provider,
        pr_info,
        actor="alice",
        kind="single",
        original_body=BOT_COMMENT,
        reply=reply,
    )
    assert provider.replies
    assert provider.comments == []


async def test_a_reply_that_cannot_be_posted_does_not_lose_the_jobs(monkeypatch) -> None:
    from mira.autofix.models import AutofixJob

    async def fake_request(provider, pr_info, request, *, config=None):  # noqa: ANN001
        return RequestOutcome(accepted=[AutofixJob(job_key="k", finding_id="f1")])

    monkeypatch.setattr("mira.autofix.commands.request_fix", fake_request)

    class Mute(RecordingProvider):
        async def post_comment(self, pr_info: Any, body: str) -> None:
            raise RuntimeError("comments are down")

    provider = Mute()
    outcome = await handle_fix_command(
        provider,
        await provider.get_pr_info("https://github.com/acme/app/pull/7"),
        actor="alice",
        kind="all",
    )
    assert outcome.accepted  # the durable work survived the silent reply


# ── GitLab and Forgejo ───────────────────────────────────────────────────────


async def test_gitlab_routes_fix_to_the_shared_handler(monkeypatch) -> None:
    from mira.platforms.gitlab import webhook as gl

    provider = RecordingProvider()
    monkeypatch.setattr(gl, "create_provider", lambda platform, token: provider)
    captured: dict[str, Any] = {}

    async def fake_handle(provider_, pr_info, **kwargs):  # noqa: ANN001
        captured.update(kwargs)
        return RequestOutcome()

    monkeypatch.setattr("mira.autofix.commands.handle_fix_command", fake_handle)

    payload = {
        "object_attributes": {
            "note": "@mira fix",
            "id": 1,
            "discussion_id": "d1",
            "position": {"new_path": "src/a.py", "new_line": 3},
        },
        "project": {"path_with_namespace": "acme/app", "web_url": "https://gitlab.com/acme/app"},
        "merge_request": {"iid": 7, "url": "https://gitlab.com/acme/app/-/merge_requests/7"},
        "user": {"username": "alice"},
    }
    await gl.handle_gitlab_note(payload, _auth(), "mira")
    assert captured["kind"] == "single"
    assert captured["actor"] == "alice"
    assert captured["original_body"] == BOT_COMMENT


async def test_forgejo_routes_fix_to_the_shared_handler(monkeypatch) -> None:
    from mira.platforms.forgejo import webhook as fj

    provider = RecordingProvider()
    monkeypatch.setattr(fj, "create_provider", lambda platform, token: provider)
    captured: dict[str, Any] = {}

    async def fake_handle(provider_, pr_info, **kwargs):  # noqa: ANN001
        captured.update(kwargs)
        return RequestOutcome()

    monkeypatch.setattr("mira.autofix.commands.handle_fix_command", fake_handle)

    payload = {
        "action": "created",
        "is_pull": True,
        "repository": {"full_name": "acme/app", "html_url": "https://forge.dev/acme/app"},
        "issue": {"number": 7},
        "comment": {
            "id": 5,
            "body": "@mira fix all",
            "user": {"username": "alice"},
            "in_reply_to_id": 4,
            "path": "src/a.py",
            "line": 3,
        },
    }
    await fj.handle_forgejo_note(payload, _auth(), "mira")
    assert captured["kind"] == "all"
    assert captured["actor"] == "alice"


async def test_every_platform_documents_the_command() -> None:
    from mira.platforms.handlers import _help_message

    body = _help_message("mira")
    assert "fix all" in body
    assert "@mira fix" in body
    assert "autofix on" in body
