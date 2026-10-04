"""Dashboard SBOM routes: CycloneDX / SPDX downloads from the package inventory.

Read-only, and offline: licenses come from what the lockfiles recorded and
from the registry cache the review-time license check fills. Asking a registry
from a page load would make a download as slow as the slowest registry.
"""

from __future__ import annotations

import json
import re

from fastapi import HTTPException, Response

from mira.dashboard import api as _api
from mira.dashboard.api import _open_store, router
from mira.index.store import IndexStore
from mira.sbom import CYCLONEDX_VERSIONS, FORMATS, render
from mira.sbom.inventory import build_repo_inventory, repo_label

_FILENAME_UNSAFE = re.compile(r"[^A-Za-z0-9._-]+")


def _check(fmt: str, spec_version: str) -> None:
    if fmt not in FORMATS:
        raise HTTPException(status_code=400, detail=f"format must be one of {list(FORMATS)}")
    if spec_version not in CYCLONEDX_VERSIONS:
        raise HTTPException(
            status_code=400, detail=f"spec_version must be one of {list(CYCLONEDX_VERSIONS)}"
        )


def _download(doc: dict, stem: str, fmt: str) -> Response:
    suffix = "cdx.json" if fmt == "cyclonedx" else "spdx.json"
    filename = f"{_FILENAME_UNSAFE.sub('-', stem).strip('-') or 'sbom'}.{suffix}"
    media = "application/vnd.cyclonedx+json" if fmt == "cyclonedx" else "application/spdx+json"
    return Response(
        content=json.dumps(doc, indent=2),
        media_type=media,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/api/repos/{owner}/{repo}/sbom")
async def get_repo_sbom(
    owner: str, repo: str, format: str = "cyclonedx", spec_version: str = "1.6"
) -> Response:
    """One repository's SBOM, as a file download."""
    _check(format, spec_version)
    with _open_store(owner, repo) as store:
        inventory = await build_repo_inventory(store, f"{owner}/{repo}")
    return _download(
        render([inventory], format, spec_version=spec_version), f"{owner}-{repo}", format
    )


@router.get("/api/sbom")
async def get_org_sbom(format: str = "cyclonedx", spec_version: str = "1.6") -> Response:
    """Every repository the dashboard tracks, in one SBOM."""
    _check(format, spec_version)
    inventories = []
    for record in _api._app_db.list_repos():
        try:
            store = IndexStore.open(record.owner, record.repo, platform=record.platform)
        except Exception as exc:  # noqa: BLE001 - one unreadable index is not the org's SBOM
            _api.logger.warning("SBOM: %s/%s skipped: %s", record.owner, record.repo, exc)
            continue
        try:
            inv = await build_repo_inventory(
                store, repo_label(record.owner, record.repo, record.platform)
            )
        finally:
            store.close()
        if inv.components:
            inventories.append(inv)
    doc = render(inventories, format, name="organization", spec_version=spec_version)
    return _download(doc, "organization", format)
