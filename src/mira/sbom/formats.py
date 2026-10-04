"""CycloneDX 1.5/1.6 and SPDX 2.3 JSON documents from repository inventories.

Both writers take a list of :class:`~mira.sbom.inventory.RepoInventory`: one
for a repository's SBOM, several for the organisation's. With several, each
repository is itself a component (CycloneDX ``application``, SPDX package) and
a library used by several repositories appears once, related to each of them.

Licenses go in as SPDX expressions. An identifier SPDX does not list becomes a
``LicenseRef-…`` (with its text recorded in SPDX's
``hasExtractedLicensingInfos``) rather than an invalid expression, and an
unknown license is ``NOASSERTION`` in SPDX and omitted in CycloneDX — never a
guess.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import replace
from datetime import UTC, datetime

from mira import __version__
from mira.licenses.expressions import (
    SPDX_IDS,
    LicenseParseError,
    Node,
    license_ref,
    parse_expression,
)
from mira.sbom.inventory import DIRECT, Component, RepoInventory

CYCLONEDX_VERSIONS = ("1.5", "1.6")
FORMATS = ("cyclonedx", "spdx")


def _now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _spdx_expression(expression: str) -> tuple[str, dict[str, str]]:
    """``expression`` with unlisted ids as LicenseRefs, and ``{ref: original}``."""
    if not expression:
        return "", {}
    try:
        tree = parse_expression(expression)
    except LicenseParseError:
        ref = license_ref(expression)
        return ref, {ref: expression}
    refs: dict[str, str] = {}

    def fix(node: Node) -> Node:
        if node.op == "id":
            if node.value in SPDX_IDS or node.value.startswith("LicenseRef-"):
                return node
            ref = license_ref(node.value)
            refs[ref] = node.value
            return replace(node, value=ref)
        return replace(node, children=tuple(fix(c) for c in node.children))

    return fix(tree).render(), refs


def _cdx_licenses(component: Component) -> list[dict]:
    expression = component.license_expression
    if not expression:
        return []
    try:
        tree = parse_expression(expression)
    except LicenseParseError:
        return [{"license": {"name": expression}}]
    if tree.op == "id" and not tree.exception:
        key = "id" if tree.value in SPDX_IDS else "name"
        return [{"license": {key: tree.value}}]
    fixed, _ = _spdx_expression(expression)
    return [{"expression": fixed}]


def _cdx_properties(component: Component, repos: list[str] | None = None) -> list[dict]:
    props = [
        {"name": "mira:ecosystem", "value": component.kind},
        {"name": "mira:dependency", "value": component.scope},
    ]
    if component.dev:
        props.append({"name": "mira:dev", "value": "true"})
    if component.constraint:
        props.append({"name": "mira:version_constraint", "value": component.constraint})
    props += [{"name": "mira:manifest", "value": f} for f in component.files]
    if component.license and component.license.source:
        props.append({"name": "mira:license_source", "value": component.license.source})
    props += [{"name": "mira:repository", "value": r} for r in repos or []]
    return props


def _cdx_component(component: Component, repos: list[str] | None = None) -> dict:
    group, _, short = component.name.rpartition("/") if component.kind == "npm" else ("", "", "")
    out: dict = {
        "type": "container" if component.kind == "docker" else "library",
        "bom-ref": component.purl,
        "name": short if group.startswith("@") else component.name,
    }
    if group.startswith("@"):
        out["group"] = group
    if component.version:
        out["version"] = component.version
    # "excluded": present for development and test, not at runtime.
    out["scope"] = "excluded" if component.dev else "required"
    licenses = _cdx_licenses(component)
    if licenses:
        out["licenses"] = licenses
    out["purl"] = component.purl
    out["properties"] = _cdx_properties(component, repos)
    return out


def to_cyclonedx(
    inventories: list[RepoInventory],
    *,
    name: str = "",
    spec_version: str = "1.6",
    timestamp: str | None = None,
    serial: str | None = None,
) -> dict:
    """A CycloneDX JSON BOM (as a dict) for one repository or several."""
    if spec_version not in CYCLONEDX_VERSIONS:
        raise ValueError(f"CycloneDX spec version must be one of {CYCLONEDX_VERSIONS}")
    # A name means "a collection": an org-wide BOM of one repository still
    # lists that repository as its own component.
    single = len(inventories) == 1 and not name
    subject = name or (inventories[0].name if single else "organization")
    root_ref = f"mira:subject:{subject}"
    root = {"type": "application", "bom-ref": root_ref, "name": subject}

    components: dict[str, dict] = {}
    used_by: dict[str, list[str]] = {}
    dependencies: list[dict] = []
    repo_refs: list[str] = []
    for inv in inventories:
        owner_ref = root_ref if single else f"mira:repository:{inv.name}"
        if not single:
            repo_refs.append(owner_ref)
            components[owner_ref] = {"type": "application", "bom-ref": owner_ref, "name": inv.name}
        direct: list[str] = []
        for comp in inv.components:
            ref = comp.purl
            used_by.setdefault(ref, [])
            if inv.name not in used_by[ref]:
                used_by[ref].append(inv.name)
            if ref not in components:
                components[ref] = _cdx_component(comp)
            if comp.scope == DIRECT and ref not in direct:
                direct.append(ref)
        dependencies.append({"ref": owner_ref, "dependsOn": direct})
    if not single:
        dependencies.insert(0, {"ref": root_ref, "dependsOn": repo_refs})
        for ref, repos in used_by.items():
            components[ref]["properties"] += [
                {"name": "mira:repository", "value": r} for r in repos
            ]

    return {
        "$schema": f"http://cyclonedx.org/schema/bom-{spec_version}.schema.json",
        "bomFormat": "CycloneDX",
        "specVersion": spec_version,
        "serialNumber": serial or f"urn:uuid:{uuid.uuid4()}",
        "version": 1,
        "metadata": {
            "timestamp": timestamp or _now(),
            "tools": {
                "components": [
                    {
                        "type": "application",
                        "author": "Mira",
                        "name": "mira",
                        "version": __version__,
                    }
                ]
            },
            "component": root,
        },
        "components": list(components.values()),
        "dependencies": dependencies,
    }


_SPDX_ID_UNSAFE = re.compile(r"[^A-Za-z0-9.\-]+")


def _spdx_id(prefix: str, text: str, taken: set[str]) -> str:
    base = f"SPDXRef-{prefix}-" + (_SPDX_ID_UNSAFE.sub("-", text).strip("-") or "x")
    candidate, n = base, 1
    while candidate in taken:
        n += 1
        candidate = f"{base}-{n}"
    taken.add(candidate)
    return candidate


def to_spdx(
    inventories: list[RepoInventory],
    *,
    name: str = "",
    timestamp: str | None = None,
    namespace: str | None = None,
) -> dict:
    """An SPDX 2.3 JSON document (as a dict) for one repository or several."""
    subject = name or (inventories[0].name if len(inventories) == 1 else "organization")
    taken: set[str] = set()
    packages: list[dict] = []
    relationships: list[dict] = []
    extracted: dict[str, str] = {}
    by_purl: dict[str, str] = {}

    for inv in inventories:
        repo_id = _spdx_id("Repository", inv.name, taken)
        packages.append(
            {
                "SPDXID": repo_id,
                "name": inv.name,
                "downloadLocation": "NOASSERTION",
                "filesAnalyzed": False,
                "licenseConcluded": "NOASSERTION",
                "licenseDeclared": "NOASSERTION",
                "copyrightText": "NOASSERTION",
                "primaryPackagePurpose": "APPLICATION",
            }
        )
        relationships.append(
            {
                "spdxElementId": "SPDXRef-DOCUMENT",
                "relationshipType": "DESCRIBES",
                "relatedSpdxElement": repo_id,
            }
        )
        for comp in inv.components:
            pkg_id = by_purl.get(comp.purl)
            if pkg_id is None:
                pkg_id = _spdx_id(
                    "Package", f"{comp.kind}-{comp.name}-{comp.version or 'unversioned'}", taken
                )
                by_purl[comp.purl] = pkg_id
                declared, refs = _spdx_expression(comp.license_expression)
                extracted.update(refs)
                pkg: dict = {
                    "SPDXID": pkg_id,
                    "name": comp.name,
                    "downloadLocation": "NOASSERTION",
                    "filesAnalyzed": False,
                    "licenseConcluded": "NOASSERTION",
                    "licenseDeclared": declared or "NOASSERTION",
                    "copyrightText": "NOASSERTION",
                    "primaryPackagePurpose": "CONTAINER" if comp.kind == "docker" else "LIBRARY",
                    "externalRefs": [
                        {
                            "referenceCategory": "PACKAGE-MANAGER",
                            "referenceType": "purl",
                            "referenceLocator": comp.purl,
                        }
                    ],
                }
                if comp.version:
                    pkg["versionInfo"] = comp.version
                notes = [f"{comp.scope} {comp.kind} dependency"]
                if comp.constraint:
                    notes.append(f"declared as {comp.constraint!r}")
                if comp.files:
                    notes.append("from " + ", ".join(comp.files))
                pkg["comment"] = "; ".join(notes)
                packages.append(pkg)
            if comp.dev:
                relationships.append(
                    {
                        "spdxElementId": pkg_id,
                        "relationshipType": "DEV_DEPENDENCY_OF",
                        "relatedSpdxElement": repo_id,
                    }
                )
            else:
                relationships.append(
                    {
                        "spdxElementId": repo_id,
                        "relationshipType": "DEPENDS_ON",
                        "relatedSpdxElement": pkg_id,
                    }
                )

    doc: dict = {
        "spdxVersion": "SPDX-2.3",
        "dataLicense": "CC0-1.0",
        "SPDXID": "SPDXRef-DOCUMENT",
        "name": f"{subject} SBOM",
        "documentNamespace": namespace
        or f"https://spdx.org/spdxdocs/mira-{_SPDX_ID_UNSAFE.sub('-', subject)}-{uuid.uuid4()}",
        "creationInfo": {
            "created": timestamp or _now(),
            "creators": [f"Tool: mira-{__version__}"],
        },
        "packages": packages,
        "relationships": relationships,
    }
    if extracted:
        doc["hasExtractedLicensingInfos"] = [
            {"licenseId": ref, "name": original, "extractedText": original}
            for ref, original in sorted(extracted.items())
        ]
    return doc


def render(
    inventories: list[RepoInventory],
    fmt: str,
    *,
    name: str = "",
    spec_version: str = "1.6",
) -> dict:
    if fmt == "cyclonedx":
        return to_cyclonedx(inventories, name=name, spec_version=spec_version)
    if fmt == "spdx":
        return to_spdx(inventories, name=name)
    raise ValueError(f"format must be one of {FORMATS}")
