"""Dependency discovery for review ownership, using PR source rather than diff order."""

from __future__ import annotations

import asyncio
import logging
import posixpath
import re
from pathlib import PurePosixPath

from mira.index.context import SourceFetcher
from mira.index.jit_context import extract_import_candidates
from mira.models import FileChangeType, FileDiff

logger = logging.getLogger(__name__)

_JS_IMPORT = re.compile(
    r"""(?:\bfrom\s*|\brequire\s*\(\s*|\bimport\s*(?:\(\s*)?)['\"]([^'\"]+)['\"]"""
)
_EXTENSIONS = (".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".vue", ".svelte")


def _js_paths(base: str) -> list[str]:
    paths = [base]
    # TypeScript's emitted-JS imports refer to TS sources in the repository.
    if base.endswith((".js", ".jsx", ".mjs", ".cjs")):
        paths.extend(str(PurePosixPath(base).with_suffix(ext)) for ext in _EXTENSIONS)
    paths.extend(base + ext for ext in _EXTENSIONS)
    paths.extend(base + "/index" + ext for ext in _EXTENSIONS)
    return paths


def import_paths(source: str, file: FileDiff, paths: set[str]) -> set[str]:
    """Resolve supported imports against known paths; never join ambiguous aliases.

    Common @/ and ~/ aliases are resolved from the nearest ancestor, checking
    its src directory too. Relative imports, re-exports, require(), dynamic
    imports and Python from-package imports are supported without an index.
    Other supported languages reuse Mira's JIT resolver.
    """
    result = set(extract_import_candidates(source, file.language, file.path, paths)) & paths
    if file.language in ("javascript", "typescript") or file.path.endswith(_EXTENSIONS):
        for spec in _JS_IMPORT.findall(source):
            bases: list[str] = []
            parent = PurePosixPath(file.path).parent
            if spec.startswith("."):
                bases = [posixpath.normpath(str(parent / spec))]
            elif spec.startswith(("@/", "~/")):
                for root in (parent, *parent.parents):
                    matches = {
                        candidate
                        for prefix in (root, root / "src")
                        for candidate in _js_paths(str(prefix / spec[2:]))
                        if candidate in paths
                    }
                    if matches:
                        if len(matches) == 1:
                            result.update(matches)
                        break
            for base in bases:
                candidates = [p for p in _js_paths(base) if p in paths]
                if candidates:
                    result.add(candidates[0])
    elif file.language == "python":
        for module, names in re.findall(r"\bfrom\s+([\w.]+)\s+import\s+([^\n]+)", source):
            dots = len(module) - len(module.lstrip("."))
            if dots:
                root = PurePosixPath(file.path).parent
                for _ in range(dots - 1):
                    root = root.parent
                base = str(root / module[dots:].replace(".", "/"))
                bases = [base]
            else:
                base = module.replace(".", "/")
                bases = [base, "src/" + base, "lib/" + base]
            for base in bases:
                candidates = [base + ".py", base + "/__init__.py"]
                for name in re.findall(r"(?:^|[,\(])\s*(\w+)", names):
                    candidates.extend(
                        [base + "/" + name + ".py", base + "/" + name + "/__init__.py"]
                    )
                result.update(p for p in candidates if p in paths)
    return result - {file.path}


async def discover_dependencies(
    files: list[FileDiff],
    source_fetcher: SourceFetcher | None = None,
    indexed_imports: dict[str, list[str]] | None = None,
    concurrency: int = 5,
    repo_tree: set[str] | None = None,
) -> dict[str, set[str]]:
    """Read unchanged imports at the review SHA, falling back to both diff sides.

    Index edges also cover languages/aliases whose resolver is unavailable.
    Fetch failures reduce grouping precision, never review coverage.
    """
    aliases = {f.old_path: f.path for f in files if f.old_path}
    changed = {f.path for f in files}
    paths = changed | set(aliases) | (repo_tree or set())
    sem = asyncio.Semaphore(concurrency)
    reexports: dict[str, asyncio.Task[set[str]]] = {}
    bridge_lock = asyncio.Lock()

    async def fetch_reexports(path: str) -> set[str]:
        # Cache only the fetch, not recursive traversal: cycles cannot make
        # tasks wait on one another. Independent paths share only the semaphore.
        try:
            assert source_fetcher is not None
            async with sem:
                content = await source_fetcher.fetch(path)
            if isinstance(content, str):
                exports = "\n".join(
                    re.findall(
                        r"\bexport\s+(?:type\s+)?(?:\*|\{[^}]*\})\s+from\s+['\"][^'\"]+['\"]",
                        content,
                    )
                )
                return import_paths(
                    exports, FileDiff(path, FileChangeType.MODIFIED, language="typescript"), paths
                )
        except Exception as exc:
            logger.debug("Barrel dependency unavailable for %s: %s", path, exc)
        return set()

    async def bridge(path: str, depth: int = 0) -> set[str]:
        """Follow unchanged JS/TS barrel files, without walking the whole repo."""
        if path in changed or path in aliases:
            return {aliases.get(path, path)}
        if not source_fetcher or depth >= 3 or not path.endswith(_EXTENSIONS):
            return set()
        async with bridge_lock:
            if path not in reexports:
                if len(reexports) >= 64:
                    return set()
                reexports[path] = asyncio.create_task(fetch_reexports(path))
            task = reexports[path]
        edges = await task
        connected = await asyncio.gather(*(bridge(edge, depth + 1) for edge in sorted(edges)))
        return {path for related in connected for path in related}

    async def discover(file: FileDiff) -> tuple[str, set[str]]:
        # Keep old and new import edges: removed dependencies matter in a review.
        source = "\n".join(
            line[1:] if line[:1] in ("+", "-", " ") else line
            for h in file.hunks
            for line in h.content.splitlines()
            if not line.startswith("@@")
        )
        if source_fetcher and file.change_type != FileChangeType.DELETED:
            try:
                async with sem:
                    full_source = await source_fetcher.fetch(file.path)
                if isinstance(full_source, str):
                    source += "\n" + full_source
            except Exception as exc:
                logger.debug("Dependency source unavailable for %s: %s", file.path, exc)
        dependencies = import_paths(source, file, paths)
        for edge in (indexed_imports or {}).get(file.path, []):
            if edge in paths:
                dependencies.add(edge)
        connected = await asyncio.gather(*(bridge(path) for path in sorted(dependencies)))
        resolved = {path for related in connected for path in related}
        return file.path, resolved - {file.path}

    try:
        return dict(await asyncio.gather(*(discover(f) for f in files)))
    finally:
        # Do not leave provider reads running when the review is cancelled.
        for task in reexports.values():
            if not task.done():
                task.cancel()
        await asyncio.gather(*reexports.values(), return_exceptions=True)
