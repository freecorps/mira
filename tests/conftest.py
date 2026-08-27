"""Shared test fixtures."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from mira.config import MiraConfig
from mira.models import (
    FileChangeType,
    FileDiff,
    HunkInfo,
    PatchSet,
    ReviewComment,
    Severity,
    WalkthroughFileEntry,
    WalkthroughResult,
)

FIXTURES_DIR = Path(__file__).parent / "fixtures"


@pytest.fixture(autouse=True)
def isolate_index_storage(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Keep tests from writing Mira's durable state to the host data directory."""
    monkeypatch.setenv("MIRA_INDEX_DIR", str(tmp_path / "indexes"))


@pytest.fixture
def sample_diff_text() -> str:
    return (FIXTURES_DIR / "sample.diff").read_text()


@pytest.fixture
def sample_config_path() -> Path:
    return FIXTURES_DIR / "sample_config.yml"


@pytest.fixture
def sample_llm_response_text() -> str:
    return (FIXTURES_DIR / "sample_llm_response.json").read_text()


@pytest.fixture
def sample_llm_response_data() -> dict:
    return json.loads((FIXTURES_DIR / "sample_llm_response.json").read_text())


@pytest.fixture
def default_config() -> MiraConfig:
    return MiraConfig()


@pytest.fixture
def sample_hunk() -> HunkInfo:
    return HunkInfo(
        source_start=10,
        source_length=5,
        target_start=10,
        target_length=7,
        content="@@ -10,5 +10,7 @@\n context\n-old line\n+new line\n+added line\n context",
    )


@pytest.fixture
def sample_file_diff(sample_hunk: HunkInfo) -> FileDiff:
    return FileDiff(
        path="src/utils.py",
        change_type=FileChangeType.MODIFIED,
        hunks=[sample_hunk],
        language="python",
        added_lines=2,
        deleted_lines=1,
    )


@pytest.fixture
def sample_patch_set(sample_file_diff: FileDiff) -> PatchSet:
    return PatchSet(files=[sample_file_diff])


@pytest.fixture
def sample_review_comment() -> ReviewComment:
    return ReviewComment(
        path="src/utils.py",
        line=15,
        end_line=None,
        severity=Severity.WARNING,
        category="security",
        title="Potential security issue",
        body="This could be a security vulnerability.",
        confidence=0.85,
        suggestion=None,
    )


@pytest.fixture
def sample_walkthrough_response_text() -> str:
    return (FIXTURES_DIR / "sample_walkthrough_response.json").read_text()


@pytest.fixture
def sample_walkthrough_response_data() -> dict:
    return json.loads((FIXTURES_DIR / "sample_walkthrough_response.json").read_text())


@pytest.fixture
def sample_walkthrough_result() -> WalkthroughResult:
    return WalkthroughResult(
        summary="This PR adds utility functions for shell commands and config parsing.",
        file_changes=[
            WalkthroughFileEntry(
                path="src/utils.py",
                change_type=FileChangeType.ADDED,
                description="New utility module with shell command runner and config reader",
                group="Core",
            ),
            WalkthroughFileEntry(
                path="src/main.py",
                change_type=FileChangeType.MODIFIED,
                description="Added debug parameter to App.start() method",
                group="App Shell",
            ),
        ],
    )


# ---------------------------------------------------------------------------
# Real git repositories, for the local review surface (Phase 7A)
# ---------------------------------------------------------------------------


class GitRepo:
    """A throwaway git repository a test can shape.

    A real repository rather than a fake: the whole point of the local surface
    is that git decides what a rename, a binary file and a submodule pointer
    look like, and a hand-written fixture would be asserting on our idea of
    git's output rather than on git's.
    """

    def __init__(self, root: Path) -> None:
        self.root = root

    def git(self, *args: str, check: bool = True) -> subprocess.CompletedProcess:
        result = subprocess.run(  # noqa: S603 - argv only, test-controlled
            ["git", *args],
            cwd=self.root,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        if check and result.returncode != 0:
            raise AssertionError(f"git {' '.join(args)} failed: {result.stderr}")
        return result

    def write(self, relative: str, content: str) -> Path:
        target = self.root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8", newline="\n")
        return target

    def write_bytes(self, relative: str, content: bytes) -> Path:
        target = self.root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
        return target

    def commit(self, message: str, *paths: str) -> str:
        self.git("add", *(paths or ("-A",)))
        self.git("commit", "-m", message)
        return self.git("rev-parse", "HEAD").stdout.strip()

    def status(self) -> str:
        return self.git("status", "--porcelain=v1", "--untracked-files=all").stdout


@pytest.fixture
def git_repo(tmp_path: Path) -> GitRepo:
    """An initialised repository with one commit and an `origin` remote."""
    if shutil.which("git") is None:  # pragma: no cover - CI always has git
        pytest.skip("git is not installed")
    root = tmp_path / "repo"
    root.mkdir()
    repo = GitRepo(root)
    repo.git("init", "-b", "main")
    repo.git("config", "user.email", "dev@example.com")
    repo.git("config", "user.name", "Dev")
    repo.git("config", "commit.gpgsign", "false")
    # Line endings are part of a diff. Leaving these to the host's git would
    # make the same test assert on different bytes on Windows.
    repo.git("config", "core.autocrlf", "false")
    repo.git("config", "core.safecrlf", "false")
    repo.git("remote", "add", "origin", "https://github.com/acme/widgets.git")
    repo.write("README.md", "# widgets\n")
    repo.write("src/app.py", "def start():\n    return 1\n")
    repo.commit("initial commit")
    return repo
