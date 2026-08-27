"""JSON-RPC 2.0 over a pipe, which is all the MCP stdio transport is.

Hand-written rather than taken from an SDK, for one reason: this is a security
surface whose whole claim is that it cannot write, and a dependency that grows
a tool-registration decorator, a resource subscription or a sampling callback
in a minor release would move that claim outside the repository. What is here
is a framing loop and a method table, and both fit on a screen.

Two properties matter more than completeness.

**stdout carries protocol and nothing else.** A stray `print`, a warning, a
progress bar - anything written to stdout is a parse error at the client, and
the failure looks like a Mira bug rather than a stray write. Logging goes to
stderr throughout, and the server writes to the stream it was handed.

**A malformed message never takes the loop down.** A client that sends half a
line, or a JSON array, or a method that does not exist, gets an error object
and the connection stays up. The alternative - exiting - is a denial of
service that any client can trigger by accident.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from typing import Any, TextIO

logger = logging.getLogger("mira.mcp")

PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603

#: The largest single message this server will parse. A pipe has no natural
#: bound and a request is a few hundred bytes; anything past this is a mistake
#: or an attempt to exhaust memory.
MAX_MESSAGE_BYTES = 1024 * 1024


class ProtocolError(Exception):
    """An error to send back as a JSON-RPC error object."""

    def __init__(self, code: int, message: str, data: Any = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.data = data


def error_response(request_id: Any, code: int, message: str, data: Any = None) -> dict[str, Any]:
    error: dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        error["data"] = data
    return {"jsonrpc": "2.0", "id": request_id, "error": error}


def result_response(request_id: Any, result: dict[str, Any]) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def dispatch(
    message: dict[str, Any], methods: dict[str, Callable[[dict[str, Any]], dict[str, Any]]]
) -> dict[str, Any] | None:
    """Route one parsed message. Returns None for notifications.

    A notification is a message with no `id`. The specification says not to
    answer one, and answering anyway would put an unmatched response into a
    client's pending table.
    """
    if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
        return error_response(None, INVALID_REQUEST, "Expected a JSON-RPC 2.0 object.")
    request_id = message.get("id")
    method = message.get("method")
    if not isinstance(method, str):
        return error_response(request_id, INVALID_REQUEST, "Missing method.")
    params = message.get("params") or {}
    if not isinstance(params, dict):
        return error_response(request_id, INVALID_PARAMS, "params must be an object.")

    handler = methods.get(method)
    is_notification = "id" not in message
    if handler is None:
        if is_notification:
            # Notifications a server does not implement are ignored by design;
            # `notifications/initialized` is the common one.
            logger.debug("ignoring notification %s", method)
            return None
        return error_response(request_id, METHOD_NOT_FOUND, f"Unknown method: {method}")
    try:
        result = handler(params)
    except ProtocolError as exc:
        if is_notification:
            return None
        return error_response(request_id, exc.code, exc.message, exc.data)
    except Exception as exc:  # noqa: BLE001 - one bad call must not end the session
        logger.exception("MCP method %s failed", method)
        if is_notification:
            return None
        return error_response(request_id, INTERNAL_ERROR, f"{type(exc).__name__}: {exc}")
    if is_notification:
        return None
    return result_response(request_id, result)


def serve(
    reader: TextIO,
    writer: TextIO,
    methods: dict[str, Callable[[dict[str, Any]], dict[str, Any]]],
) -> None:
    """Read newline-delimited JSON until the client closes the pipe."""
    for line in reader:
        text = line.strip()
        if not text:
            continue
        if len(text.encode("utf-8", errors="ignore")) > MAX_MESSAGE_BYTES:
            _write(writer, error_response(None, INVALID_REQUEST, "Message too large."))
            continue
        try:
            message = json.loads(text)
        except ValueError:
            _write(writer, error_response(None, PARSE_ERROR, "Message was not valid JSON."))
            continue
        if isinstance(message, list):
            # Batches were removed from the protocol and Mira never needed
            # them. Answering with an error is clearer than answering half.
            _write(
                writer, error_response(None, INVALID_REQUEST, "Batched requests are not accepted.")
            )
            continue
        response = dispatch(message, methods)
        if response is not None:
            _write(writer, response)


def _write(writer: TextIO, payload: dict[str, Any]) -> None:
    # ASCII-escaped: this stream's encoding belongs to whoever launched the
    # process, and a response that cannot be encoded is a dead session.
    writer.write(json.dumps(payload, ensure_ascii=True) + "\n")
    writer.flush()
