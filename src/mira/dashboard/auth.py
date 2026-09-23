"""Authentication routes and middleware for the dashboard API."""

from __future__ import annotations

import logging
import os
import re
import time
from urllib.parse import urlsplit

from fastapi import APIRouter, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from starlette.middleware.base import BaseHTTPMiddleware

from mira.dashboard import tokens
from mira.dashboard.db import AppDatabase

logger = logging.getLogger(__name__)

SESSION_COOKIE = "mira_session"

# Routes that don't require auth
_PUBLIC_PATHS = {"/api/auth/login", "/docs", "/openapi.json", "/redoc"}
# The badge SVG must be public so GitHub can embed it as an image
_PUBLIC_SVG = re.compile(r"/api/repos/[^/]+/[^/]+/blast-radius\.svg")
_MUTATING_METHODS = {"POST", "PUT", "PATCH", "DELETE"}

# What a request carrying an API token may do. Reads, and the MCP endpoint,
# which is a POST by protocol and read-only by inventory. See
# `mira.dashboard.tokens` for why the rest is out of reach.
_TOKEN_METHODS = {"GET", "HEAD"}
MCP_PATH = "/mcp"
# The one credential route a token may call: an agent asking whose token it
# holds is the first thing worth checking, and the answer changes nothing.
_TOKEN_AUTH_PATHS = {"/api/auth/me"}
# GETs with side effects. The OAuth callback finishes a login and stores a
# grant, which is a write whatever its verb says.
_TOKEN_REFUSED_PATHS = {"/api/oauth/callback"}


def _normalize_origin(value: str) -> str:
    parsed = urlsplit(value.strip())
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.netloc:
        return ""
    return f"{parsed.scheme.lower()}://{parsed.netloc.lower()}"


def _trusted_origins(request: Request) -> set[str]:
    origins = {_normalize_origin(str(request.base_url))}
    host = (
        (request.headers.get("x-forwarded-host") or request.headers.get("host") or "")
        .split(",", 1)[0]
        .strip()
    )
    scheme = (
        (request.headers.get("x-forwarded-proto") or request.url.scheme).split(",", 1)[0].strip()
    )
    if host:
        origins.add(_normalize_origin(f"{scheme}://{host}"))
    configured = [os.environ.get("MIRA_DASHBOARD_URL", "")]
    configured.extend(os.environ.get("MIRA_TRUSTED_ORIGINS", "").split(","))
    origins.update(_normalize_origin(value) for value in configured if value.strip())
    if request.url.hostname in {"localhost", "127.0.0.1", "testserver"}:
        origins.update({"http://localhost:3000", "http://localhost:5173"})
    return {origin for origin in origins if origin}


class LoginRequest(BaseModel):
    username: str
    password: str


class UserResponse(BaseModel):
    id: int
    username: str
    is_admin: bool
    theme: str = "dark"
    last_login_at: float = 0


class SetThemeRequest(BaseModel):
    theme: str


class CreateUserRequest(BaseModel):
    username: str
    password: str
    is_admin: bool = False


class ChangePasswordRequest(BaseModel):
    current_password: str
    new_password: str


class ResetPasswordRequest(BaseModel):
    new_password: str


class CreateTokenRequest(BaseModel):
    name: str
    # Days until the token stops working. 0 means it does not expire, which is
    # allowed and not the default the dashboard offers.
    expires_in_days: int = 90
    # Admins may mint a token for another user - the usual case being a
    # dedicated non-admin account for an agent. Everybody else mints their own.
    user_id: int | None = None


class ApiTokenResponse(BaseModel):
    id: int
    user_id: int
    username: str
    name: str
    prefix: str
    created_at: float
    expires_at: float
    last_used_at: float
    revoked_at: float


class CreatedTokenResponse(ApiTokenResponse):
    # The token itself. In this response and no other, ever.
    token: str


_MAX_TOKEN_DAYS = 3650


