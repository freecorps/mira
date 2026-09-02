"""Dashboard routes for OAuth connections.

Admin-only, and deliberately narrow: start a login, finish one, disconnect an
account, ask where its allowance stands, and choose which signed-in backend
serves bare model ids. No route ever returns token material — the dashboard
shows who is connected, when the session expires and how much of the plan is
spent, which is everything it needs to render the page.

A provider holds any number of accounts, each named by a key; the routes
under ``/accounts/{account_key}`` act on one of them.
"""

from __future__ import annotations

import os
from html import escape

from fastapi import HTTPException, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from mira.dashboard import api as _api
from mira.dashboard.api import _require_admin, logger, router
from mira.oauth import manager, registry, store
from mira.oauth.base import OAuthError

_CALLBACK_HTML = """<!doctype html>
<html><head><meta charset="utf-8"><title>Mira</title></head>
<body style="font-family:system-ui;padding:3rem;text-align:center">
<h2>{heading}</h2>
<p>{detail}</p>
<p><a href="/settings/connections">Back to Mira</a></p>
</body></html>"""


class OAuthCompleteRequest(BaseModel):
    # Either paste the whole redirect URL, or hand over code + state directly.
    redirect_url: str = ""
    code: str = ""
    state: str = ""


class OAuthActiveRequest(BaseModel):
    # "" routes bare model ids back through the API-key path.
    provider: str = ""
    # One of the provider's account keys, or "" / "*" for any of them.
    account: str = ""


def _spec_or_404(provider_id: str):  # type: ignore[no-untyped-def]
    spec = registry.get(provider_id)
    if spec is None:
        raise HTTPException(status_code=404, detail=f"Unknown OAuth provider {provider_id!r}")
    return spec


def _account_or_404(provider_id: str, account_key: str):  # type: ignore[no-untyped-def]
    spec = _spec_or_404(provider_id)
    tokens = store.load(spec.id, account_key, _api._app_db)
    if tokens is None or not account_key:
        raise HTTPException(
            status_code=404, detail=f"{spec.label} has no connected account {account_key!r}"
        )
    return spec, tokens


@router.get("/api/oauth/providers")
def list_oauth_providers(request: Request) -> dict:
    """Every provider, its accounts, and which backend bare model ids go to."""
    _require_admin(request)
    return manager.list_status(_api._app_db)


@router.post("/api/oauth/{provider_id}/start")
def start_oauth(provider_id: str, request: Request) -> dict:
    """Begin a login and hand back the URL to send the operator to.

    The callback origin is deployment configuration and is read from
    ``MIRA_DASHBOARD_URL`` alone — never from the request. It decides where the
    provider sends the authorization code, so anything that reaches it from
    outside (a request body, a Host header a proxy did not pin) is a way to
    have that code delivered somewhere else. It also has to match what was
    registered with the provider, which a per-request value cannot promise.
    Providers with a fixed loopback redirect, ChatGPT among them, ignore it.
    """
    _require_admin(request)
    _spec_or_404(provider_id)
    try:
        return manager.start_login(
            provider_id,
            dashboard_origin=os.environ.get("MIRA_DASHBOARD_URL", ""),
            db=_api._app_db,
        )
    except OAuthError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/api/oauth/{provider_id}/complete")
async def complete_oauth(provider_id: str, body: OAuthCompleteRequest, request: Request) -> dict:
    """Redeem the code the operator came back with and store the account.

    Signing in to an account that is already connected lands in the same
    slot — a reconnect, not a twin. Any other account is added alongside.
    """
    _require_admin(request)
    _spec_or_404(provider_id)
    try:
        return await manager.complete_login(
            code=body.code,
            state=body.state,
            redirect_url=body.redirect_url,
            db=_api._app_db,
        )
    except OAuthError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.delete("/api/oauth/{provider_id}")
def disconnect_oauth(provider_id: str, request: Request) -> dict:
    """Forget every account of a provider (and stop routing at it)."""
    _require_admin(request)
    _spec_or_404(provider_id)
    manager.disconnect(provider_id, db=_api._app_db)
    return {"ok": True}


@router.delete("/api/oauth/{provider_id}/accounts/{account_key}")
def disconnect_oauth_account(provider_id: str, account_key: str, request: Request) -> dict:
    """Forget one account. The default pointer is cleared only if it named it."""
    _require_admin(request)
    _account_or_404(provider_id, account_key)
    manager.disconnect(provider_id, account_key, db=_api._app_db)
    return {"ok": True}


