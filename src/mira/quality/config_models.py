"""Configuration for review-quality measurement: backtests and escaped bugs.

Kept in its own module, like the check and triage settings, so ``mira.config``
only has to import and mount it.

Backtests and escaped bugs are off unless asked for. A backtest only ever runs when
someone invokes ``mira backtest`` — there is nothing automatic to switch on —
so its section holds limits rather than a switch. Escaped-bug tracking reacts
to merge and push webhooks, and so it has an ``enabled`` flag that defaults to
``False``. Peer recall only reads review threads and so runs by default;
what it may *change* — positive learned rules — sits behind its own flag.
"""

from __future__ import annotations

from pydantic import BaseModel, Field, field_validator


class BacktestConfig(BaseModel):
    """Limits for ``mira backtest`` (replaying Mira over merged pull requests).

    Every limit here bounds spend: a replay calls the same models a live review
    does, once per pull request per configuration.
    """

    # Hard ceiling on pull requests per run, whatever the command line asks.
    max_prs: int = Field(default=25, ge=1, le=500)
    # Reviews running at once. Small by default: the bottleneck is the model's
    # rate limit, and a backtest must not starve live reviews of it.
    max_concurrency: int = Field(default=2, ge=1, le=16)
    # Estimated USD across the whole run (both configurations of an A/B).
    # Above it, the run refuses to start; during it, no new review is started
    # once actual spend crosses it.
    max_estimated_cost_usd: float = Field(default=5.0, ge=0.0)
    # How far, in lines, a finding may sit from a ground-truth signal and still
    # count as the same issue. Lines drift between the reviewed head and the
    # commit that later fixed them, so exact matching under-counts.
    line_tolerance: int = Field(default=3, ge=0, le=50)
    # Window after the merge in which a commit touching a flagged line counts
    # as a fix for it.
    fix_window_days: int = Field(default=30, ge=1, le=365)
    # Files per pull request whose later history is searched for fix commits.
    max_files_for_fix_search: int = Field(default=15, ge=0, le=200)
    # Commits per file inspected for fix/revert evidence.
    max_fix_commits_per_file: int = Field(default=10, ge=1, le=100)
    # Persist runs to the repository's store (shown on the dashboard).
    store_results: bool = True


class EscapedBugsConfig(BaseModel):
    """Revert and hotfix detection, linked back to pull requests Mira reviewed."""

    enabled: bool = False
    # Repositories (``owner/repo``) to track. Empty means every repository.
    repos: list[str] = Field(default_factory=list)
    detect_reverts: bool = True
    detect_hotfixes: bool = True
    # A merged pull request carrying any of these labels is a hotfix.
    hotfix_labels: list[str] = Field(
        default_factory=lambda: ["hotfix", "bug", "bugfix", "regression"]
    )
    # A merged pull request whose head branch starts with one of these is a hotfix.
    hotfix_branch_prefixes: list[str] = Field(
        default_factory=lambda: ["hotfix/", "hotfix-", "bugfix/"]
    )
    # A "fixes #N" reference counts only when issue N carries one of these.
    bug_issue_labels: list[str] = Field(default_factory=lambda: ["bug", "regression", "defect"])
    # How far back, in days, the original pull request may have merged.
    window_days: int = Field(default=90, ge=1, le=730)
    # Distance, in lines, between a fixed line and a finding for the finding
    # to count as having flagged it.
    line_tolerance: int = Field(default=3, ge=0, le=50)
    # Bounds on the work one fix may cause.
    max_files: int = Field(default=20, ge=1, le=200)
    max_original_prs: int = Field(default=5, ge=1, le=50)
    # Record each missed bug as a pending learning candidate (governed like
    # every other candidate: nothing changes a review until it is approved).
    feed_learning: bool = True

    @field_validator("repos", "hotfix_labels", "bug_issue_labels", "hotfix_branch_prefixes")
    @classmethod
    def _strip(cls, values: list[str]) -> list[str]:
        return [value.strip() for value in values if value and value.strip()]

    def tracks(self, owner: str, repo: str) -> bool:
        """Whether this repository is in scope for escaped-bug tracking."""
        if not self.enabled:
            return False
        if not self.repos:
            return True
        wanted = f"{owner}/{repo}".lower()
        return any(entry.lower() == wanted for entry in self.repos)


