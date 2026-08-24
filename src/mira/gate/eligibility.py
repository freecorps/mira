"""The eligibility matrix: is this PR one the gate may hold an opinion about,
and if so, is there anything disqualifying about it?

Every check returns a :class:`~mira.gate.models.Reason` with a ``kind``:

  ``skip``  — out of scope. The gate records ``skipped`` and says nothing.
  ``block`` — in scope and disqualified. The gate records ``not_approved``.

The distinction matters for measurement, not for safety: both are "no
approval". Keeping them apart is what lets a shadow rollout tell "the gate
never applies here" from "the gate applies and keeps saying no", which is the
difference between a misconfigured scope and a policy that is too strict.

Checks run in a fixed order and *all* of them run — the matrix collects every
reason rather than stopping at the first, because a decision that lists one of
four problems sends someone round the loop three more times.

Labels and branches are consulted here because an operator configured them as
policy inputs. Nothing in a pull request is ever read as an instruction: a
label can take a PR out of scope or disqualify it, and that is all it can do.
No label, title, body, comment or CI log can make the gate approve.
"""

from __future__ import annotations

from mira.gate.models import GateInputs, Reason, ReasonCode
from mira.gate.paths import match_any, select
from mira.gate.policy import EffectivePolicy
from mira.models import Severity

# Author associations that are never sufficient on their own, whatever the
# allowlist says. `NONE` and the first-timer values describe someone with no
# standing in the repository; `UNKNOWN` means the platform would not tell us.
_NEVER_SUFFICIENT = frozenset({"", "UNKNOWN", "NONE", "FIRST_TIME_CONTRIBUTOR", "FIRST_TIMER"})


def _lower(values: tuple[str, ...] | list[str]) -> set[str]:
    return {value.strip().lower() for value in values if value and value.strip()}


def scope_reasons(inputs: GateInputs, policy: EffectivePolicy) -> list[Reason]:
    """Reasons the gate has no business deciding this PR at all."""
    reasons: list[Reason] = []

    if not policy.enabled:
        reasons.append(
            Reason(
                ReasonCode.REPO_DISABLED, "The merge gate is disabled for this repository", "skip"
            )
        )
    if policy.mode == "off":
        reasons.append(Reason(ReasonCode.GATE_OFF, "The merge gate is off", "skip"))
    if reasons:
        # Nothing below this line can matter, and evaluating it would put PR
        # data into a decision row for a repository that opted out.
        return reasons

    if policy.skip_draft_prs and inputs.draft:
        reasons.append(Reason(ReasonCode.PR_DRAFT, "The pull request is a draft", "skip"))

    author = (inputs.pr_author or "").lower()
    bot = (inputs.bot_login or "").lower()
    if author and bot and author in {bot, f"{bot}[bot]"}:
        reasons.append(
            Reason(ReasonCode.SELF_AUTHORED, "Mira opened this pull request itself", "skip")
        )

    allowed_branches = _lower(policy.allowed_base_branches)
    base = (inputs.base_branch or "").lower()
    if allowed_branches and base not in allowed_branches:
        reasons.append(
            Reason(
                ReasonCode.BASE_BRANCH_OUT_OF_SCOPE,
                f"Base branch {inputs.base_branch!r} is not in the gate's scope",
                "skip",
            )
        )

    allowed_authors = _lower(policy.allowed_authors)
    if allowed_authors and author not in allowed_authors:
        reasons.append(
            Reason(
                ReasonCode.AUTHOR_NOT_IN_ALLOWLIST,
                "The pull request author is not in the gate's author allowlist",
                "skip",
            )
        )

    labels = _lower(inputs.labels)
    missing = sorted(_lower(policy.required_labels) - labels)
    if missing:
        reasons.append(
            Reason(
                ReasonCode.MISSING_REQUIRED_LABEL,
                f"Required label(s) not present: {', '.join(missing)}",
                "skip",
            )
        )

    if inputs.changed_paths and len(inputs.generated_paths) == len(inputs.changed_paths):
        reasons.append(
            Reason(
                ReasonCode.GENERATED_ONLY_DIFF,
                "Every changed file is generated output, which Mira does not review",
                "skip",
            )
        )

    approving_humans = sorted(
        login for login, state in (inputs.human_states or {}).items() if state == "APPROVED"
    )
    if approving_humans:
        reasons.append(
            Reason(
                ReasonCode.HUMAN_ALREADY_APPROVED,
                f"Already approved by {', '.join(approving_humans[:5])}",
                "skip",
            )
        )

    return reasons


