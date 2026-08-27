"""Resolving the correction policy that actually applies to one repository.

Autofix policy comes from deployment configuration only: the global
``autofix`` block, the admin-editable override stored in the dashboard
database, and the per-repository entries under ``autofix.repositories``. A pull
request cannot reach any of it.

That is not incidental to where the file is read from — it is the point, and it
is why the resolved policy is hashed onto every job: a fix produced under one
policy can never be reused as if it had been produced under another.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Any

from mira.autofix.capabilities import AutofixCapabilities
from mira.config import (
    AutofixConfig,
    AutofixHandoffConfig,
    AutofixRepoPolicy,
    AutofixValidationConfig,
)
from mira.gate.paths import DEFAULT_PROTECTED_PATTERNS


def repo_key(owner: str, repo: str) -> str:
    return f"{owner}/{repo}".lower()


@dataclass(frozen=True)
class EffectivePolicy:
    """The fully resolved correction policy for one repository.

    Frozen because a job quotes it: if the policy could be mutated between
    accepting a request and publishing its result, the audit trail would
    describe a policy that never ran.
    """

    mode: str = "off"
    enabled: bool = True
    declared_version: str = "autofix-v1"
    owner: str = ""
    repo: str = ""

    require_write_permission: bool = True
    allowed_requesters: tuple[str, ...] = ()
    blocked_requesters: tuple[str, ...] = ()
    allow_unknown_permission: bool = False

    allow_commit_to_pr_branch: bool = False
    branch_prefix: str = "mira/fix"
    protected_paths: tuple[str, ...] = ()
    restrict_to_changed_files: bool = True
    allow_new_files: bool = False

    max_files: int = 3
    max_lines: int = 120
    max_patch_bytes: int = 40_000
    max_fixes_per_request: int = 3
    max_concurrent_jobs: int = 2
    min_severity_for_fix_all: str = "warning"
    max_attempts: int = 2
    max_ci_retries: int = 1
    job_timeout_seconds: float = 900.0
    max_context_bytes: int = 60_000

    inline_worker: bool = True
    worker_poll_seconds: float = 5.0
    lease_seconds: float = 900.0
    retry_backoff_seconds: float = 60.0

    validation: AutofixValidationConfig = field(default_factory=AutofixValidationConfig)
    handoff: AutofixHandoffConfig = field(default_factory=AutofixHandoffConfig)

    @property
    def active(self) -> bool:
        """Whether autofix responds to a request at all for this repository."""
        return self.enabled and self.mode in {"suggest", "on"}

    @property
    def writing(self) -> bool:
        """Whether a branch, a commit or a pull request may be created."""
        return self.enabled and self.mode == "on"

    @property
    def handoff_enabled(self) -> bool:
        return bool(self.handoff.adapter)

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
            "require_write_permission": self.require_write_permission,
            "allowed_requesters": list(self.allowed_requesters),
            "blocked_requesters": list(self.blocked_requesters),
            "allow_unknown_permission": self.allow_unknown_permission,
            "allow_commit_to_pr_branch": self.allow_commit_to_pr_branch,
            "branch_prefix": self.branch_prefix,
            "protected_paths": list(self.protected_paths),
            "restrict_to_changed_files": self.restrict_to_changed_files,
            "allow_new_files": self.allow_new_files,
            "max_files": self.max_files,
            "max_lines": self.max_lines,
            "max_patch_bytes": self.max_patch_bytes,
            "max_fixes_per_request": self.max_fixes_per_request,
            "max_concurrent_jobs": self.max_concurrent_jobs,
            "min_severity_for_fix_all": self.min_severity_for_fix_all,
            "max_attempts": self.max_attempts,
            "max_ci_retries": self.max_ci_retries,
            "job_timeout_seconds": self.job_timeout_seconds,
            "max_context_bytes": self.max_context_bytes,
            "validation": self.validation.model_dump(),
            "handoff": self.handoff.model_dump(),
        }

    def as_dict(self) -> dict[str, Any]:
        return {
            **self._payload(),
            "version": self.version,
            "inline_worker": self.inline_worker,
            "worker_poll_seconds": self.worker_poll_seconds,
            "lease_seconds": self.lease_seconds,
            "retry_backoff_seconds": self.retry_backoff_seconds,
            "active": self.active,
            "writing": self.writing,
        }

    @property
    def digest(self) -> str:
        """Content hash of everything that can change what gets written.

        Excludes the queue's own tuning (`inline_worker`, poll and lease
        intervals) and the owner/repo labels: re-tuning a worker should not
        make an already-generated patch look as if it came from a different
        policy.
        """
        payload = self._payload()
        for key in ("owner", "repo"):
            payload.pop(key, None)
        blob = repr(sorted(payload.items(), key=lambda item: item[0]))
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:12]

    @property
    def version(self) -> str:
        """What gets persisted with a job: declared label plus content hash."""
        return f"{self.declared_version}+{self.digest}"

    def permitted_mode(self, requested: str, capabilities: AutofixCapabilities) -> str:
        """The delivery mode this policy and provider actually allow, or ``""``.

        Returning the empty string rather than silently falling back is the
        point: a maintainer who asked for a commit on the pull request's branch
        and got a stacked pull request instead was not told no, and being told
        no is the whole contract here.
        """
        if requested == "handoff":
            return "handoff" if self.handoff_enabled else ""
        if requested == "pr_branch":
            if not self.allow_commit_to_pr_branch:
                return ""
            return "pr_branch" if capabilities.can_push_to_pr_branch else ""
        return "branch_pr" if capabilities.can_publish else ""


def _pick(override: Any, base: Any) -> Any:
    """``None`` inherits; anything else — including ``[]`` — overrides."""
    return base if override is None else override


def resolve_policy(config: AutofixConfig, owner: str = "", repo: str = "") -> EffectivePolicy:
    """Layer the per-repository entry (if any) over the global autofix policy.

    The kill switch is applied here rather than at every call site, so there is
    exactly one place where "autofix is off everywhere" is decided and no
    caller can forget to consult it.
    """
    entry: AutofixRepoPolicy | None = None
    if owner and repo:
        table = {key.lower(): value for key, value in (config.repositories or {}).items()}
        entry = table.get(repo_key(owner, repo))
    if entry is None:
        entry = AutofixRepoPolicy()

    enabled = _pick(entry.enabled, True)
    mode = _pick(entry.mode, config.mode)
    if config.kill_switch:
        # Recorded as mode "off" *and* disabled, so a job row shows both that
        # autofix was inert and that a switch — not a policy edit — did it.
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

    return EffectivePolicy(
        mode=str(mode),
        enabled=bool(enabled),
        declared_version=config.policy_version,
        owner=owner,
        repo=repo,
        require_write_permission=config.require_write_permission,
        allowed_requesters=tuple(_pick(entry.allowed_requesters, config.allowed_requesters)),
        blocked_requesters=tuple(config.blocked_requesters),
        allow_unknown_permission=config.allow_unknown_permission,
        allow_commit_to_pr_branch=bool(
            _pick(entry.allow_commit_to_pr_branch, config.allow_commit_to_pr_branch)
        ),
        branch_prefix=config.branch_prefix,
        protected_paths=tuple(dict.fromkeys(protected)),
        restrict_to_changed_files=config.restrict_to_changed_files,
        allow_new_files=config.allow_new_files,
        max_files=int(_pick(entry.max_files, config.max_files)),
        max_lines=int(_pick(entry.max_lines, config.max_lines)),
        max_patch_bytes=config.max_patch_bytes,
        max_fixes_per_request=int(_pick(entry.max_fixes_per_request, config.max_fixes_per_request)),
        max_concurrent_jobs=config.max_concurrent_jobs,
        min_severity_for_fix_all=config.min_severity_for_fix_all,
        max_attempts=config.max_attempts,
        max_ci_retries=config.max_ci_retries,
        job_timeout_seconds=config.job_timeout_seconds,
        max_context_bytes=config.max_context_bytes,
        inline_worker=config.inline_worker,
        worker_poll_seconds=config.worker_poll_seconds,
        lease_seconds=config.lease_seconds,
        retry_backoff_seconds=config.retry_backoff_seconds,
        validation=config.validation,
        handoff=config.handoff,
    )
