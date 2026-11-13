"""Shuffled voting passes and the whole-PR system pass (core/multipass.py, core/system_pass.py)."""

from __future__ import annotations

import asyncio
import json
import time
from unittest.mock import AsyncMock, MagicMock

import pytest

from mira.config import MiraConfig
from mira.core.engine import ReviewEngine
from mira.core.multipass import (
    drop_unproven,
    pass_seed,
    pass_temperature,
    passes_per_part,
    run_passes,
    same_location,
    shuffle_files,
    vote,
)
from mira.core.passes import _critique_keep, self_critique
from mira.core.system_pass import build_system_input, system_review_pass
from mira.models import FileChangeType, FileDiff, HunkInfo, ReviewComment, Severity

# ---------------------------------------------------------------- helpers


def finding(
    path="src/a.py",
    line=10,
    title="Lock never released on error",
    severity=Severity.WARNING,
    category="bug",
    confidence=0.8,
) -> ReviewComment:
    return ReviewComment(
        path=path,
        line=line,
        end_line=None,
        severity=severity,
        category=category,
        title=title,
        body="The lock taken above is not released when the call raises.",
        confidence=confidence,
    )


def hunk(start: int, length: int = 2) -> HunkInfo:
    body = "".join(f"+line {start + i}\n" for i in range(length))
    return HunkInfo(
        start, 0, start, length, f"@@ -{start},0 +{start},{length} @@ def f{start}():\n{body}"
    )


def file(path: str, starts=(1,)) -> FileDiff:
    return FileDiff(
        path,
        FileChangeType.MODIFIED,
        [hunk(s) for s in starts],
        language="python",
        added_lines=2 * len(starts),
    )


def diff(paths, lines=12):
    return "".join(
        f"diff --git a/{path} b/{path}\n--- a/{path}\n+++ b/{path}\n"
        f"@@ -0,0 +1,{lines} @@\n" + "".join(f"+value_{i} = {i}\n" for i in range(lines))
        for path in paths
    )


def engine_config(**review) -> MiraConfig:
    cfg = MiraConfig()
    cfg.review.walkthrough = False
    cfg.review.security_pass = False
    cfg.review.dependency_overlap = False
    cfg.review.self_critique = False
    cfg.review.include_summary = False
    cfg.review.code_context = False
    for key, value in review.items():
        setattr(cfg.review, key, value)
    return cfg


def llm(review):
    model = MagicMock()
    model.count_tokens.side_effect = lambda text: (len(text) + 3) // 4
    model.review = AsyncMock(side_effect=review)
    model.usage = {}
    return model


def response(*comments: dict) -> str:
    return json.dumps({"comments": list(comments), "summary": "checked"})


def comment_json(path, line, title, severity="warning", category="bug", confidence=0.85):
    return {
        "path": path,
        "line": line,
        "severity": severity,
        "category": category,
        "title": title,
        "body": f"{title}. The value assigned here is read before it is set elsewhere.",
        "confidence": confidence,
        "existing_code": f"value_{line - 1} = {line - 1}",
    }


def is_system(messages) -> bool:
    return messages[0]["content"].startswith("You are Mira's system reviewer")


# ---------------------------------------------------------------- shuffling


def test_pass_seed_is_stable_and_differs_per_pass():
    assert pass_seed("src/a.py\nsrc/b.py", 1) == pass_seed("src/a.py\nsrc/b.py", 1)
    assert pass_seed("src/a.py\nsrc/b.py", 1) != pass_seed("src/a.py\nsrc/b.py", 2)
    assert pass_seed("src/a.py", 1) != pass_seed("src/b.py", 1)


def test_shuffle_is_deterministic_per_seed_and_leaves_originals_alone():
    files = [file(f"src/f{i}.py", starts=(1, 20, 40)) for i in range(8)]
    original = [(f.path, [h.target_start for h in f.hunks]) for f in files]

    first = shuffle_files(files, 1234)
    again = shuffle_files(files, 1234)
    other = shuffle_files(files, 9876)

    order = [f.path for f in first]
    assert order == [f.path for f in again]
    assert sorted(order) == sorted(f.path for f in files)
    assert order != [f.path for f in other] or order != [f.path for f in files]
    assert [[h.target_start for h in f.hunks] for f in first] == [
        [h.target_start for h in f.hunks] for f in again
    ]
    # Some file's hunks were reordered, and nothing was mutated in place.
    assert any([h.target_start for h in f.hunks] != [1, 20, 40] for f in first)
    assert [(f.path, [h.target_start for h in f.hunks]) for f in files] == original


