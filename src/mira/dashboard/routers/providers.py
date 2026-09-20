"""Dashboard routes for endpoints reached with an API key.

Admin-only. This is where an install is pointed at a model provider without
touching ``mira.yaml`` or the process environment: add an endpoint from a
preset or from a bare URL, give it a key, test it, make it the one bare
model ids go to, and — where the provider meters a subscription — see where
the allowance stands.

Two rules hold across every route here:

* **No route returns a key.** The cards carry where the key comes from and
  its last four characters, which is what tells two keys apart, and nothing
  else. A key is written and never read back.
* **The endpoint the config file names is shown, not edited.** The file is
  the authority for it; a form that appeared to edit something it cannot
  write would be a lie. Adding an endpoint here is how you move off it.
"""

from __future__ import annotations

import os

from fastapi import HTTPException, Request
from pydantic import BaseModel

from mira.dashboard import api as _api
from mira.dashboard.api import _require_admin, logger, router
from mira.llm import endpoints, key_providers
from mira.llm.endpoints import EndpointError


class EndpointBody(BaseModel):
    """The endpoint form. ``api_key`` is write-only and never returned.

    Every field is a tri-state on an edit: absent leaves what is stored
    alone, "" clears it, and a value replaces it. That matters most for
    ``api_key``, which the form does not have to send back — but it matters
    for the rest too, or a request that means "rename this" would also wipe
    the preset that says which endpoint it is and the variable its key is
    read from.
    """

    label: str | None = None
    base_url: str | None = None
    preset: str | None = None
    api_style: str | None = None
    api_key: str | None = None
    api_key_env: str | None = None
    model_prefix: str | None = None
    default_model: str | None = None
    # Make this the endpoint bare model ids go to, in the same request that
    # creates it — the common case, and one round trip instead of two.
    make_default: bool = False


class TestBody(BaseModel):
    """A connection test for values that may not be saved yet.

    ``api_key`` absent means "use whatever this endpoint already has", so
    the button works on a form the operator has not retyped the key into.
    """

    base_url: str = ""
    preset: str = ""
    api_key: str | None = None
    api_key_env: str = ""
    endpoint: str = ""


class ActiveBody(BaseModel):
    # "" hands bare model ids back to the endpoint the config file names.
    endpoint: str = ""


def _llm_config():  # type: ignore[no-untyped-def]
    from mira.config import LLMConfig, load_config

    try:
        return load_config().llm
    except Exception as exc:  # noqa: BLE001 - a broken config still gets a page
        logger.warning("Could not load the LLM config for the providers page: %s", exc)
        return LLMConfig()


def _db():  # type: ignore[no-untyped-def]
    return _api._app_db


def _endpoint_or_404(endpoint_id: str):  # type: ignore[no-untyped-def]
    endpoint = endpoints.get(endpoint_id, _db())
    if endpoint is None:
        raise HTTPException(status_code=404, detail=f"No endpoint {endpoint_id!r} is configured")
    return endpoint


@router.get("/api/providers")
async def list_providers(request: Request) -> dict:
    """Every configured endpoint, the presets to add one from, and the default."""
    _require_admin(request)
    config = _llm_config()
    return {
        "endpoints": await key_providers.list_status(config, _db()),
        "presets": endpoints.preset_options(),
        "active": endpoints.active(_db()),
        "env_candidates": key_providers.env_candidates(),
        # True when reviews have somewhere to go: an endpoint with a key, or
        # a signed-in account. The setup wizard asks for one when neither.
        "configured": _configured(config),
    }


def _configured(config) -> bool:  # type: ignore[no-untyped-def]
    """Can anything here serve a review right now?"""
    from mira.oauth import store as oauth_store

    if any(accounts for accounts in oauth_store.connected(_db()).values()):
        return True
    if config.provider == "bedrock":
        return True
    for endpoint in endpoints.all_endpoints(_db()).values():
        if endpoints.key_for(endpoint, _db()):
            return True
    return bool(key_providers.config_key(config))


