"""Where a local review is allowed to send the repository's code.

The property under test is narrow and absolute: the endpoint, credential,
protocol and model vendor a local run uses must be the ones the repository is
configured for. A command-line flag may change which *size* of a vendor's model
answers; it may not change who receives the source.
"""

from __future__ import annotations

import pytest

from mira.local.guard import (
    Destination,
    DestinationRefused,
    check_destinations,
    destination_for,
    load_repo_config,
    repo_config_path,
)
from tests.conftest import GitRepo

PINNED = """
llm:
  provider: openai
  base_url: https://openrouter.ai/api/v1
  api_key_env: OPENROUTER_API_KEY
  model: anthropic/claude-sonnet-4-6
"""


@pytest.fixture(autouse=True)
def no_ambient_model(monkeypatch: pytest.MonkeyPatch) -> None:
    """The host's own MIRA_MODEL must not decide what these tests observe."""
    monkeypatch.delenv("MIRA_MODEL", raising=False)


@pytest.fixture
def pinned_repo(git_repo: GitRepo) -> GitRepo:
    git_repo.write(".mira.yaml", PINNED)
    git_repo.commit("pin the model")
    return git_repo


def _guard(repo: GitRepo, overrides: dict | None = None) -> list[Destination]:
    config = load_repo_config(repo.root, overrides or {})
    return check_destinations(repo.root, effective=config)


class TestConfigAnchoring:
    def test_the_repository_root_is_where_config_is_read_from(self, pinned_repo: GitRepo) -> None:
        assert repo_config_path(pinned_repo.root) == pinned_repo.root / ".mira.yaml"

    def test_a_parent_directory_cannot_supply_the_configuration(
        self, git_repo: GitRepo, tmp_path
    ) -> None:
        # A .mira.yaml one level up — a workspace folder holding several
        # checkouts — must not decide where this repository's code goes.
        (tmp_path / ".mira.yaml").write_text(PINNED, encoding="utf-8")

        assert repo_config_path(git_repo.root) is None

    def test_the_repositorys_own_pin_is_honoured(self, pinned_repo: GitRepo) -> None:
        config = load_repo_config(pinned_repo.root)
        assert config.llm.model == "anthropic/claude-sonnet-4-6"


