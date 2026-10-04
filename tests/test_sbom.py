"""SBOM export: components from the inventory, CycloneDX and SPDX documents,
the org-wide view, the store migration, the CLI and the API routes.

No network anywhere: these exports read what indexing stored and the license
cache, and the API never contacts a registry.
"""

from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest
from click.testing import CliRunner
from fastapi import HTTPException

from mira.index.store import IndexStore
from mira.sbom import build_components, export_org, export_repo, purl, to_cyclonedx, to_spdx
from mira.sbom.inventory import DIRECT, TRANSITIVE, build_repo_inventory


@pytest.fixture(autouse=True)
def isolated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MIRA_INDEX_DIR", str(tmp_path / "index"))
    monkeypatch.delenv("DATABASE_URL", raising=False)
    # Belt and braces: nothing in an export may reach a registry.
    monkeypatch.setenv("MIRA_LICENSES_HOSTS", "")


def _row(name, kind, version, file_path, is_dev=False, license=""):
    return SimpleNamespace(
        name=name, kind=kind, version=version, file_path=file_path, is_dev=is_dev, license=license
    )


def _seed(store) -> None:
    store.replace_manifest_packages(
        "package.json",
        [
            {"name": "lodash", "kind": "npm", "version": "^4.17.0", "file_path": "package.json"},
            {
                "name": "@babel/core",
                "kind": "npm",
                "version": "^7.0.0",
                "file_path": "package.json",
                "is_dev": True,
            },
            {"name": "left-pad", "kind": "npm", "version": "^1.3.0", "file_path": "package.json"},
        ],
    )
    store.replace_manifest_packages(
        "package-lock.json",
        [
            {
                "name": "lodash",
                "kind": "npm",
                "version": "4.17.21",
                "file_path": "package-lock.json",
                "license": "MIT",
            },
            {
                "name": "@babel/core",
                "kind": "npm",
                "version": "7.24.0",
                "file_path": "package-lock.json",
                "is_dev": True,
                "license": "MIT",
            },
            {
                "name": "ms",
                "kind": "npm",
                "version": "2.1.3",
                "file_path": "package-lock.json",
                "license": "Weird-License",
            },
            {
                "name": "dual",
                "kind": "npm",
                "version": "1.0.0",
                "file_path": "package-lock.json",
                "license": "(MIT OR Apache-2.0)",
            },
        ],
    )
    store.replace_manifest_packages(
        "go.mod",
        [
            {
                "name": "github.com/pkg/errors",
                "kind": "go",
                "version": "v0.9.1",
                "file_path": "go.mod",
            },
            {
                "name": "golang.org/x/sys",
                "kind": "go",
                "version": "v0.1.0",
                "file_path": "go.mod",
                "is_dev": True,  # `// indirect`
            },
        ],
    )
    store.replace_manifest_packages(
        "requirements.txt",
        [
            {
                "name": "Requests_OAuthlib",
                "kind": "pip",
                "version": "==1.3.1",
                "file_path": "requirements.txt",
            }
        ],
    )
    store.replace_manifest_packages(
        "Dockerfile",
        [{"name": "python", "kind": "docker", "version": "3.12-slim", "file_path": "Dockerfile"}],
    )


@pytest.fixture
def store(tmp_path: Path):
    s = IndexStore.open("acme", "api")
    _seed(s)
    yield s
    s.close()


# ── Package URLs ─────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("kind", "name", "version", "expected"),
    [
        ("npm", "lodash", "4.17.21", "pkg:npm/lodash@4.17.21"),
        ("npm", "@babel/core", "7.24.0", "pkg:npm/%40babel/core@7.24.0"),
        ("pip", "Requests_OAuthlib", "1.3.1", "pkg:pypi/requests-oauthlib@1.3.1"),
        ("go", "github.com/pkg/errors", "v0.9.1", "pkg:golang/github.com/pkg/errors@v0.9.1"),
        ("rust", "serde", "1.0.0", "pkg:cargo/serde@1.0.0"),
        ("composer", "monolog/monolog", "3.5.0", "pkg:composer/monolog/monolog@3.5.0"),
        ("docker", "python", "3.12-slim", "pkg:docker/python@3.12-slim"),
        (
            "docker",
            "ghcr.io/acme/tool",
            "1.0",
            "pkg:docker/acme/tool@1.0?repository_url=ghcr.io",
        ),
        ("npm", "lodash", "", "pkg:npm/lodash"),
        ("pip", "pkg", "1.0+local", "pkg:pypi/pkg@1.0%2Blocal"),
    ],
)
def test_purl(kind: str, name: str, version: str, expected: str) -> None:
    assert purl(kind, name, version) == expected


