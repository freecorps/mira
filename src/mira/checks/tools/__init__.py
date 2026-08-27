"""Deterministic analysers Mira can run, and the closed set of them.

The registry is closed at import time and mirrors the allowlist in
:mod:`mira.checks.config_models`, which is checked when configuration loads. So
there are two independent places an unknown tool name is refused — the config
never validates, and even if it did, nothing here would return an adapter for
it — and neither of them can be reached from a pull request.

Adding an analyser means writing an adapter, adding its name to the allowlist,
and having both reviewed. That is deliberately more work than editing a config
string: "run whatever the repository asks for" is the failure mode this design
exists to make impossible.
"""

from __future__ import annotations

from mira.checks.tools.base import SubprocessTool, ToolAdapter, ToolFinding
from mira.checks.tools.linters import EslintTool, GitleaksTool, RuffTool, SemgrepTool
from mira.checks.tools.osv import OsvTool

__all__ = [
    "SubprocessTool",
    "ToolAdapter",
    "ToolFinding",
    "adapter_for",
    "registered_tools",
]

_ADAPTERS: dict[str, type[ToolAdapter]] = {
    adapter.name: adapter for adapter in (SemgrepTool, RuffTool, EslintTool, GitleaksTool, OsvTool)
}


def adapter_for(name: str) -> ToolAdapter | None:
    """The adapter for an allowlisted tool name, or None when there is none."""
    factory = _ADAPTERS.get((name or "").strip().lower())
    return factory() if factory is not None else None


def registered_tools() -> list[str]:
    return sorted(_ADAPTERS)
