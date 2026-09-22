"""The review prompt is sized against the client's output budget, not the file's."""

from __future__ import annotations

from unittest.mock import MagicMock

from mira.config import LLMConfig, MiraConfig
from mira.core.engine import ReviewEngine


def _engine(file_budget: int, client_config: LLMConfig | None) -> ReviewEngine:
    config = MiraConfig()
    config.llm.max_tokens = file_budget
    llm = MagicMock()
    if client_config is not None:
        llm.config = client_config
    return ReviewEngine(config=config, llm=llm)


def test_a_dashboard_budget_above_the_file_is_reserved():
    engine = _engine(4096, LLMConfig(model="m", max_tokens=16384))
    assert engine._output_reserve() == 16384


def test_unlimited_reserves_the_models_cap_within_a_quarter_of_the_context():
    engine = _engine(4096, LLMConfig(model="glm-5.3-flash", max_tokens=0))
    # The registry says 131072; a quarter of the 120k window is the ceiling.
    assert engine._output_reserve() == 120_000 // 4


def test_unlimited_on_an_unknown_model_reserves_a_sane_default():
    engine = _engine(4096, LLMConfig(model="unknown/model", max_tokens=0))
    assert engine._output_reserve() == 16384


def test_a_client_without_a_config_falls_back_to_the_file():
    llm = MagicMock(spec=["review", "count_tokens"])
    config = MiraConfig()
    config.llm.max_tokens = 2048
    assert ReviewEngine(config=config, llm=llm)._output_reserve() == 2048