# ── Components ───────────────────────────────────────────────────────


def test_lockfile_version_wins_and_scope_is_derived() -> None:
    comps = build_components(
        [
            _row("lodash", "npm", "^4.17.0", "package.json"),
            _row("lodash", "npm", "4.17.21", "package-lock.json", license="MIT"),
            _row("ms", "npm", "2.1.3", "package-lock.json"),
            _row("left-pad", "npm", "^1.3.0", "package.json"),
        ]
    )
    by_name = {c.name: c for c in comps}
    assert set(by_name) == {"lodash", "ms", "left-pad"}
    assert by_name["lodash"].version == "4.17.21"
    assert by_name["lodash"].scope == DIRECT
    assert by_name["lodash"].recorded_license == "MIT"
    assert by_name["ms"].scope == TRANSITIVE
    # Only a constraint is known: no version, the constraint kept.
    assert by_name["left-pad"].version == ""
    assert by_name["left-pad"].constraint == "^1.3.0"


def test_go_indirect_is_transitive_not_dev() -> None:
    comps = build_components(
        [
            _row("github.com/a/b", "go", "v1.0.0", "go.mod"),
            _row("github.com/c/d", "go", "v2.0.0", "go.mod", is_dev=True),
        ]
    )
    by_name = {c.name: c for c in comps}
    assert by_name["github.com/a/b"].scope == DIRECT
    assert by_name["github.com/c/d"].scope == TRANSITIVE
    assert not by_name["github.com/c/d"].dev


def test_dev_only_when_every_row_is_dev() -> None:
    comps = build_components(
        [
            _row("x", "npm", "1.0.0", "a/package-lock.json", is_dev=True),
            _row("x", "npm", "1.0.0", "b/package-lock.json", is_dev=False),
            _row("y", "npm", "1.0.0", "package-lock.json", is_dev=True),
        ]
    )
    by_name = {c.name: c for c in comps}
    assert not by_name["x"].dev
    assert by_name["x"].files == ["a/package-lock.json", "b/package-lock.json"]
    assert by_name["y"].dev


def test_pinned_requirement_is_a_version() -> None:
    (comp,) = build_components([_row("requests", "pip", "==2.32.3", "requirements.txt")])
    assert comp.version == "2.32.3"
    assert comp.purl == "pkg:pypi/requests@2.32.3"


# ── CycloneDX ────────────────────────────────────────────────────────


def _check_cyclonedx(doc: dict, spec: str) -> None:
    assert doc["bomFormat"] == "CycloneDX"
    assert doc["specVersion"] == spec
    assert doc["$schema"].endswith(f"bom-{spec}.schema.json")
    assert re.fullmatch(r"urn:uuid:[0-9a-f-]{36}", doc["serialNumber"])
    assert doc["version"] == 1
    meta = doc["metadata"]
    assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ", meta["timestamp"])
    assert meta["tools"]["components"][0]["name"] == "mira"
    refs = [c["bom-ref"] for c in doc["components"]]
    assert len(refs) == len(set(refs)), "bom-refs must be unique"
    known = set(refs) | {meta["component"]["bom-ref"]}
    for dep in doc["dependencies"]:
        assert dep["ref"] in known
        assert set(dep["dependsOn"]) <= known
    for c in doc["components"]:
        assert c["type"] in ("library", "container", "application")
        assert c["name"]
        if c["type"] != "application":
            assert c["purl"].startswith("pkg:")
            assert c["scope"] in ("required", "optional", "excluded")
        for lic in c.get("licenses", []):
            assert ("license" in lic) ^ ("expression" in lic)
            if "license" in lic:
                assert ("id" in lic["license"]) ^ ("name" in lic["license"])
        for prop in c.get("properties", []):
            assert set(prop) == {"name", "value"} and isinstance(prop["value"], str)


