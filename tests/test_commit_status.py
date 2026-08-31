"""The commit status that says whether the review finished.

Three properties, and only the first is about colour.

*Green means the review ran.* A status that goes red because the model timed
out, or green because every file was excluded from review, is a status that
lies about the one thing it exists to report. Mira's failures are neutral and
say so in Mira's name; a pull request nothing looked at is not approved-looking.

*The pending status always gets settled.* "Reviewing…" that never resolves is
worse than no status at all: it is the state a required check would block on
forever. So the caller that catches a failed review reports it, and the engine
publishes its result before the gate, the checks and triage run.

*The name is one Mira excludes from the CI it reads back.* The gate hit this
first — a published status read back as a failing build regenerates itself on
every event — and a third context needs the same exclusion.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from mira.checks.models import mira_status_contexts
from mira.config import MiraConfig
from mira.core.commit_status import (
    STATUS_CONTEXT,
    ReviewStatusReporter,
    failed_status,
    finished_status,
    pending_status,
)
from mira.exceptions import ProviderError
from mira.models import PRInfo, ReviewComment, ReviewResult, Severity
from mira.providers.forgejo import ForgejoProvider
from mira.providers.github import GitHubProvider
from mira.providers.gitlab import GitLabProvider


def _config(**status_kwargs) -> MiraConfig:
    cfg = MiraConfig()
    for key, value in status_kwargs.items():
        setattr(cfg.review.status, key, value)
    return cfg


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


def _comment(severity: Severity) -> ReviewComment:
    return ReviewComment(
        path="a.py",
        line=1,
        end_line=None,
        severity=severity,
        category="bug",
        title="t",
        body="b",
        confidence=0.9,
    )


def _result(*severities: Severity, **kwargs) -> ReviewResult:
    return ReviewResult(
        comments=[_comment(s) for s in severities],
        reviewed_files=kwargs.pop("reviewed_files", 3),
        **kwargs,
    )


class _Provider:
    """A provider that records what it was asked to publish."""

    def __init__(self, reference: str = "1", error: Exception | None = None) -> None:
        self.calls: list[dict] = []
        self.reference = reference
        self.error = error

    async def publish_review_status(self, pr_info, **kwargs) -> str:
        self.calls.append(kwargs)
        if self.error:
            raise self.error
        return self.reference


# ───────────────────────────────────────────────────────── what it renders ──


def test_the_name_is_one_mira_reads_past() -> None:
    """Otherwise: red status → read back as failing CI → red status, forever."""
    assert STATUS_CONTEXT in mira_status_contexts()


def test_a_review_with_nothing_to_flag_is_green() -> None:
    status = finished_status(_result(), _config())
    assert status.state == "success"
    assert status.title == "No findings"
    assert "3 files" in status.summary


def test_a_blocker_is_red_by_default() -> None:
    status = finished_status(_result(Severity.BLOCKER, Severity.WARNING), _config())
    assert status.state == "failure"
    assert "1 blocker" in status.title
    assert "1 warning" in status.title


def test_warnings_alone_stay_green_by_default() -> None:
    """The default draws its line at the severity Mira calls unmergeable.

    Warnings are worth reading and are already inline; turning the check red
    for one teaches people that red means "Mira had opinions", which is how a
    status stops being read at all.
    """
    status = finished_status(_result(Severity.WARNING, Severity.SUGGESTION), _config())
    assert status.state == "success"
    assert "1 warning, 1 suggestion" in status.title.lower()


def test_the_line_can_be_moved_to_the_approval_ceiling() -> None:
    status = finished_status(_result(Severity.WARNING), _config(fail_on="above_ceiling"))
    assert status.state == "failure"
    # …and a finding at or below the ceiling is still green under that setting.
    assert finished_status(
        _result(Severity.SUGGESTION), _config(fail_on="above_ceiling")
    ).state == ("success")


def test_the_line_can_be_removed_entirely() -> None:
    status = finished_status(_result(Severity.BLOCKER), _config(fail_on="never"))
    assert status.state == "success"
    assert "1 blocker" in status.title


def test_a_partial_review_says_how_much_it_read() -> None:
    """Green on a pull request whose biggest file was skipped is true about
    what Mira read and misleading about what that means."""
    result = _result(reviewed_paths=["a.py"], skipped_paths=["big.py", "huge.py"])
    status = finished_status(result, _config())
    assert status.state == "success"
    assert "1 of 3 changed files" in status.summary
    assert "review-rest" in status.summary


def test_a_pull_request_nothing_looked_at_is_not_green() -> None:
    """Every file excluded is not the same answer as every file clean."""
    status = finished_status(_result(skipped_reason="All files matched exclusion rules"), _config())
    assert status.state == "neutral"
    assert "All files matched exclusion rules" in status.title


def test_a_failure_is_neutral_and_named_as_miras() -> None:
    status = failed_status(TimeoutError("upstream took too long"))
    assert status.state == "neutral"
    assert "could not finish" in status.title
    assert "Mira failure" in status.summary
    # The raw exception text is not republished: it can carry a URL with a
    # token in it, and this goes on a commit rather than into a log.
    assert "upstream took too long" not in status.summary
    assert "TimeoutError" in status.summary


def test_a_mira_error_uses_its_own_safe_message() -> None:
    status = failed_status(ProviderError("GitHub said no"))
    assert status.state == "neutral"
    assert status.summary.strip()


def test_pending_says_what_is_happening() -> None:
    status = pending_status()
    assert status.state == "pending"
    assert "Reviewing" in status.title


# ─────────────────────────────────────────────────────────── the reporter ──


async def test_the_reporter_announces_the_start_and_the_end() -> None:
    provider = _Provider()
    reporter = ReviewStatusReporter(provider, _config())
    await reporter.start(_pr())
    await reporter.finish(_pr(), _result())
    assert [call["state"] for call in provider.calls] == ["pending", "success"]
    assert {call["context"] for call in provider.calls} == {STATUS_CONTEXT}
    assert reporter.published is True


async def test_nothing_is_published_when_the_status_is_off() -> None:
    provider = _Provider()
    reporter = ReviewStatusReporter(provider, _config(enabled=False))
    await reporter.start(_pr())
    await reporter.finish(_pr(), _result())
    assert provider.calls == []


async def test_the_pending_half_can_be_switched_off_on_its_own() -> None:
    provider = _Provider()
    reporter = ReviewStatusReporter(provider, _config(pending=False))
    await reporter.start(_pr())
    await reporter.finish(_pr(), _result())
    assert [call["state"] for call in provider.calls] == ["success"]


async def test_a_dry_run_touches_nothing() -> None:
    provider = _Provider()
    reporter = ReviewStatusReporter(provider, _config(), dry_run=True)
    await reporter.start(_pr())
    await reporter.finish(_pr(), _result())
    assert provider.calls == []


async def test_a_refused_status_never_reaches_the_review() -> None:
    """The review already landed. A status is how it is announced, not what it
    is, and a provider refusing one must not be able to undo it."""
    provider = _Provider(error=ProviderError("checks:write missing"))
    reporter = ReviewStatusReporter(provider, _config())
    await reporter.start(_pr())
    await reporter.finish(_pr(), _result())
    assert reporter.published is False


async def test_a_provider_with_no_status_surface_is_not_an_error() -> None:
    reporter = ReviewStatusReporter(SimpleNamespace(), _config())
    await reporter.finish(_pr(), _result())
    assert reporter.published is False


async def test_a_provider_that_declines_is_recorded_as_not_published() -> None:
    """GitLab returns "" on purpose. That is an answer, not a failure."""
    provider = _Provider(reference="")
    reporter = ReviewStatusReporter(provider, _config())
    await reporter.finish(_pr(), _result())
    assert len(provider.calls) == 1
    assert reporter.published is False


async def test_a_failure_after_the_result_does_not_overwrite_it() -> None:
    """The gate, the checks and triage all run after the status is settled.

    One of them crashing is not a review failure, and rewriting a finished
    review as "could not finish" would be the wrong half of the truth.
    """
    provider = _Provider()
    reporter = ReviewStatusReporter(provider, _config())
    await reporter.finish(_pr(), _result())
    await reporter.failed(_pr(), RuntimeError("triage exploded"))
    assert [call["state"] for call in provider.calls] == ["success"]


async def test_a_failure_before_there_is_a_pull_request_publishes_nothing() -> None:
    provider = _Provider()
    reporter = ReviewStatusReporter(provider, _config())
    await reporter.failed(None, RuntimeError("get_pr_info blew up"))
    assert provider.calls == []


async def test_a_failed_review_settles_the_pending_status() -> None:
    provider = _Provider()
    reporter = ReviewStatusReporter(provider, _config())
    await reporter.start(_pr())
    await reporter.failed(_pr(), RuntimeError("boom"))
    assert [call["state"] for call in provider.calls] == ["pending", "neutral"]


# ──────────────────────────────────────────────────────────── the adapters ──


def _github_provider(existing=None, total=0):
    provider = GitHubProvider.__new__(GitHubProvider)
    repo = MagicMock()
    commit = MagicMock()
    commit.get_check_runs.return_value = [existing] if existing is not None else []
    repo.get_commit.return_value = commit
    repo.get_pull.return_value = MagicMock(head=SimpleNamespace(sha="head123"))
    repo.create_check_run.return_value = SimpleNamespace(id=99)
    github = MagicMock()
    github.get_repo.return_value = repo
    provider._github = github
    provider._token = "t"
    return provider, repo


@pytest.mark.parametrize(
    ("state", "expected"),
    [
        ("pending", ("in_progress", None)),
        ("success", ("completed", "success")),
        ("failure", ("completed", "failure")),
        ("neutral", ("completed", "neutral")),
    ],
)
async def test_github_maps_each_state_onto_a_check_run(state: str, expected: tuple) -> None:
    provider, repo = _github_provider()
    ref = await provider.publish_review_status(
        _pr(), context=STATUS_CONTEXT, state=state, title="t", summary="s"
    )
    assert ref == "99"
    kwargs = repo.create_check_run.call_args.kwargs
    assert kwargs["name"] == STATUS_CONTEXT
    assert kwargs["status"] == expected[0]
    assert kwargs.get("conclusion") == expected[1]


async def test_github_updates_the_run_it_already_published() -> None:
    """Two publishes per review. Creating a second run each time would leave a
    stale "in progress" entry in the Checks tab of every reviewed commit."""
    existing = MagicMock(id=7, status="completed", started_at=1)
    provider, repo = _github_provider(existing=existing)
    ref = await provider.publish_review_status(
        _pr(), context=STATUS_CONTEXT, state="success", title="t", summary="s"
    )
    assert ref == "7"
    assert repo.create_check_run.call_count == 0
    assert existing.edit.call_args.kwargs["conclusion"] == "success"


async def test_github_opens_a_new_run_rather_than_reopening_a_finished_one() -> None:
    """A second review of the same commit starts its own row.

    A completed check run keeps its conclusion. Reusing it for the pending
    state would show the *previous* review's colour next to "in progress"
    until this one lands, which is the previous answer wearing this run's
    label.
    """
    finished = MagicMock(id=7, status="completed", started_at=1)
    provider, repo = _github_provider(existing=finished)
    await provider.publish_review_status(
        _pr(), context=STATUS_CONTEXT, state="pending", title="Reviewing…", summary="s"
    )
    assert finished.edit.call_count == 0
    assert repo.create_check_run.call_args.kwargs["status"] == "in_progress"


async def test_github_picks_the_newest_run_when_there_are_several() -> None:
    """ "Newest first" is not a documented ordering, and editing the wrong row
    updates one nobody is looking at."""
    old = MagicMock(id=1, status="in_progress", started_at=10)
    new = MagicMock(id=2, status="in_progress", started_at=20)
    provider, repo = _github_provider()
    repo.get_commit.return_value.get_check_runs.return_value = [old, new]
    ref = await provider.publish_review_status(
        _pr(), context=STATUS_CONTEXT, state="success", title="t", summary="s"
    )
    assert ref == "2"
    assert old.edit.call_count == 0


async def test_github_falls_back_to_creating_when_the_lookup_fails() -> None:
    """A duplicated row is a smaller problem than a lost status."""
    provider, repo = _github_provider()
    repo.get_commit.side_effect = RuntimeError("checks:read missing")
    ref = await provider.publish_review_status(
        _pr(), context=STATUS_CONTEXT, state="success", title="t", summary="s"
    )
    assert ref == "99"
    assert repo.create_check_run.call_count == 1


async def test_github_reports_a_refused_status_rather_than_swallowing_it() -> None:
    """A token without `checks:write` is a fixable misconfiguration, and
    silence is how it stays unfixed."""
    provider, repo = _github_provider()
    repo.create_check_run.side_effect = RuntimeError("checks:write missing")
    with pytest.raises(ProviderError):
        await provider.publish_review_status(
            _pr(), context=STATUS_CONTEXT, state="success", title="t", summary="s"
        )


class _FakeResp:
    def __init__(self, status=201, json_data=None):
        self.status_code = status
        self._json = json_data or {}

    def json(self):
        return self._json


@pytest.mark.parametrize(
    ("state", "expected"),
    [("pending", "pending"), ("success", "success"), ("failure", "failure"), ("neutral", "error")],
)
async def test_forgejo_maps_a_failed_review_onto_error_not_success(
    state: str, expected: str
) -> None:
    """The gate posts `success` for its neutral because a dry run *is* a
    decision it withheld. A review that broke is not; Forgejo has `error` for
    exactly that, and using `success` there would be a lie with a green tick."""
    sent: list[dict] = []

    async def _request(self, method, url, **kwargs):
        sent.append(kwargs.get("json") or {})
        return _FakeResp(json_data={"id": 3})

    provider = ForgejoProvider.__new__(ForgejoProvider)
    provider._token = "t"
    provider._api = "https://forge.example/api/v1"
    with patch.object(ForgejoProvider, "_request", _request):
        ref = await provider.publish_review_status(
            _pr("forgejo"), context=STATUS_CONTEXT, state=state, title="t", summary="s"
        )
    assert ref == "3"
    assert sent[0]["state"] == expected
    assert sent[0]["context"] == STATUS_CONTEXT


async def test_forgejo_without_a_head_sha_publishes_nothing() -> None:
    provider = ForgejoProvider.__new__(ForgejoProvider)
    provider._token = "t"
    provider._api = "https://forge.example/api/v1"
    pr = _pr("forgejo")
    pr.head_sha = ""
    assert (
        await provider.publish_review_status(pr, context=STATUS_CONTEXT, state="success", title="t")
        == ""
    )


@pytest.mark.parametrize("state", ["pending", "success", "failure", "neutral"])
async def test_gitlab_publishes_no_review_status_either(state: str) -> None:
    """A GitLab commit status joins the head pipeline: a pending one would hold
    the merge request on a build Mira never runs, and a green one can satisfy
    the "pipelines must succeed" rule it was never asked to answer."""
    provider = GitLabProvider.__new__(GitLabProvider)
    assert (
        await provider.publish_review_status(
            _pr("gitlab"), context=STATUS_CONTEXT, state=state, title="t", summary="s"
        )
        == ""
    )
