"""Resolving the triage policy that applies to one repository.

Three layers, in this order and no other:

1. the global ``triage`` block — the deployment's defaults;
2. ``triage.organizations[<owner>]``;
3. ``triage.repositories[<owner>/<repo>]``.

Every layer comes from deployment configuration — the ``mira.yaml`` the
operator wrote and the admin-editable overrides in the dashboard. A pull
request contributes files to look at and contributes nothing here.

The merge rules are the ones the rest of the codebase already uses, so that a
person who has read one policy module has read them all: ``None`` inherits,
anything else overrides, and an explicit ``[]`` means "empty at this scope"
rather than "inherit". Weights are all-or-nothing — a scope that sets them sets
all three — because a half-overridden weighting is a ranking nobody can predict
from reading the configuration.

The resolved policy is frozen and hashed into every run key: a suggestion
recorded under one policy can never be mistaken for one produced under another.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Any

from mira.triage.config_models import (
    TriageConfig,
    TriageScopePolicy,
    TriageWeights,
    _normalize_identity,
)


def repo_key(owner: str, repo: str) -> str:
    return f"{owner}/{repo}".lower()


def _pick(override: Any, base: Any) -> Any:
    """``None`` inherits; anything else — including ``[]`` — overrides."""
    return base if override is None else override


@dataclass(frozen=True)
class EffectiveTriagePolicy:
    """The fully resolved triage policy for one repository."""

    enabled: bool = False
    declared_version: str = "triage-v1"
    owner: str = ""
    repo: str = ""

    comment: bool = True
    max_suggestions: int = 3
    min_score: float = 0.75

    codeowners: bool = True
    history: bool = True
    history_days: int = 180
    history_max_paths: int = 12
    history_max_per_path: int = 20
    history_refresh_hours: float = 168.0

    exclude: tuple[str, ...] = ()
    exclude_bots: bool = True
    bots: tuple[str, ...] = ()

    load_penalty: float = 0.25
    budget_seconds: float = 30.0

    weights: TriageWeights = field(default_factory=TriageWeights)

    # True when the global kill switch — not a policy edit — made triage inert.
    # Recorded rather than inferred, so the run row says which one it was.
    killed: bool = False

    @property
    def active(self) -> bool:
        return self.enabled and not self.killed

    @property
    def signals_enabled(self) -> tuple[str, ...]:
        """The signals this policy asks for, in ranking order."""
        kinds: list[str] = []
        if self.codeowners:
            kinds.append("codeowners")
        if self.history:
            kinds.extend(("authored", "reviewed"))
        return tuple(kinds)

    def excluded(self, identity: str) -> bool:
        """Whether this identity is on the opt-out list.

        Comparison is on the normalized spelling, so ``@Dana``, ``Dana`` and
        ``dana`` are one person — which is what someone who asked not to be
        suggested meant, whichever way they wrote it.
        """
        return _normalize_identity(identity) in self.exclude

    def is_bot(self, identity: str) -> bool:
        """Whether this identity looks like a machine.

        Two rules, both deliberately dumb. The platform's own convention —
        GitHub suffixes app identities with ``[bot]`` — and an operator-supplied
        list for accounts whose names do not say so. No heuristic on ``-bot``
        substrings: ``robot-oncall`` is a team of people, and excluding a human
        for having the wrong name is worse than including one machine.
        """
        if not self.exclude_bots:
            return False
        normalized = _normalize_identity(identity)
        return normalized.endswith("[bot]") or normalized in self.bots

    def _payload(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "killed": self.killed,
            "declared_version": self.declared_version,
            "owner": self.owner,
            "repo": self.repo,
            "comment": self.comment,
            "max_suggestions": self.max_suggestions,
            "min_score": self.min_score,
            "codeowners": self.codeowners,
            "history": self.history,
            "history_days": self.history_days,
            "history_max_paths": self.history_max_paths,
            "history_max_per_path": self.history_max_per_path,
            "history_refresh_hours": self.history_refresh_hours,
            "exclude": list(self.exclude),
            "exclude_bots": self.exclude_bots,
            "bots": list(self.bots),
            "load_penalty": self.load_penalty,
            "budget_seconds": self.budget_seconds,
            "weights": self.weights.model_dump(),
        }

    def as_dict(self) -> dict[str, Any]:
        return {**self._payload(), "version": self.version, "signals": list(self.signals_enabled)}

    @property
    def digest(self) -> str:
        """Content hash of everything that can change a ranking.

        ``comment`` and the repository's name are excluded: how a suggestion is
        published, and which repository it was for, do not change who should
        review the code.
        """
        payload = self._payload()
        for key in ("comment", "owner", "repo"):
            payload.pop(key, None)
        blob = repr(sorted(payload.items(), key=lambda item: item[0]))
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:12]

    @property
    def version(self) -> str:
        return f"{self.declared_version}+{self.digest}"


def _layer(
    scope: TriageScopePolicy | None,
    *,
    enabled: bool,
    comment: bool,
    max_suggestions: int,
    min_score: float,
    codeowners: bool,
    history: bool,
    history_days: int,
    load_penalty: float,
    exclude: tuple[str, ...],
    weights: TriageWeights,
) -> dict[str, Any]:
    scope = scope or TriageScopePolicy()
    return {
        "enabled": bool(_pick(scope.enabled, enabled)),
        "comment": bool(_pick(scope.comment, comment)),
        "max_suggestions": int(_pick(scope.max_suggestions, max_suggestions)),
        "min_score": float(_pick(scope.min_score, min_score)),
        "codeowners": bool(_pick(scope.codeowners, codeowners)),
        "history": bool(_pick(scope.history, history)),
        "history_days": int(_pick(scope.history_days, history_days)),
        "load_penalty": float(_pick(scope.load_penalty, load_penalty)),
        "exclude": tuple(exclude if scope.exclude is None else scope.exclude),
        "weights": weights if scope.weights is None else scope.weights,
    }


def resolve_policy(config: TriageConfig, owner: str = "", repo: str = "") -> EffectiveTriagePolicy:
    """Layer organisation and repository entries over the global policy.

    The kill switch is applied here, once, so no caller can forget to consult
    it and no code path can turn triage back on below this function.
    """
    organizations = {key.lower(): value for key, value in (config.organizations or {}).items()}
    repositories = {key.lower(): value for key, value in (config.repositories or {}).items()}

    values: dict[str, Any] = {
        "enabled": config.enabled,
        "comment": config.comment,
        "max_suggestions": config.max_suggestions,
        "min_score": config.min_score,
        "codeowners": config.codeowners,
        "history": config.history,
        "history_days": config.history_days,
        "load_penalty": config.load_penalty,
        "exclude": tuple(config.exclude or ()),
        "weights": config.weights,
    }
    if owner:
        values = _layer(organizations.get(owner.lower()), **values)
    if owner and repo:
        values = _layer(repositories.get(repo_key(owner, repo)), **values)

    killed = bool(config.kill_switch)
    return EffectiveTriagePolicy(
        # `enabled` is what the configuration says and `killed` is what the
        # switch says; `active` is the only thing that combines them. Folding
        # the switch into `enabled` here would make a killed install report
        # "triage is disabled" on the dashboard and in the CLI, when the true
        # answer — the one an operator needs during an incident — is "enabled,
        # and currently killed".
        enabled=bool(values["enabled"]),
        killed=killed,
        declared_version=config.policy_version,
        owner=owner,
        repo=repo,
        comment=bool(values["comment"]),
        max_suggestions=int(values["max_suggestions"]),
        min_score=float(values["min_score"]),
        codeowners=bool(values["codeowners"]),
        history=bool(values["history"]),
        history_days=int(values["history_days"]),
        history_max_paths=config.history_max_paths,
        history_max_per_path=config.history_max_per_path,
        history_refresh_hours=config.history_refresh_hours,
        exclude=tuple(sorted(set(values["exclude"]))),
        exclude_bots=config.exclude_bots,
        bots=tuple(sorted(set(config.bots or ()))),
        load_penalty=float(values["load_penalty"]),
        budget_seconds=config.budget_seconds,
        weights=values["weights"],
    )
