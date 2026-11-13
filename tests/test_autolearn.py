"""The automatic learning loop: guards, judge, acceptance, retraction, reset."""

from __future__ import annotations

import json
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from mira.config import MiraConfig
from mira.feedback import autolearn
from mira.feedback.models import ReviewFinding
from mira.feedback.provenance import finding_marker
from mira.feedback.retrieval import AUTO_ACTOR, is_expired_auto_rule, retrieve_rules
from mira.index.store import IndexStore, reset_learning_org_wide_sqlite
from mira.models import PRInfo
from mira.platforms.handlers import run_thread_reply

_DIFF = (
    "diff --git a/src/app.py b/src/app.py\n"
    "--- a/src/app.py\n"
    "+++ b/src/app.py\n"
    "@@ -9,1 +9,1 @@\n"
    "-return fallback\n"
    "+return expected\n"
)


def _finding(finding_id: str) -> ReviewFinding:
    return ReviewFinding(
        id=finding_id,
        fingerprint="fingerprint",
        review_id=0,
        platform="github",
        owner="acme",
        repo="app",
        pr_number=7,
        pr_url="https://github.com/acme/app/pull/7",
        base_sha="base123",
        head_sha="head123",
        path="src/app.py",
        start_line=10,
        end_line=10,
        symbol="run",
        category="bug",
        severity="warning",
        confidence=0.92,
        title="Divisor may be zero",
        body="`b` can be zero here.",
        suggestion="",
        detector="main",
        prompt_model="test-model",
    )


def _setup(tmp_path, monkeypatch, finding_id: str, *, head_sha: str = "head123"):
    monkeypatch.setenv("MIRA_INDEX_DIR", str(tmp_path))
    monkeypatch.delenv("DATABASE_URL", raising=False)
    pr_info = PRInfo(
        title="PR",
        description="",
        base_branch="main",
        head_branch="feature",
        url="https://github.com/acme/app/pull/7",
        number=7,
        owner="acme",
        repo="app",
        base_sha="base123",
        head_sha=head_sha,
    )
    finding = _finding(finding_id)
    store = IndexStore.open("acme", "app")
    store.save_review_finding(finding)
    store.update_review_finding_posted(finding.id, 123)
    store.close()
    provider = AsyncMock()
    provider.get_pr_diff = AsyncMock(return_value=_DIFF)
    provider.get_compare_diff = AsyncMock(return_value="")
    provider.reply_to_review_comment = AsyncMock()
    provider.resolve_threads = AsyncMock(return_value=1)
    return pr_info, finding, provider


def _kwargs(finding: ReviewFinding, event: int) -> dict:
    return {
        "original_suggestion": finding_marker(finding.id),
        "thread_id": "thread-1",
        "comment_path": finding.path,
        "comment_line": finding.start_line,
        "actor": "alice",
        "parent_comment_id": 123,
        "source_event_id": f"review-comment:{event}",
    }


def _llm(*payloads: dict) -> AsyncMock:
    llm = AsyncMock()
    llm.complete_with_tools = AsyncMock(side_effect=[json.dumps(item) for item in payloads])
    return llm


_DISAGREE = {"intent": "disagreement", "reply": "Got it."}
_PREMISE_WRONG = {
    "verdict": "premise_wrong",
    "reason": "The caller validates b before calling run.",
    "rule": "Do not flag division by a parameter the only caller validates as non-zero.",
    "scope_type": "path",
    "confidence": 0.93,
    "reply": "You're right: the caller rejects b == 0 before this runs.",
}


async def _reply(provider, pr_info, text: str, event: int, finding, llm, config=None) -> str:
    with (
        patch("mira.platforms.handlers.load_config", return_value=config or MiraConfig()),
        patch("mira.platforms.handlers.create_llm", return_value=llm),
        patch("mira.platforms.handlers.llm_config_for", return_value=MagicMock()),
    ):
        await run_thread_reply(provider, pr_info, text, event, **_kwargs(finding, event))
    return provider.reply_to_review_comment.await_args.args[2]


