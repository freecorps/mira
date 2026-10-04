"""A provider that can read a repository and cannot write to it.

Backtests replay the review engine against real pull requests, and the one
thing they must never do is comment on them. ``dry_run=True`` on the engine is
a flag that every write path has to remember to check; this wrapper is the
structural version of the same promise. It is built *from an allowlist of
reads*: every other method on :class:`BaseProvider` — every write that exists
today, and every write anyone adds tomorrow — is replaced by a recorder that
notes the attempt and returns an inert value. The wrapped provider is never
asked to do anything that is not on the list.

Failing closed is the point. A new provider method nobody thought about here is
blocked, not passed through.
"""

from __future__ import annotations

import inspect
import logging
from dataclasses import dataclass, field
from typing import Any

from mira.providers.base import BaseProvider

logger = logging.getLogger(__name__)

# Methods that only read. Anything on BaseProvider that is not named here is
# blocked; anything a concrete provider adds that is not named here is too.
READ_METHODS: frozenset[str] = frozenset(
    {
        "get_pr_info",
        "get_pr_diff",
        "get_compare_diff",
        "get_review_states",
        "find_bot_comment",
        "get_unresolved_bot_threads",
        "get_all_bot_threads",
        "get_thread_id_for_comment",
        "get_human_review_comments",
        "get_file_content",
        "get_repo_tree",
        "get_repo_snapshot",
        "get_file_history",
        "get_comment_body",
        "get_discussion_root_body",
        "get_label_change_stats",
        "gate_capabilities",
        "get_ci_state",
        "get_pr_labels",
        "get_author_association",
        "get_pr_change_stats",
        "get_codeowners",
        "checks_capabilities",
        "get_issue",
        "get_ci_failures",
        "triage_capabilities",
        "get_path_authors",
        "autofix_capabilities",
        "get_actor_permission",
        "get_default_branch",
        "get_branch_head",
        "files_match",
        "find_open_pull_request",
        "pr_head_is_fork",
        "find_issue_comment",
        # History reads shared with digests/release notes and delivery analytics.
        "list_commits",
        "compare_commits",
        "get_commit_files",
        "get_commit_churn",
        "list_deployment_releases",
        "get_pr_first_commit_at",
        "get_pr_landed_at",
        # History reads used by backtests and escaped-bug tracking.
        "list_landed_pull_requests",
        "get_landed_pull_request",
        "get_commit",
        "get_commit_diff",
        "list_path_commits",
        "get_prs_for_commit",
        "get_blame",
        # Concrete-provider extras that only read.
        "list_open_prs",
        "get_pr_files",
        "get_review_inline_comments",
    }
)

# What a blocked call hands back, chosen so a caller that ignores the result
# keeps going exactly as it would after a successful no-op.
# Every write whose contract returns a value has one here, of the promised
# type, so a caller never trips over ``None`` before ``assert_no_writes`` runs.
_BLOCKED_RESULTS: dict[str, Any] = {
    "post_review": [],
    "commit_files": "",
    "create_pull_request": (0, ""),
    "submit_verdict": False,
    "publish_review_status": "",
    "publish_gate_status": "",
    "publish_checks_status": "",
    "resolve_outdated_review_threads": 0,
    "resolve_threads": 0,
}


class WriteBlocked(RuntimeError):
    """Raised by :meth:`ReadOnlyProvider.assert_no_writes` when a write was attempted."""


@dataclass
class BlockedCall:
    method: str
    args: tuple[Any, ...] = ()
    kwargs: dict[str, Any] = field(default_factory=dict)


def _public_base_methods() -> list[str]:
    names = []
    for name, value in inspect.getmembers(BaseProvider):
        if name.startswith("_") or not callable(value):
            continue
        names.append(name)
    return names


class ReadOnlyProvider(BaseProvider):
    """Delegates reads to ``inner``; records and swallows everything else."""

    def __init__(self, inner: BaseProvider) -> None:  # noqa: D107 - see class docstring
        self._inner = inner
        self.blocked_calls: list[BlockedCall] = []

    @property
    def inner(self) -> BaseProvider:
        return self._inner

    def assert_no_writes(self) -> None:
        if self.blocked_calls:
            names = ", ".join(sorted({c.method for c in self.blocked_calls}))
            raise WriteBlocked(f"Write attempted during a read-only run: {names}")

    def _record(self, name: str, args: tuple[Any, ...], kwargs: dict[str, Any]) -> Any:
        self.blocked_calls.append(BlockedCall(name, args, kwargs))
        logger.info("Read-only provider blocked %s()", name)
        result = _BLOCKED_RESULTS.get(name)
        # Fresh copies, so one caller mutating a returned list cannot leak into the next.
        return list(result) if isinstance(result, list) else result

    def __getattr__(self, name: str) -> Any:
        # Only reached for names not defined on the class — i.e. methods a
        # concrete provider adds beyond BaseProvider.
        if name.startswith("_"):
            raise AttributeError(name)
        # Raises AttributeError for a name the wrapped provider does not have,
        # so feature detection (``hasattr``/``getattr(..., None)``) stays honest.
        target = getattr(self._inner, name)
        if name in READ_METHODS:
            return target

        # Mirror the inner member's calling convention: a sync write must be
        # recorded when called, not handed back as a coroutine nobody awaits.
        if inspect.iscoroutinefunction(target):

            async def _blocked_async(*args: Any, **kwargs: Any) -> Any:
                return self._record(name, args, kwargs)

            return _blocked_async

        def _blocked(*args: Any, **kwargs: Any) -> Any:
            return self._record(name, args, kwargs)

        return _blocked


def _make_delegate(name: str, is_async: bool) -> Any:
    if is_async:

        async def _delegate(self: ReadOnlyProvider, *args: Any, **kwargs: Any) -> Any:
            return await getattr(self._inner, name)(*args, **kwargs)

    else:

        def _delegate(self: ReadOnlyProvider, *args: Any, **kwargs: Any) -> Any:  # type: ignore[misc]
            return getattr(self._inner, name)(*args, **kwargs)

    _delegate.__name__ = name
    return _delegate


def _make_blocked(name: str, is_async: bool) -> Any:
    if is_async:

        async def _blocked(self: ReadOnlyProvider, *args: Any, **kwargs: Any) -> Any:
            return self._record(name, args, kwargs)

    else:

        def _blocked(self: ReadOnlyProvider, *args: Any, **kwargs: Any) -> Any:  # type: ignore[misc]
            return self._record(name, args, kwargs)

    _blocked.__name__ = name
    return _blocked


for _name in _public_base_methods():
    _is_async = inspect.iscoroutinefunction(getattr(BaseProvider, _name))
    if _name in READ_METHODS:
        setattr(ReadOnlyProvider, _name, _make_delegate(_name, _is_async))
    else:
        setattr(ReadOnlyProvider, _name, _make_blocked(_name, _is_async))

# Every abstract method now has a concrete stand-in assigned above.
ReadOnlyProvider.__abstractmethods__ = frozenset()


def blocked_base_methods() -> list[str]:
    """The BaseProvider methods this wrapper refuses to pass through."""
    return sorted(name for name in _public_base_methods() if name not in READ_METHODS)
