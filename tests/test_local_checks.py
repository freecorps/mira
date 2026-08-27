"""The local wiring of the pre-merge checks.

Three properties, each of which was a way for a local run to say something it
had not established: reading a file the repository does not contain, reading an
unbounded amount of one it does, and reporting "nothing to see" when the checks
never started.
"""

from __future__ import annotations

import asyncio
import os

import pytest

from mira.checks.policy import EffectiveChecksPolicy
from mira.config import MiraConfig
from mira.local import checks as local_checks
from mira.local.exit_codes import ExitCode
from mira.local.repo import MODE_STAGED, MODE_WORKING_TREE, LocalDiff, RepoIdentity, resolve_diff
from mira.local.run import LocalReview
from tests.conftest import GitRepo

CHECKS_ON = """
checks:
  enabled: true
"""


def _identity(repo: GitRepo) -> RepoIdentity:
    return RepoIdentity(root=repo.root, platform="github", owner="acme", repo="widgets")


def _read(repo: GitRepo, diff: LocalDiff, path: str) -> str:
    reader = local_checks.content_reader_for(repo.root, diff)
    return asyncio.run(reader(path))


# ---------------------------------------------------------------------------
# Reading files from the work tree
# ---------------------------------------------------------------------------


class TestWorktreeReads:
    def test_a_tracked_file_is_read(self, git_repo: GitRepo) -> None:
        git_repo.write("src/app.py", "def start():\n    return 2\n")
        diff = resolve_diff(git_repo.root, mode=MODE_WORKING_TREE)

        assert "return 2" in _read(git_repo, diff, "src/app.py")

    def test_a_symlink_out_of_the_repository_reads_as_nothing(
        self, git_repo: GitRepo, tmp_path
    ) -> None:
        # Git tracks symlinks, so a branch can add `leak -> ~/.ssh/id_rsa` and
        # it arrives here as an ordinary changed path. `Path.is_file()` follows
        # links, so the naive read would hand a host secret to whatever
        # analyser or model the checks are configured with.
        secret = tmp_path / "id_rsa"
        secret.write_text("PRIVATE KEY MATERIAL\n", encoding="utf-8")
        try:
            os.symlink(secret, git_repo.root / "leak")
        except (OSError, NotImplementedError):  # pragma: no cover - needs privilege
            pytest.skip("this platform will not create symlinks unprivileged")

        diff = resolve_diff(git_repo.root, mode=MODE_WORKING_TREE, include_untracked=True)

        assert _read(git_repo, diff, "leak") == ""

    def test_a_symlink_inside_the_repository_reads_as_nothing_too(self, git_repo: GitRepo) -> None:
        # Contained, but still wrong: what git stores for a symlink is the
        # target path, not the target's contents, so reading through it would
        # misreport what the change contains.
        try:
            os.symlink(git_repo.root / "src" / "app.py", git_repo.root / "alias.py")
        except (OSError, NotImplementedError):  # pragma: no cover - needs privilege
            pytest.skip("this platform will not create symlinks unprivileged")

        diff = resolve_diff(git_repo.root, mode=MODE_WORKING_TREE, include_untracked=True)

        assert _read(git_repo, diff, "alias.py") == ""

    def test_a_path_escaping_the_root_reads_as_nothing(self, git_repo: GitRepo) -> None:
        diff = resolve_diff(git_repo.root, mode=MODE_WORKING_TREE)

        assert _read(git_repo, diff, "../outside.txt") == ""

    def test_a_missing_file_reads_as_nothing(self, git_repo: GitRepo) -> None:
        diff = resolve_diff(git_repo.root, mode=MODE_WORKING_TREE)

        assert _read(git_repo, diff, "does/not/exist.py") == ""

    def test_the_read_is_bounded(self, git_repo: GitRepo, monkeypatch: pytest.MonkeyPatch) -> None:
        # The shared context caps what the reader *returned*; a reader that
        # slurped the whole file first has already spent the memory.
        monkeypatch.setattr(local_checks, "MAX_FILE_BYTES", 64)
        git_repo.write("src/app.py", "x" * 100_000)
        diff = resolve_diff(git_repo.root, mode=MODE_WORKING_TREE)

        assert len(_read(git_repo, diff, "src/app.py")) == 65

    def test_the_staged_read_sees_the_index(self, git_repo: GitRepo) -> None:
        git_repo.write("src/app.py", "def start():\n    return 2\n")
        git_repo.git("add", "src/app.py")
        git_repo.write("src/app.py", "def start():\n    return 3\n")
        diff = resolve_diff(git_repo.root, mode=MODE_STAGED)

        content = _read(git_repo, diff, "src/app.py")

        assert "return 2" in content
        assert "return 3" not in content