def test_touching_hunks_keep_their_order():
    # Hunks that abut read as one block; shuffling them would mislead.
    files = [FileDiff("src/a.py", FileChangeType.MODIFIED, [hunk(1, 5), hunk(6, 5), hunk(11, 5)])]
    for seed in range(20):
        shuffled = shuffle_files(files, seed)[0]
        assert [h.target_start for h in shuffled.hunks] == [1, 6, 11]


def test_pass_temperature_spreads_from_the_configured_one():
    assert pass_temperature(0.2, 0.3, 0, 3) is None
    assert pass_temperature(0.2, 0.3, 1, 3) == pytest.approx(0.35)
    assert pass_temperature(0.2, 0.3, 2, 3) == pytest.approx(0.5)
    assert pass_temperature(0.9, 0.5, 4, 5) == 1.0
    assert pass_temperature(0.2, 0.3, 0, 1) is None


# ---------------------------------------------------------------- voting


def test_vote_keeps_findings_reported_by_threshold_passes():
    shared = finding(line=10)
    nearby = finding(line=12, title="Lock is never released when it raises")
    lone = finding(path="src/b.py", line=3, title="Off by one in pagination")
    result = vote([[shared, lone], [nearby], []], threshold=2)

    assert len(result.kept) == 1
    kept = result.kept[0]
    assert (kept.path, kept.votes, kept.vote_passes) == ("src/a.py", 2, 3)
    assert not kept.require_proven
    assert [(c.path, votes) for c, votes in result.dropped] == [("src/b.py", 1)]
    # Representatives are copies: the drafts keep their own (zero) counts.
    assert shared.votes == 0


def test_vote_threshold_is_clamped_to_passes_that_landed():
    a = finding(line=10)
    b = finding(path="src/b.py", line=3, title="Off by one")
    only = vote([[a, b]], threshold=2)
    assert [(c.path, c.votes, c.vote_passes) for c in only.kept] == [
        ("src/a.py", 1, 1),
        ("src/b.py", 1, 1),
    ]
    assert vote([], threshold=2).kept == []


def test_blocker_reported_once_is_kept_for_the_critic_to_prove():
    blocker = finding(severity=Severity.BLOCKER, title="Payment captured twice")
    result = vote([[blocker], [], []], threshold=2)
    assert len(result.kept) == 1
    assert result.kept[0].require_proven
    assert result.kept[0].votes == 1
    assert result.single_vote_blockers == 1


def test_one_pass_counts_once_however_often_it_repeats_itself():
    a = finding(line=10)
    b = finding(line=11, title="Lock not released on error path")
    result = vote([[a, b], [], []], threshold=2)
    assert result.kept == []


def test_same_location_needs_same_file_and_nearby_lines():
    assert same_location(finding(line=10), finding(line=13, title="Something else", category="bug"))
    assert not same_location(finding(line=10), finding(line=20))
    assert not same_location(finding(path="src/a.py"), finding(path="src/b.py"))
    assert not same_location(
        finding(line=10, category="bug", title="Lock leak"),
        finding(line=10, category="performance", title="Quadratic scan of users"),
    )


def test_passes_per_part_skips_tiny_changes_and_respects_the_run_budget():
    assert passes_per_part(3, parts=2, max_runs=30, changed_lines=10, min_lines=30) == 1
    assert passes_per_part(3, parts=2, max_runs=30, changed_lines=200, min_lines=30) == 3
    assert passes_per_part(3, parts=12, max_runs=30, changed_lines=900, min_lines=30) == 2
    assert passes_per_part(3, parts=40, max_runs=30, changed_lines=900, min_lines=30) == 1
    assert passes_per_part(1, parts=1, max_runs=30, changed_lines=900, min_lines=30) == 1


# ---------------------------------------------------------------- blocker-once rule


