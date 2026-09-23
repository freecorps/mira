"""The MCP Streamable HTTP transport, served from `mira serve`'s own port.

The stdio server is launched by its client as a subprocess, which means the
client has to run where Mira's storage is. For a laptop pointed at a local
index that holds; for an install on Railway, Fly or a VM it does not, and the
agent that most needs to read a deployment's findings and logs is exactly the
one running somewhere else. This is the same server over HTTP: the same tools,
the same framing, the same audit trail, reached with an API token.

What changes is where the trust comes from. On stdio the operator builds the
grant from the configuration at launch. Here the caller is authenticated by a
token, and the token belongs to a dashboard user, so the session reaches what
that user reaches in the dashboard and nothing else:

- **Every repository Mira has registered.** The dashboard shows all of them to
  every user, and a token is a read-only dashboard login, so narrowing the
  grant here would be decorative - the same token reads the same findings from
  `/api/repos/...`. `mcp.repositories` is the stdio server's ceiling, not this
  one's.
- **The log trail, for an admin's token only.** The dashboard's Logs page is
  admin-only; the `logs` capability is granted on the same condition.

The transport is the stateless subset of the specification. Each POST carries
one JSON-RPC message and gets one JSON response; there is no server-initiated
stream to open with a GET, and no session id to issue, because nothing here
outlives a request - the cursors are signed and carry their own state. The
audit trail groups a token's calls under one session instead, which is the
question an operator asks of it: what did *that* credential read.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

from mira.mcp import protocol
from mira.mcp.authz import Grant, InvalidRepository, parse_repository
from mira.mcp.server import PROTOCOL_VERSIONS, MiraMcpServer

logger = logging.getLogger("mira.mcp")

#: The same ceiling the stdio loop holds one message to.
MAX_BODY_BYTES = protocol.MAX_MESSAGE_BYTES


@dataclass
class Reply:
    """What the route sends back: a status, maybe a body, maybe headers."""

    status: int
    body: dict[str, Any] | None = None
    headers: dict[str, str] = field(default_factory=dict)


def grant_for(records: Iterable[Any]) -> Grant:
    """A grant over every registered repository.

    Built from the application database's registry, which is Mira's own record
    of what it was installed on - not from anything the caller sends. A row
    whose name the index could not be opened under is skipped rather than
    failing the session.
    """
    specs: list[str] = []
    for record in records:
        spec = f"{record.platform}:{record.owner}/{record.repo}"
        try:
            parse_repository(spec)
        except InvalidRepository:
            logger.debug("MCP over HTTP: skipping unservable repository %s", spec)
            continue
        specs.append(spec)
    return Grant.from_specs(specs)


def _error(status: int, code: int, message: str) -> Reply:
    return Reply(status=status, body=protocol.error_response(None, code, message))


def handle(body: bytes, server: MiraMcpServer, *, protocol_version: str = "") -> Reply:
    """Answer one POSTed message.

    A request gets its response as JSON. A notification or a response gets 202
    and no body, which is what the specification asks for and what stops an
    answer landing in a client's table of calls it never made.
    """
    if len(body) > MAX_BODY_BYTES:
        return _error(413, protocol.INVALID_REQUEST, "Message too large.")
    try:
        message = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return _error(400, protocol.PARSE_ERROR, "Message was not valid JSON.")
    if isinstance(message, list):
        return _error(400, protocol.INVALID_REQUEST, "Batched requests are not accepted.")
    # The header names the version a session negotiated, so it is only held to
    # this server's list after `initialize`. A client may send its own newest
    # version on the initialize request itself, and refusing it there would
    # refuse the negotiation that settles on one both sides speak.
    version = protocol_version.strip()
    is_initialize = isinstance(message, dict) and message.get("method") == "initialize"
    if version and not is_initialize and version not in PROTOCOL_VERSIONS:
        return _error(
            400,
            protocol.INVALID_REQUEST,
            f"Unsupported MCP-Protocol-Version {version!r}. "
            f"This server speaks {', '.join(PROTOCOL_VERSIONS)}.",
        )
    if isinstance(message, dict) and "method" not in message and "id" in message:
        # A client answering a request this server never sends. Accepted and
        # dropped: there is nothing to route it to.
        return Reply(status=202)
    response = protocol.dispatch(message, server.methods())
    if response is None:
        return Reply(status=202)
    return Reply(status=200, body=response)
