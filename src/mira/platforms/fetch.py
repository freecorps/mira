"""Repo content fetching for the indexer, per platform.

Indexing operates on a whole repo (tree, file contents, tarball) with no PR in
hand, so it doesn't go through the PR-shaped ``BaseProvider``. ``RepoFetcher`` is
the thin seam the indexer fetches through; ``make_fetcher(platform, token)``
returns the right implementation.
"""

from __future__ import annotations

import asyncio
import io
import logging
import queue
import tarfile
import threading
import time
import zlib
from dataclasses import dataclass, field
from typing import Any, Protocol
from urllib.parse import quote

import httpx

from mira.platforms import profiles

logger = logging.getLogger(__name__)


class EmptyRepoError(Exception):
    """Raised when a repo has no commits/files to index (not a failure)."""

    def __init__(self, owner: str, repo: str) -> None:
        super().__init__(f"Repository {owner}/{repo} is empty — push code, then re-index.")


class RepoFetcher(Protocol):
    async def default_branch(self, owner: str, repo: str) -> str: ...

    async def repo_tree(self, owner: str, repo: str, branch: str) -> list[str]: ...

    async def file_content(
        self,
        owner: str,
        repo: str,
        path: str,
        ref: str,
        semaphore: asyncio.Semaphore | None = None,
    ) -> str | None: ...

    async def repo_tarball(
        self,
        owner: str,
        repo: str,
        ref: str,
        max_file_size: int = 1_048_576,
        indexable_paths: set[str] | None = None,
    ) -> dict[str, str] | None: ...


def _strip_tarball(
    blob: bytes,
    max_file_size: int,
    label: str,
    indexable_paths: set[str] | None = None,
) -> dict[str, str] | None:
    """Decode a gzipped tarball into ``{repo-relative path: text}``.

    Both GitHub and GitLab wrap files under a single top-level dir
    (``owner-repo-{sha}/`` / ``repo-{ref}-{sha}/``); we strip whatever it is.
    """
    out: dict[str, str] = {}
    try:
        with tarfile.open(fileobj=io.BytesIO(blob), mode="r:gz") as tf:
            for member in tf:
                if not member.isfile():
                    continue
                if max_file_size and member.size > max_file_size:
                    continue
                parts = member.name.split("/", 1)
                if len(parts) != 2 or not parts[1]:
                    continue
                rel_path = parts[1]
                if indexable_paths is not None and rel_path not in indexable_paths:
                    continue
                f = tf.extractfile(member)
                if f is None:
                    continue
                try:
                    out[parts[1]] = f.read().decode("utf-8")
                except UnicodeDecodeError:
                    continue
    except (tarfile.TarError, OSError) as exc:
        logger.warning("Tarball extract failed for %s: %s", label, exc)
        return None
    logger.info("Tarball: fetched %d files for %s in one request", len(out), label)
    return out


@dataclass
class RepoSnapshot:
    """A repository at one commit, read from a single archive download.

    ``files`` holds the text of every file worth reading; ``paths`` is every
    file in the archive, including the ones left out of ``files`` (binary,
    oversized, vendored), so a lookup can tell "no such file" from "a file
    the snapshot chose not to hold".
    """

    files: dict[str, str] = field(default_factory=dict)
    paths: set[str] = field(default_factory=set)
    # True when text was left out for the memory cap rather than for what the
    # file is: a search over ``files`` then no longer covers the repository.
    partial: bool = False
    # Source files left out only for their size: they exist and could hold a
    # match, so a search over ``files`` has not covered them either.
    oversized: set[str] = field(default_factory=set)


# Never decoded into a snapshot: build output, vendored code and binary
# formats. A review reads source, and on a small host every megabyte of
# minified bundle held in memory is a megabyte the review cannot use.
_SNAPSHOT_SKIP_DIRS = (
    "node_modules/",
    "vendor/",
    "dist/",
    "build/",
    ".git/",
    "__pycache__/",
    ".next/",
    "coverage/",
)
_SNAPSHOT_SKIP_SUFFIXES = (
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".webp",
    ".ico",
    ".pdf",
    ".zip",
    ".gz",
    ".tgz",
    ".tar",
    ".jar",
    ".woff",
    ".woff2",
    ".ttf",
    ".eot",
    ".mp4",
    ".mp3",
    ".map",
    ".min.js",
    ".min.css",
    ".lockb",
    ".so",
    ".dll",
    ".exe",
    ".bin",
    ".pyc",
)


