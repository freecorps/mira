"""One review's copy of the pull request's head, on disk, and the only place its code runs.

The workspace is the review's repository snapshot — the archive of the head
commit it already downloaded for code context and the agentic tools — written
to a scratch directory with autofix's containment-checked writer. There is no
``git clone``: a clone needs a token, and a token in the workspace is a token
the pull request's code can read. The snapshot holds the repository's text
files; vendored and build directories and binaries are not in it, which is
what a test suite rebuilds in ``setup`` anyway.

Layout, all under one temporary directory that is the sandbox's only writable
path::

    mira-verify-XXXX/
        repo/   the checkout; every command's working directory
        home/   $HOME, so caches and dotfiles land here and not in Mira's home
        tmp/    $TMPDIR

Every command goes through :func:`mira.sandbox.run_argv` (no shell, rlimits,
group kill, environment allowlist) wrapped in the review's
:class:`~mira.verify.isolation.Isolation`. The environment is the sandbox's
allowlist with ``HOME``, ``TMPDIR`` and ``PATH`` pointed into the workspace:
no token, no database URL, no model key is ever in it.
"""

from __future__ import annotations

import contextlib
import logging
import os
import shutil
import stat
import sys
import tempfile
import time
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from mira import sandbox
from mira.verify.isolation import Isolation, toolchain_roots

logger = logging.getLogger(__name__)

WORKSPACE_PREFIX = "mira-verify-"
# A workspace older than this is debris from a process that died mid-review.
STALE_SECONDS = 6 * 3600

# Where a repository's own installed tools land, searched before the host's
# PATH so `pytest` is the project's pytest once setup has installed it.
_PROJECT_BIN_DIRS = (".venv/bin", "venv/bin", "node_modules/.bin")


class WorkspaceError(Exception):
    """The workspace could not be prepared; nothing ran."""


def _force_remove(func, path, _exc) -> None:  # noqa: ANN001 - shutil callback shape
    """Make a read-only entry writable and try again: a test may chmod what it made."""
    with contextlib.suppress(OSError):
        os.chmod(path, stat.S_IRWXU)
        parent = os.path.dirname(path)
        if parent:
            os.chmod(parent, stat.S_IRWXU)
        func(path)


def remove_tree(path: Path) -> None:
    """Delete a workspace, whatever the code that ran in it did to its permissions."""
    if sys.version_info >= (3, 12):
        shutil.rmtree(path, onexc=_force_remove)
    else:  # pragma: no cover - Python 3.11
        shutil.rmtree(path, onerror=_force_remove)


def sweep_stale(base: Path | None = None, *, older_than: float = STALE_SECONDS) -> int:
    """Remove workspaces a dead process left behind. Returns how many."""
    base = base or Path(tempfile.gettempdir())
    removed = 0
    now = time.time()
    try:
        entries = list(base.iterdir())
    except OSError:
        return 0
    for entry in entries:
        if not entry.name.startswith(WORKSPACE_PREFIX) or not entry.is_dir():
            continue
        try:
            if now - entry.stat().st_mtime < older_than:
                continue
            remove_tree(entry)
            removed += 1
        except OSError as exc:
            logger.debug("Could not remove stale workspace %s: %s", entry, exc)
    return removed


