"""Where a package's license comes from: the lockfile, the cache, the registry.

:func:`resolve_licenses` answers in that order and never raises. The registry
is asked only for what neither of the others knows, only when ``lookup`` is
on, and only through :class:`mira.dependency_updates.fetch.Fetcher` — the same
client the release notes use, so the same network posture holds:

* only the hosts in ``licenses.allowed_hosts`` (``MIRA_LICENSES_HOSTS`` wins,
  and an empty value means none), only HTTPS, no redirects;
* each connection pinned to an address checked to be public
  (:class:`~mira.dependency_updates.fetch.PinnedTransport`);
* one deadline and one byte allowance for every request of the call.

Answers are written to the index store's ``package_licenses`` table: a license
for 30 days, "the registry had none" for one, so a package with no license
metadata is not asked about on every review.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

import httpx

from mira.dependency_updates.fetch import Budget, Fetcher, PinnedTransport
from mira.licenses.expressions import normalize

logger = logging.getLogger(__name__)

HOSTS_ENV = "MIRA_LICENSES_HOSTS"
FOUND_TTL_SECONDS = 30 * 86400
MISSING_TTL_SECONDS = 86400
_CONCURRENCY = 5

#: Ecosystems a license can be looked up for; others are always "unknown".
LOOKUP_KINDS = frozenset({"npm", "pip", "rust", "go", "composer"})

Key = tuple[str, str, str]  # (kind, canonical name, version)


@dataclass
class LicenseInfo:
    expression: str  # normalized SPDX expression, "" when unknown
    raw: str = ""  # as the source wrote it
    source: str = ""  # "lockfile" | "cache" | "npm" | "pypi" | ... | ""


def canonical_name(kind: str, name: str) -> str:
    """The cache key's spelling: PEP 503 for PyPI, as written elsewhere."""
    if kind == "pip":
        return re.sub(r"[-_.]+", "-", name).lower()
    if kind in ("npm", "composer", "rust"):
        return name.lower()
    return name


_CONCRETE = re.compile(r"^v?\d+(?:\.[0-9A-Za-z\-]+)*(?:[+\-][0-9A-Za-z.\-]+)?$")


def concrete_version(raw: str) -> str:
    """The version itself for a pin (``1.2.3``, ``==1.2.3``), else ``""``.

    A range (``^1.2``, ``>=2``) names no one release, so it is looked up — if
    at all — as the package's latest.
    """
    text = (raw or "").strip()
    if text.startswith("==="):
        text = text[3:]
    elif text.startswith("=="):
        text = text[2:]
    elif text.startswith("="):
        text = text[1:]
    text = text.strip()
    return text if _CONCRETE.match(text) else ""


def allowed_hosts(configured: list[str]) -> set[str]:
    raw = os.environ.get(HOSTS_ENV)
    hosts = raw.split(",") if raw is not None else configured
    return {h.strip().lower() for h in hosts if h and h.strip()}


# ── Registries ──────────────────────────────────────────────────────


def _npm_license(data: Any) -> str:
    if not isinstance(data, dict):
        return ""
    lic = data.get("license")
    if isinstance(lic, dict):
        lic = lic.get("type")
    if isinstance(lic, str) and lic.strip():
        return lic
    many = data.get("licenses")
    if isinstance(many, list):
        raw = [m.get("type") if isinstance(m, dict) else m for m in many]
        names = [n for n in raw if isinstance(n, str) and n.strip()]
        if names:
            return " OR ".join(names)
    return ""


def _pypi_license(data: Any) -> str:
    info = (data or {}).get("info") if isinstance(data, dict) else None
    if not isinstance(info, dict):
        return ""
    expr = info.get("license_expression")
    if isinstance(expr, str) and expr.strip():
        return expr
    lic = info.get("license")
    if isinstance(lic, str) and lic.strip() and normalize(lic):
        return lic
    classifiers = [
        c
        for c in info.get("classifiers") or []
        if isinstance(c, str) and c.startswith("License ::")
    ]
    found = [n for n in (normalize(c) for c in classifiers) if n]
    if found:
        unique = list(dict.fromkeys(found))
        return unique[0] if len(unique) == 1 else " OR ".join(f"({n})" for n in unique)
    return ""


