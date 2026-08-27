"""Vocabulary and records for pre-merge checks.

A check answers one question about a pull request and says how it knows. This
module is the shape of that answer — the states it can take, the evidence it
must carry, and the identity that makes running it twice produce one row
instead of two. Nothing here talks to a provider, a store, a tool or a model.

The state vocabulary is the whole point of the phase, so it is worth being
explicit about why there are five of them rather than the usual two.

``pass``                  The check ran, looked, and found nothing to report.
``violation``             The check ran, looked, and found something the
                          project's own policy says is wrong. This is the only
                          state that is a statement about the *pull request*.
``infrastructure_error``  The check could not run, or ran and could not reach a
                          conclusion: a model that would not answer, a network
                          that refused, a store that was unavailable. It says
                          something about Mira, never about the change.
``skipped``               The check had no business running: it is off, the
                          diff is out of its scope, or the tool it needs is not
                          installed. Also a statement about Mira, and a
                          deliberately different one from an error.
``timeout``               The check was still running when its budget ran out.
                          Split from ``infrastructure_error`` because the
                          remedy is different — a timeout is usually a budget
                          to raise, an error is usually something to fix.

The distinction that makes the phase worth doing is ``violation`` against the
other four. A tool that is not installed, a model that timed out and a
repository the check does not apply to must never be rendered as "your pull
request has a problem". Everything below is arranged so that stating one as the
other requires deliberately writing the wrong constant.

Fail-closed lives here too, and is deliberately *not* the same question as what
gets shown. :attr:`CheckResult.incomplete` marks every result that did not
reach a conclusion — including a ``skipped`` one that was skipped because
something was missing rather than because it did not apply. A gate reads that,
so a check the operator declared blocking cannot be satisfied by failing to
run; a human reads the state, so the same result still reads as "skipped: ruff
is not installed" and not as a violation.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from typing import Any, Literal

CheckState = Literal["pass", "violation", "infrastructure_error", "skipped", "timeout"]

CHECK_STATES: tuple[CheckState, ...] = (
    "pass",
    "violation",
    "infrastructure_error",
    "skipped",
    "timeout",
)

# States that are *not* a statement about the pull request. Consulted anywhere
# a caller is about to render a result to a human or count it as a finding.
NON_VIOLATION_STATES: frozenset[str] = frozenset(
    {"pass", "infrastructure_error", "skipped", "timeout"}
)

# States where the check reached no conclusion. `skipped` is absent on purpose:
# whether a skip counts as incomplete depends on *why* it was skipped, which is
# `SkipReason`'s job below.
INCONCLUSIVE_STATES: frozenset[str] = frozenset({"infrastructure_error", "timeout"})

CheckMode = Literal["off", "warning", "error"]

CHECK_MODES: tuple[CheckMode, ...] = ("off", "warning", "error")


class SkipReason:
    """Why a check did not run. Strings, because they are persisted.

    The split that matters is between *did not apply* and *could not run*. A
    check that does not apply to this pull request has answered the question
    correctly; a check whose tool is missing has not answered it at all, and a
    gate must not read the second as the first.
    """

    # Did not apply. "No opinion" is the correct answer here.
    NOT_APPLICABLE = "not_applicable"
    OUT_OF_SCOPE = "out_of_scope"
    DISABLED = "disabled"
    KILL_SWITCH = "kill_switch"

    # Could not run, or ran and could not conclude. The question stands
    # unanswered, and every reason below counts as incomplete for a gate.
    TOOL_MISSING = "tool_missing"
    # The thing the check reads has not finished yet. A CI run still in flight
    # is the case this exists for: "not finished" is not "passed", and a
    # blocking check must not be satisfied by asking early.
    PENDING = "pending"
    UNSUPPORTED = "unsupported"
    BUDGET_EXHAUSTED = "budget_exhausted"
    NO_EVIDENCE = "no_evidence"
    AMBIGUOUS = "ambiguous"


# Skips that leave the question open. A check in ``error`` mode that ends in
# one of these still fails a gate closed, while still being *shown* as the skip
# it is — which is the whole reason the two questions are asked separately.
UNANSWERED_SKIPS: frozenset[str] = frozenset(
    {
        SkipReason.TOOL_MISSING,
        SkipReason.PENDING,
        SkipReason.UNSUPPORTED,
        SkipReason.BUDGET_EXHAUSTED,
        SkipReason.NO_EVIDENCE,
        SkipReason.AMBIGUOUS,
    }
)

# Where a result came from. Recorded per result so the dashboard can say
# "semgrep said this" rather than "Mira said this", and so a deduplicated
# finding can name both of its sources.
CheckOrigin = Literal["native", "natural_language", "tool", "context"]

CHECK_ORIGINS: tuple[CheckOrigin, ...] = ("native", "natural_language", "tool", "context")

# What a whole run means to a merge gate.
#
# `pass`        every blocking check answered, and none of them objected.
# `violation`   a blocking check found something wrong with the pull request.
# `incomplete`  a blocking check did not answer. Not an approval, and not a
#               violation either — the gate says so in those words.
# `not_run`     checks did not run for this pull request at all.
RunVerdict = Literal["pass", "violation", "incomplete", "not_run"]

RUN_VERDICTS: tuple[RunVerdict, ...] = ("pass", "violation", "incomplete", "not_run")

# Name of the check run / commit status the framework publishes. Stable for the
# same reason the gate's is: republishing under a new name would leave the old
# one on the commit forever.
STATUS_CONTEXT = "mira/pre-merge-checks"

# Marker for the framework's PR comment, so an update lands in place.
COMMENT_MARKER = "<!-- mira:pre-merge-checks -->"


def mira_status_contexts() -> frozenset[str]:
    """Every check-run name Mira publishes itself.

    Providers filter these out of the CI they report back. Without it the CI
    check reads Mira's own red status as a failing build, concludes CI is
    failing, publishes a red status saying so, and does it again on the next
    event — and the gate, which counts CI checks into its inputs digest,
    manufactures a new decision every pass. The merge gate hit exactly this and
    solved it for its own context; a second published context needs the same
    exclusion or it reintroduces the loop.
    """
    from mira.gate.models import STATUS_CONTEXT as GATE_STATUS_CONTEXT

    return frozenset({STATUS_CONTEXT, GATE_STATUS_CONTEXT})


@dataclass(frozen=True)
class Evidence:
    """One concrete thing a check looked at.

    Every field is quoted to humans and none of it is ever interpreted: an
    evidence snippet comes from a diff, a log or a ticket, all of which are
    written by whoever opened the pull request. Nothing downstream parses one
    back into a decision.

    ``path`` and the line numbers are what make a result checkable — a
    violation that cannot point at a line is an opinion, and this framework
    does not record opinions.
    """

    path: str = ""
    start_line: int = 0
    end_line: int = 0
    snippet: str = ""
    detail: str = ""
    url: str = ""
    # Where the quoted text came from: "diff", "file", "pr", "ticket", "ci",
    # "tool:<name>", "llm". Free-form because adapters add their own.
    source: str = ""

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)

    @property
    def locator(self) -> str:
        """``path:line`` when there is one, for rendering and for dedup."""
        if not self.path:
            return ""
        if self.start_line:
            return f"{self.path}:{self.start_line}"
        return self.path


def _digest(parts: list[str]) -> str:
    return hashlib.sha256("\x1f".join(parts).encode("utf-8")).hexdigest()


@dataclass
class CheckFinding:
    """One distinct problem a check found, with the evidence for it.

    Split from the check itself because the check and the problem have
    different identities. A check is "did this pull request change a migration
    without a rollback"; a finding is "``db/0042_add_column.sql`` has no
    rollback, at line 3". One check can produce several findings, and — this is
    the part that makes the split necessary — *two* checks can produce the
    same one.

    :attr:`fingerprint` is that shared identity. When a deterministic analyser
    and a natural-language rule both report the same problem, the two findings
    are merged into this one record: it appears once, ``sources`` names both,
    and ``evidence`` carries what each of them quoted. Neither source's
    evidence is dropped, because the whole reason to run both is that they see
    different things about the same line.
    """

    fingerprint: str = ""
    title: str = ""
    detail: str = ""
    # Advisory only. Checks report; a mode decides whether a report blocks.
    severity: str = "warning"
    evidence: list[Evidence] = field(default_factory=list)
    # Every producer that reported this finding, in the order they were
    # merged. One entry normally; two after deduplication.
    sources: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "fingerprint": self.fingerprint,
            "title": self.title,
            "detail": self.detail,
            "severity": self.severity,
            "evidence": [item.as_dict() for item in self.evidence],
            "sources": list(self.sources),
        }

    @property
    def deduplicated(self) -> bool:
        """Whether more than one producer reported this exact problem."""
        return len(self.sources) > 1


def findings_from(data: Any) -> list[CheckFinding]:
    """Rehydrate a finding list from a persisted blob, dropping nonsense."""
    if not isinstance(data, list):
        return []
    out: list[CheckFinding] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        out.append(
            CheckFinding(
                fingerprint=str(item.get("fingerprint") or ""),
                title=str(item.get("title") or ""),
                detail=str(item.get("detail") or ""),
                severity=str(item.get("severity") or "warning"),
                evidence=evidence_from(item.get("evidence")),
                sources=[str(source) for source in (item.get("sources") or [])],
            )
        )
    return out


@dataclass
class CheckResult:
    """One check's answer about one pull request, at one commit.

    Carries its own identity (``check_id`` plus ``check_version`` plus
    ``config_digest``), so a result recorded last week is attributable to the
    check and the configuration that produced it rather than to whatever those
    mean today.
    """

    check_id: str = ""
    check_version: str = "1"
    title: str = ""
    origin: CheckOrigin = "native"
    mode: CheckMode = "off"
    state: CheckState = "skipped"
    summary: str = ""
    # What the check looked at, whatever it concluded. A pass cites this too:
    # "tests/test_engine.py was touched" is the evidence for a pass, and a
    # check that cannot say what it looked at cannot be audited.
    evidence: list[Evidence] = field(default_factory=list)
    # The distinct problems, each with its own evidence and its own identity.
    # Empty unless `state` is "violation".
    findings: list[CheckFinding] = field(default_factory=list)
    # Set only when `state` is "skipped". One of `SkipReason`'s constants.
    skip_reason: str = ""
    # Set when `state` is "infrastructure_error" or "timeout": the operator's
    # half of the message, never the pull request's.
    error: str = ""
    duration_seconds: float = 0.0
    # Content hash of the resolved configuration this check ran under, so two
    # results are only comparable when they were produced by the same rules.
    config_digest: str = ""
    # Every distinct producer that reported this. One entry normally; two after
    # deduplication, which is how "semgrep and the model both saw it" is
    # recorded without the finding appearing twice.
    sources: list[str] = field(default_factory=list)
    # Stable identity of the *finding*, not of the check: two producers that
    # found the same thing share it, and that is what dedup keys on.
    fingerprint: str = ""
    result_key: str = ""
    id: int = 0
    created_at: float = 0.0

    def __post_init__(self) -> None:
        if not self.sources:
            self.sources = [self.check_id] if self.check_id else []

    @property
    def is_violation(self) -> bool:
        """True only for a real statement about the pull request."""
        return self.state == "violation"

    @property
    def incomplete(self) -> bool:
        """Whether this check failed to reach a conclusion.

        A skip counts only when the reason was that something was missing. A
        check that correctly decided it does not apply has answered.
        """
        if self.state in INCONCLUSIVE_STATES:
            return True
        return self.state == "skipped" and self.skip_reason in UNANSWERED_SKIPS

    @property
    def blocking(self) -> bool:
        """Whether this result should stop a merge under its own mode."""
        if self.mode != "error":
            return False
        return self.is_violation or self.incomplete

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "result_key": self.result_key,
            "check_id": self.check_id,
            "check_version": self.check_version,
            "title": self.title,
            "origin": self.origin,
            "mode": self.mode,
            "state": self.state,
            "summary": self.summary,
            "evidence": [item.as_dict() for item in self.evidence],
            "findings": [finding.as_dict() for finding in self.findings],
            "skip_reason": self.skip_reason,
            "error": self.error,
            "duration_seconds": round(float(self.duration_seconds), 4),
            "config_digest": self.config_digest,
            "sources": list(self.sources),
            "fingerprint": self.fingerprint,
            "incomplete": self.incomplete,
            "blocking": self.blocking,
            "created_at": self.created_at,
        }


@dataclass
class CheckRunInputs:
    """The facts a run was made from, captured before any check ran.

    Persisted verbatim with the run. Hashed into the run key, so a pull request
    re-checked after a push records a new run rather than reusing the one made
    against the previous head — while a redelivered webhook over identical
    facts converges on the row that already exists.
    """

    platform: str = "github"
    owner: str = ""
    repo: str = ""
    pr_number: int = 0
    pr_url: str = ""
    pr_author: str = ""
    pr_title: str = ""
    base_branch: str = ""
    head_branch: str = ""
    head_sha: str = ""
    draft: bool = False
    changed_paths: list[str] = field(default_factory=list)
    changed_files: int = 0
    added_lines: int = 0
    deleted_lines: int = 0
    review_id: int = 0

    def as_dict(self) -> dict[str, Any]:
        data = asdict(self)
        # A run row is an audit record, not a second copy of the diff.
        data["changed_paths"] = sorted(data["changed_paths"])[:200]
        return data

    @property
    def digest(self) -> str:
        payload = self.as_dict()
        # A retried review gets a new id for the same facts, and that must not
        # read as a different world to check.
        payload.pop("review_id", None)
        return hashlib.sha256(
            json.dumps(payload, sort_keys=True, default=str).encode("utf-8")
        ).hexdigest()[:16]


@dataclass
class CheckRun:
    """Every check that ran for one pull request at one commit."""

    run_key: str = ""
    policy_version: str = ""
    inputs: CheckRunInputs = field(default_factory=CheckRunInputs)
    results: list[CheckResult] = field(default_factory=list)
    duration_seconds: float = 0.0
    # Set when the whole run could not happen: no store, no policy, kill
    # switch. Distinct from a run in which individual checks failed.
    error: str = ""
    id: int = 0
    created_at: float = 0.0
    updated_at: float = 0.0

    @property
    def violations(self) -> list[CheckResult]:
        return [result for result in self.results if result.is_violation]

    @property
    def incomplete_results(self) -> list[CheckResult]:
        return [result for result in self.results if result.incomplete]

    @property
    def blocking_results(self) -> list[CheckResult]:
        return [result for result in self.results if result.blocking]

    @property
    def findings(self) -> list[CheckFinding]:
        """Every distinct problem in this run, already deduplicated.

        Deduplication happened before persistence, so this is a flatten rather
        than a merge: a problem two producers found lives in exactly one
        result, naming both of them.
        """
        return [finding for result in self.results for finding in result.findings]

    @property
    def verdict(self) -> RunVerdict:
        """What this run means to something that gates on it.

        Violations are reported ahead of incompleteness on purpose: when a
        blocking check found a real problem *and* another one could not run,
        the actionable half is the real problem, and burying it under "checks
        were incomplete" would be the least useful of the two true statements.
        """
        if not self.results:
            return "not_run"
        blocking = self.blocking_results
        if any(result.is_violation for result in blocking):
            return "violation"
        if blocking:
            return "incomplete"
        return "pass"

    def counts(self) -> dict[str, int]:
        """One count per state, plus the two totals a summary line needs."""
        tally: dict[str, int] = dict.fromkeys(CHECK_STATES, 0)
        for result in self.results:
            tally[result.state] = tally.get(result.state, 0) + 1
        tally["total"] = len(self.results)
        tally["blocking"] = len(self.blocking_results)
        return tally

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "run_key": self.run_key,
            "policy_version": self.policy_version,
            "verdict": self.verdict,
            "inputs": self.inputs.as_dict(),
            "results": [result.as_dict() for result in self.results],
            "counts": self.counts(),
            "duration_seconds": round(float(self.duration_seconds), 4),
            "error": self.error,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "platform": self.inputs.platform,
            "owner": self.inputs.owner,
            "repo": self.inputs.repo,
            "pr_number": self.inputs.pr_number,
            "pr_url": self.inputs.pr_url,
            "pr_author": self.inputs.pr_author,
            "head_sha": self.inputs.head_sha,
        }


def run_key(
    *,
    platform: str,
    owner: str,
    repo: str,
    pr_number: int,
    head_sha: str,
    policy_version: str,
    inputs_digest: str = "",
) -> str:
    """Identity of one evaluation of one pull request, for idempotent writes.

    Excludes the trigger, exactly as the gate's decision key does: the same
    commit checked from a review, a redelivered webhook and a manual re-run,
    under the same policy and the same facts, is one run. It *includes* the
    policy version, because a run under different rules is a different run and
    must not inherit the earlier answer.
    """
    return _digest([platform, owner, repo, str(pr_number), head_sha, policy_version, inputs_digest])


def result_key(*, run_key_value: str, check_id: str, fingerprint: str = "") -> str:
    """Identity of one check's answer inside one run.

    Keyed on the check rather than on its output, so a retried run converges on
    the same row instead of accumulating one per attempt. ``fingerprint`` is
    only set for producers that report several findings under one check id.
    """
    return _digest([run_key_value, check_id, fingerprint])


def fingerprint(*, path: str, signature: str) -> str:
    """Identity of a *finding*, shared across whoever found it.

    Two deliberate omissions, and both of them are the difference between a
    dedup key that works and one that looks like it does.

    **No line number.** Two producers rarely agree to the line — a scanner
    points at the assignment, a model points at the function containing it —
    and any bucketing scheme fails at its own boundaries, so a key built on one
    would let the same problem be reported twice for having been reported one
    line apart. The consequence is that a rule violated three times in one file
    is one finding carrying three pieces of evidence, which is also how a
    reader would rather read it.

    **No check id.** The whole point is that a problem found by
    ``tool.semgrep`` and by a natural-language rule is one problem.

    ``signature`` is the normalised description — a rule id, or the words a
    model used. It is what keeps two *different* problems in one file apart.
    """
    return _digest([path.strip().lower(), signature.strip().lower()])[:32]


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


def evidence_from(data: Any) -> list[Evidence]:
    """Rehydrate an evidence list from a persisted blob, dropping nonsense."""
    if not isinstance(data, list):
        return []
    out: list[Evidence] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        out.append(
            Evidence(
                path=str(item.get("path") or ""),
                start_line=int(item.get("start_line") or 0),
                end_line=int(item.get("end_line") or 0),
                snippet=str(item.get("snippet") or ""),
                detail=str(item.get("detail") or ""),
                url=str(item.get("url") or ""),
                source=str(item.get("source") or ""),
            )
        )
    return out
