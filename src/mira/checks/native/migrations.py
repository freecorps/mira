"""What this change does to the database, and whether it can be undone.

A schema change is the one class of change a rollback cannot fix by itself:
reverting the deployment puts the old code back and leaves the new column
exactly where it was — or, worse, leaves the dropped one gone. So this check
does not ask "is there a migration"; it asks the two questions whose answers
decide whether an incident at 3am is recoverable.

**Is anything destructive?** ``DROP TABLE``, ``DROP COLUMN``, a ``RENAME`` and a
``NOT NULL`` added to an existing column are all changes that the previous
release cannot run against. Each one is reported with its own line.

**Can it be undone?** In a language whose migrations *are* a pair of functions
— Alembic, Django, Rails, Knex — a revision with no ``downgrade``, or one whose
body is `pass`, went one way with no written path back, and that is reported.
A plain ``.sql`` migration is different: its rollback is conventionally a
second file under a naming scheme this check does not know, so it is recorded
as *not assessed* rather than guessed at in either direction. When every
migration in a pull request falls into that bucket and nothing destructive
turned up, the result is a skip — Mira could not answer, and a pass would claim
a reversibility nothing established.

A pull request with no schema change at all is skipped: this check has no
opinion about it, and saying "pass" would imply it looked at something it did
not. A schema change that is additive and reversible passes, and names the
migration it read — a reviewer should be able to see that the check understood
the file rather than merely failed to object to it.
"""

from __future__ import annotations

import re

from mira.checks.context import CheckContext, CheckOutcome
from mira.checks.models import CheckFinding, Evidence, SkipReason, fingerprint
from mira.checks.native import paths as native_paths
from mira.checks.native.evidence import DiffLine, iter_all, snippet

VERSION = "1"

CHECK_ID = "native.migrations"

_MAX_FINDINGS = 12

# Destructive DDL. Matched on added lines: a migration is the addition, and the
# thing being added is the statement that will run.
_DESTRUCTIVE = (
    (re.compile(r"(?i)\bdrop\s+table\b"), "drops a table"),
    (re.compile(r"(?i)\bdrop\s+(?:column|constraint|index)\b"), "drops a column or constraint"),
    (re.compile(r"(?i)\balter\s+table\b.*\brename\b|\brename\s+(?:table|column)\b"), "renames"),
    (re.compile(r"(?i)\bdrop_table\s*\(|\bdrop_column\s*\("), "drops a table or column"),
    (re.compile(r"(?i)\btruncate\s+table\b"), "truncates a table"),
    (
        re.compile(r"(?i)\balter\s+column\b.*\bset\s+not\s+null\b|nullable\s*=\s*False"),
        "makes an existing column NOT NULL",
    ),
)

# Any DDL at all, for deciding whether this pull request touches a schema even
# when no path looked like a migration.
_ANY_DDL = re.compile(
    r"(?i)\b(?:create\s+table|alter\s+table|drop\s+table|create\s+index|"
    r"add\s+column|drop\s+column)\b|\b(?:op\.(?:create_table|add_column|alter_column|"
    r"drop_table|drop_column)|createTable|addColumn|dropColumn)\s*\("
)

_DOWNGRADE = re.compile(
    r"^\s*(?:def\s+downgrade\b|async\s+def\s+downgrade\b|-- *down\b|"
    r"(?:public\s+)?(?:void|function)\s+down\b|\bdef\s+down\b)"
)

# A downgrade body that does nothing. `pass` and a lone `raise` are the two
# ways a generated revision arrives when nobody filled it in.
_EMPTY_BODY = re.compile(r"^\s*(?:pass|\.\.\.|raise\s+NotImplementedError.*)\s*$")


def _evidence(line: DiffLine, detail: str) -> Evidence:
    return Evidence(
        path=line.path,
        start_line=line.line,
        snippet=snippet(line.text),
        detail=detail,
        source="diff",
    )


# What Mira was able to establish about one migration file.
#
# `reversible`    it declares a downgrade with something in it.
# `irreversible`  it declares one that is empty, or — in a language whose
#                 migrations *are* a pair of functions — declares none at all.
# `unknown`       Mira has no convention to look for here. A plain `.sql`
#                 migration's rollback is usually a second file under a naming
#                 scheme this check does not know, so calling it irreversible
#                 would be noise and calling it reversible would be a claim
#                 nothing supports.
# `unreadable`    the file could not be fetched. Mira's problem, not the
#                 change's.
_REVERSIBLE = "reversible"
_IRREVERSIBLE = "irreversible"
_UNKNOWN = "unknown"
_UNREADABLE = "unreadable"

# Languages where a migration is conventionally a pair of functions, so the
# absence of a downgrade is itself the answer rather than a gap in Mira's
# knowledge: Alembic and Django (Python), Rails (Ruby), Knex and TypeORM
# (JS/TS).
_PAIRED_SUFFIXES = frozenset({".py", ".rb", ".ts", ".js"})


