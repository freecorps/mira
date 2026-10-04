"""From the package inventory to SBOM components.

The index already keeps every package its manifests and lockfiles declare
(``package_manifests``). An SBOM wants something slightly different: one entry
per package *version*, with a package URL, whether the repository asked for it
(direct) or only got it through something else (transitive), and its license.

* A lockfile row wins over a manifest row for the same package: the lockfile
  has the version that is actually installed. A manifest constraint
  (``^4.18``) is kept only when no lockfile resolves the package.
* Direct means some manifest declares the package; a package only a lockfile
  names is transitive. ``go.mod`` marks indirect requirements itself (the index
  stores them with ``is_dev`` set, as it has since before this module).
* A package is a dev dependency only when every row naming it is dev.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import quote

from mira.index.manifests import _is_lockfile_path
from mira.licenses.lookup import LicenseInfo, PackageRef, canonical_name, concrete_version

DIRECT = "direct"
TRANSITIVE = "transitive"

# Mira's ecosystem key → package URL type (https://github.com/package-url/purl-spec).
PURL_TYPES = {
    "npm": "npm",
    "pip": "pypi",
    "go": "golang",
    "rust": "cargo",
    "composer": "composer",
    "docker": "docker",
}


@dataclass
class Component:
    kind: str
    name: str
    version: str  # the concrete version, "" when only a constraint is known
    constraint: str = ""  # the manifest's constraint when there is no concrete version
    scope: str = DIRECT
    dev: bool = False
    files: list[str] = field(default_factory=list)
    recorded_license: str = ""
    license: LicenseInfo | None = None

    @property
    def purl(self) -> str:
        return purl(self.kind, self.name, self.version)

    @property
    def ref(self) -> PackageRef:
        return PackageRef(self.kind, self.name, self.version, self.recorded_license)

    @property
    def license_expression(self) -> str:
        return self.license.expression if self.license else ""


@dataclass
class RepoInventory:
    name: str  # "owner/repo", or "gitlab:group/repo" off GitHub
    components: list[Component] = field(default_factory=list)


def purl(kind: str, name: str, version: str = "") -> str:
    """The package URL for a package, e.g. ``pkg:npm/%40babel/core@7.24.0``."""
    ptype = PURL_TYPES.get(kind, kind or "generic")
    qualifiers = ""
    if kind == "pip":
        name = canonical_name("pip", name)
    if kind == "docker":
        parts = name.split("/")
        if len(parts) > 1 and ("." in parts[0] or ":" in parts[0]):
            qualifiers = "?repository_url=" + quote(parts[0], safe="")
            name = "/".join(parts[1:])
    path = "/".join(quote(seg, safe="") for seg in name.split("/") if seg)
    out = f"pkg:{ptype}/{path}"
    if version:
        out += "@" + quote(version, safe="")
    return out + qualifiers


def build_components(rows: list[Any]) -> list[Component]:
    """Collapse ``package_manifests`` rows into one component per package version."""
    direct: set[tuple[str, str]] = set()
    for r in rows:
        if _is_lockfile_path(r.file_path):
            continue
        if r.kind == "go" and r.is_dev:  # `// indirect` in go.mod
            continue
        direct.add((r.kind, r.name.lower()))

    resolved: set[tuple[str, str]] = {
        (r.kind, r.name.lower()) for r in rows if concrete_version(r.version)
    }
    by_key: dict[tuple[str, str, str], Component] = {}
    dev_votes: dict[tuple[str, str, str], list[bool]] = {}
    for r in rows:
        ident = (r.kind, r.name.lower())
        version = concrete_version(r.version)
        if not version and ident in resolved:
            continue  # a constraint, when some row knows the real version
        version = version.lstrip("=") if version else ""
        key = (r.kind, r.name.lower(), version)
        comp = by_key.get(key)
        if comp is None:
            comp = Component(
                kind=r.kind,
                name=r.name,
                version=version,
                constraint="" if version else (r.version or ""),
                scope=DIRECT if ident in direct else TRANSITIVE,
            )
            by_key[key] = comp
        if r.file_path not in comp.files:
            comp.files.append(r.file_path)
        recorded = getattr(r, "license", "") or ""
        if recorded and not comp.recorded_license:
            comp.recorded_license = recorded
        is_dev = bool(r.is_dev) and r.kind != "go"
        dev_votes.setdefault(key, []).append(is_dev)
    for key, comp in by_key.items():
        comp.dev = all(dev_votes[key])
    return sorted(by_key.values(), key=lambda c: (c.kind, c.name.lower(), c.version))


async def build_repo_inventory(
    store: Any,
    name: str,
    config: Any = None,
    *,
    lookup: bool = False,
    client: Any = None,
) -> RepoInventory:
    """One repository's components with their licenses (lockfile, cache, registry)."""
    from mira.config import LicensesConfig
    from mira.licenses.lookup import resolve_licenses

    components = build_components(store.list_manifest_packages())
    cfg = config if config is not None else LicensesConfig()
    licenses = await resolve_licenses(
        [c.ref for c in components if c.kind != "docker"], cfg, store, lookup=lookup, client=client
    )
    for c in components:
        c.license = licenses.get(c.ref.key)
    return RepoInventory(name=name, components=components)


def repo_label(owner: str, repo: str, platform: str = "github") -> str:
    return f"{owner}/{repo}" if platform == "github" else f"{platform}:{owner}/{repo}"


@contextmanager
def open_repo_stores() -> Iterator[list[tuple[str, Any]]]:
    """Every indexed repository with packages, as ``(label, store)`` pairs.

    Postgres when ``DATABASE_URL`` names one, else the per-repository SQLite
    files under ``MIRA_INDEX_DIR``. The stores are closed on exit.
    """
    stores: list[tuple[str, Any]] = []
    db_url = os.environ.get("DATABASE_URL", "")
    try:
        if db_url.startswith(("postgresql://", "postgres://")):
            from mira.index.pg_store import PgIndexStore, list_package_repos

            for owner_key, repo in list_package_repos(db_url):
                if owner_key.startswith("_") and "/" in owner_key:
                    platform, owner = owner_key[1:].split("/", 1)
                else:
                    platform, owner = "github", owner_key
                stores.append(
                    (repo_label(owner, repo, platform), PgIndexStore(owner_key, repo, db_url))
                )
        else:
            from mira.index.store import _INDEX_DIR, IndexStore, _iter_repo_dbs

            index_dir = os.environ.get("MIRA_INDEX_DIR", _INDEX_DIR)
            for platform, owner, repo, path in _iter_repo_dbs(index_dir):
                try:
                    store = IndexStore(path, owner=owner, repo=repo, platform=platform)
                except Exception:  # noqa: BLE001 - one unreadable index is not the org's SBOM
                    continue
                stores.append((repo_label(owner, repo, platform), store))
        yield stores
    finally:
        for _, opened in stores:
            with suppress(Exception):
                opened.close()