@pytest.mark.parametrize("spec", ["1.5", "1.6"])
async def test_cyclonedx_document(store, spec: str) -> None:
    doc = await export_repo(store, "acme/api", "cyclonedx", spec_version=spec)
    _check_cyclonedx(doc, spec)
    assert doc["metadata"]["component"]["name"] == "acme/api"
    comps = {c["purl"]: c for c in doc["components"]}

    babel = comps["pkg:npm/%40babel/core@7.24.0"]
    assert babel["group"] == "@babel" and babel["name"] == "core"
    assert babel["scope"] == "excluded"
    assert babel["licenses"] == [{"license": {"id": "MIT"}}]

    assert comps["pkg:npm/dual@1.0.0"]["licenses"] == [{"expression": "MIT OR Apache-2.0"}]
    # Not an SPDX id: a name, never an invalid id.
    assert comps["pkg:npm/ms@2.1.3"]["licenses"] == [{"license": {"name": "Weird-License"}}]
    # Unknown: omitted rather than guessed.
    assert "licenses" not in comps["pkg:pypi/requests-oauthlib@1.3.1"]
    assert comps["pkg:docker/python@3.12-slim"]["type"] == "container"

    left_pad = comps["pkg:npm/left-pad"]
    assert "version" not in left_pad
    assert {"name": "mira:version_constraint", "value": "^1.3.0"} in left_pad["properties"]

    (root_deps,) = doc["dependencies"]
    assert "pkg:npm/lodash@4.17.21" in root_deps["dependsOn"]
    assert "pkg:npm/ms@2.1.3" not in root_deps["dependsOn"]  # transitive
    assert "pkg:golang/golang.org/x/sys@v0.1.0" not in root_deps["dependsOn"]
    ms_props = comps["pkg:npm/ms@2.1.3"]["properties"]
    assert {"name": "mira:dependency", "value": "transitive"} in ms_props
    assert {"name": "mira:license_source", "value": "lockfile"} in ms_props
    json.dumps(doc)  # serializable as-is


def test_cyclonedx_rejects_unknown_spec_version() -> None:
    with pytest.raises(ValueError):
        to_cyclonedx([], spec_version="1.4")


def test_org_cyclonedx_shared_component_is_dev_only_if_dev_everywhere() -> None:
    from mira.sbom.inventory import Component, RepoInventory

    def comp(dev: bool, scope: str) -> Component:
        return Component("npm", "left-pad", "1.3.0", scope=scope, dev=dev)

    for first, second in (
        ((True, TRANSITIVE), (False, DIRECT)),
        ((False, DIRECT), (True, TRANSITIVE)),
    ):
        doc = to_cyclonedx(
            [RepoInventory("o/a", [comp(*first)]), RepoInventory("o/b", [comp(*second)])],
            name="o",
        )
        lib = next(c for c in doc["components"] if c["name"] == "left-pad")
        assert lib["scope"] == "required"
        props = lib["properties"]
        assert {"name": "mira:dependency", "value": DIRECT} in props
        assert {"name": "mira:dev", "value": "true"} not in props
        assert [p["value"] for p in props if p["name"] == "mira:repository"] == ["o/a", "o/b"]

    both_dev = to_cyclonedx(
        [RepoInventory("o/a", [comp(True, DIRECT)]), RepoInventory("o/b", [comp(True, DIRECT)])],
        name="o",
    )
    lib = next(c for c in both_dev["components"] if c["name"] == "left-pad")
    assert lib["scope"] == "excluded"


# ── SPDX ─────────────────────────────────────────────────────────────

_SPDX_ID = re.compile(r"^SPDXRef-[A-Za-z0-9.\-]+$")


