"""What a check is allowed to see, and what it is allowed to return.

A check gets a :class:`CheckContext` and returns a :class:`CheckOutcome`. That
is the whole interface, and its narrowness is deliberate:

* **A check cannot reach the store, the platform's write surface, or the
  policy of another check.** It gets facts and a small number of read-only
  awaitables. Everything that writes anything lives in the runner.
* **A check cannot decide whether it blocks.** It returns a state and its
  evidence; the mode that turns a violation into a blocked merge comes from
  configuration the check never sees. That is what keeps "this check found
  something" and "this repository treats that as fatal" separate questions.
* **A check cannot report a violation without evidence.** The runner enforces
  it rather than trusting each check to remember, because a violation nobody
  can look up is indistinguishable from a guess.

The context is built once per run and shared by every check, so the diff is
parsed once and a file body fetched by one check is not fetched again by the
next. It is read-only from a check's point of view: the cache is the only thing
that mutates, and two checks racing to fill the same cache entry both get the
same content.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from mira.checks.models import CheckFinding, CheckState, Evidence, SkipReason
from mira.checks.policy import EffectiveChecksPolicy
from mira.models import FileChangeStat, PatchSet

logger = logging.getLogger(__name__)

# Bytes of one file body a check may pull. A check reads files to confirm
# something the diff hinted at; one that needs the whole repository is a
# review, and Mira already has one of those.
MAX_FILE_BYTES = 200_000


@dataclass
class CheckOutcome:
    """What a check returns. The runner supplies everything else.

    A check never sets its own mode, duration, keys or config digest: those are
    facts about the *run*, and letting a check report them would let a check
    misreport them.
    """

    state: CheckState = "skipped"
    summary: str = ""
    evidence: list[Evidence] = field(default_factory=list)
    findings: list[CheckFinding] = field(default_factory=list)
    skip_reason: str = ""
    error: str = ""

    @classmethod
    def passed(cls, summary: str, evidence: list[Evidence] | None = None) -> CheckOutcome:
        return cls(state="pass", summary=summary, evidence=list(evidence or []))

    @classmethod
    def violation(
        cls,
        summary: str,
        findings: list[CheckFinding],
        evidence: list[Evidence] | None = None,
    ) -> CheckOutcome:
        return cls(
            state="violation",
            summary=summary,
            findings=list(findings),
            evidence=list(evidence or []),
        )

    @classmethod
    def skipped(cls, summary: str, reason: str = SkipReason.NOT_APPLICABLE) -> CheckOutcome:
        return cls(state="skipped", summary=summary, skip_reason=reason)

    @classmethod
    def failed(cls, error: str, summary: str = "") -> CheckOutcome:
        """Infrastructure error: the check could not answer.

        Named ``failed`` rather than ``error`` because the thing that failed is
        Mira. The summary shown to a human says so in those words — a reader
        must never have to work out from a result whether the problem is theirs.
        """
        return cls(
            state="infrastructure_error",
            summary=summary
            or (
                "This check could not run, so it says nothing about this change. "
                "This is a Mira problem, not a problem with the change."
            ),
            error=error,
        )


# What every check is: a coroutine from context to outcome.
CheckRunner = Callable[["CheckContext"], Awaitable[CheckOutcome]]


@dataclass
class CheckContext:
    """Everything the checks in one run share.

    Built once, before any check starts, so the diff is parsed once and the
    facts every check reasons about are identical — which is most of what makes
    a run reproducible.
    """

    policy: EffectiveChecksPolicy
    platform: str = "github"
    owner: str = ""
    repo: str = ""
    pr_number: int = 0
    pr_url: str = ""
    pr_author: str = ""
    pr_title: str = ""
    pr_body: str = ""
    base_branch: str = ""
    head_branch: str = ""
    head_sha: str = ""
    draft: bool = False
    labels: list[str] = field(default_factory=list)
    changes: list[FileChangeStat] = field(default_factory=list)
    patch_set: PatchSet = field(default_factory=PatchSet)
    diff_text: str = ""

    # The live provider, when there is one. Checks use it only through the
    # read-only helpers below; nothing in this package calls a write method.
    provider: Any = None
    pr_info: Any = None
    # Factory returning an LLM client, or None when the deployment has none.
    # A factory rather than a client so a run that uses no language rule never
    # constructs one.
    llm_factory: Callable[[], Any] | None = None

    # Monotonic deadline for the whole run. A check that wants to bound its own
    # work consults `remaining`; the runner enforces it regardless.
    deadline: float = 0.0

    # path -> the one in-flight or finished fetch for it.
    _file_cache: dict[str, asyncio.Future[str]] = field(default_factory=dict, repr=False)
    _file_lock: asyncio.Lock | None = field(default=None, repr=False)
    _shared: dict[str, Any] = field(default_factory=dict, repr=False)
    _shared_lock: asyncio.Lock | None = field(default=None, repr=False)

    async def shared(self, key: str, factory: Callable[[], Awaitable[Any]]) -> Any:
        """Compute ``key`` once for the whole run, however many checks ask.

        Two checks legitimately need the same expensive answer — the ticket
        check and the acceptance-criteria check both need the resolved issue —
        and a run that fetched it twice would double the API calls and, worse,
        could get two different answers and report contradictory results about
        the same pull request.

        A failure is cached too, as the exception it was. Retrying a failed
        lookup once per interested check would multiply an outage by the number
        of checks that care about it, and the second attempt would produce a
        second, differently-worded infrastructure error for one cause.
        """
        if key in self._shared:
            value = self._shared[key]
            if isinstance(value, BaseException):
                raise value
            return value
        if self._shared_lock is None:
            self._shared_lock = asyncio.Lock()
        async with self._shared_lock:
            if key in self._shared:
                value = self._shared[key]
                if isinstance(value, BaseException):
                    raise value
                return value
            try:
                value = await factory()
            except Exception as exc:  # noqa: BLE001 - cached and re-raised as itself
                self._shared[key] = exc
                raise
            self._shared[key] = value
            return value

    @property
    def changed_paths(self) -> list[str]:
        return [change.path for change in self.changes]

    @property
    def remaining(self) -> float:
        """Seconds left in the run's budget; a large number when unbounded."""
        if not self.deadline:
            return float("inf")
        return max(0.0, self.deadline - time.monotonic())

    async def file_content(self, path: str) -> str:
        """The file's content at the head commit, or "" when unavailable.

        Cached per run and bounded. Returns "" rather than raising on every
        failure mode, because "I could not read this file" and "this file does
        not exist" mean the same thing to a check that wanted to look at it —
        and a check that needs to tell them apart should not be inferring it
        from an exception type.

        The cache holds a *task* per path rather than a string behind one lock.
        A single lock would be correct and would also serialise every file read
        in the run: two checks fetching two different files would queue behind
        each other over the network for no reason. Per path, two checks asking
        for the same file share one fetch and two checks asking for different
        files do not wait.
        """
        cached = self._file_cache.get(path)
        if cached is not None:
            return await cached
        if self.provider is None or self.pr_info is None:
            return ""
        if self._file_lock is None:
            self._file_lock = asyncio.Lock()
        async with self._file_lock:
            # Re-checked inside: two callers can both miss above, and only one
            # of them may create the task.
            cached = self._file_cache.get(path)
            if cached is None:
                cached = asyncio.ensure_future(self._fetch_file(path))
                self._file_cache[path] = cached
        return await cached

    async def _fetch_file(self, path: str) -> str:
        try:
            content = await self.provider.get_file_content(
                self.pr_info, path, self.head_sha or self.head_branch
            )
        except Exception as exc:  # noqa: BLE001 - unreadable is not fatal
            logger.debug("Check could not read %s at %s: %s", path, self.head_sha, exc)
            return ""
        return (content or "")[:MAX_FILE_BYTES]
