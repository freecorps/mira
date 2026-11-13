"""Configuration for running a pull request's code during its review.

Kept out of :mod:`mira.config` so that module only has to import it, the way it
imports the checks and quality sections: this one is long, and every field in it
needs its reason next to it.

**Nothing in a pull request can reach any value defined here.** The commands,
the setup steps, the test runners, the limits and the isolation mode come from
the deployment's ``mira.yaml``, the admin overrides in the dashboard database,
and the per-repository entries under ``review.verify.repositories`` — the same
places the autofix allowlist and the pre-merge tools come from. A repository's
own ``.mira.yaml`` on the pull request's head is part of what is being
reviewed; if it could name a command, the person who opened the pull request
would choose what Mira runs.

That is a narrower promise than it was for the linters, and the docs say so:
the pull request's *code* does run — that is the feature — but only inside the
workspace sandbox, never with Mira's credentials, and never with the network
once setup is over. See ``docs/verify.md``.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, field_validator

#: Output formats Mira can turn into line-anchored diagnostics.
VERIFY_PARSERS = frozenset({"ruff", "eslint", "tsc", "pytest", "text"})

#: How a workspace command is fenced in. ``auto`` picks the strongest one that
#: works on this host and refuses to run anything when none does.
ISOLATION_MODES = ("auto", "bwrap", "sandbox-exec", "unshare", "none")

#: Categories a reproduction test can say something about. A style or
#: maintainability finding has no failing test; a bug does.
DEFAULT_VERIFY_CATEGORIES = (
    "bug",
    "security",
    "error-handling",
    "race-condition",
    "concurrency",
    "resource-leak",
)


def _valid_argv(argv: list[str], label: str) -> list[str]:
    if not isinstance(argv, list) or not argv:
        raise ValueError(f"{label} must be a non-empty list of arguments")
    if not all(isinstance(part, str) and part for part in argv):
        raise ValueError(f"{label} must contain only non-empty strings")
    return argv


class VerifyCommand(BaseModel):
    """One command run in the review workspace: a setup step or a tool.

    ``command`` is an argument *list*; there is no shell. Two placeholders are
    expanded, both to paths Mira computed from the diff and the repository
    tree: ``{files}`` to the changed files (narrowed by ``extensions``) and
    ``{tests}`` to the existing tests related to them. A command whose
    placeholder expands to nothing is skipped rather than run over the whole
    repository by accident.
    """

    name: str = ""
    command: list[str]
    # How to read the output. `ruff` and `eslint` expect their JSON formats
    # (`--output-format json`, `--format json`); `tsc` the default
    # `file(line,col): error TS…` lines; `pytest` the short summary; `text`
    # any `path:line[:col]: message` output (mypy, flake8, go vet, …).
    parser: str = "text"
    # Changed files `{files}` expands to. Empty: every changed file.
    extensions: list[str] = Field(default_factory=list)
    # Per-command ceiling; the section's `command_timeout_seconds` otherwise.
    timeout_seconds: float | None = Field(default=None, gt=0, le=1800)
    # Raise this tool's diagnostics on changed lines as review findings, unless
    # the pre-merge checks already run a tool of the same name (they post their
    # own). Off, its output is still shown to the reviewer.
    post_findings: bool = True

    @field_validator("command")
    @classmethod
    def _valid_command(cls, v: list[str]) -> list[str]:
        return _valid_argv(v, "review.verify command")

    @field_validator("parser")
    @classmethod
    def _valid_parser(cls, v: str) -> str:
        value = (v or "text").strip().lower()
        if value not in VERIFY_PARSERS:
            raise ValueError(
                f"review.verify parser {v!r} is not one of {', '.join(sorted(VERIFY_PARSERS))}"
            )
        return value

    @property
    def label(self) -> str:
        return self.name.strip() or " ".join(self.command[:2])


class VerifyTestRunner(BaseModel):
    """How a reproduction test is run for findings in one language.

    ``command`` must contain ``{test}``, which expands to the test file Mira
    wrote — inside the repository's own test directory, so its conftest,
    fixtures and import paths apply. ``canary`` is a test that must *fail*
    with ``MIRA-CANARY`` in its output; a harness that reports it as passing
    is one whose passes prove nothing, and "not reproduced" is then never
    acted on. Python runners get a built-in canary when none is given.
    """

    name: str = ""
    # Source files whose findings this runner can reproduce.
    extensions: list[str]
    command: list[str]
    canary: str | None = None

    @field_validator("command")
    @classmethod
    def _valid_command(cls, v: list[str]) -> list[str]:
        _valid_argv(v, "review.verify.test_runners[].command")
        if "{test}" not in v:
            raise ValueError("review.verify.test_runners[].command must contain '{test}'")
        return v

    @field_validator("extensions")
    @classmethod
    def _valid_extensions(cls, v: list[str]) -> list[str]:
        cleaned = [e.strip().lower() for e in v if e and e.strip()]
        if not cleaned:
            raise ValueError("review.verify.test_runners[].extensions must name at least one")
        return [e if e.startswith(".") else f".{e}" for e in cleaned]

    @property
    def label(self) -> str:
        return self.name.strip() or " ".join(self.command[:2])


class VerifyRepoOverride(BaseModel):
    """Per-repository overrides, keyed ``owner/repo`` under ``review.verify.repositories``.

    ``None`` inherits. A list set to ``[]`` means "none here", which is how a
    repository opts out of an inherited command rather than inheriting it.
    """

    enabled: bool | None = None
    setup: list[VerifyCommand] | None = None
    commands: list[VerifyCommand] | None = None
    test_runners: list[VerifyTestRunner] | None = None
    max_findings: int | None = Field(default=None, ge=0, le=20)
    test_timeout_seconds: float | None = Field(default=None, gt=0, le=600)
    budget_seconds: float | None = Field(default=None, gt=0, le=1800)
    setup_timeout_seconds: float | None = Field(default=None, gt=0, le=3600)
    setup_network: bool | None = None


class VerifyConfig(BaseModel):
    """Execution-backed review: tool output in the prompt, findings checked by a test.

    Off by default, and it should stay off until a deployment means it: with it
    on, Mira writes the pull request's head to disk and runs code from it.
    Turning it on without ``commands`` or ``test_runners`` does nothing
    beyond preparing a workspace — each half is opt-in by being configured.
    """

    enabled: bool = False

    # ── Isolation ──────────────────────────────────────────────────────
    # `auto` uses bubblewrap on Linux (own mount, PID and network namespaces;
    # only the workspace is writable and nothing of Mira's is visible) and
    # sandbox-exec on macOS (no network; no writes outside the workspace; the
    # home directory unreadable). With neither working, nothing runs. `unshare`
    # removes only the network and `none` removes nothing — both must be named
    # explicitly, and docs/verify.md explains why that is rarely a good idea.
    isolation: Literal["auto", "bwrap", "sandbox-exec", "unshare", "none"] = "auto"
    # Host paths made readable inside a bubblewrap sandbox, for toolchains
    # installed outside /usr, /opt and the venv a binary resolves into.
    extra_read_only_paths: list[str] = Field(default_factory=list)

    # ── Workspace ──────────────────────────────────────────────────────
    # The workspace is the review's repository snapshot of the head commit
    # (text files only; vendored and build directories are left out). A
    # snapshot larger than this is not written.
    max_workspace_mb: int = Field(default=200, ge=1, le=4096)
    # Commands that install what the tests need — `["uv", "sync", "--frozen"]`,
    # `["npm", "ci", "--ignore-scripts"]` — run once, before anything else, in
    # order. A step that fails stops the rest; what follows still runs, and a
    # test that cannot import is simply inconclusive.
    setup: list[VerifyCommand] = Field(default_factory=list)
    # Setup is the only phase with network access, and only when this is set:
    # installing dependencies needs a registry. Every later command runs with
    # no network at all.
    setup_network: bool = True
    setup_timeout_seconds: float = Field(default=300.0, gt=0, le=3600)
    # Ceilings applied to every command through POSIX rlimits. Node and the JVM
    # reserve far more address space than they use; raise the memory limit
    # before blaming a test for a crash.
    memory_limit_mb: int = Field(default=4096, ge=256, le=65536)
    cpu_seconds: int = Field(default=300, ge=1, le=3600)

    # ── Tool evidence ──────────────────────────────────────────────────
    # Fast, deterministic commands whose diagnostics on the changed lines are
    # shown to every review part ("Tool output on changed lines").
    commands: list[VerifyCommand] = Field(default_factory=list)
    command_timeout_seconds: float = Field(default=60.0, gt=0, le=1800)
    # How long a review part waits for the workspace and the tools, counted
    # from the start of the review. Past it, parts are reviewed without the
    # block rather than held for a slow install.
    evidence_wait_seconds: float = Field(default=90.0, ge=0, le=1800)
    # Characters of tool output one part's prompt may carry.
    max_evidence_chars: int = Field(default=6_000, ge=500, le=50_000)
    # Tool diagnostics raised as findings per review.
    max_tool_findings: int = Field(default=10, ge=0, le=100)

    # ── Finding verification ───────────────────────────────────────────
    # How reproduction tests are run, per language. Empty: no finding is
    # verified.
    test_runners: list[VerifyTestRunner] = Field(default_factory=list)
    # Findings eligible for a reproduction test: these categories, at or above
    # this severity, from the review and security passes.
    categories: list[str] = Field(default_factory=lambda: list(DEFAULT_VERIFY_CATEGORIES))
    min_severity: Literal["warning", "blocker"] = "warning"
    # Findings verified per review, most severe and most confident first.
    max_findings: int = Field(default=3, ge=0, le=20)
    # Wall-clock limit for one run of one test.
    test_timeout_seconds: float = Field(default=60.0, gt=0, le=600)
    # Wall-clock limit for the whole verification step — writing the tests
    # included — so a review's latency grows by at most this much.
    budget_seconds: float = Field(default=180.0, gt=0, le=1800)
    # Verifications in flight at once.
    max_concurrent: int = Field(default=3, ge=1, le=10)
    # Each test is run this many times and must give the same answer every
    # time; a test that passes once and fails once is flaky, and inconclusive.
    runs: int = Field(default=2, ge=1, le=5)

    repositories: dict[str, VerifyRepoOverride] = Field(default_factory=dict)

    @field_validator("categories")
    @classmethod
    def _normalise_categories(cls, v: list[str]) -> list[str]:
        return [c.strip().lower() for c in v if c and c.strip()]

    @field_validator("extra_read_only_paths")
    @classmethod
    def _absolute_paths(cls, v: list[str]) -> list[str]:
        for path in v:
            if not path.startswith("/"):
                raise ValueError("review.verify.extra_read_only_paths must be absolute paths")
        return v