async def _reversibility(ctx: CheckContext, path: str) -> str:
    """What can be established about ``path``'s rollback path.

    Reads the file at the head commit rather than the diff, because a revision
    edited in place shows only the changed lines and the ``downgrade`` that
    matters may be untouched context.

    The four answers are kept apart on purpose. An earlier version of this
    returned a bool, so "no downgrade declared at all" and "a downgrade with a
    body" both came back as *not empty* — and a migration with no way back read
    as reversible, which is the opposite of what this check exists to say.
    """
    content = await ctx.file_content(path)
    if not content:
        return _UNREADABLE

    lines = content.splitlines()
    for index, line in enumerate(lines):
        if not _DOWNGRADE.match(line):
            continue
        for following in lines[index + 1 : index + 12]:
            if not following.strip() or following.lstrip().startswith("#"):
                continue
            return _IRREVERSIBLE if _EMPTY_BODY.match(following) else _REVERSIBLE
        # A downgrade declared as the last thing in the file has no body.
        return _IRREVERSIBLE

    if native_paths.suffix(path) in _PAIRED_SUFFIXES:
        return _IRREVERSIBLE
    return _UNKNOWN


async def run(ctx: CheckContext) -> CheckOutcome:
    """Report destructive statements, and migrations with no way back."""
    migration_paths = sorted(
        {change.path for change in ctx.changes if native_paths.is_migration(change.path)}
    )
    findings: list[CheckFinding] = []
    ddl_seen = False
    schema_evidence: list[Evidence] = []
    unreadable: list[str] = []

    for line in iter_all(ctx.patch_set):
        if not line.added or native_paths.is_generated(line.path):
            continue
        if not (native_paths.is_migration(line.path) or _ANY_DDL.search(line.text)):
            continue
        ddl_seen = True
        if len(schema_evidence) < 8 and _ANY_DDL.search(line.text):
            schema_evidence.append(_evidence(line, "schema statement"))
        for pattern, what in _DESTRUCTIVE:
            if pattern.search(line.text):
                findings.append(
                    CheckFinding(
                        fingerprint=fingerprint(path=line.path, signature=f"destructive: {what}"),
                        title=f"This migration {what}",
                        detail=(
                            "The release running before this one cannot execute against the "
                            "schema this leaves behind, so rolling the deployment back does "
                            "not roll this back. If it has to ship, it wants a deliberate "
                            "two-step: deploy code that works either way, then migrate."
                        ),
                        severity="blocker",
                        evidence=[_evidence(line, what)],
                        sources=[CHECK_ID],
                    )
                )
                break

    if not ddl_seen and not migration_paths:
        return CheckOutcome.skipped(
            "This change touches no migration and contains no schema statement.",
            SkipReason.NOT_APPLICABLE,
        )

    unknown: list[str] = []
    assessed = 0
    for path in migration_paths:
        verdict = await _reversibility(ctx, path)
        if verdict == _UNREADABLE:
            unreadable.append(path)
            continue
        if verdict == _UNKNOWN:
            unknown.append(path)
            continue
        assessed += 1
        if verdict == _IRREVERSIBLE:
            findings.append(
                CheckFinding(
                    fingerprint=fingerprint(path=path, signature="migration has no downgrade"),
                    title=f"`{path}` has no way back",
                    detail=(
                        "The migration declares no downgrade, or declares one with an "
                        "empty body. Undoing this release would leave the schema where "
                        "the migration put it."
                    ),
                    evidence=[
                        Evidence(
                            path=path,
                            detail="no downgrade body at the head commit",
                            source="file",
                        )
                    ],
                    sources=[CHECK_ID],
                )
            )

    if unreadable and not findings:
        # Nothing objectionable was found *and* a file the check needed could
        # not be read. Reporting a pass here would claim a reversibility this
        # check never established.
        return CheckOutcome.failed(
            error=f"could not read {', '.join(unreadable[:5])} at {ctx.head_sha or 'the head commit'}",
            summary=(
                "Mira could not read "
                f"{len(unreadable)} migration file(s), so it cannot say whether they are "
                "reversible. This is a Mira problem, not a problem with the change."
            ),
        )

    if findings:
        trimmed = findings[:_MAX_FINDINGS]
        extra = len(findings) - len(trimmed)
        summary = f"{len(findings)} schema concern(s) in {len(migration_paths) or 1} file(s)."
        if extra:
            summary += f" Showing {len(trimmed)}; {extra} more were found and not listed."
        if unknown:
            summary += (
                f" {len(unknown)} file(s) use no rollback convention Mira recognises and "
                "were not assessed."
            )
        return CheckOutcome.violation(summary=summary, findings=trimmed)

    if unknown and not assessed:
        # Every migration used a convention this check cannot read, and nothing
        # destructive turned up in the statements themselves. Reporting a pass
        # would claim a reversibility nothing established; this says what
        # happened, and — being an unanswered skip — still keeps a blocking
        # gate closed.
        return CheckOutcome.skipped(
            f"{len(unknown)} migration file(s) use no rollback convention Mira "
            "recognises, so it could not establish whether they can be undone: "
            + ", ".join(unknown[:5])
            + ".",
            SkipReason.UNSUPPORTED,
        )

    summary = f"{assessed} migration file(s) change the schema additively and define a way back."
    if unknown:
        summary += (
            f" {len(unknown)} more use no rollback convention Mira recognises and were "
            "not assessed."
        )
    return CheckOutcome.passed(
        summary=summary,
        evidence=schema_evidence
        or [
            Evidence(path=path, detail="migration changed", source="diff")
            for path in migration_paths[:8]
        ],
    )
