"""Running an allowlisted command without letting it run away with the box.

Two features want the same thing: autofix validates a generated patch by
running the deployment's formatters and tests, and the pre-merge check
framework runs deterministic analysers over the changed files. Both hand an
argument list to a process, both have to bound what it can do, and both have to
treat "the command could not run" as a failure rather than as a pass.

Written once here, because two copies of process-group killing and rlimit
setup would eventually be two *different* copies — and the one that got the
timeout path wrong would be the one leaving a runaway linter holding four cores
on an Orange Pi.

What this module guarantees, and what it does not:

* **There is no shell.** Every caller passes ``argv`` as a list. There is
  nothing to inject into because nothing is parsed.
* **The command is bounded.** Wall-clock timeout, address-space and CPU-second
  rlimits where the platform has them, and a kill that reaches the whole
  process group rather than just the child.
* **The environment is an allowlist**, not the operator's environment minus a
  few names. Enumerating what to remove means every secret added to the
  deployment later is leaked until somebody remembers to add it to the list.
* **It does not decide what may run.** The argument vector comes from the
  caller, and every caller in this codebase builds it from deployment
  configuration and a closed allowlist. This module would happily run anything;
  the guarantee that it never sees anything from a pull request is upstream of
  it, and is stated where those vectors are built.
"""

from __future__ import annotations

import contextlib
import logging
import os
import signal
import subprocess  # noqa: S404 - argv-only, no shell; see module docstring
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Environment handed to a child. An allowlist: a formatter or a linter has no
# business inheriting the platform token, the database URL or the model API key.
ENV_KEEP = ("PATH", "HOME", "LANG", "LC_ALL", "TZ", "SYSTEMROOT", "TEMP", "TMP")

# Seconds allowed for a killed command's pipes to close before the caller gives
# up waiting for it. Short on purpose: the process has already had SIGKILL.
REAP_SECONDS = 5.0


def child_env(extra: dict[str, str] | None = None) -> dict[str, str]:
    """The environment a sandboxed command runs with."""
    env = {name: os.environ[name] for name in ENV_KEEP if name in os.environ}
    # Node and Python both write caches into the working directory otherwise,
    # which would leave debris in the scratch tree and slow every run.
    env.setdefault("PYTHONDONTWRITEBYTECODE", "1")
    env.setdefault("NO_COLOR", "1")
    env.setdefault("CI", "1")
    env.update(extra or {})
    return env


def rlimit_preexec(memory_mb: int, cpu_seconds: int) -> Any:
    """A preexec hook applying address-space and CPU ceilings, or None.

    Returns None where ``resource`` does not exist (Windows). The caller
    records that as a note on the result rather than pretending the limits
    applied — a self-hosted Windows runner should know its linter is unbounded.
    """
    try:
        import resource
    except ImportError:  # pragma: no cover - Windows
        return None

    limit_bytes = max(64, int(memory_mb)) * 1024 * 1024
    cpu = max(1, int(cpu_seconds))

    def _apply() -> None:  # pragma: no cover - runs in the forked child
        resource.setrlimit(resource.RLIMIT_AS, (limit_bytes, limit_bytes))
        resource.setrlimit(resource.RLIMIT_CPU, (cpu, cpu))
        resource.setrlimit(resource.RLIMIT_NPROC, (256, 256))
        resource.setrlimit(resource.RLIMIT_FSIZE, (256 * 1024 * 1024, 256 * 1024 * 1024))
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
        os.setsid()

    return _apply


def kill_group(process: subprocess.Popen, *, grouped: bool) -> None:
    """Kill the command and everything it started.

    ``os.setsid()`` in the preexec hook put the command in its own session, so
    its pid is also its process-group id and one ``killpg`` reaches every
    descendant. Without that hook — Windows, or a platform with no ``resource``
    module — only the direct child can be reached, which is why the hook and
    this function are described together rather than apart.
    """
    if grouped and hasattr(os, "killpg"):
        try:
            os.killpg(os.getpgid(process.pid), signal.SIGKILL)
            return
        except (ProcessLookupError, PermissionError, OSError) as exc:
            logger.debug("Could not kill the process group for pid %s: %s", process.pid, exc)
    if sys.platform == "win32":  # pragma: no cover - Windows only
        with contextlib.suppress(Exception):
            subprocess.run(  # noqa: S603, S607 - fixed argv, no shell
                ["taskkill", "/F", "/T", "/PID", str(process.pid)],
                capture_output=True,
                check=False,
                timeout=REAP_SECONDS,
            )
    with contextlib.suppress(Exception):
        process.kill()


