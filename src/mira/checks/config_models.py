"""Configuration for pre-merge checks (Phase 6).

Kept in its own module rather than in :mod:`mira.config` because it is large
and because everything in it obeys one rule that is easier to state — and to
audit — with a file boundary around it:

**Nothing in a pull request can reach any value defined here.** Not a check's
mode, not a natural-language rule's instruction, not a tool's argument vector,
not a path glob. Configuration comes from the deployment's ``mira.yaml``, the
admin-editable overrides in the dashboard database, and the per-organisation
and per-repository entries below. A title, a description, a diff, a CI log and
a ticket body are *inputs to* a check; none of them is an input to its policy.

That is why the tool adapters take an argument **list** and a name from a
closed allowlist rather than a command string: there is no shell to inject
into, and there is no path from a repository to the name of the binary that
gets executed.
"""

from __future__ import annotations

import re
from typing import Any

from pydantic import BaseModel, Field, field_validator

# Every deterministic tool Mira knows how to drive. A closed set, checked at
# config load: an operator can enable one of these and configure it, and cannot
# name a binary Mira has never heard of. Adding a tool means adding an adapter
# and a reviewed entry here, which is the point.
TOOL_ALLOWLIST: frozenset[str] = frozenset({"semgrep", "ruff", "eslint", "gitleaks", "osv"})

_CHECK_ID = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")

_MODES = {"off", "warning", "error"}


def _valid_mode_or_none(value: str | None, label: str) -> str | None:
    if value is not None and value not in _MODES:
        raise ValueError(f"{label} must be one of {sorted(_MODES)}, got {value!r}")
    return value


def _validate_globs(patterns: list[str] | None, label: str) -> list[str] | None:
    """Compile path globs at config load, never at check time.

    A pattern the matcher cannot read has no safe runtime interpretation:
    ignoring it silently widens or narrows a rule's scope without saying so.
    Failing the config load is the only honest reading.
    """
    if not patterns:
        return patterns
    from mira.gate.paths import PatternError, compile_pattern

    for pattern in patterns:
        try:
            compile_pattern(pattern)
        except PatternError as exc:
            raise ValueError(f"{label}: {exc}") from exc
    return patterns


class NaturalLanguageCheck(BaseModel):
    """One check written as an instruction rather than as code.

    The instruction is deployment configuration and is the *only* thing that
    sets this check's policy. The pull request supplies the material the
    instruction is evaluated against, and supplies nothing else — which is why
    the runner frames every piece of that material as untrusted data and why
    the model's answer has no field capable of changing a mode, a scope or a
    rule.
    """

    id: str
    title: str = ""
    instruction: str
    # Where this rule applies. Empty means every changed file; a rule whose
    # globs match nothing in the diff is skipped as out of scope, which is an
    # answer rather than a silence.
    paths: list[str] = Field(default_factory=list)
    # ``None`` inherits the policy's default mode.
    mode: str | None = None
    # Bumped by the operator when the instruction's meaning changes, so older
    # results stay attributable to the rule that produced them.
    version: str = "1"

    @field_validator("id")
    @classmethod
    def _valid_id(cls, v: str) -> str:
        if not _CHECK_ID.match(v or ""):
            raise ValueError(
                f"checks.natural_language[].id {v!r} must be lowercase letters, digits, "
                "'.', '_' or '-' (max 64 characters)"
            )
        return v

    @field_validator("instruction")
    @classmethod
    def _valid_instruction(cls, v: str) -> str:
        if not (v or "").strip():
            raise ValueError("checks.natural_language[].instruction must not be empty")
        return v

    @field_validator("mode")
    @classmethod
    def _valid_mode(cls, v: str | None) -> str | None:
        return _valid_mode_or_none(v, "checks.natural_language[].mode")

    @field_validator("paths")
    @classmethod
    def _valid_paths(cls, v: list[str]) -> list[str]:
        return _validate_globs(v, "checks.natural_language[].paths") or []

    @property
    def check_id(self) -> str:
        return f"nl.{self.id}"


