"""Review-time context builder. Queries the index to enrich the review prompt.

Now async with support for fetching real source code from the PR's head branch.
"""

from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from mira.index.extract import extract_symbols, find_symbol_by_name
from mira.index.store import IndexStore
from mira.models import PRInfo
from mira.providers.base import BaseProvider

if TYPE_CHECKING:
    from mira.platforms.fetch import RepoSnapshot

logger = logging.getLogger(__name__)

_DEFAULT_TOKEN_BUDGET = 8_000
_CHARS_PER_TOKEN = 4  # conservative estimate


@runtime_checkable
class SourceFetcher(Protocol):
    """Protocol for fetching source code from a repository."""

    async def fetch(self, path: str) -> str | None:
        """Fetch the content of a file. Returns None on failure."""
        ...


class ProviderSourceFetcher:
    """Fetches source code from the PR's head branch via a provider."""

    def __init__(self, provider: BaseProvider, pr_info: PRInfo, ref: str) -> None:
        self._provider = provider
        self._pr_info = pr_info
        self._ref = ref
        self._cache: dict[str, str | None] = {}
        # Reads in flight, so readers that ask for the same file at the same
        # time — dependency discovery and code context do, on every changed
        # file — share one request instead of racing two.
        self._pending: dict[str, asyncio.Future[str | None]] = {}

    async def fetch(self, path: str) -> str | None:
        if path in self._cache:
            return self._cache[path]
        pending = self._pending.get(path)
        if pending is not None:
            return await asyncio.shield(pending)
        future: asyncio.Future[str | None] = asyncio.get_running_loop().create_future()
        self._pending[path] = future
        try:
            content = await self._provider.get_file_content(self._pr_info, path, self._ref)
            self._cache[path] = content if content else None
        except Exception as exc:
            logger.debug("Failed to fetch source for %s: %s", path, exc)
            self._cache[path] = None
        finally:
            self._pending.pop(path, None)
            future.set_result(self._cache.get(path))
        return self._cache[path]


# Repositories whose archive could not be had (too big, refused, too slow),
# with when to try again. A 300 MB monorepo would otherwise cost a 250 MB
# abandoned download on every review of it.
_SNAPSHOT_RETRY_AFTER = 30 * 60.0
_no_snapshot_until: dict[tuple[str, str, str], float] = {}


