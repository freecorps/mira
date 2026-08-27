"""Checking a patch before anything is written anywhere.

Validation happens after the patch exists and before a branch does. That order
is the point: a patch that fails here leaves nothing behind to clean up,
because nothing was created.

There are two tiers, and they are separate because they cost different things.

**Static checks** run in-process, spawn nothing and are on by default. They
parse every edited file Mira knows how to parse and refuse a patch that broke
one, and they refuse a patch whose content still looks like a credential. On an
Orange Pi this is the entire validation budget, and it is enough to stop the
failure mode that actually happens — a model that produced code that does not
parse.

**Command checks** run an allowlist from deployment configuration in a scratch
directory holding the edited files. Empty by default. Each command runs with no
shell, a wall-clock timeout, and POSIX address-space and CPU ceilings where the
platform has them.

Two things this module will not do, ever:

* **Run anything derived from the pull request.** There is no code path from a
  comment, a title, a diff, a model response or a CI log to an argument vector.
  Commands come from :class:`~mira.config.AutofixValidationConfig` and nowhere
  else, and they are argument *lists*, so there is no shell to inject into.
* **Treat "the check could not run" as "the check passed."** A timeout, a
  missing binary and a crashed harness all block publication, because a check
  Mira could not perform is not evidence.

A note on what the scratch directory is and is not. It holds the *edited files*
at their repository-relative paths — not a checkout. That is enough for a
formatter or a linter with a config file, and it is not enough for a test suite
that imports the rest of the tree. Deployments that want a real test run point
the commands at a checkout they maintain themselves; the honest statement of
that limit lives in `docs/autofix.md` rather than in a promise here. The
serious verification of a fix is the CI run on the pull request it opens, which
is why the CI retry loop exists at all.
"""

from __future__ import annotations

import ast
import asyncio
import json
import logging
import os
import shutil
import subprocess  # noqa: S404 - argv-only, no shell; see module docstring
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

from mira.autofix.models import CheckResult, FixPatch, ValidationResult
from mira.autofix.policy import EffectivePolicy
from mira.autofix.redact import contains_secret, redact

logger = logging.getLogger(__name__)

# Environment handed to a validation command. An allowlist, not the operator's
# environment minus a few names: a formatter has no business inheriting the
# platform token, the database URL or the model API key, and enumerating what
# to *remove* means every new secret added to the deployment is leaked until
# somebody remembers to add it to the list.
_ENV_KEEP = ("PATH", "HOME", "LANG", "LC_ALL", "TZ", "SYSTEMROOT", "TEMP", "TMP")


def _child_env() -> dict[str, str]:
    env = {name: os.environ[name] for name in _ENV_KEEP if name in os.environ}
    # Node and Python both write caches into the working directory otherwise,
    # which would leave debris in the scratch tree and slow every run.
    env.setdefault("PYTHONDONTWRITEBYTECODE", "1")
    env.setdefault("NO_COLOR", "1")
    env.setdefault("CI", "1")
    return env


def _rlimit_preexec(policy_memory_mb: int, cpu_seconds: int) -> Any:
    """A preexec hook applying address-space and CPU ceilings, or None.

    Returns None where `resource` does not exist (Windows). The caller records
    that as a note on the check rather than pretending the limits applied —
    a self-hosted Windows runner should know its formatter is unbounded.
    """
    try:
        import resource
    except ImportError:  # pragma: no cover - Windows
        return None

    limit_bytes = max(64, int(policy_memory_mb)) * 1024 * 1024
    cpu = max(1, int(cpu_seconds))

    def _apply() -> None:  # pragma: no cover - runs in the forked child
        resource.setrlimit(resource.RLIMIT_AS, (limit_bytes, limit_bytes))
        resource.setrlimit(resource.RLIMIT_CPU, (cpu, cpu))
        resource.setrlimit(resource.RLIMIT_NPROC, (256, 256))
        resource.setrlimit(resource.RLIMIT_FSIZE, (256 * 1024 * 1024, 256 * 1024 * 1024))
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
        os.setsid()

    return _apply


# ── Static checks ───────────────────────────────────────────────────────────


def _check_python(path: str, content: str) -> str:
    try:
        ast.parse(content, filename=path)
    except SyntaxError as exc:
        return f"{path}:{exc.lineno}: {exc.msg}"
    return ""


def _check_json(path: str, content: str) -> str:
    try:
        json.loads(content)
    except ValueError as exc:
        return f"{path}: {exc}"
    return ""


