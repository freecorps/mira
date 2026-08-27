"""Phase 5 — the provider adapters that do the writing, against mocked APIs.

Each provider has to answer three questions truthfully — what an account may do
here, what the default branch is called, and whether the head branch lives in a
fork — and then perform four writes without ever forcing one. A wrong mapping
in the first group is what turns into a write nobody authorized; a wrong call in
the second is what turns into a rewritten history.

The other half is what happens when a read fails. A permission that cannot be
read is `unknown`, which is never treated as permission. A default branch that
cannot be read is `""`, which refuses writing altogether. A fork check that
cannot be answered says "fork", which refuses the one write that would have
crossed a repository boundary.
"""

from __future__ import annotations

import base64
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from mira.autofix.capabilities import for_provider
from mira.exceptions import ProviderError
from mira.models import PRInfo
from mira.providers.forgejo import ForgejoProvider
from mira.providers.github import GitHubProvider
from mira.providers.gitlab import GitLabProvider


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
        head_sha="head123",
        platform=platform,
        author="alice",
    )


class _FakeResp:
    def __init__(self, status=200, json_data=None, text="", headers=None):
        self.status_code = status
        self._json = json_data
        self.text = text
        self.headers = headers or {}

    def json(self):
        return self._json

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class _FakeClient:
    def __init__(self, handler):
        self._handler = handler

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def request(self, method, url, **kw):
        return self._handler(method, url, **kw)

    async def get(self, url, **kw):
        return self._handler("GET", url, **kw)


def _patch_gitlab(handler):
    return patch("mira.providers.gitlab.httpx.AsyncClient", lambda *a, **k: _FakeClient(handler))


def _patch_forgejo(handler):
    return patch("mira.providers.forgejo.httpx.AsyncClient", lambda *a, **k: _FakeClient(handler))


def _gitlab() -> GitLabProvider:
    provider = GitLabProvider.__new__(GitLabProvider)
    provider._token = "t"
    provider._api = "https://gitlab.example/api/v4"
    provider._username = "mira"
    return provider


def _forgejo() -> ForgejoProvider:
    provider = ForgejoProvider.__new__(ForgejoProvider)
    provider._token = "t"
    provider._api = "https://forge.example/api/v1"
    provider._username = "mira"
    return provider


# ───────────────────────────────────────────────────────────── capabilities ──


def test_each_provider_declares_what_it_can_write() -> None:
    github = for_provider(GitHubProvider.__new__(GitHubProvider))
    gitlab = for_provider(GitLabProvider.__new__(GitLabProvider))
    forgejo = for_provider(ForgejoProvider.__new__(ForgejoProvider))

    for capabilities in (github, gitlab, forgejo):
        assert capabilities.can_publish is True
        assert capabilities.can_read_permission is True
        assert capabilities.can_read_default_branch is True
        # The one flag that is False everywhere, deliberately and permanently.
        assert capabilities.can_merge is False

    assert any("Developer" in note for note in gitlab.notes)
    assert any("one commit" in note for note in forgejo.notes)


def test_a_provider_that_declares_nothing_gets_nothing() -> None:
    assert for_provider(None).can_publish is False
    assert for_provider(SimpleNamespace()).can_publish is False


# ────────────────────────────────────────────────────────────────── GitHub ──


def _github_provider(repo: MagicMock | None = None):
    provider = GitHubProvider.__new__(GitHubProvider)
    gh_repo = repo or MagicMock()
    github = MagicMock()
    github.get_repo.return_value = gh_repo
    provider._github = github
    provider._token = "t"
    return provider, gh_repo


@pytest.mark.parametrize(
    ("reported", "expected"),
    [("admin", "admin"), ("write", "write"), ("read", "read"), ("none", "none")],
)
async def test_github_permission_mapping(reported: str, expected: str) -> None:
    provider, repo = _github_provider()
    repo.get_collaborator_permission.return_value = reported
    assert await provider.get_actor_permission(_pr(), "alice") == expected


