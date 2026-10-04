"""Replay the review engine over merged pull requests and score the result.

The run has two phases with a human in between:

1. **Plan** — list the pull requests, fetch each diff (cheap, no model calls),
   and estimate what reviewing them will cost under every configuration.
2. **Run** — only when confirmed and under budget: review each diff with the
   engine in dry-run mode behind a :class:`~mira.quality.readonly.ReadOnlyProvider`,
   collect ground truth, and score.

The engine is driven through ``review_diff``, which records nothing — no
review event, no findings, no rule exposures — so a replay leaves the
repository's real history exactly as it found it. Posting is impossible twice
over: ``dry_run=True``, and a provider that has no write path to call.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from mira.models import MergedPullRequest, PRInfo
from mira.quality.config_models import BacktestConfig
from mira.quality.ground_truth import collect_signals
from mira.quality.models import BacktestRun, PRBacktest, Signal
from mira.quality.readonly import ReadOnlyProvider
from mira.quality.scoring import score_findings

logger = logging.getLogger(__name__)

# Rough shape of one review, for the pre-run estimate: the diff goes into the
# review prompt and the critique, the walkthrough reads it again when enabled,
# and every prompt carries fixed instructions and context around it.
_CHARS_PER_TOKEN = 3.5
_PROMPT_OVERHEAD_TOKENS = 6_000
_OUTPUT_TOKENS_PER_REVIEW = 2_500


@dataclass
class Variant:
    """One configuration under test. ``name`` is ``A`` or ``B``."""

    name: str
    label: str
    config: Any  # MiraConfig


@dataclass
class PlannedPR:
    merged: MergedPullRequest
    pr_info: PRInfo
    diff_text: str = ""
    estimated_cost_usd: dict[str, float] = field(default_factory=dict)
    error: str = ""


@dataclass
class Plan:
    prs: list[PlannedPR]
    notes: list[str] = field(default_factory=list)

    @property
    def estimated_cost_usd(self) -> float:
        return sum(sum(p.estimated_cost_usd.values()) for p in self.prs)


LLMBundle = tuple[Any, Any, Any]
LLMFactory = Callable[[Any], LLMBundle]


def default_llm_factory(config: Any) -> LLMBundle:
    """The same three clients a live review would build from this config."""
    from mira.dashboard.models_config import llm_config_for
    from mira.llm import create_llm

    return (
        create_llm(llm_config_for("review", config.llm)),
        create_llm(llm_config_for("indexing", config.llm)),
        create_llm(llm_config_for("security", config.llm)),
    )


def pr_info_for(merged: MergedPullRequest, *, owner: str, repo: str, platform: str) -> PRInfo:
    return PRInfo(
        title=merged.title,
        description=merged.body,
        base_branch=merged.base_branch,
        head_branch=merged.head_branch,
        url=merged.url,
        number=merged.number,
        owner=owner,
        repo=repo,
        base_sha=merged.base_sha,
        head_sha=merged.head_sha,
        platform=platform,
        author=merged.author,
    )


def repo_scope(owner: str, repo: str, platform: str) -> PRInfo:
    """A PRInfo that names only a repository, for repository-level provider reads."""
    return PRInfo(
        title="",
        description="",
        base_branch="",
        head_branch="",
        url="",
        number=0,
        owner=owner,
        repo=repo,
        platform=platform,
    )


def estimate_review_cost(diff_chars: int, config: Any) -> float:
    """USD estimate for reviewing a diff of ``diff_chars`` under ``config``.

    Deliberately pessimistic: it prices every token at the review model's
    rate and ignores the file filters that would shrink the diff. A cap set
    against this number holds against the real spend.
    """
    from mira.llm import registry

    review = config.review
    capped = min(int(diff_chars), int(getattr(review, "max_diff_size", diff_chars) or diff_chars))
    diff_tokens = capped / _CHARS_PER_TOKEN
    passes = 2 + (1 if getattr(review, "walkthrough", True) else 0)
    input_tokens = diff_tokens * passes + _PROMPT_OVERHEAD_TOKENS
    output_tokens = _OUTPUT_TOKENS_PER_REVIEW
    model = getattr(config.llm, "review_model", "") or config.llm.model
    input_rate, output_rate = registry.pricing(model)
    return (input_tokens * input_rate + output_tokens * output_rate) / 1_000_000


def _cost_of(clients: LLMBundle, fallback_model: str) -> tuple[int, int, float]:
    """Tokens and USD actually spent by these clients, each at its own model's price."""
    from mira.llm import registry

    prompt = completion = 0
    cost = 0.0
    seen: set[int] = set()
    for client in clients:
        if client is None or id(client) in seen:
            continue
        seen.add(id(client))
        usage = getattr(client, "usage", None)
        if not isinstance(usage, dict):
            continue
        p = int(usage.get("prompt_tokens", 0) or 0)
        c = int(usage.get("completion_tokens", 0) or 0)
        model = str(getattr(getattr(client, "config", None), "model", "") or fallback_model)
        input_rate, output_rate = registry.pricing(model)
        prompt += p
        completion += c
        cost += (p * input_rate + c * output_rate) / 1_000_000
    return prompt, completion, cost