class TestDestinationComparison:
    def test_an_unchanged_run_is_allowed(self, pinned_repo: GitRepo) -> None:
        destinations = _guard(pinned_repo)

        assert [d.purpose for d in destinations] == ["review", "indexing", "security"]
        assert all(d.vendor == "anthropic" for d in destinations)

    def test_a_bigger_model_from_the_same_vendor_is_allowed(self, pinned_repo: GitRepo) -> None:
        destinations = _guard(pinned_repo, {"llm.model": "anthropic/claude-opus-4-1"})

        assert all(d.vendor == "anthropic" for d in destinations)
        assert destinations[0].model == "anthropic/claude-opus-4-1"

    def test_another_vendor_is_refused(self, pinned_repo: GitRepo) -> None:
        with pytest.raises(DestinationRefused) as caught:
            _guard(pinned_repo, {"llm.model": "openai/gpt-5"})

        assert caught.value.configured.vendor == "anthropic"
        assert caught.value.requested.vendor == "openai"
        assert "Refusing to send" in str(caught.value)

    def test_another_endpoint_is_refused(self, pinned_repo: GitRepo) -> None:
        with pytest.raises(DestinationRefused, match="different review destination"):
            _guard(pinned_repo, {"llm.base_url": "https://api.elsewhere.example/v1"})

    def test_another_credential_is_refused(self, pinned_repo: GitRepo) -> None:
        with pytest.raises(DestinationRefused):
            _guard(pinned_repo, {"llm.api_key_env": "SOMEONE_ELSES_KEY"})

    def test_another_protocol_is_refused(self, pinned_repo: GitRepo) -> None:
        with pytest.raises(DestinationRefused):
            _guard(pinned_repo, {"llm.api_style": "responses"})

    def test_a_repository_pin_already_outranks_the_environment(
        self, pinned_repo: GitRepo, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # `load_config` only consults MIRA_MODEL when nothing else set the
        # model, so a repository that pins one is unaffected. Asserted rather
        # than assumed: it is half the reason the guard has anything to compare.
        monkeypatch.setenv("MIRA_MODEL", "openai/gpt-5")

        assert load_repo_config(pinned_repo.root).llm.model == "anthropic/claude-sonnet-4-6"
        assert all(d.vendor == "anthropic" for d in _guard(pinned_repo))

    def test_the_environment_cannot_redirect_an_unpinned_repository(
        self, git_repo: GitRepo, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # With nothing pinned, MIRA_MODEL *is* honoured by `load_config` — and
        # the baseline is computed with it removed, which is what makes an
        # ambient redirect visible instead of silent.
        monkeypatch.setenv("MIRA_MODEL", "openai/gpt-5")

        with pytest.raises(DestinationRefused):
            _guard(git_repo)

    def test_the_environment_is_restored_after_the_check(
        self, git_repo: GitRepo, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("MIRA_MODEL", "anthropic/claude-haiku-4-5")

        _guard(git_repo)

        import os

        assert os.environ["MIRA_MODEL"] == "anthropic/claude-haiku-4-5"


class TestEveryPurposeIsChecked:
    def test_the_indexing_tier_cannot_be_redirected_on_its_own(self, pinned_repo: GitRepo) -> None:
        # The review tier is unchanged here. A guard that only looked at it
        # would let the same source reach a second vendor through indexing.
        with pytest.raises(DestinationRefused) as caught:
            _guard(pinned_repo, {"llm.indexing_model": "openai/gpt-5-mini"})

        assert caught.value.purpose == "indexing"

    def test_the_security_tier_cannot_be_redirected_on_its_own(self, pinned_repo: GitRepo) -> None:
        with pytest.raises(DestinationRefused) as caught:
            _guard(pinned_repo, {"llm.security_model": "openai/gpt-5"})

        assert caught.value.purpose == "security"


class TestDestinationShape:
    def test_a_bare_model_id_has_no_vendor_to_compare(self, pinned_repo: GitRepo) -> None:
        config = load_repo_config(pinned_repo.root, {"llm.model": "local-llama"})
        destination = destination_for(config, "review")

        assert destination.vendor == ""
        assert destination.endpoint == "https://openrouter.ai/api/v1"

    def test_bedrock_is_described_by_its_region(self, pinned_repo: GitRepo) -> None:
        config = load_repo_config(
            pinned_repo.root, {"llm.provider": "bedrock", "llm.region": "eu-west-1"}
        )
        destination = destination_for(config, "review")

        assert destination.endpoint == "aws:eu-west-1"
        assert "bedrock" in destination.describe()

    def test_a_destination_never_reports_a_credential_value(
        self, pinned_repo: GitRepo, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("OPENROUTER_API_KEY", "sk-should-never-be-printed")
        destination = destination_for(load_repo_config(pinned_repo.root), "review")

        rendered = destination.describe() + repr(destination.as_dict())
        assert "sk-should-never-be-printed" not in rendered
        assert "OPENROUTER_API_KEY" in rendered


class TestRepositoriesWithoutAPin:
    def test_an_unpinned_repository_runs_under_the_defaults(self, git_repo: GitRepo) -> None:
        destinations = _guard(git_repo)

        assert destinations[0].endpoint.startswith("https://")

    def test_an_unpinned_repository_still_refuses_a_redirect(self, git_repo: GitRepo) -> None:
        # There is nothing repository-specific to protect, but "not configured
        # for this repository" is still true of a vendor nobody chose.
        with pytest.raises(DestinationRefused):
            _guard(git_repo, {"llm.model": "openai/gpt-5"})