def _snapshot_skips(path: str) -> bool:
    lower = path.lower()
    if lower.endswith(_SNAPSHOT_SKIP_SUFFIXES):
        return True
    return any(lower.startswith(d) or f"/{d}" in lower for d in _SNAPSHOT_SKIP_DIRS)


def _snapshot_from_stream(
    stream: io.BufferedIOBase, max_file_size: int, label: str, max_text_bytes: int = 0
) -> RepoSnapshot | None:
    """Decode a gzipped archive, read front to back, into a :class:`RepoSnapshot`.

    CPU-bound; run off the loop. The archive is read as a stream, so only the
    text kept is ever in memory, not the archive: a repository whose size is
    mostly screenshots and fixtures costs what its source costs.
    ``max_text_bytes`` caps that text (0: no cap); past it, files are listed
    but not kept, and the snapshot says it is partial.
    """
    snapshot = RepoSnapshot()
    held = 0
    try:
        with tarfile.open(fileobj=stream, mode="r|gz") as tf:
            for member in tf:
                if not member.isfile():
                    continue
                parts = member.name.split("/", 1)
                if len(parts) != 2 or not parts[1]:
                    continue
                path = parts[1]
                snapshot.paths.add(path)
                if _snapshot_skips(path):
                    continue
                if max_file_size and member.size > max_file_size:
                    snapshot.oversized.add(path)
                    continue
                if max_text_bytes and held + member.size > max_text_bytes:
                    snapshot.partial = True
                    continue
                f = tf.extractfile(member)
                if f is None:
                    continue
                data = f.read()
                if b"\x00" in data[:8192]:
                    continue
                try:
                    snapshot.files[path] = data.decode("utf-8")
                except UnicodeDecodeError:
                    continue
                held += len(data)
    except (tarfile.TarError, OSError, EOFError, zlib.error) as exc:
        logger.info("Snapshot extract failed for %s: %s", label, exc)
        return None
    return snapshot


def _snapshot_from_tarball(
    blob: bytes, max_file_size: int, label: str, max_text_bytes: int = 0
) -> RepoSnapshot | None:
    """:func:`_snapshot_from_stream` over an archive already in memory."""
    return _snapshot_from_stream(io.BytesIO(blob), max_file_size, label, max_text_bytes)


class _QueueReader(io.RawIOBase):
    """A byte stream read in one thread and fed, chunk by chunk, from another.

    ``None`` on the queue is the end of the stream. What the download has not
    delivered yet, the reader waits for.
    """

    def __init__(self, chunks: queue.Queue[bytes | None]) -> None:
        self._chunks = chunks
        self._pending = memoryview(b"")
        self._done = False

    def readable(self) -> bool:
        return True

    def readinto(self, buffer: Any) -> int:
        while not len(self._pending) and not self._done:
            chunk = self._chunks.get()
            if chunk is None:
                self._done = True
            else:
                self._pending = memoryview(chunk)
        n = min(len(buffer), len(self._pending))
        buffer[:n] = self._pending[:n]
        self._pending = self._pending[n:]
        return n


# Text a snapshot may hold in memory. Enough for any source tree a review is
# likely to meet, and still small next to the host: on the Orange Pi profile a
# review should not cost more than this in snapshot, whatever the repository.
_MAX_SNAPSHOT_TEXT_BYTES = 150 * 1024 * 1024


