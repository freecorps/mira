"""Tools the reviewer LLM can call during review (`read_file`, `grep_repo`).

On unindexed repos the tools cover what JIT pre-fetch can't reach
(Java/Go import resolution); on indexed repos they let the model trace
callers and dispatch points beyond the pre-fetched index context.

This module owns the tool *schemas* and a per-review *executor* that
dispatches calls, caches results, and hard-caps total output. The agentic
loop itself lives in ``passes.py:agentic_review_loop``.
"""

from __future__ import annotations

import asyncio
import fnmatch
import logging
import re
from dataclasses import dataclass, field

from mira.index.context import SourceFetcher

logger = logging.getLogger(__name__)


READ_FILE_TOOL = {
    "type": "function",
    "function": {
        "name": "read_file",
        "description": (
            "Read a file from the repository at the PR head. Use this to verify "
            "cross-file claims (does function X exist? what does the caller pass?) "
            "before filing a comment. Prefer reading specific files over guessing. "
            "Returns the content with line numbers. A long file is cut off; pass "
            "`start_line`/`end_line` (for instance around a `grep_repo` hit) to "
            "read the part you need."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": (
                        "Repo-relative path of the file to read, e.g. `src/auth/middleware.py`."
                    ),
                },
                "start_line": {
                    "type": ["integer", "null"],
                    "description": "First line to return (1-based). Omit to start at the top.",
                },
                "end_line": {
                    "type": ["integer", "null"],
                    "description": "Last line to return, inclusive. Omit to read on from start_line.",
                },
            },
            "required": ["path"],
        },
    },
}


GREP_REPO_TOOL = {
    "type": "function",
    "function": {
        "name": "grep_repo",
        "description": (
            "Search the repo for files whose path or content matches a pattern. "
            "Use this to find where a symbol is defined or called when you don't "
            "already know the file path. Returns up to 30 matching paths (path "
            "search) or up to 30 line-level hits (content search), each with its "
            "line number. The result says how many files were searched, so a miss "
            "over the whole repository means the pattern really is absent."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "pattern": {
                    "type": "string",
                    "description": (
                        "What to look for. Treated as a regex against file content "
                        "unless `path_only` is true. Keep it specific — short or "
                        "common patterns return too many matches and get capped."
                    ),
                },
                "path_glob": {
                    "type": ["string", "null"],
                    "description": (
                        "Optional glob to restrict the search, e.g. `**/*.java` "
                        "or `src/auth/*`. Defaults to all files."
                    ),
                },
                "path_only": {
                    "type": ["boolean", "null"],
                    "description": (
                        "If true, only match paths (don't read file contents). "
                        "Faster and cheaper. Use when you're hunting for a file "
                        "by name."
                    ),
                },
            },
            "required": ["pattern"],
        },
    },
}


# Hard caps to keep tool output from blowing up the prompt or the API budget.
_MAX_FILE_BYTES = 12_000  # ~3k tokens of source; truncate larger files
_MAX_RANGE_LINES = 300  # one ranged read
_MAX_GREP_HITS = 30
_MAX_GREP_BYTES_PER_HIT = 240
_MAX_TOTAL_OUTPUT_BYTES = 50_000  # all tool calls in one review combined
# How long a search waits for the review's archive before searching file by file.
_GREP_SNAPSHOT_WAIT = 30.0

# Never searched: generated, vendored and binary content.
_GREP_SKIP_EXTS = (
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".pdf",
    ".zip",
    ".gz",
    ".tar",
    ".woff",
    ".woff2",
    ".ttf",
    ".eot",
    ".ico",
    ".svg",
    ".map",
    ".lock",
)
_GREP_SKIP_DIRS = ("node_modules/", "vendor/", "dist/", "build/", ".git/")
# Scanned last, so a hit in the code under review is not crowded out of the
# 30-hit cap by the tests and docs that mention it.
_GREP_LATE_MARKERS = ("test", "spec", "docs/", ".md", "fixture", "mock", "__snapshots__")


def _in_skipped_dir(path: str) -> bool:
    """A whole path segment names a skipped directory (`src/myvendor/` is kept)."""
    return any(path.startswith(d) or f"/{d}" in path for d in _GREP_SKIP_DIRS)


def _grep_order(path: str) -> tuple[int, str]:
    lower = path.lower()
    return (1 if any(m in lower for m in _GREP_LATE_MARKERS) else 0, path)