def test_critique_keeps_a_single_vote_blocker_only_when_proven():
    blocker = finding(severity=Severity.BLOCKER, confidence=0.95)
    blocker.require_proven = True
    assert _critique_keep({"evidence": "proven"}, blocker)
    assert not _critique_keep({"evidence": "plausible"}, blocker)
    assert not _critique_keep({"keep": True}, blocker)
    blocker.require_proven = False
    assert _critique_keep({"evidence": "plausible"}, blocker)


async def test_self_critique_clears_the_marker_on_a_proven_blocker():
    proven = finding(severity=Severity.BLOCKER, title="Task ended twice")
    plausible = finding(path="src/b.py", severity=Severity.BLOCKER, title="Presence not cleared")
    for c in (proven, plausible):
        c.require_proven = True
    critic = MagicMock()
    critic.complete_with_tools = AsyncMock(
        return_value=json.dumps(
            {
                "verdicts": [
                    {"index": 0, "evidence": "proven", "reason": "shown"},
                    {"index": 1, "evidence": "plausible", "reason": "other file"},
                ]
            }
        )
    )
    kept = await self_critique(critic, [proven, plausible], critic_llm=critic)
    assert kept == [proven]
    assert not proven.require_proven


def test_drop_unproven_promotes_a_voted_occurrence():
    head = finding(severity=Severity.BLOCKER)
    head.require_proven = True
    voted = finding(path="src/b.py")
    head.occurrences = [voted]
    plain = finding(path="src/c.py")
    kept, dropped = drop_unproven([head, plain])
    assert dropped == [head]
    assert kept == [voted, plain]


# ---------------------------------------------------------------- running passes


async def test_a_failed_pass_is_left_out_of_the_vote():
    async def one(i: int) -> str:
        if i == 1:
            raise RuntimeError("provider hiccup")
        return f"pass {i}"

    run = await run_passes(one, 3)
    assert [o.result for o in run.landed] == ["pass 0", "pass 2"]
    assert [o.status for o in run.outcomes] == ["ok", "failed", "ok"]


async def test_every_pass_failing_raises_the_first_error():
    async def one(i: int) -> str:
        raise ValueError(f"broken {i}")

    with pytest.raises(ValueError, match="broken 0"):
        await run_passes(one, 3)


async def test_a_pass_still_running_at_the_soft_deadline_is_cancelled():
    cancelled = []

    async def one(i: int) -> str:
        if i == 2:
            try:
                await asyncio.sleep(30)
            except asyncio.CancelledError:
                cancelled.append(i)
                raise
        return f"pass {i}"

    started = time.monotonic()
    run = await run_passes(one, 3, soft_deadline=time.monotonic() + 0.05)
    assert time.monotonic() - started < 5
    assert [o.result for o in run.landed] == ["pass 0", "pass 1"]
    assert run.outcomes[2].status == "late"
    assert cancelled == [2]


async def test_with_nothing_landed_by_the_soft_deadline_the_first_to_land_is_kept():
    async def one(i: int) -> str:
        await asyncio.sleep(0.05 if i == 1 else 30)
        return f"pass {i}"

    run = await run_passes(one, 3, soft_deadline=time.monotonic())
    assert [o.result for o in run.landed] == ["pass 1"]


# ---------------------------------------------------------------- system pass input


def test_system_input_lists_every_file_and_fits_hunks_to_the_budget():
    small = file("src/small.py", starts=(1,))
    big = FileDiff(
        "src/big.py",
        FileChangeType.MODIFIED,
        [hunk(1, 400)],
        language="python",
        added_lines=400,
    )
    shaped = build_system_input(
        [big, small], lambda text: (len(text) + 3) // 4, token_budget=600, callers="callers"
    )
    assert "src/big.py" in shaped.summary and "src/small.py" in shaped.summary
    assert shaped.shown_paths == ["src/small.py"]
    assert shaped.omitted_paths == ["src/big.py"]
    assert "hunks not shown" in shaped.summary
    assert "`def f1():`" in shaped.summary


async def test_system_pass_failure_costs_only_its_findings():
    model = MagicMock()
    model.review = AsyncMock(side_effect=RuntimeError("boom"))
    audit: list[dict] = []
    out = await system_review_pass(
        llm=model,
        files=[file("src/a.py")],
        count_tokens=lambda text: len(text) // 4,
        token_budget=5000,
        parse=lambda raw: [],
        audit=audit,
    )
    assert out == []
    assert audit[0]["stage"] == "system_pass" and audit[0]["status"] == "failed"


