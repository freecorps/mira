"""Abstract provider interface for code hosting platforms."""

from __future__ import annotations

import abc

from mira.models import (
    BotThreadRecord,
    FileHistoryEntry,
    HumanReviewComment,
    PRInfo,
    ReviewResult,
    UnresolvedThread,
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

        Returns the GitHub comment IDs of the posted inline comments, aligned
        to ``result.comments`` (0 where the ID couldn't be determined). Used to
        link human replies back to the exact comment they answer.
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