@dataclass
class ProcessOutcome:
    """What running a command produced, including how it failed to run.

    ``status`` is the field that matters, and it has four values rather than a
    boolean because "the binary is missing", "it timed out" and "it exited
    non-zero" call for three different reports and only the last of them says
    anything about the code being analysed.
    """

    # "ok" | "missing" | "timeout" | "error"
    status: str
    stdout: str = ""
    stderr: str = ""
    exit_code: int = 0
    duration_seconds: float = 0.0
    detail: str = ""
    # True when the platform had no rlimits to apply.
    unbounded: bool = False

    @property
    def ran(self) -> bool:
        return self.status == "ok"


def run_argv(
    argv: list[str],
    *,
    cwd: str | Path,
    timeout_seconds: float,
    memory_limit_mb: int = 1024,
    cpu_seconds: int = 120,
    max_output_bytes: int = 200_000,
    env: dict[str, str] | None = None,
    encoding: str | None = None,
    errors: str | None = None,
) -> ProcessOutcome:
    """Run one command to completion or to its deadline. Never raises.

    ``Popen`` rather than ``run``, because a timeout has to kill the process
    *group* and ``run`` only kills the child it started. A linter that forked
    workers would otherwise leave them behind holding the scratch directory
    open and burning the CPU this call was supposed to bound.

    ``encoding`` and ``errors`` are passed through to the pipes. The default
    locale decoding is fine for a linter that prints ASCII diagnostics, but a
    caller reading repository *content* — a diff, a file listing — has to name
    an encoding and a lenient error handler, because a Latin-1 source file
    would otherwise raise ``UnicodeDecodeError`` out of ``communicate`` and
    turn a readable diff into a crash this function promises never to raise.
    """
    import shutil

    started = time.monotonic()
    if not argv:
        return ProcessOutcome(status="error", detail="no command was given")

    executable = shutil.which(argv[0])
    if executable is None:
        return ProcessOutcome(
            status="missing",
            detail=f"{argv[0]} is not installed in this environment",
            duration_seconds=time.monotonic() - started,
        )
    expanded = [executable, *argv[1:]]

    preexec = rlimit_preexec(memory_limit_mb, cpu_seconds)
    kwargs: dict[str, Any] = {}
    if preexec is not None:
        kwargs["preexec_fn"] = preexec
    elif sys.platform == "win32":  # pragma: no cover - Windows only
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)

    try:
        process = subprocess.Popen(  # noqa: S603 - argv from config, shell=False
            expanded,
            cwd=str(cwd),
            env=child_env(env),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding=encoding,
            errors=errors,
            shell=False,
            **kwargs,
        )
    except OSError as exc:
        return ProcessOutcome(
            status="error",
            detail=f"{argv[0]} could not be started: {exc}",
            duration_seconds=time.monotonic() - started,
        )

    grouped = preexec is not None
    try:
        stdout, stderr = process.communicate(timeout=max(0.1, timeout_seconds))
    except subprocess.TimeoutExpired:
        kill_group(process, grouped=grouped)
        # Reap it, so the pipes close and no zombie outlives the call. The
        # output is deliberately discarded: a killed command proved nothing,
        # and its half-written stderr is not evidence of anything either.
        with contextlib.suppress(Exception):
            process.communicate(timeout=REAP_SECONDS)
        return ProcessOutcome(
            status="timeout",
            detail=f"{argv[0]} did not finish within {timeout_seconds:g}s",
            duration_seconds=time.monotonic() - started,
            unbounded=preexec is None,
        )
    except OSError as exc:
        kill_group(process, grouped=grouped)
        return ProcessOutcome(
            status="error",
            detail=f"{argv[0]} could not be run: {exc}",
            duration_seconds=time.monotonic() - started,
        )

    return ProcessOutcome(
        status="ok",
        stdout=(stdout or "")[:max_output_bytes],
        stderr=(stderr or "")[:max_output_bytes],
        exit_code=int(process.returncode or 0),
        duration_seconds=time.monotonic() - started,
        unbounded=preexec is None,
    )