# ---------------------------------------------------------------------------
# A run that could not start is still a run
# ---------------------------------------------------------------------------


def _diff(repo: GitRepo) -> LocalDiff:
    repo.write("src/app.py", "def start():\n    return 2\n")
    return resolve_diff(repo.root, mode=MODE_WORKING_TREE)


class TestChecksThatCouldNotStart:
    def test_a_policy_that_raises_does_not_escape(
        self, git_repo: GitRepo, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # `run_local_checks` promises never to raise, and the review it would
        # take down with it has already completed and cost money.
        def explode(*_args, **_kwargs):
            raise RuntimeError("policy layer is broken")

        monkeypatch.setattr(local_checks, "resolve_policy", explode)
        config = MiraConfig.model_validate({"checks": {"enabled": True}})

        run, note = asyncio.run(
            local_checks.run_local_checks(
                config=config, identity=_identity(git_repo), diff=_diff(git_repo)
            )
        )

        assert run is not None
        assert run.verdict == "incomplete"
        assert "policy layer is broken" in run.error
        assert "did not run" in note

    def test_unreadable_inputs_produce_an_incomplete_run_not_an_absence(
        self, git_repo: GitRepo, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async def unavailable(*_args, **_kwargs):
            raise local_checks.ChecksUnavailable("the diff could not be parsed")

        monkeypatch.setattr(local_checks, "gather_context", unavailable)
        config = MiraConfig.model_validate({"checks": {"enabled": True}})

        run, note = asyncio.run(
            local_checks.run_local_checks(
                config=config, identity=_identity(git_repo), diff=_diff(git_repo)
            )
        )

        assert run is not None
        assert run.verdict == "incomplete"
        assert "could not be parsed" in note

    def test_checks_switched_off_are_still_an_absence(self, git_repo: GitRepo) -> None:
        # The one case that legitimately reports nothing: nothing was asked, so
        # nothing is owed.
        run, note = asyncio.run(
            local_checks.run_local_checks(
                config=MiraConfig(), identity=_identity(git_repo), diff=_diff(git_repo)
            )
        )

        assert run is None
        assert note == ""


class TestUnansweredChecksReachTheExitCode:
    def _review(self, git_repo: GitRepo, run) -> LocalReview:
        return LocalReview(
            identity=_identity(git_repo),
            diff=LocalDiff(mode=MODE_WORKING_TREE, diff_text=""),
            config=MiraConfig(),
            checks=run,
            fail_on_incomplete_checks=True,
        )

    def test_a_run_that_never_started_fails_the_strict_flag(self, git_repo: GitRepo) -> None:
        # The gap: `--fail-on-incomplete-checks` used to pass exactly when the
        # checks were least able to answer, because a failure returned no run
        # at all and the exit decision only walked a run's results.
        run = local_checks._unstarted(None, EffectiveChecksPolicy(), "the diff could not be read")

        review = self._review(git_repo, run)

        assert review.unanswered_checks
        assert review.exit_code() == ExitCode.FINDINGS

    def test_the_same_run_passes_without_the_flag(self, git_repo: GitRepo) -> None:
        run = local_checks._unstarted(None, EffectiveChecksPolicy(), "the diff could not be read")
        review = self._review(git_repo, run)
        review.fail_on_incomplete_checks = False

        assert review.exit_code() == ExitCode.OK

    def test_checks_switched_off_never_fail_the_strict_flag(self, git_repo: GitRepo) -> None:
        review = self._review(git_repo, None)

        assert review.unanswered_checks == []
        assert review.exit_code() == ExitCode.OK