class SnapshotSourceFetcher:
    """The reviewed commit, read from one archive download, shared by a whole review.

    Before this, every reader in a review built its own
    :class:`ProviderSourceFetcher` — code context, dependency discovery, the
    agentic tools, the manifest scan — and each paid one API round trip per
    file, a few hundred on a large pull request, most of them before the
    first chunk could start. One archive replaces them all, and it is also
    what lets ``grep_repo`` search the whole repository rather than the
    handful of files it could afford to fetch one by one.

    The download starts on :meth:`start` and overlaps whatever runs before the
    first read. When it fails, times out or is too big, every read falls back
    to the per-file API, so a review never has less than it had before. A
    read waits for the archive at most ``read_wait`` seconds from the start:
    a large repository on a slow link must not hold the review longer than
    reading its files one by one would have, so past that, reads go to the
    API while the download carries on for the searches that need it whole.
    """

    def __init__(
        self,
        provider: BaseProvider,
        pr_info: PRInfo,
        ref: str,
        *,
        use_snapshot: bool = True,
        max_bytes: int = 250 * 1024 * 1024,
        timeout: float = 120.0,
        read_wait: float = 15.0,
    ) -> None:
        self._provider = provider
        self._pr_info = pr_info
        self._ref = ref
        self._api = ProviderSourceFetcher(provider, pr_info, ref)
        self._use_snapshot = use_snapshot and hasattr(provider, "get_repo_snapshot")
        self._max_bytes = max_bytes
        self._timeout = timeout
        self._read_wait = read_wait
        self._task: asyncio.Task[RepoSnapshot | None] | None = None
        self._started_at = 0.0
        self._late_noted = False
        self._tree: list[str] | None = None
        # The platform's own file list, read only when the archive misses a
        # path: `git archive` leaves out `export-ignore` paths and symlinks.
        self._api_paths: asyncio.Future[set[str] | None] | None = None
        self._closed = False

    def _repo_key(self) -> tuple[str, str, str]:
        return (
            str(getattr(self._pr_info, "platform", "github")),
            self._pr_info.owner,
            self._pr_info.repo,
        )

    def start(self) -> None:
        """Begin the archive download now, if it is going to be used at all."""
        if self._task is not None or not self._use_snapshot or self._closed:
            return
        if _no_snapshot_until.get(self._repo_key(), 0.0) > time.monotonic():
            return
        self._started_at = time.monotonic()
        self._task = asyncio.create_task(self._load())

    async def _load(self) -> RepoSnapshot | None:
        from mira.platforms.fetch import RepoSnapshot

        try:
            snapshot = await asyncio.wait_for(
                self._provider.get_repo_snapshot(
                    self._pr_info, self._ref, max_bytes=self._max_bytes
                ),
                self._timeout,
            )
            # Anything else (a stand-in provider's placeholder) is no snapshot:
            # read as one, its empty path list would say no file exists.
            if isinstance(snapshot, RepoSnapshot):
                _no_snapshot_until.pop(self._repo_key(), None)
                return snapshot
            if snapshot is None:
                _no_snapshot_until[self._repo_key()] = time.monotonic() + _SNAPSHOT_RETRY_AFTER
            return None
        except TimeoutError:
            _no_snapshot_until[self._repo_key()] = time.monotonic() + _SNAPSHOT_RETRY_AFTER
            logger.info(
                "Snapshot of %s/%s took over %.0fs; reading files one by one",
                self._pr_info.owner,
                self._pr_info.repo,
                self._timeout,
            )
        except Exception as exc:  # noqa: BLE001 — a failed snapshot only costs speed
            logger.info("Snapshot unavailable, reading files one by one: %s", exc)
        return None

    async def snapshot(self, max_wait: float | None = None) -> RepoSnapshot | None:
        """The archive once it has landed, or None when there is none to be had.

        ``max_wait`` bounds the wait from now; the download itself goes on.
        """
        self.start()
        task = self._task
        if task is None:
            return None
        try:
            if max_wait is None:
                return await asyncio.shield(task)
            if max_wait <= 0 and not task.done():
                return None
            return await asyncio.wait_for(asyncio.shield(task), max(max_wait, 0.0))
        except asyncio.CancelledError:
            # The download was cancelled by `aclose`, not this reader: no archive.
            current = asyncio.current_task()
            if task.cancelled() and not (current and current.cancelling()):
                return None
            raise
        except Exception:  # noqa: BLE001 — a timeout included: no archive yet
            return None

    async def _for_read(self) -> RepoSnapshot | None:
        """The archive, if it lands within the read deadline."""
        self.start()
        if self._task is None:
            return None
        remaining = self._read_wait - (time.monotonic() - self._started_at)
        snap = await self.snapshot(max_wait=remaining)
        still_downloading = self._task is not None and not self._task.done()
        if snap is None and still_downloading and not self._late_noted:
            self._late_noted = True
            logger.info(
                "Snapshot of %s/%s still downloading after %.0fs; reading files one "
                "by one meanwhile",
                self._pr_info.owner,
                self._pr_info.repo,
                self._read_wait,
            )
        return snap

    def loaded(self) -> RepoSnapshot | None:
        """The archive if it has already landed, without waiting for it."""
        if self._task is None or not self._task.done() or self._task.cancelled():
            return None
        if self._task.exception() is not None:
            return None
        return self._task.result()

    async def fetch(self, path: str) -> str | None:
        snap = await self._for_read()
        if snap is not None:
            if path in snap.files:
                return snap.files[path]
            if path not in snap.paths:
                listed = await self._platform_paths()
                if listed is not None and path not in listed:
                    # Not in the commit at all: the API would say the same, slower.
                    return None
            # In the commit but held back (size, vendored, binary). The API
            # still answers for a text file that was only too big to keep.
        return await self._api.fetch(path)

    async def _platform_paths(self) -> set[str] | None:
        """The platform's file list, read once and only on an archive miss.

        None when the platform would not say: that is "unknown", and an
        unknown path is read from the API rather than declared missing.
        """
        if self._api_paths is None:

            async def _read() -> set[str] | None:
                try:
                    fetched = await self._provider.get_repo_tree(self._pr_info, self._ref)
                except Exception as exc:  # noqa: BLE001
                    logger.debug("Repo tree fetch failed: %s", exc)
                    return None
                if not isinstance(fetched, (list, set, tuple)) or not fetched:
                    return None
                return {p for p in fetched if isinstance(p, str)}

            self._api_paths = asyncio.ensure_future(_read())
        return await asyncio.shield(self._api_paths)

    async def tree(self) -> list[str]:
        """Every file path at the reviewed commit."""
        if self._tree is not None:
            return self._tree
        snap = await self._for_read()
        if snap is not None:
            self._tree = sorted(snap.paths)
            return self._tree
        tree: list[str] = []
        if hasattr(self._provider, "get_repo_tree"):
            try:
                fetched = await self._provider.get_repo_tree(self._pr_info, self._ref)
                if isinstance(fetched, (list, set, tuple)):
                    tree = sorted(p for p in fetched if isinstance(p, str))
            except Exception as exc:  # noqa: BLE001
                logger.debug("Repo tree fetch failed: %s", exc)
        self._tree = tree
        return tree

    async def aclose(self) -> None:
        """Drop the download if it is still running; the review is over."""
        self._closed = True
        task, self._task = self._task, None
        if task is not None and not task.done():
            task.cancel()
            # gather hands the download's own cancellation back as a value, so
            # only a cancellation of the caller propagates from here.
            await asyncio.gather(task, return_exceptions=True)


