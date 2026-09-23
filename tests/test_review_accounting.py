"""What a review reports about itself: its token count and its closing log line."""

from __future__ import annotations

import logging
from unittest.mock import AsyncMock, MagicMock

from mira.config import MiraConfig
from mira.core.engine import ReviewEngine
from mira.models import ReviewResult


def _client(total: int) -> MagicMock:
    client = MagicMock()
    client.usage = {"prompt_tokens": total - 10, "completion_tokens": 10, "total_tokens": total}
    return client


def test_usage_counts_every_model_the_review_called():
    review, indexing, security = _client(1000), _client(200), _client(30)
    engine = ReviewEngine(
        config=MiraConfig(), llm=review, indexing_llm=indexing, security_llm=security
    )
    assert engine._usage() == {
        "prompt_tokens": 1200,
        "completion_tokens": 30,
        "total_tokens": 1230,
    }


def test_a_client_serving_two_purposes_is_counted_once():
    review = _client(500)
    engine = ReviewEngine(config=MiraConfig(), llm=review)
    assert engine._usage()["total_tokens"] == 500


def test_a_client_without_usage_is_skipped():
    review = _client(500)
    engine = ReviewEngine(config=MiraConfig(), llm=review, indexing_llm=MagicMock(spec=[]))
    assert engine._usage()["total_tokens"] == 500


async def test_review_complete_is_logged_inside_the_trace(caplog):
    engine = ReviewEngine(config=MiraConfig(), llm=_client(0))
    result = ReviewResult(token_usage={"total_tokens": 42})
    engine._review_pr_traced = AsyncMock(return_value=result)  # type: ignore[method-assign]

    with caplog.at_level(logging.INFO, logger="mira.core.engine"):
        await engine.review_pr("https://github.com/o/r/pull/1")

    starting = next(r for r in caplog.records if r.getMessage().startswith("Review starting"))
    complete = next(r for r in caplog.records if r.getMessage().startswith("Review complete"))
    assert "42 tokens" in complete.getMessage()
    trace = starting.getMessage().rsplit("(trace ", 1)[1].rstrip(")")
    assert f"(trace {trace})" in complete.getMessage()
