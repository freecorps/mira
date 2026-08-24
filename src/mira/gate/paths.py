"""Path matching for protected and generated files.

Protected paths are the sharpest tool in the gate: a match is an absolute veto,
so the matcher has to be predictable enough that an operator can read a pattern
and know exactly which files it covers. That rules out `fnmatch`, whose `*`
happily crosses directory separators — `secrets/*` would then also protect
`secrets/a/b/c`, which is fine, but `*.pem` would protect `docs/*.pem` too,
which surprises people in the other direction on the day it matters.

So the rules are gitignore-shaped and spelled out:

  ``infra/**``       every path under ``infra/`` (and ``infra`` itself)
  ``infra/``         same thing, written as a directory
  ``*.tf``           a ``.tf`` file in *any* directory
  ``/deploy/*.yaml`` ``.yaml`` files directly in the root ``deploy/``
  ``**/migrations/**`` anything under any ``migrations`` directory

A pattern with no separator matches on the basename, in any directory, which is
the reading almost everyone expects from ``*.pem``. A pattern *with* a separator
is matched against the whole path, anchored at the repository root.
"""

from __future__ import annotations

import re
from functools import lru_cache

# Compiled patterns are reused across every PR of every repository, and the set
# of distinct patterns in an install is tiny (one policy, maybe a few per-repo
# overrides). The cache is bounded so a misconfigured deployment that generates
# patterns per PR cannot grow it without limit.
_CACHE_SIZE = 512


class PatternError(ValueError):
    """A protected/generated path pattern that cannot be compiled.

    Raised at configuration load, never at decision time: a policy the gate
    cannot interpret must fail the deployment, not fail open on a PR.
    """


def normalize(path: str) -> str:
    """Repository-relative POSIX path, without a leading ``./`` or ``/``."""
    cleaned = path.strip().replace("\\", "/")
    while cleaned.startswith("./"):
        cleaned = cleaned[2:]
    return cleaned.lstrip("/")


def _translate(pattern: str) -> str:
    """Turn one glob into an anchored regex source."""
    out: list[str] = []
    i = 0
    length = len(pattern)
    while i < length:
        char = pattern[i]
        if char == "*":
            if pattern.startswith("**", i):
                # `**/` consumes the separator too, so `**/x` also matches `x`.
                if pattern.startswith("**/", i):
                    out.append("(?:[^/]+/)*")
                    i += 3
                    continue
                out.append(".*")
                i += 2
                continue
            out.append("[^/]*")
            i += 1
            continue
        if char == "?":
            out.append("[^/]")
            i += 1
            continue
        if char == "[":
            end = i + 1
            if end < length and pattern[end] in "!^":
                end += 1
            if end < length and pattern[end] == "]":
                end += 1
            while end < length and pattern[end] != "]":
                end += 1
            if end >= length:
                raise PatternError(f"unterminated character class in pattern {pattern!r}")
            body = pattern[i + 1 : end]
            if body[:1] in ("!", "^"):
                body = "^" + body[1:]
            out.append(f"[{body}]")
            i = end + 1
            continue
        out.append(re.escape(char))
        i += 1
    return "".join(out)


@lru_cache(maxsize=_CACHE_SIZE)
def compile_pattern(pattern: str) -> re.Pattern[str]:
    """Compile one path pattern. Raises :class:`PatternError` on nonsense."""
    raw = pattern.strip().replace("\\", "/")
    if not raw:
        raise PatternError("empty path pattern")
    anchored = raw.startswith("/")
    body = raw.lstrip("/")
    if not body:
        raise PatternError(f"path pattern {pattern!r} selects the repository root")
    if body.endswith("/"):
        # `infra/` is `infra/**` written the other way round.
        body = body + "**"
    has_separator = "/" in body
    source = _translate(body)
    if not has_separator and not anchored:
        # Basename form: `*.pem` protects a `.pem` anywhere in the tree.
        source = f"(?:.*/)?{source}"
    try:
        return re.compile(f"^{source}$")
    except re.error as exc:  # pragma: no cover - _translate emits valid regex
        raise PatternError(f"invalid path pattern {pattern!r}: {exc}") from exc


def validate_patterns(patterns: list[str]) -> list[str]:
    """Compile every pattern once, so a broken policy fails at load time."""
    for pattern in patterns:
        compile_pattern(pattern)
    return patterns


def matches(path: str, pattern: str) -> bool:
    """Whether one repository path is covered by one pattern."""
    candidate = normalize(path)
    if not candidate:
        return False
    compiled = compile_pattern(pattern)
    if compiled.match(candidate):
        return True
    # A directory pattern covers the directory entry itself, so `infra/**`
    # still matches a rename that touches bare `infra`.
    raw = pattern.strip().replace("\\", "/").lstrip("/")
    return raw.endswith("/**") and candidate == raw[:-3].rstrip("/")


def match_any(path: str, patterns: list[str]) -> str:
    """The first pattern covering ``path``, or ``""``.

    Returning the pattern rather than a bool is what lets a decision say *which*
    rule protected a file — a veto nobody can trace is a veto nobody trusts.
    """
    for pattern in patterns:
        if matches(path, pattern):
            return pattern
    return ""


def select(paths: list[str], patterns: list[str]) -> list[str]:
    """Every path covered by any pattern, in input order, de-duplicated."""
    if not patterns:
        return []
    seen: set[str] = set()
    hits: list[str] = []
    for path in paths:
        if path in seen:
            continue
        if match_any(path, patterns):
            seen.add(path)
            hits.append(path)
    return hits


# Files a repository generates rather than writes. They are excluded from the
# size budget (a lockfile bump is not a 4,000-line change a human must read)
# and a diff made only of them gives the gate nothing to reason about.
DEFAULT_GENERATED_PATTERNS: tuple[str, ...] = (
    "*.lock",
    "*.lockb",
    "package-lock.json",
    "yarn.lock",
    "pnpm-lock.yaml",
    "Pipfile.lock",
    "poetry.lock",
    "uv.lock",
    "composer.lock",
    "Cargo.lock",
    "go.sum",
    "*.min.js",
    "*.min.css",
    "*.map",
    "*.pb.go",
    "*_pb2.py",
    "*_pb2.pyi",
    "*.generated.*",
    "**/__generated__/**",
    "**/generated/**",
    "**/node_modules/**",
    "**/vendor/**",
    "**/dist/**",
)

# Paths whose blast radius is not readable from the diff alone: credentials,
# deployment topology, CI definitions that run with repository secrets, and the
# gate's own policy. Conservative by design — an operator narrows this list
# deliberately, never by forgetting to widen it.
DEFAULT_PROTECTED_PATTERNS: tuple[str, ...] = (
    ".github/workflows/**",
    ".github/actions/**",
    ".gitlab-ci.yml",
    ".forgejo/**",
    ".gitea/**",
    "Dockerfile",
    "Dockerfile.*",
    "docker-compose*.y*ml",
    "**/migrations/**",
    "**/Chart.yaml",
    "**/values*.y*ml",
    "*.tf",
    "*.tfvars",
    "**/terraform/**",
    "**/helm/**",
    "**/k8s/**",
    "**/kubernetes/**",
    "*.pem",
    "*.key",
    "*.p12",
    "*.pfx",
    ".env*",
    "**/secrets/**",
    "CODEOWNERS",
    ".github/CODEOWNERS",
    "docs/CODEOWNERS",
    ".mira.yaml",
    ".mira.yml",
)
