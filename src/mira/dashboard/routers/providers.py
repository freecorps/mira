"""Dashboard routes for API-key providers.

Admin-only and read-mostly: which key-based endpoints Mira knows, whether
each one's key is set, which of them the config points at, and — for a
provider that meters a subscription — where the key's allowance stands.
Nothing here returns key material, and nothing here changes where reviews
go: that is ``mira.yaml``'s ``llm`` section, or the Connections page's
signed-in default.
"""

from __future__ import annotations

from fastapi import HTTPException, Request

from mira.dashboard import api as _api
from mira.dashboard.api import _require_admin, logger, router
from mira.llm import key_providers


def _llm_config():  # type: ignore[no-untyped-def]
    from mira.config import LLMConfig, load_config

    try:
        return load_config().llm
    except Exception as exc:  # noqa: BLE001 - a broken config still gets a page
        logger.warning("Could not load the LLM config for the providers page: %s", exc)
        return LLMConfig()


@router.get("/api/providers/keys")
async def list_key_providers(request: Request) -> dict:
    """Every key-based provider Mira has a profile for, and its allowance."""
    _require_admin(request)
    return {"providers": await key_providers.list_status(_llm_config(), _api._app_db)}


@router.post("/api/providers/keys/{provider_id}/usage")
async def refresh_key_provider_usage(provider_id: str, request: Request) -> dict:
    """Ask the provider where the key's allowance stands right now."""
    _require_admin(request)
    try:
        return await key_providers.refresh(provider_id, _llm_config(), _api._app_db)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"Unknown provider {provider_id!r}") from None
    except key_providers.UsageError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