@router.post("/api/providers")
async def create_provider(body: EndpointBody, request: Request) -> dict:
    """Add an endpoint, store its key, and optionally make it the default."""
    _require_admin(request)
    try:
        endpoint = endpoints.create(
            label=body.label or "",
            base_url=body.base_url or "",
            preset=body.preset or "",
            api_style=body.api_style or "",
            api_key_env=body.api_key_env or "",
            model_prefix=body.model_prefix or "",
            db=_db(),
        )
        if body.api_key:
            endpoints.set_secret(endpoint.id, body.api_key, _db())
        if body.make_default:
            endpoints.set_active(endpoint.id, _db())
            # Reviews have somewhere to go now; don't send the operator back
            # through setup.
            _db().mark_setup_complete()
    except (EndpointError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    logger.info("Added LLM endpoint %s (%s)", endpoint.id, endpoint.base_url)
    return await key_providers.endpoint_card(
        endpoint, is_default=endpoints.active(_db()) == endpoint.id, db=_db()
    )


@router.put("/api/providers/active")
def set_active_provider(body: ActiveBody, request: Request) -> dict:
    """Choose which endpoint bare model ids go to ("" = the config file's).

    Declared before ``/{endpoint_id}``, which would otherwise match it — and
    ``active`` is a reserved id for the same reason, so no stored endpoint
    can be the one this path shadows.

    A signed-in account still outranks this for bare ids — that choice lives
    on the same page — so the answer says which one is actually in front.
    """
    _require_admin(request)
    chosen = (body.endpoint or "").strip()
    if chosen == key_providers.CONFIG_ID:
        chosen = ""
    try:
        endpoints.set_active(chosen, _db())
    except EndpointError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if chosen:
        _db().mark_setup_complete()
    from mira.oauth import store as oauth_store

    return {
        "ok": True,
        "active": endpoints.active(_db()),
        "oauth_provider": oauth_store.get_active_provider(_db()),
    }


@router.post("/api/providers/test")
async def test_provider(body: TestBody, request: Request) -> dict:
    """Try a URL and a key before saving them, by asking for the model list.

    Answers 200 with ``ok: false`` for an endpoint that refused: "the key is
    wrong" is the result of the test, not a failure of the request, and the
    form shows it either way.
    """
    _require_admin(request)
    preset = endpoints.presets().get(body.preset) if body.preset else None
    profile = dict(preset or {})
    base_url = (body.base_url or profile.get("base_url") or "").strip()
    if not base_url:
        raise HTTPException(status_code=400, detail="Enter a URL to test")
    try:
        from mira.config import validate_base_url

        validate_base_url(base_url, "The endpoint URL")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    profile["base_url"] = base_url

    api_key = body.api_key or ""
    if not api_key:
        stored = endpoints.get(body.endpoint, _db()) if body.endpoint else None
        if stored is not None:
            api_key = endpoints.key_for(stored, _db())
        elif body.api_key_env:
            api_key = os.environ.get(body.api_key_env, "")
        elif profile.get("api_key_env"):
            api_key = os.environ.get(str(profile["api_key_env"]), "")
    return await key_providers.test_connection(profile, api_key)


@router.put("/api/providers/{endpoint_id}")
async def update_provider(endpoint_id: str, body: EndpointBody, request: Request) -> dict:
    """Edit an endpoint. A key is replaced only when one was sent."""
    _require_admin(request)
    endpoint = _endpoint_or_404(endpoint_id)
    if body.label:
        endpoint.label = body.label
    if body.base_url:
        endpoint.base_url = body.base_url
    if body.api_style:
        endpoint.api_style = body.api_style
    if body.api_key_env is not None:
        endpoint.api_key_env = body.api_key_env
    if body.model_prefix is not None:
        endpoint.model_prefix = body.model_prefix
    if body.default_model is not None:
        endpoint.default_model = body.default_model
    if body.preset is not None and body.preset != endpoint.preset:
        endpoint.preset = body.preset
        # The old preset's model is no longer this endpoint's to fall back
        # to; `save` fills in the new preset's, if the registry knows one.
        if body.default_model is None:
            endpoint.default_model = ""
    try:
        endpoints.save(endpoint, _db())
        if body.api_key is not None:
            endpoints.set_secret(endpoint.id, body.api_key, _db())
        if body.make_default:
            endpoints.set_active(endpoint.id, _db())
    except (EndpointError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    # A changed key or URL invalidates what was recorded against the old one.
    key_providers.forget_usage(endpoint.id, _db())
    logger.info("Updated LLM endpoint %s (%s)", endpoint.id, endpoint.base_url)
    return await key_providers.endpoint_card(
        endpoint, is_default=endpoints.active(_db()) == endpoint.id, db=_db()
    )


@router.delete("/api/providers/{endpoint_id}")
def delete_provider(endpoint_id: str, request: Request) -> dict:
    """Forget an endpoint, its key and its usage. The default falls back."""
    _require_admin(request)
    _endpoint_or_404(endpoint_id)
    endpoints.delete(endpoint_id, _db())
    logger.info("Removed LLM endpoint %s", endpoint_id)
    return {"ok": True, "active": endpoints.active(_db())}


@router.post("/api/providers/{endpoint_id}/usage")
async def refresh_provider_usage(endpoint_id: str, request: Request) -> dict:
    """Ask the provider where this endpoint's allowance stands right now."""
    _require_admin(request)
    try:
        return await key_providers.refresh(endpoint_id, _llm_config(), _db())
    except KeyError:
        raise HTTPException(status_code=404, detail=f"Unknown endpoint {endpoint_id!r}") from None
    except key_providers.UsageError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
