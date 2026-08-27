"""Did code change without a test changing with it?

The check is a coverage question asked cheaply: it does not run the suite, does
not measure coverage, and does not know whether the test that changed has
anything to do with the code that changed. What it can say — and all it claims
to say — is that source files were modified and no test file was.

That is worth saying because it is the failure that actually happens. A pull
request that adds a branch nobody exercises usually also adds no test file at
all, and the version of this question that needs a coverage run cannot be asked
on the deployment profile Mira targets.

Three things keep it from being noise:

* **A diff with no source file in it is skipped, not passed.** A documentation
  pull request has not failed to write a test; the question does not apply.
* **Generated output does not count as source.** A lockfile bump is not code
  somebody wrote.
* **Deletion-only changes do not count.** Removing code does not oblige anyone
  to write a test for the code that is gone.

What it cannot tell is whether the repository has a test suite at all. A
repository with none will see this check object on every pull request, which is
the correct behaviour for a check in ``warning`` mode and the reason
``default_mode`` is ``warning``: an install turns a check to ``error`` when it
has decided the answer matters, not before.
"""

from __future__ import annotations

from mira.checks.context import CheckContext, CheckOutcome
from mira.checks.models import CheckFinding, Evidence, SkipReason, fingerprint
from mira.checks.native import paths as native_paths

VERSION = "1"

CHECK_ID = "native.tests"

# Changed source files shown as evidence. A cap, not a page: the point is to
# name the change, not to reproduce the diff.
_MAX_EVIDENCE = 10


async def run(ctx: CheckContext) -> CheckOutcome:
    """Object when source changed, nothing under test changed, and code was added."""
    source: list[str] = []
    tests: list[str] = []
    added_by_path: dict[str, int] = {}

    for change in ctx.changes:
        added_by_path[change.path] = change.added_lines
        if native_paths.is_test(change.path):
            tests.append(change.path)
        elif native_paths.is_source(change.path):
            source.append(change.path)

    if not source:
        return CheckOutcome.skipped(
            "No source files changed, so there is nothing this check would expect a test for.",
            SkipReason.NOT_APPLICABLE,
        )

    if tests:
        return CheckOutcome.passed(
            summary=f"{len(tests)} test file(s) changed alongside {len(source)} source file(s).",
            evidence=[
                Evidence(path=path, detail="test file changed", source="diff")
                for path in sorted(tests)[:_MAX_EVIDENCE]
            ],
        )

    added = sum(added_by_path.get(path, 0) for path in source)
    if added == 0:
        return CheckOutcome.passed(
            summary=(
                "The source changes only remove lines, so no new behaviour is going untested here."
            ),
            evidence=[
                Evidence(path=path, detail="lines removed only", source="diff")
                for path in sorted(source)[:_MAX_EVIDENCE]
            ],
        )

    evidence = [
        Evidence(
            path=path,
            detail=f"{added_by_path.get(path, 0)} added line(s), no test changed",
            source="diff",
        )
        for path in sorted(source)[:_MAX_EVIDENCE]
    ]
    finding = CheckFinding(
        fingerprint=fingerprint(path=sorted(source)[0], signature="source changed without a test"),
        title="Source changed and no test changed with it",
        detail=(
            f"{len(source)} source file(s) gained {added} line(s) and no file this "
            "repository recognises as a test was touched. Mira did not run the suite "
            "and cannot say whether the new lines are covered — only that nothing "
            "under test moved."
        ),
        evidence=evidence,
        sources=[CHECK_ID],
    )
    return CheckOutcome.violation(
        summary=f"{len(source)} source file(s) changed with no accompanying test change.",
        findings=[finding],
    )
