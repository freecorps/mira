"""Triage and reviewer suggestion: what this change is, and who knows it best.

The phase in one paragraph. A **run** describes one pull request twice: a
deterministic **classification** built from the diff alone — size, areas,
whether it carries tests, a migration, a lockfile — and a ranked list of
**candidates**, each carrying the evidence that put them there. Candidates come
from two signals: what the repository *declares* in CODEOWNERS, and what Mira
has *observed* about who writes and reviews these files. Nothing is assigned,
nothing is requested, and no status is published, so no merge can ever wait on
a ranking built out of inference.

Three properties hold it together.

**Ownership is read at the base commit.** CODEOWNERS is repository policy and
the pull request is the thing being measured against it, so a branch cannot add
a line naming a friendly account and be ranked under it. The ref that was used
is recorded on the run.

**"Nobody" and "we could not tell" are different answers.** A run that read
every signal and found no candidate is ``no_candidates``. A run whose ownership
lookup failed, or whose history could not be read, is ``unavailable`` — shown
in Mira's own name, never as a statement about the people who work here.

**Every name carries its reason.** A CODEOWNERS line with its line number, a
commit, a pull request someone reviewed. A ranking nobody can check is a
ranking nobody should follow.

Where things live:

``models``          the vocabulary, the records, the run identity.
``config_models``   the policy, in its own module because nothing in a pull
                    request may reach a value in it.
``policy``          three-layer resolution: global, organisation, repository.
``capabilities``    what each platform can actually tell us, declared.
``classify``        what kind of change this is, from paths and line counts.
``ownership``       the CODEOWNERS signal, read at the base.
``history``         the two history signals and the bounded cache behind them.
``contributions``   recording what Mira watched merge, so history exists at all.
``load``            how much review is already waiting on somebody.
``scoring``         pure arithmetic: the same signals always rank the same way.
``service``         the half with side effects: gather, persist, announce.
``persistence``     the tables, written once for SQLite and Postgres.
``explain``         saying it, without ever mentioning anybody.
"""

from __future__ import annotations