async def test_github_a_non_collaborator_is_none_not_unknown() -> None:
    from github import GithubException

    provider, repo = _github_provider()
    repo.get_collaborator_permission.side_effect = GithubException(404, {}, {})
    assert await provider.get_actor_permission(_pr(), "stranger") == "none"


async def test_github_an_unreadable_permission_is_unknown() -> None:
    provider, repo = _github_provider()
    repo.get_collaborator_permission.side_effect = RuntimeError("500")
    assert await provider.get_actor_permission(_pr(), "alice") == "unknown"


async def test_github_default_branch_is_read_and_degrades_to_empty() -> None:
    provider, repo = _github_provider()
    repo.default_branch = "trunk"
    assert await provider.get_default_branch(_pr()) == "trunk"

    broken, github = _github_provider()
    broken._github.get_repo.side_effect = RuntimeError("no")
    assert await broken.get_default_branch(_pr()) == ""


async def test_github_a_missing_branch_is_an_empty_sha_not_an_error() -> None:
    from github import GithubException

    provider, repo = _github_provider()
    repo.get_git_ref.side_effect = GithubException(404, {}, {})
    assert await provider.get_branch_head(_pr(), "mira/fix/pr-7/abc") == ""


async def test_github_an_existing_branch_reports_its_tip() -> None:
    provider, repo = _github_provider()
    repo.get_git_ref.return_value = SimpleNamespace(object=SimpleNamespace(sha="c0ffee"))
    assert await provider.get_branch_head(_pr(), "mira/fix/pr-7/abc") == "c0ffee"


async def test_github_creates_a_branch_and_never_updates_one() -> None:
    provider, repo = _github_provider()
    await provider.create_branch(_pr(), "mira/fix/pr-7/abc", "head123")
    repo.create_git_ref.assert_called_once_with(ref="refs/heads/mira/fix/pr-7/abc", sha="head123")
    # There is no update/reset call in the adapter at all.
    repo.get_git_ref.return_value.edit.assert_not_called()


async def test_github_commits_one_commit_and_never_forces() -> None:
    provider, repo = _github_provider()
    ref = MagicMock()
    ref.object.sha = "parent1"
    repo.get_git_ref.return_value = ref
    repo.get_git_commit.return_value = SimpleNamespace(tree=SimpleNamespace(sha="tree1"))
    repo.create_git_tree.return_value = SimpleNamespace(sha="tree2")
    repo.create_git_commit.return_value = SimpleNamespace(sha="newsha")

    sha = await provider.commit_files(
        _pr(), "mira/fix/pr-7/abc", {"a.py": "x = 1\n", "b.py": "y = 2\n"}, "fix: guard"
    )
    assert sha == "newsha"
    # One tree, one commit — a multi-file patch is not a sequence of half-states.
    assert repo.create_git_tree.call_count == 1
    assert repo.create_git_commit.call_count == 1
    ref.edit.assert_called_once_with(sha="newsha", force=False)


async def test_github_opens_a_pull_request() -> None:
    provider, repo = _github_provider()
    repo.create_pull.return_value = SimpleNamespace(
        number=900, html_url="https://github.com/acme/app/pull/900"
    )
    number, url = await provider.create_pull_request(
        _pr(), head="mira/fix/pr-7/abc", base="feature", title="fix: guard", body="why"
    )
    assert number == 900
    assert url.endswith("/900")
    repo.create_pull.assert_called_once_with(
        title="fix: guard", body="why", head="mira/fix/pr-7/abc", base="feature"
    )


async def test_github_finds_an_existing_pull_request_for_a_branch() -> None:
    provider, repo = _github_provider()
    repo.get_pulls.return_value = [
        SimpleNamespace(number=900, html_url="https://github.com/acme/app/pull/900")
    ]
    found = await provider.find_open_pull_request(_pr(), "mira/fix/pr-7/abc")
    assert found == (900, "https://github.com/acme/app/pull/900")
    repo.get_pulls.assert_called_once_with(state="open", head="acme:mira/fix/pr-7/abc")