def blocking_reasons(inputs: GateInputs, policy: EffectivePolicy) -> list[Reason]:
    """Reasons the gate is in scope and says no.

    Ordered from "a person decided this" through "the platform disagrees" down
    to "we could not see enough", because that is the order a reader wants: the
    deliberate reasons first, the mechanical ones after.
    """
    reasons: list[Reason] = []

    labels = _lower(inputs.labels)
    hit_labels = sorted(labels & _lower(policy.blocked_labels))
    if hit_labels:
        reasons.append(
            Reason(
                ReasonCode.BLOCKED_LABEL,
                f"Blocking label present: {', '.join(hit_labels)}",
            )
        )

    base = (inputs.base_branch or "").lower()
    if base and base in _lower(policy.blocked_base_branches):
        reasons.append(
            Reason(
                ReasonCode.BLOCKED_BASE_BRANCH,
                f"Base branch {inputs.base_branch!r} never receives an automatic approval",
            )
        )

    author = (inputs.pr_author or "").lower()
    if author and author in _lower(policy.blocked_authors):
        reasons.append(
            Reason(ReasonCode.AUTHOR_BLOCKED, "The pull request author is on the gate's blocklist")
        )

    association = (inputs.author_association or "").upper()
    allowed_associations = {value.upper() for value in policy.allowed_author_associations}
    if association in {"", "UNKNOWN"}:
        reasons.append(
            Reason(
                ReasonCode.AUTHOR_ASSOCIATION_UNKNOWN,
                "The platform did not report the author's association with the repository",
            )
        )
    elif allowed_associations and (
        association not in allowed_associations or association in _NEVER_SUFFICIENT
    ):
        reasons.append(
            Reason(
                ReasonCode.AUTHOR_ASSOCIATION_INSUFFICIENT,
                f"Author association {association} is not permitted to receive an "
                "automatic approval",
            )
        )

    counted_files = inputs.changed_files
    counted_lines = inputs.added_lines + inputs.deleted_lines
    if policy.size_excludes_generated:
        counted_files = max(0, counted_files - len(inputs.generated_paths))
        # Discounting the files but not their lines would still trip the line
        # limit on exactly the diffs this setting exists to forgive.
        counted_lines = max(0, counted_lines - inputs.generated_lines)
    if counted_files > policy.max_changed_files:
        reasons.append(
            Reason(
                ReasonCode.PR_TOO_MANY_FILES,
                f"{counted_files} reviewable files changed, above the limit of "
                f"{policy.max_changed_files}",
            )
        )
    if counted_lines > policy.max_changed_lines:
        reasons.append(
            Reason(
                ReasonCode.PR_TOO_MANY_LINES,
                f"{counted_lines} lines changed, above the limit of {policy.max_changed_lines}",
            )
        )

    if inputs.protected_matches:
        shown = sorted(inputs.protected_matches)[:5]
        more = len(inputs.protected_matches) - len(shown)
        suffix = f" (+{more} more)" if more > 0 else ""
        reasons.append(
            Reason(
                ReasonCode.PROTECTED_PATH,
                f"Protected path(s) touched: {', '.join(shown)}{suffix}",
            )
        )

    if policy.codeowners == "block":
        if inputs.codeowners_status == "unreadable":
            reasons.append(
                Reason(
                    ReasonCode.CODEOWNERS_UNREADABLE,
                    "CODEOWNERS could not be parsed, so ownership could not be established",
                )
            )
        elif inputs.codeowner_matches:
            shown = sorted(inputs.codeowner_matches)[:5]
            reasons.append(
                Reason(
                    ReasonCode.CODEOWNERS_PATH,
                    f"CODEOWNERS assigns an owner to: {', '.join(shown)}",
                )
            )

    if policy.require_ci_success:
        state = inputs.ci.state
        if state == "failure":
            reasons.append(
                Reason(
                    ReasonCode.CI_FAILING,
                    "CI is failing: " + (", ".join(sorted(inputs.ci.failing)[:5]) or "see checks"),
                )
            )
        elif state == "pending":
            reasons.append(
                Reason(
                    ReasonCode.CI_PENDING,
                    "CI has not finished: "
                    + (", ".join(sorted(inputs.ci.pending)[:5]) or "see checks"),
                )
            )
        elif state != "success":
            reasons.append(
                Reason(
                    ReasonCode.CI_UNKNOWN,
                    "CI status could not be read, so it is treated as not green",
                )
            )

    if inputs.review_failed:
        reasons.append(
            Reason(ReasonCode.REVIEW_FAILED, f"The review did not complete: {inputs.review_failed}")
        )
    if policy.require_all_files_reviewed and not inputs.review_complete:
        reasons.append(
            Reason(
                ReasonCode.REVIEW_INCOMPLETE,
                f"{len(inputs.review_skipped_paths)} file(s) in this PR were never reviewed",
            )
        )
    if policy.require_index_ready and not inputs.index_ready:
        reasons.append(
            Reason(
                ReasonCode.INDEX_NOT_READY,
                "The repository index is not ready, so cross-file context was incomplete",
            )
        )

    if inputs.open_blockers:
        reasons.append(
            Reason(
                ReasonCode.OPEN_BLOCKER,
                f"{inputs.open_blockers} blocker finding(s) are still open",
            )
        )

    ceiling = Severity.from_str(policy.approve_max_severity)
    worst = Severity.from_str(inputs.worst_severity) if inputs.worst_severity else None
    if worst is not None and worst > ceiling:
        reasons.append(
            Reason(
                ReasonCode.SEVERITY_ABOVE_CEILING,
                f"Worst open finding is {worst.name.lower()}, above the "
                f"{ceiling.name.lower()} ceiling",
            )
        )

    blocking_humans = sorted(
        login
        for login, state in (inputs.human_states or {}).items()
        if state == "CHANGES_REQUESTED" and not login.endswith("[bot]")
    )
    if blocking_humans:
        reasons.append(
            Reason(
                ReasonCode.HUMAN_CHANGES_REQUESTED,
                f"Changes requested by {', '.join(blocking_humans[:5])}",
            )
        )

    return reasons


def classify_paths(paths: list[str], policy: EffectivePolicy) -> tuple[list[str], list[str]]:
    """``(generated, protected)`` slices of a changed-path list."""
    generated = select(paths, list(policy.generated_paths))
    protected = select(paths, list(policy.protected_paths))
    return generated, protected


def protecting_pattern(path: str, policy: EffectivePolicy) -> str:
    """Which configured pattern protects ``path``, for explaining one file."""
    return match_any(path, list(policy.protected_paths))
