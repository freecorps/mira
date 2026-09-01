"""The login state machine: start an attempt, finish it, hand back a session.

An attempt is the PKCE verifier plus the redirect URI it was started with,
stored in its own ``settings`` row under its ``state`` until the user comes
back with a code. Persisted rather than held in memory so a dashboard restart —
or a second worker process — between "Connect" and the redirect does not strand
a login the user has already approved, and one row per attempt so two logins
in flight at once cannot overwrite each other.

Two ways an attempt finishes, both landing in :func:`complete_login`:

* **dashboard redirect** — the provider allows our own callback URL, the
  browser returns to ``/api/oauth/callback``, and we finish it there.
* **pasted URL** — the provider only accepts a fixed ``localhost`` redirect
  (ChatGPT does), so a browser on a different machine than the server lands on
  a page that cannot load. The user copies that URL back into the dashboard and
  we take the code out of it. Unglamorous, but it is the only thing that works
  for a remotely-hosted dashboard against a fixed loopback redirect — and the
  CLI (``mira auth login``) runs a real listener for the local case.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any
from urllib.parse import parse_qs, urlsplit

from mira.oauth import registry, store
from mira.oauth.base import OAuthError, OAuthTokens, PkcePair, new_state
from mira.oauth.store import SettingsStore

logger = logging.getLogger(__name__)

# One settings row per attempt, keyed by state. Deliberately not one JSON
# document holding all of them: that turns every start and every completion
# into a read-modify-write of the same row, and two logins overlapping — two
# admins, two tabs, a second worker — then drop each other's entries and
# strand a login the user has already approved. Separate rows never collide.
_PENDING_PREFIX = "oauth_pending:"
# How long an approved login may sit before the code is redeemed. Long enough
# to walk through a consent screen and paste a URL, short enough that an
# abandoned attempt is not a verifier sitting in the database for a week.
PENDING_TTL_SECONDS = 900


# ── Pending-attempt bookkeeping ─────────────────────────────────────


def _expired(attempt: dict[str, Any]) -> bool:
    try:
        created = float(attempt.get("created_at", 0))
    except (TypeError, ValueError):
        return True
    return created <= time.time() - PENDING_TTL_SECONDS


def _write_attempt(db: SettingsStore, state: str, attempt: dict[str, Any]) -> None:
    db.set_setting(_PENDING_PREFIX + state, json.dumps(attempt))


def _take_attempt(db: SettingsStore, state: str) -> dict[str, Any] | None:
    """Claim an attempt: return it and remove it in the same statement.

    Removed before the caller redeems it, not after: an authorization code is
    single-use, so a retry has to start a fresh login rather than replay this
    verifier against a code the issuer has already burned. And claimed
    atomically, so two requests carrying the same state cannot both come away
    with the verifier and both go redeem it — which is the one-time consumption
    the state parameter is for.
    """
    raw = db.take_setting(_PENDING_PREFIX + state)
    if not raw:
        return None
    try:
        attempt = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if not isinstance(attempt, dict) or _expired(attempt):
        return None
    return attempt


def _prune(db: SettingsStore) -> None:
    """Drop attempts nobody came back for. Best-effort, and safe to race.

    Each row is deleted on its own, so two callers pruning at once delete the
    same expired rows rather than fighting over a shared document.
    """
    lister = getattr(db, "list_settings", None)
    if lister is None:  # pragma: no cover - stores that can't enumerate
        return
    for key, raw in lister(_PENDING_PREFIX).items():
        try:
            attempt = json.loads(raw)
        except json.JSONDecodeError:
            attempt = {}
        if not isinstance(attempt, dict) or _expired(attempt):
            db.delete_setting(key)


def _dashboard_redirect_uri(spec: Any, origin: str) -> str:
    """Where a dashboard-mode provider should send the code back to.

    ``origin`` is deployment configuration — the caller reads it from
    ``MIRA_DASHBOARD_URL``, never from a request. This value tells the provider
    where to deliver an authorization code, so it is checked rather than
    trusted: an absolute http(s) URL with a host, and nothing else. A relative
    or scheme-less value would otherwise be pasted into a redirect URI that
    resolves somewhere nobody intended.
    """
    parsed = urlsplit((origin or "").strip())
    if not parsed.scheme or not parsed.netloc:
        raise OAuthError(
            f"{spec.label} needs the dashboard's public URL — set MIRA_DASHBOARD_URL "
            "to the absolute address this dashboard is reached at "
            "(e.g. https://mira.example.com)"
        )
    if parsed.scheme not in ("http", "https"):
        raise OAuthError(f"MIRA_DASHBOARD_URL must be an http(s) URL, got {origin!r}")
    base = f"{parsed.scheme}://{parsed.netloc}{parsed.path}".rstrip("/")
    return base + spec.dashboard_callback_path


# ── Flow ────────────────────────────────────────────────────────────


def start_login(
    provider_id: str,
    *,
    dashboard_origin: str = "",
    db: SettingsStore | None = None,
) -> dict[str, Any]:
    """Begin a login and return everything the caller needs to send the user off.

    ``dashboard_origin`` (e.g. ``https://mira.example.com``) is used only by
    providers that accept our own callback URL; loopback-only providers ignore
    it and get their fixed redirect.
    """
    spec = registry.require(provider_id)
    resolved_db = db or store.default_db()
    if resolved_db is None:
        raise OAuthError("No dashboard database available to track the login")

    if spec.redirect_mode == "dashboard":
        redirect_uri = _dashboard_redirect_uri(spec, dashboard_origin)
    else:
        redirect_uri = spec.loopback_redirect_uri()

    pkce = PkcePair.generate()
    state = new_state()
    _prune(resolved_db)
    _write_attempt(
        resolved_db,
        state,
        {
            "provider": spec.id,
            "verifier": pkce.verifier,
            "redirect_uri": redirect_uri,
            "created_at": time.time(),
        },
    )

    return {
        "provider": spec.id,
        "label": spec.label,
        "authorization_url": spec.authorization_url(
            state=state, challenge=pkce.challenge, redirect_uri=redirect_uri
        ),
        "state": state,
        "redirect_uri": redirect_uri,
        "redirect_mode": spec.redirect_mode,
        # True when the browser cannot deliver the code back to us on its own
        # and the user has to paste the redirect URL.
        "manual_exchange": spec.redirect_mode != "dashboard",
        "expires_in": PENDING_TTL_SECONDS,
    }


def parse_redirect(url: str) -> dict[str, str]:
    """Pull ``code``/``state`` out of a pasted redirect URL (or bare query string).

    Raises :class:`OAuthError` when the provider redirected with an error
    instead of a code — that message is the useful part of a failed consent,
    and swallowing it leaves the user with "invalid URL" for a denied login.
    """
    text = (url or "").strip()
    if not text:
        raise OAuthError("Paste the full URL you were redirected to")
    query = urlsplit(text).query or (text if "=" in text else "")
    params = parse_qs(query)
    error = (params.get("error") or [""])[0]
    if error:
        detail = (params.get("error_description") or [""])[0]
        raise OAuthError(f"Sign-in was not completed: {detail or error}")
    code = (params.get("code") or [""])[0]
    state = (params.get("state") or [""])[0]
    if not code:
        raise OAuthError("That URL has no authorization code in it")
    return {"code": code, "state": state}


async def complete_login(
    *,
    code: str = "",
    state: str = "",
    redirect_url: str = "",
    db: SettingsStore | None = None,
) -> dict[str, Any]:
    """Redeem a code against its pending attempt and store the session.

    The attempt is removed before the exchange runs, not after: an
    authorization code is single-use, so a retry must start a fresh login
    rather than replay a verifier against a code the issuer has already burned.

    A pasted redirect has to identify its own attempt: the state it carries is
    the only evidence that this URL came back from the login we started, so it
    is required and it decides which attempt is claimed. ``state`` from the
    caller — the dashboard passes the one its dialog started — is an
    expectation checked against it, never a substitute for it. Falling back to
    the caller's value when the redirect has none accepts a URL that proves
    nothing, and spends the pending attempt on screen doing it.

    Completing with ``code`` and ``state`` directly, with no redirect URL, is
    the dashboard-callback path and stays available: there the state arrives as
    its own request parameter rather than inside a URL somebody pasted.
    """
    resolved_db = db or store.default_db()
    if resolved_db is None:
        raise OAuthError("No dashboard database available to complete the login")

    if redirect_url:
        parsed = parse_redirect(redirect_url)
        if not parsed["state"]:
            raise OAuthError("That redirect carries no state — start the sign-in again")
        if state and parsed["state"] != state:
            raise OAuthError("That redirect belongs to a different sign-in — start this one again")
        code, state = parsed["code"], parsed["state"]
    if not code:
        raise OAuthError("No authorization code to redeem")
    if not state:
        raise OAuthError("The redirect carried no state value — start the sign-in again")

    attempt = _take_attempt(resolved_db, state)
    if attempt is None:
        raise OAuthError("This sign-in expired or was already completed — start it again")

    spec = registry.require(str(attempt.get("provider", "")))
    tokens = await spec.exchange_code(
        code=code,
        verifier=str(attempt.get("verifier", "")),
        redirect_uri=str(attempt.get("redirect_uri", "")),
    )
    _store_session(tokens, spec.label, resolved_db)
    return store.status(spec.id, resolved_db)


def _store_session(tokens: OAuthTokens, label: str, db: SettingsStore) -> None:
    """Persist a fresh grant and log who it belongs to (never the token)."""
    store.save(tokens, db)
    logger.info(
        "Connected %s account %s%s",
        label,
        tokens.account_label or tokens.account_id or "(unknown)",
        f" [{tokens.plan}]" if tokens.plan else "",
    )


def disconnect(provider_id: str, db: SettingsStore | None = None) -> None:
    """Forget a provider's session."""
    spec = registry.require(provider_id)
    store.delete(spec.id, db)
    logger.info("Disconnected %s", spec.label)


def list_status(db: SettingsStore | None = None) -> dict[str, Any]:
    """Every provider's connection state plus which one is serving reviews."""
    return {
        "active_provider": store.get_active_provider(db),
        "providers": [store.status(pid, db) for pid in sorted(registry.all_providers())],
    }