@dataclass
class Workspace:
    """A materialised checkout and the policy its commands run under."""

    root: Path
    isolation: Isolation
    memory_limit_mb: int = 4096
    cpu_seconds: int = 300
    files_written: int = 0

    @property
    def repo(self) -> Path:
        return self.root / "repo"

    @property
    def home(self) -> Path:
        return self.root / "home"

    @property
    def tmp(self) -> Path:
        return self.root / "tmp"

    @classmethod
    def create(
        cls,
        files: Mapping[str, str],
        isolation: Isolation,
        *,
        max_bytes: int,
        memory_limit_mb: int = 4096,
        cpu_seconds: int = 300,
    ) -> Workspace:
        """Write ``files`` into a fresh workspace. Blocking; raises :class:`WorkspaceError`."""
        from mira.autofix.validate import materialize_files

        total = sum(len(text.encode("utf-8", errors="replace")) for text in files.values())
        if total > max_bytes:
            raise WorkspaceError(
                f"the head commit holds {total // (1024 * 1024)} MB of text, over the "
                f"{max_bytes // (1024 * 1024)} MB workspace limit"
            )
        sweep_stale()
        root = Path(tempfile.mkdtemp(prefix=WORKSPACE_PREFIX)).resolve()
        workspace = cls(
            root=root,
            isolation=isolation,
            memory_limit_mb=memory_limit_mb,
            cpu_seconds=cpu_seconds,
        )
        try:
            for directory in (workspace.repo, workspace.home, workspace.tmp):
                directory.mkdir(parents=True, exist_ok=True)
            written = materialize_files(files, workspace.repo, skip_unsafe=True)
        except (OSError, ValueError) as exc:
            workspace.cleanup()
            raise WorkspaceError(f"the workspace could not be written: {exc}") from exc
        workspace.files_written = len(written)
        return workspace

    # ── Running ─────────────────────────────────────────────────────────

    def search_path(self) -> str:
        project = [str(self.repo / d) for d in _PROJECT_BIN_DIRS]
        return os.pathsep.join([*project, os.environ.get("PATH", os.defpath)])

    def resolve(self, command: str) -> str | None:
        """The absolute executable ``command`` names, looked up as the child would.

        A relative path with a slash (``.venv/bin/python``) is the workspace's;
        a bare name is searched in the project's bin directories first.
        """
        if os.sep in command or "/" in command:
            candidate = Path(command) if os.path.isabs(command) else self.repo / command
            if candidate.is_file() and os.access(candidate, os.X_OK):
                return str(candidate)
            return None
        return shutil.which(command, path=self.search_path())

    def env(self) -> dict[str, str]:
        home = str(self.home)
        tmp = str(self.tmp)
        return {
            "HOME": home,
            "TMPDIR": tmp,
            "TMP": tmp,
            "TEMP": tmp,
            "PATH": self.search_path(),
            "XDG_CACHE_HOME": str(self.home / ".cache"),
            "XDG_CONFIG_HOME": str(self.home / ".config"),
            "PIP_NO_INPUT": "1",
            "PIP_DISABLE_PIP_VERSION_CHECK": "1",
            "npm_config_update_notifier": "false",
            "npm_config_fund": "false",
            "PYTEST_ADDOPTS": "-p no:cacheprovider",
        }

    def run(
        self,
        argv: list[str],
        *,
        timeout_seconds: float,
        network: bool = False,
        max_output_bytes: int = 2_000_000,
    ) -> sandbox.ProcessOutcome:
        """Run one command in the workspace, isolated. Blocking; never raises."""
        if not argv:
            return sandbox.ProcessOutcome(status="error", detail="no command was given")
        executable = self.resolve(argv[0])
        if executable is None:
            return sandbox.ProcessOutcome(
                status="missing", detail=f"{argv[0]} is not installed in this environment"
            )
        read_only = tuple(
            root
            for root in toolchain_roots(executable)
            if not Path(root).resolve().is_relative_to(self.root)
        )
        wrapped = self.isolation.wrap(
            [executable, *argv[1:]],
            root=self.root,
            cwd=self.repo,
            network=network,
            read_only=read_only,
        )
        return sandbox.run_argv(
            wrapped,
            cwd=self.repo,
            timeout_seconds=timeout_seconds,
            memory_limit_mb=self.memory_limit_mb,
            cpu_seconds=self.cpu_seconds,
            max_output_bytes=max_output_bytes,
            env=self.env(),
            encoding="utf-8",
            errors="replace",
        )

    # ── Files ───────────────────────────────────────────────────────────

    def _inside(self, relative: str) -> Path:
        target = (self.repo / relative).resolve()
        if not target.is_relative_to(self.repo.resolve()) or target == self.repo.resolve():
            raise WorkspaceError(f"refusing to touch {relative!r} outside the workspace")
        return target

    def write(self, relative: str, content: str) -> Path:
        target = self._inside(relative)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8", newline="")
        return target

    def remove(self, relative: str) -> None:
        with contextlib.suppress(OSError, WorkspaceError):
            self._inside(relative).unlink()

    def exists(self, relative: str) -> bool:
        try:
            return self._inside(relative).exists()
        except WorkspaceError:
            return False

    def cleanup(self) -> None:
        """Delete the whole workspace. Idempotent and never raises."""
        try:
            if self.root.exists():
                remove_tree(self.root)
        except OSError as exc:  # pragma: no cover - logged, swept on a later review
            logger.warning("Could not remove verification workspace %s: %s", self.root, exc)