def _crates_license(data: Any) -> str:
    if not isinstance(data, dict):
        return ""
    version = data.get("version")
    if isinstance(version, dict) and isinstance(version.get("license"), str):
        return version["license"]
    versions = data.get("versions")
    if isinstance(versions, list) and versions and isinstance(versions[0], dict):
        lic = versions[0].get("license")
        return lic if isinstance(lic, str) else ""
    return ""


def _composer_license(data: Any, name: str, version: str) -> str:
    entries = ((data or {}).get("packages") or {}).get(name) if isinstance(data, dict) else None
    if not isinstance(entries, list):
        return ""
    # Packagist's p2 metadata is minified: an entry repeats a field only when it
    # changed from the entry before, so carry the last license forward.
    current: Any = None
    first = ""
    want = version.lstrip("v")
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        if "license" in entry:
            current = entry["license"]
        lic = current if isinstance(current, list) else []
        text = " OR ".join(str(x) for x in lic if isinstance(x, str))
        if not first:
            first = text
        if want and str(entry.get("version", "")).lstrip("v") == want:
            return text
    return first


async def _lookup_one(fetcher: Fetcher, kind: str, name: str, version: str) -> tuple[str, str]:
    """``(raw license, source)`` from the package's registry, ``("", "")`` when none."""
    if kind == "npm":
        base = f"https://registry.npmjs.org/{quote(name, safe='@/')}"
        data = await fetcher.json(f"{base}/{quote(version, safe='')}") if version else None
        if data is None:
            data = await fetcher.json(f"{base}/latest")
        return _npm_license(data), "npm"
    if kind == "pip":
        base = f"https://pypi.org/pypi/{quote(name, safe='')}"
        data = await fetcher.json(f"{base}/{quote(version, safe='')}/json") if version else None
        if data is None:
            data = await fetcher.json(f"{base}/json")
        return _pypi_license(data), "pypi"
    if kind == "rust":
        base = f"https://crates.io/api/v1/crates/{quote(name, safe='')}"
        data = await fetcher.json(f"{base}/{quote(version, safe='')}") if version else None
        if data is None:
            data = await fetcher.json(base)
        return _crates_license(data), "crates.io"
    if kind == "go":
        if not version:
            return "", ""
        v = version if version.startswith("v") else f"v{version}"
        data = await fetcher.json(
            f"https://api.deps.dev/v3/systems/go/packages/{quote(name, safe='')}"
            f"/versions/{quote(v, safe='')}"
        )
        licenses = (data or {}).get("licenses") if isinstance(data, dict) else None
        if isinstance(licenses, list):
            names = [x for x in licenses if isinstance(x, str) and x and x != "non-standard"]
            return " AND ".join(dict.fromkeys(names)), "deps.dev"
        return "", "deps.dev"
    if kind == "composer":
        data = await fetcher.json(f"https://repo.packagist.org/p2/{quote(name, safe='/')}.json")
        return _composer_license(data, name, version), "packagist"
    return "", ""


