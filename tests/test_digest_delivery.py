"""Delivering digests (webhooks, email), the scheduler loop, the dashboard
routes and the two CLI commands."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from click.testing import CliRunner
from fastapi import HTTPException

from mira import outbound_webhooks as nf
from mira.cli import main
from mira.config import MiraConfig
from mira.dashboard.db import AppDatabase
from mira.digests import delivery, runtime
from tests.test_digests import T0, T1, FakeProvider, _provider_with_week

# ── Webhook rendering ──────────────────────────────────────────────────────


def _digest_data(**kw: Any) -> dict:
    data = nf.sample_data(nf.DIGEST_READY)
    data.update(kw)
    return data


class TestDigestWebhook:
    def test_event_is_offered(self) -> None:
        assert nf.DIGEST_READY in {e["value"] for e in nf.AVAILABLE_EVENTS}
        nf.WebhookConfig(url="https://example.com/h", events=[nf.DIGEST_READY])

    def test_slack(self) -> None:
        data = _digest_data(
            overview="Shipped <b>search</b> & more",
            areas=[{"name": "src/<x>", "count": 2, "summary": "", "highlights": []}],
            more_areas=3,
        )
        body = nf.render(nf.DIGEST_READY, data, "slack")
        text = body["blocks"][0]["text"]["text"]
        assert body["text"].startswith("📰 Mira digest for octocat/hello-world")
        assert "&lt;b&gt;search&lt;/b&gt; &amp; more" in text
        assert "• *src/&lt;x&gt;* (2)" in text
        assert "3 more area(s)" in text

    def test_slack_body_is_bounded(self) -> None:
        areas = [{"name": f"a{i}/", "count": 1, "summary": "x" * 400} for i in range(12)]
        body = nf.render(nf.DIGEST_READY, _digest_data(areas=areas), "slack")
        assert len(body["blocks"][0]["text"]["text"]) <= 3000

    def test_discord(self) -> None:
        data = _digest_data(overview="Ping @everyone <@&123>")
        body = nf.render(nf.DIGEST_READY, data, "discord")
        embed = body["embeds"][0]
        assert body["allowed_mentions"] == {"parse": []}
        assert embed["title"].startswith("📰 Mira digest")
        assert isinstance(embed["color"], int)
        assert len(embed["description"]) <= 4096

    def test_discord_converts_slack_links_for_other_events(self) -> None:
        body = nf.render(nf.REVIEW_COMPLETED, nf.sample_data(nf.REVIEW_COMPLETED), "discord")
        assert (
            "[octocat/hello-world #42](https://github.com/octocat/hello-world/pull/42)"
            in (body["embeds"][0]["description"])
        )

    def test_teams_and_generic(self) -> None:
        data = _digest_data()
        assert nf.render(nf.DIGEST_READY, data, "teams")["@type"] == "MessageCard"
        generic = nf.render(nf.DIGEST_READY, data, "generic")
        assert generic["event"] == "digest.ready" and generic["data"] == data

    def test_bad_period_values_do_not_raise(self) -> None:
        body = nf.render(nf.DIGEST_READY, {"repo": "o/r", "period_start": "x"}, "slack")
        assert "? to" in body["blocks"][0]["text"]["text"]


# ── Email ──────────────────────────────────────────────────────────────────


class TestEmail:
    async def test_not_configured(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("MIRA_SMTP_HOST", raising=False)
        assert await delivery.send_email("s", "b", ["a@b.c"]) is False
        monkeypatch.setenv("MIRA_SMTP_HOST", "smtp.example.com")
        assert await delivery.send_email("s", "b", []) is False

    async def test_sends_with_starttls_and_login(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("MIRA_SMTP_HOST", "smtp.example.com")
        monkeypatch.setenv("MIRA_SMTP_USER", "mira@example.com")
        monkeypatch.setenv("MIRA_SMTP_PASSWORD", "pw")
        smtp = MagicMock()
        smtp.__enter__ = MagicMock(return_value=smtp)
        smtp.__exit__ = MagicMock(return_value=False)
        with patch("mira.digests.delivery.smtplib.SMTP", return_value=smtp) as cls:
            ok = await delivery.send_email("Digest\r\nBcc: x@y.z", "body", ["a@b.c", "d@e.f"])
        assert ok
        assert cls.call_args.args[:2] == ("smtp.example.com", 587)
        smtp.starttls.assert_called_once()
        smtp.login.assert_called_once_with("mira@example.com", "pw")
        message = smtp.send_message.call_args.args[0]
        assert message["To"] == "a@b.c, d@e.f"
        assert message["From"] == "mira@example.com"
        assert "\n" not in message["Subject"] and "\r" not in message["Subject"]

    async def test_implicit_tls_and_failure(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("MIRA_SMTP_HOST", "smtp.example.com")
        monkeypatch.setenv("MIRA_SMTP_PORT", "465")
        with patch("mira.digests.delivery.smtplib.SMTP_SSL", side_effect=OSError("refused")):
            assert await delivery.send_email("s", "b", ["a@b.c"]) is False


# ── Runtime ────────────────────────────────────────────────────────────────


class TestRuntime:
    async def test_github_uses_the_installation_token(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        record = SimpleNamespace(installation_id=42)
        monkeypatch.setattr(
            "mira.dashboard.api._app_db", SimpleNamespace(get_repo=lambda *a, **k: record)
        )
        auth = SimpleNamespace(get_token=AsyncMock(return_value="inst-token"))
        with patch("mira.providers.create_provider", return_value="P") as create:
            got = await runtime.provider_factory({"github": auth})("github", "o", "r")
        assert got == "P"
        auth.get_token.assert_awaited_once_with(42)
        create.assert_called_once_with("github", "inst-token")

    async def test_github_falls_back_to_a_personal_token(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("GITHUB_TOKEN", "pat")
        with patch("mira.providers.create_provider", return_value="P") as create:
            await runtime.provider_factory({})("github", "o", "r")
        create.assert_called_once_with("github", "pat")

    async def test_missing_credentials_raise(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("GITHUB_TOKEN", raising=False)
        factory = runtime.provider_factory({})
        with pytest.raises(RuntimeError):
            await factory("github", "o", "r")
        with pytest.raises(RuntimeError):
            await factory("gitlab", "g", "p")

    async def test_token_platforms(self) -> None:
        auth = SimpleNamespace(get_token=AsyncMock(return_value="gl"))
        with patch("mira.providers.create_provider", return_value="P") as create:
            await runtime.provider_factory({"gitlab": auth})("gitlab", "g", "p")
        create.assert_called_once_with("gitlab", "gl")

    def test_auths_from_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        for name in ("MIRA_GITHUB_APP_ID", "MIRA_GITHUB_PRIVATE_KEY"):
            monkeypatch.delenv(name, raising=False)
        monkeypatch.setenv("MIRA_GITLAB_TOKEN", "gl")
        monkeypatch.setenv("MIRA_FORGEJO_TOKEN", "fj")
        assert set(runtime.auths_from_env()) == {"gitlab", "forgejo"}

    async def test_tick_skips_when_disabled_and_never_raises(self) -> None:
        provider_for = AsyncMock()
        assert await runtime.tick(provider_for, config=MiraConfig()) == []
        enabled = MiraConfig.model_validate({"digests": {"enabled": True, "use_llm": False}})
        with patch("mira.digests.service.run_scheduled", AsyncMock(side_effect=RuntimeError)):
            assert await runtime.tick(provider_for, config=enabled) == []

    async def test_start_and_stop(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(runtime, "_FIRST_TICK_DELAY", 3600.0)
        task = runtime.start({})
        assert task is not None and runtime.start({}) is task
        await runtime.stop()
        assert task.done()


# ── Dashboard routes ───────────────────────────────────────────────────────


@pytest.fixture
def app_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AppDatabase:
    monkeypatch.setenv("MIRA_INDEX_DIR", str(tmp_path))
    db = AppDatabase(url="", admin_password="admin")
    monkeypatch.setattr("mira.dashboard.api._app_db", db)
    return db


def _admin() -> SimpleNamespace:
    return SimpleNamespace(state=SimpleNamespace(user=SimpleNamespace(is_admin=True)))


def _user() -> SimpleNamespace:
    return SimpleNamespace(state=SimpleNamespace(user=SimpleNamespace(is_admin=False)))


class TestRoutes:
    async def test_generate_list_and_get(self, app_db: AppDatabase) -> None:
        from mira.dashboard.routers import digests as routes

        app_db.register_repo("o", "r")
        with (
            patch.object(routes, "_provider", AsyncMock(return_value=_provider_with_week())),
            patch(
                "mira.dashboard.routers.digests.load_config",
                return_value=MiraConfig.model_validate({"digests": {"use_llm": False}}),
            ),
            patch("mira.outbound_webhooks.dispatch_event", AsyncMock()) as dispatch,
            patch("mira.dashboard.routers.digests.time.time", return_value=T1),
        ):
            created = await routes.generate_digest(
                routes.DigestGenerate(owner="o", repo="r", days=7), _admin()
            )
        dispatch.assert_not_awaited()  # deliver defaults to off
        assert created["id"] > 0 and created["data"]["pull_requests"] == 2
        page = routes.list_digests(owner="o")
        assert page.total == 1 and page.digests[0]["id"] == created["id"]
        assert routes.get_digest(created["id"])["markdown"].startswith("## Mira digest")
        with pytest.raises(HTTPException) as exc:
            routes.get_digest(12345)
        assert exc.value.status_code == 404

    async def test_generate_refuses_unregistered_and_non_admin(self, app_db: AppDatabase) -> None:
        from mira.dashboard.routers import digests as routes

        body = routes.DigestGenerate(owner="o", repo="nope")
        with pytest.raises(HTTPException) as exc:
            await routes.generate_digest(body, _user())
        assert exc.value.status_code == 403
        with pytest.raises(HTTPException) as exc:
            await routes.generate_digest(body, _admin())
        assert exc.value.status_code == 404

    async def test_generate_reports_provider_failure(self, app_db: AppDatabase) -> None:
        from mira.dashboard.routers import digests as routes

        app_db.register_repo("o", "r")
        broken = FakeProvider()
        broken.list_landed_pull_requests = AsyncMock(side_effect=RuntimeError("502"))  # type: ignore[method-assign]
        with (
            patch.object(routes, "_provider", AsyncMock(return_value=broken)),
            pytest.raises(HTTPException) as exc,
        ):
            await routes.generate_digest(routes.DigestGenerate(owner="o", repo="r"), _admin())
        assert exc.value.status_code == 502

    async def test_release_notes(self, app_db: AppDatabase) -> None:
        from mira.dashboard.routers import digests as routes

        app_db.register_repo("o", "r")
        with patch.object(routes, "_provider", AsyncMock(return_value=_provider_with_week())):
            got = await routes.release_notes(
                _admin(), owner="o", repo="r", since="2026-09-28", llm=False
            )
        assert "### Features" in got.markdown and "add search" in got.markdown
        assert got.notes["sections"]["features"][0]["number"] == 10

    async def test_release_notes_validation(self, app_db: AppDatabase) -> None:
        from mira.dashboard.routers import digests as routes

        app_db.register_repo("o", "r")
        with pytest.raises(HTTPException) as exc:
            await routes.release_notes(_admin(), owner="o", repo="r", llm=False)
        assert exc.value.status_code == 422
        with (
            patch.object(routes, "_provider", AsyncMock(return_value=FakeProvider())),
            pytest.raises(HTTPException) as exc,
        ):
            await routes.release_notes(_admin(), owner="o", repo="r", since="not-a-date", llm=False)
        assert exc.value.status_code == 422


# ── CLI ────────────────────────────────────────────────────────────────────


class TestCLI:
    def test_digest_markdown_and_json(self) -> None:
        runner = CliRunner()
        with patch("mira.providers.create_provider", return_value=_provider_with_week()):
            result = runner.invoke(
                main,
                [
                    "digest",
                    "--repo",
                    "o/r",
                    "--token",
                    "t",
                    "--since",
                    "2026-09-28T09:00:00",
                    "--until",
                    "2026-10-05T09:00:00",
                    "--no-llm",
                ],
            )
        assert result.exit_code == 0, result.output
        assert "## Mira digest: o/r, 2026-09-28 to 2026-10-05" in result.output
        with patch("mira.providers.create_provider", return_value=_provider_with_week()):
            result = runner.invoke(
                main,
                [
                    "digest",
                    "--repo",
                    "o/r",
                    "--token",
                    "t",
                    "--since",
                    "2026-09-28T09:00:00",
                    "--until",
                    "2026-10-05T09:00:00",
                    "--no-llm",
                    "--output",
                    "json",
                ],
            )
        assert result.exit_code == 0, result.output
        assert json.loads(result.output)["pull_requests"] == 2

    def test_digest_needs_a_token_and_a_repo(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("GITHUB_TOKEN", raising=False)
        monkeypatch.delenv("MIRA_GIT_TOKEN", raising=False)
        runner = CliRunner()
        assert runner.invoke(main, ["digest", "--repo", "o/r"]).exit_code != 0
        assert runner.invoke(main, ["digest", "--repo", "nope", "--token", "t"]).exit_code != 0
        bad_window = runner.invoke(
            main,
            [
                "digest",
                "--repo",
                "o/r",
                "--token",
                "t",
                "--since",
                "2026-10-02",
                "--until",
                "2026-10-01",
            ],
        )
        assert bad_window.exit_code != 0

    def test_release_notes(self) -> None:
        runner = CliRunner()
        with patch("mira.providers.create_provider", return_value=_provider_with_week()):
            result = runner.invoke(
                main,
                [
                    "release-notes",
                    "--repo",
                    "o/r",
                    "--token",
                    "t",
                    "--since",
                    "2026-09-28",
                    "--no-llm",
                ],
            )
        assert result.exit_code == 0, result.output
        assert result.output.startswith("## o/r: changes on main since 2026-09-28")
        assert "### Features" in result.output and "### Other" in result.output

    def test_release_notes_needs_one_start(self) -> None:
        runner = CliRunner()
        both = runner.invoke(
            main,
            [
                "release-notes",
                "--repo",
                "o/r",
                "--token",
                "t",
                "--from",
                "v1",
                "--since",
                "2026-01-01",
            ],
        )
        assert both.exit_code != 0
        neither = runner.invoke(main, ["release-notes", "--repo", "o/r", "--token", "t"])
        assert neither.exit_code != 0


def test_period_constants_are_consistent() -> None:
    assert T1 - T0 == 7 * 86400