class CheckToolConfig(BaseModel):
    """One deterministic analyser, and the only things it may be told.

    ``name`` comes from :data:`TOOL_ALLOWLIST` and nothing else. ``args`` is an
    argument *list* appended to the adapter's own argv — there is no shell, so
    there is nothing to inject into, and it still comes from deployment
    configuration rather than from a repository.
    """

    name: str
    enabled: bool = True
    mode: str | None = None
    # Extra arguments, appended verbatim to the adapter's argv.
    args: list[str] = Field(default_factory=list)
    # A config file *inside the repository* the tool should be pointed at.
    # Repository-relative and validated as such: a rule file a team commits is
    # data the team already reviews, and an absolute path would let a
    # deployment read files the tool has no business reading.
    config_path: str = ""
    # Substring the tool's own ``--version`` output must contain. Empty accepts
    # whatever is installed; set, a mismatch is a *skip* with the versions
    # named, never a silent run under rules nobody reviewed.
    require_version: str = ""
    # Restrict this tool to the changed files matching these globs.
    paths: list[str] = Field(default_factory=list)
    timeout_seconds: float | None = Field(default=None, gt=0, le=1800)

    @field_validator("name")
    @classmethod
    def _valid_name(cls, v: str) -> str:
        name = (v or "").strip().lower()
        if name not in TOOL_ALLOWLIST:
            raise ValueError(
                f"checks.tools[].name {v!r} is not an allowlisted analyser; "
                f"choose one of {sorted(TOOL_ALLOWLIST)}"
            )
        return name

    @field_validator("args")
    @classmethod
    def _valid_args(cls, v: list[str]) -> list[str]:
        for part in v:
            if not isinstance(part, str) or not part:
                raise ValueError("checks.tools[].args must contain only non-empty strings")
        return v

    @field_validator("config_path")
    @classmethod
    def _valid_config_path(cls, v: str) -> str:
        path = (v or "").strip()
        if not path:
            return ""
        if path.startswith(("/", "\\")) or ".." in path.replace("\\", "/").split("/"):
            raise ValueError(
                "checks.tools[].config_path must be a repository-relative path "
                "without '..' segments"
            )
        if re.match(r"^[A-Za-z]:", path):
            raise ValueError("checks.tools[].config_path must not be an absolute path")
        return path

    @field_validator("mode")
    @classmethod
    def _valid_mode(cls, v: str | None) -> str | None:
        return _valid_mode_or_none(v, "checks.tools[].mode")

    @field_validator("paths")
    @classmethod
    def _valid_paths(cls, v: list[str]) -> list[str]:
        return _validate_globs(v, "checks.tools[].paths") or []

    @property
    def check_id(self) -> str:
        return f"tool.{self.name}"


class TicketContextConfig(BaseModel):
    """Validating the issue a pull request claims to be about.

    Optional in the strongest sense: ``provider: "none"`` disables every
    outbound lookup, and the default ``"auto"`` only ever asks the hosting
    platform Mira is already talking to. No third-party tracker is required,
    and the adapter interface exists so one can be added without this module
    learning anything about it.
    """

    # "auto" — ask the pull request's own platform.
    # "none" — never look anything up; the reference check still runs offline.
    # Any other value names a registered adapter.
    provider: str = "auto"
    # A pull request with no issue reference at all.
    require_reference: bool = True
    # A referenced issue with no parseable acceptance criteria.
    require_acceptance_criteria: bool = False
    # Extra regexes for project-specific ticket shapes (``ACME-123``). Compiled
    # at load; a pattern that will not compile fails the config, not the check.
    reference_patterns: list[str] = Field(default_factory=list)
    # Labels that excuse a pull request from needing a ticket at all.
    exempt_labels: list[str] = Field(default_factory=lambda: ["no-ticket", "chore", "dependencies"])
    mode: str | None = None
    timeout_seconds: float = Field(default=15.0, gt=0, le=120)

    @field_validator("reference_patterns")
    @classmethod
    def _valid_patterns(cls, v: list[str]) -> list[str]:
        for pattern in v:
            try:
                re.compile(pattern)
            except re.error as exc:
                raise ValueError(
                    f"checks.ticket.reference_patterns: {pattern!r} is not a valid regex ({exc})"
                ) from exc
        return v

    @field_validator("mode")
    @classmethod
    def _valid_mode(cls, v: str | None) -> str | None:
        return _valid_mode_or_none(v, "checks.ticket.mode")