@pytest.mark.asyncio
async def test_a_confident_premise_wrong_verdict_is_learned_automatically(
    tmp_path, monkeypatch
) -> None:
    pr_info, finding, provider = _setup(
        tmp_path, monkeypatch, "00000000-0000-4000-8000-0000000000a1"
    )
    llm = _llm(_DISAGREE, _PREMISE_WRONG)
    reply = await _reply(
        provider, pr_info, "No, the caller validates b already.", 456, finding, llm
    )

    assert "the caller rejects b == 0" in reply
    assert "Learned rule #" in reply
    provider.resolve_threads.assert_awaited_once_with(pr_info, ["thread-1"])
    store = IndexStore.open("acme", "app")
    rules = [rule for rule in store.list_learned_rules("approved") if rule.active]
    assert [rule.created_by for rule in rules] == [AUTO_ACTOR]
    assert rules[0].rule_text == _PREMISE_WRONG["rule"]
    assert store.list_learning_candidates(status="pending") == []
    assert store.get_review_finding(finding.id).state == "dismissed"  # type: ignore[union-attr]
    store.close()


@pytest.mark.asyncio
async def test_a_reply_pointing_at_the_fix_never_becomes_a_false_positive(
    tmp_path, monkeypatch
) -> None:
    """The poison the old queue was full of: a fixed finding recorded as wrong."""
    pr_info, finding, provider = _setup(
        tmp_path, monkeypatch, "00000000-0000-4000-8000-0000000000a2"
    )
    llm = _llm(_DISAGREE, _PREMISE_WRONG)
    reply = await _reply(
        provider,
        pr_info,
        "Não procede, foi corrigido no último commit, está na linha 120.",
        457,
        finding,
        llm,
    )

    assert "falso positivo" in reply and "corrigido" in reply
    store = IndexStore.open("acme", "app")
    assert store.list_learning_candidates() == []
    assert store.list_learned_rules() == []
    assert store.get_review_finding(finding.id).state == "fixed"  # type: ignore[union-attr]
    kinds = {event.kind for event in store.list_feedback_v2(finding_id=finding.id)}
    assert "fixed" in kinds
    store.close()


@pytest.mark.asyncio
async def test_a_change_near_the_finding_vetoes_learning(tmp_path, monkeypatch) -> None:
    pr_info, finding, provider = _setup(
        tmp_path, monkeypatch, "00000000-0000-4000-8000-0000000000a3", head_sha="head456"
    )
    provider.get_compare_diff = AsyncMock(
        return_value=(
            "diff --git a/src/app.py b/src/app.py\n"
            "--- a/src/app.py\n"
            "+++ b/src/app.py\n"
            "@@ -8,3 +8,4 @@\n"
            " def run(a, b):\n"
            "+    if b == 0: raise ValueError\n"
            "     return a / b\n"
            " \n"
        )
    )
    llm = _llm(_DISAGREE, _PREMISE_WRONG)
    await _reply(provider, pr_info, "This was never a problem.", 458, finding, llm)

    provider.get_compare_diff.assert_awaited_with(pr_info, "head123", "head456")
    judge_prompt = llm.complete_with_tools.await_args_list[1].kwargs["messages"][0]["content"]
    assert "if b == 0" in judge_prompt
    store = IndexStore.open("acme", "app")
    assert store.list_learned_rules() == []
    store.close()


@pytest.mark.asyncio
async def test_accepted_risk_closes_the_thread_without_a_rule(tmp_path, monkeypatch) -> None:
    pr_info, finding, provider = _setup(
        tmp_path, monkeypatch, "00000000-0000-4000-8000-0000000000a4"
    )
    llm = _llm(
        _DISAGREE,
        {
            "verdict": "accepted_risk",
            "reason": "The runtime cannot do DNS pinning.",
            "confidence": 0.9,
            "reply": "Entendi, fica como risco aceito.",
        },
    )
    reply = await _reply(
        provider, pr_info, "Não dá pra corrigir no runtime do Convex.", 459, finding, llm
    )

    assert reply == "Entendi, fica como risco aceito."
    provider.resolve_threads.assert_awaited_once()
    store = IndexStore.open("acme", "app")
    assert store.list_learning_candidates() == []
    store.close()


@pytest.mark.asyncio
async def test_a_correction_undoes_what_was_learned(tmp_path, monkeypatch) -> None:
    pr_info, finding, provider = _setup(
        tmp_path, monkeypatch, "00000000-0000-4000-8000-0000000000a5"
    )
    await _reply(
        provider,
        pr_info,
        "No, the caller validates b already.",
        460,
        finding,
        _llm(_DISAGREE, _PREMISE_WRONG),
    )
    provider.resolve_threads.reset_mock()

    reply = await _reply(
        provider,
        pr_info,
        "Correção: isto NÃO foi falso positivo, descarte o candidato.",
        461,
        finding,
        _llm(_DISAGREE),
    )

    assert "desativei a regra #" in reply
    provider.resolve_threads.assert_not_awaited()
    store = IndexStore.open("acme", "app")
    assert [rule for rule in store.list_learned_rules() if rule.active] == []
    assert [c.status for c in store.list_learning_candidates()] == ["retracted"]
    store.close()


