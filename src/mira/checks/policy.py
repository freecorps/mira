"""Resolving the check policy that actually applies to one repository.

Three layers, always in this order and never any other:

1. the global ``checks`` block — the deployment's own defaults;
2. ``checks.organizations[<owner>]`` — what an organisation standardises on;
3. ``checks.repositories[<owner>/<repo>]`` — what one repository needs
   differently.

Each layer may only be reached from deployment configuration: the ``mira.yaml``
the operator wrote, the admin-editable overrides in the dashboard, and nothing
else. A pull request supplies material for checks to look at and supplies
nothing to this file.

Two merge rules are worth stating because they are the ones people get wrong.

**Modes merge; lists replace.** A repository that wants one check louder should
not have to restate the other twelve, so ``modes`` is a mapping merged key by
key. A repository that sets ``natural_language: []`` genuinely means "no
language rules here", so lists replace rather than concatenate — the same
``None``-inherits/``[]``-is-empty sentinel the gate and autofix policies use.

**Tools merge by name.** They sit between the two: a repository usually wants to
disable one analyser or pin its config, not redeclare the set. Setting the list
to ``[]`` still means "no tools here", because the sentinel has to mean the same
thing everywhere or it means nothing.

The resolved policy is frozen and hashed into every run key, so a result
recorded under one policy can never be reused as though it had been reached
under another.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Any

from mira.checks.config_models import (
    ChecksConfig,
    ChecksScopePolicy,
    CheckToolConfig,
    CIContextConfig,
    NaturalLanguageCheck,
    TicketContextConfig,
)


def repo_key(owner: str, repo: str) -> str:
    return f"{owner}/{repo}".lower()


def _pick(override: Any, base: Any) -> Any:
    """``None`` inherits; anything else — including ``[]`` — overrides."""
    return base if override is None else override


@dataclass(frozen=True)
class EffectiveChecksPolicy:
    """The fully resolved check policy for one repository.

    Frozen because every run quotes it: if the policy could be mutated between
    running a check and persisting its result, the audit trail would describe a
    policy that never ran.
    """

    enabled: bool = False
    declared_version: str = "checks-v1"
    owner: str = ""
    repo: str = ""

    default_mode: str = "warning"
    modes: tuple[tuple[str, str], ...] = ()

    natural_language: tuple[NaturalLanguageCheck, ...] = ()
    tools: tuple[CheckToolConfig, ...] = ()
    ticket: TicketContextConfig = field(default_factory=TicketContextConfig)
    ci: CIContextConfig = field(default_factory=CIContextConfig)

    max_concurrency: int = 2
    check_timeout_seconds: float = 60.0
    total_timeout_seconds: float = 300.0
    max_evidence_per_check: int = 10

    publish_status: bool = True
    comment: bool = False

    # Set when the global kill switch turned everything off. Recorded rather
    # than inferred, so a run row shows that a switch — not a policy edit —
    # made the framework inert.
    killed: bool = False

    @property
    def active(self) -> bool:
        """Whether any check runs at all for this repository."""
        return self.enabled and not self.killed

    def mode_for(self, check_id: str) -> str:
        """The mode this check runs under, after every layer has been applied."""
        table = dict(self.modes)
        if check_id in table:
            return table[check_id]
        return self.default_mode

    def _payload(self) -> dict[str, Any]:
        """Every resolved field, without the derived version.

        Split out so :attr:`digest` can hash the policy without hashing a value
        computed from the hash.
        """
        return {
            "enabled": self.enabled,
            "killed": self.killed,
            "declared_version": self.declared_version,
            "owner": self.owner,
            "repo": self.repo,
            "default_mode": self.default_mode,
            "modes": dict(self.modes),
            "natural_language": [rule.model_dump() for rule in self.natural_language],
            "tools": [tool.model_dump() for tool in self.tools],
            "ticket": self.ticket.model_dump(),
            "ci": self.ci.model_dump(),
            "max_concurrency": self.max_concurrency,
            "check_timeout_seconds": self.check_timeout_seconds,
            "total_timeout_seconds": self.total_timeout_seconds,
            "max_evidence_per_check": self.max_evidence_per_check,
            "publish_status": self.publish_status,
            "comment": self.comment,
        }

    def as_dict(self) -> dict[str, Any]:
        return {**self._payload(), "version": self.version}

    @property
    def digest(self) -> str:
        """Content hash of everything that can change a result.

        Excludes the fields that only govern *how* a run is announced
        (``publish_status``, ``comment``) and which repository it was for:
        changing those should not invalidate results that were already
        correctly produced.
        """
        payload = self._payload()
        for key in ("publish_status", "comment", "owner", "repo"):
            payload.pop(key, None)
        blob = repr(sorted(payload.items(), key=lambda item: item[0]))
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:12]

    @property
    def version(self) -> str:
        """What gets persisted with a run: declared label plus content hash."""
        return f"{self.declared_version}+{self.digest}"

    def config_digest_for(self, check_id: str) -> str:
        """Content hash of the configuration *this one check* ran under.

        Per-check rather than per-policy, so two results are comparable exactly
        when the rules behind them were the same. Changing a natural-language
        rule invalidates that rule's history and leaves ``native.tests`` alone,
        which is what makes "has this check regressed?" answerable at all.
        """
        payload: dict[str, Any] = {"mode": self.mode_for(check_id)}
        for rule in self.natural_language:
            if rule.check_id == check_id:
                payload["rule"] = rule.model_dump()
        for tool in self.tools:
            if tool.check_id == check_id:
                payload["tool"] = tool.model_dump()
        if check_id.startswith("context.ticket") or check_id == "context.acceptance_criteria":
            payload["ticket"] = self.ticket.model_dump()
        if check_id == "context.ci":
            payload["ci"] = self.ci.model_dump()
        blob = repr(sorted(payload.items(), key=lambda item: item[0]))
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:12]

    def tool_for(self, name: str) -> CheckToolConfig | None:
        for tool in self.tools:
            if tool.name == name:
                return tool
        return None


def _merge_tools(
    base: tuple[CheckToolConfig, ...], override: list[CheckToolConfig] | None
) -> tuple[CheckToolConfig, ...]:
    """Layer one scope's tool list over the inherited one, keyed by name.

    ``None`` inherits and ``[]`` empties, exactly as every other list-shaped
    override does. Anything else is merged by name so that disabling one
    analyser does not silently drop the others.
    """
    if override is None:
        return base
    if not override:
        return ()
    merged = {tool.name: tool for tool in base}
    for tool in override:
        merged[tool.name] = tool
    return tuple(merged[name] for name in sorted(merged))


def _layer(
    scope: ChecksScopePolicy | None,
    *,
    enabled: bool,
    default_mode: str,
    modes: dict[str, str],
    natural_language: tuple[NaturalLanguageCheck, ...],
    tools: tuple[CheckToolConfig, ...],
    max_concurrency: int,
    check_timeout_seconds: float,
    total_timeout_seconds: float,
    publish_status: bool,
    comment: bool,
) -> dict[str, Any]:
    """Apply one scope over the accumulated values, returning the new ones."""
    scope = scope or ChecksScopePolicy()
    merged_modes = {**modes, **(scope.modes or {})}
    return {
        "enabled": bool(_pick(scope.enabled, enabled)),
        "default_mode": str(_pick(scope.default_mode, default_mode)),
        "modes": merged_modes,
        "natural_language": (
            natural_language if scope.natural_language is None else tuple(scope.natural_language)
        ),
        "tools": _merge_tools(tools, scope.tools),
        "max_concurrency": int(_pick(scope.max_concurrency, max_concurrency)),
        "check_timeout_seconds": float(_pick(scope.check_timeout_seconds, check_timeout_seconds)),
        "total_timeout_seconds": float(_pick(scope.total_timeout_seconds, total_timeout_seconds)),
        "publish_status": bool(_pick(scope.publish_status, publish_status)),
        "comment": bool(_pick(scope.comment, comment)),
    }


def resolve_policy(config: ChecksConfig, owner: str = "", repo: str = "") -> EffectiveChecksPolicy:
    """Layer organisation and repository entries over the global check policy.

    The kill switch is applied here rather than at every call site, so there is
    exactly one place where "checks are off everywhere" is decided and no
    caller can forget to consult it.
    """
    organizations = {key.lower(): value for key, value in (config.organizations or {}).items()}
    repositories = {key.lower(): value for key, value in (config.repositories or {}).items()}

    values: dict[str, Any] = {
        "enabled": config.enabled,
        "default_mode": config.default_mode,
        "modes": dict(config.modes or {}),
        "natural_language": tuple(config.natural_language),
        "tools": tuple(config.tools),
        "max_concurrency": config.max_concurrency,
        "check_timeout_seconds": config.check_timeout_seconds,
        "total_timeout_seconds": config.total_timeout_seconds,
        "publish_status": config.publish_status,
        "comment": config.comment,
    }
    if owner:
        values = _layer(organizations.get(owner.lower()), **values)
    if owner and repo:
        values = _layer(repositories.get(repo_key(owner, repo)), **values)

    killed = bool(config.kill_switch)
    return EffectiveChecksPolicy(
        enabled=bool(values["enabled"]) and not killed,
        killed=killed,
        declared_version=config.policy_version,
        owner=owner,
        repo=repo,
        default_mode=str(values["default_mode"]),
        modes=tuple(sorted(values["modes"].items())),
        natural_language=tuple(values["natural_language"]),
        tools=tuple(values["tools"]),
        ticket=config.ticket,
        ci=config.ci,
        max_concurrency=int(values["max_concurrency"]),
        check_timeout_seconds=float(values["check_timeout_seconds"]),
        total_timeout_seconds=float(values["total_timeout_seconds"]),
        max_evidence_per_check=config.max_evidence_per_check,
        publish_status=bool(values["publish_status"]),
        comment=bool(values["comment"]),
    )
