"""Phase 4 — the provider adapters the gate depends on.

Each provider has to answer four questions truthfully: what CI says, who the
author is to this repository, which labels are on the PR, and whether it can
record an approval at all. The tests below fix the *mapping* — GitLab's numeric
access levels, Forgejo's commit statuses, GitHub's split between check runs and
legacy statuses — because a wrong mapping here is the one bug that turns into a
silent approval.

The other half is what happens when a read fails. Reads with a safe unknown
report it (CI, association). Reads whose emptiness would read as good news
(labels, review states) raise, so the gate records an `error` instead.
"""

from __future__ import annotations

import base64
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from mira.exceptions import ProviderError
from mira.gate.capabilities import for_provider
from mira.gate.codeowners import CODEOWNERS_LOCATIONS
from mira.gate.models import STATUS_CONTEXT
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


# ───────────────────────────────────────────────────────────── capabilities ──


def test_each_provider_declares_what_it_can_do() -> None:
    github = for_provider(GitHubProvider.__new__(GitHubProvider))
    gitlab = for_provider(GitLabProvider.__new__(GitLabProvider))
    forgejo = for_provider(ForgejoProvider.__new__(ForgejoProvider))

    assert github.can_approve and github.can_request_changes and github.can_publish_status
    # GitLab has approvals but no "request changes" review event, and the gate
    # says so rather than approximating one with a comment nothing reads.
    assert gitlab.can_approve and not gitlab.can_request_changes
    assert any("REQUEST_CHANGES" in note for note in gitlab.notes)
    assert forgejo.can_approve and forgejo.can_request_changes


def test_a_provider_that_declares_nothing_gets_nothing() -> None:
    assert for_provider(None).can_approve is False
    assert for_provider(SimpleNamespace()).can_approve is False


# ────────────────────────────────────────────────────────────────── GitHub ──


def _github_provider(commit=None, pull=None, issue=None):
    provider = GitHubProvider.__new__(GitHubProvider)
    repo = MagicMock()
    repo.get_commit.return_value = commit or MagicMock()
    repo.get_pull.return_value = pull or MagicMock(head=SimpleNamespace(sha="head123"))
    repo.get_issue.return_value = issue or MagicMock()
    github = MagicMock()
    github.get_repo.return_value = repo
    provider._github = github
    provider._token = "t"
    return provider, repo


def _check_run(name: str, status: str, conclusion: str | None):
    return SimpleNamespace(name=name, status=status, conclusion=conclusion)


def _status(context: str, state: str):
    return SimpleNamespace(context=context, state=state)


@pytest.mark.parametrize(
    "runs,statuses,expected",
    [
        ([_check_run("build", "completed", "success")], [], "success"),
        ([_check_run("build", "completed", "skipped")], [], "success"),
        ([_check_run("build", "in_progress", None)], [], "pending"),
        ([_check_run("build", "completed", "failure")], [], "failure"),
        # An outcome we do not recognise is a failure, never a pass.
        ([_check_run("build", "completed", "action_required")], [], "failure"),
        ([], [_status("ci/legacy", "success")], "success"),
        ([], [_status("ci/legacy", "pending")], "pending"),
        ([], [_status("ci/legacy", "error")], "failure"),
        # Nothing ever ran: "none" is not "green".
        ([], [], "none"),
        (
            [_check_run("build", "completed", "success")],
            [_status("ci/legacy", "failure")],
            "failure",
        ),
    ],
)
async def test_github_ci_mapping(runs, statuses, expected) -> None:
    commit = MagicMock()
    commit.get_check_runs.return_value = runs
    commit.get_statuses.return_value = statuses
    provider, _ = _github_provider(commit=commit)
    assert (await provider.get_ci_state(_pr())).state == expected


async def test_github_ci_takes_the_newest_entry_per_context() -> None:
    commit = MagicMock()
    commit.get_check_runs.return_value = []
    # Chronological, newest first — the older failure must not resurrect.
    commit.get_statuses.return_value = [_status("ci", "success"), _status("ci", "failure")]
    provider, _ = _github_provider(commit=commit)
    state = await provider.get_ci_state(_pr())
    assert state.state == "success"
    assert state.total == 1


async def test_github_ci_that_cannot_be_read_is_unknown_not_green() -> None:
    commit = MagicMock()
    commit.get_check_runs.side_effect = RuntimeError("403")
    provider, _ = _github_provider(commit=commit)
    assert (await provider.get_ci_state(_pr())).state == "unknown"


async def test_github_labels_raise_rather_than_read_as_absent() -> None:
    issue = MagicMock()
    issue.get_labels.side_effect = RuntimeError("boom")
    provider, _ = _github_provider(issue=issue)
    with pytest.raises(ProviderError, match="labels"):
        await provider.get_pr_labels(_pr())