async def test_github_files_match_only_when_every_byte_agrees() -> None:
    provider, repo = _github_provider()
    repo.get_contents.return_value = SimpleNamespace(content=base64.b64encode(b"x = 1\n").decode())
    assert await provider.files_match(_pr(), "b", {"a.py": "x = 1\n"}) is True
    assert await provider.files_match(_pr(), "b", {"a.py": "x = 2\n"}) is False


async def test_github_files_match_is_false_on_any_doubt() -> None:
    """Committing the same content twice is untidy; skipping a real commit is a
    fix that silently did nothing."""
    provider, repo = _github_provider()
    repo.get_contents.side_effect = RuntimeError("rate limited")
    assert await provider.files_match(_pr(), "b", {"a.py": "x"}) is False


@pytest.mark.parametrize(
    ("full_name", "expected"),
    [("acme/app", False), ("ACME/App", False), ("mallory/app", True)],
)
async def test_github_fork_detection(full_name: str, expected: bool) -> None:
    provider, repo = _github_provider()
    repo.get_pull.return_value = SimpleNamespace(
        head=SimpleNamespace(repo=SimpleNamespace(full_name=full_name))
    )
    assert await provider.pr_head_is_fork(_pr()) is expected


async def test_github_an_unanswerable_fork_check_says_fork() -> None:
    provider, repo = _github_provider()
    repo.get_pull.side_effect = RuntimeError("gone")
    assert await provider.pr_head_is_fork(_pr()) is True

    provider, repo = _github_provider()
    repo.get_pull.return_value = SimpleNamespace(head=SimpleNamespace(repo=None))
    assert await provider.pr_head_is_fork(_pr()) is True


async def test_github_a_failed_write_raises_rather_than_reporting_success() -> None:
    provider, repo = _github_provider()
    repo.create_git_ref.side_effect = RuntimeError("protected branch")
    with pytest.raises(ProviderError):
        await provider.create_branch(_pr(), "mira/fix/pr-7/abc", "head123")


# ────────────────────────────────────────────────────────────────── GitLab ──


@pytest.mark.parametrize(
    ("level", "expected"),
    [(50, "admin"), (40, "maintain"), (30, "write"), (20, "read"), (10, "read"), (0, "none")],
)
async def test_gitlab_access_level_mapping(level: int, expected: str) -> None:
    def handler(method, url, **kw):
        return _FakeResp(json_data=[{"username": "alice", "access_level": level}])

    with _patch_gitlab(handler):
        assert await _gitlab().get_actor_permission(_pr("gitlab"), "alice") == expected


async def test_gitlab_below_developer_never_counts_as_write() -> None:
    """Reporter (20) can read the project and cannot push to it."""
    from mira.autofix.authorization import WRITE_PERMISSIONS

    def handler(method, url, **kw):
        return _FakeResp(json_data=[{"username": "alice", "access_level": 20}])

    with _patch_gitlab(handler):
        got = await _gitlab().get_actor_permission(_pr("gitlab"), "alice")
    assert got not in WRITE_PERMISSIONS


async def test_gitlab_an_unreadable_membership_is_unknown() -> None:
    def handler(method, url, **kw):
        raise RuntimeError("502")

    with _patch_gitlab(handler):
        assert await _gitlab().get_actor_permission(_pr("gitlab"), "alice") == "unknown"


async def test_gitlab_default_branch_and_its_failure_mode() -> None:
    with _patch_gitlab(lambda *a, **k: _FakeResp(json_data={"default_branch": "trunk"})):
        assert await _gitlab().get_default_branch(_pr("gitlab")) == "trunk"

    def broken(method, url, **kw):
        raise RuntimeError("down")

    with _patch_gitlab(broken):
        assert await _gitlab().get_default_branch(_pr("gitlab")) == ""


