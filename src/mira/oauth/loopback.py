"""A one-shot localhost listener for the CLI login flow.

Providers with a fixed ``http://localhost:<port>/auth/callback`` redirect can
be logged into properly — no pasting — as long as the browser and the process
waiting for the callback are on the same machine. That is exactly the CLI case,
so ``mira auth login`` serves a single request on that port, takes the code out
of the query string, and shuts down.

It is deliberately not used by the dashboard: a server-side listener would bind
a port on the *server*, while the browser resolves ``localhost`` to the user's
own machine. The dashboard uses the pasted-URL path in
:mod:`mira.oauth.manager` instead.
"""

from __future__ import annotations

import asyncio
import logging
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any
from urllib.parse import parse_qs, urlsplit

from mira.oauth import registry, store
from mira.oauth.base import OAuthError, PkcePair, new_state

logger = logging.getLogger(__name__)

_SUCCESS_HTML = b"""<!doctype html>
<html><head><meta charset="utf-8"><title>Mira</title></head>
<body style="font-family:system-ui;padding:3rem;text-align:center">
<h2>Signed in</h2>
<p>Mira has your session. You can close this tab and go back to the terminal.</p>
</body></html>"""

_FAILURE_HTML = b"""<!doctype html>
<html><head><meta charset="utf-8"><title>Mira</title></head>
<body style="font-family:system-ui;padding:3rem;text-align:center">
<h2>Sign-in failed</h2>
<p>Go back to the terminal for the details.</p>
</body></html>"""


class _CallbackHandler(BaseHTTPRequestHandler):
    """Answers exactly one callback and records its query parameters."""

    result: dict[str, str] = {}
    callback_path: str = "/auth/callback"

    def do_GET(self) -> None:  # noqa: N802 - name fixed by BaseHTTPRequestHandler
        parsed = urlsplit(self.path)
        if parsed.path != self.callback_path:
            self.send_response(404)
            self.end_headers()
            return
        params = parse_qs(parsed.query)
        _CallbackHandler.result = {k: v[0] for k, v in params.items() if v}
        ok = "code" in _CallbackHandler.result
        body = _SUCCESS_HTML if ok else _FAILURE_HTML
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args: Any) -> None:
        """Silence the default stderr access log — the CLI prints its own."""


def _serve_once(port: int, path: str, timeout: float) -> dict[str, str]:
    """Block until one callback arrives (or ``timeout`` seconds pass)."""
    _CallbackHandler.result = {}
    _CallbackHandler.callback_path = path
    server = HTTPServer(("127.0.0.1", port), _CallbackHandler)
    server.timeout = timeout
    try:
        # handle_request honours `timeout` and returns without a result when it
        # expires, which is how an abandoned browser tab stops being a hang.
        server.handle_request()
    finally:
        server.server_close()
    return dict(_CallbackHandler.result)


async def login_via_loopback(
    provider_id: str,
    *,
    timeout: float = 300.0,
    on_url: Any = None,
    db: store.SettingsStore | None = None,
) -> dict[str, Any]:
    """Run the whole flow locally: open the URL, catch the redirect, store it.

    ``on_url`` is called with the authorization URL before we start waiting, so
    the caller can open a browser and/or print it.
    """
    spec = registry.require(provider_id)
    if spec.redirect_mode != "loopback":
        raise OAuthError(f"{spec.label} does not use a loopback redirect")

    pkce = PkcePair.generate()
    state = new_state()
    redirect_uri = spec.loopback_redirect_uri()
    url = spec.authorization_url(state=state, challenge=pkce.challenge, redirect_uri=redirect_uri)
    if on_url is not None:
        on_url(url)

    try:
        params = await asyncio.to_thread(
            _serve_once, spec.loopback_port, spec.loopback_path, timeout
        )
    except OSError as exc:
        raise OAuthError(
            f"Could not listen on localhost:{spec.loopback_port} ({exc}). "
            "Close whatever is using that port — the redirect is registered to it."
        ) from exc

    if not params:
        raise OAuthError("Timed out waiting for the browser to come back")
    if params.get("error"):
        detail = params.get("error_description") or params["error"]
        raise OAuthError(f"Sign-in was not completed: {detail}")
    # The state check is the CSRF guard: it proves the redirect we just served
    # belongs to the request we started, not to one somebody else aimed at us.
    if params.get("state") != state:
        raise OAuthError("The redirect did not match this sign-in — start it again")
    if not params.get("code"):
        raise OAuthError("The redirect carried no authorization code")

    tokens = await spec.exchange_code(
        code=params["code"], verifier=pkce.verifier, redirect_uri=redirect_uri
    )
    store.save(tokens, db)
    logger.info("Connected %s account %s", spec.label, tokens.account_label or tokens.account_id)
    return store.status(spec.id, db)
