"""Which repositories an MCP session can reach, and how it cannot reach others.

The whole of this surface's isolation is one decision: a tool call names a
repository, and that name is *looked up* in a grant built at startup rather
than parsed into a repository to open. These tests are about the difference,
because the difference is invisible until somebody sends a name that parses
into something Mira would otherwise have happily opened.
"""

from __future__ import annotations

import os

import pytest

from mira.config import McpConfig, MiraConfig
from mira.index.store import IndexStore
from mira.mcp.authz import Grant, InvalidRepository, NotAuthorized, parse_repository
from tests.mcp_support import call, finding, payload_of, populate, server, text_of


class TestNamingARepository:
    def test_a_bare_slug_means_github(self) -> None:
        assert parse_repository("acme/widgets").key == "github:acme/widgets"

    def test_a_platform_can_be_stated(self) -> None:
        assert parse_repository("forgejo:acme/widgets").platform == "forgejo"

    def test_a_gitlab_subgroup_is_part_of_the_owner(self) -> None:
        # GitLab namespaces nest, and the index store keys on owner + repo, so
        # the group path has to survive as the owner.
        repository = parse_repository("gitlab:group/sub/project")

        assert (repository.owner, repository.repo) == ("group/sub", "project")

    @pytest.mark.parametrize(
        "spec",
        [
            "",
            "widgets",
            "acme/",
            "/widgets",
            "../../etc/passwd",
            "acme/../widgets",
            ".hidden/widgets",
            "acme/.hidden",
            "acme/wid gets",
            "acme/wid\ngets",
            "bitbucket:acme/widgets",
            "acme/widgets;drop",
        ],
    )
    def test_a_name_that_could_not_be_a_repository_is_refused(self, spec: str) -> None:
        # These become directory names under MIRA_INDEX_DIR. A name that can
        # mean something else on a filesystem is rejected where an operator
        # sees it, rather than at the point it would have opened something.
        with pytest.raises(InvalidRepository):
            parse_repository(spec)

    def test_surrounding_whitespace_is_forgiven(self) -> None:
        # A name pasted out of a config file or a shell often arrives with a
        # newline on it. That is a typo, not an attack, and the character set
        # inside the name is what the refusals above are about.
        assert parse_repository("  acme/widgets\n").key == "github:acme/widgets"


class TestTheGrantIsTheOnlyWayIn:
    def test_a_granted_repository_resolves_to_the_startup_object(self) -> None:
        grant = Grant.from_specs(["acme/widgets"])

        assert grant.resolve("github:acme/widgets") is grant.repositories[0]

    def test_an_ungranted_repository_is_refused(self) -> None:
        grant = Grant.from_specs(["acme/widgets"])

        with pytest.raises(NotAuthorized):
            grant.resolve("acme/secrets")

    def test_a_malformed_name_is_refused_the_same_way(self) -> None:
        # Not `InvalidRepository`: a client has no business learning which of
        # its refused names were well formed.
        grant = Grant.from_specs(["acme/widgets"])

        with pytest.raises(NotAuthorized):
            grant.resolve("../../../etc/passwd")

    def test_an_empty_grant_refuses_everything(self) -> None:
        # The state an operator reaches by enabling the feature and not saying
        # which repositories. It must not read as "all of them".
        grant = Grant.from_specs([])

        assert not grant
        with pytest.raises(NotAuthorized):
            grant.resolve("acme/widgets")

    def test_two_spellings_of_one_repository_are_one_entry(self) -> None:
        grant = Grant.from_specs(["acme/widgets", "github:acme/widgets"])

        assert grant.keys == ("github:acme/widgets",)

    def test_the_same_name_on_two_platforms_is_two_repositories(self) -> None:
        grant = Grant.from_specs(["acme/widgets", "gitlab:acme/widgets"])

        assert len(grant.repositories) == 2


