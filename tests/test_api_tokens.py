"""API tokens: a read-only dashboard login that a program can carry.

The tests that matter most are the refusals. A token exists to be handed to an
agent, which means it will be handed to something Mira does not control, and
what makes that safe is what the token cannot do: write, manage credentials,
outlive its revocation, or be recovered from what is stored.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest
from click.testing import CliRunner
from fastapi import FastAPI
from fastapi.testclient import TestClient

from mira.autofix.redact import redact
from mira.cli import main
from mira.dashboard import tokens
from mira.dashboard.auth import AuthMiddleware, create_auth_router
from mira.dashboard.db import AppDatabase


@pytest.fixture
def db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AppDatabase:
    monkeypatch.setenv("MIRA_INDEX_DIR", str(tmp_path))
    monkeypatch.delenv("DATABASE_URL", raising=False)
    return AppDatabase(url="", admin_password="pw")


def _admin(db: AppDatabase):  # type: ignore[no-untyped-def]
    admin = db.authenticate("admin", "pw")
    assert admin is not None
    return admin


@pytest.fixture
def client(db: AppDatabase) -> TestClient:
    app = FastAPI()
    app.add_middleware(AuthMiddleware, db=db)
    app.include_router(create_auth_router(db))

    @app.api_route("/api/{full_path:path}", methods=["GET", "HEAD", "POST", "PUT", "DELETE"])
    def catch_all(full_path: str) -> dict:
        return {"ok": True, "path": full_path}

    return TestClient(app)


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


class TestTheStore:
    def test_only_a_digest_is_stored(self, db: AppDatabase) -> None:
        token, record = db.create_api_token(_admin(db).id, "agent")

        assert token.startswith(tokens.TOKEN_PREFIX)
        rows = db._sqlite_conn.execute("SELECT token_hash, prefix FROM api_tokens").fetchall()  # noqa: SLF001
        assert rows == [(tokens.digest(token), record["prefix"])]
        assert token not in {rows[0][0], rows[0][1]}
        assert token.startswith(record["prefix"])

    def test_a_live_token_names_its_user(self, db: AppDatabase) -> None:
        token, _ = db.create_api_token(_admin(db).id, "agent")

        found = db.validate_api_token(token)

        assert found is not None
        user, record = found
        assert (user.username, user.is_admin, record["name"]) == ("admin", True, "agent")

    def test_a_revoked_token_stops_working(self, db: AppDatabase) -> None:
        token, record = db.create_api_token(_admin(db).id, "agent")

        assert db.revoke_api_token(record["id"]) is True
        assert db.validate_api_token(token) is None
        # Revoking twice is not an error and does not move the first stamp.
        first = db.get_api_token(record["id"])["revoked_at"]
        assert db.revoke_api_token(record["id"]) is False
        assert db.get_api_token(record["id"])["revoked_at"] == first

    def test_an_expired_token_stops_working(self, db: AppDatabase) -> None:
        token, _ = db.create_api_token(_admin(db).id, "agent", expires_at=time.time() - 1)

        assert db.validate_api_token(token) is None

    def test_deleting_the_user_takes_their_tokens(self, db: AppDatabase) -> None:
        bot = db.create_user("bot", "pw2")
        token, _ = db.create_api_token(bot.id, "agent")

        db.delete_user(bot.id)

        assert db.validate_api_token(token) is None
        assert db.list_api_tokens() == []

    def test_a_string_that_is_not_a_token_is_not_looked_up(self, db: AppDatabase) -> None:
        assert db.validate_api_token("ghp_" + "a" * 36) is None
        assert db.validate_api_token("") is None

    def test_last_use_is_recorded(self, db: AppDatabase) -> None:
        token, record = db.create_api_token(_admin(db).id, "agent")
        assert record["last_used_at"] == 0

        db.validate_api_token(token)

        assert db.get_api_token(record["id"])["last_used_at"] > 0


class TestTheMiddleware:
    def test_a_token_reads(self, db: AppDatabase, client: TestClient) -> None:
        token, _ = db.create_api_token(_admin(db).id, "agent")

        response = client.get("/api/logs", headers=_bearer(token))

        assert response.status_code == 200

    def test_a_token_is_who_it_says(self, db: AppDatabase, client: TestClient) -> None:
        token, _ = db.create_api_token(_admin(db).id, "agent")

        response = client.get("/api/auth/me", headers=_bearer(token))

        assert response.status_code == 200
        assert response.json()["username"] == "admin"

    @pytest.mark.parametrize("method", ["POST", "PUT", "DELETE"])
    def test_a_token_does_not_write(self, db: AppDatabase, client: TestClient, method: str) -> None:
        token, _ = db.create_api_token(_admin(db).id, "agent")

        response = client.request(method, "/api/logs", headers=_bearer(token))

        assert response.status_code == 403
        assert "read-only" in response.json()["error"]

    def test_a_write_is_refused_before_the_token_is_checked(self, client: TestClient) -> None:
        # Same answer for a good token and a made-up one: a caller probing a
        # stolen token learns nothing about it from trying to write.
        response = client.post("/api/logs", headers=_bearer("mira_pat_" + "x" * 40))

        assert response.status_code == 403

    @pytest.mark.parametrize("path", ["/api/auth/tokens", "/api/auth/users", "/api/oauth/callback"])
    def test_a_token_cannot_reach_credentials(
        self, db: AppDatabase, client: TestClient, path: str
    ) -> None:
        token, _ = db.create_api_token(_admin(db).id, "agent")

        response = client.get(path, headers=_bearer(token))

        assert response.status_code == 403

    def test_a_bad_token_is_a_challenge(self, client: TestClient) -> None:
        response = client.get("/api/logs", headers=_bearer("mira_pat_" + "x" * 40))

        assert response.status_code == 401
        assert response.headers["WWW-Authenticate"].startswith("Bearer")

    def test_a_revoked_token_is_refused(self, db: AppDatabase, client: TestClient) -> None:
        token, record = db.create_api_token(_admin(db).id, "agent")
        db.revoke_api_token(record["id"])

        assert client.get("/api/logs", headers=_bearer(token)).status_code == 401

    def test_the_mcp_path_needs_a_token_not_a_cookie(
        self, db: AppDatabase, client: TestClient
    ) -> None:
        client.cookies.set("mira_session", db.create_session(_admin(db).id))

        response = client.post("/mcp", headers={"Origin": "http://testserver"})

        assert response.status_code == 401
        assert "API token" in response.json()["error"]

    def test_the_session_cookie_still_works(self, db: AppDatabase, client: TestClient) -> None:
        client.cookies.set("mira_session", db.create_session(_admin(db).id))

        assert client.get("/api/logs").status_code == 200


class TestTheRoutes:
    @pytest.fixture
    def signed_in(self, db: AppDatabase, client: TestClient) -> TestClient:
        client.cookies.set("mira_session", db.create_session(_admin(db).id))
        client.headers["Origin"] = "http://testserver"
        return client

    def test_create_shows_the_token_once(self, signed_in: TestClient) -> None:
        created = signed_in.post("/api/auth/tokens", json={"name": "claude"}).json()

        assert created["token"].startswith(tokens.TOKEN_PREFIX)
        assert created["expires_at"] > time.time() + 89 * 86400
        listed = signed_in.get("/api/auth/tokens").json()
        assert [t["name"] for t in listed] == ["claude"]
        assert "token" not in listed[0]

    def test_a_token_needs_a_name(self, signed_in: TestClient) -> None:
        assert signed_in.post("/api/auth/tokens", json={"name": "  "}).status_code == 400

    def test_zero_days_means_no_expiry(self, signed_in: TestClient) -> None:
        created = signed_in.post(
            "/api/auth/tokens", json={"name": "ci", "expires_in_days": 0}
        ).json()

        assert created["expires_at"] == 0

    def test_an_admin_mints_for_another_user(self, db: AppDatabase, signed_in: TestClient) -> None:
        bot = db.create_user("agent-bot", "pw2")

        created = signed_in.post(
            "/api/auth/tokens", json={"name": "agent", "user_id": bot.id}
        ).json()

        assert created["username"] == "agent-bot"

    def test_a_user_cannot_mint_for_somebody_else(
        self, db: AppDatabase, client: TestClient
    ) -> None:
        user = db.create_user("dev", "pw2")
        client.cookies.set("mira_session", db.create_session(user.id))

        response = client.post(
            "/api/auth/tokens",
            json={"name": "x", "user_id": _admin(db).id},
            headers={"Origin": "http://testserver"},
        )

        assert response.status_code == 403

    def test_a_user_cannot_see_or_revoke_somebody_elses(
        self, db: AppDatabase, client: TestClient
    ) -> None:
        _, admin_token = db.create_api_token(_admin(db).id, "admin's")
        user = db.create_user("dev", "pw2")
        client.cookies.set("mira_session", db.create_session(user.id))
        client.headers["Origin"] = "http://testserver"

        assert client.get("/api/auth/tokens").json() == []
        assert client.get("/api/auth/tokens?all_users=true").status_code == 403
        assert client.delete(f"/api/auth/tokens/{admin_token['id']}").status_code == 404
        assert db.get_api_token(admin_token["id"])["revoked_at"] == 0

    def test_revoke(self, db: AppDatabase, signed_in: TestClient) -> None:
        token = signed_in.post("/api/auth/tokens", json={"name": "claude"}).json()

        assert signed_in.delete(f"/api/auth/tokens/{token['id']}").status_code == 200
        assert db.validate_api_token(token["token"]) is None


class TestTheCli:
    def test_create_prints_the_token_alone_on_stdout(self, db: AppDatabase) -> None:
        result = CliRunner().invoke(main, ["token", "create", "--user", "admin", "--name", "ci"])

        assert result.exit_code == 0, result.output
        token = result.stdout.strip()
        assert token.startswith(tokens.TOKEN_PREFIX)
        assert db.validate_api_token(token) is not None

    def test_create_refuses_an_unknown_user(self, db: AppDatabase) -> None:
        result = CliRunner().invoke(main, ["token", "create", "--user", "nobody", "--name", "x"])

        assert result.exit_code != 0
        assert "No user named" in result.output

    def test_list_and_revoke(self, db: AppDatabase) -> None:
        token, record = db.create_api_token(_admin(db).id, "agent")

        listed = CliRunner().invoke(main, ["token", "list"])
        revoked = CliRunner().invoke(main, ["token", "revoke", str(record["id"])])

        assert "agent" in listed.output
        assert token not in listed.output
        assert revoked.exit_code == 0
        assert db.validate_api_token(token) is None


def test_a_token_in_a_log_line_is_redacted() -> None:
    token = tokens.generate()

    assert token not in redact(f"curl -H 'Authorization: Bearer {token}'")