def _line_arg(value: object) -> int | None:
    """A line-number argument as the model sent it (int, numeric string or null)."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    return None


def _grep_files(
    files: dict[str, str], rx: re.Pattern[str], candidates: list[str]
) -> tuple[list[str], int]:
    """Line hits for ``rx`` across ``candidates`` read from ``files``. CPU-bound."""
    hits: list[str] = []
    scanned = 0
    for cand in candidates:
        content = files.get(cand)
        if not content:
            continue
        scanned += 1
        # One C-level search per file skips the line split for the (many)
        # files that do not match at all.
        if rx.search(content) is None:
            continue
        for lineno, line in enumerate(content.split("\n"), start=1):
            if rx.search(line):
                snippet = line.strip()
                if len(snippet) > _MAX_GREP_BYTES_PER_HIT:
                    snippet = snippet[:_MAX_GREP_BYTES_PER_HIT] + "…"
                hits.append(f"{cand}:{lineno}: {snippet}")
                if len(hits) >= _MAX_GREP_HITS:
                    return hits, scanned
    return hits, scanned


@dataclass
class AgenticToolExecutor:
    """Per-review tool dispatcher with caching and an output-size budget.

    Construct one per chunk review. The `repo_tree` is captured once
    (already fetched for JIT) so `grep_repo` doesn't keep re-listing the
    repo. The `source_fetcher` already has its own per-path cache, so
    repeated `read_file` calls don't re-hit GitHub.
    """

    source_fetcher: SourceFetcher
    repo_tree: list[str]
    bytes_used: int = 0
    _content_cache: dict[str, str | None] = field(default_factory=dict)
    call_log: list[dict] = field(default_factory=list)
    _tree_set: set[str] = field(init=False, repr=False)

    def __post_init__(self) -> None:
        # Membership is checked on every read; the tree can be tens of
        # thousands of paths.
        self._tree_set = set(self.repo_tree)

    async def execute(self, name: str, args: dict) -> str:
        """Dispatch a tool call. Returns the tool result as a string.

        Always returns *something* — errors are reported back to the LLM
        rather than raised, so the model can recover (e.g. by trying a
        different path). The string is what gets fed back into the next
        LLM hop as a `tool` message.
        """
        arg = (args or {}).get("path") or (args or {}).get("pattern") or ""
        self.call_log.append({"tool": name, "arg": arg})

        if self.bytes_used >= _MAX_TOTAL_OUTPUT_BYTES:
            return "[tool budget exhausted — no more tool calls accepted; submit your review now]"

        try:
            if name == "read_file":
                path = (args or {}).get("path") or ""
                if not path:
                    return "[error: missing `path` argument]"
                result = await self._read_file(
                    path,
                    _line_arg((args or {}).get("start_line")),
                    _line_arg((args or {}).get("end_line")),
                )
            elif name == "grep_repo":
                pattern = (args or {}).get("pattern") or ""
                if not pattern:
                    return "[error: missing `pattern` argument]"
                path_glob = (args or {}).get("path_glob") or None
                path_only = bool((args or {}).get("path_only") or False)
                result = await self._grep_repo(pattern, path_glob, path_only)
            else:
                return f"[error: unknown tool `{name}`]"
        except Exception as exc:
            logger.debug("Tool %s failed: %s", name, exc)
            return f"[error executing `{name}`: {exc}]"

        self.bytes_used += len(result)
        return result

    async def _read_file(
        self, path: str, start_line: int | None = None, end_line: int | None = None
    ) -> str:
        # Don't waste tokens hunting for files we know don't exist.
        if self.repo_tree and path not in self._tree_set:
            name = path.rsplit("/", 1)[-1].lower()
            close = [p for p in self.repo_tree if path.lower() in p.lower()][:5] or [
                p for p in self.repo_tree if p.lower().endswith("/" + name)
            ][:5]
            hint = f" Did you mean: {', '.join(close)}?" if close else ""
            return f"[file not found at PR head: `{path}`.{hint}]"

        if path in self._content_cache:
            content = self._content_cache[path]
        else:
            content = await self.source_fetcher.fetch(path)
            self._content_cache[path] = content

        if content is None:
            return f"[failed to read `{path}`]"

        lines = content.split("\n")
        if start_line is not None or end_line is not None:
            first = max(1, start_line or 1)
            if first > len(lines):
                return f"[`{path}` has {len(lines)} lines; start_line {first} is past the end]"
            last = min(len(lines), end_line or first + _MAX_RANGE_LINES - 1)
            last = min(max(first, last), first + _MAX_RANGE_LINES - 1)
            numbered = "\n".join(f"{n:>5}  {lines[n - 1]}" for n in range(first, last + 1))
            if len(numbered) > _MAX_FILE_BYTES:
                # A ranged read of minified code can be one enormous line.
                numbered = numbered[:_MAX_FILE_BYTES] + "… [cut: the range is too long]"
            more = (
                f"\n... [{len(lines) - last} more lines; read on with start_line={last + 1}]"
                if last < len(lines)
                else ""
            )
            return f"`{path}` (lines {first}-{last} of {len(lines)}):\n```\n{numbered}{more}\n```"

        truncated = False
        if len(content) > _MAX_FILE_BYTES:
            content = content[:_MAX_FILE_BYTES]
            truncated = True

        # Number lines so the LLM can cite them by number when filing comments.
        shown = content.split("\n")
        numbered = "\n".join(f"{i + 1:>5}  {line}" for i, line in enumerate(shown))
        suffix = (
            f"\n... [truncated at line {len(shown)} of {len(lines)}; pass start_line and "
            "end_line to read further]"
            if truncated
            else ""
        )
        return f"`{path}`:\n```\n{numbered}{suffix}\n```"

    async def _grep_repo(self, pattern: str, path_glob: str | None, path_only: bool) -> str:
        if not self.repo_tree:
            return "[grep unavailable: repo tree not loaded]"

        candidates: list[str]
        if path_glob:
            candidates = [p for p in self.repo_tree if fnmatch.fnmatch(p, path_glob)]
        else:
            candidates = list(self.repo_tree)

        if path_only:
            try:
                rx = re.compile(pattern)
            except re.error:
                # Treat as substring on regex error.
                path_hits = [p for p in candidates if pattern in p][:_MAX_GREP_HITS]
            else:
                path_hits = [p for p in candidates if rx.search(p)][:_MAX_GREP_HITS]
            if not path_hits:
                return f"[no path matches for `{pattern}`]"
            return "Path matches:\n" + "\n".join(f"- `{p}`" for p in path_hits)

        try:
            # MULTILINE: the whole-file pre-check in `_grep_files` must let `^`
            # and `$` match at line boundaries, as the per-line search does.
            rx = re.compile(pattern, re.MULTILINE)
        except re.error as exc:
            return f"[invalid regex `{pattern}`: {exc}]"

        candidates = sorted(
            (c for c in candidates if not c.endswith(_GREP_SKIP_EXTS) and not _in_skipped_dir(c)),
            key=_grep_order,
        )

        # The whole repository, when the review has it in memory: the search
        # then answers for every file, and a miss is a real miss. An archive
        # still downloading is worth a short wait for that.
        loaded = getattr(self.source_fetcher, "loaded", None)
        snapshot = loaded() if callable(loaded) else None
        waiter = getattr(self.source_fetcher, "snapshot", None)
        if snapshot is None and callable(waiter):
            try:
                snapshot = await waiter(max_wait=_GREP_SNAPSHOT_WAIT)
            except Exception:  # noqa: BLE001 — search file by file instead
                snapshot = None
        files = getattr(snapshot, "files", None)
        if isinstance(files, dict) and files:
            hits, scanned = await asyncio.to_thread(_grep_files, files, rx, candidates)
            # Files the tree lists but the archive left out (`export-ignore`,
            # symlinks) were not searched either.
            archived = getattr(snapshot, "paths", None) or set()
            oversized = getattr(snapshot, "oversized", None) or set()
            missing = sum(
                1 for c in candidates if (archived and c not in archived) or c in oversized
            )
            if getattr(snapshot, "partial", False) or missing:
                # Say so rather than let a miss read as proof of absence.
                scope = f"{scanned} files; the rest of the repository could not be searched"
                if not hits:
                    return f"[no content matches for `{pattern}` in the {scope}]"
                prefix = f"Content matches (searched {scope}):"
            elif not hits:
                return (
                    f"[no content matches for `{pattern}` in any of the {scanned} files "
                    "searched — the whole repository]"
                )
            else:
                prefix = f"Content matches (searched all {scanned} files):"
            if len(hits) >= _MAX_GREP_HITS:
                prefix += f" showing the first {_MAX_GREP_HITS} — narrow with path_glob"
            return prefix + "\n" + "\n".join(hits)

        hits = []
        files_scanned = 0
        # Each scanned file is a network round-trip via source_fetcher; keep tight.
        max_files_to_scan = 15

        for cand in candidates:
            if len(hits) >= _MAX_GREP_HITS or files_scanned >= max_files_to_scan:
                break

            files_scanned += 1
            content = self._content_cache.get(cand)
            if content is None and cand not in self._content_cache:
                content = await self.source_fetcher.fetch(cand)
                self._content_cache[cand] = content
            if not content:
                continue

            for lineno, line in enumerate(content.split("\n"), start=1):
                if rx.search(line):
                    snippet = line.strip()
                    if len(snippet) > _MAX_GREP_BYTES_PER_HIT:
                        snippet = snippet[:_MAX_GREP_BYTES_PER_HIT] + "…"
                    hits.append(f"{cand}:{lineno}: {snippet}")
                    if len(hits) >= _MAX_GREP_HITS:
                        break

        partial = (
            f" — only {files_scanned} of {len(candidates)} files could be searched, so an "
            "absence here proves nothing; narrow with path_glob"
            if files_scanned < len(candidates)
            else ""
        )
        if not hits:
            return f"[no content matches for `{pattern}` (scanned {files_scanned} files){partial}]"
        prefix = f"Content matches (scanned {files_scanned} files{partial}):"
        if len(hits) == _MAX_GREP_HITS:
            prefix += f" showing first {_MAX_GREP_HITS}"
        return prefix + "\n" + "\n".join(hits)


AGENTIC_TOOLS = [READ_FILE_TOOL, GREP_REPO_TOOL]