async def fetch_snapshot(
    url: str,
    headers: dict[str, str],
    *,
    label: str,
    max_bytes: int,
    max_file_size: int = 1_048_576,
    max_text_bytes: int = _MAX_SNAPSHOT_TEXT_BYTES,
    timeout: float = 60.0,
) -> RepoSnapshot | None:
    """Download a repository archive and decode it as it arrives, or None.

    The archive is never held whole: chunks go straight from the socket to a
    worker thread that decompresses and reads them, keeping only text files.
    The download is abandoned the moment it passes ``max_bytes``, and the
    worker ends with it. Decoding stays off the event loop, where it would
    stall every other review and webhook for as long as it took.
    """
    started = time.monotonic()
    chunks: queue.Queue[bytes | None] = queue.Queue(maxsize=64)
    finished = threading.Event()

    def _extract() -> RepoSnapshot | None:
        try:
            reader = io.BufferedReader(_QueueReader(chunks), 1 << 16)
            return _snapshot_from_stream(reader, max_file_size, label, max_text_bytes)
        except Exception as exc:  # noqa: BLE001 — a failed snapshot only costs speed
            logger.info("Snapshot extract failed for %s: %s", label, exc)
            return None
        finally:
            finished.set()

    def _put(item: bytes | None) -> bool:
        # Blocks while the worker is behind; gives up once it has stopped.
        while not finished.is_set():
            try:
                chunks.put(item, timeout=0.5)
                return True
            except queue.Full:
                continue
        return False

    extraction = asyncio.ensure_future(asyncio.to_thread(_extract))
    complete = False
    size = 0
    try:
        async with (
            httpx.AsyncClient(follow_redirects=True, timeout=timeout) as client,
            client.stream("GET", url, headers=headers) as resp,
        ):
            if resp.status_code != 200:
                logger.info("Snapshot download refused for %s: HTTP %d", label, resp.status_code)
                return None
            async for chunk in resp.aiter_bytes():
                size += len(chunk)
                if size > max_bytes:
                    logger.info(
                        "Snapshot of %s passed %d MB; reading files one by one instead",
                        label,
                        max_bytes // (1024 * 1024),
                    )
                    return None
                try:
                    chunks.put_nowait(chunk)
                except queue.Full:
                    if not await asyncio.to_thread(_put, chunk):
                        break
            complete = True
    except Exception as exc:  # noqa: BLE001 — any failure means "read files one by one"
        logger.info("Snapshot download failed for %s: %s: %s", label, type(exc).__name__, exc)
        return None
    finally:
        # End of stream, whole or abandoned: the worker reads to it and stops.
        # An abandoned archive ends mid-member, which it reports as a failure.
        await asyncio.to_thread(_put, None)
        if not complete:
            extraction.cancel()

    snapshot = await extraction
    if snapshot is not None:
        logger.info(
            "Snapshot of %s: %d files (%d readable%s, %.1f MB) in %.1fs",
            label,
            len(snapshot.paths),
            len(snapshot.files),
            ", partial: memory cap reached" if snapshot.partial else "",
            size / (1024 * 1024),
            time.monotonic() - started,
        )
    return snapshot


class GitHubRepoFetcher:
    """Fetches repo content via the GitHub REST API."""

    def __init__(self, token: str, api_url: str = "https://api.github.com") -> None:
        self._token = token
        self._api = api_url.rstrip("/")

    def _headers(self, accept: str = "application/vnd.github+json") -> dict[str, str]:
        return {"Authorization": f"Bearer {self._token}", "Accept": accept}

    async def default_branch(self, owner: str, repo: str) -> str:
        url = f"{self._api}/repos/{owner}/{repo}"
        try:
            async with httpx.AsyncClient() as client:
                resp = await client.get(url, headers=self._headers(), timeout=15)
                resp.raise_for_status()
                return str(resp.json().get("default_branch", "main"))
        except Exception as exc:
            logger.warning("Failed to fetch default branch for %s/%s: %s", owner, repo, exc)
            return "main"

    async def repo_tree(self, owner: str, repo: str, branch: str) -> list[str]:
        url = f"{self._api}/repos/{owner}/{repo}/git/trees/{branch}?recursive=1"
        async with httpx.AsyncClient() as client:
            resp = await client.get(url, headers=self._headers(), timeout=30)
            # GitHub returns 409 (and sometimes 404) for an empty repo.
            if resp.status_code in (404, 409):
                raise EmptyRepoError(owner, repo)
            resp.raise_for_status()
            data = resp.json()
        return [item["path"] for item in data.get("tree", []) if item.get("type") == "blob"]

    async def file_content(
        self, owner: str, repo: str, path: str, ref: str, semaphore=None
    ) -> str | None:
        url = f"{self._api}/repos/{owner}/{repo}/contents/{path}?ref={ref}"
        headers = self._headers("application/vnd.github.raw+json")

        async def _fetch() -> str | None:
            try:
                async with httpx.AsyncClient() as client:
                    resp = await client.get(url, headers=headers, timeout=30)
                    if resp.status_code == 404:
                        return None
                    resp.raise_for_status()
                    return resp.text
            except Exception as exc:
                logger.warning("Failed to fetch %s: %s", path, exc)
                return None

        if semaphore:
            async with semaphore:
                return await _fetch()
        return await _fetch()

    async def repo_tarball(
        self,
        owner: str,
        repo: str,
        ref: str,
        max_file_size: int = 1_048_576,
        indexable_paths: set[str] | None = None,
    ) -> dict[str, str] | None:
        url = f"{self._api}/repos/{owner}/{repo}/tarball/{ref}"
        headers = {**self._headers(), "User-Agent": "mira-indexer"}
        try:
            async with httpx.AsyncClient(follow_redirects=True, timeout=120) as client:
                resp = await client.get(url, headers=headers)
                if resp.status_code != 200:
                    logger.warning(
                        "Tarball fetch failed for %s/%s: %d", owner, repo, resp.status_code
                    )
                    return None
                blob = resp.content
        except Exception as exc:
            logger.warning("Tarball fetch failed for %s/%s: %s", owner, repo, exc)
            return None
        return _strip_tarball(blob, max_file_size, f"{owner}/{repo}", indexable_paths)


