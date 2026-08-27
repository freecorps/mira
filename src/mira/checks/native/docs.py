"""Did something documented change without the documentation changing?

"Needs docs" is not a property of a diff's size, so this check does not use
one. It asks a narrower question with a defensible answer: did this pull
request change a surface that people outside the repository read about, and
leave every document untouched?

Three things count as such a surface, and nothing else does:

* a file that *is* an interface — the CLI, the configuration model, an HTTP
  router, ``.env.example``, a Dockerfile;
* a new public symbol added to source: an exported function, a class, a route
  decorator. Added, not modified — renaming a private helper documents nothing;
* a removed public symbol, because a document that still describes it is now
  wrong. This one overlaps with the breaking-change check on purpose: the two
  are asking different questions about the same line, and the deduplication
  pass is what keeps a reader from being told about it twice.

Everything else — an internal refactor, a bug fix, a test — passes without
comment. A check that demanded a documentation change for every pull request
would be turned off within a week, and a check that is off finds nothing.
"""

from __future__ import annotations

import re

from mira.checks.context import CheckContext, CheckOutcome
from mira.checks.models import CheckFinding, Evidence, SkipReason, fingerprint
from mira.checks.native import paths as native_paths
from mira.checks.native.evidence import DiffLine, iter_all, snippet

VERSION = "1"

CHECK_ID = "native.docs"

_MAX_EVIDENCE = 8

# Definitions of something a caller outside this file can reach. Deliberately
# syntactic: this runs on a diff, not on a parsed program, and a regex that
# occasionally misses a declaration is a check that occasionally stays quiet —
# which is the safe direction for a check that asks people to write prose.
_PUBLIC_DEFINITION = re.compile(
    r"^\s*(?:"
    r"(?:async\s+)?def\s+(?P<py>[A-Za-z][A-Za-z0-9_]*)"  # python function
    r"|class\s+(?P<cls>[A-Za-z][A-Za-z0-9_]*)"  # python/ts/java class
    r"|export\s+(?:default\s+)?(?:async\s+)?(?:function|const|class|interface|type|enum)"
    r"\s+(?P<js>[A-Za-z][A-Za-z0-9_]*)"  # javascript/typescript
    r"|func\s+(?:\([^)]*\)\s*)?(?P<go>[A-Z][A-Za-z0-9_]*)"  # exported go function
    r"|pub\s+(?:async\s+)?fn\s+(?P<rs>[A-Za-z][A-Za-z0-9_]*)"  # rust
    r"|public\s+[A-Za-z<>\[\]]+\s+(?P<java>[A-Za-z][A-Za-z0-9_]*)\s*\("  # java/c#
    r")"
)

# A route decorator or registration: the most externally visible thing a
# repository can add without adding a document.
_ROUTE = re.compile(
    r"(?:@(?:app|router|api|blueprint)\.(?:get|post|put|patch|delete|route)\s*\(|"
    r"\b(?:app|router)\.(?:get|post|put|patch|delete)\s*\(\s*[\"'])"
)


def _symbol(line: str) -> str:
    match = _PUBLIC_DEFINITION.match(line)
    if not match:
        return ""
    name = next((value for value in match.groupdict().values() if value), "")
    # A leading underscore is the one convention every language in the list
    # agrees on for "not yours to call".
    return "" if name.startswith("_") else name


def _describe(line: DiffLine, name: str) -> Evidence:
    return Evidence(
        path=line.path,
        start_line=line.line,
        snippet=snippet(line.text),
        detail=f"{'removed' if line.removed else 'added'} {name}",
        source="diff",
    )


async def run(ctx: CheckContext) -> CheckOutcome:
    """Object when a documented surface moved and no document did."""
    docs_changed = [change.path for change in ctx.changes if native_paths.is_doc(change.path)]
    interface_files = [
        change.path
        for change in ctx.changes
        if native_paths.is_interface(change.path) and not native_paths.is_generated(change.path)
    ]

    surface: list[Evidence] = []
    for line in iter_all(ctx.patch_set):
        if not (line.added or line.removed):
            continue
        if native_paths.is_test(line.path) or native_paths.is_generated(line.path):
            continue
        name = _symbol(line.text)
        if name:
            surface.append(_describe(line, f"public symbol `{name}`"))
        elif line.added and _ROUTE.search(line.text):
            surface.append(_describe(line, "route"))
        if len(surface) >= _MAX_EVIDENCE * 4:
            break

    interface_evidence = [
        Evidence(path=path, detail="interface file changed", source="diff")
        for path in sorted(interface_files)
    ]

    if not surface and not interface_evidence:
        return CheckOutcome.skipped(
            "Nothing in this change alters a documented surface: no interface file, "
            "no public symbol, no route.",
            SkipReason.NOT_APPLICABLE,
        )

    if docs_changed:
        return CheckOutcome.passed(
            summary=(
                f"{len(docs_changed)} document(s) changed alongside a change to a "
                "documented surface."
            ),
            evidence=[
                Evidence(path=path, detail="documentation changed", source="diff")
                for path in sorted(docs_changed)[:_MAX_EVIDENCE]
            ],
        )

    evidence = (interface_evidence + surface)[:_MAX_EVIDENCE]
    anchor = evidence[0]
    finding = CheckFinding(
        fingerprint=fingerprint(
            path=anchor.path,
            signature="documented surface changed without documentation",
        ),
        title="A documented surface changed and no document did",
        detail=(
            "This pull request changes something a reader outside the repository can "
            "see — the items below — and touches no file this repository recognises as "
            "documentation. Either the documentation needs the same change, or this "
            "surface was never documented, which is worth knowing too."
        ),
        evidence=evidence,
        sources=[CHECK_ID],
    )
    return CheckOutcome.violation(
        summary="A documented surface changed with no documentation change.",
        findings=[finding],
    )