def _check_yaml(path: str, content: str) -> str:
    try:
        import yaml
    except ImportError:  # pragma: no cover - yaml is a hard dependency
        return ""
    try:
        list(yaml.safe_load_all(content))
    except Exception as exc:  # noqa: BLE001 - any parse failure is the answer
        return f"{path}: {exc}"
    return ""


def _check_toml(path: str, content: str) -> str:
    try:
        import tomllib
    except ImportError:  # pragma: no cover - Python < 3.11
        return ""
    try:
        tomllib.loads(content)
    except Exception as exc:  # noqa: BLE001
        return f"{path}: {exc}"
    return ""


_PARSERS = {
    ".py": _check_python,
    ".pyi": _check_python,
    ".json": _check_json,
    ".yaml": _check_yaml,
    ".yml": _check_yaml,
    ".toml": _check_toml,
}


def syntax_check(patch: FixPatch) -> CheckResult:
    """Parse every edited file Mira has a parser for.

    Files in languages there is no in-process parser for are not counted as
    passing and not counted as failing — they are simply not covered, and the
    detail says so. Pretending a Rust file was checked because nothing
    complained would be the same lie as skipping the check entirely.
    """
    started = time.monotonic()
    failures: list[str] = []
    covered = 0
    for path, content in sorted(patch.files.items()):
        parser = _PARSERS.get(Path(path).suffix.lower())
        if parser is None:
            continue
        covered += 1
        problem = parser(path, content)
        if problem:
            failures.append(problem)
    duration = time.monotonic() - started
    if failures:
        return CheckResult(
            name="syntax",
            outcome="failed",
            detail="\n".join(failures)[:4_000],
            duration_seconds=duration,
        )
    uncovered = len(patch.files) - covered
    detail = f"{covered} file(s) parsed"
    if uncovered:
        detail += f"; {uncovered} not covered by an in-process parser"
    return CheckResult(name="syntax", outcome="passed", detail=detail, duration_seconds=duration)


def secret_check(patch: FixPatch) -> CheckResult:
    """Refuse to commit content that still looks like a credential.

    Redaction protects the *model*; this protects the *repository*. A model
    handed a redacted file could still write a plausible-looking key into its
    replacement, and a fix that commits one would be Mira introducing the exact
    class of problem it exists to catch.
    """
    started = time.monotonic()
    offenders = [
        path
        for path, content in sorted(patch.files.items())
        # The *added* text only: a credential already in the file is a finding
        # for the review to raise, not a reason this patch cannot land.
        if any(contains_secret(edit.replace) for edit in patch.edits if edit.path == path)
    ]
    duration = time.monotonic() - started
    if offenders:
        return CheckResult(
            name="secrets",
            outcome="failed",
            detail="The patch would commit something that looks like a credential in: "
            + ", ".join(offenders),
            duration_seconds=duration,
        )
    return CheckResult(
        name="secrets",
        outcome="passed",
        detail="no credential-shaped additions",
        duration_seconds=duration,
    )


# ── Command checks ──────────────────────────────────────────────────────────


def materialize(patch: FixPatch, root: Path) -> list[Path]:
    """Write the patched files into a scratch tree, and nothing else.

    Paths were already normalised and proven repository-relative by
    :mod:`mira.autofix.patch`, so this cannot escape ``root``. It is re-checked
    anyway — this function writes to a filesystem, and a filesystem write is
    not the place to rely on a caller having done its job.
    """
    written: list[Path] = []
    resolved_root = root.resolve()
    for path, content in sorted(patch.files.items()):
        target = (resolved_root / path).resolve()
        if not target.is_relative_to(resolved_root):  # pragma: no cover - defence in depth
            raise ValueError(f"refusing to write outside the workspace: {path}")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8", newline="")
        written.append(target)
    return written