def _check_spdx(doc: dict) -> None:
    assert doc["spdxVersion"] == "SPDX-2.3"
    assert doc["dataLicense"] == "CC0-1.0"
    assert doc["SPDXID"] == "SPDXRef-DOCUMENT"
    assert doc["name"]
    assert doc["documentNamespace"].startswith("https://")
    assert doc["creationInfo"]["creators"][0].startswith("Tool: mira-")
    assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ", doc["creationInfo"]["created"])
    ids = [p["SPDXID"] for p in doc["packages"]]
    assert len(ids) == len(set(ids))
    assert all(_SPDX_ID.match(i) for i in ids)
    known = set(ids) | {"SPDXRef-DOCUMENT"}
    for rel in doc["relationships"]:
        assert rel["spdxElementId"] in known
        assert rel["relatedSpdxElement"] in known
        assert rel["relationshipType"] in ("DESCRIBES", "DEPENDS_ON", "DEV_DEPENDENCY_OF")
    refs_declared = {e["licenseId"] for e in doc.get("hasExtractedLicensingInfos", [])}
    for p in doc["packages"]:
        for key in ("downloadLocation", "licenseConcluded", "licenseDeclared", "copyrightText"):
            assert p[key]
        assert p["filesAnalyzed"] is False
        used = set(re.findall(r"LicenseRef-[A-Za-z0-9.\-]+", p["licenseDeclared"]))
        assert used <= refs_declared, "every LicenseRef needs its extracted text"
        for ref in p.get("externalRefs", []):
            assert ref["referenceType"] == "purl"
            assert ref["referenceLocator"].startswith("pkg:")


async def test_spdx_document(store) -> None:
    doc = await export_repo(store, "acme/api", "spdx")
    _check_spdx(doc)
    pkgs = {p["name"]: p for p in doc["packages"]}
    assert pkgs["acme/api"]["primaryPackagePurpose"] == "APPLICATION"
    assert pkgs["lodash"]["licenseDeclared"] == "MIT"
    assert pkgs["lodash"]["versionInfo"] == "4.17.21"
    assert pkgs["dual"]["licenseDeclared"] == "MIT OR Apache-2.0"
    assert pkgs["ms"]["licenseDeclared"] == "LicenseRef-Weird-License"
    assert pkgs["Requests_OAuthlib"]["licenseDeclared"] == "NOASSERTION"
    assert "transitive" in pkgs["ms"]["comment"]
    assert "versionInfo" not in pkgs["left-pad"]

    rels = {
        (r["spdxElementId"], r["relationshipType"], r["relatedSpdxElement"])
        for r in doc["relationships"]
    }
    repo_id = pkgs["acme/api"]["SPDXID"]
    assert ("SPDXRef-DOCUMENT", "DESCRIBES", repo_id) in rels
    assert (repo_id, "DEPENDS_ON", pkgs["lodash"]["SPDXID"]) in rels
    assert (pkgs["@babel/core"]["SPDXID"], "DEV_DEPENDENCY_OF", repo_id) in rels


def test_spdx_ids_stay_unique_for_lookalike_names() -> None:
    from mira.sbom.inventory import RepoInventory

    comps = build_components(
        [
            _row("a.b", "npm", "1.0.0", "package-lock.json"),
            _row("a_b", "npm", "1.0.0", "package-lock.json"),
        ]
    )
    doc = to_spdx([RepoInventory("o/r", comps)])
    _check_spdx(doc)


# ── Licenses in the inventory: cache, never the network ──────────────


async def test_inventory_uses_the_license_cache(store) -> None:
    store.upsert_package_licenses([("pip", "requests-oauthlib", "1.3.1", "ISC", "pypi")])
    inv = await build_repo_inventory(store, "acme/api")
    comp = next(c for c in inv.components if c.kind == "pip")
    assert comp.license_expression == "ISC"
    assert comp.license.source == "cache"


# ── Org-wide ─────────────────────────────────────────────────────────


async def test_org_wide_document_lists_each_repository() -> None:
    a = IndexStore.open("acme", "api")
    b = IndexStore.open("acme", "web")
    try:
        _seed(a)
        b.replace_manifest_packages(
            "package-lock.json",
            [
                {
                    "name": "lodash",
                    "kind": "npm",
                    "version": "4.17.21",
                    "file_path": "package-lock.json",
                    "license": "MIT",
                }
            ],
        )
    finally:
        a.close()
        b.close()

    doc = await export_org("cyclonedx")
    _check_cyclonedx(doc, "1.6")
    apps = [c for c in doc["components"] if c["type"] == "application"]
    assert {c["name"] for c in apps} == {"acme/api", "acme/web"}
    lodash = [c for c in doc["components"] if c.get("purl") == "pkg:npm/lodash@4.17.21"]
    assert len(lodash) == 1, "a shared library appears once"
    repos = {p["value"] for p in lodash[0]["properties"] if p["name"] == "mira:repository"}
    assert repos == {"acme/api", "acme/web"}
    root = doc["dependencies"][0]
    assert root["ref"] == doc["metadata"]["component"]["bom-ref"]
    assert len(root["dependsOn"]) == 2

    spdx = await export_org("spdx")
    _check_spdx(spdx)
    describes = [r for r in spdx["relationships"] if r["relationshipType"] == "DESCRIBES"]
    assert len(describes) == 2


