"""Does this change take something away that a caller depends on?

A breaking change is not detectable from a diff in general — it needs the call
graph, the published API and the version policy. What *is* detectable is the
shape of one: a public thing that existed on the base commit and does not exist
on the head commit, or exists with a signature that a current caller could not
satisfy.

So this check reports candidates, and says so in those words. It looks for four
shapes, all of them in removed lines:

* a public symbol removed and not re-added anywhere in the diff — a rename
  shows up as a removal *and* an addition, and a rename inside one pull request
  is usually a rename of something the pull request also updates every caller
  of, so it is not reported;
* a route removed;
* a required parameter added to a function that already existed — every current
  caller passes too few arguments now;
* an environment variable or configuration key removed from the example file,
  which is a deployment break rather than a compile one.

Each candidate carries the removed line and its position, so the reader can
decide in a glance. That is the point: the check is here to make a human look
at four lines, not to be certain on their behalf.
"""

from __future__ import annotations

import re

from mira.checks.context import CheckContext, CheckOutcome
from mira.checks.models import CheckFinding, Evidence, SkipReason, fingerprint
from mira.checks.native import paths as native_paths
from mira.checks.native.evidence import DiffLine, iter_all, snippet

VERSION = "1"

CHECK_ID = "native.breaking_change"

_MAX_FINDINGS = 12

_DEFINITION = re.compile(
    r"^\s*(?:"
    r"(?:async\s+)?def\s+(?P<py>[A-Za-z][A-Za-z0-9_]*)\s*\((?P<pyargs>[^)]*)"
    r"|export\s+(?:default\s+)?(?:async\s+)?function\s+(?P<js>[A-Za-z][A-Za-z0-9_]*)"
    r"\s*\((?P<jsargs>[^)]*)"
    r"|func\s+(?:\([^)]*\)\s*)?(?P<go>[A-Z][A-Za-z0-9_]*)\s*\((?P<goargs>[^)]*)"
    r"|pub\s+(?:async\s+)?fn\s+(?P<rs>[A-Za-z][A-Za-z0-9_]*)\s*\((?P<rsargs>[^)]*)"
    r")"
)

_CLASS = re.compile(
    r"^\s*(?:class|interface|enum|type)\s+(?P<name>[A-Za-z][A-Za-z0-9_]*)"
    r"|^\s*export\s+(?:class|interface|enum|type)\s+(?P<exported>[A-Za-z][A-Za-z0-9_]*)"
)

_ROUTE = re.compile(
    r"(?:@(?:app|router|api|blueprint)\.(?:get|post|put|patch|delete|route)|"
    r"\b(?:app|router)\.(?:get|post|put|patch|delete))\s*\(\s*[\"'](?P<path>[^\"']+)"
)

_ENV_KEY = re.compile(r"^\s*(?:export\s+)?(?P<key>[A-Z][A-Z0-9_]{2,})\s*=")


def _name(line: str) -> tuple[str, str]:
    """``(name, arguments)`` for a definition line, or ``("", "")``."""
    match = _DEFINITION.match(line)
    if match:
        groups = match.groupdict()
        for key in ("py", "js", "go", "rs"):
            if groups.get(key):
                return groups[key], groups.get(f"{key}args") or ""
    match = _CLASS.match(line)
    if match:
        return (match.group("name") or match.group("exported") or ""), ""
    return "", ""


def _required_parameters(arguments: str) -> list[str]:
    """Parameter names with no default, ignoring ``self``/``cls`` and varargs.

    Split on top-level commas only: ``a: dict[str, int]`` is one parameter, and
    a naive split would report a type annotation as a new argument.
    """
    names: list[str] = []
    depth = 0
    current = ""
    for char in arguments + ",":
        if char in "([{<":
            depth += 1
        elif char in ")]}>":
            depth -= 1
        if char == "," and depth == 0:
            piece = current.strip()
            current = ""
            if not piece or piece.startswith(("*", "&")) or "=" in piece:
                continue
            name = piece.split(":")[0].strip().lstrip("*&")
            if name and name not in {"self", "cls", "this"}:
                names.append(name)
        else:
            current += char
    return names


def _evidence(line: DiffLine, detail: str) -> Evidence:
    return Evidence(
        path=line.path,
        start_line=line.line,
        snippet=snippet(line.text),
        detail=detail,
        source="diff",
    )


