"""Tools the reviewer LLM can call during review.

`read_file` and `grep_repo` read and search the repository at the PR head;
`find_usages` and `find_definition` answer from the code graph
(``index/code_graph.py``); `grep_index` searches the repository index's file
and symbol summaries, when the repository has been indexed.

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
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from mira.index.code_graph import (
    CodeGraph,
    graph_searchable,
    match_path,
    name_pattern,
    repo_snapshot,
    select_candidates,
)
from mira.index.context import SourceFetcher

if TYPE_CHECKING:
    from mira.index._store_shared import IndexMatch

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


FIND_USAGES_TOOL = {
    "type": "function",
    "function": {
        "name": "find_usages",
        "description": (
            "Find the call sites of a function, method or class across the repository, "
            "from parsed syntax trees: each hit is `path:line`, the function it sits "
            "in, and the line. Use it to check that every caller of a symbol whose "
            "signature or behaviour changed still matches — better than grep_repo for "
            "this, because comments, strings and same-prefixed names are not calls. "
            "Calls are matched by name, so `obj.save()` counts for every `save`; "
            "other mentions (imports, a function passed as a value) are listed after."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "symbol": {
                    "type": "string",
                    "description": (
                        "The name to look up, e.g. `authenticate` or `SessionStore.save` "
                        "(only the last part is matched against calls)."
                    ),
                },
                "path": {
                    "type": ["string", "null"],
                    "description": (
                        "Optional directory, file or glob to search in, e.g. `src/api/` "
                        "or `**/*.ts`. Defaults to the whole repository."
                    ),
                },
            },
            "required": ["symbol"],
        },
    },
}


FIND_DEFINITION_TOOL = {
    "type": "function",
    "function": {
        "name": "find_definition",
        "description": (
            "Find where a function, method or class is defined: its file, line span, "
            "signature and the first lines of its body. Use it to check what a called "
            "function actually accepts, returns or raises without knowing its file."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "symbol": {
                    "type": "string",
                    "description": (
                        "The name, plain (`parse_config`) or qualified with its class "
                        "or receiver (`Config.parse`, `Server.Handle`)."
                    ),
                },
            },
            "required": ["symbol"],
        },
    },
}


GREP_INDEX_TOOL = {
    "type": "function",
    "function": {
        "name": "grep_index",
        "description": (
            "Search the repository's index — a summary of every file and its "
            "symbols — by keywords, to answer *which file handles X?* when you "
            "don't know a name to grep for (e.g. `rate limiting`, `webhook "
            "signature`). Returns the best-matching files with their summaries "
            "and matching symbols; read the file to confirm."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "A few keywords describing what you are looking for.",
                },
            },
            "required": ["query"],
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
# Code-graph lookups: files parsed per call (only those that mention the
# name), the largest parsed, text parsed per call, and seconds of parsing.
_GRAPH_MAX_FILES = 300
_GRAPH_MAX_FILE_BYTES = 300_000
_GRAPH_MAX_TOTAL_BYTES = 16 * 1024 * 1024
_GRAPH_TIME_BUDGET = 15.0
_MAX_DEFINITIONS = 5
_DEFINITION_EXCERPT_LINES = 15
_MAX_INDEX_HITS = 12
_SYMBOL_RE = re.compile(r"^[A-Za-z_$][\w$]*(?:(?:\.|::)[A-Za-z_$][\w$]*)*$")

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

    `find_usages` / `find_definition` read the code graph (`code_graph`,
    shared by the review's parts so a file is parsed once per review); with
    `graph_tools` off they are not offered. `grep_index` is offered only with
    an `index_search` — a repository that has not been indexed has nothing
    for it to search.
    """

    source_fetcher: SourceFetcher
    repo_tree: list[str]
    bytes_used: int = 0
    _content_cache: dict[str, str | None] = field(default_factory=dict)
    call_log: list[dict] = field(default_factory=list)
    graph_tools: bool = True
    code_graph: CodeGraph | None = None
    # (query, limit) -> ranked index matches; blocking, run in a thread.
    index_search: Callable[[str, int], list[IndexMatch]] | None = None
    _tree_set: set[str] = field(init=False, repr=False)
    _result_cache: dict[tuple[str, str], str] = field(default_factory=dict, repr=False)

    def __post_init__(self) -> None:
        # Membership is checked on every read; the tree can be tens of
        # thousands of paths.
        self._tree_set = set(self.repo_tree)

    @property
    def tools(self) -> list[dict]:
        """The tool schemas this executor can run, for the agentic loop to offer."""
        tools = [READ_FILE_TOOL, GREP_REPO_TOOL]
        if self.graph_tools:
            tools += [FIND_USAGES_TOOL, FIND_DEFINITION_TOOL]
        if self.index_search is not None:
            tools.append(GREP_INDEX_TOOL)
        return tools

    def _graph(self) -> CodeGraph:
        if self.code_graph is None:
            self.code_graph = CodeGraph()
        return self.code_graph

    async def execute(self, name: str, args: dict) -> str:
        """Dispatch a tool call. Returns the tool result as a string.

        Always returns *something* — errors are reported back to the LLM
        rather than raised, so the model can recover (e.g. by trying a
        different path). The string is what gets fed back into the next
        LLM hop as a `tool` message.
        """
        args = args if isinstance(args, dict) else {}
        arg = next(
            (str(args[k]) for k in ("symbol", "query", "path", "pattern") if args.get(k)), ""
        )
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
            elif name in ("find_usages", "find_definition") and self.graph_tools:
                symbol = str(args.get("symbol") or "").strip()
                if not symbol:
                    return "[error: missing `symbol` argument]"
                if not _SYMBOL_RE.match(symbol):
                    return (
                        f"[error: `{symbol[:80]}` is not a symbol name; use grep_repo for patterns]"
                    )
                scope = str(args.get("path") or "").strip() or None
                key = (name, f"{symbol}\0{scope or ''}")
                cached = self._result_cache.get(key)
                if cached is None:
                    if name == "find_usages":
                        cached = await self._find_usages(symbol, scope)
                    else:
                        cached = await self._find_definition(symbol)
                    self._result_cache[key] = cached
                result = cached
            elif name == "grep_index" and self.index_search is not None:
                query = str(args.get("query") or "").strip()
                if not query:
                    return "[error: missing `query` argument]"
                key = (name, query.lower())
                cached = self._result_cache.get(key)
                if cached is None:
                    cached = await self._grep_index(query)
                    self._result_cache[key] = cached
                result = cached
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

    async def _grep_repo(
        self, pattern: str, path_glob: str | None, path_only: bool, *, scope: str | None = None
    ) -> str:
        """``scope`` narrows like the graph tools' ``path``: a glob, a directory or a file."""
        if not self.repo_tree:
            return "[grep unavailable: repo tree not loaded]"

        candidates: list[str]
        if path_glob:
            candidates = [p for p in self.repo_tree if fnmatch.fnmatch(p, path_glob)]
        else:
            candidates = list(self.repo_tree)
        if scope:
            candidates = [p for p in candidates if match_path(p, scope)]

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

    async def _graph_candidates(
        self, name: str, scope: str | None, rank: Callable[[str], tuple] | None = None
    ) -> tuple[list[tuple[str, str]], bool] | None:
        """Parsed files that mention ``name``, or None without the repository snapshot.

        Only paths in the PR head's tree are considered, so a ``scope`` the
        model made up selects nothing rather than reading outside the tree.
        """
        snapshot = await repo_snapshot(self.source_fetcher, _GREP_SNAPSHOT_WAIT)
        rx = name_pattern([name])
        if snapshot is None or rx is None:
            return None
        pool = [p for p in self.repo_tree if graph_searchable(p) and match_path(p, scope)]
        if rank is not None:
            pool.sort(key=rank)
        candidates, capped = await asyncio.to_thread(
            select_candidates,
            snapshot.files,
            rx,
            pool,
            max_files=_GRAPH_MAX_FILES,
            max_file_bytes=_GRAPH_MAX_FILE_BYTES,
            max_total_bytes=_GRAPH_MAX_TOTAL_BYTES,
            presorted=rank is not None,
        )
        graph = self._graph()
        _, timed_out = await asyncio.to_thread(
            graph.add_files, candidates, time.monotonic() + _GRAPH_TIME_BUDGET
        )
        parsed = [(p, c) for p, c in candidates if p in graph]
        complete = not capped and not timed_out and not getattr(snapshot, "partial", False)
        return parsed, complete

    async def _text_fallback(self, name: str, scope: str | None, why: str) -> str:
        # The same scope rules as the graph search: `src/api` is a directory,
        # not the prefix of `src/apix`.
        found = await self._grep_repo(
            rf"(?<![\w$]){re.escape(name)}(?![\w$])", None, path_only=False, scope=scope
        )
        return f"[{why}; word-boundary text matches instead — comments and strings count]\n{found}"

    async def _find_usages(self, symbol: str, scope: str | None) -> str:
        if not self.repo_tree:
            return "[find_usages unavailable: repo tree not loaded]"
        name = re.split(r"\.|::", symbol)[-1]
        found = await self._graph_candidates(name, scope)
        if found is None:
            return await self._text_fallback(
                name,
                scope,
                "the code graph needs the repository snapshot, which this review has not got",
            )
        candidates, complete = found
        if not candidates:
            searched = "the whole repository" if complete else "the files that could be searched"
            return f"[no usages of `{name}` in {searched}]"
        graph = self._graph()
        rx = name_pattern([name])
        calls: list[str] = []
        mentions: list[str] = []
        defined: list[str] = []
        total_calls = 0
        for path, _content in candidates:
            fg = graph.get(path)
            if fg is None:
                continue
            def_lines = {d.start_line for d in fg.definitions if d.name == name}
            for d in fg.definitions:
                if d.name == name and len(defined) < 3:
                    defined.append(f"`{path}:{d.start_line}` `{_cap(d.signature)}`")
            call_lines: set[int] = set()
            for ref in fg.references:
                if ref.name != name:
                    continue
                total_calls += 1
                call_lines.add(ref.line)
                if len(calls) < _MAX_GREP_HITS:
                    where = f" in `{ref.enclosing}`" if ref.enclosing else ""
                    calls.append(f"{path}:{ref.line}{where}: {_cap(ref.text)}")
            if rx is None or len(mentions) >= _MAX_GREP_HITS // 2:
                continue
            for lineno, line in enumerate(fg.lines, start=1):
                if lineno in call_lines or lineno in def_lines or not rx.search(line):
                    continue
                mentions.append(f"{path}:{lineno}: {_cap(line.strip())}")
                if len(mentions) >= _MAX_GREP_HITS // 2:
                    break
        parsed_with = {fg.backend for p, _ in candidates if (fg := graph.get(p)) is not None}
        how = (
            "syntax trees"
            if parsed_with == {"tree-sitter"}
            else "a regex parse (tree-sitter unavailable)"
            if parsed_with == {"regex"}
            else "syntax trees (regex for some files)"
        )
        scope_note = (
            f"all {len(candidates)} files that mention it"
            if complete
            else f"{len(candidates)} files that mention it — the search was capped, so an "
            "absent caller proves nothing; narrow with `path`"
        )
        out = [f"Usages of `{name}` from {how}, over {scope_note}:"]
        if defined:
            out.append("Defined at: " + "; ".join(defined))
        if calls:
            more = f" (showing {len(calls)} of {total_calls})" if total_calls > len(calls) else ""
            out.append(f"Calls{more}:")
            out.extend(calls)
        else:
            out.append("Calls: none")
        if mentions:
            out.append("Other mentions (imports, values, comments):")
            out.extend(mentions[: max(0, _MAX_GREP_HITS - len(calls)) or 5])
        return "\n".join(out)

    async def _find_definition(self, symbol: str) -> str:
        if not self.repo_tree:
            return "[find_definition unavailable: repo tree not loaded]"
        name = re.split(r"\.|::", symbol)[-1]
        lowered = name.lower()

        def _rank(path: str) -> tuple:
            # A file named after the symbol is the likeliest home; tests last.
            stem = path.rsplit("/", 1)[-1].rsplit(".", 1)[0].lower().replace("_", "")
            return (lowered.replace("_", "") not in stem, *_grep_order(path))

        found = await self._graph_candidates(name, None, rank=_rank)
        if found is None:
            keyword = (
                r"(?:def|class|function|func|fn|interface|struct|enum|trait|type|record)"
                rf"\s+(?:\([^)]*\)\s*)?{re.escape(name)}(?![\w$])"
            )
            found_text = await self._grep_repo(keyword, None, path_only=False)
            return (
                "[the code graph needs the repository snapshot, which this review has not "
                f"got; definition-keyword text matches instead]\n{found_text}"
            )
        candidates, complete = found
        graph = self._graph()
        defs = [
            d
            for d in graph.definitions_of(symbol.replace("::", "."))
            if any(d.path == p for p, _ in candidates)
        ]
        if not defs:
            searched = "the whole repository" if complete else "the files that could be searched"
            return f"[no definition of `{symbol}` found in {searched}]"
        defs.sort(key=lambda d: (_grep_order(d.path), d.start_line))
        out = [f"Definitions of `{symbol}` ({len(defs)} found):"]
        for d in defs[:_MAX_DEFINITIONS]:
            fg = graph.get(d.path)
            lines = fg.lines if fg else []
            last = min(d.end_line, d.start_line + _DEFINITION_EXCERPT_LINES - 1, len(lines))
            body = "\n".join(f"{n:>5}  {lines[n - 1]}" for n in range(d.start_line, last + 1))
            if len(body) > _MAX_FILE_BYTES // _MAX_DEFINITIONS:
                body = body[: _MAX_FILE_BYTES // _MAX_DEFINITIONS] + "…"
            more = (
                f"\n... [{d.end_line - last} more lines; read_file with start_line={last + 1}]"
                if d.end_line > last
                else ""
            )
            out.append(
                f"\n`{d.path}` lines {d.start_line}-{d.end_line} ({d.kind} "
                f"`{d.qualified_name}`): `{_cap(d.signature)}`\n```\n{body}{more}\n```"
            )
        if len(defs) > _MAX_DEFINITIONS:
            out.append(f"\n... and {len(defs) - _MAX_DEFINITIONS} more; qualify the name to narrow")
        return "\n".join(out)

    async def _grep_index(self, query: str) -> str:
        search = self.index_search
        if search is None:
            return "[grep_index unavailable: the repository is not indexed]"
        matches = await asyncio.to_thread(search, query, _MAX_INDEX_HITS)
        if not matches:
            return f"[no indexed files match `{_cap(query)}`; try other words or grep_repo]"
        out = [f"Indexed files matching `{_cap(query)}` (best first):"]
        for m in matches:
            line = f"- `{m.path}`"
            if m.summary:
                line += f" — {_cap(m.summary.strip().splitlines()[0] if m.summary.strip() else '')}"
            if m.symbols:
                line += f" (symbols: {', '.join(m.symbols)})"
            out.append(line)
        return "\n".join(out)


def _cap(text: str) -> str:
    return text if len(text) <= _MAX_GREP_BYTES_PER_HIT else text[:_MAX_GREP_BYTES_PER_HIT] + "…"


# The full set, for a caller whose executor does not say what it offers.
AGENTIC_TOOLS = [READ_FILE_TOOL, GREP_REPO_TOOL, FIND_USAGES_TOOL, FIND_DEFINITION_TOOL]
