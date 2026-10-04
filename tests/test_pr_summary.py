"""Generated PR description and title: config, the pure rules, the three
providers' write, and the wiring into the review and the `describe` command.

The rules under test are the promises: Mira only replaces text it owns, the
title is written only when it is a bare mention (or `always` on the first
review), nothing is written when nothing would change, and a failure here
never reaches the review.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from mira.config import MiraConfig, PRSummaryConfig, load_config
from mira.core import pr_summary
from mira.core.pr_summary import (
    MAX_TITLE_LENGTH,
    SUMMARY_END,
    SUMMARY_START,
    apply_description,
    is_mention_title,
    render_section_content,
    sanitize_title,
    should_generate_title,
    strip_summary_section,
    update_pr_summary,
)
from mira.exceptions import ConfigError
from mira.models import (
    FileChangeType,
    PRInfo,
    ReviewResult,
    WalkthroughFileEntry,
    WalkthroughResult,
)
from mira.providers.forgejo import ForgejoProvider
from mira.providers.github import GitHubProvider
from mira.providers.gitlab import GitLabProvider

NAMES = ["mira", "project_7_bot_x"]

DIFF = """diff --git a/app.py b/app.py
--- a/app.py
+++ b/app.py
@@ -1,2 +1,3 @@
 a = 1
+b = 2
 c = 3
