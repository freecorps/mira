"""The review engine's four touch points with mira.verify, with the session stubbed."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from mira.config import MiraConfig
from mira.core.engine import ReviewEngine
from mira.llm.provider import LLMProvider
from mira.models import FindingVerification, ReviewComment, Severity
from tests.test_engine import _WALKTHROUGH_LLM_RESPONSE


class _StubSession:
    def __init__(self) -> None:
        self.paths: list[list[str]] = []
        self.verified: list[ReviewComment] = []
        self.closed = False

    async def prompt_block(self, paths: list[str]) -> str:
        self.paths.append(paths)
        return "\n\n## Tool output on changed lines\nSTUB-TOOL-BLOCK\n"

    async def verify(self, comments, llm, audit):  # noqa: ANN001
        self.verified = list(comments)
        comments[0].verification = FindingVerification(outcome="reproduced")
        audit.append({"stage": "verify_summary", "attempted": 1})
        # The second finding is dropped, as a not-reproduced plausible one would be.
        return [comments[0], *comments[2:]]

    def tool_findings(self, comments):  # noqa: ANN001
        return [
            ReviewComment(
                path="src/utils.py",
                line=3,
                end_line=None,
                severity=Severity.SUGGESTION,
                category="style",
                title="ruff F401",
                body="unused import",
                confidence=0.9,
                source_pass="tool",
            ),
            ReviewComment(
                path="not/reviewed.py",
                line=1,
                end_line=None,
                severity=Severity.SUGGESTION,
                category="style",
                title="elsewhere",
                body="x",
                confidence=0.9,
                source_pass="tool",
            ),
        ]

    async def aclose(self) -> None:
        self.closed = True


@pytest.fixture
def llm(sample_llm_response_text: str) -> LLMProvider:
    mock = MagicMock(spec=LLMProvider)
    mock.walkthrough = AsyncMock(return_value=_WALKTHROUGH_LLM_RESPONSE)
    mock.review = AsyncMock(return_value=sample_llm_response_text)
    mock.complete = AsyncMock(return_value=sample_llm_response_text)
    mock.count_tokens = MagicMock(return_value=100)
    mock.usage = {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}
    return mock


@pytest.mark.asyncio
async def test_engine_feeds_tools_verifies_and_closes(llm, sample_diff_text: str) -> None:
    stub = _StubSession()
    config = MiraConfig()
    config.review.self_critique = False
    config.review.agentic_tools = False
    with patch("mira.core.engine.VerifySession.maybe_start", return_value=stub) as start:
        engine = ReviewEngine(config=config, llm=llm)
        result = await engine.review_diff(sample_diff_text)

    # Started with the whole diff, and closed when the review ended.
    assert start.call_args.args[3] == sample_diff_text
    assert stub.closed
    # Every part's prompt carried its tool block.
    assert stub.paths
    prompts = [
        message["content"]
        for call in llm.review.call_args_list
        for message in call.args[0]
        if isinstance(message.get("content"), str)
    ]
    assert any("STUB-TOOL-BLOCK" in text for text in prompts)
    # Verification saw the critiqued drafts and its drop stuck.
    assert len(stub.verified) >= 2
    titles = [c.title for c in result.comments]
    assert stub.verified[1].title not in titles
    assert result.comments[0].verification is not None
    # Tool findings joined, only for files this review covered.
    assert "ruff F401" in titles and "elsewhere" not in titles
    assert any(e.get("stage") == "verify_summary" for e in result.audit)
    assert any(e.get("chunk") == "tools" for e in result.audit if e.get("stage") == "drafted")


@pytest.mark.asyncio
async def test_a_failing_session_never_costs_the_review(llm, sample_diff_text: str) -> None:
    stub = _StubSession()
    stub.verify = AsyncMock(side_effect=RuntimeError("sandbox exploded"))  # type: ignore[method-assign]
    config = MiraConfig()
    config.review.self_critique = False
    config.review.agentic_tools = False
    with patch("mira.core.engine.VerifySession.maybe_start", return_value=stub):
        result = await ReviewEngine(config=config, llm=llm).review_diff(sample_diff_text)
    assert result.comments
    assert stub.closed


def test_review_finding_carries_the_verification() -> None:
    from mira.core.engine import _build_review_finding
    from mira.models import PRInfo

    pr = PRInfo(
        title="t",
        description="",
        base_branch="main",
        head_branch="f",
        url="u",
        number=1,
        owner="o",
        repo="r",
    )
    comment = ReviewComment(
        path="a.py",
        line=1,
        end_line=None,
        severity=Severity.WARNING,
        category="bug",
        title="t",
        body="b",
        confidence=0.9,
    )
    assert _build_review_finding(pr, comment, prompt_model="m").verification == ""
    comment.verification = FindingVerification(outcome="not_reproduced", reason="passed")
    finding = _build_review_finding(pr, comment, prompt_model="m")
    assert (finding.verification, finding.verification_detail) == ("not_reproduced", "passed")


@pytest.mark.asyncio
async def test_self_critique_keeps_its_grade_on_the_comment() -> None:
    """Verification drops a plausible finding whose test passed; it needs the grade."""
    import json

    from mira.core.passes import self_critique

    def _c(title: str) -> ReviewComment:
        return ReviewComment(
            path="app.py",
            line=10,
            end_line=None,
            severity=Severity.WARNING,
            category="bug",
            title=title,
            body="body",
            confidence=0.9,
        )

    critic = MagicMock()
    critic.complete_with_tools = AsyncMock(
        return_value=json.dumps(
            {
                "verdicts": [
                    {"index": 0, "evidence": "proven", "reason": "shown"},
                    {"index": 1, "evidence": "plausible", "reason": "depends"},
                ]
            }
        )
    )
    kept = await self_critique(AsyncMock(), [_c("a"), _c("b")], critic_llm=critic)
    assert [(c.title, c.critique_grade) for c in kept] == [("a", "proven"), ("b", "plausible")]