async def test_github_review_states_raise_rather_than_read_as_silence() -> None:
    pull = MagicMock()
    pull.get_reviews.side_effect = RuntimeError("boom")
    provider, _ = _github_provider(pull=pull)
    with pytest.raises(ProviderError, match="review states"):
        await provider.get_review_states(_pr())


async def test_github_association_defaults_to_unknown() -> None:
    pull = MagicMock()
    pull.raw_data = {}
    provider, _ = _github_provider(pull=pull)
    assert await provider.get_author_association(_pr()) == "UNKNOWN"
    pull.raw_data = {"author_association": "member"}
    assert await provider.get_author_association(_pr()) == "MEMBER"


async def test_github_publishes_the_gate_status_under_a_stable_name() -> None:
    provider, repo = _github_provider()
    repo.create_check_run.return_value = SimpleNamespace(id=99)
    ref = await provider.publish_gate_status(
        _pr(), context="mira/merge-gate", conclusion="neutral", title="Would approve", summary="s"
    )
    assert ref == "99"
    kwargs = repo.create_check_run.call_args.kwargs
    assert kwargs["name"] == "mira/merge-gate"
    assert kwargs["conclusion"] == "neutral"


async def test_github_status_failure_is_reported_not_swallowed() -> None:
    provider, repo = _github_provider()
    repo.create_check_run.side_effect = RuntimeError("checks:write missing")
    with pytest.raises(ProviderError):
        await provider.publish_gate_status(
            _pr(), context="c", conclusion="neutral", title="t", summary="s"
        )


async def test_github_change_stats_come_from_the_files_api() -> None:
    pull = MagicMock(head=SimpleNamespace(sha="head123"))
    pull.get_files.return_value = [
        SimpleNamespace(filename="src/a.py", additions=10, deletions=2),
        SimpleNamespace(filename="src/b.py", additions=5, deletions=0),
    ]
    provider, _ = _github_provider(pull=pull)
    changes = await provider.get_pr_change_stats(_pr())
    assert [change.path for change in changes] == ["src/a.py", "src/b.py"]
    assert [(c.added_lines, c.deleted_lines) for c in changes] == [(10, 2), (5, 0)]


# ────────────────────────────────────────────────────────────────── GitLab ──


def _gitlab() -> GitLabProvider:
    provider = GitLabProvider.__new__(GitLabProvider)
    provider._token = "t"
    provider._api = "https://gitlab.example.com/api/v4"
    provider._username = "bot"
    return provider


@pytest.mark.parametrize(
    "status,expected",
    [
        ("success", "success"),
        ("manual", "success"),
        ("running", "pending"),
        ("pending", "pending"),
        ("failed", "failure"),
        ("canceled", "failure"),
        ("weird", "unknown"),
    ],
)
async def test_gitlab_pipeline_mapping(status: str, expected: str) -> None:
    def handler(method, url, **kw):
        return _FakeResp(json_data={"head_pipeline": {"id": 1, "status": status}})

    with _patch_gitlab(handler):
        assert (await _gitlab().get_ci_state(_pr("gitlab"))).state == expected


async def test_gitlab_no_pipeline_is_none_not_success() -> None:
    with _patch_gitlab(lambda *a, **kw: _FakeResp(json_data={})):
        assert (await _gitlab().get_ci_state(_pr("gitlab"))).state == "none"


@pytest.mark.parametrize(
    "access_level,expected",
    [(50, "OWNER"), (40, "MEMBER"), (30, "COLLABORATOR"), (20, "CONTRIBUTOR"), (0, "NONE")],
)
async def test_gitlab_access_levels_map_to_associations(access_level, expected) -> None:
    def handler(method, url, **kw):
        return _FakeResp(json_data=[{"username": "alice", "access_level": access_level}])

    with _patch_gitlab(handler):
        assert await _gitlab().get_author_association(_pr("gitlab")) == expected


async def test_gitlab_unreadable_membership_is_unknown() -> None:
    def handler(method, url, **kw):
        return _FakeResp(status=500, text="nope")

    with _patch_gitlab(handler):
        assert await _gitlab().get_author_association(_pr("gitlab")) == "UNKNOWN"


async def test_gitlab_refuses_request_changes_rather_than_faking_it() -> None:
    calls: list[str] = []

    def handler(method, url, **kw):
        calls.append(url)
        return _FakeResp(status=201, json_data={})

    with _patch_gitlab(handler):
        assert await _gitlab().submit_verdict(_pr("gitlab"), "REQUEST_CHANGES", "body") is False
    assert calls == []


