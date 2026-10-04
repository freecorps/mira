"""Tests for the `full review`, `resolve` and `config` chat commands and for
review profiles, across GitHub, GitLab and Forgejo dispatch."""

from __future__ import annotations

import hashlib
import hmac
import json
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from mira.config import FilterConfig, LLMConfig, MiraConfig, ReviewConfig, load_config
from mira.exceptions import ConfigError
from mira.models import PRInfo, ReviewResult, UnresolvedThread
from mira.platforms.chat_commands import (
    CONFIG_KEYWORDS,
    FULL_REVIEW_KEYWORDS,
    RESOLVE_KEYWORDS,
    effective_config_view,
    is_review_request,
    normalize_command,
    render_config_reply,
)
from mira.platforms.handlers import (
    _HELP_KEYWORDS,
    _REJECT_KEYWORDS,
    _REVIEW_KEYWORDS,
    _REVIEW_REST_KEYWORDS,
    _help_message,
    run_pr_command,
)

BOT = "mira-bot"
NAMES = [BOT]


def _pr_info(number: int = 42) -> PRInfo:
    return PRInfo(
        title="t",
        description="",
        base_branch="main",
        head_branch="f",
        url=f"https://github.com/o/r/pull/{number}",
        number=number,
        owner="o",
        repo="r",
        head_sha="HEAD_SHA",
    )


# ── Keyword parsing ──────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Full   Review", "full review"),
        ("  full-review  ", "full-review"),
        ("CONFIG", "config"),
        ("resolve\n", "resolve"),
    ],
)
def test_normalize_command(text: str, expected: str) -> None:
    assert normalize_command(text) == expected


def test_new_keywords_do_not_collide_with_existing_ones() -> None:
    existing = _REVIEW_KEYWORDS | _REVIEW_REST_KEYWORDS | _HELP_KEYWORDS
    for new in (FULL_REVIEW_KEYWORDS, RESOLVE_KEYWORDS, CONFIG_KEYWORDS):
        assert not (new & existing)
    # Inline `resolve` keeps meaning reject; the PR-level command reuses the word.
    assert "resolve" in _REJECT_KEYWORDS


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        ("@mira-bot review", True),
        ("@mira-bot full review", True),
        ("@mira-bot Full-Review", True),
        ("@mira-bot full explanation of this please", False),
        ("@mira-bot pause", False),
        # A question that starts with "review" is dispatched as a question,
        # so it must not get past the author filter as a review request.
        ("@mira-bot review please, and explain the cache change", False),
        ("@mira-bot review this pr", True),
    ],
)
def test_is_review_request(body: str, expected: bool) -> None:
    assert is_review_request(body, NAMES) is expected


def test_help_lists_new_commands_and_keeps_existing_rows() -> None:
    text = _help_message(BOT)
    for row in (
        "`@mira-bot full review`",
        "`@mira-bot resolve`",
        "`@mira-bot config`",
        "`@mira-bot review`",
        "`@mira-bot review-rest`",
        "`@mira-bot pause`",
        "`@mira-bot resume`",
        "`@mira-bot fix all`",
        "`@mira-bot help`",
        "`@mira-bot reject`",
    ):
        assert row in text


# ── run_pr_command dispatch ──────────────────────────────────────────────────


def _provider() -> AsyncMock:
    provider = AsyncMock()
    provider.get_pr_info = AsyncMock(return_value=_pr_info())
    provider.post_comment = AsyncMock()
    return provider