def _run_one(
    entry: dict[str, Any],
    *,
    workspace: Path,
    policy: EffectivePolicy,
    paths: list[str],
    deadline: float,
) -> CheckResult:
    """Run one allowlisted command. Never raises; every failure is a result."""
    config = policy.validation
    name = str(entry.get("name") or "").strip() or " ".join(entry["command"][:2])
    argv = [str(part) for part in entry["command"]]
    optional = bool(entry.get("optional"))
    started = time.monotonic()

    remaining = deadline - time.monotonic()
    if remaining <= 0:
        return CheckResult(
            name=name,
            outcome="timeout",
            detail="the validation budget was exhausted before this check ran",
        )

    executable = shutil.which(argv[0])
    if executable is None:
        # A command the operator configured but the image does not have. This
        # is a deployment error, and treating it as a pass would silently
        # remove a check somebody believes is running.
        return CheckResult(
            name=name,
            outcome="skipped" if optional else "error",
            detail=f"{argv[0]} is not installed in this environment",
            duration_seconds=time.monotonic() - started,
        )

    # `{files}` expands to the patched paths, so a formatter can be pointed at
    # exactly what changed. It is the only substitution there is, and it
    # expands to paths Mira produced — never to anything from the pull request.
    expanded: list[str] = []
    for part in argv:
        if part == "{files}":
            expanded.extend(paths)
        else:
            expanded.append(part)
    expanded[0] = executable

    timeout = min(config.command_timeout_seconds, max(1.0, remaining))
    preexec = _rlimit_preexec(config.memory_limit_mb, config.cpu_seconds)
    note = "" if preexec is not None else " (CPU/memory limits unavailable on this platform)"
    kwargs: dict[str, Any] = {}
    if preexec is not None:
        kwargs["preexec_fn"] = preexec
    elif sys.platform == "win32":  # pragma: no cover - Windows only
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)

    try:
        completed = subprocess.run(  # noqa: S603 - argv from config, shell=False
            expanded,
            cwd=str(workspace),
            env=_child_env(),
            capture_output=True,
            text=True,
            timeout=timeout,
            shell=False,
            check=False,
            **kwargs,
        )
    except subprocess.TimeoutExpired:
        return CheckResult(
            name=name,
            outcome="timeout",
            detail=f"{name} did not finish within {timeout:g}s{note}",
            duration_seconds=time.monotonic() - started,
        )
    except OSError as exc:
        return CheckResult(
            name=name,
            outcome="error",
            detail=f"{name} could not be started: {exc}",
            duration_seconds=time.monotonic() - started,
        )

    # Output is untrusted data: it goes through redaction on the way into an
    # audit record and, later, into a model prompt.
    output = redact((completed.stdout or "") + (completed.stderr or ""))[: config.max_output_bytes]
    duration = time.monotonic() - started
    if completed.returncode == 0:
        return CheckResult(
            name=name,
            outcome="passed",
            detail=(output.strip() or "ok") + note,
            duration_seconds=duration,
            exit_code=0,
        )
    return CheckResult(
        name=name,
        outcome="skipped" if optional else "failed",
        detail=output.strip() or f"exited {completed.returncode}",
        duration_seconds=duration,
        exit_code=completed.returncode,
    )


def _run_commands(patch: FixPatch, policy: EffectivePolicy) -> list[CheckResult]:
    entries = list(policy.validation.commands)
    if not entries:
        return []
    deadline = time.monotonic() + policy.validation.total_timeout_seconds
    results: list[CheckResult] = []
    with tempfile.TemporaryDirectory(prefix="mira-autofix-") as scratch:
        workspace = Path(scratch)
        try:
            written = materialize(patch, workspace)
        except OSError as exc:
            return [
                CheckResult(
                    name="workspace",
                    outcome="error",
                    detail=f"the validation workspace could not be prepared: {exc}",
                )
            ]
        paths = [str(path.relative_to(workspace.resolve())) for path in written]
        for entry in entries:
            results.append(
                _run_one(entry, workspace=workspace, policy=policy, paths=paths, deadline=deadline)
            )
    return results


async def validate(patch: FixPatch, policy: EffectivePolicy) -> ValidationResult:
    """Every check this policy asks for, in order, with nothing written.

    Commands run on a worker thread: they are blocking subprocesses, and the
    event loop this is called from is also serving webhooks.
    """
    checks: list[CheckResult] = []
    if policy.validation.syntax_check:
        checks.append(syntax_check(patch))
    checks.append(secret_check(patch))

    if policy.validation.commands:
        try:
            command_results = await asyncio.wait_for(
                asyncio.to_thread(_run_commands, patch, policy),
                timeout=policy.validation.total_timeout_seconds + 30,
            )
        except TimeoutError:
            command_results = [
                CheckResult(
                    name="validation",
                    outcome="timeout",
                    detail="validation exceeded its total budget",
                )
            ]
        except Exception as exc:  # noqa: BLE001 - a broken harness blocks, never passes
            logger.warning("Autofix validation harness failed: %s", exc)
            command_results = [
                CheckResult(name="validation", outcome="error", detail=str(exc)[:1_000])
            ]
        checks.extend(command_results)

    executed = bool(policy.validation.commands) or policy.validation.syntax_check
    return ValidationResult(checks=checks, executed=executed)
