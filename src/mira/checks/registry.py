"""Which checks exist, and how one is added.

Every check in a run comes from here, and a check that is not registered cannot
run. That is the plugin boundary: a native check, a deterministic analyser and
a natural-language rule are all just a :class:`CheckSpec` with a coroutine, and
adding a fourth kind means adding specs — not editing the runner, the store,
the API or the dashboard.

The registry is *closed to pull requests and open to deployments*. Native check
ids are compiled in. Tool ids come from the allowlist in
:mod:`mira.checks.config_models`, so an operator picks from a reviewed set
rather than naming a binary. Natural-language ids come from the operator's own
configuration and are namespaced ``nl.`` so they can never collide with a
built-in one — a repository cannot define a rule called ``native.tests`` and
have its instruction answer for the compiled check of that name.

Specs are built per policy rather than kept in a module-level table, because
two repositories in the same install legitimately have different rules and a
shared mutable registry would let one repository's configuration decide
another's.
"""

from __future__ import annotations

from dataclasses import dataclass

from mira.checks.context import CheckRunner
from mira.checks.models import CheckOrigin
from mira.checks.policy import EffectiveChecksPolicy


@dataclass(frozen=True)
class CheckSpec:
    """One runnable check: its identity, its version, and how to run it.

    ``version`` is the check's *own* version, bumped when its logic changes.
    Together with the per-check config digest the runner records, it is what
    makes "this check regressed" a question with an answer: a result is only
    comparable with another produced by the same logic under the same rules.
    """

    check_id: str
    title: str
    origin: CheckOrigin
    version: str
    run: CheckRunner
    # One line, shown in the dashboard's catalog. What the check asks, not what
    # it concluded.
    description: str = ""


# Native check ids, compiled in. Named as constants because the persistence
# layer, the gate, the dashboard and the docs all refer to them, and a typo in
# a string literal is a check that silently never runs.
NATIVE_TITLE_DESCRIPTION = "native.title_description"
NATIVE_DOCS = "native.docs"
NATIVE_TESTS = "native.tests"
NATIVE_BREAKING_CHANGE = "native.breaking_change"
NATIVE_MIGRATIONS = "native.migrations"

CONTEXT_TICKET = "context.ticket"
CONTEXT_ACCEPTANCE_CRITERIA = "context.acceptance_criteria"
CONTEXT_CI = "context.ci"

NATIVE_CHECK_IDS: tuple[str, ...] = (
    NATIVE_TITLE_DESCRIPTION,
    NATIVE_DOCS,
    NATIVE_TESTS,
    NATIVE_BREAKING_CHANGE,
    NATIVE_MIGRATIONS,
)

CONTEXT_CHECK_IDS: tuple[str, ...] = (
    CONTEXT_TICKET,
    CONTEXT_ACCEPTANCE_CRITERIA,
    CONTEXT_CI,
)


def native_specs() -> list[CheckSpec]:
    """The compiled-in checks, in a stable order.

    Imported lazily so that importing the registry — which the dashboard does
    to render a catalog — does not drag in a diff parser and a manifest reader.
    """
    from mira.checks.native import breaking, description, docs, migrations, tests

    return [
        CheckSpec(
            check_id=NATIVE_TITLE_DESCRIPTION,
            title="Title and description",
            origin="native",
            version=description.VERSION,
            run=description.run,
            description="Whether the pull request says what it does and why.",
        ),
        CheckSpec(
            check_id=NATIVE_DOCS,
            title="Documentation",
            origin="native",
            version=docs.VERSION,
            run=docs.run,
            description="Whether a change that alters documented behaviour updated the docs.",
        ),
        CheckSpec(
            check_id=NATIVE_TESTS,
            title="Tests",
            origin="native",
            version=tests.VERSION,
            run=tests.run,
            description="Whether changed source code came with a changed test.",
        ),
        CheckSpec(
            check_id=NATIVE_BREAKING_CHANGE,
            title="Possible breaking change",
            origin="native",
            version=breaking.VERSION,
            run=breaking.run,
            description="Whether the diff removes or reshapes something callers depend on.",
        ),
        CheckSpec(
            check_id=NATIVE_MIGRATIONS,
            title="Migrations and schema",
            origin="native",
            version=migrations.VERSION,
            run=migrations.run,
            description="Whether a schema change is reversible and separated from data changes.",
        ),
    ]