def create_auth_router(db: AppDatabase) -> APIRouter:
    router = APIRouter(prefix="/api/auth", tags=["auth"])

    @router.post("/login")
    def login(body: LoginRequest, request: Request, response: Response) -> dict:
        user = db.authenticate(body.username, body.password)
        if not user:
            return JSONResponse(status_code=401, content={"error": "Invalid credentials"})
        db.record_login(user.id)
        token = db.create_session(user.id)
        response.set_cookie(
            SESSION_COOKIE,
            token,
            httponly=True,
            samesite="lax",
            # Secure when served over HTTPS. Behind a TLS-terminating proxy this
            # relies on uvicorn honoring X-Forwarded-Proto (trusted from
            # 127.0.0.1 by default; see the deployment docs for other setups).
            secure=request.url.scheme == "https",
            max_age=86400 * 7,
        )
        return {
            "ok": True,
            "user": {
                "id": user.id,
                "username": user.username,
                "is_admin": user.is_admin,
                "theme": user.theme,
            },
        }

    @router.post("/logout")
    def logout(request: Request, response: Response) -> dict:
        token = request.cookies.get(SESSION_COOKIE)
        if token:
            db.delete_session(token)
        response.delete_cookie(SESSION_COOKIE)
        return {"ok": True}

    @router.get("/me", response_model=UserResponse)
    def me(request: Request) -> dict:
        user = getattr(request.state, "user", None)
        if not user:
            return JSONResponse(status_code=401, content={"error": "Not authenticated"})
        return {
            "id": user.id,
            "username": user.username,
            "is_admin": user.is_admin,
            "theme": user.theme,
        }

    @router.put("/theme")
    def set_theme(body: SetThemeRequest, request: Request) -> dict:
        user = getattr(request.state, "user", None)
        if not user:
            return JSONResponse(status_code=401, content={"error": "Not authenticated"})
        if body.theme not in ("dark", "light"):
            return JSONResponse(
                status_code=400, content={"error": "Theme must be 'dark' or 'light'"}
            )
        db.set_user_theme(user.id, body.theme)
        return {"ok": True, "theme": body.theme}

    @router.post("/change-password")
    def change_password(body: ChangePasswordRequest, request: Request) -> dict:
        user = getattr(request.state, "user", None)
        if not user:
            return JSONResponse(status_code=401, content={"error": "Not authenticated"})
        if not body.new_password:
            return JSONResponse(status_code=400, content={"error": "New password cannot be empty"})
        # Re-verify the current password before allowing a change.
        if db.authenticate(user.username, body.current_password) is None:
            return JSONResponse(status_code=400, content={"error": "Current password is incorrect"})
        db.update_password(user.id, body.new_password)
        return {"ok": True}

    # ── User management (admin only) ──

    @router.get("/users", response_model=list[UserResponse])
    def list_users(request: Request) -> list[dict]:
        user = getattr(request.state, "user", None)
        if not user or not user.is_admin:
            return JSONResponse(status_code=403, content={"error": "Admin access required"})
        return [
            {
                "id": u.id,
                "username": u.username,
                "is_admin": u.is_admin,
                "last_login_at": u.last_login_at,
            }
            for u in db.list_users()
        ]

    @router.post("/users", response_model=UserResponse)
    def create_user(body: CreateUserRequest, request: Request) -> dict:
        user = getattr(request.state, "user", None)
        if not user or not user.is_admin:
            return JSONResponse(status_code=403, content={"error": "Admin access required"})
        try:
            new_user = db.create_user(body.username, body.password, is_admin=body.is_admin)
            return {"id": new_user.id, "username": new_user.username, "is_admin": new_user.is_admin}
        except Exception as exc:
            return JSONResponse(status_code=400, content={"error": str(exc)})

    @router.delete("/users/{user_id}")
    def delete_user(user_id: int, request: Request) -> dict:
        user = getattr(request.state, "user", None)
        if not user or not user.is_admin:
            return JSONResponse(status_code=403, content={"error": "Admin access required"})
        if user_id == user.id:
            return JSONResponse(status_code=400, content={"error": "Cannot delete yourself"})
        db.delete_user(user_id)
        return {"ok": True}

    @router.post("/users/{user_id}/password")
    def reset_user_password(user_id: int, body: ResetPasswordRequest, request: Request) -> dict:
        user = getattr(request.state, "user", None)
        if not user or not user.is_admin:
            return JSONResponse(status_code=403, content={"error": "Admin access required"})
        if not body.new_password:
            return JSONResponse(status_code=400, content={"error": "New password cannot be empty"})
        db.update_password(user_id, body.new_password)
        return {"ok": True}

    # ── API tokens ──
    #
    # Reachable from a session only: the middleware refuses a token on every
    # /api/auth route but `me`, so a token can never list, mint or revoke one.

    @router.get("/tokens", response_model=list[ApiTokenResponse])
    def list_tokens(request: Request, all_users: bool = False) -> list[dict] | JSONResponse:
        """The caller's tokens; an admin may ask for everybody's."""
        user = getattr(request.state, "user", None)
        if not user:
            return JSONResponse(status_code=401, content={"error": "Not authenticated"})
        if all_users and not user.is_admin:
            return JSONResponse(status_code=403, content={"error": "Admin access required"})
        return db.list_api_tokens(user_id=None if all_users else user.id)

    @router.post("/tokens", response_model=CreatedTokenResponse)
    def create_token(body: CreateTokenRequest, request: Request) -> dict | JSONResponse:
        user = getattr(request.state, "user", None)
        if not user:
            return JSONResponse(status_code=401, content={"error": "Not authenticated"})
        name = body.name.strip()
        if not name:
            return JSONResponse(status_code=400, content={"error": "Give the token a name"})
        if len(name) > tokens.MAX_NAME_CHARS:
            # Refused rather than cut: a created token whose name is not the
            # one that was asked for is a token nobody can find in the list.
            return JSONResponse(
                status_code=400,
                content={"error": f"Token names are at most {tokens.MAX_NAME_CHARS} characters"},
            )
        if not 0 <= body.expires_in_days <= _MAX_TOKEN_DAYS:
            return JSONResponse(
                status_code=400,
                content={"error": f"Expiry must be between 0 and {_MAX_TOKEN_DAYS} days"},
            )
        owner_id = user.id
        if body.user_id is not None and body.user_id != user.id:
            if not user.is_admin:
                return JSONResponse(status_code=403, content={"error": "Admin access required"})
            if not any(u.id == body.user_id for u in db.list_users()):
                return JSONResponse(status_code=404, content={"error": "No such user"})
            owner_id = body.user_id
        expires_at = time.time() + body.expires_in_days * 86400 if body.expires_in_days else 0.0
        token, record = db.create_api_token(owner_id, name, expires_at=expires_at)
        logger.info(
            "API token %d (%s) created for user %d by %s",
            record["id"],
            record["name"],
            owner_id,
            user.username,
        )
        return {**record, "token": token}

    @router.delete("/tokens/{token_id}", response_model=None)
    def revoke_token(token_id: int, request: Request) -> dict | JSONResponse:
        """Revoke a token. Your own, or anybody's if you are an admin."""
        user = getattr(request.state, "user", None)
        if not user:
            return JSONResponse(status_code=401, content={"error": "Not authenticated"})
        record = db.get_api_token(token_id)
        # Somebody else's token reads as missing to a non-admin: whether a
        # token id exists is not their business.
        if record is None or (record["user_id"] != user.id and not user.is_admin):
            return JSONResponse(status_code=404, content={"error": "No such token"})
        db.revoke_api_token(token_id)
        logger.info("API token %d (%s) revoked by %s", token_id, record["name"], user.username)
        return {"ok": True}

    return router


