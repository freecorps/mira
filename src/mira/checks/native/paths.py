"""Classifying a changed path, once, for every check that asks.

Four checks need to know whether a path is a test, a document, a migration or
generated output, and four private answers to that question would eventually
disagree — the tests check would count a file the docs check ignored, and a
pull request would get contradictory advice from two checks that were supposed
to be looking at the same diff.

The patterns are conservative in one specific direction: a path Mira is not
sure about is *not* claimed. An unrecognised file is source, not a test; a file
that might be generated is not treated as generated. Every check here can only
lose precision by over-claiming, and a check that quietly excused a change
because it mistook a directory name for a test suite is worse than one that
asked a question it did not need to.
"""

from __future__ import annotations

import re
from pathlib import PurePosixPath

# Extensions Mira recognises as code. A check about "did the source change"
# needs a closed set: everything else in a repository is data, configuration or
# prose, and a change to any of those has different obligations.
SOURCE_EXTENSIONS: frozenset[str] = frozenset(
    {
        ".py",
        ".pyi",
        ".js",
        ".jsx",
        ".mjs",
        ".cjs",
        ".ts",
        ".tsx",
        ".go",
        ".rs",
        ".java",
        ".kt",
        ".kts",
        ".rb",
        ".php",
        ".cs",
        ".c",
        ".h",
        ".cc",
        ".cpp",
        ".hpp",
        ".m",
        ".mm",
        ".swift",
        ".scala",
        ".ex",
        ".exs",
        ".erl",
        ".dart",
        ".sh",
        ".bash",
        ".lua",
        ".pl",
        ".r",
        ".jl",
        ".vue",
        ".svelte",
    }
)

DOC_EXTENSIONS: frozenset[str] = frozenset({".md", ".mdx", ".rst", ".txt", ".adoc"})

_TEST_DIRECTORIES = frozenset(
    {"test", "tests", "spec", "specs", "__tests__", "testing", "e2e", "integration_tests"}
)

_TEST_FILENAME = re.compile(
    r"(?:^test_[^/]*|[^/]*_test|[^/]*\.test|[^/]*\.spec|[^/]*Test|[^/]*Tests|[^/]*Spec)$"
)

_DOC_DIRECTORIES = frozenset({"doc", "docs", "documentation", "website", "site", "handbook"})

_GENERATED_NAMES = frozenset(
    {
        "package-lock.json",
        "yarn.lock",
        "pnpm-lock.yaml",
        "poetry.lock",
        "uv.lock",
        "pipfile.lock",
        "gemfile.lock",
        "cargo.lock",
        "composer.lock",
        "go.sum",
    }
)

_GENERATED_MARKERS = ("/dist/", "/build/", "/vendor/", "/node_modules/", "/__generated__/")

_GENERATED_SUFFIXES = (".min.js", ".min.css", ".map", ".pb.go", "_pb2.py", ".g.dart")

_MIGRATION_DIRECTORIES = frozenset({"migrations", "migrate", "migration", "versions", "schema"})

_MIGRATION_SUFFIXES = (".sql",)

# Files whose contents *are* a public interface: what the deployment can
# configure, what the CLI accepts, what an HTTP client can call. A change to
# one of these is a change somebody outside the repository can notice.
_INTERFACE_HINTS = (
    "cli.py",
    "config.py",
    "settings.py",
    "api.py",
    "routes.py",
    "router.py",
    "urls.py",
    "schema.py",
    "openapi.json",
    "openapi.yaml",
    ".env.example",
    "docker-compose.yml",
    "dockerfile",
)


def _parts(path: str) -> tuple[str, ...]:
    return tuple(part.lower() for part in PurePosixPath(path.replace("\\", "/")).parts)


def _stem(path: str) -> str:
    return PurePosixPath(path.replace("\\", "/")).stem


def suffix(path: str) -> str:
    return PurePosixPath(path.replace("\\", "/")).suffix.lower()


def is_test(path: str) -> bool:
    """Whether this path is part of a test suite.

    A directory named ``tests`` counts, and so does a file named for the
    convention of its language. Both, because a repository that keeps
    ``src/foo/foo_test.go`` next to ``src/foo/foo.go`` has a test suite that no
    directory rule would find.
    """
    parts = _parts(path)
    if any(part in _TEST_DIRECTORIES for part in parts):
        return True
    stem = _stem(path)
    return bool(_TEST_FILENAME.match(stem))


def is_doc(path: str) -> bool:
    """Whether this path is documentation."""
    parts = _parts(path)
    if any(part in _DOC_DIRECTORIES for part in parts):
        return True
    if suffix(path) in DOC_EXTENSIONS:
        return True
    name = parts[-1] if parts else ""
    return name.startswith(("readme", "changelog", "contributing", "upgrading", "migrating"))


def is_generated(path: str) -> bool:
    """Whether this path is machine-written output rather than somebody's work."""
    lowered = "/" + path.replace("\\", "/").lower()
    name = lowered.rsplit("/", 1)[-1]
    if name in _GENERATED_NAMES:
        return True
    if any(marker in lowered for marker in _GENERATED_MARKERS):
        return True
    return lowered.endswith(_GENERATED_SUFFIXES)


def is_source(path: str) -> bool:
    """Whether this path is code somebody wrote, in a language Mira knows."""
    if is_generated(path) or is_test(path):
        return False
    return suffix(path) in SOURCE_EXTENSIONS


def is_migration(path: str) -> bool:
    """Whether this path is a schema migration.

    A ``.sql`` file anywhere counts, and so does anything under a migrations
    directory whatever its extension — Alembic writes Python, Rails writes Ruby
    and Prisma writes its own dialect.
    """
    lowered = path.replace("\\", "/").lower()
    if lowered.endswith(_MIGRATION_SUFFIXES):
        return True
    parts = _parts(path)
    return any(part in _MIGRATION_DIRECTORIES for part in parts)


def is_interface(path: str) -> bool:
    """Whether this path defines something outside the repository can depend on."""
    lowered = path.replace("\\", "/").lower()
    name = lowered.rsplit("/", 1)[-1]
    if name in _INTERFACE_HINTS or lowered.endswith(_INTERFACE_HINTS):
        return True
    return "/routers/" in f"/{lowered}" or "/api/" in f"/{lowered}"
