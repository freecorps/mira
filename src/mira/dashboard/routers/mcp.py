"""The `/mcp` endpoint: Mira's read-only MCP server, over HTTP, by API token.

The transport and its reasoning live in :mod:`mira.mcp.http`; this is the
wiring. Authentication happens before any of it, in the middleware, which lets
nothing but an API token through to this path.
"""

from __future__ import annotations

import logging

from fastapi import Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse, Response

from mira.dashboard.api import router
from mira.dashboard.auth import _normalize_origin, _trusted_origins

logger = logging.getLogger(__name__)


def _not_allowed() -> Response:
    # No server-initiated stream and no session to end: POST is the whole of
    # this transport, and the specification's answer for the rest is a 405.
    return Response(status_code=405, headers={"Allow": "POST"})


@router.get("/mcp", include_in_schema=False)
def mcp_get() -> Response:
    return _not_allowed()


@router.delete("/mcp", include_in_schema=False)
def mcp_delete() -> Response:
    return _not_allowed()


async def _bounded_body(request: Request, limit: int) -> bytes | None:
    """The request body, or None once it passes ``limit``.

    Read in chunks and abandoned as soon as it is over, rather than read whole
    and measured: a declared length can lie, and an undeclared one is whatever
    the client decides to send.
    """
    chunks: list[bytes] = []
    size = 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > limit:
            return None
        chunks.append(chunk)
    return b"".join(chunks)


@router.post("/mcp", include_in_schema=False)
async def mcp_post(request: Request) -> Response:
    from mira.config import load_config
    from mira.dashboard.api import _app_db
    from mira.mcp import http, protocol
    from mira.mcp.audit import AuditLog
    from mira.mcp.server import MiraMcpServer
    from mira.mcp.tools import LOGS

    config = load_config().mcp
    if not config.http_enabled:
        return JSONResponse(status_code=404, content={"error": "The MCP endpoint is switched off"})

    # A browser page cannot set an Authorization header on a cross-origin POST
    # without a preflight this server does not grant, so this is defence in
    # depth - but it is the check the specification asks every HTTP server to
    # make, against DNS rebinding, and it costs a header lookup.
    origin = request.headers.get("origin")
    if origin and _normalize_origin(origin) not in _trusted_origins(request):
        return JSONResponse(status_code=403, content={"error": "Invalid request origin"})

    body = await _bounded_body(request, http.MAX_BODY_BYTES)
    if body is None:
        return JSONResponse(
            status_code=413,
            content=protocol.error_response(None, protocol.INVALID_REQUEST, "Message too large."),
        )

    user = request.state.user
    token = request.state.api_token
    audit = AuditLog(
        enabled=config.audit, db=_app_db, client=f"http token:{token['name']} ({user.username})"
    )
    # One session per credential: the question the trail answers about this
    # transport is what a given token read, not what one request did.
    audit.session_id = f"token-{token['id']}"
    server = MiraMcpServer(
        grant=http.grant_for(_app_db.list_repos()),
        config=config,
        audit=audit,
        capabilities=frozenset({LOGS}) if user.is_admin else frozenset(),
        app_db=_app_db,
    )
    reply = await run_in_threadpool(
        http.handle,
        body,
        server,
        protocol_version=request.headers.get("mcp-protocol-version", ""),
    )
    if reply.body is None:
        return Response(status_code=reply.status, headers=reply.headers)
    return JSONResponse(status_code=reply.status, content=reply.body, headers=reply.headers)