class TestNarrowingOnly:
    def test_a_launch_can_ask_for_less(self) -> None:
        grant = Grant.from_specs(["acme/widgets", "acme/other"])

        assert grant.narrow(["acme/other"]).keys == ("github:acme/other",)

    def test_a_launch_cannot_ask_for_more(self) -> None:
        # Otherwise the configured ceiling is decorative: anyone who can start
        # the process can grant themselves the rest of the install.
        grant = Grant.from_specs(["acme/widgets"])

        with pytest.raises(NotAuthorized):
            grant.narrow(["acme/secrets"])


class TestOneSessionSeesOneSetOfRepositories:
    def test_a_call_for_an_ungranted_repository_is_an_error_result(self) -> None:
        populate("acme", "widgets", findings=[finding()])
        populate("other", "thing", findings=[finding(owner="other", repo="thing")])

        response = call(server("acme/widgets"), "mira_list_findings", repository="other/thing")

        assert response["isError"] is True
        assert "not granted" in text_of(response)

    def test_the_refusal_does_not_depend_on_the_repository_existing(self) -> None:
        # A different answer for "exists but not yours" and "does not exist"
        # is an existence oracle over the whole install.
        session = server("acme/widgets")
        populate("other", "thing", findings=[finding(owner="other", repo="thing")])

        existing = text_of(call(session, "mira_list_findings", repository="other/thing"))
        absent = text_of(call(session, "mira_list_findings", repository="ghost/nothing"))

        assert existing.replace("other/thing", "X") == absent.replace("ghost/nothing", "X")

    def test_a_grant_for_one_repository_returns_only_that_one(self) -> None:
        populate("acme", "widgets", findings=[finding()])
        populate("other", "thing", findings=[finding(owner="other", repo="thing")])

        listed = payload_of(call(server("acme/widgets"), "mira_list_repositories"))

        assert [item["repository"] for item in listed["items"]] == ["github:acme/widgets"]

    def test_the_same_slug_on_another_platform_is_another_repository(self) -> None:
        populate("acme", "widgets", findings=[finding(title="on github")])
        populate("acme", "widgets", platform="gitlab", findings=[finding(title="on gitlab")])

        response = call(
            server("acme/widgets"), "mira_list_findings", repository="gitlab:acme/widgets"
        )

        assert response["isError"] is True

    def test_a_granted_repository_reads_its_own_data(self) -> None:
        populate("acme", "widgets", findings=[finding(title="ours")])
        populate("other", "thing", findings=[finding(owner="other", repo="thing", title="theirs")])

        payload = payload_of(
            call(server("acme/widgets"), "mira_list_findings", repository="acme/widgets")
        )

        assert [item["title"] for item in payload["items"]] == ["ours"]


class TestTheConfigurationIsTheCeiling:
    def test_a_repository_specification_that_cannot_parse_fails_at_load(self) -> None:
        with pytest.raises(ValueError, match="mcp.repositories"):
            MiraConfig.model_validate({"mcp": {"enabled": True, "repositories": ["../../etc"]}})

    def test_the_feature_is_off_by_default(self) -> None:
        config = McpConfig()

        assert config.enabled is False
        assert config.repositories == []

    def test_configured_repositories_are_stored_canonically(self) -> None:
        config = MiraConfig.model_validate(
            {"mcp": {"repositories": ["acme/widgets", "github:acme/widgets"]}}
        )

        assert config.mcp.repositories == ["github:acme/widgets"]


class TestReadingCreatesNothing:
    def test_a_granted_repository_with_no_index_is_not_created_by_reading(self) -> None:
        # Opening a store creates its database. On a surface whose whole claim
        # is that it does not write, a read that leaves a file behind is the
        # claim failing quietly.
        path = IndexStore.db_path_for("acme", "widgets")
        assert not os.path.exists(path)

        payload = payload_of(
            call(server("acme/widgets"), "mira_list_findings", repository="acme/widgets")
        )

        assert payload["indexed"] is False
        assert payload["items"] == []
        assert not os.path.exists(path)

    def test_an_unindexed_repository_says_so_rather_than_looking_empty(self) -> None:
        payload = payload_of(
            call(server("acme/widgets"), "mira_list_rules", repository="acme/widgets")
        )

        assert "no index" in payload["note"]