async def test_gitlab_approval_degrades_when_the_tier_refuses() -> None:
    def handler(method, url, **kw):
        if url.endswith("/approve"):
            return _FakeResp(status=403, text="not available on this plan")
        return _FakeResp(json_data={})

    with _patch_gitlab(handler):
        assert await _gitlab().submit_verdict(_pr("gitlab"), "APPROVE", "body") is False


async def test_gitlab_approval_succeeds_when_the_api_accepts_it() -> None:
    seen: list[str] = []

    def handler(method, url, **kw):
        seen.append(f"{method} {url}")
        return _FakeResp(status=201, json_data={"id": 1})

    with _patch_gitlab(handler):
        assert await _gitlab().submit_verdict(_pr("gitlab"), "APPROVE", "") is True
    assert any(entry.endswith("/approve") for entry in seen)


async def test_gitlab_labels_raise_rather_than_read_as_absent() -> None:
    def handler(method, url, **kw):
        return _FakeResp(status=500, text="boom")

    with _patch_gitlab(handler), pytest.raises(ProviderError, match="labels"):
        await _gitlab().get_pr_labels(_pr("gitlab"))


async def test_gitlab_publishes_a_commit_status() -> None:
    captured: dict = {}

    def handler(method, url, **kw):
        captured["url"] = url
        captured["params"] = kw.get("params")
        return _FakeResp(status=201, json_data={"id": 5})

    with _patch_gitlab(handler):
        ref = await _gitlab().publish_gate_status(
            _pr("gitlab"), context="mira/merge-gate", conclusion="failure", title="No", summary="s"
        )
    assert ref == "5"
    assert "/statuses/head123" in captured["url"]
    assert captured["params"]["state"] == "failed"
    assert captured["params"]["name"] == "mira/merge-gate"


# ───────────────────────────────────────────────────────────────── Forgejo ──


def _forgejo() -> ForgejoProvider:
    provider = ForgejoProvider.__new__(ForgejoProvider)
    provider._token = "t"
    provider._api = "https://forgejo.example.com/api/v1"
    provider._username = "bot"
    return provider


@pytest.mark.parametrize(
    "statuses,expected",
    [
        ([{"context": "ci", "status": "success"}], "success"),
        ([{"context": "ci", "status": "pending"}], "pending"),
        ([{"context": "ci", "status": "failure"}], "failure"),
        ([], "none"),
        (
            [{"context": "ci", "status": "success"}, {"context": "lint", "status": "failure"}],
            "failure",
        ),
    ],
)
async def test_forgejo_commit_status_mapping(statuses, expected) -> None:
    def handler(method, url, **kw):
        return _FakeResp(json_data=statuses)

    with _patch_forgejo(handler):
        assert (await _forgejo().get_ci_state(_pr("forgejo"))).state == expected


async def test_forgejo_unreadable_status_is_unknown() -> None:
    def handler(method, url, **kw):
        return _FakeResp(status=500, text="boom")

    with _patch_forgejo(handler):
        assert (await _forgejo().get_ci_state(_pr("forgejo"))).state == "unknown"


@pytest.mark.parametrize(
    "permission,expected",
    [
        ("owner", "OWNER"),
        ("admin", "MEMBER"),
        ("write", "COLLABORATOR"),
        ("read", "CONTRIBUTOR"),
        ("none", "NONE"),
    ],
)
async def test_forgejo_permissions_map_to_associations(permission, expected) -> None:
    def handler(method, url, **kw):
        return _FakeResp(json_data={"permission": permission})

    with _patch_forgejo(handler):
        assert await _forgejo().get_author_association(_pr("forgejo")) == expected


async def test_forgejo_unknown_collaborator_is_none() -> None:
    def handler(method, url, **kw):
        return _FakeResp(status=404, json_data={})

    with _patch_forgejo(handler):
        assert await _forgejo().get_author_association(_pr("forgejo")) == "NONE"


@pytest.mark.parametrize("event", ["APPROVE", "REQUEST_CHANGES"])
async def test_forgejo_submits_real_review_events(event: str) -> None:
    captured: dict = {}

    def handler(method, url, **kw):
        captured["url"] = url
        captured["json"] = kw.get("json")
        return _FakeResp(status=201, json_data={"id": 1})

    with _patch_forgejo(handler):
        assert await _forgejo().submit_verdict(_pr("forgejo"), event, "body") is True
    assert captured["url"].endswith("/reviews")
    assert captured["json"]["event"] == event


async def test_forgejo_refused_verdict_degrades() -> None:
    def handler(method, url, **kw):
        return _FakeResp(status=422, text="cannot approve your own pull request")

    with _patch_forgejo(handler):
        assert await _forgejo().submit_verdict(_pr("forgejo"), "APPROVE", "body") is False