async def lookup_registry(
    wanted: list[tuple[str, str, str]],
    config: Any,
    *,
    client: httpx.AsyncClient | None = None,
) -> dict[tuple[str, str, str], tuple[str, str]]:
    """``(kind, name, version)`` → ``(raw license, source)`` for those a registry answered.

    Bounded by ``config.timeout_seconds`` and ``config.max_bytes`` overall;
    whatever has not answered by the deadline is left out, never waited for.
    """
    hosts = allowed_hosts(list(getattr(config, "allowed_hosts", []) or []))
    if not wanted or not hosts:
        return {}
    budget = Budget(config.timeout_seconds, config.max_bytes)
    results: dict[tuple[str, str, str], tuple[str, str]] = {}
    sem = asyncio.Semaphore(_CONCURRENCY)

    async def one(fetcher: Fetcher, key: tuple[str, str, str]) -> None:
        async with sem:
            if fetcher.budget.exhausted:
                return
            kind, name, version = key
            try:
                raw, source = await _lookup_one(fetcher, kind, name, version)
            except Exception as exc:  # noqa: BLE001 - one package's metadata is not worth more
                logger.debug("License lookup for %s %s failed: %s", kind, name, exc)
                return
            if source:
                results[key] = (raw, source)

    async def run(c: httpx.AsyncClient) -> None:
        fetcher = Fetcher(
            c, hosts=hosts, budget=budget, request_timeout=config.request_timeout_seconds
        )
        tasks = [asyncio.create_task(one(fetcher, k)) for k in wanted]
        _done, pending = await asyncio.wait(tasks, timeout=max(0.1, budget.time_left()))
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)

    if client is not None:
        await run(client)
    else:
        async with httpx.AsyncClient(follow_redirects=False, transport=PinnedTransport()) as c:
            await run(c)
    return results


# ── Lockfile → cache → registry ─────────────────────────────────────


@dataclass(frozen=True)
class PackageRef:
    kind: str
    name: str
    version: str = ""  # raw, as the manifest wrote it
    recorded: str = ""  # the license the lockfile recorded, if any

    @property
    def key(self) -> Key:
        return (self.kind, canonical_name(self.kind, self.name), concrete_version(self.version))


async def resolve_licenses(
    packages: list[PackageRef],
    config: Any,
    store: Any = None,
    *,
    lookup: bool = True,
    client: httpx.AsyncClient | None = None,
) -> dict[Key, LicenseInfo]:
    """Every package's license, best source first. Never raises."""
    out: dict[Key, LicenseInfo] = {}
    try:
        for p in packages:
            if p.recorded and p.key not in out:
                expr = normalize(p.recorded)
                if expr:
                    out[p.key] = LicenseInfo(expr, p.recorded, "lockfile")
        missing = [p for p in packages if p.key not in out and p.kind in LOOKUP_KINDS]
        keys = list(dict.fromkeys(p.key for p in missing))
        now = time.time()
        cached: dict[Key, tuple[str, str, float]] = {}
        if store is not None and keys and hasattr(store, "get_package_licenses"):
            try:
                cached = store.get_package_licenses(keys)
            except Exception as exc:  # noqa: BLE001 - an unreadable cache is a cold cache
                logger.debug("License cache unavailable: %s", exc)
        stale: list[Key] = []
        for key in keys:
            hit = cached.get(key)
            ttl = FOUND_TTL_SECONDS if hit and hit[0] else MISSING_TTL_SECONDS
            if hit and now - hit[2] < ttl:
                out[key] = LicenseInfo(normalize(hit[0]), hit[0], "cache" if hit[0] else "")
            else:
                stale.append(key)
        if not (lookup and getattr(config, "lookup", True) and stale):
            return out
        limit = int(getattr(config, "max_lookups", 25))
        names = {p.key: p.name for p in missing}
        wanted = [(k[0], names.get(k, k[1]), k[2]) for k in stale[:limit]]
        answers = await lookup_registry(wanted, config, client=client)
        rows: list[tuple[str, str, str, str, str]] = []
        for (kind, name, version), (raw, source) in answers.items():
            key = (kind, canonical_name(kind, name), version)
            out[key] = LicenseInfo(normalize(raw), raw, source if raw else "")
            rows.append((key[0], key[1], key[2], raw, source))
        if store is not None and rows and hasattr(store, "upsert_package_licenses"):
            try:
                store.upsert_package_licenses(rows)
            except Exception as exc:  # noqa: BLE001 - the answer stands without the cache
                logger.debug("Could not cache licenses: %s", exc)
    except Exception as exc:  # noqa: BLE001 - licenses are best effort
        logger.warning("License resolution failed, continuing without: %s", exc)
    return out