def context_specs() -> list[CheckSpec]:
    """Checks that read something outside the diff: a ticket, a CI run."""
    from mira.checks.external import acceptance, ci, ticket

    return [
        CheckSpec(
            check_id=CONTEXT_TICKET,
            title="Linked issue",
            origin="context",
            version=ticket.VERSION,
            run=ticket.run,
            description="Whether the pull request references an issue that exists.",
        ),
        CheckSpec(
            check_id=CONTEXT_ACCEPTANCE_CRITERIA,
            title="Acceptance criteria",
            origin="context",
            version=acceptance.VERSION,
            run=acceptance.run,
            description="Whether the linked issue states acceptance criteria.",
        ),
        CheckSpec(
            check_id=CONTEXT_CI,
            title="CI result",
            origin="context",
            version=ci.VERSION,
            run=ci.run,
            description="What the head commit's CI says, with the failing output quoted.",
        ),
    ]


def tool_specs(policy: EffectiveChecksPolicy) -> list[CheckSpec]:
    """One spec per configured analyser, enabled or not.

    A disabled analyser keeps its spec and resolves to mode ``off``, so the
    runner records it as `skipped: disabled` and the catalog still lists it.
    Dropping it here instead would make "switched off" indistinguishable from
    "not present in this version" — and would remove a check from the run that
    an operator had put in `error` mode, which is the one direction a
    fail-closed framework must never move in by accident.
    """
    from mira.checks.tools import adapter_for

    specs: list[CheckSpec] = []
    for tool in policy.tools:
        adapter = adapter_for(tool.name)
        if adapter is None:  # pragma: no cover - the allowlist is closed
            continue
        specs.append(
            CheckSpec(
                check_id=tool.check_id,
                title=adapter.title,
                origin="tool",
                version=adapter.version,
                run=adapter.runner(tool),
                description=adapter.description,
            )
        )
    return specs


def natural_language_specs(policy: EffectiveChecksPolicy) -> list[CheckSpec]:
    """One spec per natural-language rule this repository's policy carries."""
    from mira.checks.natural import runner_for

    return [
        CheckSpec(
            check_id=rule.check_id,
            title=rule.title or rule.id,
            origin="natural_language",
            version=f"nl-1+{rule.version}",
            run=runner_for(rule),
            description=rule.instruction[:200],
        )
        for rule in policy.natural_language
    ]


def specs_for(policy: EffectiveChecksPolicy) -> list[CheckSpec]:
    """Every check this repository could run, in a deterministic order.

    Deterministic because the order decides which producer owns a deduplicated
    finding, and a dedup outcome that depended on dictionary iteration order
    would make two identical runs disagree.

    Includes checks whose mode is ``off``: the runner records those as skipped
    rather than omitting them, because "this check is off" and "this check does
    not exist in this version" are different facts and a dashboard that cannot
    tell them apart cannot be trusted about coverage.
    """
    return [*native_specs(), *context_specs(), *tool_specs(policy), *natural_language_specs(policy)]


def catalog(policy: EffectiveChecksPolicy) -> list[dict]:
    """The registered checks, for the dashboard's catalog view."""
    return [
        {
            "check_id": spec.check_id,
            "title": spec.title,
            "origin": spec.origin,
            "version": spec.version,
            "description": spec.description,
            "mode": policy.mode_for(spec.check_id),
            "config_digest": policy.config_digest_for(spec.check_id),
        }
        for spec in specs_for(policy)
    ]
