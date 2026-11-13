"""Fencing in code Mira did not write.

:mod:`mira.sandbox` bounds a command — no shell, an environment allowlist,
rlimits, a group kill — and says plainly that it does not decide *what* runs:
every argv it has ever seen came from deployment configuration and ran
deployment-installed binaries over files Mira wrote. Verification breaks that
assumption on purpose. ``pytest`` imports the pull request's ``conftest.py``;
``npm ci`` runs whatever the lockfile installs. So the code is the attacker's,
and rlimits alone leave it the whole host: Mira's database, the GitHub App
key on disk, the network to send them over.

This module wraps an argv in something that takes those away, and refuses to
hand back an argv when it cannot:

* **bwrap** (Linux, bubblewrap). New mount, PID, IPC, UTS, user and network
  namespaces. The root is empty except the system directories (read-only), a
  handful of ``/etc`` entries a toolchain needs, the toolchain a binary
  resolves into, and the workspace — the one writable path. Mira's own files,
  home directory and environment are not there to read.
* **sandbox-exec** (macOS, Seatbelt). Network denied, writes only inside the
  workspace, and the home directory unreadable except the workspace and the
  toolchain. Weaker than bwrap — the rest of the filesystem stays readable —
  and meant for running Mira locally; a deployment belongs on Linux.
* **unshare** (Linux, network namespace only). No network and nothing else:
  the filesystem is the host's. Only used when named explicitly.
* **none**. Only used when named explicitly, and logged as unisolated.

``auto`` takes the first of bwrap and sandbox-exec that *works* — a probe runs
it once per process, because a bwrap that exists but is denied user namespaces
(Docker's default seccomp profile, AppArmor on recent Ubuntu) fails at run
time, not at ``which`` time. When neither works, verification does not run.
"""

from __future__ import annotations

import logging
import os
import shutil
import sys
import threading
from dataclasses import dataclass, field
from pathlib import Path

from mira import sandbox

logger = logging.getLogger(__name__)

# System directories a toolchain lives in, bound read-only under bwrap. A
# missing one is skipped (`--ro-bind-try`), and a merged-/usr symlink such as
# `/bin -> usr/bin` is recreated as the same symlink.
_SYSTEM_DIRS = ("/usr", "/bin", "/sbin", "/lib", "/lib32", "/lib64", "/libx32", "/opt", "/nix")

# The `/etc` a process needs to resolve users, load libraries and verify TLS
# during setup — and nothing else from it. `/etc` as a whole holds whatever the
# operator keeps there, which may include Mira's own configuration.
_ETC_ENTRIES = (
    "/etc/alternatives",
    "/etc/ca-certificates",
    "/etc/ca-certificates.conf",
    "/etc/pki",
    "/etc/ssl",
    "/etc/hosts",
    "/etc/resolv.conf",
    "/etc/nsswitch.conf",
    "/etc/passwd",
    "/etc/group",
    "/etc/ld.so.cache",
    "/etc/ld.so.conf",
    "/etc/ld.so.conf.d",
    "/etc/localtime",
    "/etc/mime.types",
    "/etc/os-release",
)

PROBE_TIMEOUT_SECONDS = 15.0


@dataclass(frozen=True)
class Isolation:
    """One isolation mode, ready to wrap commands.

    ``offline`` is whether a command wrapped with ``network=False`` really has
    no network: True for every mode but ``none``, which is the reason ``none``
    must be asked for by name.
    """

    mode: str
    executable: str = ""
    extra_read_only: tuple[str, ...] = field(default_factory=tuple)

    @property
    def offline(self) -> bool:
        return self.mode != "none"

    @property
    def confines_filesystem(self) -> bool:
        return self.mode in {"bwrap", "sandbox-exec"}

    def wrap(
        self,
        argv: list[str],
        *,
        root: Path,
        cwd: Path,
        network: bool,
        read_only: tuple[str, ...] = (),
    ) -> list[str]:
        """``argv`` as it runs inside this isolation, with ``root`` the only writable path."""
        roots = tuple(dict.fromkeys((*self.extra_read_only, *read_only)))
        if self.mode == "bwrap":
            return _bwrap_argv(self.executable, argv, root=root, cwd=cwd, network=network, ro=roots)
        if self.mode == "sandbox-exec":
            profile = _seatbelt_profile(root=root, network=network, readable=roots)
            return [self.executable, "-p", profile, *argv]
        if self.mode == "unshare":
            if network:
                return list(argv)
            # A user namespace is what lets an unprivileged process have its
            # own (empty) network namespace; the mapped root in it is not root
            # outside it.
            return [self.executable, "--net", "--map-root-user", "--", *argv]
        return list(argv)


def _bwrap_argv(
    executable: str,
    argv: list[str],
    *,
    root: Path,
    cwd: Path,
    network: bool,
    ro: tuple[str, ...],
) -> list[str]:
    out = [
        executable,
        "--die-with-parent",
        "--new-session",
        "--unshare-all",
    ]
    if network:
        out.append("--share-net")
    for path in _SYSTEM_DIRS:
        if os.path.islink(path):
            out += ["--symlink", os.readlink(path), path]
        else:
            out += ["--ro-bind-try", path, path]
    for path in _ETC_ENTRIES:
        out += ["--ro-bind-try", path, path]
    for path in ro:
        out += ["--ro-bind-try", path, path]
    out += ["--proc", "/proc", "--dev", "/dev", "--tmpfs", "/tmp"]
    resolved = str(root.resolve())
    out += ["--bind", resolved, resolved, "--chdir", str(cwd.resolve()), "--", *argv]
    return out