@pytest.mark.parametrize(
    ("question", "full"),
    [("full review", True), ("Full-Review", True), ("review", False)],
)
@patch("mira.platforms.handlers.ReviewEngine")
@patch("mira.platforms.handlers.create_llm")
@patch("mira.platforms.handlers.load_config")
async def test_full_review_builds_a_non_incremental_engine(
    mock_config: MagicMock,
    mock_llm: MagicMock,
    mock_engine_cls: MagicMock,
    question: str,
    full: bool,
) -> None:
    mock_config.return_value = MiraConfig()
    engine = AsyncMock()
    engine.review_pr = AsyncMock(return_value=ReviewResult(summary="ok"))
    mock_engine_cls.return_value = engine

    await run_pr_command(
        _provider(), "o", "r", 901, "https://github.com/o/r/pull/901", question, "alice", BOT
    )

    engine.review_pr.assert_awaited_once()
    assert mock_engine_cls.call_args.kwargs["full_review"] is full


@patch("mira.platforms.handlers.set_finding_state")
@patch("mira.platforms.handlers.create_learning_candidate_for_feedback")
@patch("mira.platforms.handlers.record_finding_feedback")
@patch("mira.platforms.handlers.create_llm")
@patch("mira.platforms.handlers.load_config")
async def test_resolve_closes_bot_threads_without_feedback(
    mock_config: MagicMock,
    mock_llm: MagicMock,
    mock_record: MagicMock,
    mock_candidate: MagicMock,
    mock_state: MagicMock,
) -> None:
    mock_config.return_value = MiraConfig()
    provider = _provider()
    provider.get_unresolved_bot_threads = AsyncMock(
        return_value=[
            UnresolvedThread(thread_id="T1", path="a.py", line=1, body="x"),
            UnresolvedThread(thread_id="T2", path="b.py", line=2, body="y"),
        ]
    )
    provider.resolve_threads = AsyncMock(return_value=2)

    await run_pr_command(
        provider, "o", "r", 42, "https://github.com/o/r/pull/42", "resolve", "alice", BOT
    )

    provider.resolve_threads.assert_awaited_once()
    assert provider.resolve_threads.call_args.args[1] == ["T1", "T2"]
    posted = provider.post_comment.call_args.args[1]
    assert "Resolved 2 open Mira review threads" in posted
    assert "not recorded as false positives" in posted
    mock_record.assert_not_called()
    mock_candidate.assert_not_called()
    mock_state.assert_not_called()


@patch("mira.platforms.handlers.create_llm")
@patch("mira.platforms.handlers.load_config")
async def test_resolve_with_nothing_open(mock_config: MagicMock, mock_llm: MagicMock) -> None:
    mock_config.return_value = MiraConfig()
    provider = _provider()
    provider.get_unresolved_bot_threads = AsyncMock(return_value=[])
    await run_pr_command(provider, "o", "r", 42, "u", "resolve", "alice", BOT)
    provider.resolve_threads.assert_not_called()
    assert "no open Mira review threads" in provider.post_comment.call_args.args[1]


@patch("mira.platforms.handlers.create_llm")
@patch("mira.platforms.handlers.load_config")
async def test_resolve_reports_a_platform_that_cannot_resolve(
    mock_config: MagicMock, mock_llm: MagicMock
) -> None:
    """Forgejo's resolve_threads is a no-op returning 0; say so honestly."""
    mock_config.return_value = MiraConfig()
    provider = _provider()
    provider.get_unresolved_bot_threads = AsyncMock(
        return_value=[UnresolvedThread(thread_id="9", path="a.py", line=1, body="x")]
    )
    provider.resolve_threads = AsyncMock(return_value=0)
    await run_pr_command(provider, "o", "r", 42, "u", "resolve", "alice", BOT)
    assert "could not resolve" in provider.post_comment.call_args.args[1]


# ── config ───────────────────────────────────────────────────────────────────

_GH_TOKEN = "ghp_" + "A" * 36


def _secret_config() -> MiraConfig:
    return MiraConfig(
        llm=LLMConfig(
            model="anthropic/claude-sonnet-4-6",
            base_url="https://user:hunter2secret@llm.internal.example/v1",
            api_key_env="SUPER_SECRET_KEY_ENV",
        ),
        filter=FilterConfig(exclude_patterns=["*.lock", _GH_TOKEN]),
        review=ReviewConfig(profile="chill"),
    )