# Peer review bots this organisation's pull requests carry, by GitHub login.
# The first three were measured: across pull requests Mira also reviewed, Mira
# had a finding within 15 lines of 87 of Cursor Bugbot's 490 threads, 38 of
# CodeRabbit's 215 and 39 of CodeAnt's 223. The others were present in smaller
# numbers. A login that never comments costs nothing.
DEFAULT_PEER_BOTS = (
    "cursor[bot]",
    "coderabbitai[bot]",
    "codeant-ai[bot]",
    "chatgpt-codex-connector[bot]",
    "qodo-merge-pro[bot]",
    "graphite-app[bot]",
)


class PeerRecallConfig(BaseModel):
    """Recall measured against other review bots, and positive rules learned from it.

    Unlike escaped bugs, which wait for something to be reverted, every review
    thread another bot opened on a pull request Mira reviewed is a labelled
    example within a day: either Mira had a finding near it or it did not, and
    either the author fixed it or they did not. On this organisation's history
    Mira missed 403 of Cursor Bugbot's 490 threads and the author fixed 297 of
    those, mostly lifecycle, state-machine and cross-file bugs — exactly what
    a "do not report X" rule can never teach. See docs/peer-recall.md.

    Reading is GitHub-only (review threads come from its GraphQL API); other
    platforms are skipped. Collecting reads and stores; nothing is posted.
    """

    # The scheduler inside ``mira serve``. ``mira peer-recall scan`` and the
    # dashboard's "Run now" work regardless of it.
    enabled: bool = True
    # How often the scheduler collects: once a day at ``hour`` UTC.
    schedule: str = "daily"
    hour: int = Field(default=4, ge=0, le=23)
    # Repositories (``owner/repo``) to cover. Empty means every registered one.
    repos: list[str] = Field(default_factory=list)
    bots: list[str] = Field(default_factory=lambda: list(DEFAULT_PEER_BOTS))
    # Pull requests Mira reviewed in this window, closed or merged, are read.
    # An open pull request is left for a later run: its threads are not settled.
    lookback_days: int = Field(default=30, ge=1, le=365)
    # One GraphQL request per pull request (plus one compare per distinct
    # commit a thread was opened on, only when a resolved or outdated thread
    # needs checking); a pull request is read once after it closes.
    max_prs_per_run: int = Field(default=200, ge=1, le=5000)
    # A Mira finding in the same file within this many lines of a peer thread
    # counts as having seen the same issue. 15 is what the measurement above
    # used: line numbers drift between the commits each bot reviewed.
    match_line_tolerance: int = Field(default=15, ge=0, le=200)
    # A resolved or outdated thread counts as addressed only when the code
    # within this many lines of it changed between the thread's commit and the
    # merged head. Resolving alone is also how people dismiss bot noise.
    addressed_line_tolerance: int = Field(default=5, ge=0, le=50)

    # Positive learning: missed-and-addressed threads become "look for …"
    # rules. Accepted automatically (as ``mira-auto``) only once enough
    # independent misses support one pattern, so ``learning.auto_rule_ttl_days``
    # and ``learning.max_auto_rules_per_repo`` govern them like any other
    # automatic rule.
    positive_learning: bool = True
    # Distinct pull requests whose misses must support one pattern before it
    # becomes active. Two threads on one pull request (two bots on one bug)
    # count once.
    min_support: int = Field(default=2, ge=1, le=50)
    # The synthesiser's own confidence that a miss generalises to a pattern.
    min_confidence: float = Field(default=0.7, ge=0.0, le=1.0)
    # Review-model calls per repository per run: one per missed-and-addressed
    # thread, each bounded to one diff of at most 6,000 characters.
    max_syntheses_per_run: int = Field(default=20, ge=0, le=500)
    # Active positive rules per repository, inside the overall automatic-rule
    # budget, so "look for" rules cannot crowd out what pushback taught.
    max_positive_rules_per_repo: int = Field(default=10, ge=1, le=100)

    @field_validator("repos", "bots")
    @classmethod
    def _strip(cls, values: list[str]) -> list[str]:
        return [value.strip() for value in values if value and value.strip()]

    @field_validator("schedule")
    @classmethod
    def _schedule(cls, value: str) -> str:
        value = (value or "daily").strip().lower()
        if value not in {"daily", "weekly"}:
            raise ValueError("peer_recall.schedule must be 'daily' or 'weekly'")
        return value

    def covers(self, owner: str, repo: str) -> bool:
        """Whether this repository is in scope for peer recall."""
        if not self.repos:
            return True
        wanted = f"{owner}/{repo}".lower()
        return any(entry.lower() == wanted for entry in self.repos)