async def test_org_wide_with_nothing_indexed_is_an_empty_document() -> None:
    doc = await export_org("spdx")
    _check_spdx(doc)
    assert doc["packages"] == []


# ── Storage migration ────────────────────────────────────────────────


def test_an_old_index_gains_the_license_column(tmp_path: Path) -> None:
    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE package_manifests (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, "
        "kind TEXT NOT NULL DEFAULT '', version TEXT NOT NULL DEFAULT '', "
        "file_path TEXT NOT NULL DEFAULT '', is_dev INTEGER NOT NULL DEFAULT 0, "
        "updated_at REAL NOT NULL DEFAULT 0, UNIQUE(name, kind, file_path))"
    )
    conn.execute(
        "INSERT INTO package_manifests (name, kind, version, file_path) "
        "VALUES ('lodash', 'npm', '4.17.21', 'package-lock.json')"
    )
    conn.commit()
    conn.close()

    s = IndexStore(str(path), owner="o", repo="r")
    try:
        (row,) = s.list_manifest_packages()
        assert row.license == ""
        s.replace_manifest_packages(
            "package-lock.json",
            [
                {
                    "name": "lodash",
                    "kind": "npm",
                    "version": "4.17.21",
                    "file_path": "package-lock.json",
                    "license": "MIT",
                }
            ],
        )
        assert s.list_manifest_packages()[0].license == "MIT"
        assert s.get_package_licenses([("npm", "lodash", "4.17.21")]) == {}
    finally:
        s.close()


# ── CLI ──────────────────────────────────────────────────────────────


def test_cli_sbom_for_one_repository(store, tmp_path: Path) -> None:
    from mira.cli import main

    out = tmp_path / "api.cdx.json"
    result = CliRunner().invoke(
        main, ["sbom", "--repo", "acme/api", "--spec-version", "1.5", "-o", str(out)]
    )
    assert result.exit_code == 0, result.output
    _check_cyclonedx(json.loads(out.read_text()), "1.5")

    result = CliRunner().invoke(main, ["sbom", "--repo", "acme/api", "--format", "spdx"])
    assert result.exit_code == 0, result.output
    _check_spdx(json.loads(result.stdout))


def test_cli_sbom_org_wide(store) -> None:
    from mira.cli import main

    result = CliRunner().invoke(main, ["sbom", "--all"])
    assert result.exit_code == 0, result.output
    doc = json.loads(result.stdout)
    assert any(c["name"] == "acme/api" for c in doc["components"])


def test_cli_sbom_refuses_an_unindexed_repository(tmp_path: Path) -> None:
    from mira.cli import main

    result = CliRunner().invoke(main, ["sbom", "--repo", "nobody/nothing"])
    assert result.exit_code != 0
    assert "has not been indexed" in result.output
    assert not Path(IndexStore.db_path_for("nobody", "nothing")).exists()


def test_cli_sbom_needs_exactly_one_subject() -> None:
    from mira.cli import main

    assert CliRunner().invoke(main, ["sbom"]).exit_code != 0
    assert CliRunner().invoke(main, ["sbom", "--all", "--repo", "a/b"]).exit_code != 0


# ── API ──────────────────────────────────────────────────────────────


class _Registry:
    def get_repo_any_platform(self, owner, repo):
        return [SimpleNamespace(platform="github")] if (owner, repo) == ("acme", "api") else []

    def list_repos(self):
        return [SimpleNamespace(owner="acme", repo="api", platform="github")]