def test_config_view_keeps_review_sections_and_drops_secrets() -> None:
    view = effective_config_view(_secret_config())
    assert set(view) == {"llm", "review", "filter"}
    assert view["llm"]["model"] == "anthropic/claude-sonnet-4-6"
    for dropped in ("base_url", "api_key_env", "endpoint", "fallbacks"):
        assert dropped not in view["llm"]
    # Token *budgets* are counts, not credentials.
    assert "context_token_budget" in view["review"]
    assert "agent_token_budget" in view["review"]


def test_config_reply_redacts_secret_values() -> None:
    reply = render_config_reply(_secret_config(), "alice")
    assert "```yaml" in reply
    assert "profile: chill" in reply
    assert "hunter2secret" not in reply
    assert "SUPER_SECRET_KEY_ENV" not in reply
    assert _GH_TOKEN not in reply
    assert "[REDACTED:github-token]" in reply


@pytest.mark.parametrize("verb", ["config", "Configuration"])
@patch("mira.platforms.handlers.create_llm")
@patch("mira.platforms.handlers.load_config")
async def test_config_command_posts_redacted_yaml(
    mock_config: MagicMock, mock_llm: MagicMock, verb: str
) -> None:
    mock_config.return_value = _secret_config()
    llm = AsyncMock()
    mock_llm.return_value = llm
    provider = _provider()
    await run_pr_command(provider, "o", "r", 42, "u", verb, "alice", BOT)
    provider.post_comment.assert_awaited_once()
    posted = provider.post_comment.call_args.args[1]
    assert "```yaml" in posted
    assert "hunter2secret" not in posted
    llm.complete.assert_not_awaited()


# ── Review profiles ──────────────────────────────────────────────────────────


def test_balanced_profile_is_the_unchanged_default() -> None:
    cfg = MiraConfig()
    assert cfg.review.profile == "balanced"
    assert cfg.filter.confidence_threshold == 0.7
    assert cfg.filter.max_comments == 5
    assert cfg.filter.min_severity == "nitpick"
    assert cfg.review.critique_plausible_min_confidence == 0.7


def test_chill_profile_preset() -> None:
    cfg = MiraConfig.model_validate({"review": {"profile": "chill"}})
    assert cfg.filter.confidence_threshold == 0.8
    assert cfg.filter.max_comments == 3
    assert cfg.filter.min_severity == "suggestion"
    assert cfg.review.critique_plausible_min_confidence == 0.8


def test_assertive_profile_preset() -> None:
    cfg = MiraConfig.model_validate({"review": {"profile": "ASSERTIVE"}})
    assert cfg.review.profile == "assertive"
    assert cfg.filter.confidence_threshold == 0.55
    assert cfg.filter.max_comments == 10
    assert cfg.filter.min_severity == "nitpick"
    assert cfg.review.critique_plausible_min_confidence == 0.6


def test_explicit_knob_wins_over_profile() -> None:
    cfg = MiraConfig.model_validate(
        {
            "review": {"profile": "chill", "critique_plausible_min_confidence": 0.5},
            "filter": {"max_comments": 7},
        }
    )
    assert cfg.filter.max_comments == 7  # explicit
    assert cfg.review.critique_plausible_min_confidence == 0.5  # explicit
    assert cfg.filter.confidence_threshold == 0.8  # preset
    # A preset is not something the user wrote.
    assert cfg.filter.model_fields_set == {"max_comments"}


def test_explicit_knob_wins_through_load_config(tmp_path: Path) -> None:
    path = tmp_path / ".mira.yaml"
    path.write_text(
        "review:\n  profile: assertive\nfilter:\n  confidence_threshold: 0.9\n",
        encoding="utf-8",
    )
    cfg = load_config(config_path=path, use_db_overrides=False)
    assert cfg.filter.confidence_threshold == 0.9
    assert cfg.filter.max_comments == 10


