"""Abstract provider interface for code hosting platforms."""

from __future__ import annotations

import abc

from mira.autofix.capabilities import (
    NO_CAPABILITIES as NO_AUTOFIX_CAPABILITIES,
)
from mira.autofix.capabilities import (
    AutofixCapabilities,
)
from mira.checks.capabilities import (
    NO_CAPABILITIES as NO_CHECK_CAPABILITIES,
)
from mira.checks.capabilities import (
    CheckCapabilities,
)
from mira.gate.capabilities import NO_CAPABILITIES, GateCapabilities
from mira.gate.models import CIState
from mira.models import (
    BotThreadRecord,
    CIJobFailure,
    FileChangeStat,
    FileHistoryEntry,
    HumanReviewComment,
    IssueInfo,
    PathAuthorship,
    PRInfo,
    ReviewResult,
    UnresolvedThread,
)
from mira.triage.capabilities import (
    NO_CAPABILITIES as NO_TRIAGE_CAPABILITIES,
)
from mira.triage.capabilities import (
    TriageCapabilities,
)


class BaseProvider(abc.ABC):
    """Abstract base class for code hosting providers."""

    @abc.abstractmethod
    def __init__(self, token: str) -> None:
        """Configure the API client with an auth token."""

    @abc.abstractmethod
    async def get_pr_info(self, pr_url: str) -> PRInfo:
        """Fetch metadata about a pull request."""

    @abc.abstractmethod
    async def get_pr_diff(self, pr_info: PRInfo) -> str:
        """Fetch the raw diff for a pull request."""

    @abc.abstractmethod
    async def post_review(
        self,
        pr_info: PRInfo,
        result: ReviewResult,
        bot_name: str = "miracodeai",
    ) -> list[int]:
        """Post review comments to a pull request.

        Returns platform comment IDs aligned to ``result.comments`` (0 where
        the ID couldn't be determined). Used to link later replies/reactions
        back to the exact persisted finding.
        """

    async def submit_verdict(self, pr_info: PRInfo, event: str, body: str) -> bool:
        """Submit a standalone review carrying an APPROVE / REQUEST_CHANGES verdict.

        Kept separate from ``post_review`` so the verdict lands the same way
        whether the inline comments went out as a batch review or through the
        per-comment 422 fallback — and so a PR with zero findings can still be
        approved.

        Concrete (not abstract) with a no-op default: providers without review
        events keep working, they just never emit a verdict. Returns True when
        the platform actually recorded it.
        """
        return False

    async def get_review_states(self, pr_info: PRInfo) -> dict[str, str]:
        """Latest review state per reviewer login (e.g. ``{"alice": "CHANGES_REQUESTED"}``).

        Used to keep Mira from approving over a human who asked for changes.
        Empty dict when the provider can't report it — callers treat that as
        "no information", not "nobody objected".
        """
        return {}

    @abc.abstractmethod
    async def post_comment(self, pr_info: PRInfo, body: str) -> None:
        """Post a top-level comment on a pull request."""

    @abc.abstractmethod
    async def find_bot_comment(self, pr_info: PRInfo, marker: str) -> int | None:
        """Find an existing comment containing the marker. Returns comment ID or None."""

    @abc.abstractmethod
    async def update_comment(self, pr_info: PRInfo, comment_id: int, body: str) -> None:
        """Edit an existing comment by its ID."""

    @abc.abstractmethod
    async def resolve_outdated_review_threads(self, pr_info: PRInfo) -> int:
        """Resolve all unresolved review threads authored by this bot. Returns count resolved."""

    async def get_unresolved_bot_threads(
        self, pr_info: PRInfo, bot_login: str | None = None
    ) -> list[UnresolvedThread]:
        """Fetch all unresolved review threads authored by the bot."""
        return []

    async def resolve_threads(self, pr_info: PRInfo, thread_ids: list[str]) -> int:
        """Resolve review threads by ID. Returns count of successfully resolved."""
        return 0

    async def get_thread_id_for_comment(
        self,
        comment_node_id: str,
        pr_info: PRInfo,
    ) -> str | None:
        """Look up the review thread for a comment. Returns thread ID or None."""
        return None

    async def add_label(self, pr_info: PRInfo, label: str) -> None:
        """Add a label to a pull request."""
        return

    async def remove_label(self, pr_info: PRInfo, label: str) -> None:
        """Remove a label from a pull request."""
        return

    async def get_file_content(self, pr_info: PRInfo, path: str, ref: str) -> str:
        """Fetch file content at a specific ref."""
        return ""

    # The methods below are called by the engine and merge handler. They have
    # safe defaults so a provider can ship without them and simply degrade
    # (no incremental re-review, no JIT context, no merge-time learning)
    # rather than raising AttributeError.

    async def get_compare_diff(self, pr_info: PRInfo, base_sha: str, head_sha: str) -> str:
        """Diff between two commits, for incremental (round 2+) reviews."""
        return ""

    async def get_all_bot_threads(
        self, pr_info: PRInfo, bot_login: str | None = None
    ) -> list[BotThreadRecord]:
        """All bot-authored review threads (resolved and unresolved)."""
        return []

    async def get_human_review_comments(
        self, pr_info: PRInfo, bot_login: str
    ) -> list[HumanReviewComment]:
        """Non-bot line-level review comments, for merge-time learning."""
        return []

    async def get_repo_tree(self, pr_info: PRInfo, ref: str) -> list[str]:
        """Every file path in the repo at a ref, for JIT cross-file context."""
        return []

    async def get_file_history(
        self, pr_info: PRInfo, paths: list[str], max_per_file: int = 5
    ) -> dict[str, list[FileHistoryEntry]]:
        """Recent commit history per path, for decision archaeology."""
        return {}

    async def reply_to_review_comment(self, pr_info: PRInfo, comment_id: int, body: str) -> None:
        """Reply to an existing line comment, threading it."""
        return

    async def get_comment_body(self, pr_info: PRInfo, comment_id: int) -> str:
        """Fetch a single comment/note body by id (best-effort, "" on failure)."""
        return ""

    async def get_discussion_root_body(self, pr_info: PRInfo, discussion_id: str) -> str:
        """The first comment of a thread/discussion (best-effort, "" on failure)."""
        return ""

    # ── Merge gate (Phase 4) ──
    #
    # Every default below reports *ignorance*, never good news. A provider that
    # does not implement `get_ci_state` returns "unknown", which the gate reads
    # as "not green"; one that cannot report an author association returns
    # "unknown", which is never sufficient for an approval. Adding a provider
    # therefore degrades the gate rather than weakening it.

    def gate_capabilities(self) -> GateCapabilities:
        """What this provider can do for the merge gate.

        Declared, not probed: a probe costs a round trip per pull request on a
        device that has none to spare, and a probe that fails transiently would
        silently downgrade a working install.
        """
        return NO_CAPABILITIES

    async def get_ci_state(self, pr_info: PRInfo) -> CIState:
        """Combined CI outcome for the PR head commit.

        "unknown" is the honest default and the safe one — the gate treats
        anything that is not `success` as a reason not to approve.
        """
        return CIState()

    async def get_pr_labels(self, pr_info: PRInfo) -> list[str]:
        """Labels currently on the PR. Empty when the provider cannot say."""
        return []

    async def get_author_association(self, pr_info: PRInfo) -> str:
        """The author's relationship to the repository.

        Uppercase, GitHub's vocabulary (`OWNER`, `MEMBER`, `COLLABORATOR`,
        `CONTRIBUTOR`, `FIRST_TIME_CONTRIBUTOR`, `NONE`), which the other
        providers map onto. `"UNKNOWN"` when it could not be determined.
        """
        return "UNKNOWN"

    async def get_pr_change_stats(self, pr_info: PRInfo) -> list[FileChangeStat]:
        """Every file the PR touches, with its own line counts.

        *Every* file — including deletions, binaries, and anything the review
        filters out. Whether Mira reviewed a file and whether that file is
        protected are different questions, and answering the second from the
        first is how a deleted CI workflow gets approved.

        Derived from the diff the provider already knows how to fetch, so a new
        provider gets it for free.
        """
        from mira.core.diff_parser import parse_diff

        # Deliberately not caught: zero changed files would read as a trivially
        # small pull request and clear every size limit there is.
        diff_text = await self.get_pr_diff(pr_info)
        patch_set = parse_diff(diff_text or "")
        return [
            FileChangeStat(
                path=file.path,
                added_lines=file.added_lines,
                deleted_lines=file.deleted_lines,
            )
            for file in patch_set.files
        ]

    async def get_codeowners(self, pr_info: PRInfo, ref: str = "") -> tuple[str, str]:
        """``(path, contents)`` of the repository CODEOWNERS, at ``ref``.

        ``("", "")`` when the repository has none. A provider that *cannot look*
        must raise rather than return this — the gate distinguishes "no owners
        are declared" from "we could not find out", and only the first is safe.

        ``ref`` defaults to the head, which is what the merge gate wants:
        ownership declared on the branch can only *add* owners, and an added
        owner only ever stops an automatic approval. Reviewer suggestion passes
        the base explicitly, because there the direction reverses — a branch
        that could add itself an owner would be choosing who reviews it.
        """
        return "", ""

    async def publish_gate_status(
        self,
        pr_info: PRInfo,
        *,
        context: str,
        conclusion: str,
        title: str,
        summary: str,
        target_url: str = "",
    ) -> str:
        """Publish the gate decision as a check run / commit status.

        Idempotent by ``context``: re-publishing replaces the previous entry
        rather than adding one, so a retried webhook cannot litter the PR.
        Returns a provider reference for the audit trail, or "" if unsupported.
        """
        return ""

    # ── Pre-merge checks (Phase 6) ──
    #
    # Read-only, all of it. Every default below reports *ignorance* and none of
    # them reports good news: a provider that does not implement `get_issue`
    # makes the ticket check skip with the reason, never pass; one that cannot
    # list failing jobs makes the CI check say so rather than infer a green
    # build. Adding a provider therefore degrades checks rather than weakening
    # them.

    def checks_capabilities(self) -> CheckCapabilities:
        """What this provider can do for pre-merge checks.

        Declared, not probed, for the same reasons as the gate's table.
        """
        return NO_CHECK_CAPABILITIES

    async def get_issue(
        self, pr_info: PRInfo, number: int, *, owner: str = "", repo: str = ""
    ) -> IssueInfo | None:
        """The issue ``number`` in ``owner/repo``, defaulting to the PR's own repo.

        ``None`` means the platform answered and there is no such issue — a
        fact about the pull request's reference. Anything that stops the
        provider from *asking* must raise, because "we could not find out" and
        "it does not exist" lead to opposite conclusions, and a check that
        cannot tell them apart would report an API outage as a bad reference.
        """
        raise NotImplementedError(f"{type(self).__name__} cannot read issues")

    async def get_ci_failures(
        self, pr_info: PRInfo, *, max_jobs: int = 3, max_log_bytes: int = 16_000
    ) -> list[CIJobFailure]:
        """The failing CI jobs on the head commit, with an excerpt of each.

        Bounded by the caller, because the caller is about to put this text in
        front of a model and in a database. A provider that can name a failing
        job but not read its output returns the job with
        ``log_unavailable=True`` rather than an empty excerpt that would read
        as "the job printed nothing".
        """
        return []

    # ── Phase 7C: triage and reviewer suggestion ──
    #
    # Two additions, both read-only. There is deliberately no method here that
    # requests a review, adds an assignee or applies a reviewer label: this
    # phase suggests, and suggestion and assignment are different acts.

    def triage_capabilities(self) -> TriageCapabilities:
        """What this provider can tell reviewer triage. Declared, not probed."""
        return NO_TRIAGE_CAPABILITIES

    async def get_path_authors(
        self,
        pr_info: PRInfo,
        paths: list[str],
        *,
        ref: str = "",
        max_per_path: int = 20,
    ) -> dict[str, list[PathAuthorship]]:
        """Recent commits per path, attributed to platform accounts.

        Distinct from :meth:`get_file_history`, which returns the commit's own
        author name — a string chosen by whoever made the commit, and therefore
        not something to rank a person on. An entry the platform could not
        resolve to an account carries an empty ``login`` and is dropped by the
        caller rather than falling back to the commit's fields.

        ``ref`` is the pull request's base. A provider must not substitute the
        head: commits on the proposed branch are written by the person
        proposing the change.
        """
        return {}

    async def publish_checks_status(
        self,
        pr_info: PRInfo,
        *,
        context: str,
        conclusion: str,
        title: str,
        summary: str,
        target_url: str = "",
    ) -> str:
        """Publish the check run's verdict as a check run / commit status.

        Idempotent by ``context``: re-publishing replaces the previous entry
        rather than adding one. Returns a provider reference for the audit
        trail, or "" if unsupported. Defaults to the gate's implementation on
        providers that have one, because the two publish the same artifact type
        under different names.
        """
        return await self.publish_gate_status(
            pr_info,
            context=context,
            conclusion=conclusion,
            title=title,
            summary=summary,
            target_url=target_url,
        )

    # ── Assisted correction (Phase 5) ──
    #
    # The only write surface in the provider interface. Every default below
    # either reports ignorance or does nothing, so a provider that does not
    # implement these makes autofix *refuse*, never makes it improvise: the
    # capability table says what is missing and the job records why it stopped.
    #
    # There is deliberately no `merge`, no `force` parameter and no
    # `delete_branch`. Mira opens changes and humans dispose of them.

    def autofix_capabilities(self) -> AutofixCapabilities:
        """What this provider can do for assisted correction.

        Declared, not probed, for the same reasons as the gate's table: a probe
        costs a round trip per request and a transient failure would silently
        downgrade a working install.
        """
        return NO_AUTOFIX_CAPABILITIES

    async def get_actor_permission(self, pr_info: PRInfo, login: str) -> str:
        """What ``login`` may do in this repository.

        Lowercase, GitHub's vocabulary (``admin``, ``maintain``, ``write``,
        ``triage``, ``read``, ``none``), which the other providers map onto.
        ``"unknown"`` when it could not be determined — and unknown is never
        treated as permission.
        """
        return "unknown"

    async def get_default_branch(self, pr_info: PRInfo) -> str:
        """The repository's default branch. ``""`` when it cannot be read.

        A provider that returns ``""`` makes autofix refuse to write at all:
        "never touch the default branch" cannot be enforced against a name
        nobody knows.
        """
        return ""

    async def get_branch_head(self, pr_info: PRInfo, branch: str) -> str:
        """Commit sha at the tip of ``branch``, or ``""`` if it does not exist."""
        return ""

    async def create_branch(self, pr_info: PRInfo, branch: str, from_sha: str) -> None:
        """Create ``branch`` pointing at ``from_sha``.

        Creation only. There is no update path and no force: a branch that
        already exists is adopted by the caller, never reset.
        """
        raise NotImplementedError(f"{type(self).__name__} cannot create branches")

    async def files_match(self, pr_info: PRInfo, branch: str, files: dict[str, str]) -> bool:
        """Whether ``branch`` already carries exactly this content.

        What makes a retried publish idempotent: a worker that committed and
        then died before recording the sha comes back, sees its own work, and
        does not commit it twice. False on any doubt — committing the same
        content again is untidy, and skipping a commit that never happened is
        a fix that silently did nothing.
        """
        return False

    async def commit_files(
        self, pr_info: PRInfo, branch: str, files: dict[str, str], message: str
    ) -> str:
        """Commit ``files`` (path → content) onto ``branch``. Returns the sha.

        Fast-forward only, from the branch's current head. No force, no
        history rewriting, no amend.
        """
        raise NotImplementedError(f"{type(self).__name__} cannot create commits")

    async def create_pull_request(
        self, pr_info: PRInfo, *, head: str, base: str, title: str, body: str
    ) -> tuple[int, str]:
        """Open a pull request from ``head`` into ``base``. Returns ``(number, url)``."""
        raise NotImplementedError(f"{type(self).__name__} cannot open pull requests")

    async def find_open_pull_request(self, pr_info: PRInfo, head: str) -> tuple[int, str] | None:
        """An already-open pull request from ``head``, or None."""
        return None

    async def pr_head_is_fork(self, pr_info: PRInfo) -> bool:
        """Whether this pull request's head branch lives in another repository.

        True is the safe answer and the default an implementation should
        degrade to: committing onto a fork's branch is a cross-repository write
        that nobody authorized.
        """
        return True
