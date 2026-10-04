"""Fetching upstream release notes: registries, GitHub releases, changelogs.

Every request goes through :class:`Fetcher`, which is where the network posture
lives:

* **Only listed hosts, only HTTPS.** ``allowed_hosts`` (or the
  ``MIRA_DEPENDENCY_UPDATES_HOSTS`` environment variable, which wins) is the
  complete list. Registry metadata is written by package authors, so a
  repository URL read from it is never fetched as given: only a GitHub
  ``owner/repo`` pair is taken from it, and the URLs Mira requests are built
  from that pair on fixed hosts.
* **No private networks.** A host that resolves to a private, loopback,
  link-local or reserved address is refused, the same rule the outbound
  webhooks use. Redirects are not followed.
* **A budget, not just timeouts.** One review shares a deadline and a byte
  allowance across every request; a response is read in chunks and cut off at
  what is left.
* **An in-process TTL cache.** Release notes for ``requests 2.31 → 2.32`` are
  the same for every repository that makes that bump.
"""

from __future__ import annotations

import asyncio
import ipaddress
import json
import logging
import os
import re
import socket
import time
from typing import Any
from urllib.parse import quote, urlsplit

import httpx

from mira.dependency_updates.bumps import is_prerelease, version_key
from mira.dependency_updates.models import DependencyBump, ReleaseNote

logger = logging.getLogger(__name__)

HOSTS_ENV = "MIRA_DEPENDENCY_UPDATES_HOSTS"

#: How long a fetched document, and a host's resolution, is trusted.
CACHE_TTL_SECONDS = 6 * 3600
_CACHE_MAX_ENTRIES = 512
#: Most a single response may contribute, whatever the review has left.
MAX_RESPONSE_BYTES = 1_000_000
#: Releases kept per bump, newest first, once the range is selected.
MAX_RELEASES = 15

_USER_AGENT = "mira-code-review (dependency release notes)"
_GITHUB_SEGMENT = re.compile(r"^[A-Za-z0-9_.-]{1,100}$")
_GITHUB_URL = re.compile(
    r"github\.com[/:]+([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+?)(?:\.git)?(?:[/#?].*)?$",
    re.IGNORECASE,
)
_CHANGELOG_FILES = ("CHANGELOG.md", "CHANGES.md", "HISTORY.md", "CHANGES.rst", "NEWS.md")

# url → (expires_at, status, text)
_CACHE: dict[str, tuple[float, int, str]] = {}
# host → (expires_at, is_public)
_HOST_CACHE: dict[str, tuple[float, bool]] = {}


def reset_cache() -> None:
    _CACHE.clear()
    _HOST_CACHE.clear()


def allowed_hosts(configured: list[str]) -> set[str]:
    """The hosts this install may contact. An empty set means: contact nothing.

    The environment wins over configuration so an offline install can turn the
    feature off without touching every repository's ``.mira.yaml``:
    ``MIRA_DEPENDENCY_UPDATES_HOSTS=""``.
    """
    raw = os.environ.get(HOSTS_ENV)
    hosts = raw.split(",") if raw is not None else configured
    return {h.strip().lower() for h in hosts if h and h.strip()}