class GitLabRepoFetcher:
    """Fetches repo content via the GitLab REST v4 API.

    Project id is the URL-encoded ``owner/repo`` path (``owner`` may itself
    contain slashes for nested groups). The tree endpoint is paginated, unlike
    GitHub's single recursive call.
    """

    def __init__(self, token: str, base_url: str = "https://gitlab.com/api/v4") -> None:
        self._token = token
        self._api = base_url.rstrip("/")

    def _headers(self) -> dict[str, str]:
        return {"PRIVATE-TOKEN": self._token}

    @staticmethod
    def _pid(owner: str, repo: str) -> str:
        return quote(f"{owner}/{repo}", safe="")

    def _project(self, owner: str, repo: str) -> str:
        return f"{self._api}/projects/{self._pid(owner, repo)}"

    async def default_branch(self, owner: str, repo: str) -> str:
        try:
            async with httpx.AsyncClient() as client:
                resp = await client.get(
                    self._project(owner, repo), headers=self._headers(), timeout=15
                )
                resp.raise_for_status()
                return str(resp.json().get("default_branch", "main"))
        except Exception as exc:
            logger.warning("Failed to fetch default branch for %s/%s: %s", owner, repo, exc)
            return "main"

    async def repo_tree(self, owner: str, repo: str, branch: str) -> list[str]:
        # Keyset pagination via the Link header; recursive lists the whole tree.
        url: str | None = (
            f"{self._project(owner, repo)}/repository/tree"
            f"?recursive=true&per_page=100&pagination=keyset&ref={branch}"
        )
        paths: list[str] = []
        async with httpx.AsyncClient() as client:
            while url:
                resp = await client.get(url, headers=self._headers(), timeout=30)
                # An empty repo (or a ref that doesn't exist yet) 404s.
                if resp.status_code == 404:
                    raise EmptyRepoError(owner, repo)
                resp.raise_for_status()
                for item in resp.json():
                    if item.get("type") == "blob":
                        paths.append(item["path"])
                url = _next_link(resp.headers.get("link", ""))
        return paths

    async def file_content(
        self, owner: str, repo: str, path: str, ref: str, semaphore=None
    ) -> str | None:
        url = f"{self._project(owner, repo)}/repository/files/{quote(path, safe='')}/raw?ref={ref}"

        async def _fetch() -> str | None:
            try:
                async with httpx.AsyncClient() as client:
                    resp = await client.get(url, headers=self._headers(), timeout=30)
                    if resp.status_code == 404:
                        return None
                    resp.raise_for_status()
                    return resp.text
            except Exception as exc:
                logger.warning("Failed to fetch %s: %s", path, exc)
                return None

        if semaphore:
            async with semaphore:
                return await _fetch()
        return await _fetch()

    async def repo_tarball(
        self,
        owner: str,
        repo: str,
        ref: str,
        max_file_size: int = 1_048_576,
        indexable_paths: set[str] | None = None,
    ) -> dict[str, str] | None:
        url = f"{self._project(owner, repo)}/repository/archive.tar.gz?sha={ref}"
        try:
            async with httpx.AsyncClient(follow_redirects=True, timeout=120) as client:
                resp = await client.get(url, headers=self._headers())
                if resp.status_code != 200:
                    logger.warning(
                        "Tarball fetch failed for %s/%s: %d", owner, repo, resp.status_code
                    )
                    return None
                blob = resp.content
        except Exception as exc:
            logger.warning("Tarball fetch failed for %s/%s: %s", owner, repo, exc)
            return None
        return _strip_tarball(blob, max_file_size, f"{owner}/{repo}", indexable_paths)


