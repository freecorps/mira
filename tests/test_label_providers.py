"""Provider contracts for custom label creation and complete size statistics."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from github import GithubException

from mira.exceptions import ProviderError
from mira.models import PRInfo
from mira.providers.forgejo import ForgejoProvider
from mira.providers.github import GitHubProvider
from mira.providers.gitlab import GitLabProvider


def pr(platform="github"):
    return PRInfo(
        title="",
        description="",
        base_branch="main",
        head_branch="feature",
        url="https://example.com/acme/app/pull/7",
        number=7,
        owner="acme",
        repo="app",
        platform=platform,
    )


def response(data, status=200):
    return SimpleNamespace(json=lambda: data, status_code=status)


async def test_github_creates_missing_label_and_keeps_existing_definition():
    provider = GitHubProvider("token")
    repo = MagicMock()
    provider._github = MagicMock()
    provider._github.get_repo.return_value = repo
    await provider.ensure_label(pr(), "mediana", "f9d65c", "Size")
    repo.create_label.assert_not_called()
    repo.get_label.side_effect = GithubException(404, {})
    await provider.ensure_label(pr(), "mediana", "f9d65c", "Size")
    repo.create_label.assert_called_once_with("mediana", "f9d65c", "Size")


async def test_github_label_creation_race_is_idempotent():
    provider = GitHubProvider("token")
    repo = MagicMock()
    provider._github = MagicMock()
    provider._github.get_repo.return_value = repo
    repo.get_label.side_effect = [GithubException(404, {}), SimpleNamespace(name="size/M")]
    repo.create_label.side_effect = GithubException(422, {})
    await provider.ensure_label(pr(), "size/M", "f9d65c")
    assert repo.get_label.call_count == 2


@pytest.mark.parametrize(
    "count,added,deleted,ok", [(1, 300, 201, True), (2, 300, 201, False), (1, 301, 201, False)]
)
async def test_github_detects_truncated_statistics(count, added, deleted, ok):
    provider = GitHubProvider("token")
    provider._github = MagicMock()
    pull = provider._github.get_repo.return_value.get_pull.return_value
    pull.changed_files, pull.additions, pull.deletions = count, added, deleted
    pull.get_files.return_value = [SimpleNamespace(filename="app.py", additions=300, deletions=201)]
    if not ok:
        with pytest.raises(ProviderError):
            await provider.get_label_change_stats(pr())
    else:
        stats = await provider.get_label_change_stats(pr())
        assert stats[0].added_lines + stats[0].deleted_lines == 501


@pytest.mark.parametrize(
    "provider_class,platform", [(GitLabProvider, "gitlab"), (ForgejoProvider, "forgejo")]
)
async def test_rest_provider_preserves_existing_label(provider_class, platform):
    provider = provider_class("token")
    provider._paginate = AsyncMock(return_value=[{"id": 1, "name": "mediana"}])
    provider._request = AsyncMock()
    await provider.ensure_label(pr(platform), "mediana", "ff0000", "New description")
    provider._request.assert_not_called()


@pytest.mark.parametrize(
    "provider_class,platform", [(GitLabProvider, "gitlab"), (ForgejoProvider, "forgejo")]
)
async def test_rest_provider_creates_custom_label(provider_class, platform):
    provider = provider_class("token")
    provider._paginate = AsyncMock(return_value=[])
    provider._request = AsyncMock(return_value=response({"id": 4}, 201))
    await provider.ensure_label(pr(platform), "mediana", "f9d65c", "Size")
    call = provider._request.call_args
    assert call.args[0] == "POST"
    body = call.kwargs["data" if platform == "gitlab" else "json"]
    assert body["name"] == "mediana"
    assert body["color"] == ("#f9d65c" if platform == "gitlab" else "f9d65c")


@pytest.mark.parametrize(
    "data",
    [
        {"overflow": True, "changes": []},
        {"changes": [{"too_large": True}]},
        {"changes": [{"collapsed": True}]},
        {},
    ],
)
async def test_gitlab_never_treats_incomplete_diff_as_small(data):
    provider = GitLabProvider("token")
    provider._request = AsyncMock(return_value=response(data))
    with pytest.raises(ProviderError):
        await provider.get_label_change_stats(pr("gitlab"))


async def test_gitlab_counts_additions_and_deletions_and_binary_paths():
    provider = GitLabProvider("token")
    provider._request = AsyncMock(
        return_value=response(
            {
                "overflow": False,
                "changes": [
                    {"new_path": "app.py", "diff": "@@ -1 +1,2 @@\n-old\n+new\n+more\n"},
                    {"new_path": "image.png", "diff": ""},
                ],
            }
        )
    )
    stats = await provider.get_label_change_stats(pr("gitlab"))
    assert len(stats) == 2
    assert (stats[0].added_lines, stats[0].deleted_lines) == (2, 1)


async def test_forgejo_removes_only_selected_label_by_id():
    provider = ForgejoProvider("token")
    provider._paginate = AsyncMock(
        return_value=[{"id": 11, "name": "size/M"}, {"id": 12, "name": "bug"}]
    )
    provider._request = AsyncMock(return_value=response(None, 204))
    await provider.remove_label(pr("forgejo"), "size/M")
    assert provider._request.call_args.args[0] == "DELETE"
    assert provider._request.call_args.args[1].endswith("/issues/7/labels/11")
    assert 204 in provider._request.call_args.kwargs["ok"]


async def test_forgejo_validates_file_count():
    provider = ForgejoProvider("token")
    provider._paginate = AsyncMock(
        return_value=[{"filename": "app.py", "additions": 250, "deletions": 251}]
    )
    provider._request = AsyncMock(
        return_value=response({"changed_files": 1, "additions": 250, "deletions": 251})
    )
    assert (await provider.get_label_change_stats(pr("forgejo")))[0].deleted_lines == 251
    provider._request = AsyncMock(return_value=response({"changed_files": 2}))
    with pytest.raises(ProviderError):
        await provider.get_label_change_stats(pr("forgejo"))


@pytest.mark.parametrize(
    "summary",
    [
        {"changed_files": 1, "additions": 251, "deletions": 251},
        {"changed_files": 1, "additions": 250, "deletions": 252},
        {"changed_files": 1},
        {"changed_files": 1, "additions": None, "deletions": 251},
    ],
)
async def test_forgejo_rejects_missing_or_inconsistent_totals_with_complete_file_count(summary):
    provider = ForgejoProvider("token")
    provider._paginate = AsyncMock(
        return_value=[{"filename": "app.py", "additions": 250, "deletions": 251}]
    )
    provider._request = AsyncMock(return_value=response(summary))
    with pytest.raises(ProviderError):
        await provider.get_label_change_stats(pr("forgejo"))
