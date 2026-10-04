"""License data and license policy: normalization, policy evaluation, registry
lookups (all HTTP through ``httpx.MockTransport``), the cache, and the
review-time check on dependencies a pull request adds.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from pydantic import ValidationError

from mira.config import LicensesConfig, MiraConfig
from mira.dependency_updates import fetch as fetch_mod
from mira.index.manifests import parse_manifest
from mira.index.store import IndexStore
from mira.licenses.expressions import LicenseParseError, normalize, parse_expression
from mira.licenses.lookup import PackageRef, concrete_version, lookup_registry, resolve_licenses
from mira.licenses.policy import ALLOWED, DENIED, UNKNOWN, Policy, evaluate
from mira.licenses.review import check_manifest_licenses
from mira.models import FileChangeType, FileDiff, HunkInfo, Severity

HOSTS = ",".join(LicensesConfig().allowed_hosts)


@pytest.fixture(autouse=True)
def _network(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Hosts allowed, DNS answered as public, caches empty, stores isolated."""
    monkeypatch.setenv("MIRA_LICENSES_HOSTS", HOSTS)
    monkeypatch.setenv("MIRA_INDEX_DIR", str(tmp_path / "index"))
    monkeypatch.delenv("DATABASE_URL", raising=False)

    async def public(host: str) -> bool:
        return True

    monkeypatch.setattr(fetch_mod, "resolves_public", public)
    fetch_mod.reset_cache()


def _client(handler: Any, seen: list[str] | None = None) -> httpx.AsyncClient:
    def recording(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(str(request.url))
        return handler(request)

    return httpx.AsyncClient(transport=httpx.MockTransport(recording))


def _json(data: Any, status: int = 200) -> httpx.Response:
    return httpx.Response(status, content=json.dumps(data).encode())


def _config(**kw: Any) -> LicensesConfig:
    return LicensesConfig(enabled=True, **kw)


# ── Normalization ────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("MIT", "MIT"),
        ("mit", "MIT"),
        ("MIT License", "MIT"),
        ("Apache 2.0", "Apache-2.0"),
        ("Apache License, Version 2.0", "Apache-2.0"),
        ("(MIT OR Apache-2.0)", "MIT OR Apache-2.0"),
        ("MIT/Apache-2.0", "MIT OR Apache-2.0"),
        ("mit and isc", "MIT AND ISC"),
        ("(MIT AND (BSD-3-Clause OR ISC))", "MIT AND (BSD-3-Clause OR ISC)"),
        ("GPL-3.0", "GPL-3.0-only"),
        ("GPL-2.0+", "GPL-2.0-or-later"),
        ("LGPL-2.1-or-later", "LGPL-2.1-or-later"),
        ("License :: OSI Approved :: MIT License", "MIT"),
        ("License :: OSI Approved :: BSD License", "BSD-3-Clause"),
        (
            "License :: OSI Approved :: GNU General Public License v3 (GPLv3)",
            "GPL-3.0-only",
        ),
        ("GPL-2.0-only WITH Classpath-exception-2.0", "GPL-2.0-only WITH Classpath-exception-2.0"),
        ("Proprietary", "Proprietary"),
        # No answer is unknown, never a guess.
        ("", ""),
        (None, ""),
        ("UNKNOWN", ""),
        ("UNLICENSED", ""),
        ("SEE LICENSE IN LICENSE.md", ""),
        ("Dual licensed under the terms of the foo", ""),
        ("Permission is hereby granted, free of charge...\n" * 3, ""),
        ("MIT OR", ""),
    ],
)
def test_normalize(raw: str | None, expected: str) -> None:
    assert normalize(raw) == expected


@pytest.mark.parametrize("bad", ["", "MIT OR", "(MIT", "MIT)", "AND MIT", "MIT WITH", "a b"])
def test_parse_errors(bad: str) -> None:
    with pytest.raises(LicenseParseError):
        parse_expression(bad)


