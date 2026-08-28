"""Phase 7C — the provider adapters, and the identity they are allowed to claim.

The parity that matters is not that all three do the same thing. It is that
each one *declares* what it cannot do and that triage then says so instead of
guessing — and, in one case, that a provider refuses to supply an identity it
cannot vouch for.

GitLab is that case. Its commit API reports an author's name and email, both
written into the commit by whoever made it. Ranking a colleague on that would
mean ranking them on a string a contributor typed, so GitLab declares
``can_attribute_commits=False`` and the authorship signal on GitLab comes from
the merge requests Mira itself watched merge.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

import pytest

from mira.models import PRInfo
from mira.providers.base import BaseProvider
from mira.providers.forgejo import ForgejoProvider
from mira.providers.github import GitHubProvider
from mira.providers.gitlab import GitLabProvider
from mira.triage.capabilities import (
    FORGEJO_CAPABILITIES,
    GITHUB_CAPABILITIES,
    GITLAB_CAPABILITIES,
    NO_CAPABILITIES,
    TriageCapabilities,
    for_platform,
    for_provider,
    narrow,
)


def _pr(platform: str = "github") -> PRInfo:
    return PRInfo(
        title="t",
        description="",
        base_branch="main",
        head_branch="feature",
        url="https://example.com/acme/app/pull/7",
        number=7,
        owner="acme",
        repo="app",
        base_sha="base111",
        head_sha="head222",
        platform=platform,
    )


class _FakeResp:
    def __init__(self, status: int = 200, json_data: Any = None) -> None:
        self.status_code = status
        self._json = json_data if json_data is not None else []

    def json(self) -> Any:
        return self._json


# ─────────────────────────────────────────────────────────── capabilities ──


def test_each_platform_declares_what_it_can_attribute() -> None:
    assert GITHUB_CAPABILITIES.can_attribute_commits is True
    assert FORGEJO_CAPABILITIES.can_attribute_commits is True
    assert GITLAB_CAPABILITIES.can_attribute_commits is False
    assert "not verified" in " ".join(GITLAB_CAPABILITIES.notes)


def test_every_platform_can_be_asked_for_ownership() -> None:
    for platform in ("github", "gitlab", "forgejo"):
        assert for_platform(platform).can_read_ownership is True


def test_an_unknown_platform_declares_nothing() -> None:
    assert for_platform("bitbucket") is NO_CAPABILITIES
    assert for_platform("").can_read_ownership is False


def test_a_provider_may_claim_less_than_its_platform() -> None:
    reduced = TriageCapabilities(provider="github", can_read_ownership=True)
    result = narrow(reduced, GITHUB_CAPABILITIES)
    assert result.can_read_ownership is True
    assert result.can_attribute_commits is False


def test_a_provider_may_never_claim_more_than_its_platform() -> None:
    """Otherwise a skip that explains itself becomes a call that fails."""
    overreaching = TriageCapabilities(
        provider="gitlab", can_read_ownership=True, can_attribute_commits=True
    )
    assert narrow(overreaching, GITLAB_CAPABILITIES).can_attribute_commits is False


def test_the_live_providers_report_their_own_platform() -> None:
    assert for_provider(GitHubProvider("t")).provider == "github"
    assert for_provider(GitLabProvider("t")).provider == "gitlab"
    assert for_provider(ForgejoProvider("t")).provider == "forgejo"


def test_no_provider_at_all_declares_nothing() -> None:
    assert for_provider(None) is NO_CAPABILITIES


def test_a_provider_that_raises_degrades_rather_than_widening() -> None:
    class Broken:
        def triage_capabilities(self) -> TriageCapabilities:
            raise RuntimeError("boom")

    assert for_provider(Broken()) is NO_CAPABILITIES


def test_a_provider_that_returns_nonsense_degrades() -> None:
    class Odd:
        def triage_capabilities(self) -> Any:
            return {"can_attribute_commits": True}

    assert for_provider(Odd()) is NO_CAPABILITIES


def test_the_base_provider_declares_nothing_and_supplies_nothing() -> None:
    assert BaseProvider.triage_capabilities(object()) is NO_CAPABILITIES  # type: ignore[arg-type]


# ─────────────────────────────────────────────────── CODEOWNERS at a ref ──


@pytest.mark.parametrize("provider_class", [GitHubProvider, GitLabProvider, ForgejoProvider])
def test_every_provider_can_be_pointed_at_a_ref(provider_class: type) -> None:
    """Triage refuses to read ownership from a provider that cannot be.

    A provider whose `get_codeowners` has no `ref` parameter is reported as
    ``unsupported`` rather than read at its default — which is the head.
    """
    import inspect

    assert "ref" in inspect.signature(provider_class.get_codeowners).parameters


async def test_github_reads_codeowners_at_the_ref_it_is_given() -> None:
    provider = GitHubProvider("token")
    seen: list[tuple[str, str]] = []

    async def _content(self: Any, pr_info: PRInfo, path: str, ref: str) -> str:
        seen.append((path, ref))
        return "src/ @dana\n" if path == ".github/CODEOWNERS" else ""

    with patch.object(GitHubProvider, "get_file_content", _content):
        path, content = await provider.get_codeowners(_pr(), ref="base111")

    assert path == ".github/CODEOWNERS"
    assert "@dana" in content
    assert seen[0][1] == "base111"


async def test_github_still_defaults_to_the_head_for_the_merge_gate() -> None:
    """The gate's reading is unchanged: an owner declared on the branch can
    only ever *stop* an automatic approval."""
    provider = GitHubProvider("token")
    seen: list[str] = []

    async def _content(self: Any, pr_info: PRInfo, path: str, ref: str) -> str:
        seen.append(ref)
        return "src/ @dana\n"

    with patch.object(GitHubProvider, "get_file_content", _content):
        await provider.get_codeowners(_pr())
    assert seen == ["head222"]


# ─────────────────────────────────────────────────────────── path authors ──


async def test_github_attributes_a_commit_to_the_account_not_the_commit_fields() -> None:
    payload = [
        {
            "sha": "abc1234567890",
            "html_url": "https://github.com/acme/app/commit/abc",
            "author": {"login": "dana"},
            "commit": {"author": {"name": "Someone Else", "date": "2026-08-01T12:00:00Z"}},
        }
    ]
    provider = GitHubProvider("token")

    class _Client:
        async def __aenter__(self) -> Any:
            return self

        async def __aexit__(self, *args: Any) -> None:
            return None

        async def get(self, url: str, **kwargs: Any) -> _FakeResp:
            assert kwargs["params"]["sha"] == "base111"
            return _FakeResp(200, payload)

    with patch("httpx.AsyncClient", lambda *a, **k: _Client()):
        result = await provider.get_path_authors(_pr(), ["src/app.py"], ref="base111")

    entry = result["src/app.py"][0]
    assert entry.login == "dana"
    assert entry.sha == "abc123456789"
    assert entry.at > 0


async def test_github_reports_an_unresolvable_commit_as_unresolved() -> None:
    """`author: null` is GitHub saying it does not know who this was.

    The entry is returned with an empty login and the caller drops it; falling
    back to the commit's own author fields would turn "we do not know" into a
    name.
    """
    payload = [
        {
            "sha": "abc",
            "author": None,
            "commit": {"author": {"name": "Anyone", "date": "2026-08-01T12:00:00Z"}},
        }
    ]
    provider = GitHubProvider("token")

    class _Client:
        async def __aenter__(self) -> Any:
            return self

        async def __aexit__(self, *args: Any) -> None:
            return None

        async def get(self, url: str, **kwargs: Any) -> _FakeResp:
            return _FakeResp(200, payload)

    with patch("httpx.AsyncClient", lambda *a, **k: _Client()):
        result = await provider.get_path_authors(_pr(), ["src/app.py"], ref="base111")
    assert result["src/app.py"][0].login == ""


async def test_github_asks_for_nothing_without_a_ref() -> None:
    provider = GitHubProvider("token")
    empty = _pr()
    empty.base_sha = ""
    empty.base_branch = ""
    assert await provider.get_path_authors(empty, ["src/app.py"]) == {}


async def test_gitlab_supplies_no_commit_authorship_at_all() -> None:
    """Declared in the capability table and true of the adapter."""
    provider = GitLabProvider("token")
    assert await provider.get_path_authors(_pr("gitlab"), ["src/app.py"], ref="base111") == {}


async def test_forgejo_attributes_a_commit_when_the_email_maps_to_an_account() -> None:
    payload = [
        {
            "sha": "def4567890123",
            "html_url": "https://forge.example/acme/app/commit/def",
            "author": {"login": "sam"},
            "commit": {"author": {"name": "Sam", "date": "2026-08-01T12:00:00Z"}},
        },
        {
            "sha": "aaa",
            "author": None,
            "commit": {"author": {"name": "Nobody", "date": "2026-08-01T12:00:00Z"}},
        },
    ]
    provider = ForgejoProvider("token")

    async def _request(self: Any, method: str, url: str, **kwargs: Any) -> _FakeResp:
        assert "base111" in url
        return _FakeResp(200, payload)

    with patch.object(ForgejoProvider, "_request", _request):
        result = await provider.get_path_authors(_pr("forgejo"), ["src/app.py"], ref="base111")

    logins = [entry.login for entry in result["src/app.py"]]
    assert logins == ["sam", ""]


async def test_a_failed_fetch_yields_no_entries_rather_than_an_exception() -> None:
    provider = GitHubProvider("token")

    class _Client:
        async def __aenter__(self) -> Any:
            return self

        async def __aexit__(self, *args: Any) -> None:
            return None

        async def get(self, url: str, **kwargs: Any) -> _FakeResp:
            return _FakeResp(500, {})

    with patch("httpx.AsyncClient", lambda *a, **k: _Client()):
        assert await provider.get_path_authors(_pr(), ["src/app.py"], ref="base111") == {}


def test_no_provider_offers_a_way_to_request_a_review() -> None:
    """The invariant of the phase, asserted against the provider surface itself.

    If a method with one of these names ever appears, the guarantee that triage
    only suggests stops being structural and becomes a promise.
    """
    forbidden = {
        "request_reviewers",
        "request_review",
        "add_assignee",
        "add_assignees",
        "create_review_request",
    }
    for provider_class in (BaseProvider, GitHubProvider, GitLabProvider, ForgejoProvider):
        assert forbidden.isdisjoint(set(dir(provider_class)))
