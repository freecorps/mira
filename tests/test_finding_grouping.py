"""Grouped findings and the comment cap that lists, rather than drops, overflow."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from mira.config import FilterConfig, MiraConfig
from mira.core.commit_status import finished_status
from mira.core.engine import ReviewEngine, _drop_unanchorable_comments, _finding_records
from mira.core.grouping import (
    cap_comments,
    format_also_at,
    location_url,
    prune_group,
    render_overflow_section,
)
from mira.core.noise_filter import drop_already_posted, filter_noise
from mira.core.verdict import APPROVE, REQUEST_CHANGES, decide_verdict
from mira.feedback.provenance import finding_marker, parse_finding_id
from mira.index.store import IndexStore
from mira.llm.provider import LLMProvider
from mira.models import (
    PRInfo,
    ReviewComment,
    ReviewResult,
    Severity,
    UnresolvedThread,
    WalkthroughResult,
)
from mira.providers.formatting import format_comment_body

PR = PRInfo(
    title="t",
    description="",
    base_branch="main",
    head_branch="feature",
    url="https://github.com/acme/app/pull/7",
    number=7,
    owner="acme",
    repo="app",
    base_sha="base000",
    head_sha="head111",
    author="dev",
)


def _c(
    path: str = "a.py",
    line: int = 10,
    *,
    severity: Severity = Severity.SUGGESTION,
    category: str = "bug",
    title: str = "Missing await on save_user call",
    body: str = "The coroutine returned by save_user is never awaited so the write is lost.",
    confidence: float = 0.9,
) -> ReviewComment:
    return ReviewComment(
        path=path,
        line=line,
        end_line=None,
        severity=severity,
        category=category,
        title=title,
        body=body,
        confidence=confidence,
    )


class TestGrouping:
    def test_same_problem_in_other_files_is_grouped_not_dropped(self):
        comments = [_c("a.py", 10, confidence=0.95), _c("b.py", 3), _c("c.py", 40)]
        result = filter_noise(comments, FilterConfig(confidence_threshold=0.0), group_findings=True)
        assert len(result) == 1
        assert result[0].path == "a.py"
        assert [(o.path, o.line) for o in result[0].occurrences] == [("b.py", 3), ("c.py", 40)]

    def test_without_grouping_the_old_dedupe_still_drops(self):
        comments = [_c("a.py", 10), _c("b.py", 3)]
        result = filter_noise(comments, FilterConfig(confidence_threshold=0.0))
        assert len(result) == 1
        assert result[0].occurrences == []

    def test_strongest_occurrence_carries_the_group(self):
        comments = [_c("a.py", 10, confidence=0.75), _c("b.py", 3, confidence=0.99)]
        result = filter_noise(comments, FilterConfig(confidence_threshold=0.0), group_findings=True)
        assert result[0].path == "b.py"
        assert [o.path for o in result[0].occurrences] == ["a.py"]

    def test_the_same_location_twice_is_still_a_duplicate(self):
        comments = [_c("a.py", 10), _c("a.py", 10)]
        result = filter_noise(comments, FilterConfig(confidence_threshold=0.0), group_findings=True)
        assert len(result) == 1
        assert result[0].occurrences == []

    def test_different_categories_are_not_one_root_cause(self):
        comments = [_c("a.py", 10, category="bug"), _c("b.py", 3, category="style")]
        result = filter_noise(comments, FilterConfig(confidence_threshold=0.0), group_findings=True)
        assert len(result) == 1
        assert result[0].occurrences == []

    def test_unrelated_findings_stay_apart(self):
        comments = [
            _c("a.py", 10),
            _c("b.py", 3, title="SQL built from user input", body="Use bound parameters."),
        ]
        result = filter_noise(comments, FilterConfig(confidence_threshold=0.0), group_findings=True)
        assert len(result) == 2
        assert all(not c.occurrences for c in result)


class TestCap:
    def test_blockers_are_never_capped(self):
        comments = [_c(f"f{i}.py", severity=Severity.BLOCKER, title=f"b{i}") for i in range(4)]
        posted, overflow = cap_comments(comments, 1, exempt_severity=Severity.BLOCKER)
        assert len(posted) == 4
        assert overflow == []

    def test_warnings_overflow_when_only_blockers_are_exempt(self):
        comments = [
            _c("a.py", severity=Severity.WARNING, confidence=0.8, title="w1"),
            _c("b.py", severity=Severity.WARNING, confidence=0.95, title="w2"),
            _c("c.py", severity=Severity.BLOCKER, confidence=0.7, title="b"),
        ]
        posted, overflow = cap_comments(comments, 1, exempt_severity=Severity.BLOCKER)
        assert [c.title for c in posted] == ["b", "w2"]
        assert [c.title for c in overflow] == ["w1"]

    def test_ranked_by_severity_then_confidence(self):
        comments = [
            _c("a.py", severity=Severity.NITPICK, confidence=0.99, title="nit"),
            _c("b.py", severity=Severity.SUGGESTION, confidence=0.71, title="low"),
            _c("c.py", severity=Severity.SUGGESTION, confidence=0.9, title="high"),
        ]
        posted, overflow = cap_comments(comments, 2)
        assert [c.title for c in posted] == ["high", "low"]
        assert [c.title for c in overflow] == ["nit"]

    def test_default_keeps_every_warning_inline(self):
        comments = [_c(f"f{i}.py", severity=Severity.WARNING, title=f"w{i}") for i in range(7)]
        posted, overflow = cap_comments(comments, 2)
        assert len(posted) == 7 and overflow == []

    def test_a_group_spends_one_slot(self):
        grouped = _c("a.py")
        grouped.occurrences = [_c("b.py"), _c("c.py")]
        posted, overflow = cap_comments([grouped, _c("d.py", title="other")], 2)
        assert len(posted) == 2 and overflow == []


class TestPruning:
    def test_next_occurrence_is_promoted_when_the_first_is_rejected(self):
        group = _c("a.py", 10)
        group.occurrences = [_c("b.py", 3), _c("c.py", 40)]
        survivor = prune_group(group, lambda c: c.path != "a.py")
        assert survivor is not None
        assert survivor.path == "b.py"
        assert [o.path for o in survivor.occurrences] == ["c.py"]

    def test_already_posted_location_does_not_hide_the_others(self):
        group = _c("a.py", 10)
        group.occurrences = [_c("b.py", 3)]
        open_thread = UnresolvedThread(
            thread_id="t1", path="a.py", line=10, body="", is_outdated=False
        )
        kept = drop_already_posted([group], [open_thread])
        assert [(c.path, c.line) for c in kept] == [("b.py", 3)]

    def test_occurrences_outside_the_diff_are_dropped(self):
        group = _c("a.py", 10)
        group.occurrences = [_c("a.py", 500), _c("b.py", 3)]
        kept, dropped = _drop_unanchorable_comments([group], {"a.py": {10}, "b.py": {3}})
        assert len(kept) == 1
        assert [(o.path, o.line) for o in kept[0].occurrences] == [("b.py", 3)]
        assert [(c.path, c.line) for c in dropped] == [("a.py", 500)]


class TestLinks:
    @pytest.mark.parametrize(
        ("pr_url", "expected"),
        [
            ("https://github.com/acme/app/pull/7", "https://github.com/acme/app/blob/s/x/y.py#L4"),
            (
                "https://gitlab.com/g/app/-/merge_requests/7",
                "https://gitlab.com/g/app/-/blob/s/x/y.py#L4",
            ),
            (
                "https://code.example/acme/app/pulls/7",
                "https://code.example/acme/app/src/commit/s/x/y.py#L4",
            ),
            ("https://example.com/whatever", ""),
        ],
    )
    def test_location_url_per_platform(self, pr_url: str, expected: str):
        assert location_url(pr_url, "s", "x/y.py", 4) == expected

    def test_also_at_lists_linked_locations(self):
        group = _c("a.py")
        group.occurrences = [_c("b.py", 3)]
        block = format_also_at(group, PR.url, PR.head_sha)
        assert "**Also at:**" in block
        assert "[`b.py:3`](https://github.com/acme/app/blob/head111/b.py#L3)" in block

    def test_comment_carries_the_primary_marker_and_the_list(self):
        group = _c("a.py")
        group.finding_id = "11111111-2222-3333-4444-555555555555"
        other = _c("b.py", 3)
        other.finding_id = "99999999-2222-3333-4444-555555555555"
        group.occurrences = [other]
        body = format_comment_body(group, pr_info=PR)
        assert finding_marker(group.finding_id) in body
        assert parse_finding_id(body) == group.finding_id
        assert other.finding_id not in body
        assert "`b.py:3`" in body

    def test_a_lone_finding_has_no_list(self):
        assert "Also at" not in format_comment_body(_c(), pr_info=PR)


class TestOverflowListing:
    def test_section_lists_severity_location_and_title(self):
        hidden = _c("z.py", 8, title="Prefer a set for membership tests")
        hidden.occurrences = [_c("y.py", 1)]
        section = render_overflow_section([hidden], pr_url=PR.url, sha="head111", max_comments=5)
        assert section.startswith("<details>")
        assert "More findings (1)" in section
        assert "**suggestion**" in section
        assert "[`z.py:8`](https://github.com/acme/app/blob/head111/z.py#L8)" in section
        assert "Prefer a set for membership tests" in section
        assert "also at 1 other location" in section

    def test_walkthrough_renders_it(self):
        md = WalkthroughResult(summary="s").to_markdown(
            overflow=[_c("z.py", 8, title="Hidden one")], pr_url=PR.url, head_sha="head111"
        )
        assert "More findings (1)" in md and "Hidden one" in md

    def test_walkthrough_without_overflow_has_no_section(self):
        assert "More findings" not in WalkthroughResult(summary="s").to_markdown()


class TestDecisionsSeeHiddenFindings:
    def _config(self, mode: str = "approve") -> MiraConfig:
        config = MiraConfig()
        config.review.verdict.mode = mode
        config.review.verdict.approve_min_confidence = 0
        return config

    def test_hidden_warning_blocks_an_approval(self):
        result = ReviewResult(
            comments=[_c(severity=Severity.SUGGESTION)],
            overflow_comments=[_c("b.py", severity=Severity.WARNING, title="hidden")],
            reviewed_files=2,
        )
        assert decide_verdict(result, self._config(), PR, "mira") is None

    def test_hidden_warning_is_counted_when_requesting_changes(self):
        result = ReviewResult(
            overflow_comments=[_c("b.py", severity=Severity.WARNING, title="hidden")],
            reviewed_files=2,
        )
        verdict = decide_verdict(result, self._config("request_changes"), PR, "mira")
        assert verdict is not None and verdict.event == REQUEST_CHANGES
        assert "1 warning" in verdict.body and "More findings" in verdict.body

    def test_approval_counts_listed_notes(self):
        result = ReviewResult(
            comments=[_c(severity=Severity.SUGGESTION)],
            overflow_comments=[_c("b.py", severity=Severity.NITPICK, title="n")],
            reviewed_files=2,
        )
        verdict = decide_verdict(result, self._config(), PR, "mira")
        assert verdict is not None and verdict.event == APPROVE
        assert "2 non-blocking notes" in verdict.body

    def test_status_counts_overflow(self):
        config = MiraConfig()
        config.review.status.fail_on = "above_ceiling"
        result = ReviewResult(
            overflow_comments=[_c("b.py", severity=Severity.WARNING)], reviewed_files=1
        )
        status = finished_status(result, config)
        assert status.state == "failure"
        assert "listed in the walkthrough" in status.summary


class TestProvenance:
    def test_every_finding_is_recorded_with_its_presentation(self):
        posted = _c("a.py")
        posted.occurrences = [_c("b.py", 3)]
        hidden = _c("z.py", 8, title="other")
        result = ReviewResult(comments=[posted], overflow_comments=[hidden])
        records = _finding_records(PR, result, prompt_model="m")
        by_path = {r.path: r for r in records}
        assert by_path["a.py"].presentation == "inline"
        assert by_path["b.py"].presentation == "grouped"
        assert by_path["b.py"].primary_finding_id == by_path["a.py"].id
        assert by_path["z.py"].presentation == "summary"
        assert by_path["z.py"].primary_finding_id == ""
        # The ids are written back, so the posted marker names the primary.
        assert posted.finding_id == by_path["a.py"].id

    def test_store_round_trip_and_state_follows_the_posted_comment(self):
        posted = _c("a.py")
        posted.occurrences = [_c("b.py", 3)]
        result = ReviewResult(comments=[posted], overflow_comments=[_c("z.py", title="x")])
        store = IndexStore.open("acme", "app")
        try:
            records = _finding_records(PR, result, prompt_model="m")
            for record in records:
                store.save_review_finding(record)
            primary, grouped, summary = records
            loaded = store.get_review_finding(grouped.id)
            assert loaded is not None
            assert loaded.presentation == "grouped"
            assert loaded.primary_finding_id == primary.id

            store.update_review_finding_state(primary.id, "dismissed")
            assert store.get_review_finding(grouped.id).state == "dismissed"
            # An overflowed finding is not grouped under anything and keeps its state.
            assert store.get_review_finding(summary.id).state == "open"
        finally:
            store.close()


def _llm(comments: list[dict]) -> LLMProvider:
    response = json.dumps({"comments": comments, "summary": "s", "key_issues": []})
    llm = MagicMock(spec=LLMProvider)
    llm.walkthrough = AsyncMock(
        return_value=json.dumps({"summary": "w", "change_groups": [], "sequence_diagram": None})
    )
    llm.review = AsyncMock(return_value=response)
    llm.complete = AsyncMock(return_value=response)
    llm.count_tokens = MagicMock(return_value=100)
    llm.usage = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
    return llm


def _draft(
    path: str,
    line: int,
    title: str,
    body: str,
    confidence: float = 0.9,
    severity: str = "suggestion",
) -> dict:
    return {
        "path": path,
        "line": line,
        "end_line": None,
        "severity": severity,
        "category": "maintainability",
        "title": title,
        "body": body,
        "confidence": confidence,
        "existing_code": "",
    }


@pytest.mark.asyncio
async def test_engine_groups_then_overflows_instead_of_dropping(sample_diff_text: str):
    config = MiraConfig()
    config.filter.max_comments = 1
    config.review.self_critique = False
    config.review.security_pass = False
    config.review.include_summary = False
    repeated = ("Name helper duplicated inline", "Use the shared format_name helper here.")
    llm = _llm(
        [
            _draft("src/utils.py", 9, *repeated, confidence=0.95),
            _draft("src/main.py", 14, *repeated, confidence=0.8),
            _draft("src/utils.py", 16, "Config parser accepts any file", "Validate the path."),
        ]
    )
    result = await ReviewEngine(config=config, llm=llm).review_diff(sample_diff_text)

    assert len(result.comments) == 1
    assert result.comments[0].title == repeated[0]
    assert [(o.path, o.line) for o in result.comments[0].occurrences] == [("src/main.py", 14)]
    assert [c.title for c in result.overflow_comments] == ["Config parser accepts any file"]
    assert any(e.get("stage") == "overflow" for e in result.audit)


@pytest.mark.asyncio
async def test_review_pr_posts_the_cap_lists_the_rest_and_records_all(sample_diff_text: str):
    config = MiraConfig()
    config.filter.max_comments = 1
    # Let warnings overflow too, so the verdict has a hidden one to answer for.
    config.review.cap_exempt_severity = "blocker"
    config.review.self_critique = False
    config.review.security_pass = False
    config.review.include_summary = False
    llm = _llm(
        [
            _draft("src/utils.py", 9, "Shell helper is untested", "Add a test.", 0.95, "warning"),
            _draft(
                "src/utils.py", 16, "Config parser accepts any file", "Validate it.", 0.9, "warning"
            ),
        ]
    )
    provider = AsyncMock()
    provider.get_pr_info.return_value = PR
    provider.get_pr_diff.return_value = sample_diff_text
    provider.post_review = AsyncMock(return_value=[101])
    provider.post_comment = AsyncMock()
    provider.find_bot_comment = AsyncMock(return_value=None)
    provider.update_comment = AsyncMock()
    provider.get_unresolved_bot_threads = AsyncMock(return_value=[])
    provider.get_review_states = AsyncMock(return_value={})
    provider.submit_verdict = AsyncMock(return_value=True)
    provider.get_repo_tree = AsyncMock(return_value=[])
    provider.get_file_content = AsyncMock(side_effect=RuntimeError("404"))

    result = await ReviewEngine(config=config, llm=llm, provider=provider).review_pr(PR.url)

    posted = provider.post_review.call_args.args[1]
    assert [c.title for c in posted.comments] == ["Shell helper is untested"]
    walkthroughs = [
        call.args[-1]
        for call in (*provider.post_comment.call_args_list, *provider.update_comment.call_args_list)
    ]
    assert any("More findings (1)" in md and "Config parser" in md for md in walkthroughs)

    store = IndexStore.open("acme", "app")
    try:
        hidden = store.get_review_finding(result.overflow_comments[0].finding_id)
        shown = store.get_review_finding(result.comments[0].finding_id)
    finally:
        store.close()
    assert hidden is not None and hidden.presentation == "summary"
    assert shown is not None and shown.platform_comment_id == "101"
    # Two warnings, one of them hidden: the default `approve` verdict stays quiet.
    provider.submit_verdict.assert_not_called()

    # `.mira/REVIEW.md` was looked for at the base, never at the head.
    refs = {
        call.args[2]
        for call in provider.get_file_content.call_args_list
        if call.args[1].endswith(".mira/REVIEW.md")
    }
    assert refs == {PR.base_sha}