def test_precedence_and_over_or() -> None:
    tree = parse_expression("MIT OR ISC AND Apache-2.0")
    assert tree.op == "or"
    assert tree.children[1].op == "and"
    assert tree.render() == "MIT OR ISC AND Apache-2.0"
    assert parse_expression("(MIT OR ISC) AND Apache-2.0").render() == "(MIT OR ISC) AND Apache-2.0"


# ── Policy ───────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("expression", "allow", "deny", "status"),
    [
        ("MIT", (), (), ALLOWED),
        ("MIT", ("MIT",), (), ALLOWED),
        ("ISC", ("MIT",), (), DENIED),
        ("GPL-3.0-only", (), ("GPL-3.0",), DENIED),
        ("GPL-3.0-or-later", (), ("GPL-3.0",), DENIED),
        ("GPL-2.0-only", (), ("GPL-3.0",), ALLOWED),
        ("GPL-2.0-or-later", (), ("GPL-2.0+",), DENIED),
        ("GPL-2.0-only", (), ("GPL-2.0+",), ALLOWED),
        ("gpl-3.0-only", (), ("GPL-3.0-only",), DENIED),
        # OR: the consumer may choose either.
        ("MIT OR GPL-3.0-only", (), ("GPL-3.0",), ALLOWED),
        ("ISC OR GPL-3.0-only", ("MIT",), ("GPL-3.0",), DENIED),
        # AND: every part must pass.
        ("MIT AND GPL-3.0-only", (), ("GPL-3.0",), DENIED),
        ("MIT AND Apache-2.0", ("MIT", "Apache-2.0"), (), ALLOWED),
        # Deny beats allow.
        ("MIT", ("MIT",), ("MIT",), DENIED),
        # A verbatim compound entry decides the expression.
        ("GPL-3.0-only OR ISC", ("GPL-3.0-only OR ISC",), (), ALLOWED),
        ("MIT OR Apache-2.0", ("MIT",), ("MIT OR Apache-2.0",), DENIED),
        # WITH is judged by its license.
        ("GPL-2.0-only WITH Classpath-exception-2.0", (), ("GPL-2.0",), DENIED),
        ("GPL-2.0-only WITH Classpath-exception-2.0", ("GPL-2.0-only",), (), ALLOWED),
        ("Proprietary", ("MIT",), (), DENIED),
        ("", ("MIT",), (), UNKNOWN),
    ],
)
def test_policy(expression: str, allow: tuple, deny: tuple, status: str) -> None:
    verdict = evaluate(expression, Policy(allow=allow, deny=deny))
    assert verdict.status == status, verdict


def test_policy_reasons_and_unknown() -> None:
    verdict = evaluate("MIT AND GPL-3.0-only", Policy(allow=("MIT",), deny=("GPL-3.0",)))
    assert verdict.reasons == ["GPL-3.0-only is denied"]
    verdict = evaluate("ISC", Policy(allow=("MIT",)))
    assert verdict.reasons == ["ISC is not in the allow list"]
    unknown = evaluate("", Policy(deny=("GPL-3.0",)))
    assert not unknown.violates(fail_on_unknown=False)
    assert unknown.violates(fail_on_unknown=True)


def test_config_is_off_by_default_and_validated() -> None:
    cfg = MiraConfig().licenses
    assert cfg.enabled is False
    assert cfg.allow == [] and cfg.deny == [] and cfg.fail_on_unknown is False
    with pytest.raises(ValidationError, match="not a license expression"):
        LicensesConfig(deny=["MIT OR"])
    with pytest.raises(ValidationError):
        LicensesConfig(severity="error")
    assert LicensesConfig(allow=["MIT OR Apache-2.0"], severity="blocker").severity == "blocker"


# ── Lockfiles record licenses ────────────────────────────────────────