class CIContextConfig(BaseModel):
    """Reading what CI said, and how much of it is allowed near a model.

    Every limit here is a security control, not a performance knob. A CI log is
    the most attacker-reachable text in the whole system — anybody who can open
    a pull request can print anything they like into it — so it is truncated,
    redacted and framed as untrusted data before it is summarised, and the
    summary that comes back cannot change a check's mode or a run's verdict.
    """

    mode: str | None = None
    # Failing jobs whose logs are fetched. A cap, not a page.
    max_jobs: int = Field(default=3, ge=1, le=20)
    # Bytes kept per job log, taken from the *end* — a build failure is at the
    # bottom of the file, not the top.
    max_log_bytes: int = Field(default=16_000, ge=500, le=1_000_000)
    # Lines of a log quoted as evidence per job.
    max_evidence_lines: int = Field(default=20, ge=1, le=200)
    # Ask a model to summarise the failure in prose. Off by default: the job
    # name, step name and quoted log lines are already evidence, and a
    # deployment that does not want its CI output leaving the box does not
    # have to send it anywhere.
    summarize_with_llm: bool = False
    timeout_seconds: float = Field(default=30.0, gt=0, le=300)

    @field_validator("mode")
    @classmethod
    def _valid_mode(cls, v: str | None) -> str | None:
        return _valid_mode_or_none(v, "checks.ci.mode")


class ChecksScopePolicy(BaseModel):
    """Overrides for one organisation or one repository.

    Set only what differs. ``None`` means "inherit"; a list set to ``[]`` means
    "explicitly empty", which is how a scope opts out of an inherited
    requirement rather than inheriting it forever.

    Organisations are keyed by owner under ``checks.organizations``;
    repositories by ``owner/repo`` under ``checks.repositories``. A repository
    entry is layered over its organisation's, which is layered over the global
    block — three layers, resolved once, hashed into every run.
    """

    enabled: bool | None = None
    default_mode: str | None = None
    # Per-check overrides, keyed by check id. Merged with the inherited mapping
    # rather than replacing it, because a repository that wants one check
    # louder should not have to restate the other twelve.
    modes: dict[str, str] = Field(default_factory=dict)
    # Replaces the inherited list when set; ``[]`` genuinely means "none here".
    natural_language: list[NaturalLanguageCheck] | None = None
    # Merged by tool name over the inherited list, so a repository can disable
    # one analyser or pin its config without restating the rest.
    tools: list[CheckToolConfig] | None = None
    max_concurrency: int | None = Field(default=None, ge=1, le=16)
    check_timeout_seconds: float | None = Field(default=None, gt=0, le=1800)
    total_timeout_seconds: float | None = Field(default=None, gt=0, le=3600)
    publish_status: bool | None = None
    comment: bool | None = None

    @field_validator("default_mode")
    @classmethod
    def _valid_default_mode(cls, v: str | None) -> str | None:
        return _valid_mode_or_none(v, "checks scope default_mode")

    @field_validator("modes")
    @classmethod
    def _valid_modes(cls, v: dict[str, str]) -> dict[str, str]:
        return _validate_mode_table(v, "checks scope modes")


def _validate_mode_table(table: dict[str, str], label: str) -> dict[str, str]:
    """Reject an unreadable mode table at load, never at check time.

    A mode nobody can parse has no safe runtime reading: treating it as ``off``
    silently removes a check an operator believes is blocking, and treating it
    as ``error`` blocks every merge in the install on a typo.
    """
    for check_id, mode in (table or {}).items():
        if not _CHECK_ID.match(str(check_id)):
            raise ValueError(f"{label}: {check_id!r} is not a valid check id")
        if mode not in _MODES:
            raise ValueError(f"{label}[{check_id}] must be one of {sorted(_MODES)}, got {mode!r}")
    return table


