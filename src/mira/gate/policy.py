"""Resolving the policy that actually applies to one repository.

Gate policy comes from deployment configuration only: the global ``gate``
block, the admin-editable overrides stored in the dashboard database, and the
per-repository entries under ``gate.repositories``. A pull request cannot reach
any of it. That is not an incidental property of where the file happens to be
read from — it is the point, and it is why the resolved policy is hashed into
every decision key: a decision made under one policy can never be reused as if
it had been made under another.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Any

from mira.config import GateConfig, GateRepoPolicy, RiskWeights
from mira.gate.paths import DEFAULT_GENERATED_PATTERNS, DEFAULT_PROTECTED_PATTERNS


def repo_key(owner: str, repo: str) -> str:
    return f"{owner}/{repo}".lower()


@dataclass(frozen=True)
class EffectivePolicy:
    """The fully resolved policy for one repository.

    Frozen because a decision quotes it: if the policy could be mutated between
    scoring and persisting, the audit trail would describe a policy that never
    ran.
    """

    mode: str = "off"
    enabled: bool = True
    declared_version: str = "gate-v1"
    owner: str = ""
    repo: str = ""

    allowed_base_branches: tuple[str, ...] = ()
    blocked_base_branches: tuple[str, ...] = ()
    required_labels: tuple[str, ...] = ()
    blocked_labels: tuple[str, ...] = ()
    allowed_authors: tuple[str, ...] = ()
    blocked_authors: tuple[str, ...] = ()
    allowed_author_associations: tuple[str, ...] = ()
    skip_draft_prs: bool = True
    max_changed_files: int = 20
    max_changed_lines: int = 500
    generated_paths: tuple[str, ...] = ()
    size_excludes_generated: bool = True

    protected_paths: tuple[str, ...] = ()
    codeowners: str = "off"

    require_ci_success: bool = True
    require_all_files_reviewed: bool = True
    require_index_ready: bool = True
    approve_max_severity: str = "suggestion"
    require_checks_pass: bool = True

    risk_threshold: int = 25
    risk_medium_at: int = 20
    risk_high_at: int = 50
    weights: RiskWeights = field(default_factory=RiskWeights)

    request_changes_on_blockers: bool = False
    publish_status: bool = True
    comment: bool = False

    allow_overrides: bool = True
    allow_approval_override: bool = False
    override_admins: tuple[str, ...] = ()

    timeout_seconds: float = 20.0

    @property
    def active(self) -> bool:
        """Whether the gate runs at all for this repository."""
        return self.enabled and self.mode in {"shadow", "enforce"}

    @property
    def enforcing(self) -> bool:
        return self.enabled and self.mode == "enforce"

    def _payload(self) -> dict[str, Any]:
        """Every resolved field, without the derived version.

        Split out so `digest` can hash the policy without hashing a value
        computed from the hash.
        """
        return {
            "mode": self.mode,
            "enabled": self.enabled,
            "declared_version": self.declared_version,
            "owner": self.owner,
            "repo": self.repo,
            "allowed_base_branches": list(self.allowed_base_branches),
            "blocked_base_branches": list(self.blocked_base_branches),
            "required_labels": list(self.required_labels),
            "blocked_labels": list(self.blocked_labels),
            "allowed_authors": list(self.allowed_authors),
            "blocked_authors": list(self.blocked_authors),
            "allowed_author_associations": list(self.allowed_author_associations),
            "skip_draft_prs": self.skip_draft_prs,
            "max_changed_files": self.max_changed_files,
            "max_changed_lines": self.max_changed_lines,
            "generated_paths": list(self.generated_paths),
            "size_excludes_generated": self.size_excludes_generated,
            "protected_paths": list(self.protected_paths),
            "codeowners": self.codeowners,
            "require_ci_success": self.require_ci_success,
            "require_all_files_reviewed": self.require_all_files_reviewed,
            "require_index_ready": self.require_index_ready,
            "approve_max_severity": self.approve_max_severity,
            "require_checks_pass": self.require_checks_pass,
            "risk_threshold": self.risk_threshold,
            "risk_medium_at": self.risk_medium_at,
            "risk_high_at": self.risk_high_at,
            "weights": self.weights.model_dump(),
            "request_changes_on_blockers": self.request_changes_on_blockers,
            "publish_status": self.publish_status,
            "comment": self.comment,
            "allow_overrides": self.allow_overrides,
            "allow_approval_override": self.allow_approval_override,
            "override_admins": list(self.override_admins),
            "timeout_seconds": self.timeout_seconds,
        }

    def as_dict(self) -> dict[str, Any]:
        return {**self._payload(), "version": self.version}

    @property
    def digest(self) -> str:
        """Content hash of everything that can change a decision.

        Excludes the fields that only govern *how* a decision is announced
        (`publish_status`, `comment`) and who may override it — changing those
        should not invalidate a decision that was already correctly reached.
        """
        payload = self._payload()
        for key in (
            "publish_status",
            "comment",
            "allow_overrides",
            "allow_approval_override",
            "override_admins",
            "timeout_seconds",
            "owner",
            "repo",
        ):
            payload.pop(key, None)
        blob = repr(sorted(payload.items(), key=lambda item: item[0]))
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:12]

    @property
    def version(self) -> str:
        """What gets persisted with a decision: declared label plus content hash."""
        return f"{self.declared_version}+{self.digest}"


def _pick(override: Any, base: Any) -> Any:
    """``None`` inherits; anything else — including ``[]`` — overrides."""
    return base if override is None else override


def resolve_policy(config: GateConfig, owner: str = "", repo: str = "") -> EffectivePolicy:
    """Layer the per-repository entry (if any) over the global gate policy.

    The kill switch is applied here rather than at every call site, so there is
    exactly one place where "the gate is off everywhere" is decided and no
    caller can forget to consult it.
    """
    entry: GateRepoPolicy | None = None
    if owner and repo:
        table = {key.lower(): value for key, value in (config.repositories or {}).items()}
        entry = table.get(repo_key(owner, repo))
    if entry is None:
        entry = GateRepoPolicy()

    enabled = _pick(entry.enabled, True)
    mode = _pick(entry.mode, config.mode)
    if config.kill_switch:
        # Recorded as mode "off" *and* disabled, so a decision row shows both
        # that the gate was inert and that a switch — not a policy edit — did it.
        enabled = False
        mode = "off"

    base_protected = (
        list(config.protected_paths)
        if config.protected_paths is not None
        else list(DEFAULT_PROTECTED_PATTERNS)
    )
    if entry.protected_paths is not None:
        base_protected = list(entry.protected_paths)
    protected = [*base_protected, *config.extra_protected_paths, *entry.extra_protected_paths]

    generated = (
        list(config.generated_paths)
        if config.generated_paths is not None
        else list(DEFAULT_GENERATED_PATTERNS)
    )

    return EffectivePolicy(
        mode=mode,
        enabled=bool(enabled),
        declared_version=config.policy_version,
        owner=owner,
        repo=repo,
        allowed_base_branches=tuple(
            _pick(entry.allowed_base_branches, config.allowed_base_branches)
        ),
        blocked_base_branches=tuple(
            _pick(entry.blocked_base_branches, config.blocked_base_branches)
        ),
        required_labels=tuple(_pick(entry.required_labels, config.required_labels)),
        blocked_labels=tuple(_pick(entry.blocked_labels, config.blocked_labels)),
        allowed_authors=tuple(config.allowed_authors),
        blocked_authors=tuple(config.blocked_authors),
        allowed_author_associations=tuple(
            _pick(entry.allowed_author_associations, config.allowed_author_associations)
        ),
        skip_draft_prs=config.skip_draft_prs,
        max_changed_files=int(_pick(entry.max_changed_files, config.max_changed_files)),
        max_changed_lines=int(_pick(entry.max_changed_lines, config.max_changed_lines)),
        generated_paths=tuple(generated),
        size_excludes_generated=config.size_excludes_generated,
        protected_paths=tuple(dict.fromkeys(protected)),
        codeowners=str(_pick(entry.codeowners, config.codeowners)),
        require_ci_success=config.require_ci_success,
        require_all_files_reviewed=config.require_all_files_reviewed,
        require_index_ready=config.require_index_ready,
        approve_max_severity=config.approve_max_severity,
        require_checks_pass=bool(_pick(entry.require_checks_pass, config.require_checks_pass)),
        risk_threshold=int(_pick(entry.risk_threshold, config.risk_threshold)),
        risk_medium_at=config.risk_medium_at,
        risk_high_at=config.risk_high_at,
        weights=config.weights,
        request_changes_on_blockers=bool(
            _pick(entry.request_changes_on_blockers, config.request_changes_on_blockers)
        ),
        publish_status=config.publish_status,
        comment=config.comment,
        allow_overrides=config.allow_overrides,
        allow_approval_override=config.allow_approval_override,
        override_admins=tuple(config.override_admins),
        timeout_seconds=config.timeout_seconds,
    )