def test_package_lock_records_licenses() -> None:
    content = json.dumps(
        {
            "lockfileVersion": 3,
            "packages": {
                "": {"name": "app"},
                "node_modules/lodash": {"version": "4.17.21", "license": "MIT"},
                "node_modules/@scope/pkg": {"version": "1.0.0", "license": {"type": "ISC"}},
                "node_modules/nolicense": {"version": "1.0.0"},
            },
        }
    )
    pkgs = {p.name: p for p in parse_manifest("package-lock.json", content)}
    assert pkgs["lodash"].license == "MIT"
    assert pkgs["@scope/pkg"].license == "ISC"
    assert pkgs["nolicense"].license == ""


def test_composer_lock_license_list_is_alternatives() -> None:
    content = json.dumps(
        {
            "packages": [
                {"name": "a/one", "version": "1.0.0", "license": ["MIT"]},
                {"name": "a/two", "version": "1.0.0", "license": ["GPL-2.0-only", "MIT"]},
            ]
        }
    )
    pkgs = {p.name: p for p in parse_manifest("composer.lock", content)}
    assert pkgs["a/one"].license == "MIT"
    assert normalize(pkgs["a/two"].license) == "GPL-2.0-only OR MIT"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("1.2.3", "1.2.3"),
        ("==1.2.3", "1.2.3"),
        ("v0.9.1", "v0.9.1"),
        ("^1.2.3", ""),
        (">=2.0", ""),
        ("*", ""),
        ("", ""),
    ],
)
def test_concrete_version(raw: str, expected: str) -> None:
    assert concrete_version(raw) == expected


# ── Registry lookups ─────────────────────────────────────────────────


def _registry(request: httpx.Request) -> httpx.Response:
    url = str(request.url)
    if url == "https://registry.npmjs.org/left-pad/1.3.0":
        return _json({"name": "left-pad", "license": "WTFPL"})
    if url == "https://registry.npmjs.org/old-style/1.0.0":
        return _json({"licenses": [{"type": "MIT"}, {"type": "Apache-2.0"}]})
    if url == "https://registry.npmjs.org/ranged/latest":
        return _json({"license": "ISC"})
    if url == "https://pypi.org/pypi/requests/2.32.3/json":
        return _json({"info": {"license": "Apache 2.0", "classifiers": []}})
    if url == "https://pypi.org/pypi/newstyle/1.0/json":
        return _json({"info": {"license_expression": "MIT OR Apache-2.0", "license": None}})
    if url == "https://pypi.org/pypi/classified/2.0/json":
        return _json(
            {
                "info": {
                    "license": "A long license text that is not an identifier at all",
                    "classifiers": ["License :: OSI Approved :: BSD License"],
                }
            }
        )
    if url == "https://pypi.org/pypi/missing/9.9/json":
        return _json({"message": "Not Found"}, 404)
    if url == "https://pypi.org/pypi/missing/json":
        return _json({"info": {"license_expression": "MIT"}})
    if url == "https://crates.io/api/v1/crates/serde/1.0.0":
        return _json({"version": {"license": "MIT OR Apache-2.0"}})
    if url.startswith("https://api.deps.dev/v3/systems/go/packages/github.com%2Fpkg%2Ferrors"):
        assert url.endswith("/versions/v0.9.1")
        return _json({"licenses": ["BSD-2-Clause"]})
    if url == "https://repo.packagist.org/p2/monolog/monolog.json":
        return _json(
            {
                "packages": {
                    "monolog/monolog": [
                        {"version": "3.5.0", "license": ["MIT"]},
                        {"version": "3.4.0"},  # minified: inherits MIT
                        {"version": "1.0.0", "license": ["GPL-2.0-only"]},
                    ]
                }
            }
        )
    return httpx.Response(404)


