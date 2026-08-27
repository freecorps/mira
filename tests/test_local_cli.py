"""``mira local review``, end to end.

Driven through the Click runner with a stubbed model, because the contract this
phase publishes is the *command*: its exit codes, its JSON document, and the
fact that it neither writes to the checkout nor talks to a forge.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from click.testing import CliRunner

from mira.cli import main
from mira.exceptions import LLMError
from mira.llm.provider import LLMProvider
from mira.local.exit_codes import ExitCode
from mira.local.output import SCHEMA_VERSION
from tests.conftest import GitRepo

# Everything that costs a second model call is off: these tests are about the
# command, not about the review pipeline, which has its own suite.
QUIET_CONFIG = """
llm:
  model: anthropic/claude-sonnet-4-6
review:
  walkthrough: false
  security_pass: false
  self_critique: false
  code_context: false
  dependency_overlap: false
"""


def _response(path: str, line: int, severity: str = "blocker") -> str:
    return json.dumps(
        {
            "comments": [
                {
                    "path": path,
                    "line": line,
                    "end_line": None,
                    "severity": severity,
                    "category": "bug",
                    "title": "Reassigns the caller's argument",
                    "body": "The local name shadows the parameter, so the caller's value is lost.",
                    "confidence": 0.95,
                    "existing_code": "    total = 0",
                    "suggestion": "    running_total = 0",
                }
            ],
            "summary": "One shadowed argument.",
            "metadata": {"reviewed_files": 1, "skipped_reason": None},
        }
    )


_EMPTY_RESPONSE = json.dumps(
    {"comments": [], "summary": "Nothing to report.", "metadata": {"reviewed_files": 1}}
)


def _stub_llm(review_response: str, summary: str = "A short summary of the change.") -> MagicMock:
    llm = MagicMock(spec=LLMProvider)
    llm.review = AsyncMock(return_value=review_response)
    # `complete` is the summarisation call, not a second review. Giving it the
    # review's own JSON would make every assertion about the summary an
    # assertion about the raw response.
    llm.complete = AsyncMock(return_value=summary)
    llm.walkthrough = AsyncMock(return_value='{"summary": "s", "file_changes": []}')
    llm.count_tokens = MagicMock(return_value=100)
    llm.usage = {"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150}
    return llm


@pytest.fixture(autouse=True)
def no_ambient_model(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MIRA_MODEL", raising=False)


@pytest.fixture
def repo(git_repo: GitRepo) -> GitRepo:
    git_repo.write(".mira.yaml", QUIET_CONFIG)
    git_repo.commit("configure mira")
    return git_repo


def _dirty(repo: GitRepo) -> None:
    """A working-tree change whose one added line is line 2 of src/app.py."""
    repo.write("src/app.py", "def start(total):\n    total = 0\n    return total\n")


def _invoke(repo: GitRepo, *args: str, llm: MagicMock | None = None):
    runner = CliRunner()
    stub = llm if llm is not None else _stub_llm(_response("src/app.py", 2))
    with patch("mira.llm.create_llm", return_value=stub):
        return runner.invoke(
            main,
            ["local", "review", "--path", str(repo.root), *args],
            catch_exceptions=False,
        )


# ---------------------------------------------------------------------------
# Exit codes
# ---------------------------------------------------------------------------


class TestExitCodes:
    def test_a_clean_tree_exits_zero(self, repo: GitRepo) -> None:
        result = _invoke(repo)

        assert result.exit_code == ExitCode.OK
        assert "nothing to review" in result.output.lower()

    def test_a_blocker_exits_one(self, repo: GitRepo) -> None:
        _dirty(repo)

        result = _invoke(repo)

        assert result.exit_code == ExitCode.FINDINGS
        assert "Reassigns the caller's argument" in result.output

    def test_fail_on_raises_the_bar(self, repo: GitRepo) -> None:
        _dirty(repo)
        llm = _stub_llm(_response("src/app.py", 2, severity="suggestion"))

        below = _invoke(repo, llm=llm)
        assert below.exit_code == ExitCode.OK

        at = _invoke(
            repo, "--fail-on", "suggestion", llm=_stub_llm(_response("src/app.py", 2, "suggestion"))
        )
        assert at.exit_code == ExitCode.FINDINGS

    def test_fail_on_never_reports_without_failing(self, repo: GitRepo) -> None:
        _dirty(repo)

        result = _invoke(repo, "--fail-on", "never")

        assert result.exit_code == ExitCode.OK
        assert "Reassigns the caller's argument" in result.output

    def test_conflicting_modes_are_a_usage_error(self, repo: GitRepo) -> None:
        result = _invoke(repo, "--staged", "--range", "HEAD~1..HEAD")

        assert result.exit_code == ExitCode.USAGE

    def test_a_bad_range_is_a_usage_error(self, repo: GitRepo) -> None:
        result = _invoke(repo, "--range", "HEAD")

        assert result.exit_code == ExitCode.USAGE

    @pytest.mark.parametrize("mode", [("--range", "HEAD~1..HEAD"), ("--staged",)])
    def test_untracked_outside_the_working_tree_is_a_usage_error(
        self, repo: GitRepo, mode: tuple[str, ...]
    ) -> None:
        result = _invoke(repo, *mode, "--include-untracked")

        assert result.exit_code == ExitCode.USAGE

    def test_outside_a_repository_is_a_git_error(self, tmp_path) -> None:
        outside = tmp_path / "plain"
        outside.mkdir()
        runner = CliRunner()

        result = runner.invoke(
            main, ["local", "review", "--path", str(outside)], catch_exceptions=False
        )

        assert result.exit_code == ExitCode.GIT

    def test_an_unknown_revision_is_a_git_error(self, repo: GitRepo) -> None:
        result = _invoke(repo, "--range", "nosuchbranch..HEAD")

        assert result.exit_code == ExitCode.GIT

    def test_a_redirected_destination_is_a_config_error(self, repo: GitRepo) -> None:
        _dirty(repo)

        result = _invoke(repo, "--model", "openai/gpt-5")

        assert result.exit_code == ExitCode.CONFIG
        assert "Refusing to send" in result.output

    def test_the_environment_cannot_redirect_the_destination(
        self, repo: GitRepo, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # --model reads MIRA_MODEL, so the environment reaches the same guard
        # the flag does.
        monkeypatch.setenv("MIRA_MODEL", "openai/gpt-5")
        _dirty(repo)

        result = _invoke(repo)

        assert result.exit_code == ExitCode.CONFIG

    def test_an_unreachable_model_is_an_engine_error_not_a_clean_review(
        self, repo: GitRepo
    ) -> None:
        _dirty(repo)
        llm = _stub_llm(_EMPTY_RESPONSE)
        llm.review = AsyncMock(
            side_effect=LLMError("api_error", status=502, body="upstream unreachable")
        )

        result = _invoke(repo, llm=llm)

        assert result.exit_code == ExitCode.ENGINE
        assert "could not complete" in result.output
        assert "No issues found" not in result.output

    def test_the_exit_code_table_is_printable(self) -> None:
        result = CliRunner().invoke(main, ["local", "review", "--explain-exit-codes"])

        assert result.exit_code == 0
        for code in ExitCode:
            assert str(int(code)) in result.output


# ---------------------------------------------------------------------------
# Modes
# ---------------------------------------------------------------------------


class TestModes:
    def test_staged_reviews_the_index(self, repo: GitRepo) -> None:
        _dirty(repo)
        repo.git("add", "src/app.py")
        repo.write("README.md", "# widgets\n\nunstaged\n")

        result = _invoke(repo, "--staged", "--output", "json")

        payload = json.loads(result.output)
        assert payload["mode"] == "staged"
        assert [f["path"] for f in payload["changed_files"]] == ["src/app.py"]

    def test_a_range_reviews_the_commits(self, repo: GitRepo) -> None:
        base = repo.git("rev-parse", "HEAD").stdout.strip()
        _dirty(repo)
        repo.commit("shadow the argument")

        result = _invoke(repo, "--range", f"{base}..HEAD", "--output", "json")

        payload = json.loads(result.output)
        assert payload["mode"] == "range"
        assert payload["base"]["sha"] == base
        assert payload["comparison"] == f"{base}..HEAD"

    def test_untracked_files_are_reported_but_not_sent(self, repo: GitRepo) -> None:
        repo.write("scratch.py", "secret = 1\n")

        result = _invoke(repo, "--output", "json")

        payload = json.loads(result.output)
        assert payload["untracked"]["paths"] == ["scratch.py"]
        assert payload["untracked"]["included"] is False


# ---------------------------------------------------------------------------
# The JSON contract
# ---------------------------------------------------------------------------


class TestJsonOutput:
    def test_the_document_carries_every_documented_key(self, repo: GitRepo) -> None:
        _dirty(repo)

        result = _invoke(repo, "--output", "json")

        payload = json.loads(result.output)
        assert payload["schema_version"] == SCHEMA_VERSION
        assert set(payload) >= {
            "schema_version",
            "mode",
            "comparison",
            "repository",
            "base",
            "head",
            "destinations",
            "review",
            "changed_files",
            "untracked",
            "checks",
            "counts",
            "fail_on",
            "notes",
            "exit_code",
        }
        assert payload["exit_code"] == ExitCode.FINDINGS
        assert payload["counts"]["blocker"] == 1
        assert payload["repository"]["owner"] == "acme"
        assert payload["repository"]["repo"] == "widgets"
        assert payload["repository"]["platform"] == "github"

    def test_findings_use_the_same_shape_as_the_server_cli(self, repo: GitRepo) -> None:
        _dirty(repo)

        result = _invoke(repo, "--output", "json")

        finding = json.loads(result.output)["review"]["comments"][0]
        assert set(finding) == {
            "path",
            "line",
            "end_line",
            "severity",
            "category",
            "title",
            "body",
            "confidence",
            "suggestion",
        }
        assert finding["severity"] == "blocker"

    def test_two_identical_runs_produce_identical_documents(self, repo: GitRepo) -> None:
        _dirty(repo)

        first = _invoke(repo, "--output", "json").output
        second = _invoke(repo, "--output", "json").output

        assert first == second

    def test_the_document_is_ascii_so_any_console_can_carry_it(self, repo: GitRepo) -> None:
        # A console that is not UTF-8 is the default on Windows, and a report
        # that cannot be printed there is not a report. Non-ASCII reaches the
        # document from the model, not from the diff.
        _dirty(repo)
        response = json.loads(_response("src/app.py", 2))
        response["comments"][0]["title"] = "Réassigne l'argument — naïve"
        llm = _stub_llm(json.dumps(response))

        result = _invoke(repo, "--output", "json", llm=llm)

        result.output.encode("ascii")  # raises if the document is not ASCII
        payload = json.loads(result.output)
        # Escaped, not transliterated: the content survives the round trip.
        assert payload["review"]["comments"][0]["title"] == ("Réassigne l'argument — naïve")

    def test_the_local_frame_of_the_text_report_is_ascii(self, repo: GitRepo) -> None:
        _dirty(repo)

        result = _invoke(repo)

        frame = [
            line
            for line in result.output.splitlines()
            if line.startswith(("Mira -", "  repository", "  comparison", "  files", "Exit code"))
        ]
        assert frame
        "\n".join(frame).encode("ascii")

    def test_no_log_line_reaches_the_json_stream(self, repo: GitRepo) -> None:
        _dirty(repo)

        result = _invoke(repo, "--output", "json", "--verbose")

        json.loads(result.output)  # would raise if a log line landed on stdout


# ---------------------------------------------------------------------------
# Read-only, offline, unindexed
# ---------------------------------------------------------------------------


class TestReadOnlyAndOffline:
    def test_the_checkout_is_untouched(self, repo: GitRepo) -> None:
        _dirty(repo)
        before_status = repo.status()
        before_head = repo.git("rev-parse", "HEAD").stdout

        _invoke(repo)

        assert repo.status() == before_status
        assert repo.git("rev-parse", "HEAD").stdout == before_head

    def test_no_platform_client_is_constructed(self, repo: GitRepo) -> None:
        _dirty(repo)

        with patch("mira.providers.create_provider") as create_provider:
            _invoke(repo)

        create_provider.assert_not_called()

    def test_no_forge_credential_is_needed(
        self, repo: GitRepo, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The offline case that matters: a laptop on a train, with no token for
        # the forge and no route to it. A local review has to be a review, not
        # an authentication error.
        for name in ("GITHUB_TOKEN", "MIRA_GIT_TOKEN", "MIRA_GITLAB_TOKEN", "MIRA_FORGEJO_TOKEN"):
            monkeypatch.delenv(name, raising=False)
        _dirty(repo)

        result = _invoke(repo)

        assert result.exit_code == ExitCode.FINDINGS

    def test_the_engine_is_given_no_provider_at_all(self, repo: GitRepo) -> None:
        _dirty(repo)
        seen: list[object] = []
        from mira.core.engine import ReviewEngine

        original = ReviewEngine.__init__

        def record(self, *args, **kwargs):  # type: ignore[no-untyped-def]
            original(self, *args, **kwargs)
            seen.append(self.provider)

        with patch.object(ReviewEngine, "__init__", record):
            _invoke(repo)

        assert seen == [None]

    def test_an_unindexed_repository_says_so_rather_than_failing(self, git_repo: GitRepo) -> None:
        # `code_context` left on, and no index exists for acme/widgets.
        git_repo.write(
            ".mira.yaml",
            "review:\n  walkthrough: false\n  security_pass: false\n"
            "  self_critique: false\n  dependency_overlap: false\n",
        )
        git_repo.commit("configure mira")
        _dirty(git_repo)

        result = _invoke(git_repo, "--output", "json")

        payload = json.loads(result.output)
        assert any("no index on this machine" in note for note in payload["notes"])

    def test_a_checkout_with_no_remote_reviews_anyway(self, repo: GitRepo) -> None:
        repo.git("remote", "remove", "origin")
        _dirty(repo)

        result = _invoke(repo, "--output", "json")

        payload = json.loads(result.output)
        assert payload["repository"]["identified"] is False
        assert any("no remote" in note for note in payload["notes"])
        assert payload["review"]["comments"]

    def test_a_stated_slug_scopes_an_unremoted_checkout(self, repo: GitRepo) -> None:
        repo.git("remote", "remove", "origin")
        _dirty(repo)

        result = _invoke(repo, "--repo", "acme/widgets", "--output", "json")

        payload = json.loads(result.output)
        assert payload["repository"]["owner"] == "acme"
        assert payload["repository"]["identified"] is True


# ---------------------------------------------------------------------------
# Pre-merge checks
# ---------------------------------------------------------------------------


class TestChecks:
    def test_checks_are_absent_when_the_repository_has_them_off(self, repo: GitRepo) -> None:
        _dirty(repo)

        payload = json.loads(_invoke(repo, "--output", "json").output)

        assert payload["checks"] is None

    def test_checks_run_when_the_repository_turns_them_on(self, repo: GitRepo) -> None:
        repo.write(".mira.yaml", QUIET_CONFIG + "checks:\n  enabled: true\n")
        repo.commit("turn checks on")
        _dirty(repo)

        payload = json.loads(_invoke(repo, "--output", "json").output)

        assert payload["checks"] is not None
        assert payload["checks"]["pr_number"] == 0
        assert payload["checks"]["pr_url"].startswith("local:")

    def test_pull_request_shaped_checks_are_skipped_not_failed(self, repo: GitRepo) -> None:
        repo.write(".mira.yaml", QUIET_CONFIG + "checks:\n  enabled: true\n")
        repo.commit("turn checks on")
        _dirty(repo)

        payload = json.loads(_invoke(repo, "--output", "json").output)

        by_id = {r["check_id"]: r for r in payload["checks"]["results"]}
        assert by_id["native.title_description"]["state"] == "skipped"
        assert by_id["native.title_description"]["mode"] == "off"
        assert any("no pull request to read" in note for note in payload["notes"])

    def test_no_check_run_is_recorded_for_a_local_review(self, repo: GitRepo) -> None:
        repo.write(".mira.yaml", QUIET_CONFIG + "checks:\n  enabled: true\n")
        repo.commit("turn checks on")
        _dirty(repo)

        with patch("mira.index.store.IndexStore.record_check_run") as record:
            _invoke(repo, "--output", "json")

        record.assert_not_called()

    def test_no_checks_flag_skips_them_entirely(self, repo: GitRepo) -> None:
        repo.write(".mira.yaml", QUIET_CONFIG + "checks:\n  enabled: true\n")
        repo.commit("turn checks on")
        _dirty(repo)

        payload = json.loads(_invoke(repo, "--no-checks", "--output", "json").output)

        assert payload["checks"] is None
