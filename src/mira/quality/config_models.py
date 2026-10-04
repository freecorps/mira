"""Configuration for review-quality measurement: backtests and escaped bugs.

Kept in its own module, like the check and triage settings, so ``mira.config``
only has to import and mount it.

Both features are off unless asked for. A backtest only ever runs when
someone invokes ``mira backtest`` — there is nothing automatic to switch on —
so its section holds limits rather than a switch. Escaped-bug tracking reacts
to merge and push webhooks, and so it has an ``enabled`` flag that defaults to
``False``.
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