def test_unknown_profile_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / ".mira.yaml"
    path.write_text("review:\n  profile: grumpy\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="review.profile"):
        load_config(config_path=path, use_db_overrides=False)


@pytest.mark.parametrize(
    ("profile", "needle", "present"),
    [
        ("chill", "Review profile: chill", True),
        ("assertive", "Review profile: assertive", True),
        ("balanced", "Review profile:", False),
    ],
)
def test_profile_tone_hint_in_prompt(profile: str, needle: str, present: bool) -> None:
    from mira.llm.prompts.review import build_review_prompt

    cfg = MiraConfig.model_validate({"review": {"profile": profile}})
    system = build_review_prompt(files=[], config=cfg)[0]["content"]
    assert (needle in system) is present


# ── Engine: full review bypasses the incremental diff ────────────────────────


async def test_full_review_skips_incremental_compare(monkeypatch: pytest.MonkeyPatch) -> None:
    from mira.core.engine import ReviewEngine
    from mira.models import BotThreadRecord

    provider = MagicMock()
    provider.get_pr_info = AsyncMock(return_value=_pr_info(1))
    provider.get_pr_diff = AsyncMock(return_value="FULL")
    provider.get_compare_diff = AsyncMock(return_value="INCR")
    provider.get_unresolved_bot_threads = AsyncMock(return_value=[])
    provider.get_all_bot_threads = AsyncMock(
        return_value=[
            BotThreadRecord(thread_id="t", path="a.py", line=1, body="x", is_resolved=False)
        ]
    )
    provider.find_bot_comment = AsyncMock(return_value=None)
    provider.post_comment = AsyncMock()
    provider.update_comment = AsyncMock()
    provider.resolve_outdated_review_threads = AsyncMock(return_value=0)

    db = MagicMock()
    db.get_last_reviewed_sha = MagicMock(return_value="OLD_SHA")
    db.get_repo = MagicMock(return_value=None)
    monkeypatch.setattr("mira.dashboard.api._app_db", db)

    captured: dict[str, Any] = {}

    async def fake_internal(self: Any, diff_text: str, **kwargs: Any) -> ReviewResult:
        captured["diff_text"] = diff_text
        captured["review_round"] = kwargs.get("review_round")
        return ReviewResult(comments=[], summary="")

    monkeypatch.setattr(ReviewEngine, "_review_diff_internal", fake_internal)

    engine = ReviewEngine(
        config=MiraConfig(), llm=AsyncMock(), provider=provider, bot_name="mira", full_review=True
    )
    await engine.review_pr("https://github.com/o/r/pull/1")

    provider.get_compare_diff.assert_not_called()
    assert captured["diff_text"] == "FULL"
    assert captured["review_round"] == 1


# ── Platform dispatch ────────────────────────────────────────────────────────


@patch("mira.platforms.github.webhook.create_provider")
async def test_github_inline_resolve_still_rejects(mock_provider_cls: MagicMock) -> None:
    """On an inline review comment `resolve` keeps meaning reject."""
    from mira.platforms.github.webhook import handle_thread_reject

    auth = AsyncMock()
    auth.get_installation_token = AsyncMock(return_value="tok")
    auth.get_bot_identity = AsyncMock(return_value=BOT)
    provider = AsyncMock()
    provider.get_thread_id_for_comment = AsyncMock(return_value="PRRT_1")
    provider.resolve_threads = AsyncMock(return_value=1)
    mock_provider_cls.return_value = provider
    payload = {
        "installation": {"id": 1},
        "comment": {
            "id": 5,
            "body": f"@{BOT} resolve",
            "node_id": "N1",
            "user": {"login": "alice"},
            "in_reply_to_id": 4,
        },
        "pull_request": {"number": 42},
        "repository": {"owner": {"login": "o"}, "name": "r"},
    }
    with (
        patch(
            "mira.platforms.github.webhook.record_finding_feedback",
            return_value=(None, None, True),
        ) as rec,
        patch("mira.platforms.handlers.run_pr_command", new=AsyncMock()) as rpc,
    ):
        await handle_thread_reject(payload, auth, BOT)
    rec.assert_called_once()
    assert rec.call_args.kwargs["kind"] == "dismissed"
    rpc.assert_not_called()


@patch("mira.platforms.github.webhook.create_provider")
@patch("mira.platforms.handlers.create_llm")
@patch("mira.platforms.handlers.load_config")
async def test_github_pr_level_resolve_resolves_all(
    mock_config: MagicMock, mock_llm: MagicMock, mock_provider_cls: MagicMock
) -> None:
    from mira.platforms.github.webhook import handle_comment

    mock_config.return_value = MiraConfig()
    provider = _provider()
    provider.get_unresolved_bot_threads = AsyncMock(
        return_value=[UnresolvedThread(thread_id="T1", path="a.py", line=1, body="x")]
    )
    provider.resolve_threads = AsyncMock(return_value=1)
    mock_provider_cls.return_value = provider
    auth = AsyncMock()
    auth.get_installation_token = AsyncMock(return_value="tok")
    auth.get_bot_identity = AsyncMock(return_value=BOT)
    payload = {
        "installation": {"id": 1},
        "comment": {"body": f"@{BOT} resolve", "user": {"login": "alice"}},
        "issue": {"number": 7, "pull_request": {"url": "x"}},
        "repository": {"owner": {"login": "o"}, "name": "r"},
    }
    with patch("mira.platforms.handlers.record_finding_feedback") as rec:
        await handle_comment(payload, auth, BOT)
    provider.resolve_threads.assert_awaited_once()
    assert "Resolved 1 open Mira review thread." in provider.post_comment.call_args.args[1]
    rec.assert_not_called()


def _gitlab_auth() -> Any:
    from mira.platforms.gitlab.auth import GitLabTokenAuth

    auth = GitLabTokenAuth("tok")
    auth.get_bot_identity = AsyncMock(return_value=BOT)  # type: ignore[method-assign]
    return auth


def _gitlab_note(note: str, *, inline: bool) -> dict[str, Any]:
    attrs: dict[str, Any] = {
        "note": note,
        "noteable_type": "MergeRequest",
        "id": 99,
        "discussion_id": "abc123",
    }
    if inline:
        attrs["position"] = {"new_path": "a.py", "new_line": 4}
    return {
        "object_attributes": attrs,
        "merge_request": {"iid": 7, "url": "https://gitlab.com/g/p/-/merge_requests/7"},
        "project": {"path_with_namespace": "g/p", "web_url": "https://gitlab.com/g/p"},
        "user": {"username": "alice"},
    }


@pytest.mark.parametrize(
    "note", ["@mira-bot resolve", "@mira-bot full review", "@mira-bot config"]
)
async def test_gitlab_pr_level_commands_reach_run_pr_command(note: str) -> None:
    from mira.platforms.gitlab import webhook as gw

    prov = AsyncMock()
    prov.get_pr_info = AsyncMock(return_value=object())
    with (
        patch("mira.platforms.gitlab.webhook.create_provider", return_value=prov),
        patch("mira.platforms.handlers.run_pr_command", new=AsyncMock()) as rpc,
        patch("mira.platforms.gitlab.webhook.record_finding_feedback") as rec,
    ):
        await gw.handle_gitlab_note(_gitlab_note(note, inline=False), _gitlab_auth(), BOT)
    rpc.assert_awaited_once()
    assert rpc.call_args.args[5] == note.removeprefix("@mira-bot ")
    rec.assert_not_called()


async def test_gitlab_inline_resolve_still_rejects() -> None:
    from mira.platforms.gitlab import webhook as gw

    prov = AsyncMock()
    prov.get_pr_info = AsyncMock(return_value=object())
    prov.get_discussion_root_body = AsyncMock(
        return_value="<!-- mira:finding:00000000-0000-4000-8000-000000000001 -->"
    )
    with (
        patch("mira.platforms.gitlab.webhook.create_provider", return_value=prov),
        patch("mira.platforms.handlers.run_pr_command", new=AsyncMock()) as rpc,
        patch(
            "mira.platforms.gitlab.webhook.record_finding_feedback",
            return_value=(None, None, True),
        ) as rec,
    ):
        await gw.handle_gitlab_note(
            _gitlab_note("@mira-bot resolve", inline=True), _gitlab_auth(), BOT
        )
    rec.assert_called_once()
    assert rec.call_args.kwargs["kind"] == "dismissed"
    rpc.assert_not_called()


def _forgejo_auth() -> Any:
    from mira.platforms.forgejo.auth import ForgejoTokenAuth

    auth = ForgejoTokenAuth("tok")
    auth.get_bot_identity = AsyncMock(return_value=BOT)  # type: ignore[method-assign]
    return auth


def _forgejo_comment(body: str, *, inline: bool) -> dict[str, Any]:
    comment: dict[str, Any] = {"body": body, "id": 99, "user": {"username": "alice"}}
    if inline:
        comment.update({"in_reply_to_id": 42, "path": "a.py", "line": 4})
    return {
        "action": "created",
        "is_pull": True,
        "comment": comment,
        "repository": {"full_name": "acme/app", "html_url": "https://forge.example/acme/app"},
        "issue": {"number": 7},
        "sender": {"login": "alice"},
    }


@pytest.mark.parametrize(
    "body", ["@mira-bot resolve", "@mira-bot full-review", "@mira-bot configuration"]
)
async def test_forgejo_pr_level_commands_reach_run_pr_command(body: str) -> None:
    from mira.platforms.forgejo import webhook as fw

    prov = AsyncMock()
    prov.get_pr_info = AsyncMock(return_value=object())
    with (
        patch("mira.platforms.forgejo.webhook.create_provider", return_value=prov),
        patch("mira.platforms.handlers.run_pr_command", new=AsyncMock()) as rpc,
        patch("mira.platforms.forgejo.webhook.record_finding_feedback") as rec,
    ):
        await fw.handle_forgejo_note(_forgejo_comment(body, inline=False), _forgejo_auth(), BOT)
    rpc.assert_awaited_once()
    assert rpc.call_args.args[5] == body.removeprefix("@mira-bot ")
    rec.assert_not_called()


async def test_forgejo_inline_resolve_still_rejects() -> None:
    from mira.platforms.forgejo import webhook as fw

    prov = AsyncMock()
    prov.get_pr_info = AsyncMock(return_value=object())
    prov.get_comment_body = AsyncMock(
        return_value="<!-- mira:finding:00000000-0000-4000-8000-000000000001 -->"
    )
    with (
        patch("mira.platforms.forgejo.webhook.create_provider", return_value=prov),
        patch("mira.platforms.handlers.run_pr_command", new=AsyncMock()) as rpc,
        patch(
            "mira.platforms.forgejo.webhook.record_finding_feedback",
            return_value=(None, None, True),
        ) as rec,
    ):
        await fw.handle_forgejo_note(
            _forgejo_comment("@mira-bot resolve", inline=True), _forgejo_auth(), BOT
        )
    rec.assert_called_once()
    assert rec.call_args.kwargs["kind"] == "dismissed"
    rpc.assert_not_called()


# ── Author filter: full review bypasses it like review does ──────────────────


@pytest.fixture
async def gitlab_client() -> Any:
    from mira.platforms.server import create_app

    app = create_app(
        app_auth=None,
        webhook_secret=None,
        bot_name=BOT,
        gitlab_auth=_gitlab_auth(),
        gitlab_webhook_secret="gl-secret",
    )
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


@pytest.fixture
async def forgejo_client() -> Any:
    from mira.platforms.server import create_app

    app = create_app(
        app_auth=None,
        webhook_secret=None,
        bot_name=BOT,
        forgejo_auth=_forgejo_auth(),
        forgejo_webhook_secret="fj-secret",
    )
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


@pytest.fixture
async def github_client() -> Any:
    from mira.platforms.github.auth import GitHubAppAuth
    from mira.platforms.server import create_app

    auth = GitHubAppAuth(app_id="12345", private_key="fake-key")
    auth.get_bot_identity = AsyncMock(return_value=BOT)  # type: ignore[method-assign]
    app = create_app(app_auth=auth, webhook_secret="gh-secret", bot_name=BOT)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


_BLOCKED = MiraConfig(filter=FilterConfig(blocked_authors=["dependabot"]))


@pytest.mark.parametrize(("verb", "status"), [("full review", "processing"), ("config", "ignored")])
async def test_github_full_review_bypasses_author_filter(
    github_client: AsyncClient, verb: str, status: str
) -> None:
    payload = {
        "action": "created",
        "installation": {"id": 1},
        "comment": {"body": f"@{BOT} {verb}", "user": {"login": "dependabot[bot]"}},
        "issue": {"number": 7, "pull_request": {"url": "x"}},
        "repository": {"owner": {"login": "o"}, "name": "r"},
    }
    raw = json.dumps(payload).encode()
    sig = "sha256=" + hmac.new(b"gh-secret", raw, hashlib.sha256).hexdigest()
    with (
        patch("mira.platforms.github.webhook.load_config", return_value=_BLOCKED),
        patch("mira.platforms.github.webhook.handle_comment", new=AsyncMock()),
    ):
        resp = await github_client.post(
            "/webhook",
            content=raw,
            headers={
                "X-Hub-Signature-256": sig,
                "X-GitHub-Event": "issue_comment",
                "Content-Type": "application/json",
            },
        )
    assert resp.json()["status"] == status


@pytest.mark.parametrize(("verb", "status"), [("full review", "processing"), ("config", "ignored")])
async def test_gitlab_full_review_bypasses_author_filter(
    gitlab_client: AsyncClient, verb: str, status: str
) -> None:
    payload = _gitlab_note(f"@{BOT} {verb}", inline=False)
    payload["object_kind"] = "note"
    payload["user"]["username"] = "dependabot"
    with (
        patch("mira.platforms.gitlab.webhook.load_config", return_value=_BLOCKED),
        patch("mira.platforms.gitlab.webhook.handle_gitlab_note", new=AsyncMock()),
    ):
        resp = await gitlab_client.post(
            "/gitlab/webhook",
            content=json.dumps(payload),
            headers={"X-Gitlab-Token": "gl-secret", "X-Gitlab-Event": "Note Hook"},
        )
    assert resp.json()["status"] == status


@pytest.mark.parametrize(("verb", "status"), [("full-review", "processing"), ("config", "ignored")])
async def test_forgejo_full_review_bypasses_author_filter(
    forgejo_client: AsyncClient, verb: str, status: str
) -> None:
    payload = {
        "action": "created",
        "is_pull": True,
        "comment": {"body": f"@{BOT} {verb}"},
        "sender": {"login": "dependabot[bot]"},
    }
    raw = json.dumps(payload).encode()
    with (
        patch("mira.platforms.forgejo.webhook.load_config", return_value=_BLOCKED),
        patch("mira.platforms.forgejo.webhook.handle_forgejo_note", new=AsyncMock()),
    ):
        resp = await forgejo_client.post(
            "/forgejo/webhook",
            content=raw,
            headers={
                "X-Forgejo-Event": "issue_comment",
                "X-Forgejo-Signature": hmac.new(b"fj-secret", raw, hashlib.sha256).hexdigest(),
            },
        )
    assert resp.json()["status"] == status
