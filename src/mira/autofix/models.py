"""Vocabulary and records for assisted correction.

Everything the autofix pipeline decides is expressed with the codes here, so a
job is a value the dashboard, the tests and the audit trail can all read
without re-deriving anything. Nothing in this module talks to a provider, a
store, a model or a subprocess — it is the shape of the work, not how the work
is done.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass, field
from typing import Any, Literal

# ── Job states ───────────────────────────────────────────────────────────────
#
# `queued`      Accepted and durable. Nothing has been generated and nothing
#               has been written; a worker will pick it up when one is free.
# `running`     A worker holds the lease and is asking a model for a patch.
# `validating`  A patch exists in memory and is being checked. Still nothing
#               written to the platform.
# `publishing`  The checks passed and a branch/commit/pull request is being
#               created. The only state in which Mira writes.
# `opened`      A reviewable change exists. Terminal, and the only success.
# `failed`      This attempt failed. Retried from `available_at` while attempts
#               remain; never a partial write left behind.
# `dead_letter` Out of attempts. Parked with the last error for a human.
# `cancelled`   An admin stopped it. Terminal, and never resumes.
JobState = Literal[
    "queued",
    "running",
    "validating",
    "publishing",
    "opened",
    "failed",
    "dead_letter",
    "cancelled",
]

JOB_STATES: tuple[JobState, ...] = (
    "queued",
    "running",
    "validating",
    "publishing",
    "opened",
    "failed",
    "dead_letter",
    "cancelled",
)

# States from which no worker will ever pick the job up again.
TERMINAL_STATES: frozenset[str] = frozenset({"opened", "dead_letter", "cancelled"})

# States a worker owns. A lease that expires in one of these means the worker
# died mid-flight, and the job is reclaimable.
LEASED_STATES: frozenset[str] = frozenset({"running", "validating", "publishing"})

# How the change reaches the reviewer.
#
# `branch_pr`  Mira's own branch, its own commit, its own pull request opened
#              against the branch under review. The default, and the only mode
#              that needs no write access to somebody else's branch.
# `pr_branch`  A commit pushed onto the pull request's own head branch. Opt-in
#              only, and refused outright when the head branch is the default
#              branch or lives on a fork.
# `handoff`    No write at all: the job is handed to an external agent through
#              an adapter, and Mira records what it handed over.
FixMode = Literal["branch_pr", "pr_branch", "handoff"]

FIX_MODES: tuple[FixMode, ...] = ("branch_pr", "pr_branch", "handoff")

# What the requester asked for. `all` is always bounded — see `select_findings`
# in `mira.autofix.service`, which records what it left out.
RequestKind = Literal["single", "all"]

REQUEST_KINDS: tuple[RequestKind, ...] = ("single", "all")

# `suggest` generates, validates and *renders* a patch without writing anything
# to the platform. It is the dry run this phase is rolled out with, and it is
# what "autofix is enabled but nothing may be written yet" means.
AutofixMode = Literal["off", "suggest", "on"]

AUTOFIX_MODES: tuple[AutofixMode, ...] = ("off", "suggest", "on")


class ReasonCode:
    """Stable identifiers for why a job landed where it did.

    Strings, not an enum, because they are persisted on job rows and read back
    by older code after an upgrade. Adding a code is safe; renaming one
    rewrites history, so codes are append-only.
    """

    # Refused before anything was queued.
    AUTOFIX_OFF = "autofix_off"
    KILL_SWITCH = "kill_switch"
    REPO_NOT_ENABLED = "repo_not_enabled"
    ACTOR_UNKNOWN = "actor_unknown"
    ACTOR_NOT_ALLOWED = "actor_not_allowed"
    ACTOR_LACKS_WRITE = "actor_lacks_write"
    PERMISSION_UNREADABLE = "permission_unreadable"
    PROVIDER_CANNOT_WRITE = "provider_cannot_write"
    PR_CLOSED = "pr_closed"
    FINDING_NOT_FOUND = "finding_not_found"
    FINDING_NOT_OPEN = "finding_not_open"
    FINDING_OTHER_PR = "finding_other_pr"
    NOTHING_TO_FIX = "nothing_to_fix"
    REQUEST_LIMIT = "request_limit"
    CONCURRENCY_LIMIT = "concurrency_limit"
    MODE_NOT_PERMITTED = "mode_not_permitted"

    # Refused while generating or applying.
    NO_PATCH = "no_patch"
    MODEL_FAILURE = "model_failure"
    PATCH_INVALID = "patch_invalid"
    PATCH_EMPTY = "patch_empty"
    PATCH_NOT_APPLICABLE = "patch_not_applicable"
    PATH_TRAVERSAL = "path_traversal"
    PATH_OUTSIDE_REPO = "path_outside_repo"
    PATH_PROTECTED = "path_protected"
    PATH_NOT_IN_DIFF = "path_not_in_diff"
    NEW_FILE_REFUSED = "new_file_refused"
    TOO_MANY_FILES = "too_many_files"
    TOO_MANY_LINES = "too_many_lines"
    PATCH_TOO_LARGE = "patch_too_large"
    ATTEMPT_LIMIT = "attempt_limit"
    JOB_TIMEOUT = "job_timeout"

    # Refused while validating.
    VALIDATION_FAILED = "validation_failed"
    VALIDATION_ERROR = "validation_error"
    VALIDATION_TIMEOUT = "validation_timeout"
    VALIDATION_NOT_RUN = "validation_not_run"
    COMMAND_NOT_ALLOWED = "command_not_allowed"
    SYNTAX_BROKEN = "syntax_broken"

    # Refused while publishing.
    DEFAULT_BRANCH_REFUSED = "default_branch_refused"
    FORK_HEAD_REFUSED = "fork_head_refused"
    PUBLISH_FAILED = "publish_failed"
    BRANCH_CONFLICT = "branch_conflict"
    STATE_UNREADABLE = "state_unreadable"

    # Outcomes.
    PATCH_READY = "patch_ready"
    SUGGEST_ONLY = "suggest_only"
    PR_OPENED = "pr_opened"
    COMMIT_PUSHED = "commit_pushed"
    HANDED_OFF = "handed_off"
    REUSED_EXISTING = "reused_existing"
    CI_RETRY = "ci_retry"
    CI_RETRY_LIMIT = "ci_retry_limit"
    CANCELLED_BY_ADMIN = "cancelled_by_admin"


# Reasons that mean "asking again will not help". A job that stops for one of
# these is dead-lettered immediately rather than burning its remaining attempts
# on a refusal that is a property of the request, not of the try.
NON_RETRYABLE_CODES: frozenset[str] = frozenset(
    {
        ReasonCode.AUTOFIX_OFF,
        ReasonCode.KILL_SWITCH,
        ReasonCode.REPO_NOT_ENABLED,
        ReasonCode.ACTOR_UNKNOWN,
        ReasonCode.ACTOR_NOT_ALLOWED,
        ReasonCode.ACTOR_LACKS_WRITE,
        ReasonCode.PERMISSION_UNREADABLE,
        ReasonCode.PROVIDER_CANNOT_WRITE,
        ReasonCode.FINDING_NOT_FOUND,
        ReasonCode.FINDING_OTHER_PR,
        ReasonCode.MODE_NOT_PERMITTED,
        ReasonCode.PATH_PROTECTED,
        ReasonCode.PATH_TRAVERSAL,
        ReasonCode.PATH_OUTSIDE_REPO,
        ReasonCode.DEFAULT_BRANCH_REFUSED,
        ReasonCode.FORK_HEAD_REFUSED,
        ReasonCode.COMMAND_NOT_ALLOWED,
        ReasonCode.VALIDATION_NOT_RUN,
        ReasonCode.CANCELLED_BY_ADMIN,
        ReasonCode.CI_RETRY_LIMIT,
    }
)


@dataclass(frozen=True)
class Reason:
    """One reason, in the pipeline's own words.

    ``message`` is rendered to humans and may quote repository data (a path, a
    command name, a compiler error). It is never interpreted: nothing
    downstream parses a reason back into a decision or a command.
    """

    code: str
    message: str
    # "refused" — the request will not proceed; "info" — context only.
    kind: str = "refused"

    def as_dict(self) -> dict[str, str]:
        return {"code": self.code, "message": self.message, "kind": self.kind}


@dataclass(frozen=True)
class FileEdit:
    """One exact-match replacement inside one file.

    Deliberately *not* a unified diff. A model that emits a diff has to invent
    line numbers and hunk headers, and a diff that almost applies is the worst
    possible artifact — it either fails opaquely or lands in the wrong place.
    An anchored find/replace either matches the file byte for byte or it does
    not, and "it did not" is a clean refusal Mira can explain.
    """

    path: str
    find: str
    replace: str
    # Free-text, rendered next to the hunk. Never executed, never parsed.
    rationale: str = ""

    def as_dict(self) -> dict[str, str]:
        return {
            "path": self.path,
            "find": self.find,
            "replace": self.replace,
            "rationale": self.rationale,
        }


@dataclass
class FixPatch:
    """A generated change, before anything has been written anywhere."""

    edits: list[FileEdit] = field(default_factory=list)
    summary: str = ""
    rationale: str = ""
    model: str = ""
    prompt_digest: str = ""
    # Rendered unified diff, produced by Mira from the applied result rather
    # than by the model, so what is displayed is what would be committed.
    diff: str = ""
    # path → post-edit content. Populated by `apply_patch`, empty before that.
    files: dict[str, str] = field(default_factory=dict)
    changed_files: int = 0
    added_lines: int = 0
    deleted_lines: int = 0

    @property
    def empty(self) -> bool:
        return not self.edits

    @property
    def digest(self) -> str:
        """Content hash of the applied result, so a retry can recognise itself."""
        payload = json.dumps(
            {"files": self.files, "edits": [edit.as_dict() for edit in self.edits]},
            sort_keys=True,
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]

    def as_dict(self) -> dict[str, Any]:
        return {
            "edits": [edit.as_dict() for edit in self.edits],
            "summary": self.summary,
            "rationale": self.rationale,
            "model": self.model,
            "prompt_digest": self.prompt_digest,
            "diff": self.diff,
            "changed_files": self.changed_files,
            "added_lines": self.added_lines,
            "deleted_lines": self.deleted_lines,
            "digest": self.digest,
            "paths": sorted(self.files),
        }


@dataclass
class CheckResult:
    """One validation command, or one built-in check, and what it said."""

    name: str
    # "passed" | "failed" | "skipped" | "error" | "timeout"
    outcome: str
    detail: str = ""
    duration_seconds: float = 0.0
    exit_code: int | None = None

    @property
    def blocking(self) -> bool:
        """Anything that is not an unambiguous pass blocks publication.

        `skipped` does not: a check that never ran had no opinion. Everything
        else — including an error in the harness itself — is treated as a
        failure, because the alternative is publishing on a check Mira could
        not actually perform.
        """
        return self.outcome not in {"passed", "skipped"}

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "outcome": self.outcome,
            "detail": self.detail,
            "duration_seconds": round(self.duration_seconds, 3),
            "exit_code": self.exit_code,
        }


@dataclass
class ValidationResult:
    """Everything that was checked, and whether the patch survived all of it."""

    checks: list[CheckResult] = field(default_factory=list)
    # Explicit rather than derived from `checks` being empty: "nothing was
    # configured to run" and "everything that ran passed" must not be the same
    # value, because only one of them is evidence.
    executed: bool = False

    @property
    def ok(self) -> bool:
        """Whether the patch may be published on this evidence.

        ``executed`` is part of the answer, not a label beside it. An install
        with the syntax check off and no commands configured produces an empty
        check list, and "nothing objected" is not the same claim as "something
        looked". Publishing on the first would mean writing a model's output to
        a repository having verified nothing about it, which is the one thing
        the validation phase exists to prevent.
        """
        return self.executed and not any(check.blocking for check in self.checks)

    @property
    def failures(self) -> list[CheckResult]:
        return [check for check in self.checks if check.blocking]

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "executed": self.executed,
            "checks": [check.as_dict() for check in self.checks],
        }


@dataclass
class AutofixJob:
    """One unit of durable work: one finding, one pull request, one outcome."""

    job_key: str = ""
    id: int = 0
    state: JobState = "queued"
    mode: FixMode = "branch_pr"
    request_kind: RequestKind = "single"
    platform: str = "github"
    owner: str = ""
    repo: str = ""
    pr_number: int = 0
    pr_url: str = ""
    base_branch: str = ""
    head_branch: str = ""
    head_sha: str = ""
    finding_id: str = ""
    finding_title: str = ""
    requested_by: str = ""
    # The batch a `fix all` request produced, so a dashboard can group them and
    # a limit can be enforced across the whole request rather than per job.
    request_id: str = ""
    policy_version: str = ""
    attempts: int = 0
    max_attempts: int = 2
    ci_attempts: int = 0
    max_ci_attempts: int = 1
    available_at: float = 0.0
    lease_owner: str = ""
    lease_expires_at: float = 0.0
    branch_name: str = ""
    commit_sha: str = ""
    child_pr_url: str = ""
    child_pr_number: int = 0
    model: str = ""
    patch_digest: str = ""
    diff: str = ""
    reasons: list[Reason] = field(default_factory=list)
    validation: ValidationResult = field(default_factory=ValidationResult)
    handoff_ref: str = ""
    cancelled_by: str = ""
    error: str = ""
    created_at: float = 0.0
    updated_at: float = 0.0

    @property
    def terminal(self) -> bool:
        return self.state in TERMINAL_STATES

    @property
    def wrote_anything(self) -> bool:
        """Whether this job has already put something on the platform."""
        return bool(self.branch_name or self.commit_sha or self.child_pr_url)

    def reason_codes(self) -> list[str]:
        return [reason.code for reason in self.reasons]

    def as_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["reasons"] = [reason.as_dict() for reason in self.reasons]
        data["validation"] = self.validation.as_dict()
        data["terminal"] = self.terminal
        return data


@dataclass
class AutofixAttempt:
    """One try at one job, recorded whether it worked or not.

    Append-only. A job row carries the latest state; the attempts carry the
    story, which is the only thing that makes "it opened a pull request on the
    third try" reviewable rather than merely true.
    """

    id: int = 0
    job_id: int = 0
    job_key: str = ""
    attempt: int = 0
    # "generate" | "apply" | "validate" | "publish" | "handoff" | "ci_retry"
    phase: str = ""
    outcome: str = ""
    model: str = ""
    prompt_digest: str = ""
    patch_digest: str = ""
    diff: str = ""
    reasons: list[Reason] = field(default_factory=list)
    validation: ValidationResult = field(default_factory=ValidationResult)
    detail: str = ""
    duration_seconds: float = 0.0
    created_at: float = 0.0

    def as_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["reasons"] = [reason.as_dict() for reason in self.reasons]
        data["validation"] = self.validation.as_dict()
        return data


def _digest(parts: list[str]) -> str:
    return hashlib.sha256("\x1f".join(parts).encode("utf-8")).hexdigest()


def job_key(
    *,
    platform: str,
    owner: str,
    repo: str,
    pr_number: int,
    head_sha: str,
    finding_id: str,
    mode: str,
) -> str:
    """Identity of one piece of correction work, for idempotent enqueueing.

    Excludes the requester and the request kind on purpose: two maintainers
    typing ``fix`` on the same finding at the same commit want one branch
    between them, not two competing ones. It *includes* the head commit and the
    mode, because a finding re-raised after a push is a different fix, and a
    commit onto the pull request's branch is a different act from a stacked
    pull request.
    """
    return _digest([platform, owner, repo, str(pr_number), head_sha, finding_id, mode])


def request_id(*, platform: str, owner: str, repo: str, pr_number: int, head_sha: str) -> str:
    """Identity of one ``fix all`` batch, so its limit applies to the batch."""
    return _digest([platform, owner, repo, str(pr_number), head_sha])[:16]


# A branch name is a ref on every server there is and an argument to every git
# plumbing call. It is built from a template Mira controls and a slug derived
# from data it does not, so the slug is reduced to an alphabet with no way out.
_SLUG_UNSAFE = re.compile(r"[^a-z0-9]+")
_MAX_SLUG = 32

# Refs git itself refuses, plus the shapes that make a ref ambiguous. Checked
# rather than assumed: `sanitize_slug` cannot produce them, but the prefix
# comes from configuration and the number comes from a webhook.
_INVALID_REF = re.compile(r"(^/)|(//)|(/$)|(\.\.)|(@\{)|([\x00-\x20~^:?*\[\\])|(\.lock$)|(^@$)")


def sanitize_slug(value: str, *, limit: int = _MAX_SLUG) -> str:
    """Reduce arbitrary text to ``[a-z0-9-]``, or ``""`` if nothing survives."""
    slug = _SLUG_UNSAFE.sub("-", (value or "").lower()).strip("-")
    return slug[:limit].strip("-")


def branch_name(
    *,
    prefix: str,
    pr_number: int,
    finding_id: str,
    request_kind: str = "single",
    title: str = "",
) -> str:
    """A deterministic, collision-resistant, git-legal branch name.

    Deterministic because a retry has to land on the branch the previous
    attempt created rather than beside it. Collision-resistant because the
    readable part is derived from a title Mira does not control, and two
    findings on one pull request must never share a branch — so the identity
    comes from the finding id, and the title is decoration that can be dropped
    entirely without changing which branch this is.
    """
    stem = sanitize_slug(finding_id.replace("-", ""), limit=12) or _digest([finding_id])[:12]
    parts = [prefix.strip("/"), f"pr-{int(pr_number)}"]
    if request_kind == "all":
        parts.append("all")
    parts.append(stem)
    readable = sanitize_slug(title, limit=24)
    if readable:
        parts.append(readable)
    candidate = "/".join(part for part in parts if part)
    if _INVALID_REF.search(candidate) or not candidate:
        # The prefix is the only component that can still be nonsense here, so
        # drop it rather than emitting a ref the platform will reject.
        candidate = "/".join(["mira-fix", f"pr-{int(pr_number)}", stem])
    return candidate


def dumps(value: Any) -> str:
    """Stable JSON for persisted columns, so two runs produce equal bytes."""
    return json.dumps(value, sort_keys=True, default=str)


def loads(value: str, fallback: Any) -> Any:
    """Parse a persisted JSON column, tolerating rows written by older code."""
    if not value:
        return fallback
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return fallback


def reasons_from_json(blob: str) -> list[Reason]:
    return [
        Reason(
            code=str(item.get("code", "")),
            message=str(item.get("message", "")),
            kind=str(item.get("kind", "refused")),
        )
        for item in loads(blob, [])
        if isinstance(item, dict)
    ]


def validation_from_json(blob: str) -> ValidationResult:
    data = loads(blob, {})
    if not isinstance(data, dict):
        return ValidationResult()
    checks = [
        CheckResult(
            name=str(item.get("name", "")),
            outcome=str(item.get("outcome", "error")),
            detail=str(item.get("detail", "")),
            duration_seconds=float(item.get("duration_seconds", 0.0) or 0.0),
            exit_code=item.get("exit_code"),
        )
        for item in data.get("checks", [])
        if isinstance(item, dict)
    ]
    return ValidationResult(checks=checks, executed=bool(data.get("executed")))
