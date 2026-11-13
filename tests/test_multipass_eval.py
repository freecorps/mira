"""Offline evaluation of the voting passes with the backtest harness.

A recorded case — a merged pull request, its diff, and a thread another review
bot opened that the author addressed and Mira missed — is replayed through the
real engine twice, single-pass (A) and with the shuffled voting passes plus the
system pass (B). The model is scripted, never live: what each pass "finds" is
decided by its temperature (pass 0 runs at the configured one) and by whether
it is the system prompt, so the comparison exercises the harness, the vote and
the scoring end to end without spending anything.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

from mira.config import MiraConfig
from mira.llm.provider import LLMProvider
from mira.quality.backtest import BacktestRunner, Variant, estimate_review_cost
from mira.quality.config_models import BacktestConfig
from mira.quality.models import SIGNAL_PEER_ADDRESSED, PeerThread
from mira.quality.multipass_eval import pass_variants, peer_miss_prs, recall_by_kind
from mira.quality.report import render_markdown
from mira.quality.store import open_quality_store
from tests.quality_support import FakeHistoryProvider, merged_pr

MERGED_AT = 1_780_000_000.0


def _diff() -> str:
    """Two files, 24 changed lines each: past the multipass threshold."""
    out = []
    for path in ("src/session.py", "src/presence.py"):
        out.append(f"diff --git a/{path} b/{path}\n--- a/{path}\n+++ b/{path}\n")
        out.append("@@ -0,0 +1,24 @@\n" + "".join(f"+state_{i} = {i}\n" for i in range(24)))
    return "".join(out)


def _finding(path: str, line: int, title: str, severity: str = "warning") -> dict:
    return {
        "path": path,
        "line": line,
        "severity": severity,
        "category": "bug",
        "title": title,
        "body": f"{title}: the state set here outlives the session that owns it.",
        "confidence": 0.85,
        "existing_code": f"state_{line - 1} = {line - 1}",
    }


SEEN_BY_EVERY_PASS = _finding("src/session.py", 3, "Token kept after sign-out")
# What the bot found and Mira's single pass did not: the shuffled passes
# (which read presence.py first) both report it, so the vote keeps it.
PEER_MISS = _finding("src/presence.py", 8, "Presence not cleared on pagehide")
# One pass's flicker: reported once, dropped by the vote.
FLICKER = _finding("src/session.py", 20, "Unused variable")


def _scripted_llm() -> Any:
    llm = MagicMock(spec=LLMProvider)

    async def review(messages: list[dict], temperature: float | None = None) -> str:
        if messages[0]["content"].startswith("You are Mira's system reviewer"):
            comments: list[dict] = []
        elif temperature is None:
            comments = [SEEN_BY_EVERY_PASS]
        elif temperature < 0.4:
            comments = [SEEN_BY_EVERY_PASS, PEER_MISS, FLICKER]
        else:
            comments = [SEEN_BY_EVERY_PASS, PEER_MISS]
        return json.dumps({"comments": comments, "summary": "ok"})

    llm.review = AsyncMock(side_effect=review)
    llm.count_tokens = MagicMock(side_effect=lambda text: (len(text) + 3) // 4)
    llm.usage = {"prompt_tokens": 1000, "completion_tokens": 100, "total_tokens": 1100}
    llm.config = SimpleNamespace(model="anthropic/claude-sonnet-4-6")
    return llm


def _base_config() -> MiraConfig:
    config = MiraConfig()
    for knob in (
        "walkthrough",
        "security_pass",
        "dependency_overlap",
        "self_critique",
        "include_summary",
        "code_context",
        "agentic_tools",
        "osv_scan",
    ):
        setattr(config.review, knob, False)
    config.review.code_graph.enabled = False
    config.review.dependency_updates.enabled = False
    return config


def _peer_thread() -> PeerThread:
    return PeerThread(
        id="github:acme/app:7:bugbot-1",
        platform="github",
        owner="acme",
        repo="app",
        pr_number=7,
        bot="cursor",
        path="src/presence.py",
        line=8,
        pr_state="merged",
        pr_closed_at=MERGED_AT,
        title="Presence is not cleared on pagehide",
        addressed=True,
        mira_flagged=False,
    )


def test_pass_variants_differ_only_in_the_passes():
    config = _base_config()
    (a_name, a_label, a), (b_name, b_label, b) = pass_variants(config, "mira.yaml")
    assert (a_name, b_name) == ("A", "B")
    assert (a.review.passes, a.review.system_pass) == (1, False)
    assert (b.review.passes, b.review.system_pass) == (3, True)
    assert "passes=1" in a_label and "passes=3 + system" in b_label
    assert a.review.self_critique == b.review.self_critique == config.review.self_critique
    assert config.review.passes == 3  # the input is not mutated


def test_the_estimate_counts_the_extra_passes():
    config = _base_config()
    single, multi = (v[2] for v in pass_variants(config))
    assert estimate_review_cost(40_000, multi) > estimate_review_cost(40_000, single)


async def test_voting_passes_close_a_recorded_peer_miss_offline():
    provider = FakeHistoryProvider(
        merged={7: merged_pr(7, title="Session cleanup", merged_at=MERGED_AT)},
        diffs={7: _diff()},
    )
    llm = _scripted_llm()
    with open_quality_store("acme", "app") as store:
        store.upsert_peer_thread(_peer_thread())
        assert peer_miss_prs(store) == [7]

        runner = BacktestRunner(
            provider,
            [Variant(*v) for v in pass_variants(_base_config())],
            owner="acme",
            repo="app",
            platform="github",
            limits=BacktestConfig(max_estimated_cost_usd=10.0, store_results=False),
            llm_factory=lambda _config: (llm, llm, llm),
            quality_store=store,
        )
        run = await runner.run(await runner.plan(numbers=peer_miss_prs(store)))

    assert provider.writes == []
    results = {r.variant: r for r in run.results}
    assert results["A"].error == results["B"].error == ""
    assert {s.kind for s in results["A"].signals} == {SIGNAL_PEER_ADDRESSED}

    by_kind = recall_by_kind(run.results)
    assert by_kind["A"][SIGNAL_PEER_ADDRESSED] == (0, 1)
    assert by_kind["B"][SIGNAL_PEER_ADDRESSED] == (1, 1)
    # The flicker one pass reported did not survive the vote.
    assert {f.title for f in results["B"].findings} == {
        SEEN_BY_EVERY_PASS["title"],
        PEER_MISS["title"],
    }

    markdown = render_markdown(run)
    assert "Recall by signal kind" in markdown
    assert "| `peer_addressed` | 0/1 (0%) | 1/1 (100%) |" in markdown


def test_cli_compare_passes_plans_both_variants_from_one_config(monkeypatch: Any) -> None:
    from click.testing import CliRunner

    from mira.cli import main

    provider = FakeHistoryProvider(
        merged={7: merged_pr(7, merged_at=MERGED_AT)},
        diffs={7: _diff()},
    )
    monkeypatch.setattr("mira.quality.cli._provider", lambda platform, token: provider)
    args = ["backtest", "--repo", "acme/app", "--prs", "7", "--token", "t", "--no-save"]

    result = CliRunner().invoke(main, [*args, "--compare-passes"])
    assert result.exit_code == 0, result.output
    assert "× 2 configuration(s)" in result.output

    clash = CliRunner().invoke(main, [*args, "--compare-passes", "--config-b", "b.yaml"])
    assert clash.exit_code != 0 and "drop --config-b" in clash.output


def test_cli_peer_misses_needs_a_peer_recall_scan(monkeypatch: Any) -> None:
    from click.testing import CliRunner

    from mira.cli import main

    monkeypatch.setattr("mira.quality.cli._provider", lambda platform, token: FakeHistoryProvider())
    result = CliRunner().invoke(
        main, ["backtest", "--repo", "acme/app", "--token", "t", "--peer-misses", "--no-save"]
    )
    assert result.exit_code != 0
    assert "peer-recall scan" in result.output