@pytest.fixture
def api_registry(monkeypatch: pytest.MonkeyPatch) -> None:
    from mira.dashboard import api

    monkeypatch.setattr(api, "_app_db", _Registry())


async def test_api_repo_sbom_is_a_download(store, api_registry) -> None:
    from mira.dashboard.routers.sbom import get_repo_sbom

    resp = await get_repo_sbom("acme", "api", format="spdx", spec_version="1.6")
    assert resp.media_type == "application/spdx+json"
    assert resp.headers["content-disposition"] == 'attachment; filename="acme-api.spdx.json"'
    _check_spdx(json.loads(resp.body))

    resp = await get_repo_sbom("acme", "api", format="cyclonedx", spec_version="1.5")
    assert resp.media_type == "application/vnd.cyclonedx+json"
    _check_cyclonedx(json.loads(resp.body), "1.5")


async def test_api_rejects_bad_parameters_and_unknown_repos(store, api_registry) -> None:
    from mira.dashboard.routers.sbom import get_org_sbom, get_repo_sbom

    with pytest.raises(HTTPException) as exc:
        await get_repo_sbom("acme", "api", format="xml", spec_version="1.6")
    assert exc.value.status_code == 400
    with pytest.raises(HTTPException) as exc:
        await get_org_sbom(format="cyclonedx", spec_version="2.0")
    assert exc.value.status_code == 400
    with pytest.raises(HTTPException) as exc:
        await get_repo_sbom("acme", "missing", format="spdx", spec_version="1.6")
    assert exc.value.status_code == 404


async def test_api_org_sbom(store, api_registry) -> None:
    from mira.dashboard.routers.sbom import get_org_sbom

    resp = await get_org_sbom(format="cyclonedx", spec_version="1.6")
    doc = json.loads(resp.body)
    _check_cyclonedx(doc, "1.6")
    assert any(c["name"] == "acme/api" for c in doc["components"])


async def test_api_spdx_takes_its_own_spec_version(store, api_registry) -> None:
    from mira.dashboard.routers.sbom import get_org_sbom, get_repo_sbom

    resp = await get_repo_sbom("acme", "api", format="spdx", spec_version="2.3")
    _check_spdx(json.loads(resp.body))
    _check_spdx(json.loads((await get_org_sbom(format="spdx", spec_version="2.3")).body))
    with pytest.raises(HTTPException) as exc:
        await get_repo_sbom("acme", "api", format="cyclonedx", spec_version="2.3")
    assert exc.value.status_code == 400
    with pytest.raises(HTTPException) as exc:
        await get_repo_sbom("acme", "api", format="spdx", spec_version="3.0")
    assert exc.value.status_code == 400


async def test_api_org_sbom_skips_a_repo_whose_inventory_fails(
    store, monkeypatch: pytest.MonkeyPatch
) -> None:
    from mira.dashboard import api
    from mira.dashboard.routers import sbom as sbom_router

    class _Two(_Registry):
        def list_repos(self):
            return [
                SimpleNamespace(owner="acme", repo="broken", platform="github"),
                SimpleNamespace(owner="acme", repo="api", platform="github"),
            ]

    monkeypatch.setattr(api, "_app_db", _Two())
    real = sbom_router.build_repo_inventory

    async def flaky(store, label, *args, **kwargs):
        if label.endswith("broken"):
            raise sqlite3.OperationalError("no such table: packages")
        return await real(store, label, *args, **kwargs)

    monkeypatch.setattr(sbom_router, "build_repo_inventory", flaky)
    doc = json.loads((await sbom_router.get_org_sbom(format="cyclonedx", spec_version="1.6")).body)
    names = {c["name"] for c in doc["components"]}
    assert "acme/api" in names
    assert "acme/broken" not in names


def test_api_packages_carry_licenses(store, api_registry) -> None:
    from mira.dashboard.routers.repos import get_packages

    store.upsert_package_licenses([("pip", "requests-oauthlib", "1.3.1", "BSD License", "pypi")])
    pkgs = {p.name: p for p in get_packages("acme", "api")}
    assert pkgs["lodash"].license == "MIT"
    assert pkgs["Requests_OAuthlib"].license == "BSD-3-Clause"
    assert pkgs["python"].license == ""