def bearer_token(request: Request) -> str:
    """The credential in an ``Authorization: Bearer`` header, or ``""``."""
    header = request.headers.get("authorization", "")
    scheme, _, value = header.partition(" ")
    if scheme.lower() != "bearer":
        return ""
    return value.strip()


def _unauthorized(message: str) -> JSONResponse:
    # The challenge header is what tells an MCP client, or any HTTP client,
    # that a bearer token is what this server wants.
    return JSONResponse(
        status_code=401,
        content={"error": message},
        headers={"WWW-Authenticate": 'Bearer realm="mira"'},
    )


class AuthMiddleware(BaseHTTPMiddleware):
    def __init__(self, app, db: AppDatabase) -> None:  # type: ignore[no-untyped-def]
        super().__init__(app)
        self.db = db

    async def dispatch(self, request: Request, call_next):  # type: ignore[no-untyped-def]
        # Skip auth for public paths and OPTIONS (CORS preflight)
        if request.method == "OPTIONS":
            return await call_next(request)
        if request.url.path in _PUBLIC_PATHS:
            return await call_next(request)
        # Allow the badge SVG for GitHub image embedding
        if _PUBLIC_SVG.fullmatch(request.url.path):
            return await call_next(request)
        is_mcp = request.url.path == MCP_PATH
        # Non-API paths (frontend assets) don't need auth
        if not request.url.path.startswith("/api/") and not is_mcp:
            return await call_next(request)

        bearer = bearer_token(request)
        if bearer:
            return await self._dispatch_token(request, call_next, bearer, is_mcp=is_mcp)
        if is_mcp:
            # The MCP endpoint takes a token and nothing else. A cookie would
            # make a cross-site POST from any page the admin has open an
            # authenticated MCP call, and no MCP client carries one anyway.
            return _unauthorized("The MCP endpoint needs an API token (Authorization: Bearer)")

        token = request.cookies.get(SESSION_COOKIE)
        if not token:
            return JSONResponse(status_code=401, content={"error": "Not authenticated"})

        user = self.db.validate_session(token)
        if not user:
            return JSONResponse(status_code=401, content={"error": "Session expired"})

        # Cookie-authenticated mutations must originate from this dashboard.
        # Browsers send Origin for fetch/XHR and form POSTs; requests without
        # Origin remain available to non-browser API clients using a session.
        if request.method in _MUTATING_METHODS:
            origin = request.headers.get("origin")
            if not origin or _normalize_origin(origin) not in _trusted_origins(request):
                return JSONResponse(status_code=403, content={"error": "Invalid request origin"})

        request.state.user = user
        return await call_next(request)

    async def _dispatch_token(  # type: ignore[no-untyped-def]
        self, request: Request, call_next, token: str, *, is_mcp: bool
    ):
        """Authenticate a request by API token, and hold it to reading.

        The method and path checks run before the token is looked up, so a
        write attempted with a token is refused the same way whether or not
        the token is any good: a caller probing for what a stolen token can
        do learns nothing from the difference.
        """
        path = request.url.path
        if not is_mcp:
            if request.method not in _TOKEN_METHODS:
                return JSONResponse(
                    status_code=403,
                    content={"error": "API tokens are read-only; sign in to make changes"},
                )
            if (path.startswith("/api/auth/") and path not in _TOKEN_AUTH_PATHS) or (
                path in _TOKEN_REFUSED_PATHS
            ):
                return JSONResponse(
                    status_code=403,
                    content={"error": "API tokens cannot manage credentials; sign in instead"},
                )
        found = self.db.validate_api_token(token)
        if found is None:
            return _unauthorized("Invalid, expired or revoked API token")
        user, record = found
        request.state.user = user
        request.state.api_token = record
        return await call_next(request)