class ChecksConfig(BaseModel):
    """The pre-merge check framework (Phase 6).

    Off by default. A check that blocks a merge is a change to a team's
    workflow, so turning the framework on is a decision a deployment makes
    deliberately — and the recommended first step is leaving ``default_mode``
    at ``warning``, which reports everything and blocks nothing.

    Modes, per check:

      ``off``      — the check does not run. Recorded as skipped, not as a pass.
      ``warning``  — the check runs and reports. It never blocks a merge.
      ``error``    — the check runs, reports, and a violation *or* an inability
                     to answer stops the gate. Fail closed, in that order.
    """

    enabled: bool = False
    # Hard global disable, independent of ``enabled``, of every mode and of
    # every per-scope override. Exists so an operator can stop every check in
    # the install in one edit during an incident without reconstructing the
    # policy afterwards.
    kill_switch: bool = False
    # Recorded with every run. Bump it when changing policy semantics so old
    # runs stay attributable to the policy that produced them.
    policy_version: str = "checks-v1"

    # Mode for any check without an explicit entry in ``modes``.
    default_mode: str = "warning"
    # Per-check overrides, keyed by check id (``native.tests``, ``tool.ruff``,
    # ``nl.my-rule``, ``context.ci``).
    modes: dict[str, str] = Field(default_factory=dict)

    # ── Budget ───────────────────────────────────────────────────────────
    # Checks that may run at once. Two by default because the reference
    # deployment is a four-core Orange Pi also serving webhooks, and a linter
    # fan-out that saturates it turns every review into a timeout.
    max_concurrency: int = Field(default=2, ge=1, le=16)
    # Wall-clock ceiling for one check. Exceeding it is a `timeout` result,
    # which is never a violation and never a pass.
    check_timeout_seconds: float = Field(default=60.0, gt=0, le=1800)
    # Ceiling for the whole run. Checks that never got to start because of it
    # are recorded as skipped with the budget named — and still count as
    # unanswered, so a blocking check cannot be satisfied by running out of
    # time.
    total_timeout_seconds: float = Field(default=300.0, gt=0, le=3600)
    # Evidence items kept per check. Evidence is quoted repository text, and a
    # result row is an audit record rather than a second copy of the diff.
    max_evidence_per_check: int = Field(default=10, ge=1, le=100)

    # ── Checks ───────────────────────────────────────────────────────────
    natural_language: list[NaturalLanguageCheck] = Field(default_factory=list)
    tools: list[CheckToolConfig] = Field(default_factory=list)
    ticket: TicketContextConfig = Field(default_factory=TicketContextConfig)
    ci: CIContextConfig = Field(default_factory=CIContextConfig)

    # ── Announcing ───────────────────────────────────────────────────────
    # Publish the run as a check run / commit status where the provider
    # supports one. Neutral while every check is in warning mode.
    publish_status: bool = True
    # Also post the summary as a PR comment, updated in place.
    comment: bool = False

    # ── Inheritance ──────────────────────────────────────────────────────
    organizations: dict[str, ChecksScopePolicy] = Field(default_factory=dict)
    repositories: dict[str, ChecksScopePolicy] = Field(default_factory=dict)

    @field_validator("default_mode")
    @classmethod
    def _valid_default_mode(cls, v: str) -> str:
        if v not in _MODES:
            raise ValueError(f"checks.default_mode must be one of {sorted(_MODES)}, got {v!r}")
        return v

    @field_validator("modes")
    @classmethod
    def _valid_modes(cls, v: dict[str, str]) -> dict[str, str]:
        return _validate_mode_table(v, "checks.modes")

    @field_validator("natural_language")
    @classmethod
    def _unique_rule_ids(cls, v: list[NaturalLanguageCheck]) -> list[NaturalLanguageCheck]:
        seen: set[str] = set()
        for rule in v:
            if rule.id in seen:
                raise ValueError(
                    f"checks.natural_language: duplicate rule id {rule.id!r}; ids are the "
                    "identity a result is recorded under and cannot repeat"
                )
            seen.add(rule.id)
        return v

    @field_validator("tools")
    @classmethod
    def _unique_tool_names(cls, v: list[CheckToolConfig]) -> list[CheckToolConfig]:
        seen: set[str] = set()
        for tool in v:
            if tool.name in seen:
                raise ValueError(f"checks.tools: {tool.name!r} is configured more than once")
            seen.add(tool.name)
        return v


def scope_defaults() -> dict[str, Any]:  # pragma: no cover - documentation helper
    """The shape an admin override for this section may take."""
    return ChecksConfig().model_dump()