async def build_code_context(
    changed_paths: list[str],
    store: IndexStore,
    token_budget: int = _DEFAULT_TOKEN_BUDGET,
    source_fetcher: SourceFetcher | None = None,
    include_changed_source: bool = True,
) -> str:
    """Build a compact codebase context block for the review prompt.

    Queries the index for summaries of changed files, their imports,
    parent directories, and the blast radius. When a source_fetcher is
    provided, fetches actual source code of affected functions.

    Token budget is split into tiers:
    - 60% real source code (when source_fetcher is available)
    - 30% summaries
    - 10% directory structure

    ``include_changed_source=False`` leaves the changed files' own symbols out
    of the source tier, for a caller that shows each review part its changed
    functions itself; the tier then goes to the code that depends on them.

    Returns a formatted string ready to inject into the review prompt.
    """
    char_budget = token_budget * _CHARS_PER_TOKEN
    parts: list[str] = []
    parts.append("## Codebase Context\n")

    # Rank changed files by inbound edge count so the most-depended-on files
    # get priority in the token budget.
    try:
        edge_counts = store.get_inbound_edge_counts(changed_paths)
        changed_paths = sorted(changed_paths, key=lambda p: edge_counts.get(p, 0), reverse=True)
    except Exception:
        pass  # Fall through with original order

    # Calculate tier budgets
    if source_fetcher:
        source_budget = int(char_budget * 0.60)
        summary_budget = int(char_budget * 0.30)
        dir_budget = int(char_budget * 0.10)
    else:
        source_budget = 0
        summary_budget = int(char_budget * 0.90)
        dir_budget = int(char_budget * 0.10)

    # Track which files have real source shown (excluded from summaries)
    files_with_source: set[str] = set()

    # 1. Directory summaries (10% budget)
    dir_parts: list[str] = []
    # Repository paths always use POSIX separators, even when Mira itself runs
    # on Windows. Path would turn these into backslashes and miss indexed
    # directory summaries on Windows hosts.
    parent_dirs = sorted(
        {
            str(PurePosixPath(path).parent)
            for path in changed_paths
            if str(PurePosixPath(path).parent) != "."
        }
    )
    if parent_dirs:
        dir_summaries = store.get_directory_summaries(parent_dirs)
        if dir_summaries:
            dir_parts.append("### Repository Structure")
            for dir_path in sorted(dir_summaries):
                ds = dir_summaries[dir_path]
                dir_parts.append(f"- `{ds.path}/`: {ds.summary} ({ds.file_count} files)")
            dir_parts.append("")

    dir_text = "\n".join(dir_parts)
    if len(dir_text) > dir_budget:
        dir_text = dir_text[:dir_budget]
        last_nl = dir_text.rfind("\n")
        if last_nl > 0:
            dir_text = dir_text[:last_nl]
    if dir_text.strip():
        parts.append(dir_text)

    # 2. Real source code (60% budget, when source_fetcher available)
    if source_fetcher and source_budget > 0:
        source_parts: list[str] = []
        source_chars_used = 0

        # Get blast radius to know which files + symbols are affected
        blast_radius = store.get_blast_radius(changed_paths)
        blast_by_path = {e.path: e for e in blast_radius}

        # Fetch source for changed files, then blast radius ranked by
        # number of affected symbols (most impacted first)
        all_source_paths = list(changed_paths) if include_changed_source else []
        blast_sorted = sorted(blast_radius, key=lambda e: len(e.affected_symbols), reverse=True)
        for entry in blast_sorted:
            if entry.path not in all_source_paths and entry.path not in changed_paths:
                all_source_paths.append(entry.path)

        source_parts.append("### Source Code")
        source_parts.append("")

        # Read the likely candidates together rather than one round trip at a
        # time; the budget usually runs out within the first dozen files.
        prefetch = all_source_paths[:12]
        fetched = await asyncio.gather(
            *(source_fetcher.fetch(path) for path in prefetch), return_exceptions=True
        )
        sources: dict[str, object] = dict(zip(prefetch, fetched, strict=True))

        for path in all_source_paths:
            if source_chars_used >= source_budget:
                break

            try:
                source = sources[path] if path in sources else await source_fetcher.fetch(path)
            except Exception as exc:
                logger.debug("Source fetch failed for %s: %s", path, exc)
                continue
            if not isinstance(source, str) or not source:
                continue

            # Detect language from file extension
            ext = Path(path).suffix.lstrip(".")
            lang = _ext_to_language(ext)

            # For blast-radius files, only extract affected symbols
            blast_entry = blast_by_path.get(path)
            if blast_entry and path not in changed_paths:
                symbol_parts: list[str] = []
                for sym_name in blast_entry.affected_symbols:
                    span = find_symbol_by_name(source, lang, sym_name)
                    if span:
                        symbol_parts.append(
                            f"#### `{path}` — `{sym_name}` (lines {span.start_line}-{span.end_line})"
                        )
                        symbol_parts.append(f"```{lang}")
                        symbol_parts.append(span.source)
                        symbol_parts.append("```")
                        symbol_parts.append("")
                if symbol_parts:
                    block = "\n".join(symbol_parts)
                    if source_chars_used + len(block) <= source_budget:
                        source_parts.extend(symbol_parts)
                        source_chars_used += len(block)
                        files_with_source.add(path)
            else:
                # Changed files: extract all symbols
                symbols = extract_symbols(source, lang)
                if symbols:
                    for span in symbols:
                        block_lines = [
                            f"#### `{path}` — `{span.name}` (lines {span.start_line}-{span.end_line})",
                            f"```{lang}",
                            span.source,
                            "```",
                            "",
                        ]
                        block = "\n".join(block_lines)
                        if source_chars_used + len(block) > source_budget:
                            break
                        source_parts.extend(block_lines)
                        source_chars_used += len(block)
                    files_with_source.add(path)

        if source_chars_used > 0:
            parts.extend(source_parts)

    # 3. Changed files with full summary + symbol list (30% budget)
    summary_parts: list[str] = []
    changed_summaries = store.get_summaries(changed_paths)
    if changed_summaries:
        summary_parts.append("### Changed Files")
        for path in sorted(changed_summaries):
            if path in files_with_source:
                continue  # Dedup: already shown as source
            fs = changed_summaries[path]
            summary_parts.append(f"- `{fs.path}`: {fs.summary}")
            for sym in fs.symbols:
                summary_parts.append(f"  - `{sym.signature}`: {sym.description}")
            if fs.imports:
                imports_str = ", ".join(fs.imports)
                summary_parts.append(f"  - Imports: {imports_str}")
        summary_parts.append("")

    # Related files (imported by changed files)
    import_paths: set[str] = set()
    for path in changed_paths:
        changed_fs = changed_summaries.get(path)
        if changed_fs:
            import_paths.update(changed_fs.imports)
    import_paths -= set(changed_paths)
    import_paths -= files_with_source  # Dedup

    if import_paths:
        import_summaries = store.get_summaries(list(import_paths))
        if import_summaries:
            summary_parts.append("### Related Files (imported by changed files)")
            for path in sorted(import_summaries):
                fs = import_summaries[path]
                summary_parts.append(f"- `{fs.path}`: {fs.summary}")
                for sym in fs.symbols:
                    summary_parts.append(f"  - `{sym.signature}`: {sym.description}")
            summary_parts.append("")

    summary_text = "\n".join(summary_parts)
    if len(summary_text) > summary_budget:
        summary_text = summary_text[:summary_budget]
        last_nl = summary_text.rfind("\n")
        if last_nl > 0:
            summary_text = summary_text[:last_nl]
    if summary_text.strip():
        parts.append(summary_text)

    # 4. Blast radius (from remaining summary budget)
    blast_radius = store.get_blast_radius(changed_paths)
    blast_parts: list[str] = []
    if blast_radius:
        blast_parts.append("### Blast Radius (code that depends on changed files)")
        for entry in blast_radius:
            if entry.path in files_with_source:
                continue  # Already shown as source
            symbols_str = ", ".join(f"`{s}()`" for s in entry.affected_symbols)
            depth_label = f"depth {entry.depth}"
            blast_parts.append(f"- `{entry.path}` \u2192 calls {symbols_str} ({depth_label})")
        blast_parts.append("")

    if blast_parts:
        parts.extend(blast_parts)

    result = "\n".join(parts)

    # Final truncation safeguard
    if len(result) > char_budget:
        result = result[:char_budget]
        last_nl = result.rfind("\n")
        if last_nl > 0:
            result = result[:last_nl]
        result += "\n\n*(codebase context truncated to fit token budget)*"

    return result


def _ext_to_language(ext: str) -> str:
    """Map file extension to language identifier."""
    mapping = {
        "py": "python",
        "js": "javascript",
        "ts": "typescript",
        "tsx": "typescript",
        "jsx": "javascript",
        "go": "go",
        "rs": "rust",
        "java": "java",
        "rb": "ruby",
        "php": "php",
        "c": "c",
        "cpp": "cpp",
        "h": "c",
        "hpp": "cpp",
        "cs": "cs",
        "swift": "swift",
        "kt": "kotlin",
        "scala": "scala",
        "lua": "lua",
    }
    return mapping.get(ext.lower(), ext.lower())