def _sb_quote(path: str) -> str:
    return '"' + path.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _seatbelt_profile(*, root: Path, network: bool, readable: tuple[str, ...]) -> str:
    """A Seatbelt profile: writes only under ``root``, home unreadable, maybe no network.

    Seatbelt applies the *last* matching rule, so each deny is followed by the
    narrower allows that carve the workspace and the toolchain back out of it.
    """
    resolved = os.path.realpath(root)
    home = os.path.realpath(os.path.expanduser("~"))
    rules = ["(version 1)", "(allow default)"]
    if not network:
        rules.append("(deny network*)")
    rules.append("(deny file-write*)")
    rules.append(
        f"(allow file-write* (subpath {_sb_quote(resolved)}) "
        '(literal "/dev/null") (literal "/dev/zero") (literal "/dev/tty") '
        '(regex #"^/dev/fd/"))'
    )
    # Contents and listings, not metadata: interpreters `realpath()` their own
    # location on start, which needs `lstat` on every ancestor directory, and
    # an `lstat` says that a file exists, not what is in it.
    rules.append(f"(deny file-read-data (subpath {_sb_quote(home)}))")
    allowed = [resolved, *(os.path.realpath(p) for p in readable)]
    rules.append(
        "(allow file-read-data " + " ".join(f"(subpath {_sb_quote(p)})" for p in allowed) + ")"
    )
    return "".join(rules)


def toolchain_roots(executable: str) -> tuple[str, ...]:
    """Directories a binary needs to read to run: its venv, and the interpreter's prefix.

    A console script in a virtualenv (``.venv/bin/pytest``) is a shebang for
    the venv's python, which is a symlink to an interpreter elsewhere whose
    standard library sits under its own prefix. Each of those has to be
    readable inside the sandbox, and nothing next to them does.
    """
    roots: list[str] = []
    path = Path(executable)
    candidates = [path, Path(os.path.realpath(path))]
    for candidate in candidates:
        bin_dir = candidate.parent
        prefix = bin_dir.parent
        cfg = prefix / "pyvenv.cfg"
        if cfg.is_file():
            roots.append(str(prefix))
            try:
                for line in cfg.read_text(encoding="utf-8", errors="replace").splitlines():
                    key, _, value = line.partition("=")
                    if key.strip() == "home" and value.strip():
                        home_bin = Path(value.strip())
                        roots.append(str(home_bin.parent))
                        roots.append(str(Path(os.path.realpath(home_bin)).parent))
            except OSError:
                pass
        roots.append(str(prefix))
    # Python run from a venv reads the base interpreter's stdlib by prefix.
    return tuple(dict.fromkeys(r for r in roots if r and r != "/"))


_probe_lock = threading.Lock()
_probe_cache: dict[str, Isolation | None] = {}


def _probe(mode: str) -> Isolation | None:
    """The mode, if it runs a trivial command on this host; None otherwise."""
    names = {"bwrap": "bwrap", "sandbox-exec": "sandbox-exec", "unshare": "unshare"}
    if mode == "none":
        return Isolation(mode="none")
    executable = shutil.which(names[mode])
    if executable is None:
        return None
    isolation = Isolation(mode=mode, executable=executable)
    true_bin = shutil.which("true") or "/bin/true"
    import tempfile

    with tempfile.TemporaryDirectory(prefix="mira-verify-probe-") as scratch:
        root = Path(scratch)
        argv = isolation.wrap(
            [true_bin], root=root, cwd=root, network=False, read_only=toolchain_roots(true_bin)
        )
        outcome = sandbox.run_argv(argv, cwd=root, timeout_seconds=PROBE_TIMEOUT_SECONDS)
    if outcome.ran and outcome.exit_code == 0:
        return isolation
    logger.info(
        "Verification isolation %s is installed but does not work here (%s): %s",
        mode,
        outcome.status,
        (outcome.stderr or outcome.detail).strip()[:300],
    )
    return None


def resolve_isolation(mode: str, extra_read_only: tuple[str, ...] = ()) -> Isolation | None:
    """The isolation to run workspace commands under, or None when there is none.

    Probed once per process and mode. Blocking: call it off the event loop.
    """
    if mode == "auto":
        order = ["bwrap"] if sys.platform.startswith("linux") else []
        if sys.platform == "darwin":
            order.append("sandbox-exec")
    else:
        order = [mode]
    for candidate in order:
        with _probe_lock:
            if candidate not in _probe_cache:
                _probe_cache[candidate] = _probe(candidate)
            found = _probe_cache[candidate]
        if found is not None:
            if found.mode in {"unshare", "none"}:
                logger.warning(
                    "Verification runs pull-request code with isolation %r: %s",
                    found.mode,
                    "network removed, filesystem shared with Mira"
                    if found.mode == "unshare"
                    else "NO isolation — the code can read what Mira can read and reach the network",
                )
            return Isolation(
                mode=found.mode, executable=found.executable, extra_read_only=extra_read_only
            )
    return None