async def test_gitlab_branch_head_and_missing_branch() -> None:
    with _patch_gitlab(lambda *a, **k: _FakeResp(json_data={"commit": {"id": "c0ffee"}})):
        assert await _gitlab().get_branch_head(_pr("gitlab"), "b") == "c0ffee"

    with _patch_gitlab(lambda *a, **k: _FakeResp(status=404, json_data={})):
        assert await _gitlab().get_branch_head(_pr("gitlab"), "b") == ""


async def test_gitlab_creates_a_branch_through_the_create_only_endpoint() -> None:
    calls: list[tuple[str, str, dict]] = []

    def handler(method, url, **kw):
        calls.append((method, url, kw.get("json") or {}))
        return _FakeResp(status=201, json_data={})

    with _patch_gitlab(handler):
        await _gitlab().create_branch(_pr("gitlab"), "mira/fix/pr-7/abc", "head123")
    method, url, body = calls[0]
    assert method == "POST"
    assert url.endswith("/repository/branches")
    assert body == {"branch": "mira/fix/pr-7/abc", "ref": "head123"}


async def test_gitlab_commits_with_the_right_verb_per_file() -> None:
    """GitLab rejects `update` on a path that does not exist, and vice versa."""
    calls: list[tuple[str, str, dict]] = []

    def handler(method, url, **kw):
        calls.append((method, url, kw.get("json") or {}))
        if "/repository/branches/" in url:
            return _FakeResp(status=200, json_data={"commit": {"id": "tip123"}})
        if "/repository/files/" in url:
            # `a.py` exists on the branch; `new.py` does not.
            return _FakeResp(status=200 if "a.py" in url else 404, json_data={})
        return _FakeResp(status=201, json_data={"id": "newsha"})

    with _patch_gitlab(handler):
        sha = await _gitlab().commit_files(
            _pr("gitlab"), "b", {"a.py": "x", "new.py": "y"}, "fix: guard"
        )
    assert sha == "newsha"
    actions = calls[-1][2]["actions"]
    assert {a["file_path"]: a["action"] for a in actions} == {
        "a.py": "update",
        "new.py": "create",
    }


async def test_gitlab_binds_the_commit_to_the_branch_tip_it_read() -> None:
    """Without `last_commit_id` a whole-file write silently eats a racing push."""
    calls: list[tuple[str, str, dict]] = []

    def handler(method, url, **kw):
        calls.append((method, url, kw.get("json") or {}))
        if "/repository/branches/" in url:
            return _FakeResp(status=200, json_data={"commit": {"id": "tip123"}})
        if "/repository/files/" in url:
            return _FakeResp(status=200, json_data={})
        return _FakeResp(status=201, json_data={"id": "newsha"})

    with _patch_gitlab(handler):
        await _gitlab().commit_files(_pr("gitlab"), "b", {"a.py": "x"}, "fix: guard")
    assert calls[-1][2]["last_commit_id"] == "tip123"


async def test_gitlab_opens_a_merge_request() -> None:
    with _patch_gitlab(
        lambda *a, **k: _FakeResp(
            status=201, json_data={"iid": 900, "web_url": "https://gitlab.example/mr/900"}
        )
    ):
        number, url = await _gitlab().create_pull_request(
            _pr("gitlab"), head="mira/fix/pr-7/abc", base="feature", title="t", body="b"
        )
    assert (number, url) == (900, "https://gitlab.example/mr/900")


async def test_gitlab_finds_an_existing_merge_request() -> None:
    with _patch_gitlab(
        lambda *a, **k: _FakeResp(
            json_data=[{"iid": 900, "web_url": "https://gitlab.example/mr/900"}]
        )
    ):
        assert await _gitlab().find_open_pull_request(_pr("gitlab"), "b") == (
            900,
            "https://gitlab.example/mr/900",
        )


@pytest.mark.parametrize(
    ("source", "target", "expected"),
    [(1, 1, False), (2, 1, True)],
)
async def test_gitlab_fork_detection(source: int, target: int, expected: bool) -> None:
    with _patch_gitlab(
        lambda *a, **k: _FakeResp(
            json_data={"source_project_id": source, "target_project_id": target}
        )
    ):
        assert await _gitlab().pr_head_is_fork(_pr("gitlab")) is expected


