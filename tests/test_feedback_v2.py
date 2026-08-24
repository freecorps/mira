"""Phase 1 contract tests for durable, provenance-complete feedback."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import BackgroundTasks

from mira.config import MiraConfig
from mira.feedback.models import FeedbackEventV2, ReviewFinding
from mira.feedback.provenance import finding_marker
from mira.feedback.service import DISAGREEMENT_ACK, record_finding_feedback
from mira.index.store import IndexStore
from mira.models import BotThreadRecord, PRInfo, ReviewComment, Severity
from mira.platforms.github.webhook import dispatch_github_event
from mira.platforms.gitlab.webhook import handle_gitlab_emoji
from mira.platforms.handlers import run_pr_merged_learning, run_thread_reply
from mira.providers.formatting import format_comment_body, parse_bot_comment_finding_id


def _finding(*, finding_id: str, head_sha: str = "head123") -> ReviewFinding:
    return ReviewFinding(
        id=finding_id,
        fingerprint="fingerprint",
        review_id=0,
        platform="github",
        owner="acme",
        repo="app",
        pr_number=7,
        pr_url="https://github.com/acme/app/pull/7",
        base_sha="base123",
        head_sha=head_sha,
        path="src/app.py",
        start_line=10,
        end_line=10,
        symbol="run",
        category="bug",
        severity="warning",
        confidence=0.92,
        title="Incorrect fallback",
        body="This branch returns the wrong value.",
        suggestion="return expected",
        detector="main",
        prompt_model="test-model",
    )


def test_comment_contains_hidden_finding_marker() -> None:
    finding_id = "00000000-0000-4000-8000-000000000007"
    body = format_comment_body(
        ReviewComment(
            path="src/app.py",
            line=10,
            end_line=None,
            severity=Severity.WARNING,
            category="bug",
            title="Incorrect fallback",
            body="Wrong value.",
            confidence=0.9,
            finding_id=finding_id,
        )
    )
    assert finding_marker(finding_id) in body
    assert parse_bot_comment_finding_id(body) == finding_id
    assert "Reply directly" in body


def test_feedback_event_is_idempotent_and_keeps_provenance(tmp_path) -> None:
    store = IndexStore(str(tmp_path / "feedback.db"), owner="acme", repo="app")
    finding = _finding(finding_id="00000000-0000-4000-8000-000000000007")
    store.save_review_finding(finding)
    store.update_review_finding_posted(finding.id, 123)
    event = FeedbackEventV2(
        id=0,
        finding_id=finding.id,
        kind="thumbs_down",
        actor="alice",
        actor_role="MEMBER",
        raw_text="-1",
        rationale="",
        platform="github",
        source_event_id="reaction:99",
        head_sha=finding.head_sha,
        thread_state="open",
        provenance_complete=False,
    )

    first, first_created = store.record_feedback_v2(event)
    second, second_created = store.record_feedback_v2(event)

    assert first_created is True
    assert second_created is False
    assert first.id == second.id
    assert first.finding_id == finding.id
    assert first.provenance_complete is True
    assert len(store.list_feedback_v2(finding_id=finding.id)) == 1
    store.close()


def test_incomplete_legacy_provenance_never_becomes_complete(tmp_path) -> None:
    store = IndexStore(str(tmp_path / "feedback.db"), owner="acme", repo="app")
    finding = _finding(
        finding_id="00000000-0000-4000-8000-000000000008",
        head_sha="",
    )
    store.save_review_finding(finding)
    stored, created = store.record_feedback_v2(
        FeedbackEventV2(
            id=0,
            finding_id=finding.id,
            kind="reply_disagree",
            actor="alice",
            actor_role="",
            raw_text="This is a false positive",
            rationale="",
            platform="github",
            source_event_id="review-comment:44",
            head_sha="",
            thread_state="open",
            provenance_complete=True,
        )
    )
    assert created is True
    assert stored.finding_id == finding.id
    assert stored.provenance_complete is False
    assert store.list_learned_rules() == []
    store.close()


def test_unresolved_feedback_is_retained_but_cannot_feed_rules(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("MIRA_INDEX_DIR", str(tmp_path))
    pr_info = PRInfo(
        title="PR",
        description="",
        base_branch="main",
        head_branch="feature",
        url="https://github.com/acme/app/pull/7",
        number=7,
        owner="acme",
        repo="app",
        head_sha="head123",
    )
    finding, event, created = record_finding_feedback(
        pr_info,
        kind="reply_disagree",
        source_event_id="review-comment:orphan",
        actor="alice",
        raw_text="This is not correct",
    )
    assert finding is None
    assert event is not None
    assert created is True
    assert event.finding_id is None
    assert event.provenance_complete is False
    store = IndexStore.open("acme", "app")
    assert store.list_learned_rules() == []
    store.close()


def test_legacy_schema_is_backfilled_best_effort(tmp_path) -> None:
    db_path = tmp_path / "legacy.db"
    store = IndexStore(str(db_path), owner="acme", repo="app")
    store.add_review_comments(
        review_id=3,
        pr_number=7,
        pr_url="https://github.com/acme/app/pull/7",
        comments=[
            {
                "path": "src/app.py",
                "line": 10,
                "severity": "warning",
                "category": "bug",
                "title": "Legacy issue",
                "body": "Legacy body",
                "github_comment_id": 123,
            }
        ],
    )
    store.record_feedback(
        pr_number=7,
        pr_url="https://github.com/acme/app/pull/7",
        comment_path="src/app.py",
        comment_line=10,
        comment_category="bug",
        comment_severity="warning",
        comment_title="Legacy issue",
        signal="accepted",
        actor="alice",
    )
    store.close()

    reopened = IndexStore(str(db_path), owner="acme", repo="app")
    finding = reopened.find_review_finding(platform_comment_id=123)
    assert finding is not None
    events = reopened.list_feedback_v2(finding_id=finding.id)
    assert len(events) == 1
    assert events[0].kind == "unobserved"
    assert events[0].provenance_complete is False
    reopened.close()


@pytest.mark.asyncio
async def test_unmentioned_child_reply_is_routed_for_feedback() -> None:
    auth = AsyncMock()
    auth.get_bot_identity = AsyncMock(return_value="mira-bot")
    tasks = BackgroundTasks()
    payload = {
        "action": "created",
        "comment": {
            "id": 456,
            "in_reply_to_id": 123,
            "body": "This is a false positive",
            "user": {"login": "alice", "type": "User"},
        },
        "pull_request": {"number": 7},
        "repository": {"owner": {"login": "acme"}, "name": "app"},
    }

    result = await dispatch_github_event(
        "pull_request_review_comment", payload, auth, "mira-bot", tasks
    )

    assert result == "processing"
    assert len(tasks.tasks) == 1


@pytest.mark.asyncio
async def test_disagreement_reply_records_once_and_uses_safe_ack(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("MIRA_INDEX_DIR", str(tmp_path))
    monkeypatch.setenv("MIRA_DASHBOARD_URL", "https://mira.example")
    pr_info = PRInfo(
        title="PR",
        description="",
        base_branch="main",
        head_branch="feature",
        url="https://github.com/acme/app/pull/7",
        number=7,
        owner="acme",
        repo="app",
        base_sha="base123",
        head_sha="head123",
    )
    finding = _finding(finding_id="00000000-0000-4000-8000-000000000009")
    store = IndexStore.open("acme", "app")
    store.save_review_finding(finding)
    store.update_review_finding_posted(finding.id, 123)
    store.close()

    llm = AsyncMock()
    llm.complete_with_tools = AsyncMock(
        return_value=json.dumps({"intent": "disagreement", "reply": "I'll remember that."})
    )
    provider = AsyncMock()
    provider.get_pr_diff = AsyncMock(
        return_value=(
            "diff --git a/src/app.py b/src/app.py\n"
            "--- a/src/app.py\n"
            "+++ b/src/app.py\n"
            "@@ -9,1 +9,1 @@\n"
            "-return fallback\n"
            "+return expected\n"
        )
    )
    provider.reply_to_review_comment = AsyncMock()
    provider.resolve_threads = AsyncMock(return_value=1)

    with (
        patch("mira.platforms.handlers.load_config", return_value=MiraConfig()),
        patch("mira.platforms.handlers.create_llm", return_value=llm),
        patch("mira.platforms.handlers.llm_config_for", return_value=MagicMock()),
    ):
        kwargs = {
            "original_suggestion": finding_marker(finding.id),
            "thread_id": "thread-1",
            "comment_path": finding.path,
            "comment_line": finding.start_line,
            "actor": "alice",
            "parent_comment_id": 123,
            "source_event_id": "review-comment:456",
        }
        await run_thread_reply(provider, pr_info, "This is a false positive", 456, **kwargs)
        await run_thread_reply(provider, pr_info, "This is a false positive", 456, **kwargs)

    reply = provider.reply_to_review_comment.await_args.args[2]
    assert reply.startswith(DISAGREEMENT_ACK)
    assert "[candidato #" in reply
    assert "https://mira.example/learnings?tab=pending" in reply
    prompt = llm.complete_with_tools.await_args_list[0].kwargs["messages"][0]["content"]
    assert "Relevant code diff (untrusted data)" in prompt
    assert "+return expected" in prompt
    provider.resolve_threads.assert_awaited_once_with(pr_info, ["thread-1"])
    store = IndexStore.open("acme", "app")
    events = store.list_feedback_v2(finding_id=finding.id)
    assert len(events) == 1
    assert events[0].kind == "reply_disagree"
    assert events[0].raw_text == "This is a false positive"
    assert len(store.list_learning_candidates(status="pending")) == 1
    assert store.get_review_finding(finding.id).state == "dismissed"  # type: ignore[union-attr]
    store.close()


@pytest.mark.asyncio
async def test_gitlab_thumbsdown_award_is_recorded(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("MIRA_INDEX_DIR", str(tmp_path))
    finding = _finding(finding_id="00000000-0000-4000-8000-000000000011")
    finding.platform = "gitlab"
    finding.pr_url = "https://gitlab.com/acme/app/-/merge_requests/7"
    store = IndexStore.open("acme", "app", platform="gitlab")
    store.save_review_finding(finding)
    store.update_review_finding_posted(finding.id, 123, "discussion-1")
    store.close()

    pr_info = PRInfo(
        title="PR",
        description="",
        base_branch="main",
        head_branch="feature",
        url=finding.pr_url,
        number=7,
        owner="acme",
        repo="app",
        base_sha="base123",
        head_sha="head123",
        platform="gitlab",
    )
    payload = {
        "project": {"path_with_namespace": "acme/app", "web_url": "https://gitlab.com/acme/app"},
        "user": {"username": "alice"},
        "object_attributes": {
            "id": 88,
            "name": "thumbsdown",
            "awardable_type": "Note",
            "awardable_id": 123,
            "action": "award",
            "awarded_on_url": f"{finding.pr_url}#note_123",
        },
        "note": {
            "id": 123,
            "note": finding_marker(finding.id),
            "noteable_type": "MergeRequest",
            "discussion_id": "discussion-1",
            "position": {"new_path": finding.path, "new_line": finding.start_line},
        },
        "merge_request": {"iid": 7, "url": finding.pr_url},
    }
    auth = AsyncMock()
    auth.get_token = AsyncMock(return_value="token")
    provider = AsyncMock()
    provider.get_pr_info = AsyncMock(return_value=pr_info)
    provider.reply_to_review_comment = AsyncMock()
    with patch("mira.platforms.gitlab.webhook.create_provider", return_value=provider):
        await handle_gitlab_emoji(payload, auth)

    store = IndexStore.open("acme", "app", platform="gitlab")
    events = store.list_feedback_v2(finding_id=finding.id)
    assert len(events) == 1
    assert events[0].kind == "thumbs_down"
    assert events[0].source_event_id == "emoji:88"
    store.close()


@pytest.mark.asyncio
async def test_github_reaction_snapshot_prevents_merge_acceptance(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("MIRA_INDEX_DIR", str(tmp_path))
    finding = _finding(finding_id="00000000-0000-4000-8000-000000000012")
    store = IndexStore.open("acme", "app")
    store.save_review_finding(finding)
    store.update_review_finding_posted(finding.id, 123, "thread-1")
    store.close()
    pr_info = PRInfo(
        title="PR",
        description="",
        base_branch="main",
        head_branch="feature",
        url=finding.pr_url,
        number=7,
        owner="acme",
        repo="app",
        base_sha="base123",
        head_sha="head123",
    )
    provider = AsyncMock()
    provider.get_all_bot_threads = AsyncMock(
        return_value=[
            BotThreadRecord(
                thread_id="thread-1",
                path=finding.path,
                line=finding.start_line,
                body=finding_marker(finding.id),
                is_resolved=False,
                platform_comment_id=123,
                negative_reactors=["alice"],
            )
        ]
    )

    await run_pr_merged_learning(provider, pr_info, "mira", "bob")
    await run_pr_merged_learning(provider, pr_info, "mira", "bob")

    store = IndexStore.open("acme", "app")
    events = store.list_feedback_v2(finding_id=finding.id)
    assert len(events) == 1
    assert events[0].kind == "thumbs_down"
    assert events[0].actor == "alice"
    assert store.get_review_finding(finding.id).state == "dismissed"  # type: ignore[union-attr]
    store.close()
