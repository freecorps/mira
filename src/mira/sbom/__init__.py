"""Software bill of materials export from the package inventory. No model is called.

``mira sbom``, ``GET /api/repos/{owner}/{repo}/sbom`` and ``GET /api/sbom``
all go through :func:`export_repo` / :func:`export_org`. See
docs/sbom-licenses.md.
"""

from __future__ import annotations

from typing import Any

from mira.sbom.formats import CYCLONEDX_VERSIONS, FORMATS, render, to_cyclonedx, to_spdx
from mira.sbom.inventory import (
    Component,
    RepoInventory,
    build_components,
    build_repo_inventory,
    open_repo_stores,
    purl,
)

__all__ = [
    "CYCLONEDX_VERSIONS",
    "FORMATS",
    "Component",
    "RepoInventory",
    "build_components",
    "build_repo_inventory",
    "export_org",
    "export_repo",
    "open_repo_stores",
    "purl",
    "render",
    "to_cyclonedx",
    "to_spdx",
]


async def export_repo(
    store: Any,
    name: str,
    fmt: str,
    *,
    spec_version: str = "1.6",
    config: Any = None,
    lookup: bool = False,
    client: Any = None,
) -> dict:
    """One repository's SBOM document."""
    inventory = await build_repo_inventory(store, name, config, lookup=lookup, client=client)
    return render([inventory], fmt, spec_version=spec_version)


async def export_org(
    fmt: str,
    *,
    name: str = "organization",
    spec_version: str = "1.6",
    config: Any = None,
    lookup: bool = False,
    client: Any = None,
) -> dict:
    """Every indexed repository's packages in one SBOM document."""
    inventories: list[RepoInventory] = []
    with open_repo_stores() as stores:
        for label, store in stores:
            inv = await build_repo_inventory(store, label, config, lookup=lookup, client=client)
            if inv.components:
                inventories.append(inv)
    return render(inventories, fmt, name=name, spec_version=spec_version)