async def test_gitlab_an_unanswerable_fork_check_says_fork() -> None:
    with _patch_gitlab(lambda *a, **k: _FakeResp(json_data={})):
        assert await _gitlab().pr_head_is_fork(_pr("gitlab")) is True


async def test_gitlab_files_match_compares_raw_content() -> None:
    with _patch_gitlab(lambda *a, **k: _FakeResp(status=200, text="x = 1\n")):
        assert await _gitlab().files_match(_pr("gitlab"), "b", {"a.py": "x = 1\n"}) is True
        assert await _gitlab().files_match(_pr("gitlab"), "b", {"a.py": "x = 2\n"}) is False


# ───────────────────────────────────────────────────────────────── Forgejo ──


@pytest.mark.parametrize(
    ("reported", "expected"),
    [("owner", "admin"), ("admin", "admin"), ("write", "write"), ("read", "read")],
)
async def test_forgejo_permission_mapping(reported: str, expected: str) -> None:
    with _patch_forgejo(lambda *a, **k: _FakeResp(json_data={"permission": reported})):
        assert await _forgejo().get_actor_permission(_pr("forgejo"), "alice") == expected


async def test_forgejo_a_non_collaborator_is_none() -> None:
    with _patch_forgejo(lambda *a, **k: _FakeResp(status=404, json_data={})):
        assert await _forgejo().get_actor_permission(_pr("forgejo"), "stranger") == "none"


async def test_forgejo_an_unreadable_permission_is_unknown() -> None:
    def broken(method, url, **kw):
        raise RuntimeError("502")

    with _patch_forgejo(broken):
        assert await _forgejo().get_actor_permission(_pr("forgejo"), "alice") == "unknown"


async def test_forgejo_default_branch_and_its_failure_mode() -> None:
    with _patch_forgejo(lambda *a, **k: _FakeResp(json_data={"default_branch": "trunk"})):
        assert await _forgejo().get_default_branch(_pr("forgejo")) == "trunk"

    def broken(method, url, **kw):
        raise RuntimeError("down")

    with _patch_forgejo(broken):
        assert await _forgejo().get_default_branch(_pr("forgejo")) == ""


async def test_forgejo_creates_a_branch() -> None:
    calls: list[tuple[str, str, dict]] = []

    def handler(method, url, **kw):
        calls.append((method, url, kw.get("json") or {}))
        return _FakeResp(status=201, json_data={})

    with _patch_forgejo(handler):
        await _forgejo().create_branch(_pr("forgejo"), "mira/fix/pr-7/abc", "head123")
    method, url, body = calls[0]
    assert method == "POST" and url.endswith("/branches")
    assert body == {"new_branch_name": "mira/fix/pr-7/abc", "old_ref_name": "head123"}


async def test_forgejo_commits_each_file_with_its_previous_blob_sha() -> None:
    """The sha makes the write a compare-and-swap rather than a blind overwrite."""
    calls: list[tuple[str, str, dict]] = []

    def handler(method, url, **kw):
        calls.append((method, url, kw.get("json") or {}))
        if method == "GET":
            return _FakeResp(
                json_data={"sha": "blob1", "content": base64.b64encode(b"old").decode()}
            )
        return _FakeResp(status=200, json_data={"commit": {"sha": "newsha"}})

    with _patch_forgejo(handler):
        sha = await _forgejo().commit_files(_pr("forgejo"), "b", {"a.py": "new"}, "fix: guard")
    assert sha == "newsha"
    writes = [call for call in calls if call[0] == "POST"]
    assert len(writes) == 1
    entry = writes[0][2]["files"][0]
    assert entry["operation"] == "update"
    assert entry["sha"] == "blob1"
    assert base64.b64decode(entry["content"]).decode() == "new"