async def test_lookup_each_registry() -> None:
    wanted = [
        ("npm", "left-pad", "1.3.0"),
        ("npm", "old-style", "1.0.0"),
        ("npm", "ranged", ""),
        ("pip", "requests", "2.32.3"),
        ("pip", "newstyle", "1.0"),
        ("pip", "classified", "2.0"),
        ("pip", "missing", "9.9"),
        ("rust", "serde", "1.0.0"),
        ("go", "github.com/pkg/errors", "v0.9.1"),
        ("composer", "monolog/monolog", "3.4.0"),
        ("composer", "monolog/monolog", "1.0.0"),
    ]
    async with _client(_registry) as client:
        got = await lookup_registry(wanted, LicensesConfig(), client=client)
    norm = {k: normalize(v[0]) for k, v in got.items()}
    assert norm[("npm", "left-pad", "1.3.0")] == "WTFPL"
    assert norm[("npm", "old-style", "1.0.0")] == "MIT OR Apache-2.0"
    assert norm[("npm", "ranged", "")] == "ISC"
    assert norm[("pip", "requests", "2.32.3")] == "Apache-2.0"
    assert norm[("pip", "newstyle", "1.0")] == "MIT OR Apache-2.0"
    assert norm[("pip", "classified", "2.0")] == "BSD-3-Clause"
    assert norm[("pip", "missing", "9.9")] == "MIT"  # unknown version → latest
    assert norm[("rust", "serde", "1.0.0")] == "MIT OR Apache-2.0"
    assert norm[("go", "github.com/pkg/errors", "v0.9.1")] == "BSD-2-Clause"
    assert norm[("composer", "monolog/monolog", "3.4.0")] == "MIT"
    assert norm[("composer", "monolog/monolog", "1.0.0")] == "GPL-2.0-only"
    assert got[("rust", "serde", "1.0.0")][1] == "crates.io"