async def select_prs(
    provider: Any,
    scope: PRInfo,
    *,
    numbers: list[int] | None = None,
    since: float = 0.0,
    limit: int = 10,
) -> tuple[list[MergedPullRequest], list[str]]:
    """The merged pull requests to replay, plus notes on any that were dropped."""
    notes: list[str] = []
    if numbers:
        out = []
        for number in numbers[:limit]:
            merged = await provider.get_landed_pull_request(scope, number)
            if merged is None:
                notes.append(f"#{number} skipped: not found or not merged")
                continue
            out.append(merged)
        if len(numbers) > limit:
            notes.append(f"{len(numbers) - limit} PR(s) dropped by the max-PRs limit")
        return out, notes
    prs = await provider.list_landed_pull_requests(scope, since=since, limit=limit, max_files=0)
    return prs[:limit], notes


class BacktestRunner:
    """Plans and runs one backtest. Construct, ``plan()``, then ``run(plan)``."""

    def __init__(
        self,
        provider: Any,
        variants: list[Variant],
        *,
        owner: str,
        repo: str,
        platform: str,
        limits: BacktestConfig,
        bot_login: str = "miracodeai",
        llm_factory: LLMFactory = default_llm_factory,
        quality_store: Any = None,
        engine_factory: Callable[..., Any] | None = None,
    ) -> None:
        if not variants:
            raise ValueError("A backtest needs at least one configuration")
        # The raw provider is never kept: every read in this class goes
        # through a read-only wrapper.
        self._inner = provider.inner if isinstance(provider, ReadOnlyProvider) else provider
        self.provider = ReadOnlyProvider(self._inner)
        self.variants = variants
        self.owner = owner
        self.repo = repo
        self.platform = platform
        self.limits = limits
        self.bot_login = bot_login
        self.llm_factory = llm_factory
        self.quality_store = quality_store
        self.engine_factory = engine_factory or _default_engine_factory

    @property
    def scope(self) -> PRInfo:
        return repo_scope(self.owner, self.repo, self.platform)

    async def plan(
        self, *, numbers: list[int] | None = None, since: float = 0.0, limit: int = 10
    ) -> Plan:
        limit = max(1, min(int(limit), self.limits.max_prs))
        merged, notes = await select_prs(
            self.provider, self.scope, numbers=numbers, since=since, limit=limit
        )
        planned: list[PlannedPR] = []
        for item in merged:
            pr_info = pr_info_for(item, owner=self.owner, repo=self.repo, platform=self.platform)
            entry = PlannedPR(merged=item, pr_info=pr_info)
            try:
                entry.diff_text = await self.provider.get_pr_diff(pr_info)
            except Exception as exc:  # noqa: BLE001 - one bad PR does not sink the run
                entry.error = f"diff unavailable: {exc}"
            for variant in self.variants:
                entry.estimated_cost_usd[variant.name] = (
                    estimate_review_cost(len(entry.diff_text), variant.config)
                    if entry.diff_text
                    else 0.0
                )
            planned.append(entry)
        return Plan(prs=planned, notes=notes)

    def over_budget(self, plan: Plan) -> bool:
        return plan.estimated_cost_usd > self.limits.max_estimated_cost_usd

    async def run(
        self,
        plan: Plan,
        *,
        on_result: Callable[[PRBacktest], Awaitable[None] | None] | None = None,
    ) -> BacktestRun:
        run = BacktestRun(
            id=f"bt-{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}",
            platform=self.platform,
            owner=self.owner,
            repo=self.repo,
            created_at=time.time(),
            variants={v.name: v.label for v in self.variants},
            params={
                "prs": [p.merged.number for p in plan.prs],
                "max_concurrency": self.limits.max_concurrency,
                "max_estimated_cost_usd": self.limits.max_estimated_cost_usd,
                "line_tolerance": self.limits.line_tolerance,
                "fix_window_days": self.limits.fix_window_days,
            },
            estimated_cost_usd=plan.estimated_cost_usd,
            notes=list(plan.notes),
        )
        self._save_run(run)

        semaphore = asyncio.Semaphore(self.limits.max_concurrency)
        spent = [0.0]
        # Estimated cost of reviews already started but not yet finished.
        # Counted against the limit so concurrent workers cannot all pass the
        # check before any of them has reported what it actually spent.
        reserved = [0.0]
        stopped = [False]
        lock = asyncio.Lock()

        async def _one(entry: PlannedPR) -> list[PRBacktest]:
            async with semaphore:
                signals, notes = await self._signals(entry)
                results = []
                for variant in self.variants:
                    estimate = float(entry.estimated_cost_usd.get(variant.name, 0.0))
                    async with lock:
                        committed = spent[0] + reserved[0]
                        exhausted = committed >= self.limits.max_estimated_cost_usd
                        if not exhausted:
                            reserved[0] += estimate
                    if exhausted:
                        stopped[0] = True
                        result = self._skeleton(variant, entry, signals)
                        result.skipped_reason = "spend limit reached"
                    else:
                        cost = 0.0
                        try:
                            result = await self._review(variant, entry, signals)
                            cost = result.cost_usd
                        finally:
                            # Swap the reservation for the actual cost in one step.
                            async with lock:
                                reserved[0] -= estimate
                                spent[0] += cost
                    results.append(result)
                    await self._emit(run, result, on_result)
                for note in notes:
                    line = f"#{entry.merged.number}: {note}"
                    if line not in run.notes:
                        run.notes.append(line)
                return results

        gathered = await asyncio.gather(*[_one(entry) for entry in plan.prs])
        run.results = [r for batch in gathered for r in batch]
        run.results.sort(key=lambda r: (-r.pr_number, r.variant))
        run.status = "stopped" if stopped[0] else "completed"
        if stopped[0]:
            run.notes.append(
                f"Stopped starting reviews once spend reached "
                f"${self.limits.max_estimated_cost_usd:.2f}."
            )
        run.finished_at = time.time()
        self._save_run(run)
        return run

    # ---------------------------------------------------------------- steps

    async def _signals(self, entry: PlannedPR) -> tuple[list[Signal], list[str]]:
        if entry.error:
            return [], []
        try:
            return await collect_signals(
                self.provider,
                entry.pr_info,
                entry.merged,
                entry.diff_text,
                bot_login=self.bot_login,
                window_days=self.limits.fix_window_days,
                max_files=self.limits.max_files_for_fix_search,
                max_commits=self.limits.max_fix_commits_per_file,
                quality_store=self.quality_store,
                line_tolerance=self.limits.line_tolerance,
            )
        except Exception as exc:  # noqa: BLE001
            return [], [f"ground truth unavailable: {exc}"]

    @staticmethod
    def _skeleton(variant: Variant, entry: PlannedPR, signals: list[Signal]) -> PRBacktest:
        result = PRBacktest(
            variant=variant.name,
            pr_number=entry.merged.number,
            pr_title=entry.merged.title,
            pr_url=entry.merged.url,
            merged_at=entry.merged.merged_at,
            signals=list(signals),
        )
        result.score.positives = sum(1 for s in signals if s.positive)
        return result

    async def _review(
        self, variant: Variant, entry: PlannedPR, signals: list[Signal]
    ) -> PRBacktest:
        result = self._skeleton(variant, entry, signals)
        if entry.error:
            result.error = entry.error
            return result
        if not entry.diff_text.strip():
            result.skipped_reason = "empty diff"
            return result

        # A wrapper per review, so a blocked write is attributed to the PR
        # and configuration that attempted it.
        guard = ReadOnlyProvider(self._inner)
        clients: LLMBundle = (None, None, None)
        started = time.monotonic()
        try:
            clients = self.llm_factory(variant.config)
            engine = self.engine_factory(variant.config, clients, guard)
            review = await engine.review_diff(
                entry.diff_text,
                repo_scope=entry.pr_info,
                title=entry.pr_info.title,
                description=entry.pr_info.description,
            )
        except Exception as exc:  # noqa: BLE001 - recorded on the result
            logger.warning("Backtest review of #%s failed: %s", entry.merged.number, exc)
            result.error = f"{type(exc).__name__}: {exc}"[:500]
            review = None
        result.latency_ms = int((time.monotonic() - started) * 1000)
        prompt, completion, cost = _cost_of(clients, variant.config.llm.model)
        result.prompt_tokens, result.completion_tokens, result.cost_usd = prompt, completion, cost
        result.blocked_writes = len(guard.blocked_calls)
        if review is not None:
            if review.skipped_reason and not review.comments:
                result.skipped_reason = review.skipped_reason
            scored, score = score_findings(
                review.comments, signals, tolerance=self.limits.line_tolerance
            )
            result.findings = scored
            result.score = score
        return result

    async def _emit(
        self,
        run: BacktestRun,
        result: PRBacktest,
        on_result: Callable[[PRBacktest], Awaitable[None] | None] | None,
    ) -> None:
        if self.quality_store is not None and self.limits.store_results:
            try:
                self.quality_store.save_backtest_result(
                    run.id, result, owner=run.owner, repo=run.repo
                )
            except Exception as exc:  # noqa: BLE001 - the report still has it
                logger.warning("Could not store backtest result: %s", exc)
        if on_result is not None:
            maybe = on_result(result)
            if asyncio.iscoroutine(maybe):
                await maybe

    def _save_run(self, run: BacktestRun) -> None:
        if self.quality_store is None or not self.limits.store_results:
            return
        try:
            self.quality_store.save_backtest_run(run)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Could not store backtest run: %s", exc)


def _default_engine_factory(config: Any, clients: LLMBundle, provider: Any) -> Any:
    from mira.core.engine import ReviewEngine

    llm, indexing_llm, security_llm = clients
    return ReviewEngine(
        config=config,
        llm=llm,
        provider=provider,
        dry_run=True,
        indexing_llm=indexing_llm,
        security_llm=security_llm,
    )