class ForgejoRepoFetcher:
    """Fetches repo content via the Gitea/Forgejo REST API (/api/v1).

    Forgejo ``owner`` and ``repo`` are simple names (no nested groups).
    Uses ``Authorization: token <token>`` for auth.
    """

    def __init__(self, token: str, base_url: str = "https://codeberg.org/api/v1") -> None:
        self._token = token
        self._api = base_url.rstrip("/")

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"token {self._token}"}

    def _repo(self, owner: str, repo: str) -> str:
        return f"{self._api}/repos/{owner}/{repo}"

    async def default_branch(self, owner: str, repo: str) -> str:
        try:
            async with httpx.AsyncClient() as client:
                resp = await client.get(
                    self._repo(owner, repo), headers=self._headers(), timeout=15
                )
                resp.raise_for_status()
                return str(resp.json().get("default_branch", "main"))
        except Exception as exc:
            logger.warning("Failed to fetch default branch for %s/%s: %s", owner, repo, exc)
            return "main"

    async def repo_tree(self, owner: str, repo: str, branch: str) -> list[str]:
        url = f"{self._repo(owner, repo)}/git/trees/{branch}?recursive=true"
        paths: list[str] = []
        async with httpx.AsyncClient() as client:
            page = 1
            while True:
                page_url = f"{url}&page={page}" if page > 1 else url
                resp = await client.get(page_url, headers=self._headers(), timeout=30)
                if resp.status_code == 404:
                    raise EmptyRepoError(owner, repo)
                resp.raise_for_status()
                data = resp.json()
                for item in data.get("tree") or []:
                    if item.get("type") == "blob":
                        paths.append(item["path"])
                if not data.get("truncated", False):
                    break
                page += 1
        return paths

    async def file_content(
        self, owner: str, repo: str, path: str, ref: str, semaphore=None
    ) -> str | None:
        import base64

        url = f"{self._repo(owner, repo)}/contents/{quote(path, safe='')}?ref={ref}"

        async def _fetch() -> str | None:
            try:
                async with httpx.AsyncClient() as client:
                    resp = await client.get(url, headers=self._headers(), timeout=30)
                    if resp.status_code == 404:
                        return None
                    resp.raise_for_status()
                    data = resp.json()
                    raw = data["content"]
                    return base64.b64decode(raw).decode("utf-8")
            except Exception as exc:
                logger.warning("Failed to fetch %s: %s", path, exc)
                return None

        if semaphore:
            async with semaphore:
                return await _fetch()
        return await _fetch()

    async def repo_tarball(
        self,
        owner: str,
        repo: str,
        ref: str,
        max_file_size: int = 1_048_576,
        indexable_paths: set[str] | None = None,
    ) -> dict[str, str] | None:
        url = f"{self._repo(owner, repo)}/archive/{ref}.tar.gz"
        try:
            async with httpx.AsyncClient(follow_redirects=True, timeout=120) as client:
                resp = await client.get(url, headers=self._headers())
                if resp.status_code != 200:
                    logger.warning(
                        "Tarball fetch failed for %s/%s: %d", owner, repo, resp.status_code
                    )
                    return None
                blob = resp.content
        except Exception as exc:
            logger.warning("Tarball fetch failed for %s/%s: %s", owner, repo, exc)
            return None
        return _strip_tarball(blob, max_file_size, f"{owner}/{repo}", indexable_paths)


def _next_link(link_header: str) -> str | None:
    """Extract the rel="next" URL from a Link header (GitLab keyset pagination)."""
    if not link_header:
        return None
    for part in link_header.split(","):
        if 'rel="next"' in part:
            return part.split(";")[0].strip().strip("<>")
    return None


def make_fetcher(platform: str, token: str) -> RepoFetcher:
    """Build the RepoFetcher for a platform, using its profile's api_url."""
    profile = profiles.resolve(platform)
    api_url = profile["api_url"]
    if platform == "gitlab":
        return GitLabRepoFetcher(token, api_url or "https://gitlab.com/api/v4")
    if platform == "forgejo":
        return ForgejoRepoFetcher(token, api_url or "https://codeberg.org/api/v1")
    return GitHubRepoFetcher(token, api_url or "https://api.github.com")