async def test_lookup_contacts_only_allowed_hosts(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[str] = []
    monkeypatch.setenv("MIRA_LICENSES_HOSTS", "pypi.org")
    async with _client(_registry, seen) as client:
        got = await lookup_registry(
            [("npm", "left-pad", "1.3.0"), ("pip", "requests", "2.32.3")],
            LicensesConfig(),
            client=client,
        )
    assert all(u.startswith("https://pypi.org/") for u in seen)
    assert ("pip", "requests", "2.32.3") in got

    seen.clear()
    monkeypatch.setenv("MIRA_LICENSES_HOSTS", "")
    async with _client(_registry, seen) as client:
        assert (
            await lookup_registry([("pip", "requests", "2.32.3")], LicensesConfig(), client=client)
            == {}
        )
    assert seen == []


async def test_lookup_refuses_private_addresses(monkeypatch: pytest.MonkeyPatch) -> None:
    async def private(host: str) -> bool:
        return False

    monkeypatch.setattr(fetch_mod, "resolves_public", private)
    seen: list[str] = []
    async with _client(_registry, seen) as client:
        got = await lookup_registry([("npm", "left-pad", "1.3.0")], LicensesConfig(), client=client)
    assert seen == []
    assert got.get(("npm", "left-pad", "1.3.0"), ("", ""))[0] == ""


async def test_lookup_does_not_follow_redirects() -> None:
    def redirect(request: httpx.Request) -> httpx.Response:
        if request.url.host == "registry.npmjs.org":
            return httpx.Response(302, headers={"Location": "http://169.254.169.254/latest"})
        return httpx.Response(500)

    seen: list[str] = []
    async with _client(redirect, seen) as client:
        got = await lookup_registry([("npm", "x", "1.0.0")], LicensesConfig(), client=client)
    assert all("169.254" not in u for u in seen)
    assert got.get(("npm", "x", "1.0.0"), ("", ""))[0] == ""


async def test_lookup_respects_the_deadline() -> None:
    import asyncio

    async def slow(request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(5)
        return _json({"license": "MIT"})

    cfg = LicensesConfig(timeout_seconds=0.2)
    started = time.monotonic()
    async with httpx.AsyncClient(transport=httpx.MockTransport(slow)) as client:
        got = await lookup_registry(
            [("npm", "a", "1.0.0"), ("npm", "b", "1.0.0")], cfg, client=client
        )
    assert time.monotonic() - started < 2
    assert got == {}


# ── Lockfile → cache → registry ──────────────────────────────────────


@pytest.fixture
def store():
    s = IndexStore.open("acme", "api")
    yield s
    s.close()


async def test_resolve_prefers_lockfile_then_cache_then_registry(store) -> None:
    store.upsert_package_licenses([("pip", "requests", "2.32.3", "Apache-2.0", "pypi")])
    seen: list[str] = []
    refs = [
        PackageRef("npm", "lodash", "4.17.21", recorded="MIT"),
        PackageRef("pip", "Requests", "==2.32.3"),
        PackageRef("npm", "left-pad", "1.3.0"),
        PackageRef("docker", "python", "3.12"),
    ]
    async with _client(_registry, seen) as client:
        got = await resolve_licenses(refs, _config(), store, client=client)
    assert got[refs[0].key].expression == "MIT" and got[refs[0].key].source == "lockfile"
    assert got[refs[1].key].expression == "Apache-2.0" and got[refs[1].key].source == "cache"
    assert got[refs[2].key].expression == "WTFPL" and got[refs[2].key].source == "npm"
    assert refs[3].key not in got
    assert seen == ["https://registry.npmjs.org/left-pad/1.3.0"]
    # The registry's answer is cached for the next review.
    assert store.get_package_licenses([refs[2].key])[refs[2].key][0] == "WTFPL"

    seen.clear()
    async with _client(_registry, seen) as client:
        again = await resolve_licenses(refs, _config(), store, client=client)
    assert seen == []
    assert again[refs[2].key].source == "cache"


async def test_resolve_refreshes_stale_cache_and_respects_limits(store) -> None:
    store.upsert_package_licenses([("npm", "left-pad", "1.3.0", "MIT", "npm")])
    store._conn.execute("UPDATE package_licenses SET fetched_at = ?", (time.time() - 40 * 86400,))
    store._conn.commit()
    seen: list[str] = []
    refs = [PackageRef("npm", "left-pad", "1.3.0"), PackageRef("pip", "requests", "2.32.3")]
    async with _client(_registry, seen) as client:
        got = await resolve_licenses(refs, _config(max_lookups=1), store, client=client)
    assert len(seen) == 1
    assert got[refs[0].key].expression == "WTFPL"
    assert refs[1].key not in got

    seen.clear()
    async with _client(_registry, seen) as client:
        got = await resolve_licenses(refs, _config(), store, lookup=False, client=client)
    assert seen == []


async def test_resolve_never_raises() -> None:
    class Broken:
        def get_package_licenses(self, keys):
            raise RuntimeError("boom")

    got = await resolve_licenses([PackageRef("npm", "x", "1.0.0")], _config(lookup=False), Broken())
    assert got == {}


# ── Review-time check ────────────────────────────────────────────────


def _diff(path: str, added: list[str], target_start: int = 1) -> FileDiff:
    body = "\n".join(f"+{line}" for line in added)
    return FileDiff(
        path=path,
        change_type=FileChangeType.MODIFIED,
        hunks=[
            HunkInfo(
                source_start=1,
                source_length=0,
                target_start=target_start,
                target_length=len(added),
                content=f"@@ -1,0 +{target_start},{len(added)} @@\n{body}\n",
            )
        ],
    )


class _Fetcher:
    def __init__(self, files: dict[str, str]) -> None:
        self.files = files

    async def fetch(self, path: str) -> str | None:
        return self.files.get(path)


_PACKAGE_JSON = json.dumps(
    {
        "dependencies": {"lodash": "^4.17.21", "gpl-thing": "^1.0.0"},
        "devDependencies": {"devtool": "1.0.0"},
    },
    indent=2,
)
_LOCK = json.dumps(
    {
        "lockfileVersion": 3,
        "packages": {
            "": {"name": "app"},
            "node_modules/lodash": {"version": "4.17.21", "license": "MIT"},
            "node_modules/gpl-thing": {"version": "1.2.0", "license": "GPL-3.0-or-later"},
            "node_modules/devtool": {"version": "1.0.0", "license": "AGPL-3.0-only", "dev": True},
        },
    },
    indent=2,
)


def _pr() -> tuple[list[FileDiff], _Fetcher]:
    files = [
        _diff("package.json", ['    "gpl-thing": "^1.0.0",', '    "devtool": "1.0.0"'], 5),
        _diff(
            "package-lock.json",
            [
                '    "node_modules/gpl-thing": {',
                '      "version": "1.2.0",',
                '      "license": "GPL-3.0-or-later"',
                '    "node_modules/devtool": {',
            ],
            20,
        ),
    ]
    return files, _Fetcher({"package.json": _PACKAGE_JSON, "package-lock.json": _LOCK})


async def test_review_flags_a_denied_license_on_the_manifest_line() -> None:
    files, fetcher = _pr()
    cfg = _config(deny=["GPL-3.0", "AGPL-3.0"], lookup=False)
    comments = await check_manifest_licenses(files, fetcher, cfg)
    by_title = {c.title: c for c in comments}
    assert set(by_title) == {
        "License not allowed: gpl-thing (GPL-3.0-or-later)",
        "License not allowed: devtool (AGPL-3.0-only)",
    }
    c = by_title["License not allowed: gpl-thing (GPL-3.0-or-later)"]
    # One finding per package, where a person added it, with the lockfile's facts.
    assert c.path == "package.json" and c.line == 5
    assert "gpl-thing@1.2.0" in c.body and "GPL-3.0-or-later is denied" in c.body
    assert c.severity == Severity.WARNING
    assert c.category == "license" and c.source_pass == "licenses"
    assert c.confidence == 1.0
    # lodash was not added by this pull request.
    assert all("lodash" not in t for t in by_title)


async def test_review_options() -> None:
    files, fetcher = _pr()
    # ignore_dev and ignore_packages
    cfg = _config(deny=["GPL-3.0", "AGPL-3.0"], lookup=False, ignore_dev=True)
    assert [c.title for c in await check_manifest_licenses(files, fetcher, cfg)] == [
        "License not allowed: gpl-thing (GPL-3.0-or-later)"
    ]
    cfg = _config(
        deny=["GPL-3.0", "AGPL-3.0"], lookup=False, ignore_packages=["GPL-Thing", "devtool"]
    )
    assert await check_manifest_licenses(files, fetcher, cfg) == []
    # blocker severity, which is what fails the review status
    cfg = _config(deny=["GPL-3.0"], lookup=False, severity="blocker")
    (c,) = await check_manifest_licenses(files, fetcher, cfg)
    assert c.severity == Severity.BLOCKER
    # cap, with the rest counted on the last one
    cfg = _config(deny=["GPL-3.0", "AGPL-3.0"], lookup=False, max_comments=1)
    (c,) = await check_manifest_licenses(files, fetcher, cfg)
    assert "1 more package" in c.body


async def test_review_does_nothing_unless_enabled_with_a_policy() -> None:
    files, fetcher = _pr()
    assert await check_manifest_licenses(files, fetcher, LicensesConfig(deny=["GPL-3.0"])) == []
    assert await check_manifest_licenses(files, fetcher, _config()) == []
    assert await check_manifest_licenses(files, None, _config(deny=["GPL-3.0"])) == []


async def test_review_unknown_licenses_and_registry_lookup() -> None:
    files = [_diff("requirements.txt", ["mystery==1.0", "requests==2.32.3"])]
    fetcher = _Fetcher({"requirements.txt": "mystery==1.0\nrequests==2.32.3\n"})
    seen: list[str] = []
    cfg = _config(allow=["MIT", "Apache-2.0"], fail_on_unknown=True)
    async with _client(_registry, seen) as client:
        comments = await check_manifest_licenses(files, fetcher, cfg, client=client)
    assert [c.title for c in comments] == ["Unknown license for mystery"]
    assert "fail_on_unknown" in comments[0].body
    assert "https://pypi.org/pypi/requests/2.32.3/json" in seen

    # Without fail_on_unknown an unknown license is not a violation.
    async with _client(_registry) as client:
        assert (
            await check_manifest_licenses(
                files, fetcher, _config(allow=["MIT", "Apache-2.0"]), client=client
            )
            == []
        )


async def test_review_never_raises() -> None:
    class Exploding:
        async def fetch(self, path: str) -> str:
            raise RuntimeError("provider down")

    files, _ = _pr()
    assert await check_manifest_licenses(files, Exploding(), _config(deny=["GPL-3.0"])) == []


async def test_review_skips_deleted_manifests() -> None:
    files, fetcher = _pr()
    for f in files:
        f.change_type = FileChangeType.DELETED
    assert (
        await check_manifest_licenses(files, fetcher, _config(deny=["GPL-3.0"], lookup=False)) == []
    )


async def test_engine_runs_the_check_only_when_enabled() -> None:
    from mira.core.engine import ReviewEngine

    files, fetcher = _pr()
    pr_info = SimpleNamespace(owner="acme", repo="api", platform="github")
    off = SimpleNamespace(config=MiraConfig(), _pr_info=pr_info)
    assert await ReviewEngine._license_findings(off, files, fetcher) == []

    cfg = MiraConfig(licenses={"enabled": True, "deny": ["GPL-3.0"], "lookup": False})
    on = SimpleNamespace(config=cfg, _pr_info=pr_info)
    comments = await ReviewEngine._license_findings(on, files, fetcher)
    assert [c.path for c in comments] == ["package.json"]
    # Local review has no PR, so it never runs.
    local = SimpleNamespace(config=cfg, _pr_info=None)
    assert await ReviewEngine._license_findings(local, files, fetcher) == []


def test_license_findings_render_with_their_category() -> None:
    from mira.providers.formatting import _CATEGORY_DISPLAY

    assert _CATEGORY_DISPLAY["license"][1] == "License compliance"


# ── CLI ──────────────────────────────────────────────────────────────


def test_cli_licenses_exits_nonzero_on_violation(tmp_path: Path) -> None:
    from click.testing import CliRunner

    from mira.cli import main

    s = IndexStore.open("acme", "api")
    try:
        s.replace_manifest_packages(
            "package-lock.json",
            [
                {
                    "name": "ok",
                    "kind": "npm",
                    "version": "1.0.0",
                    "file_path": "package-lock.json",
                    "license": "MIT",
                },
                {
                    "name": "bad",
                    "kind": "npm",
                    "version": "1.0.0",
                    "file_path": "package-lock.json",
                    "license": "GPL-3.0",
                },
            ],
        )
    finally:
        s.close()
    cfg = tmp_path / "mira.yaml"
    cfg.write_text("licenses:\n  deny: [GPL-3.0]\n")
    result = CliRunner().invoke(
        main, ["licenses", "--repo", "acme/api", "--config", str(cfg), "--output", "json"]
    )
    assert result.exit_code == 1, result.output
    data = json.loads(result.stdout)
    rows = {r["name"]: r for r in data["packages"]}
    assert rows["bad"]["violation"] and rows["bad"]["license"] == "GPL-3.0-only"
    assert not rows["ok"]["violation"]

    cfg.write_text("licenses:\n  deny: [AGPL-3.0]\n")
    result = CliRunner().invoke(main, ["licenses", "--repo", "acme/api", "--config", str(cfg)])
    assert result.exit_code == 0, result.output
    assert "0 violating the policy" in result.output