@pytest.mark.asyncio
async def test_queue_mode_still_waits_for_an_admin(tmp_path, monkeypatch) -> None:
    pr_info, finding, provider = _setup(
        tmp_path, monkeypatch, "00000000-0000-4000-8000-0000000000a6"
    )
    config = MiraConfig()
    config.learning.auto_accept = False
    await _reply(
        provider,
        pr_info,
        "False positive.",
        462,
        finding,
        _llm({**_DISAGREE, "learning": {"rule": "x rule", "confidence": 0.5}}),
        config,
    )
    store = IndexStore.open("acme", "app")
    assert len(store.list_learning_candidates(status="pending")) == 1
    assert store.list_learned_rules() == []
    store.close()


def test_region_check_uses_the_finding_side_of_the_diff() -> None:
    finding = _finding("f")
    far = "--- a/src/app.py\n+++ b/src/app.py\n@@ -200,1 +200,1 @@\n-a\n+b\n"
    near = "--- a/src/app.py\n+++ b/src/app.py\n@@ -20,1 +20,1 @@\n-a\n+b\n"
    other = "--- a/src/other.py\n+++ b/src/other.py\n@@ -10,1 +10,1 @@\n-a\n+b\n"
    assert autolearn.region_changed_in_diff(far, finding)[0] is False
    assert autolearn.region_changed_in_diff(near, finding)[0] is True
    assert autolearn.region_changed_in_diff(other, finding) == (False, "")


@pytest.mark.parametrize(
    ("text", "claim"),
    [
        ("Corrigido no último commit.", True),
        ("Fixed in a1b2c3d.", True),
        ("Addressed below.", True),
        ("Isso já é tratado pelo middleware.", False),
        ("The caller already handles it.", False),
    ],
)
def test_fix_claims_are_narrow(text: str, claim: bool) -> None:
    assert autolearn.fix_claimed(text) is claim