async def test_system_pass_respects_its_deadline():
    model = MagicMock()

    async def slow(messages):
        await asyncio.sleep(30)

    model.review = AsyncMock(side_effect=slow)
    audit: list[dict] = []
    started = time.monotonic()
    out = await system_review_pass(
        llm=model,
        files=[file("src/a.py")],
        count_tokens=lambda text: len(text) // 4,
        token_budget=5000,
        parse=lambda raw: [],
        deadline_seconds=1,
        audit=audit,
    )
    assert out == [] and time.monotonic() - started < 10
    assert "deadline" in audit[0]["error"]


# ---------------------------------------------------------------- the engine


async def test_engine_votes_across_shuffled_passes():
    paths = ["src/a.py", "src/b.py", "src/c.py"]
    seen_orders: list[list[str]] = []
    temperatures: list[float | None] = []

    async def review(messages, temperature=None):
        content = messages[-1]["content"]
        seen_orders.append(sorted(paths, key=lambda p: content.index(f"### `{p}`")))
        temperatures.append(temperature)
        recurring = comment_json("src/a.py", 4, "Value read before it is assigned")
        if temperature is None:
            return response(recurring, comment_json("src/b.py", 2, "Counter never reset"))
        if temperature < 0.4:
            return response(recurring)
        return response(
            recurring,
            comment_json("src/c.py", 6, "Session left behind after sign-out", "blocker"),
        )

    model = llm(review)
    config = engine_config(system_pass=False, agent_max_files=10)
    result = await ReviewEngine(config=config, llm=model).review_diff(diff(paths))

    assert model.review.await_count == 3
    assert sorted(t or 0 for t in temperatures) == pytest.approx([0, 0.35, 0.5])
    # Pass 0 reads the part as chunked; the others in their own seeded orders.
    by_temperature = dict(zip(temperatures, seen_orders, strict=True))
    assert by_temperature[None] == paths
    assert by_temperature[0.35] != paths and by_temperature[0.5] != paths
    titles = {c.title: c for c in result.comments}
    # Reported by all three passes: kept, with its votes on the finding.
    assert titles["Value read before it is assigned"].votes == 3
    assert titles["Value read before it is assigned"].vote_passes == 3
    # Reported once: a warning is dropped by the vote...
    assert "Counter never reset" not in titles
    # ...and a blocker needs a `proven` grade, which no critic gave here.
    assert "Session left behind after sign-out" not in titles
    stages = [e["stage"] for e in result.audit]
    assert "pass_vote" in stages
    vote_entry = next(e for e in result.audit if e["stage"] == "pass_vote")
    assert vote_entry["landed"] == 3 and vote_entry["single_vote_blockers"] == 1
    assert any(
        e["stage"] == "pass_vote" and "not proven" in e.get("reason", "") for e in result.audit
    )


async def test_engine_single_pass_on_a_tiny_change():
    model = llm(lambda messages, temperature=None: response())
    config = engine_config()
    await ReviewEngine(config=config, llm=model).review_diff(diff(["src/a.py"], lines=3))
    # One part, one pass, no system pass: exactly the old review.
    assert model.review.await_count == 1
    for call in model.review.await_args_list:
        assert "temperature" not in call.kwargs


async def test_engine_passes_one_keeps_the_old_behaviour():
    model = llm(lambda messages, temperature=None: response())
    config = engine_config(passes=1, system_pass=False)
    await ReviewEngine(config=config, llm=model).review_diff(diff(["src/a.py"], lines=60))
    assert model.review.await_count == 1


async def test_system_pass_findings_flow_through_the_pipeline():
    async def review(messages, temperature=None):
        if is_system(messages):
            assert "## Changed files" in messages[-1]["content"]
            return response(
                comment_json("src/b.py", 5, "Background task ended twice on retry", "warning"),
                # Not in the diff's paths: the parser drops it, as for any pass.
                comment_json("src/elsewhere.py", 1, "Caller not updated"),
            )
        return response()

    model = llm(review)
    config = engine_config(passes=1)
    result = await ReviewEngine(config=config, llm=model).review_diff(
        diff(["src/a.py", "src/b.py"], lines=20)
    )
    system = [c for c in result.comments if c.source_pass == "system"]
    assert [c.title for c in system] == ["Background task ended twice on retry"]
    assert any(e["stage"] == "system_pass" and e["status"] == "ok" for e in result.audit)
    assert {"stage": "drafted", "chunk": "system", "count": 1} in result.audit


