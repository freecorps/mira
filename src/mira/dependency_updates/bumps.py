"""Finding the version bumps in a pull request, from the manifests themselves.

A bump is read by parsing the manifest at both ends of the pull request with
the same deterministic parsers the indexer uses, not by matching ``-``/``+``
lines: a diff line shows that *something* changed, while two parsed manifests
say which package moved from which version to which. A package whose version
cannot be read as a concrete version on either side (``*``, a git URL, a Go
pseudo-version, ``workspace:``) is not a bump anyone has release notes for and
is left out.
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Awaitable, Callable

from mira.dependency_updates.models import DependencyBump
from mira.index.manifests import ParsedPackage, _is_lockfile_path, is_manifest, parse_manifest
from mira.models import FileChangeType, FileDiff
from mira.security.osv import normalize_version

logger = logging.getLogger(__name__)

#: ``await read(path, side)`` with side ``"base"`` or ``"head"``: the file's
#: text at that end of the review, or None when it is not there.
ManifestReader = Callable[[str, str], Awaitable[str | None]]

#: Ecosystems with a registry Mira knows how to ask for a source repository.
SUPPORTED_KINDS = frozenset({"pip", "npm", "go", "composer"})

#: Changed manifests read per review. Each costs two reads.
MAX_MANIFESTS = 10

_PRE_RANK = {"dev": 0, "a": 1, "alpha": 1, "b": 2, "beta": 2, "c": 3, "rc": 3, "pre": 3}
_VERSION_CORE = (
    r"(?P<nums>\d+(?:\.\d+){0,3})"
    r"(?:[-.]?(?P<pre>dev|alpha|beta|preview|pre|rc|a|b|c|next|canary)\.?(?P<prenum>\d*))?"
)
_VERSION_FULL = re.compile(r"v?" + _VERSION_CORE + r"(?:\+[\w.\-]+)?", re.IGNORECASE)

VersionKey = tuple[int, ...]


def version_key(text: str) -> VersionKey | None:
    """A sortable key for a concrete version, or None when it is not one.

    Pre-releases sort before their release (``2.0.0rc1 < 2.0.0``). Go
    pseudo-versions (``v0.0.0-20240101000000-abcdef123456``) and anything with
    letters the grammar does not know are not versions here.
    """
    m = _VERSION_FULL.fullmatch((text or "").strip())
    if not m:
        return None
    nums = [int(n) for n in m.group("nums").split(".")]
    nums += [0] * (4 - len(nums))
    pre = (m.group("pre") or "").lower()
    if pre:
        return (*nums, 0, _PRE_RANK.get(pre, 3), int(m.group("prenum") or 0))
    return (*nums, 1, 0, 0)


def is_prerelease(key: VersionKey) -> bool:
    return key[4] == 0


def concrete_version(spec: str) -> str:
    """The version a constraint names (``^4.18.0`` → ``4.18.0``), or ""."""
    version = normalize_version(spec)
    return version if version_key(version) is not None else ""


def _by_name(packages: list[ParsedPackage]) -> dict[tuple[str, str], ParsedPackage]:
    out: dict[tuple[str, str], ParsedPackage] = {}
    for pkg in packages:
        if pkg.kind not in SUPPORTED_KINDS:
            continue
        out.setdefault((pkg.kind, pkg.name.lower()), pkg)
    return out


def bumps_between(base: str, head: str, path: str, base_path: str = "") -> list[DependencyBump]:
    """Every package whose concrete version goes *up* between two manifest texts."""
    before = _by_name(parse_manifest(base_path or path, base))
    after = _by_name(parse_manifest(path, head))
    out: list[DependencyBump] = []
    for key, new_pkg in after.items():
        old_pkg = before.get(key)
        if old_pkg is None or old_pkg.version == new_pkg.version:
            continue
        old, new = concrete_version(old_pkg.version), concrete_version(new_pkg.version)
        old_key, new_key = version_key(old), version_key(new)
        if old_key is None or new_key is None or new_key <= old_key:
            continue
        out.append(
            DependencyBump(
                name=new_pkg.name,
                kind=new_pkg.kind,
                old=old,
                new=new,
                file_path=path,
                old_spec=old_pkg.version,
                new_spec=new_pkg.version,
            )
        )
    return out


def candidate_manifests(files: list[FileDiff]) -> list[FileDiff]:
    """Changed manifests that exist on both sides; lockfiles and Dockerfiles aside."""
    out = []
    for f in files:
        if f.change_type not in (FileChangeType.MODIFIED, FileChangeType.RENAMED):
            continue
        if not is_manifest(f.path) or _is_lockfile_path(f.path):
            continue
        if f.path.rsplit("/", 1)[-1].lower().endswith("dockerfile"):
            continue
        out.append(f)
    return out[:MAX_MANIFESTS]


async def detect_bumps(files: list[FileDiff], read: ManifestReader) -> list[DependencyBump]:
    """Read each changed manifest at both ends and collect its upgrades.

    A manifest that cannot be read on either side contributes nothing; the
    others still count.
    """

    async def one(f: FileDiff) -> list[DependencyBump]:
        base_path = f.old_path or f.path
        try:
            base, head = await asyncio.gather(read(base_path, "base"), read(f.path, "head"))
        except Exception as exc:
            logger.debug("Dependency updates: could not read %s: %s", f.path, exc)
            return []
        if not base or not head:
            return []
        return bumps_between(base, head, f.path, base_path)

    found = await asyncio.gather(*(one(f) for f in candidate_manifests(files)))
    seen: set[tuple[str, str, str, str]] = set()
    out: list[DependencyBump] = []
    for bumps in found:
        for bump in bumps:
            if bump.key not in seen:
                seen.add(bump.key)
                out.append(bump)
    return out