async def test_forgejo_review_states_raise_rather_than_read_as_silence() -> None:
    def handler(method, url, **kw):
        raise RuntimeError("boom")

    with _patch_forgejo(handler), pytest.raises(ProviderError, match="reviews"):
        await _forgejo().get_review_states(_pr("forgejo"))


async def test_forgejo_publishes_a_commit_status() -> None:
    captured: dict = {}

    def handler(method, url, **kw):
        captured["url"] = url
        captured["json"] = kw.get("json")
        return _FakeResp(status=201, json_data={"id": 7})

    with _patch_forgejo(handler):
        ref = await _forgejo().publish_gate_status(
            _pr("forgejo"),
            context="mira/merge-gate",
            conclusion="neutral",
            title="Would approve (dry run)",
            summary="s",
        )
    assert ref == "7"
    assert "/statuses/head123" in captured["url"]
    # No neutral state exists, so a dry run is a success whose description says
    # it is a dry run — never a failure for a PR the gate would have approved.
    assert captured["json"]["state"] == "success"
    assert "dry run" in captured["json"]["description"].lower()


# ───────────────────────────────── regressions from the pre-merge review ──


async def test_github_ci_ignores_the_gates_own_check_run() -> None:
    """Counting it would let the gate read its own verdict back as a build.

    It would also change the check count on every pass, which changes the
    inputs digest, which manufactures a fresh decision row each time.
    """
    commit = MagicMock()
    commit.get_check_runs.return_value = [
        _check_run("build", "completed", "success"),
        _check_run(STATUS_CONTEXT, "completed", "failure"),
    ]
    commit.get_statuses.return_value = [_status(STATUS_CONTEXT, "failure")]
    provider, _ = _github_provider(commit=commit)
    state = await provider.get_ci_state(_pr())
    assert state.state == "success"
    assert state.total == 1


async def test_forgejo_ci_ignores_the_gates_own_status() -> None:
    def handler(method, url, **kw):
        return _FakeResp(
            json_data=[
                {"context": "ci", "status": "success"},
                {"context": STATUS_CONTEXT, "status": "failure"},
            ]
        )

    with _patch_forgejo(handler):
        state = await _forgejo().get_ci_state(_pr("forgejo"))
    assert state.state == "success"
    assert state.total == 1


@pytest.mark.parametrize("status", [500, 502])
async def test_gitlab_codeowners_failure_raises_rather_than_reading_as_absent(
    status: int,
) -> None:
    """ "No owners" and "we could not check" are different answers."""

    def handler(method, url, **kw):
        return _FakeResp(status=status, text="upstream is down")

    with _patch_gitlab(handler), pytest.raises(ProviderError, match="CODEOWNERS"):
        await _gitlab().get_codeowners(_pr("gitlab"))


async def test_gitlab_codeowners_absent_is_reported_as_absent() -> None:
    def handler(method, url, **kw):
        return _FakeResp(status=404, text="")

    with _patch_gitlab(handler):
        assert await _gitlab().get_codeowners(_pr("gitlab")) == ("", "")


async def test_gitlab_codeowners_is_read_from_the_head_ref() -> None:
    def handler(method, url, **kw):
        if "CODEOWNERS" in url:
            return _FakeResp(status=200, text="src/**  @backend\n")
        return _FakeResp(status=404, text="")

    with _patch_gitlab(handler):
        path, content = await _gitlab().get_codeowners(_pr("gitlab"))
    assert path == "CODEOWNERS"
    assert "@backend" in content


@pytest.mark.parametrize("status", [500, 502])
async def test_forgejo_codeowners_failure_raises_rather_than_reading_as_absent(
    status: int,
) -> None:
    def handler(method, url, **kw):
        return _FakeResp(status=status, text="upstream is down")

    with _patch_forgejo(handler), pytest.raises(ProviderError, match="CODEOWNERS"):
        await _forgejo().get_codeowners(_pr("forgejo"))


async def test_forgejo_codeowners_absent_is_reported_as_absent() -> None:
    def handler(method, url, **kw):
        return _FakeResp(status=404, json_data={})

    with _patch_forgejo(handler):
        assert await _forgejo().get_codeowners(_pr("forgejo")) == ("", "")


async def test_forgejo_codeowners_decodes_the_contents_api() -> None:
    encoded = base64.b64encode(b"src/**  @backend\n").decode()

    def handler(method, url, **kw):
        return _FakeResp(status=200, json_data={"content": encoded})

    with _patch_forgejo(handler):
        path, content = await _forgejo().get_codeowners(_pr("forgejo"))
    assert path in CODEOWNERS_LOCATIONS["forgejo"]
    assert "@backend" in content