@router.post("/api/oauth/{provider_id}/accounts/{account_key}/refresh")
async def refresh_oauth_account(provider_id: str, account_key: str, request: Request) -> dict:
    """Renew an account's session now, so a stale one is fixed here and not mid-review.

    Always goes to the issuer, expiry or not: this button exists for the case
    the expiry cannot describe — a grant revoked upstream, which still looks
    valid here until something tries to use it.
    """
    _require_admin(request)
    spec, _tokens = _account_or_404(provider_id, account_key)
    try:
        tokens = await store.valid_tokens(spec.id, account_key, _api._app_db, force=True)
    except OAuthError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    logger.info("Refreshed OAuth session for %s account %s", spec.label, account_key)
    return store.account_status(spec.id, tokens, _api._app_db)


@router.post("/api/oauth/{provider_id}/accounts/{account_key}/usage")
async def refresh_oauth_usage(provider_id: str, account_key: str, request: Request) -> dict:
    """Ask the provider where this account's allowance stands, and record it.

    Reviews keep the snapshot current on their own (every response reports
    it); this is for looking before a review runs, or after a quiet spell.
    """
    _require_admin(request)
    spec, _tokens = _account_or_404(provider_id, account_key)
    if not spec.reports_usage:
        raise HTTPException(status_code=400, detail=f"{spec.label} does not report usage")
    try:
        return await manager.refresh_usage(spec.id, account_key, _api._app_db)
    except OAuthError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.put("/api/oauth/active")
def set_active_oauth(body: OAuthActiveRequest, request: Request) -> dict:
    """Choose which signed-in backend serves bare model ids.

    ``account`` pins one of the provider's accounts; empty (or ``*``) lets
    every call go to whichever account has the most allowance left. Selecting
    a provider or account that is not connected is rejected rather than
    stored: the pointer would resolve back to the API-key path on every
    review, and the page would show a choice that quietly does nothing.
    """
    _require_admin(request)
    provider_id = (body.provider or "").strip()
    account_key = (body.account or "").strip()
    if account_key == "*":
        account_key = ""
    if provider_id:
        spec = _spec_or_404(provider_id)
        if spec.llm is None:
            raise HTTPException(status_code=400, detail=f"{spec.label} cannot serve models")
        accounts = store.accounts(spec.id, _api._app_db)
        if not accounts:
            raise HTTPException(status_code=400, detail=f"{spec.label} is not connected")
        if account_key and account_key not in accounts:
            raise HTTPException(
                status_code=400, detail=f"{spec.label} has no connected account {account_key!r}"
            )
        provider_id = spec.id
    else:
        account_key = ""
    store.set_active(provider_id, account_key, _api._app_db)
    # Reviews are configured now; don't send the operator back through setup.
    _api._app_db.mark_setup_complete()
    return {
        "ok": True,
        "active_provider": provider_id,
        "active_account": account_key,
        "active_ref": store.format_ref(provider_id, account_key),
    }


@router.get("/api/oauth/callback", response_class=HTMLResponse)
async def oauth_callback(
    request: Request, code: str = "", state: str = "", error: str = ""
) -> HTMLResponse:
    """Landing point for providers that redirect back to the dashboard.

    Answers HTML because a browser lands here directly, and always with 200:
    the page is the message. A failure here is the provider's or the operator's
    (a denied consent, an expired attempt), and rendering it as an HTTP error
    would show a blank browser error page instead of the reason.
    """
    _require_admin(request)
    if error:
        return _callback_page("Sign-in failed", error)
    try:
        status = await manager.complete_login(code=code, state=state, db=_api._app_db)
    except OAuthError as exc:
        return _callback_page("Sign-in failed", str(exc))
    account = status.get("account_label") or status.get("label") or "your account"
    return _callback_page("Connected", f"Signed in as {account}.")


def _callback_page(heading: str, detail: str) -> HTMLResponse:
    """Render the callback page with every interpolated value escaped.

    Both values reach this page from outside: ``detail`` carries either a query
    parameter the provider (or anyone who can hand an admin a link) put in the
    redirect, or an error message quoting one, and the account label comes from
    a token payload. Interpolated raw, either one is script running in the
    browser of the one person on the instance who can change these settings.
    """
    return HTMLResponse(
        _CALLBACK_HTML.format(heading=escape(heading), detail=escape(detail)),
        status_code=200,
    )