async def test_system_pass_findings_are_graded_by_self_critique():
    async def review(messages, temperature=None):
        if is_system(messages):
            return response(comment_json("src/a.py", 5, "Presence not cleared on pagehide"))
        return response()

    model = llm(review)
    model.complete_with_tools = AsyncMock(
        return_value=json.dumps(
            {"verdicts": [{"index": 0, "evidence": "unsupported", "reason": "cleared in unload"}]}
        )
    )
    config = engine_config(passes=1, self_critique=True)
    result = await ReviewEngine(config=config, llm=model).review_diff(diff(["src/a.py"], lines=40))
    assert result.comments == []
    assert any(
        e["stage"] == "self_critique" and e["title"] == "Presence not cleared on pagehide"
        for e in result.audit
    )


async def test_a_hanging_system_pass_does_not_hold_the_review():
    async def review(messages, temperature=None):
        if is_system(messages):
            await asyncio.sleep(30)
        return response(comment_json("src/a.py", 3, "Value read before it is assigned"))

    model = llm(review)
    config = engine_config(passes=1, system_pass_deadline_seconds=1)
    started = time.monotonic()
    result = await ReviewEngine(config=config, llm=model).review_diff(diff(["src/a.py"], lines=40))
    assert time.monotonic() - started < 10
    assert [c.title for c in result.comments] == ["Value read before it is assigned"]
    assert any(e["stage"] == "system_pass" and e["status"] == "failed" for e in result.audit)


async def test_a_hanging_pass_is_left_out_and_the_part_still_lands():
    async def review(messages, temperature=None):
        if temperature is not None and temperature > 0.4:
            await asyncio.sleep(30)
        return response(comment_json("src/a.py", 3, "Value read before it is assigned"))

    model = llm(review)
    config = engine_config(system_pass=False, part_deadline_seconds=2)
    started = time.monotonic()
    result = await ReviewEngine(config=config, llm=model).review_diff(diff(["src/a.py"], lines=40))
    assert time.monotonic() - started < 10
    assert [(c.title, c.votes, c.vote_passes) for c in result.comments] == [
        ("Value read before it is assigned", 2, 2)
    ]
    entry = next(e for e in result.audit if e["stage"] == "pass_vote")
    assert [t["status"] for t in entry["timing"]] == ["ok", "ok", "late"]
    assert not any(e["stage"] == "chunk_failed" for e in result.audit)


async def test_total_pass_runs_are_bounded():
    model = llm(lambda messages, temperature=None: response())
    config = engine_config(system_pass=False, max_pass_runs=4, agent_max_files=1)
    config.filter.max_files = 1
    paths = [f"src/f{i}.py" for i in range(4)]
    await ReviewEngine(config=config, llm=model).review_diff(diff(paths, lines=20))
    # Four parts, a budget of four runs: one pass each.
    assert model.review.await_count == 4


# ---------------------------------------------------------------- recording the votes


def test_votes_are_persisted_with_the_finding_and_marked_on_the_comment(tmp_path):
    from mira.core.engine import _build_review_finding
    from mira.feedback.provenance import parse_votes
    from mira.index.store import IndexStore
    from mira.models import PRInfo
    from mira.providers.formatting import format_comment_body
    from mira.report import comment_dict

    pr = PRInfo(
        title="PR",
        description="",
        base_branch="main",
        head_branch="feature",
        url="https://github.com/acme/app/pull/7",
        number=7,
        owner="acme",
        repo="app",
        base_sha="base",
        head_sha="head",
    )
    comment = finding()
    comment.votes, comment.vote_passes = 2, 3
    row = _build_review_finding(pr, comment, prompt_model="m")
    store = IndexStore(str(tmp_path / "idx.db"))
    try:
        store.save_review_finding(row)
        loaded = store.get_review_finding(row.id)
    finally:
        store.close()
    assert loaded is not None and (loaded.votes, loaded.vote_passes) == (2, 3)

    assert parse_votes(format_comment_body(comment)) == (2, 3)
    assert comment_dict(comment)["votes"] == 2
    single = finding()
    assert parse_votes(format_comment_body(single)) is None
