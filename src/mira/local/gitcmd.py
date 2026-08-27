"""Talking to git without being able to change anything.

Every git invocation in the local review surface goes through :func:`run_git`,
and :func:`run_git` refuses any subcommand that is not on a reviewed allowlist.
That is a stronger guarantee than "we only wrote read-only calls": the
allowlist is a single, small, testable statement, and a future caller that
reaches for ``git stash`` or ``git add -N`` — both of which are tempting ways to
make a working-tree diff nicer — fails immediately rather than quietly
modifying somebody's checkout.

Three further precautions, all of them about a review that must not have side
effects and must not hang:

* ``--no-optional-locks`` and ``GIT_OPTIONAL_LOCKS=0``, so a status or a diff
  never refreshes the index on disk. Read-only means read-only including the
  bytes under ``.git``.
* ``GIT_TERMINAL_PROMPT=0`` and an empty ``GIT_ASKPASS``, so no invocation can
  block a CI job on a credential prompt. Nothing here talks to a remote, but a
  repository with a misconfigured ``insteadOf`` can surprise you.
* ``diff.external=`` on every call and ``core.quotepath=false`` everywhere, so
  the output is the diff git itself produces and paths arrive as their real
  bytes rather than as octal escapes.

The environment is the sandbox allowlist plus the handful of names git needs to
find its own configuration. It deliberately does *not* inherit ``GIT_DIR`` or
``GIT_WORK_TREE``: the repository is the one we resolved, not the one an
ambient variable points at.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from mira.sandbox import run_argv

#: Subcommands the local review may run. Every one of them is read-only in
#: every form this package uses it. `remote` is here for `get-url` alone and is
#: narrowed further below; `config` is deliberately absent, because its
#: read-only and write forms differ by a single argument.
READ_ONLY_SUBCOMMANDS = frozenset(
    {
        "cat-file",
        "diff",
        "ls-files",
        "merge-base",
        "remote",
        "rev-list",
        "rev-parse",
        "show",
        "status",
        "version",
    }
)

#: `git remote` grows write forms (`add`, `remove`, `set-url`, `rename`). Only
#: the forms that read are allowed, and the check is on the first argument.
_REMOTE_READ_ONLY = frozenset({"get-url", "show", "-v", "--verbose"})

#: Config git is invoked with, on every call. These change how git *reports*,
#: never what it does.
_CONFIG_ARGS = (
    # Paths as their real bytes, not octal escapes, so a non-ASCII filename
    # round-trips into the diff the engine parses.
    "-c",
    "core.quotepath=false",
    # A repository that configures an external diff driver would otherwise run
    # it here and hand back something that is not a unified diff.
    "-c",
    "diff.external=",
    # Rename detection is what makes a rename read as a rename rather than as a
    # delete plus an add. Set explicitly so a repository that turned it off
    # does not change the shape of a local review.
    "-c",
    "diff.renames=true",
)

#: Environment names git needs to locate its own configuration, carried over
#: from the caller's environment on top of the sandbox allowlist.
_GIT_ENV_PASSTHROUGH = (
    "HOME",
    "USERPROFILE",
    "HOMEDRIVE",
    "HOMEPATH",
    "APPDATA",
    "LOCALAPPDATA",
    "PROGRAMDATA",
    "XDG_CONFIG_HOME",
)

#: git's own name for the empty tree. Used as the base when ``HEAD`` does not
#: exist yet, so a repository with no commits still produces a diff instead of
#: an error nobody can act on.
EMPTY_TREE = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"

DEFAULT_TIMEOUT_SECONDS = 120.0


class GitError(Exception):
    """Git could not answer. Never a statement about the code under review."""


class GitCommandRefused(GitError):
    """A caller asked for a subcommand the local review is not allowed to run.

    A programming error rather than a user error: it means someone added a call
    that could modify the developer's checkout.
    """


@dataclass(frozen=True)
class GitResult:
    """One git invocation's output."""

    argv: tuple[str, ...]
    exit_code: int
    stdout: str
    stderr: str

    @property
    def ok(self) -> bool:
        return self.exit_code == 0


def _assert_allowed(args: tuple[str, ...]) -> None:
    if not args:
        raise GitCommandRefused("no git subcommand was given")
    subcommand = args[0]
    if subcommand not in READ_ONLY_SUBCOMMANDS:
        raise GitCommandRefused(
            f"git {subcommand!r} is not on the local review's read-only allowlist"
        )
    if subcommand == "remote" and len(args) > 1:
        # Bare `git remote` lists them and is read-only. Every subcommand that
        # writes takes a name, so anything with an argument must be named here.
        following = args[1]
        if following not in _REMOTE_READ_ONLY:
            raise GitCommandRefused(f"git remote {following!r} is not a read-only form")


