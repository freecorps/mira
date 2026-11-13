"""Resolving the verification settings that apply to one repository.

Layered exactly like the autofix policy: the global ``review.verify`` block,
then the ``owner/repo`` entry under ``review.verify.repositories``. Both are
deployment configuration; nothing here reads the repository.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from mira.verify.config_models import (
    VerifyCommand,
    VerifyConfig,
    VerifyRepoOverride,
    VerifyTestRunner,
)


def _pick(override: Any, base: Any) -> Any:
    """``None`` inherits; anything else — including ``[]`` — overrides."""
    return base if override is None else override


@dataclass(frozen=True)
class VerifyPolicy:
    """The resolved settings for one review. Frozen: a review quotes it."""

    enabled: bool = False
    isolation: str = "auto"
    extra_read_only_paths: tuple[str, ...] = ()
    max_workspace_mb: int = 200
    setup: tuple[VerifyCommand, ...] = ()
    setup_network: bool = True
    setup_timeout_seconds: float = 300.0
    memory_limit_mb: int = 4096
    cpu_seconds: int = 300
    commands: tuple[VerifyCommand, ...] = ()
    command_timeout_seconds: float = 60.0
    evidence_wait_seconds: float = 90.0
    max_evidence_chars: int = 6_000
    max_tool_findings: int = 10
    test_runners: tuple[VerifyTestRunner, ...] = ()
    categories: frozenset[str] = field(default_factory=frozenset)
    min_severity: str = "warning"
    max_findings: int = 3
    test_timeout_seconds: float = 60.0
    budget_seconds: float = 180.0
    max_concurrent: int = 3
    runs: int = 2

    @property
    def active(self) -> bool:
        """Whether a workspace is worth building: enabled, with something to run."""
        return self.enabled and bool(self.commands or self.reproducing)

    @property
    def reproducing(self) -> bool:
        return bool(self.test_runners) and self.max_findings > 0

    def runner_for(self, path: str) -> VerifyTestRunner | None:
        lower = path.lower()
        for runner in self.test_runners:
            if any(lower.endswith(ext) for ext in runner.extensions):
                return runner
        return None


def resolve_verify_policy(config: VerifyConfig, owner: str = "", repo: str = "") -> VerifyPolicy:
    entry: VerifyRepoOverride | None = None
    if owner and repo:
        table = {key.lower(): value for key, value in (config.repositories or {}).items()}
        entry = table.get(f"{owner}/{repo}".lower())
    if entry is None:
        entry = VerifyRepoOverride()
    return VerifyPolicy(
        enabled=bool(_pick(entry.enabled, config.enabled)),
        isolation=config.isolation,
        extra_read_only_paths=tuple(config.extra_read_only_paths),
        max_workspace_mb=config.max_workspace_mb,
        setup=tuple(_pick(entry.setup, config.setup)),
        setup_network=bool(_pick(entry.setup_network, config.setup_network)),
        setup_timeout_seconds=float(
            _pick(entry.setup_timeout_seconds, config.setup_timeout_seconds)
        ),
        memory_limit_mb=config.memory_limit_mb,
        cpu_seconds=config.cpu_seconds,
        commands=tuple(_pick(entry.commands, config.commands)),
        command_timeout_seconds=config.command_timeout_seconds,
        evidence_wait_seconds=config.evidence_wait_seconds,
        max_evidence_chars=config.max_evidence_chars,
        max_tool_findings=config.max_tool_findings,
        test_runners=tuple(_pick(entry.test_runners, config.test_runners)),
        categories=frozenset(config.categories),
        min_severity=config.min_severity,
        max_findings=int(_pick(entry.max_findings, config.max_findings)),
        test_timeout_seconds=float(_pick(entry.test_timeout_seconds, config.test_timeout_seconds)),
        budget_seconds=float(_pick(entry.budget_seconds, config.budget_seconds)),
        max_concurrent=config.max_concurrent,
        runs=config.runs,
    )