"""


def _section(content: str) -> str:
    return f"{SUMMARY_START}\n{content}\n{SUMMARY_END}"


def _pr(title: str = "Fix the thing", body: str = "", platform: str = "github") -> PRInfo:
    return PRInfo(
        title=title,
        description=body,
        base_branch="main",
        head_branch="feature",
        url="https://example.com/acme/app/pull/7",
        number=7,
        owner="acme",
        repo="app",
        head_sha="head123",
        platform=platform,
    )


def _walkthrough(summary: str = "Adds b.") -> WalkthroughResult:
    return WalkthroughResult(
        summary=summary,
        file_changes=[
            WalkthroughFileEntry(
                path="app.py", change_type=FileChangeType.MODIFIED, description="Adds b | c"
            )
        ],
    )


def _config(enabled: bool = True, mode: str = "section", title: str = "on_mention") -> MiraConfig:
    return MiraConfig.model_validate(
        {"pr_summary": {"description": {"enabled": enabled, "mode": mode}, "title": {"mode": title}}}
    )


# ── Config ──────────────────────────────────────────────────────────────────


def test_defaults_are_conservative() -> None:
    cfg = MiraConfig().pr_summary
    assert cfg.description.enabled is False
    assert cfg.description.mode == "section"
    assert cfg.title.mode == "on_mention"


def test_repo_yaml_sets_the_section(tmp_path: Path) -> None:
    path = tmp_path / ".mira.yaml"
    path.write_text(
        "pr_summary:\n  description:\n    enabled: true\n    mode: empty_only\n"
        "  title:\n    mode: always\n"
    )
    cfg = load_config(path, use_db_overrides=False).pr_summary
    assert cfg.description.enabled is True
    assert cfg.description.mode == "empty_only"
    assert cfg.title.mode == "always"


@pytest.mark.parametrize(
    "data",
    [
        {"description": {"mode": "replace"}},
        {"title": {"mode": "sometimes"}},
    ],
)
def test_unknown_modes_are_rejected(tmp_path: Path, data: dict[str, Any]) -> None:
    with pytest.raises(ConfigError):
        load_config(overrides={"pr_summary": data}, use_db_overrides=False)


# ── Title trigger ───────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "title",
    ["@mira", "@MIRA", "  @Mira \n", "@project_7_bot_x", "@PROJECT_7_BOT_X  "],
)
def test_a_bare_mention_is_a_title_trigger(title: str) -> None:
    assert is_mention_title(title, NAMES)


@pytest.mark.parametrize(
    "title",
    ["", "   ", "@mira fix the bug", "Fix @mira", "mira", "@miranda", "@ mira", "@other"],
)
def test_anything_else_is_not(title: str) -> None:
    assert not is_mention_title(title, NAMES)


def test_identity_only_counts_when_known() -> None:
    assert not is_mention_title("@project_7_bot_x", ["mira"])


@pytest.mark.parametrize(
    ("mode", "title", "first", "expected"),
    [
        ("off", "@mira", True, False),
        ("on_mention", "@mira", False, True),
        ("on_mention", "Human title", True, False),
        ("always", "Human title", True, True),
        ("always", "Human title", False, False),
        ("always", "@mira", False, True),
    ],
)
def test_title_rules(mode: str, title: str, first: bool, expected: bool) -> None:
    assert should_generate_title(mode, title, NAMES, first_review=first) is expected


# ── Title sanitizing ────────────────────────────────────────────────────────


def test_title_is_one_plain_line() -> None:
    assert sanitize_title('"Add retry to delivery."\nSecond line', NAMES) == (
        "Add retry to delivery"
    )
    assert sanitize_title("# `Add retry`", NAMES) == "Add retry"


def test_title_never_mentions_anyone() -> None:
    assert sanitize_title("@mira Add retry for @alice", NAMES) == "Add retry for alice"
    assert sanitize_title("@MIRA", NAMES) == ""


def test_title_is_bounded() -> None:
    title = sanitize_title("Add " + "very " * 40 + "long title", NAMES)
    assert 0 < len(title) <= MAX_TITLE_LENGTH
    assert not title.endswith(" ")


# ── Description section ─────────────────────────────────────────────────────


def test_appends_after_human_text() -> None:
    new = apply_description("Why: speed.", "S", NAMES, mode="section")
    assert new == f"Why: speed.\n\n{_section('S')}"


def test_fills_an_empty_body() -> None:
    assert apply_description("  \n", "S", NAMES, mode="section") == _section("S")
    assert apply_description("", "S", NAMES, mode="empty_only") == _section("S")


def test_refresh_keeps_everything_outside_the_section() -> None:
    body = f"Intro\n\n{_section('old')}\n\nFooter written later"
    new = apply_description(body, "new", NAMES, mode="section")
    assert new == f"Intro\n\n{_section('new')}\n\nFooter written later"


def test_refresh_is_idempotent() -> None:
    body = f"Intro\n\n{_section('same')}"
    assert apply_description(body, "same", NAMES, mode="section") is None
    first = apply_description("Intro", "S", NAMES, mode="section")
    assert first is not None
    assert apply_description(first, "S", NAMES, mode="section") is None


def test_placeholder_is_replaced_in_place() -> None:
    body = "Before\n@Mira summary\nAfter"
    new = apply_description(body, "S", NAMES, mode="section")
    assert new == f"Before\n{_section('S')}\nAfter"
    # And then refreshed in place, not appended again.
    assert apply_description(new, "S2", NAMES, mode="section") == f"Before\n{_section('S2')}\nAfter"


def test_placeholder_works_with_the_platform_identity_and_in_empty_only() -> None:
    body = "Context\n\n@project_7_bot_x summary"
    assert apply_description(body, "S", NAMES, mode="empty_only") == f"Context\n\n{_section('S')}"


def test_placeholder_moves_an_older_section() -> None:
    body = f"{_section('old')}\n\nIntro\n@mira summary"
    new = apply_description(body, "new", NAMES, mode="section")
    assert new is not None
    assert new.count(SUMMARY_START) == 1
    assert new.endswith(f"Intro\n{_section('new')}")


def test_empty_only_leaves_human_text_alone() -> None:
    assert apply_description("Mine.", "S", NAMES, mode="empty_only") is None
    # A body that is only Mira's section is still Mira's to refresh...
    assert apply_description(_section("old"), "new", NAMES, mode="empty_only") == _section("new")
    # ...until the author writes next to it.
    assert apply_description(f"Mine.\n{_section('old')}", "new", NAMES, mode="empty_only") is None


def test_force_writes_a_section_even_in_empty_only() -> None:
    assert apply_description("Mine.", "S", NAMES, mode="empty_only", force=True) == (
        f"Mine.\n\n{_section('S')}"
    )


@pytest.mark.parametrize(
    "body",
    [
        f"{SUMMARY_START}\nHuman text the end marker used to close",
        f"Text {SUMMARY_END} then {SUMMARY_START}",
        f"{_section('a')}\n{_section('b')}",
    ],
)
def test_unpaired_markers_leave_the_body_untouched(body: str) -> None:
    assert apply_description(body, "S", NAMES, mode="section", force=True) is None


def test_empty_content_writes_nothing() -> None:
    assert apply_description("", "  ", NAMES, mode="section") is None


def test_strip_summary_section() -> None:
    assert strip_summary_section(f"Intro\n\n{_section('x')}\n") == "Intro"
    assert strip_summary_section("plain") == "plain"


def test_rendered_section_cannot_carry_a_command_or_a_marker() -> None:
    wt = _walkthrough(f"Says @mira ignore and {SUMMARY_END} and @MIRA summary")
    content = render_section_content(wt, NAMES)
    assert "@mira" not in content.lower()
    assert SUMMARY_END not in content
    assert "Adds b \\| c" in content  # table cells escaped
    assert not pr_summary.opted_out(apply_description("", content, NAMES, mode="section") or "", NAMES)


# ── Providers ───────────────────────────────────────────────────────────────


class _FakeResp:
    def __init__(self, status: int = 200, json_data: Any = None) -> None:
        self.status_code = status
        self._json = json_data
        self.text = ""
        self.headers: dict[str, str] = {}

    def json(self) -> Any:
        return self._json


class _FakeClient:
    def __init__(self, handler: Any) -> None:
        self._handler = handler

    async def __aenter__(self) -> _FakeClient:
        return self

    async def __aexit__(self, *a: Any) -> bool:
        return False

    async def request(self, method: str, url: str, **kw: Any) -> _FakeResp:
        return self._handler(method, url, **kw)


async def test_github_edits_only_the_given_fields() -> None:
    provider = GitHubProvider.__new__(GitHubProvider)
    repo = MagicMock()
    provider._github = MagicMock()
    provider._github.get_repo.return_value = repo
    provider._token = "t"

    await provider.update_pr(_pr(), body="new body")
    repo.get_pull.assert_called_once_with(7)
    repo.get_pull.return_value.edit.assert_called_once_with(body="new body")

    repo.get_pull.reset_mock()
    await provider.update_pr(_pr(), title="T", body="B")
    repo.get_pull.return_value.edit.assert_called_once_with(title="T", body="B")

    repo.get_pull.reset_mock()
    await provider.update_pr(_pr())
    repo.get_pull.assert_not_called()


async def test_gitlab_puts_title_and_description() -> None:
    calls: list[tuple[str, str, Any]] = []

    def handler(method: str, url: str, **kw: Any) -> _FakeResp:
        calls.append((method, url, kw.get("json")))
        return _FakeResp(200, {})

    provider = GitLabProvider.__new__(GitLabProvider)
    provider._token = "t"
    provider._api = "https://gitlab.example/api/v4"
    provider._username = "mira"
    with patch("mira.providers.gitlab.httpx.AsyncClient", lambda *a, **k: _FakeClient(handler)):
        await provider.update_pr(_pr(platform="gitlab"), title="T", body="B")
        await provider.update_pr(_pr(platform="gitlab"), body="only")
    assert calls == [
        (
            "PUT",
            "https://gitlab.example/api/v4/projects/acme%2Fapp/merge_requests/7",
            {"title": "T", "description": "B"},
        ),
        (
            "PUT",
            "https://gitlab.example/api/v4/projects/acme%2Fapp/merge_requests/7",
            {"description": "only"},
        ),
    ]


async def test_forgejo_patches_title_and_body() -> None:
    calls: list[tuple[str, str, Any]] = []

    def handler(method: str, url: str, **kw: Any) -> _FakeResp:
        calls.append((method, url, kw.get("json")))
        return _FakeResp(201, {})

    provider = ForgejoProvider.__new__(ForgejoProvider)
    provider._token = "t"
    provider._api = "https://forge.example/api/v1"
    provider._username = "mira"
    with patch("mira.providers.forgejo.httpx.AsyncClient", lambda *a, **k: _FakeClient(handler)):
        await provider.update_pr(_pr(platform="forgejo"), title="T")
    assert calls == [("PATCH", "https://forge.example/api/v1/repos/acme/app/pulls/7", {"title": "T"})]


# ── Orchestration ───────────────────────────────────────────────────────────


def _provider(pr: PRInfo) -> AsyncMock:
    provider = AsyncMock()
    provider.get_pr_info = AsyncMock(return_value=pr)
    provider.get_pr_diff = AsyncMock(return_value=DIFF)
    provider.update_pr = AsyncMock()
    return provider


def _llm(title: str = "Add b to the app") -> MagicMock:
    llm = MagicMock()
    llm.walkthrough = AsyncMock(return_value="{}")
    llm.generate_object = AsyncMock(return_value=SimpleNamespace(title=title))
    return llm


async def test_description_reuses_the_review_walkthrough() -> None:
    provider, llm = _provider(_pr(body="Why.")), _llm()
    written = await update_pr_summary(
        provider,
        "u",
        config=_config(),
        llm=llm,
        names=NAMES,
        first_review=True,
        walkthrough=_walkthrough(),
        diff_text=DIFF,
    )
    assert written == {"description": True, "title": False}
    llm.walkthrough.assert_not_called()
    llm.generate_object.assert_not_called()
    kwargs = provider.update_pr.await_args.kwargs
    assert kwargs["title"] is None
    assert kwargs["body"].startswith("Why.\n\n" + SUMMARY_START)


async def test_bare_mention_title_is_replaced() -> None:
    provider, llm = _provider(_pr(title=" @Mira ")), _llm('"@mira Add b."')
    written = await update_pr_summary(
        provider, "u", config=_config(enabled=False), llm=llm, names=NAMES, first_review=False
    )
    assert written == {"description": False, "title": True}
    provider.update_pr.assert_awaited_once()
    assert provider.update_pr.await_args.kwargs == {"title": "Add b", "body": None}
    # The title prompt frames the PR as data.
    prompt = llm.generate_object.await_args.args[0][0]["content"]
    assert "<<<MIRA-UNTRUSTED-FILE>>>" in prompt


async def test_human_title_and_disabled_description_cost_nothing() -> None:
    provider, llm = _provider(_pr()), _llm()
    written = await update_pr_summary(
        provider, "u", config=_config(enabled=False), llm=llm, names=NAMES, first_review=False
    )
    assert written == {"description": False, "title": False}
    provider.get_pr_diff.assert_not_called()
    provider.update_pr.assert_not_called()
    llm.generate_object.assert_not_called()


async def test_title_off_ignores_even_a_mention() -> None:
    provider, llm = _provider(_pr(title="@mira")), _llm()
    await update_pr_summary(
        provider,
        "u",
        config=_config(enabled=False, title="off"),
        llm=llm,
        names=NAMES,
        first_review=True,
    )
    provider.update_pr.assert_not_called()


async def test_unchanged_body_is_not_written() -> None:
    content = render_section_content(_walkthrough(), NAMES)
    provider, llm = _provider(_pr(body=f"Why.\n\n{_section(content)}")), _llm()
    written = await update_pr_summary(
        provider,
        "u",
        config=_config(),
        llm=llm,
        names=NAMES,
        first_review=False,
        walkthrough=_walkthrough(),
        diff_text=DIFF,
    )
    assert written == {"description": False, "title": False}
    provider.update_pr.assert_not_called()


async def test_push_with_nothing_new_skips_an_existing_section() -> None:
    provider, llm = _provider(_pr(body=f"Why.\n\n{_section('old')}")), _llm()
    await update_pr_summary(
        provider,
        "u",
        config=_config(),
        llm=llm,
        names=NAMES,
        first_review=False,
        has_new_changes=False,
    )
    llm.walkthrough.assert_not_called()
    provider.update_pr.assert_not_called()


async def test_empty_only_with_human_text_costs_no_walkthrough() -> None:
    provider, llm = _provider(_pr(body="Mine.")), _llm()
    await update_pr_summary(
        provider,
        "u",
        config=_config(mode="empty_only"),
        llm=llm,
        names=NAMES,
        first_review=False,
    )
    llm.walkthrough.assert_not_called()
    provider.update_pr.assert_not_called()


async def test_incremental_round_generates_a_full_walkthrough() -> None:
    provider, llm = _provider(_pr(body="")), _llm()
    with patch.object(pr_summary, "generate_walkthrough", AsyncMock(return_value=_walkthrough())):
        written = await update_pr_summary(
            provider, "u", config=_config(), llm=llm, names=NAMES, first_review=False
        )
        pr_summary.generate_walkthrough.assert_awaited_once()  # type: ignore[attr-defined]
    assert written["description"] is True
    provider.get_pr_diff.assert_awaited_once()


async def test_ignored_pr_is_left_alone() -> None:
    provider, llm = _provider(_pr(title="@mira", body="@mira ignore")), _llm()
    written = await update_pr_summary(
        provider, "u", config=_config(), llm=llm, names=NAMES, first_review=True
    )
    assert written == {"description": False, "title": False}
    provider.update_pr.assert_not_called()


async def test_failures_never_raise() -> None:
    provider, llm = _provider(_pr(title="@mira")), _llm()
    provider.update_pr = AsyncMock(side_effect=RuntimeError("403"))
    llm.generate_object = AsyncMock(side_effect=RuntimeError("model down"))
    written = await update_pr_summary(
        provider,
        "u",
        config=_config(),
        llm=llm,
        names=NAMES,
        first_review=True,
        walkthrough=_walkthrough(),
        diff_text=DIFF,
    )
    # The title failed and was skipped; the description write failed and was logged.
    assert written == {"description": False, "title": False}


async def test_a_config_without_the_section_is_a_no_op() -> None:
    provider = _provider(_pr(title="@mira"))
    await update_pr_summary(
        provider, "u", config=MagicMock(), llm=_llm(), names=NAMES, first_review=True
    )
    provider.get_pr_info.assert_not_called()


def test_summary_config_only_accepts_the_model() -> None:
    assert pr_summary.summary_config(MagicMock()) is None
    assert isinstance(pr_summary.summary_config(MiraConfig()), PRSummaryConfig)


# ── Handler wiring ──────────────────────────────────────────────────────────


@patch("mira.platforms.handlers.ReviewEngine")
@patch("mira.platforms.handlers.create_llm")
@patch("mira.platforms.handlers.load_config")
async def test_review_refreshes_the_summary_afterwards(
    mock_config: MagicMock, mock_llm: MagicMock, mock_engine_cls: MagicMock
) -> None:
    from mira.platforms.handlers import run_pr_review

    mock_config.return_value = _config()
    wt = _walkthrough()
    engine = MagicMock()
    engine.review_pr = AsyncMock(return_value=ReviewResult(summary="ok", walkthrough=wt))
    engine.last_walkthrough_covers_pr = True
    engine.last_full_diff = DIFF
    engine.last_review_had_changes = True
    mock_engine_cls.return_value = engine

    with (
        patch("mira.dashboard.api._app_db") as db,
        patch("mira.outbound_webhooks.dispatch_event", new_callable=AsyncMock),
        patch("mira.platforms.handlers.pr_summary.update_pr_summary", new=AsyncMock()) as ups,
    ):
        db.get_last_reviewed_sha.return_value = ""
        await run_pr_review(
            AsyncMock(), "acme", "app", 7, "u", False, "mira", bot_identity="project_7_bot_x"
        )
    kwargs = ups.await_args.kwargs
    assert kwargs["walkthrough"] is wt
    assert kwargs["diff_text"] == DIFF
    assert kwargs["first_review"] is True
    assert kwargs["names"] == NAMES


@patch("mira.platforms.handlers.ReviewEngine")
@patch("mira.platforms.handlers.create_llm")
@patch("mira.platforms.handlers.load_config")
async def test_incremental_review_does_not_hand_over_its_walkthrough(
    mock_config: MagicMock, mock_llm: MagicMock, mock_engine_cls: MagicMock
) -> None:
    from mira.platforms.handlers import run_pr_review

    mock_config.return_value = _config()
    engine = MagicMock()
    engine.review_pr = AsyncMock(return_value=ReviewResult(summary="ok", walkthrough=_walkthrough()))
    engine.last_walkthrough_covers_pr = False
    engine.last_full_diff = DIFF
    engine.last_review_had_changes = True
    mock_engine_cls.return_value = engine

    with (
        patch("mira.dashboard.api._app_db") as db,
        patch("mira.outbound_webhooks.dispatch_event", new_callable=AsyncMock),
        patch("mira.platforms.handlers.pr_summary.update_pr_summary", new=AsyncMock()) as ups,
    ):
        db.get_last_reviewed_sha.return_value = "abc"
        await run_pr_review(AsyncMock(), "acme", "app", 7, "u", False, "mira")
    kwargs = ups.await_args.kwargs
    assert kwargs["walkthrough"] is None
    assert kwargs["first_review"] is False


@pytest.mark.parametrize("verb", ["describe", "Describe", "summary"])
@patch("mira.platforms.handlers.create_llm")
@patch("mira.platforms.handlers.load_config")
async def test_describe_command_dispatches(
    mock_config: MagicMock, mock_llm: MagicMock, verb: str
) -> None:
    from mira.platforms.handlers import run_pr_command

    mock_config.return_value = _config()
    with patch("mira.platforms.handlers.run_pr_describe", new=AsyncMock()) as describe:
        await run_pr_command(
            AsyncMock(), "acme", "app", 7, "u", verb, "alice", "mira", bot_identity="id"
        )
    describe.assert_awaited_once()
    assert describe.await_args.args[-1] == "id"


@patch("mira.platforms.handlers.create_llm")
@patch("mira.platforms.handlers.load_config")
async def test_describe_forces_a_refresh_and_replies(
    mock_config: MagicMock, mock_llm: MagicMock
) -> None:
    from mira.platforms.handlers import run_pr_describe

    mock_config.return_value = _config()
    provider = _provider(_pr(body="Why."))
    provider.get_pr_labels = AsyncMock(return_value=[])
    with patch(
        "mira.platforms.handlers.pr_summary.update_pr_summary",
        new=AsyncMock(return_value={"description": True, "title": False}),
    ) as ups:
        await run_pr_describe(provider, "acme", "app", 7, "u", "alice", "mira")
    assert ups.await_args.kwargs["force"] is True
    assert "updated the description" in provider.post_comment.await_args.args[1]


@pytest.mark.parametrize(
    ("body", "labels", "expected"),
    [("@mira ignore", [], "opted out"), ("", ["mira-paused"], "paused")],
)
@patch("mira.platforms.handlers.create_llm")
@patch("mira.platforms.handlers.load_config")
async def test_describe_respects_ignore_and_pause(
    mock_config: MagicMock, mock_llm: MagicMock, body: str, labels: list[str], expected: str
) -> None:
    from mira.platforms.handlers import run_pr_describe

    mock_config.return_value = _config()
    provider = _provider(_pr(body=body))
    provider.get_pr_labels = AsyncMock(return_value=labels)
    with patch("mira.platforms.handlers.pr_summary.update_pr_summary", new=AsyncMock()) as ups:
        await run_pr_describe(provider, "acme", "app", 7, "u", "alice", "mira")
    ups.assert_not_called()
    provider.update_pr.assert_not_called()
    assert expected in provider.post_comment.await_args.args[1]


@patch("mira.platforms.handlers.create_llm")
@patch("mira.platforms.handlers.load_config")
async def test_describe_says_when_description_is_off(
    mock_config: MagicMock, mock_llm: MagicMock
) -> None:
    from mira.platforms.handlers import run_pr_describe

    mock_config.return_value = _config(enabled=False)
    provider = _provider(_pr())
    provider.get_pr_labels = AsyncMock(return_value=[])
    await run_pr_describe(provider, "acme", "app", 7, "u", "alice", "mira")
    provider.update_pr.assert_not_called()
    assert "pr_summary.description.enabled" in provider.post_comment.await_args.args[1]


def test_help_lists_describe() -> None:
    from mira.platforms.handlers import _help_message

    assert "`@mira describe`" in _help_message("mira")



async def test_engine_reviews_the_authors_text_and_reports_coverage(monkeypatch) -> None:
    """The review reads the description without Mira's own section, and tells
    the summary writer its walkthrough covered the whole pull request."""
    from mira.core.engine import ReviewEngine

    provider = MagicMock()
    provider.get_pr_info = AsyncMock(return_value=_pr(body=f"Why.\n\n{_section('old summary')}"))
    provider.get_pr_diff = AsyncMock(return_value=DIFF)
    provider.get_unresolved_bot_threads = AsyncMock(return_value=[])
    provider.get_all_bot_threads = AsyncMock(return_value=[])
    provider.find_bot_comment = AsyncMock(return_value=None)
    provider.post_comment = AsyncMock()
    provider.update_comment = AsyncMock()
    provider.get_compare_diff = AsyncMock(return_value="")
    provider.list_open_prs = AsyncMock(return_value=[])

    captured: dict[str, Any] = {}

    async def fake_internal(self: Any, diff_text: str, **kwargs: Any) -> ReviewResult:
        captured["description"] = kwargs.get("pr_description")
        return ReviewResult(comments=[], summary="")

    monkeypatch.setattr(ReviewEngine, "_review_diff_internal", fake_internal)
    engine = ReviewEngine(
        config=MiraConfig(), llm=AsyncMock(), provider=provider, bot_name="mira", dry_run=True
    )
    await engine.review_pr("https://example.com/acme/app/pull/7")

    assert captured["description"] == "Why."
    assert engine.last_full_diff == DIFF
    assert engine.last_walkthrough_covers_pr is True
    assert engine.last_review_had_changes is True