async def test_forgejo_publishes_a_multi_file_patch_as_one_commit() -> None:
    """A validated patch is one unit; a sequence of per-file commits is not.

    Committing file by file means a call that fails halfway leaves the earlier
    files on the branch in a state nothing ever validated -- and in `pr_branch`
    mode that branch belongs to a contributor.
    """
    calls: list[tuple[str, str, dict]] = []

    def handler(method, url, **kw):
        calls.append((method, url, kw.get("json") or {}))
        if method == "GET":
            return _FakeResp(status=404, json_data={})
        return _FakeResp(status=201, json_data={"commit": {"sha": "newsha"}})

    with _patch_forgejo(handler):
        sha = await _forgejo().commit_files(
            _pr("forgejo"), "b", {"a.py": "x", "b.py": "y", "c.py": "z"}, "fix: guard"
        )
    assert sha == "newsha"
    writes = [call for call in calls if call[0] == "POST"]
    assert len(writes) == 1
    assert [entry["path"] for entry in writes[0][2]["files"]] == ["a.py", "b.py", "c.py"]
    assert {entry["operation"] for entry in writes[0][2]["files"]} == {"create"}


async def test_forgejo_will_not_call_an_unreadable_file_a_match() -> None:
    """`_file_sha` answers ("", "") for both "absent" and "the read failed"."""
    with _patch_forgejo(lambda *a, **k: _FakeResp(status=404, json_data={})):
        assert await _forgejo().files_match(_pr("forgejo"), "b", {"a.py": ""}) is False


async def test_forgejo_creates_a_file_that_does_not_exist_yet() -> None:
    calls: list[tuple[str, str, dict]] = []

    def handler(method, url, **kw):
        calls.append((method, url, kw.get("json") or {}))
        if method == "GET":
            return _FakeResp(status=404, json_data={})
        return _FakeResp(status=201, json_data={"commit": {"sha": "newsha"}})

    with _patch_forgejo(handler):
        await _forgejo().commit_files(_pr("forgejo"), "b", {"new.py": "x"}, "m")
    writes = [call for call in calls if call[0] in ("PUT", "POST")]
    assert writes[0][0] == "POST"
    assert "sha" not in writes[0][2]


async def test_forgejo_opens_a_pull_request() -> None:
    with _patch_forgejo(
        lambda *a, **k: _FakeResp(
            status=201, json_data={"number": 900, "html_url": "https://forge.example/pull/900"}
        )
    ):
        assert await _forgejo().create_pull_request(
            _pr("forgejo"), head="b", base="feature", title="t", body="b"
        ) == (900, "https://forge.example/pull/900")


async def test_forgejo_finds_an_existing_pull_request_by_head_ref() -> None:
    with _patch_forgejo(
        lambda *a, **k: _FakeResp(
            json_data=[
                {"number": 1, "head": {"ref": "other"}, "html_url": "u1"},
                {"number": 900, "head": {"ref": "b"}, "html_url": "u900"},
            ]
        )
    ):
        assert await _forgejo().find_open_pull_request(_pr("forgejo"), "b") == (900, "u900")


@pytest.mark.parametrize(
    ("full_name", "expected"),
    [("acme/app", False), ("mallory/app", True), ("", True)],
)
async def test_forgejo_fork_detection(full_name: str, expected: bool) -> None:
    with _patch_forgejo(
        lambda *a, **k: _FakeResp(json_data={"head": {"repo": {"full_name": full_name}}})
    ):
        assert await _forgejo().pr_head_is_fork(_pr("forgejo")) is expected


# ──────────────────────────────────────────────────── the shared guarantee ──


def test_no_provider_exposes_a_way_to_force_or_merge() -> None:
    """The guarantee is an absence, so it is asserted against the surface.

    Adding a `merge_pull_request` or a `force` parameter later fails here, and
    whoever adds it has to say why in a review.
    """
    for provider_class in (GitHubProvider, GitLabProvider, ForgejoProvider):
        names = dir(provider_class)
        assert not [name for name in names if "merge" in name.lower()]
        assert not [name for name in names if "force" in name.lower()]
        assert not [name for name in names if "delete_branch" in name.lower()]