def git_env() -> dict[str, str]:
    """The environment every git invocation runs with."""
    extra = {name: os.environ[name] for name in _GIT_ENV_PASSTHROUGH if name in os.environ}
    extra.update(
        {
            # Nothing here contacts a remote, and a job that blocks on a
            # username prompt is worse than one that fails.
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_ASKPASS": "",
            "GIT_OPTIONAL_LOCKS": "0",
            "GIT_PAGER": "cat",
            "PAGER": "cat",
        }
    )
    return extra


def run_git(
    repo_root: str | Path,
    *args: str,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    max_output_bytes: int = 20_000_000,
) -> GitResult:
    """Run one read-only git command in ``repo_root``.

    Raises :class:`GitCommandRefused` for a subcommand outside the allowlist and
    :class:`GitError` when git could not be started at all. A git command that
    ran and exited non-zero is *returned*, not raised: several callers here ask
    questions where "no" is a normal answer.
    """
    _assert_allowed(args)
    argv = ["git", "--no-optional-locks", "--no-pager", *_CONFIG_ARGS, *args]
    outcome = run_argv(
        argv,
        cwd=repo_root,
        timeout_seconds=timeout_seconds,
        # A diff is repository text and may be in any encoding on disk. Decoding
        # it strictly would turn one Latin-1 source file into a crash.
        encoding="utf-8",
        errors="replace",
        max_output_bytes=max_output_bytes,
        env=git_env(),
    )
    if outcome.status == "missing":
        raise GitError("git is not installed or not on PATH")
    if outcome.status == "timeout":
        raise GitError(f"git {args[0]} did not finish within {timeout_seconds:g}s")
    if outcome.status == "error":
        raise GitError(outcome.detail or f"git {args[0]} could not be run")
    return GitResult(
        argv=tuple(argv),
        exit_code=outcome.exit_code,
        stdout=outcome.stdout,
        stderr=outcome.stderr,
    )


def git_text(repo_root: str | Path, *args: str, **kwargs: Any) -> str:
    """Run a git command that is expected to succeed, and return its stdout."""
    result = run_git(repo_root, *args, **kwargs)
    if not result.ok:
        detail = (result.stderr or result.stdout).strip().splitlines()
        message = detail[0] if detail else f"exit {result.exit_code}"
        raise GitError(f"git {args[0]} failed: {message}")
    return result.stdout


def find_repo_root(start: str | Path) -> Path:
    """The top level of the repository containing ``start``.

    Raises :class:`GitError` when ``start`` is not inside a work tree — which
    includes being inside a bare repository, where there is nothing local to
    review.
    """
    start_path = Path(start).resolve()
    if not start_path.is_dir():
        start_path = start_path.parent
    if not start_path.is_dir():
        raise GitError(f"{start} does not exist")
    result = run_git(start_path, "rev-parse", "--show-toplevel")
    top = result.stdout.strip()
    if not result.ok or not top:
        raise GitError(f"{start_path} is not inside a git work tree")
    return Path(top).resolve()


def head_exists(repo_root: str | Path) -> bool:
    """False in a repository whose first commit has not been made yet."""
    return run_git(repo_root, "rev-parse", "--verify", "--quiet", "HEAD^{commit}").ok


def current_branch(repo_root: str | Path) -> str:
    """The checked-out branch, or "" when detached or unborn."""
    result = run_git(repo_root, "rev-parse", "--abbrev-ref", "HEAD")
    name = result.stdout.strip() if result.ok else ""
    return "" if name in ("", "HEAD") else name


def resolve_commit(repo_root: str | Path, revision: str) -> str:
    """The full SHA a revision names, or raise :class:`GitError`.

    Refuses anything beginning with ``-`` before asking git. A revision comes
    from the command line, and a value like ``--output=/etc/passwd`` must be
    rejected as a revision rather than reaching git as an option.
    """
    candidate = (revision or "").strip()
    if not candidate:
        raise GitError("an empty string is not a revision")
    if candidate.startswith("-"):
        raise GitError(f"{revision!r} is not a revision (it looks like an option)")
    result = run_git(repo_root, "rev-parse", "--verify", "--quiet", candidate + "^{commit}")
    sha = result.stdout.strip()
    if not result.ok or not sha:
        raise GitError(f"{revision!r} does not resolve to a commit in this repository")
    return sha


def merge_base(repo_root: str | Path, left: str, right: str) -> str:
    """The best common ancestor of two commits, or raise when there is none."""
    result = run_git(repo_root, "merge-base", left, right)
    base = result.stdout.strip()
    if not result.ok or not base:
        raise GitError(f"{left} and {right} have no common ancestor")
    return base