def test_auto_rules_expire_unless_reinforced(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("MIRA_INDEX_DIR", str(tmp_path))
    store = IndexStore.open("acme", "app")
    auto = store.create_learned_rule(
        rule_text="auto",
        category="bug",
        path_pattern="",
        status="approved",
        created_by=AUTO_ACTOR,
        scope_type="repo",
        scope_value="acme/app",
    )
    manual = store.create_learned_rule(
        rule_text="manual",
        category="bug",
        path_pattern="",
        status="approved",
        created_by="admin",
        scope_type="repo",
        scope_value="acme/app",
    )
    later = time.time() + 61 * 86400
    assert is_expired_auto_rule(auto, 60, later)
    assert not is_expired_auto_rule(manual, 60, later)
    kept = retrieve_rules(store, paths=["x.py"], languages=[], auto_rule_ttl_days=60, now=later)
    assert [rule.id for rule in kept] == [manual.id]
    store.close()


def test_budget_disables_the_stalest_auto_rules(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("MIRA_INDEX_DIR", str(tmp_path))
    store = IndexStore.open("acme", "app")
    ids = [
        store.create_learned_rule(
            rule_text=f"auto {index}",
            category="bug",
            path_pattern="",
            status="approved",
            created_by=AUTO_ACTOR,
            scope_type="repo",
            scope_value="acme/app",
        ).id
        for index in range(3)
    ]
    assert autolearn.enforce_auto_rule_budget(store, 2) == [ids[0]]
    assert sorted(r.id for r in store.list_learned_rules("approved") if r.active) == ids[1:]
    store.close()


@pytest.mark.asyncio
async def test_reset_starts_learning_from_zero(tmp_path, monkeypatch) -> None:
    pr_info, finding, provider = _setup(
        tmp_path, monkeypatch, "00000000-0000-4000-8000-0000000000a7"
    )
    await _reply(
        provider,
        pr_info,
        "No, the caller validates b already.",
        463,
        finding,
        _llm(_DISAGREE, _PREMISE_WRONG),
    )
    store = IndexStore.open("acme", "app")
    manual = store.create_learned_rule(
        rule_text="manual",
        category="bug",
        path_pattern="",
        status="approved",
        created_by="admin",
        scope_type="repo",
        scope_value="acme/app",
    )
    store.close()

    totals = reset_learning_org_wide_sqlite()

    assert totals == {"candidates_deleted": 1, "rules_archived": 1}
    store = IndexStore.open("acme", "app")
    assert store.list_learning_candidates() == []
    active = [rule.id for rule in store.list_learned_rules() if rule.active]
    assert active == [manual.id]
    store.close()

    # A fresh rule learns cleanly after the reset, with no identity collision.
    provider.reply_to_review_comment.reset_mock()
    await _reply(
        provider,
        pr_info,
        "No, the caller validates b already.",
        464,
        finding,
        _llm(_DISAGREE, _PREMISE_WRONG),
    )
    assert "Learned rule #" in provider.reply_to_review_comment.await_args.args[2]


def test_reset_skips_a_database_it_cannot_read(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("MIRA_INDEX_DIR", str(tmp_path))
    store = IndexStore.open("acme", "app")
    store.create_learned_rule(
        rule_text="learned",
        category="bug",
        source_signal="feedback_v2",
        status="approved",
        scope_type="repo",
        scope_value="acme/app",
    )
    store.close()
    broken = tmp_path / "acme" / "broken.db"
    broken.write_bytes(b"not a database")

    assert reset_learning_org_wide_sqlite()["rules_archived"] == 1


@pytest.mark.asyncio
async def test_a_reply_sees_the_file_at_head_around_the_line_it_cites(
    tmp_path, monkeypatch
) -> None:
    """28% of thread replies said the code was not visible; this is that code."""
    pr_info, finding, provider = _setup(
        tmp_path, monkeypatch, "00000000-0000-4000-8000-0000000000b1", head_sha="head456"
    )
    head_file = "\n".join(f"line {number}" for number in range(1, 301))
    provider.get_file_content = AsyncMock(return_value=head_file)
    provider.get_compare_diff = AsyncMock(
        return_value="--- a/src/other.py\n+++ b/src/other.py\n@@ -1,1 +1,1 @@\n-a\n+b\n"
    )
    llm = _llm(
        {"intent": "fixed", "reply": "Checking."},
        {"resolved": True, "reply": "Confirmed at line 250."},
    )
    with (
        patch("mira.platforms.handlers.load_config", return_value=MiraConfig()),
        patch("mira.platforms.handlers.create_llm", return_value=llm),
        patch("mira.platforms.handlers.llm_config_for", return_value=MagicMock()) as purpose,
    ):
        await run_thread_reply(
            provider,
            pr_info,
            "Corrigido, está na linha 250 agora.",
            470,
            **_kwargs(finding, 470),
        )

    assert purpose.call_args_list[0].args[0] == "review"
    provider.get_file_content.assert_awaited_with(pr_info, "src/app.py", "head456")
    classifier = llm.complete_with_tools.await_args_list[0].kwargs["messages"][0]["content"]
    recheck = llm.complete_with_tools.await_args_list[1].kwargs["messages"][0]["content"]
    for prompt in (classifier, recheck):
        assert "250| line 250" in prompt
        assert " 10| line 10" in prompt
        assert "has not changed since the comment" in prompt


@pytest.mark.asyncio
async def test_a_reply_is_read_against_the_thread_so_far(tmp_path, monkeypatch) -> None:
    pr_info, finding, provider = _setup(
        tmp_path, monkeypatch, "00000000-0000-4000-8000-0000000000b2"
    )
    await _reply(
        provider,
        pr_info,
        "Fixed.",
        471,
        finding,
        _llm(
            {"intent": "fixed", "reply": "Checking."},
            {"resolved": False, "reply": "The visible code still divides by b."},
        ),
    )
    llm = _llm(
        {"intent": "fixed", "reply": "Checking."},
        {"resolved": True, "reply": "Confirmed."},
    )
    await _reply(provider, pr_info, "It's guarded in the caller now.", 472, finding, llm)

    classifier = llm.complete_with_tools.await_args_list[0].kwargs["messages"][0]["content"]
    assert "Earlier in this thread" in classifier
    assert "human (@alice): Fixed." in classifier
    assert "mira: The visible code still divides by b." in classifier


def test_only_a_hunk_near_the_line_is_shown() -> None:
    import asyncio

    from mira.platforms.handlers import _pr_hunk

    provider = AsyncMock()
    provider.get_pr_diff = AsyncMock(
        return_value=(
            "diff --git a/src/app.py b/src/app.py\n--- a/src/app.py\n+++ b/src/app.py\n"
            "@@ -1,1 +1,1 @@\n-a\n+b\n"
        )
    )
    assert asyncio.run(_pr_hunk(provider, MagicMock(), "src/app.py", 400)) == ""
    assert "+b" in asyncio.run(_pr_hunk(provider, MagicMock(), "src/app.py", 5))