def _is_blocked_ip(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    from mira.outbound_webhooks import _is_blocked_ip as blocked

    return blocked(ip)


async def resolves_public(host: str) -> bool:
    """Whether every address ``host`` resolves to is a public one."""
    now = time.monotonic()
    cached = _HOST_CACHE.get(host)
    if cached and cached[0] > now:
        return cached[1]
    try:
        try:
            return not _is_blocked_ip(ipaddress.ip_address(host))
        except ValueError:
            pass
        loop = asyncio.get_running_loop()
        infos = await loop.getaddrinfo(host, 443, proto=socket.IPPROTO_TCP)
        public = bool(infos) and not any(
            _is_blocked_ip(ipaddress.ip_address(info[4][0])) for info in infos
        )
    except (OSError, ValueError) as exc:
        logger.debug("Dependency updates: could not resolve %s: %s", host, exc)
        public = False
    _HOST_CACHE[host] = (now + CACHE_TTL_SECONDS, public)
    return public


class Budget:
    """A deadline and a byte allowance shared by every request of one review."""

    def __init__(self, seconds: float, max_bytes: int) -> None:
        self.deadline = time.monotonic() + seconds
        self.bytes_left = max_bytes

    def time_left(self) -> float:
        return self.deadline - time.monotonic()

    @property
    def exhausted(self) -> bool:
        return self.time_left() <= 0 or self.bytes_left <= 0


class Fetcher:
    """GET with the allowlist, the private-network check, the budget and the cache."""

    def __init__(
        self,
        client: httpx.AsyncClient,
        *,
        hosts: set[str],
        budget: Budget,
        request_timeout: float,
        github_token: str = "",
    ) -> None:
        self.client = client
        self.hosts = hosts
        self.budget = budget
        self.request_timeout = request_timeout
        self.github_token = github_token
        self.github_limited = False

    async def get(self, url: str) -> tuple[int, str] | None:
        """``(status, text)``, or None when the request was refused or failed."""
        parts = urlsplit(url)
        host = (parts.hostname or "").lower()
        if parts.scheme != "https" or host not in self.hosts:
            logger.debug("Dependency updates: %s is not an allowed host", host or url)
            return None
        if host == "api.github.com" and self.github_limited:
            return None
        now = time.monotonic()
        cached = _CACHE.get(url)
        if cached and cached[0] > now:
            return cached[1], cached[2]
        if self.budget.exhausted:
            return None
        if not await resolves_public(host):
            logger.info("Dependency updates: refusing %s (not a public address)", host)
            return None
        headers = {"User-Agent": _USER_AGENT}
        if host == "api.github.com":
            headers["Accept"] = "application/vnd.github+json"
            if self.github_token:
                headers["Authorization"] = f"Bearer {self.github_token}"
        cap = min(MAX_RESPONSE_BYTES, self.budget.bytes_left)
        timeout = max(0.1, min(self.request_timeout, self.budget.time_left()))
        try:
            async with self.client.stream(
                "GET", url, headers=headers, timeout=timeout, follow_redirects=False
            ) as resp:
                chunks: list[bytes] = []
                size = 0
                truncated = False
                async for chunk in resp.aiter_bytes():
                    room = cap - size
                    if len(chunk) >= room:
                        chunks.append(chunk[:room])
                        size += room
                        truncated = True
                        break
                    chunks.append(chunk)
                    size += len(chunk)
                status = resp.status_code
        except (httpx.HTTPError, OSError) as exc:
            logger.debug("Dependency updates: GET %s failed: %s", url, exc)
            return None
        self.budget.bytes_left -= size
        if host == "api.github.com" and status in (403, 429):
            # The unauthenticated quota is per IP and shared; once it is gone
            # every further call this review would make fails the same way.
            self.github_limited = True
            logger.info("Dependency updates: GitHub API rate limit reached")
        text = b"".join(chunks).decode("utf-8", errors="replace")
        if truncated:
            # Half a JSON document is not a document; half a changelog still is.
            if status == 200 and text.lstrip()[:1] in ("{", "["):
                return None
            return status, text
        if status in (200, 404):
            if len(_CACHE) >= _CACHE_MAX_ENTRIES:
                _CACHE.pop(next(iter(_CACHE)))
            _CACHE[url] = (now + CACHE_TTL_SECONDS, status, text)
        return status, text

    async def json(self, url: str) -> Any:
        got = await self.get(url)
        if got is None or got[0] != 200:
            return None
        try:
            return json.loads(got[1])
        except ValueError:
            return None


# ── Source repository from registry metadata ─────────────────────────


def github_repo(url: object) -> tuple[str, str] | None:
    """``(owner, repo)`` from a URL naming a GitHub repository, else None."""
    if not isinstance(url, str):
        return None
    m = _GITHUB_URL.search(url.strip())
    if not m:
        return None
    owner, repo = m.group(1), m.group(2)
    if not (_GITHUB_SEGMENT.match(owner) and _GITHUB_SEGMENT.match(repo)):
        return None
    if repo.lower().endswith(".git"):
        repo = repo[:-4]
    return owner, repo


def _go_escape(path: str) -> str:
    """The Go module proxy's case encoding: ``Azure`` → ``!azure``."""
    return "".join(f"!{c.lower()}" if c.isupper() else c for c in path)


def _first_github(urls: list[object]) -> tuple[str, str] | None:
    for url in urls:
        found = github_repo(url)
        if found:
            return found
    return None


async def resolve_repo(fetcher: Fetcher, bump: DependencyBump) -> tuple[str, str] | None:
    """The package's GitHub repository, read from its registry's metadata."""
    name = bump.name
    if bump.kind == "pip":
        data = await fetcher.json(f"https://pypi.org/pypi/{quote(name, safe='')}/{bump.new}/json")
        info = (data or {}).get("info") or {}
        project_urls = info.get("project_urls") or {}
        ordered: list[object] = []
        if isinstance(project_urls, dict):
            preferred = ("source", "repository", "code", "github", "changelog", "homepage")
            for key in preferred:
                ordered += [v for k, v in project_urls.items() if key in str(k).lower()]
            ordered += list(project_urls.values())
        ordered.append(info.get("home_page"))
        return _first_github(ordered)
    if bump.kind == "npm":
        data = await fetcher.json(
            f"https://registry.npmjs.org/{quote(name, safe='@/')}/{quote(bump.new, safe='')}"
        )
        if not isinstance(data, dict):
            return None
        repo = data.get("repository")
        url = repo.get("url") if isinstance(repo, dict) else repo
        bugs = data.get("bugs")
        return _first_github(
            [url, data.get("homepage"), bugs.get("url") if isinstance(bugs, dict) else bugs]
        )
    if bump.kind == "go":
        if name.lower().startswith("github.com/"):
            return github_repo(name)
        data = await fetcher.json(
            f"https://proxy.golang.org/{quote(_go_escape(name), safe='/!')}/@v/"
            f"{quote(_go_escape('v' + bump.new.lstrip('v')), safe='!')}.info"
        )
        origin = (data or {}).get("Origin") or {}
        return github_repo(origin.get("URL"))
    if bump.kind == "composer":
        data = await fetcher.json(f"https://repo.packagist.org/p2/{quote(name, safe='/')}.json")
        versions = ((data or {}).get("packages") or {}).get(name) or []
        for entry in versions if isinstance(versions, list) else []:
            if isinstance(entry, dict):
                source = entry.get("source") or {}
                found = github_repo(source.get("url") if isinstance(source, dict) else None)
                if found:
                    return found
        return None
    return None


# ── Choosing the releases between two versions ───────────────────────

_TAG_VERSION = re.compile(
    r"v?(\d+(?:\.\d+){0,3}(?:[-.]?(?:dev|alpha|beta|preview|pre|rc|a|b|c|next|canary)\.?\d*)?)$",
    re.IGNORECASE,
)
_GENERIC_PREFIXES = {"", "v", "release", "rel", "version"}


def _norm(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name.lower().lstrip("@"))


def _name_variants(name: str) -> set[str]:
    lowered = name.lower()
    return {_norm(lowered), _norm(lowered.rsplit("/", 1)[-1])}


def parse_tag(tag: str) -> tuple[str, str] | None:
    """``(prefix, version)`` of a release tag: ``pkg@1.2.0`` → ``("pkg", "1.2.0")``."""
    m = _TAG_VERSION.search(tag.strip())
    if not m:
        return None
    prefix = tag[: m.start()].strip().rstrip("-_/@ ").lower()
    return prefix, m.group(1)


def in_range(version: str, bump: DependencyBump) -> bool:
    """Whether ``version`` is after ``bump.old`` and at most ``bump.new``.

    Pre-releases count only when the bump itself lands on one — a move to
    ``2.0.0`` is described by the 2.0.0 notes, not by every ``2.0.0rc`` before.
    """
    key, old, new = version_key(version), version_key(bump.old), version_key(bump.new)
    if key is None or old is None or new is None:
        return False
    if is_prerelease(key) and not is_prerelease(new):
        return False
    return old < key <= new


def select_releases(releases: list[Any], bump: DependencyBump) -> list[ReleaseNote]:
    """The published releases between the two versions, newest first.

    A monorepo tags each package separately (``@scope/pkg@1.2.0``,
    ``pkg-v1.2.0``); when any tag names this package, only those count.
    """
    variants = _name_variants(bump.name)
    generic: list[tuple[tuple[int, ...], ReleaseNote]] = []
    named: list[tuple[tuple[int, ...], ReleaseNote]] = []
    for rel in releases:
        if not isinstance(rel, dict) or rel.get("draft"):
            continue
        parsed = parse_tag(str(rel.get("tag_name") or ""))
        if parsed is None:
            continue
        prefix, version = parsed
        is_named = _norm(prefix) in variants or _norm(prefix.rsplit("/", 1)[-1]) in variants
        if not is_named and prefix not in _GENERIC_PREFIXES:
            continue
        if not in_range(version, bump):
            continue
        key = version_key(version)
        assert key is not None
        note = ReleaseNote(
            version=version,
            url=str(rel.get("html_url") or ""),
            body=str(rel.get("body") or ""),
        )
        (named if is_named else generic).append((key, note))
    chosen = named or generic
    chosen.sort(key=lambda pair: pair[0], reverse=True)
    return [note for _, note in chosen[:MAX_RELEASES]]


_VERSION_IN_HEADING = re.compile(
    r"(?<![\d.])v?(\d+\.\d+(?:\.\d+){0,2}(?:[-.]?(?:dev|alpha|beta|pre|rc|a|b)\.?\d*)?)(?![\d])",
    re.IGNORECASE,
)


def changelog_sections(text: str, bump: DependencyBump) -> list[tuple[str, str]]:
    """``(version, body)`` for each changelog section in the bump's range.

    A heading is a markdown ``#`` line, an underlined reST title, or a line
    that starts with a version (``1.2.0 (2024-05-01)``, ``[1.2.0]``). Text
    before the first versioned heading — an "Unreleased" section — is ignored.
    """
    lines = text.splitlines()
    heads: list[tuple[int, str]] = []
    for i, line in enumerate(lines):
        stripped = line.strip()
        underlined = i + 1 < len(lines) and re.fullmatch(r"[=\-~^*]{3,}", lines[i + 1].strip())
        looks_heading = (
            stripped.startswith("#")
            or bool(underlined)
            or bool(re.match(r"^\[?v?\d+\.\d+", stripped))
        )
        if not looks_heading or len(stripped) > 120:
            continue
        m = _VERSION_IN_HEADING.search(stripped)
        if m:
            heads.append((i, m.group(1)))
    out: list[tuple[str, str]] = []
    for idx, (start, version) in enumerate(heads):
        end = heads[idx + 1][0] if idx + 1 < len(heads) else len(lines)
        if in_range(version, bump):
            out.append((version, "\n".join(lines[start:end]).strip()))
    return out[:MAX_RELEASES]


async def fetch_release_notes(
    fetcher: Fetcher, owner: str, repo: str, bump: DependencyBump
) -> tuple[list[ReleaseNote], str]:
    """Release notes in the bump's range, and the page to link to.

    GitHub releases first; a changelog file at the new version's tag when the
    project publishes no releases for the range.
    """
    base = f"https://github.com/{owner}/{repo}"
    releases = await fetcher.json(
        f"https://api.github.com/repos/{owner}/{repo}/releases?per_page=100"
    )
    if isinstance(releases, list):
        chosen = select_releases(releases, bump)
        if chosen:
            return chosen, chosen[0].url or f"{base}/releases"
    version = bump.new.lstrip("v")
    for tag in (f"v{version}", version):
        for name in _CHANGELOG_FILES:
            if fetcher.budget.exhausted:
                return [], ""
            got = await fetcher.get(
                f"https://raw.githubusercontent.com/{owner}/{repo}/{quote(tag, safe='')}/{name}"
            )
            if got is None or got[0] != 200:
                continue
            url = f"{base}/blob/{quote(tag, safe='')}/{name}"
            sections = changelog_sections(got[1], bump)
            return [ReleaseNote(version=v, url=url, body=b) for v, b in sections], url
    return [], ""