def _finding(title: str, detail: str, evidence: Evidence, signature: str) -> CheckFinding:
    return CheckFinding(
        fingerprint=fingerprint(path=evidence.path, signature=signature),
        title=title,
        detail=detail,
        severity="warning",
        evidence=[evidence],
        sources=[CHECK_ID],
    )


async def run(ctx: CheckContext) -> CheckOutcome:
    """Report every removal-shaped change that a caller could notice."""
    removed_symbols: dict[str, DiffLine] = {}
    added_symbols: dict[str, str] = {}
    removed_routes: dict[str, DiffLine] = {}
    added_routes: set[str] = set()
    removed_env: dict[str, DiffLine] = {}
    added_env: set[str] = set()
    # name -> (required parameters before, required parameters after)
    signatures: dict[str, tuple[list[str] | None, list[str] | None]] = {}

    for line in iter_all(ctx.patch_set):
        if not (line.added or line.removed):
            continue
        if native_paths.is_test(line.path) or native_paths.is_generated(line.path):
            continue

        name, arguments = _name(line.text)
        if name and not name.startswith("_"):
            key = f"{line.path}::{name}"
            before, after = signatures.get(key, (None, None))
            if line.removed:
                removed_symbols[key] = line
                signatures[key] = (_required_parameters(arguments), after)
            else:
                added_symbols[key] = name
                signatures[key] = (before, _required_parameters(arguments))

        route = _ROUTE.search(line.text)
        if route:
            path = route.group("path")
            if line.removed:
                removed_routes[path] = line
            else:
                added_routes.add(path)

        if line.path.lower().endswith((".env.example", ".env.sample", ".env.template")):
            env = _ENV_KEY.match(line.text)
            if env:
                if line.removed:
                    removed_env[env.group("key")] = line
                else:
                    added_env.add(env.group("key"))

    findings: list[CheckFinding] = []

    for key, line in sorted(removed_symbols.items()):
        if key in added_symbols:
            continue
        name = key.split("::", 1)[1]
        findings.append(
            _finding(
                f"`{name}` was removed",
                f"`{name}` is public and no longer defined in `{line.path}` after this "
                "change. Anything outside this pull request that referenced it breaks.",
                _evidence(line, f"removed definition of {name}"),
                f"removed symbol {name}",
            )
        )

    for key, (before, after) in sorted(signatures.items()):
        if before is None or after is None:
            continue
        new_required = [name for name in after if name not in before]
        if not new_required:
            continue
        name = key.split("::", 1)[1]
        removed_line = removed_symbols.get(key)
        evidence = (
            _evidence(removed_line, f"previous signature of {name}")
            if removed_line is not None
            else Evidence(path=key.split("::", 1)[0], detail=f"signature of {name}", source="diff")
        )
        findings.append(
            _finding(
                f"`{name}` gained required parameter(s): {', '.join(new_required)}",
                "Every existing caller passes too few arguments now. Giving the new "
                "parameter(s) a default would keep them working.",
                evidence,
                f"signature widened {name}",
            )
        )

    for path, line in sorted(removed_routes.items()):
        if path in added_routes:
            continue
        findings.append(
            _finding(
                f"Route `{path}` was removed",
                "A client calling this endpoint will get a 404 after this change.",
                _evidence(line, f"removed route {path}"),
                f"removed route {path}",
            )
        )

    for key, line in sorted(removed_env.items()):
        if key in added_env:
            continue
        findings.append(
            _finding(
                f"Configuration key `{key}` was removed from the example environment",
                "A deployment that sets this key is configuring something that no "
                "longer exists, and one that relied on its documented default has "
                "nothing to read.",
                _evidence(line, f"removed {key}"),
                f"removed env key {key}",
            )
        )

    if not findings:
        return CheckOutcome.skipped(
            "Nothing in this change has the shape of a removal a caller could notice.",
            SkipReason.NOT_APPLICABLE,
        )

    trimmed = findings[:_MAX_FINDINGS]
    extra = len(findings) - len(trimmed)
    summary = f"{len(findings)} possible breaking change(s)."
    if extra:
        summary += f" Showing {len(trimmed)}; {extra} more were found and not listed."
    return CheckOutcome.violation(summary=summary, findings=trimmed)
