"""Pre-merge checks: reproducible, evidenced answers about a pull request.

The shape of the phase in one paragraph. A **check** answers one question and
says how it knows. It has an identity, a version, a configuration digest, a
duration and one of five states — and the state vocabulary is the point:
``violation`` is the only one that says anything about the pull request, and
``infrastructure_error``, ``timeout`` and ``skipped`` all say something about
Mira instead. A **run** is every check for one pull request at one commit, with
a verdict a merge gate can read: ``pass``, ``violation``, ``incomplete`` or
``not_run``. Incomplete is not a pass, which is what "fail closed" means here.

Where things live:

``models``          the vocabulary, the records, and the identities that make a
                    retry converge on one row.
``config_models``   the configuration, in its own module because nothing in a
                    pull request may reach any value in it.
``policy``          three-layer resolution: global, organisation, repository.
``registry``        which checks exist, and how a new kind is added.
``context``         what a check may see; ``CheckOutcome``, all it may return.
``runner``          concurrency, budgets, timeouts, and the rule that a
                    violation without evidence is not recorded as one.
``dedupe``          one problem, one entry, every source named.
``native/``         the compiled-in checks: description, docs, tests, breaking
                    change, migrations.
``external/``       the ones that read something outside the diff: a ticket, a
                    CI run.
``tools/``          deterministic analysers, behind a closed allowlist.
``natural``         checks written as instructions, evaluated by a model that
                    is never trusted and whose quotes are always verified.
``persistence``     the two tables, written once for SQLite and Postgres.
``service``         the half with side effects: gather, persist, announce.
``explain``         saying what happened, without ever mixing "your problem"
                    with "our problem".
"""

from __future__ import annotations
