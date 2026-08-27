"""Exit codes for ``mira local review``, and the contract they carry.

A local review is meant to run in a pre-commit hook and in a CI job, and both
branch on the process's exit status. So the codes are a published interface:
they are enumerated here, documented in ``docs/local-cli.md``, asserted in the
test suite, and they do not change meaning without a major version.

The distinction that matters most is between *the review found something* and
*the review did not happen*. A job that treats every non-zero status as "the
code is bad" will block on a missing API key; a job that treats every non-zero
status as "the tool is broken" will merge a blocker. Only :data:`ExitCode.FINDINGS`
is a statement about the code. Every other non-zero code is a statement about
the run.
"""

from __future__ import annotations

import enum


class ExitCode(enum.IntEnum):
    """What the process's exit status means."""

    #: The review completed and found nothing at or above the fail threshold.
    OK = 0

    #: The review completed and found something. The only code that says
    #: anything about the code under review: findings at or above ``--fail-on``,
    #: or a blocking pre-merge check that reported a violation.
    FINDINGS = 1

    #: The command line was wrong: conflicting modes, an unparseable commit
    #: range, a revision that does not resolve. Click's own usage errors also
    #: exit 2, deliberately — a caller does not have to tell them apart.
    USAGE = 2

    #: Git could not answer: not a repository, no ``git`` on PATH, a diff that
    #: could not be produced. Nothing was reviewed and nothing is claimed.
    GIT = 3

    #: The configuration is unusable, or using it would have sent the code
    #: somewhere the repository did not configure. Nothing was sent.
    CONFIG = 4

    #: The review itself could not complete — the model endpoint was
    #: unreachable, the engine raised. Nothing is claimed about the code.
    ENGINE = 5

    #: Interrupted (Ctrl-C). The conventional 128 + SIGINT.
    INTERRUPTED = 130


#: One-line descriptions, rendered by ``mira local review --explain-exit-codes``
#: so the contract is readable from the tool itself and not only from the docs.
EXIT_CODE_HELP: dict[ExitCode, str] = {
    ExitCode.OK: "Review completed; nothing at or above the fail threshold.",
    ExitCode.FINDINGS: "Review completed; findings at or above the fail threshold.",
    ExitCode.USAGE: "Invalid arguments (conflicting modes, bad commit range).",
    ExitCode.GIT: "Git could not answer (not a repository, git missing, diff failed).",
    ExitCode.CONFIG: "Configuration is unusable, or the destination was refused.",
    ExitCode.ENGINE: "The review could not complete (model endpoint, engine error).",
    ExitCode.INTERRUPTED: "Interrupted.",
}
