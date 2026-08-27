"""Phase 6 — the provider adapters the checks depend on, and their parity.

Three providers answer the same two questions for the checks: what is this
issue, and what exactly failed in CI. The mapping is fixed here for each of
them, because a wrong mapping is the bug that turns an outage into a wave of
violations across every open pull request.

The parity that matters is not that all three do the same thing — they cannot;
Forgejo has no job logs and GitLab must not publish a status. It is that each
one *declares* what it cannot do, and that the checks then say so instead of
guessing. So the capability table is asserted alongside the behaviour, and the
narrowing rule is asserted directly: a provider may claim less than its
platform and may never claim more.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from github import GithubException

from mira.checks.capabilities import (
    FORGEJO_CAPABILITIES,
    GITHUB_CAPABILITIES,
    GITLAB_CAPABILITIES,
    NO_CAPABILITIES,
    CheckCapabilities,
    for_platform,
    for_provider,
    narrow,
)
from mira.checks.models import STATUS_CONTEXT, mira_status_contexts
from mira.gate.models import STATUS_CONTEXT as GATE_STATUS_CONTEXT
from mira.models import PRInfo
from mira.providers.base import BaseProvider
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

    for capability in (github, gitlab, forgejo):
        assert capability.can_read_issues
        assert capability.can_read_ci

    assert github.can_read_ci_logs and github.can_publish_status
    # A GitLab commit status joins the head pipeline, so publishing one would
    # corrupt the CI signal these checks read back — and on a project with
    # "pipelines must succeed" could satisfy the rule a failing check refused.
    assert gitlab.can_publish_status is False
    assert any("pipeline" in note for note in gitlab.notes)
    # Forgejo reports CI as commit statuses, which carry no job output.
    assert forgejo.can_read_ci_logs is False
    assert any("no job output" in note for note in forgejo.notes)


def test_a_provider_that_declares_nothing_gets_nothing() -> None:
    assert for_provider(None) is NO_CAPABILITIES
    assert for_provider(SimpleNamespace()).can_read_issues is False


def test_a_broken_capability_method_degrades_rather_than_widening() -> None:
    class _Broken:
        def checks_capabilities(self):
            raise RuntimeError("boom")

    assert for_provider(_Broken()).can_read_issues is False


def test_a_provider_may_narrow_its_platform_and_never_widen_it() -> None:
    """A token with reduced scopes narrows; a claim beyond the table does not."""
    reduced = narrow(
        CheckCapabilities(provider="github", can_read_issues=False, can_read_ci=True),
        GITHUB_CAPABILITIES,
    )
    assert reduced.can_read_issues is False
    assert reduced.can_read_ci is True

    overreaching = narrow(
        CheckCapabilities(
            provider="gitlab",
            can_read_issues=True,
            can_read_ci=True,
            can_publish_status=True,
        ),
        GITLAB_CAPABILITIES,
    )
    assert overreaching.can_publish_status is False


def test_an_unknown_platform_degrades() -> None:
    assert for_platform("bitbucket") is NO_CAPABILITIES
    assert for_platform("github") is GITHUB_CAPABILITIES
    assert for_platform("forgejo") is FORGEJO_CAPABILITIES


def test_the_base_provider_reports_ignorance_rather_than_good_news() -> None:
    """Adding a provider degrades the checks; it never weakens them."""

    class _Bare(BaseProvider):
        def __init__(self) -> None:  # pragma: no cover - never constructed fully
            pass

        async def get_pr_info(self, pr_url):
            raise NotImplementedError

        async def get_pr_diff(self, pr_info):
            raise NotImplementedError

        async def post_review(self, pr_info, result, bot_name="miracodeai"):
            raise NotImplementedError

        async def post_comment(self, pr_info, body):
            raise NotImplementedError

        async def find_bot_comment(self, pr_info, marker):
            raise NotImplementedError

        async def update_comment(self, pr_info, comment_id, body):
            raise NotImplementedError

        async def resolve_outdated_review_threads(self, pr_info):
            raise NotImplementedError

    provider = _Bare()
    assert provider.checks_capabilities() is NO_CAPABILITIES


async def test_the_base_issue_reader_refuses_rather_than_returning_absence() -> None:
    """`None` means "no such issue" — a provider that cannot look must raise."""

    class _Bare(BaseProvider):
        def __init__(self) -> None:
            pass

        async def get_pr_info(self, pr_url):
            raise NotImplementedError

        async def get_pr_diff(self, pr_info):
            raise NotImplementedError

        async def post_review(self, pr_info, result, bot_name="miracodeai"):
            raise NotImplementedError

        async def post_comment(self, pr_info, body):
            raise NotImplementedError

        async def find_bot_comment(self, pr_info, marker):
            raise NotImplementedError

        async def update_comment(self, pr_info, comment_id, body):
            raise NotImplementedError

        async def resolve_outdated_review_threads(self, pr_info):
            raise NotImplementedError

    with pytest.raises(NotImplementedError):
        await _Bare().get_issue(_pr(), 1)


def test_mira_never_reads_its_own_published_status_back_as_ci() -> None:
    """Without this, a red check status becomes a failing build on the next pass."""
    contexts = mira_status_contexts()
    assert STATUS_CONTEXT in contexts
    assert GATE_STATUS_CONTEXT in contexts


# ────────────────────────────────────────────────────────────────── GitHub ──


def _github_provider(commit=None, repo_overrides=None):
    provider = GitHubProvider.__new__(GitHubProvider)
    repo = MagicMock()
    repo.get_commit.return_value = commit or MagicMock()
    repo.get_pull.return_value = MagicMock(head=SimpleNamespace(sha="head123"))
    for name, value in (repo_overrides or {}).items():
        setattr(repo, name, value)
    github = MagicMock()
    github.get_repo.return_value = repo
    provider._github = github
    provider._token = "t"
    return provider


async def test_github_reads_an_issue() -> None:
    issue = MagicMock(
        number=42,
        title="Ingest is unbounded",
        body="## Acceptance criteria\n\n- [ ] rejects over 100 rps",
        state="open",
        html_url="https://github.com/acme/app/issues/42",
        labels=[SimpleNamespace(name="bug")],
    )
    provider = _github_provider(repo_overrides={"get_issue": MagicMock(return_value=issue)})
    result = await provider.get_issue(_pr(), 42)
    assert result is not None
    assert result.number == 42
    assert result.title == "Ingest is unbounded"
    assert result.labels == ["bug"]


async def test_github_reports_a_missing_issue_as_none() -> None:
    def _raise(**_kwargs):
        raise GithubException(404, {"message": "Not Found"}, {})

    provider = _github_provider(repo_overrides={"get_issue": MagicMock(side_effect=_raise)})
    assert await provider.get_issue(_pr(), 404) is None


async def test_github_propagates_anything_that_is_not_a_404() -> None:
    """A revoked token is not a missing issue, and must not read as one."""

    def _raise(**_kwargs):
        raise GithubException(403, {"message": "Bad credentials"}, {})

    provider = _github_provider(repo_overrides={"get_issue": MagicMock(side_effect=_raise)})
    with pytest.raises(GithubException):
        await provider.get_issue(_pr(), 1)


async def test_github_ci_failures_quote_the_check_runs_own_output() -> None:
    failing = MagicMock(
        name_="build",
        status="completed",
        conclusion="failure",
        id=99,
        html_url="https://github.com/acme/app/runs/99",
        output=SimpleNamespace(
            title="pytest", summary="1 failed", text="FAILED tests/test_x.py::test_y"
        ),
    )
    failing.name = "build"
    passing = MagicMock(status="completed", conclusion="success")
    passing.name = "lint"
    commit = MagicMock()
    commit.get_check_runs.return_value = [failing, passing]
    commit.get_statuses.return_value = []

    failures = await _github_provider(commit=commit).get_ci_failures(_pr())
    assert len(failures) == 1
    assert failures[0].name == "build"
    assert failures[0].step == "pytest"
    assert "FAILED tests/test_x.py::test_y" in failures[0].excerpt
    assert failures[0].log_unavailable is False


async def test_github_reports_a_failing_job_that_published_nothing() -> None:
    """ "This job failed and told us nothing" beats quoting an empty string."""
    failing = MagicMock(
        status="completed",
        conclusion="failure",
        id=1,
        html_url="",
        output=SimpleNamespace(title="", summary="", text=""),
    )
    failing.name = "deploy"
    commit = MagicMock()
    commit.get_check_runs.return_value = [failing]
    commit.get_statuses.return_value = []

    failures = await _github_provider(commit=commit).get_ci_failures(_pr())
    assert failures[0].log_unavailable is True


async def test_github_never_reports_its_own_status_as_a_failing_job() -> None:
    own = MagicMock(status="completed", conclusion="failure", output=None)
    own.name = STATUS_CONTEXT
    commit = MagicMock()
    commit.get_check_runs.return_value = [own]
    commit.get_statuses.return_value = []

    assert await _github_provider(commit=commit).get_ci_failures(_pr()) == []


async def test_github_ci_state_ignores_both_of_miras_own_contexts() -> None:
    gate_status = MagicMock(status="completed", conclusion="failure", output=None)
    gate_status.name = GATE_STATUS_CONTEXT
    checks_status = MagicMock(status="completed", conclusion="failure", output=None)
    checks_status.name = STATUS_CONTEXT
    real = MagicMock(status="completed", conclusion="success", output=None)
    real.name = "build"
    commit = MagicMock()
    commit.get_check_runs.return_value = [gate_status, checks_status, real]
    commit.get_statuses.return_value = []

    state = await _github_provider(commit=commit).get_ci_state(_pr())
    assert state.state == "success"
    assert state.total == 1


async def test_github_caps_the_jobs_it_returns() -> None:
    runs = []
    for index in range(10):
        run = MagicMock(
            status="completed",
            conclusion="failure",
            id=index,
            html_url="",
            output=SimpleNamespace(title="", summary=f"job {index}", text=""),
        )
        run.name = f"job-{index}"
        runs.append(run)
    commit = MagicMock()
    commit.get_check_runs.return_value = runs
    commit.get_statuses.return_value = []

    failures = await _github_provider(commit=commit).get_ci_failures(_pr(), max_jobs=2)
    assert len(failures) == 2


# ────────────────────────────────────────────────────────────────── GitLab ──


def _gitlab_provider():
    provider = GitLabProvider.__new__(GitLabProvider)
    provider._token = "t"
    provider._api = "https://gitlab.example.com/api/v4"
    provider._username = "bot"
    return provider


async def test_gitlab_reads_an_issue() -> None:
    def handler(method, url, **_kw):
        assert url.endswith("/issues/42")
        return _FakeResp(
            200,
            {
                "iid": 42,
                "title": "Ingest is unbounded",
                "description": "body",
                "state": "opened",
                "web_url": "https://gitlab.example.com/acme/app/-/issues/42",
                "labels": ["bug"],
            },
        )

    with _patch_gitlab(handler):
        issue = await _gitlab_provider().get_issue(_pr("gitlab"), 42)
    assert issue is not None
    assert issue.number == 42
    assert issue.labels == ["bug"]


async def test_gitlab_reports_a_missing_issue_as_none() -> None:
    with _patch_gitlab(lambda *a, **kw: _FakeResp(404, {}, "not found")):
        assert await _gitlab_provider().get_issue(_pr("gitlab"), 404) is None


async def test_gitlab_ci_failures_take_the_tail_of_each_trace() -> None:
    trace = "\n".join(f"line {n}" for n in range(500))

    def handler(method, url, **_kw):
        if url.endswith("/merge_requests/7"):
            return _FakeResp(200, {"head_pipeline": {"id": 9, "status": "failed"}})
        if "/pipelines/9/jobs" in url:
            return _FakeResp(
                200,
                [
                    {
                        "id": 11,
                        "name": "test",
                        "stage": "verify",
                        "status": "failed",
                        "web_url": "https://gitlab.example.com/-/jobs/11",
                    }
                ],
            )
        if url.endswith("/jobs/11/trace"):
            return _FakeResp(200, None, trace)
        raise AssertionError(f"unexpected call {url}")

    with _patch_gitlab(handler):
        failures = await _gitlab_provider().get_ci_failures(_pr("gitlab"), max_log_bytes=40)

    assert len(failures) == 1
    assert failures[0].name == "test"
    assert failures[0].step == "verify"
    assert failures[0].log_unavailable is False
    # The tail: a build failure is at the bottom of the log.
    assert "line 499" in failures[0].excerpt
    assert "line 0\n" not in failures[0].excerpt


async def test_gitlab_reports_a_job_whose_trace_it_cannot_read() -> None:
    def handler(method, url, **_kw):
        if url.endswith("/merge_requests/7"):
            return _FakeResp(200, {"head_pipeline": {"id": 9}})
        if "/pipelines/9/jobs" in url:
            return _FakeResp(200, [{"id": 11, "name": "test", "status": "failed"}])
        return _FakeResp(404, None, "")

    with _patch_gitlab(handler):
        failures = await _gitlab_provider().get_ci_failures(_pr("gitlab"))
    assert failures[0].log_unavailable is True


async def test_gitlab_with_no_pipeline_reports_no_failing_jobs() -> None:
    with _patch_gitlab(lambda *a, **kw: _FakeResp(200, {"head_pipeline": None})):
        assert await _gitlab_provider().get_ci_failures(_pr("gitlab")) == []


async def test_gitlab_publishes_no_check_status() -> None:
    """The declared capability, honoured by the method rather than only stated."""
    published = await _gitlab_provider().publish_checks_status(
        _pr("gitlab"), context=STATUS_CONTEXT, conclusion="failure", title="t", summary="s"
    )
    assert published == ""


# ───────────────────────────────────────────────────────────────── Forgejo ──


def _forgejo_provider():
    provider = ForgejoProvider.__new__(ForgejoProvider)
    provider._token = "t"
    provider._api = "https://forgejo.example.com/api/v1"
    provider._username = "bot"
    return provider


async def test_forgejo_reads_an_issue() -> None:
    def handler(method, url, **_kw):
        assert url.endswith("/issues/42")
        return _FakeResp(
            200,
            {
                "number": 42,
                "title": "Ingest is unbounded",
                "body": "body",
                "state": "open",
                "html_url": "https://forgejo.example.com/acme/app/issues/42",
                "labels": [{"name": "bug"}],
            },
        )

    with _patch_forgejo(handler):
        issue = await _forgejo_provider().get_issue(_pr("forgejo"), 42)
    assert issue is not None
    assert issue.labels == ["bug"]


async def test_forgejo_reports_a_missing_issue_as_none() -> None:
    with _patch_forgejo(lambda *a, **kw: _FakeResp(404, {}, "")):
        assert await _forgejo_provider().get_issue(_pr("forgejo"), 404) is None


async def test_forgejo_reports_a_failing_status_and_says_it_has_no_log() -> None:
    def handler(method, url, **_kw):
        return _FakeResp(
            200,
            [
                {
                    "context": "woodpecker",
                    "status": "failure",
                    "description": "",
                    "target_url": "https://ci.example.com/1",
                },
                {"context": "lint", "status": "success"},
            ],
        )

    with _patch_forgejo(handler):
        failures = await _forgejo_provider().get_ci_failures(_pr("forgejo"))

    assert len(failures) == 1
    assert failures[0].name == "woodpecker"
    assert failures[0].url == "https://ci.example.com/1"
    # Honest: the status carried no output, and the check says so rather than
    # quoting an empty string as evidence.
    assert failures[0].log_unavailable is True


async def test_forgejo_quotes_a_status_description_when_there_is_one() -> None:
    def handler(method, url, **_kw):
        return _FakeResp(
            200,
            [{"context": "woodpecker", "status": "failure", "description": "3 tests failed"}],
        )

    with _patch_forgejo(handler):
        failures = await _forgejo_provider().get_ci_failures(_pr("forgejo"))
    assert failures[0].excerpt == "3 tests failed"
    assert failures[0].log_unavailable is False


async def test_forgejo_never_reports_miras_own_statuses_as_ci() -> None:
    def handler(method, url, **_kw):
        return _FakeResp(
            200,
            [
                {"context": STATUS_CONTEXT, "status": "failure"},
                {"context": GATE_STATUS_CONTEXT, "status": "failure"},
                {"context": "woodpecker", "status": "success"},
            ],
        )

    with _patch_forgejo(handler):
        provider = _forgejo_provider()
        assert await provider.get_ci_failures(_pr("forgejo")) == []
        state = await provider.get_ci_state(_pr("forgejo"))
    assert state.state == "success"
    assert state.total == 1


# ────────────────────────────────────────────────────────────────── parity ──


@pytest.mark.parametrize(
    "provider_class",
    [GitHubProvider, GitLabProvider, ForgejoProvider],
)
def test_every_provider_implements_the_check_surface(provider_class) -> None:
    """Parity is not "all three behave alike" — it is "all three answer"."""
    for name in ("checks_capabilities", "get_issue", "get_ci_failures", "get_ci_state"):
        assert hasattr(provider_class, name), f"{provider_class.__name__} is missing {name}"

    capability = for_provider(provider_class.__new__(provider_class))
    assert capability.provider != "unknown"
    # And what it declares is never wider than the reviewed platform table.
    platform = for_platform(capability.provider)
    for flag in (
        "can_read_issues",
        "can_read_ci",
        "can_read_ci_logs",
        "can_publish_status",
    ):
        assert not (getattr(capability, flag) and not getattr(platform, flag))
