"""The local review's git layer: what it reads, and what it refuses to do.

These tests drive a real repository, because every fact under test is a fact
about git's output — that a rename is one entry and not two, that a submodule
pointer carries mode 160000, that a binary file produces a header and no body.
A hand-rolled fixture would assert on our idea of those, which is exactly the
thing that has to be checked.
"""

from __future__ import annotations

import pytest

from mira.local import gitcmd
from mira.local import repo as repo_module
from mira.local.gitcmd import GitCommandRefused, GitError
from mira.local.repo import (
    MODE_RANGE,
    MODE_STAGED,
    MODE_WORKING_TREE,
    identify_repo,
    parse_range,
    platform_for_host,
    resolve_diff,
    split_remote_url,
)
from tests.conftest import GitRepo


def _entry(diff, path: str):
    return next((entry for entry in diff.entries if entry.path == path), None)


# ---------------------------------------------------------------------------
# The read-only guarantee
# ---------------------------------------------------------------------------


class TestReadOnly:
    def test_every_write_subcommand_is_refused(self, git_repo: GitRepo) -> None:
        for subcommand in ("commit", "add", "stash", "checkout", "push", "reset", "clean"):
            with pytest.raises(GitCommandRefused):
                gitcmd.run_git(git_repo.root, subcommand)

    def test_config_is_refused_even_though_it_can_read(self, git_repo: GitRepo) -> None:
        # `git config --get x` reads and `git config x y` writes, one argument
        # apart. The allowlist does not try to tell them apart.
        with pytest.raises(GitCommandRefused):
            gitcmd.run_git(git_repo.root, "config", "--get", "user.name")

    def test_remote_write_forms_are_refused(self, git_repo: GitRepo) -> None:
        with pytest.raises(GitCommandRefused):
            gitcmd.run_git(git_repo.root, "remote", "set-url", "origin", "https://evil/x.git")
        assert gitcmd.run_git(git_repo.root, "remote").ok
        assert gitcmd.run_git(git_repo.root, "remote", "get-url", "origin").ok

    def test_resolving_a_diff_leaves_the_repository_untouched(self, git_repo: GitRepo) -> None:
        git_repo.write("src/app.py", "def start():\n    return 2\n")
        git_repo.write("untracked.py", "x = 1\n")
        git_repo.git("add", "src/app.py")
        before = git_repo.status()
        head_before = git_repo.git("rev-parse", "HEAD").stdout

        resolve_diff(git_repo.root, mode=MODE_WORKING_TREE, include_untracked=True)
        resolve_diff(git_repo.root, mode=MODE_STAGED)

        assert git_repo.status() == before
        assert git_repo.git("rev-parse", "HEAD").stdout == head_before

    def test_environment_carries_no_credential_prompt(self) -> None:
        env = gitcmd.git_env()
        assert env["GIT_TERMINAL_PROMPT"] == "0"
        assert env["GIT_OPTIONAL_LOCKS"] == "0"
        # An ambient GIT_DIR must never redirect a local review at another
        # repository, so it is not carried over.
        assert "GIT_DIR" not in env
        assert "GIT_WORK_TREE" not in env


# ---------------------------------------------------------------------------
# A dirty working tree
# ---------------------------------------------------------------------------


class TestDirtyRepository:
    def test_working_tree_covers_staged_and_unstaged(self, git_repo: GitRepo) -> None:
        git_repo.write("src/app.py", "def start():\n    return 2\n")
        git_repo.git("add", "src/app.py")
        git_repo.write("README.md", "# widgets\n\nnow with docs\n")

        diff = resolve_diff(git_repo.root, mode=MODE_WORKING_TREE)

        paths = {entry.path for entry in diff.entries}
        assert paths == {"src/app.py", "README.md"}
        assert "return 2" in diff.diff_text
        assert "now with docs" in diff.diff_text

    def test_staged_excludes_what_is_only_in_the_working_tree(self, git_repo: GitRepo) -> None:
        git_repo.write("src/app.py", "def start():\n    return 2\n")
        git_repo.git("add", "src/app.py")
        git_repo.write("README.md", "# widgets\n\nnow with docs\n")

        diff = resolve_diff(git_repo.root, mode=MODE_STAGED)

        assert {entry.path for entry in diff.entries} == {"src/app.py"}
        assert "now with docs" not in diff.diff_text

    def test_staged_sees_the_index_not_the_later_edit(self, git_repo: GitRepo) -> None:
        git_repo.write("src/app.py", "def start():\n    return 2\n")
        git_repo.git("add", "src/app.py")
        git_repo.write("src/app.py", "def start():\n    return 3\n")

        staged = resolve_diff(git_repo.root, mode=MODE_STAGED)
        working = resolve_diff(git_repo.root, mode=MODE_WORKING_TREE)

        assert "return 2" in staged.diff_text
        assert "return 3" not in staged.diff_text
        assert "return 3" in working.diff_text

    def test_a_clean_tree_produces_an_empty_diff(self, git_repo: GitRepo) -> None:
        diff = resolve_diff(git_repo.root, mode=MODE_WORKING_TREE)
        assert diff.is_empty
        assert diff.entries == []

    def test_a_repository_with_no_commits_still_resolves(self, tmp_path) -> None:
        root = tmp_path / "unborn"
        root.mkdir()
        repo = GitRepo(root)
        repo.git("init", "-b", "main")
        repo.git("config", "user.email", "dev@example.com")
        repo.git("config", "user.name", "Dev")
        repo.write("first.py", "x = 1\n")
        repo.git("add", "first.py")

        diff = resolve_diff(root, mode=MODE_STAGED)

        assert not diff.is_empty
        assert {entry.path for entry in diff.entries} == {"first.py"}
        assert any("no commits yet" in note for note in diff.notes)


# ---------------------------------------------------------------------------
# Renames, binaries, submodules
# ---------------------------------------------------------------------------


class TestSpecialFiles:
    def test_a_rename_is_one_entry_carrying_its_old_path(self, git_repo: GitRepo) -> None:
        git_repo.write("src/app.py", "def start():\n    return 1\n" + "# padding\n" * 20)
        git_repo.commit("pad it out so the similarity index is unambiguous")
        git_repo.git("mv", "src/app.py", "src/application.py")

        diff = resolve_diff(git_repo.root, mode=MODE_WORKING_TREE)

        entry = _entry(diff, "src/application.py")
        assert entry is not None
        assert entry.status == "R"
        assert entry.old_path == "src/app.py"
        assert _entry(diff, "src/app.py") is None

    def test_a_binary_file_is_listed_and_carries_no_body(self, git_repo: GitRepo) -> None:
        git_repo.write_bytes("logo.png", bytes(range(256)) * 8)

        diff = resolve_diff(git_repo.root, mode=MODE_WORKING_TREE, include_untracked=True)

        # Untracked binaries are not synthesised into a patch: there is nothing
        # a reviewing model could read.
        assert _entry(diff, "logo.png") is None
        assert any("looks binary" in note for note in diff.notes)

    def test_a_tracked_binary_change_is_reported_without_its_bytes(self, git_repo: GitRepo) -> None:
        git_repo.write_bytes("logo.png", b"\x89PNG\r\n\x1a\n" + bytes(range(256)))
        git_repo.commit("add a logo")
        git_repo.write_bytes("logo.png", b"\x89PNG\r\n\x1a\n" + bytes(range(255, -1, -1)))

        diff = resolve_diff(git_repo.root, mode=MODE_WORKING_TREE)

        assert _entry(diff, "logo.png") is not None
        assert "Binary files" in diff.diff_text
        assert "\x00" not in diff.diff_text

    def test_a_submodule_pointer_is_excluded_and_explained(
        self, git_repo: GitRepo, tmp_path
    ) -> None:
        upstream = GitRepo(tmp_path / "dep")
        upstream.root.mkdir()
        upstream.git("init", "-b", "main")
        upstream.git("config", "user.email", "dev@example.com")
        upstream.git("config", "user.name", "Dev")
        upstream.write("lib.py", "value = 1\n")
        upstream.commit("dep initial")

        git_repo.git(
            "-c",
            "protocol.file.allow=always",
            "submodule",
            "add",
            str(upstream.root).replace("\\", "/"),
            "dep",
        )
        git_repo.commit("vendor the dependency")

        upstream.write("lib.py", "value = 2\n")
        upstream.commit("dep moves on")
        git_repo.git("-C", "dep", "fetch", "origin", check=False)
        git_repo.git("-C", "dep", "checkout", upstream.git("rev-parse", "HEAD").stdout.strip())
        git_repo.write("src/app.py", "def start():\n    return 2\n")

        diff = resolve_diff(git_repo.root, mode=MODE_WORKING_TREE)

        pointer = _entry(diff, "dep")
        assert pointer is not None and pointer.submodule
        assert "Subproject commit" not in diff.diff_text
        assert "def start" in diff.diff_text
        assert any("Submodule pointer" in note for note in diff.notes)


# ---------------------------------------------------------------------------
# Untracked files
# ---------------------------------------------------------------------------


class TestUntracked:
    def test_untracked_files_are_left_out_and_said_so(self, git_repo: GitRepo) -> None:
        git_repo.write("scratch.py", "x = 1\n")

        diff = resolve_diff(git_repo.root, mode=MODE_WORKING_TREE)

        assert diff.untracked == ["scratch.py"]
        assert not diff.untracked_included
        assert "x = 1" not in diff.diff_text
        assert any("--include-untracked" in note for note in diff.notes)

    def test_include_untracked_synthesises_a_reviewable_patch(self, git_repo: GitRepo) -> None:
        git_repo.write("scratch.py", "def go():\n    return 1\n")

        diff = resolve_diff(git_repo.root, mode=MODE_WORKING_TREE, include_untracked=True)

        assert diff.untracked_included
        assert "new file mode 100644" in diff.diff_text
        assert "+++ b/scratch.py" in diff.diff_text
        # It has to parse as a diff, or the engine will never see it.
        from mira.core.diff_parser import parse_diff

        parsed = parse_diff(diff.diff_text)
        assert [f.path for f in parsed.files] == ["scratch.py"]
        assert parsed.files[0].added_lines == 2

    def test_a_file_with_no_trailing_newline_still_parses(self, git_repo: GitRepo) -> None:
        git_repo.write("scratch.py", "value = 1")

        diff = resolve_diff(git_repo.root, mode=MODE_WORKING_TREE, include_untracked=True)

        from mira.core.diff_parser import parse_diff

        assert [f.path for f in parse_diff(diff.diff_text).files] == ["scratch.py"]
        assert "\\ No newline at end of file" in diff.diff_text

    def test_a_form_feed_does_not_split_a_line(self, git_repo: GitRepo) -> None:
        # `splitlines` breaks on form feed and on the Unicode separators; git
        # does not. A hunk header counting lines nobody else counts is a diff
        # every other tool reads as corrupt.
        git_repo.write("scratch.py", "a = 1" + chr(12) + "b = 2\n")

        diff = resolve_diff(git_repo.root, mode=MODE_WORKING_TREE, include_untracked=True)

        assert "@@ -0,0 +1,1 @@" in diff.diff_text

    def test_an_empty_untracked_file_is_named_not_patched(self, git_repo: GitRepo) -> None:
        git_repo.write("empty.py", "")

        diff = resolve_diff(git_repo.root, mode=MODE_WORKING_TREE, include_untracked=True)

        assert diff.diff_text.strip() == ""
        assert any("it is empty" in note for note in diff.notes)

    def test_gitignored_files_are_not_offered_at_all(self, git_repo: GitRepo) -> None:
        git_repo.write(".gitignore", "secrets.env\n")
        git_repo.commit("ignore secrets")
        git_repo.write("secrets.env", "TOKEN=hunter2\n")

        diff = resolve_diff(git_repo.root, mode=MODE_WORKING_TREE, include_untracked=True)

        assert diff.untracked == []
        assert "hunter2" not in diff.diff_text

    def test_an_oversized_untracked_file_is_named_rather_than_read(
        self, git_repo: GitRepo, monkeypatch
    ) -> None:
        monkeypatch.setattr("mira.local.repo.MAX_UNTRACKED_BYTES", 16)
        git_repo.write("big.py", "# padding\n" * 100)

        diff = resolve_diff(git_repo.root, mode=MODE_WORKING_TREE, include_untracked=True)

        assert "padding" not in diff.diff_text
        assert any("larger than" in note for note in diff.notes)


# ---------------------------------------------------------------------------
# Commit ranges
# ---------------------------------------------------------------------------


class TestExclusionsAreLiteral:
    def test_an_excluded_path_is_passed_as_a_literal_pathspec(
        self, git_repo: GitRepo, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A pathspec is a pattern by default: git reads `*` and `?` in it, so
        # excluding a submodule whose path contains one would quietly take
        # unrelated files out of the review with it. Asserted on the argv
        # because a path with a wildcard in it cannot be created on every
        # platform this runs on, and the guarantee is in how the path is
        # passed, not in which paths exist.
        seen: list[tuple[str, ...]] = []
        real = repo_module.run_git

        def record(root, *args, **kwargs):
            seen.append(args)
            return real(root, *args, **kwargs)

        monkeypatch.setattr(repo_module, "run_git", record)
        repo_module._run_diff(git_repo.root, ["HEAD"], ["weird*name"], raw=False)

        assert seen
        assert ":(exclude,literal)weird*name" in seen[-1]
        assert ":(exclude)weird*name" not in seen[-1]

    def test_a_diff_larger_than_the_ceiling_is_named_not_truncated(
        self, git_repo: GitRepo, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A diff cut mid-hunk reaches the parser as a malformed one and fails
        # as a Mira bug rather than as the oversized comparison it is.
        monkeypatch.setattr(repo_module, "MAX_DIFF_BYTES", 64)
        git_repo.write("src/app.py", "# padding\n" * 200)

        with pytest.raises(GitError, match="too much to read"):
            resolve_diff(git_repo.root, mode=MODE_WORKING_TREE)


class TestRanges:
    def test_two_dot_range_reviews_the_commits_between(self, git_repo: GitRepo) -> None:
        base = git_repo.git("rev-parse", "HEAD").stdout.strip()
        git_repo.write("src/app.py", "def start():\n    return 2\n")
        git_repo.commit("bump the return")

        diff = resolve_diff(git_repo.root, mode=MODE_RANGE, range_spec=f"{base}..HEAD")

        assert {entry.path for entry in diff.entries} == {"src/app.py"}
        assert "return 2" in diff.diff_text
        assert diff.base_sha == base

    def test_three_dot_range_uses_the_merge_base(self, git_repo: GitRepo) -> None:
        git_repo.git("checkout", "-b", "feature")
        git_repo.write("src/feature.py", "def feature():\n    return 1\n")
        git_repo.commit("add the feature")
        git_repo.git("checkout", "main")
        git_repo.write("src/unrelated.py", "def other():\n    return 1\n")
        git_repo.commit("unrelated work on main")

        diff = resolve_diff(git_repo.root, mode=MODE_RANGE, range_spec="main...feature")

        # The merge base excludes main's later commit; a two-dot range would
        # have reported it as a deletion.
        assert {entry.path for entry in diff.entries} == {"src/feature.py"}

    @pytest.mark.parametrize(
        "spec",
        ["HEAD", "", "   ", "..HEAD", "a..b..c", "main....feature"],
    )
    def test_a_malformed_range_is_rejected_before_git_sees_it(
        self, git_repo: GitRepo, spec: str
    ) -> None:
        with pytest.raises(ValueError):
            resolve_diff(git_repo.root, mode=MODE_RANGE, range_spec=spec)

    def test_an_unknown_revision_is_a_git_error(self, git_repo: GitRepo) -> None:
        with pytest.raises(GitError, match="does not resolve"):
            resolve_diff(git_repo.root, mode=MODE_RANGE, range_spec="nosuchref..HEAD")

    def test_a_revision_that_looks_like_an_option_is_refused(self, git_repo: GitRepo) -> None:
        with pytest.raises(GitError, match="looks like an option"):
            resolve_diff(git_repo.root, mode=MODE_RANGE, range_spec="--output=/tmp/x..HEAD")

    def test_unrelated_histories_are_named_rather_than_reported_empty(
        self, git_repo: GitRepo
    ) -> None:
        git_repo.git("checkout", "--orphan", "island")
        git_repo.git("rm", "-rf", "--cached", ".")
        git_repo.write("island.py", "x = 1\n")
        git_repo.commit("an island")

        with pytest.raises(GitError, match="no common ancestor"):
            resolve_diff(git_repo.root, mode=MODE_RANGE, range_spec="main...island")

    def test_untracked_files_are_not_consulted_for_a_range(self, git_repo: GitRepo) -> None:
        base = git_repo.git("rev-parse", "HEAD").stdout.strip()
        git_repo.write("src/app.py", "def start():\n    return 2\n")
        git_repo.commit("bump")
        git_repo.write("scratch.py", "x = 1\n")

        diff = resolve_diff(git_repo.root, mode=MODE_RANGE, range_spec=f"{base}..HEAD")

        assert diff.untracked == []

    def test_untracked_files_are_not_consulted_for_the_index(self, git_repo: GitRepo) -> None:
        # An untracked file is not staged, so a review of what a commit would
        # contain has nothing to say about it either way.
        git_repo.write("scratch.py", "x = 1\n")
        git_repo.write("src/app.py", "def start():\n    return 2\n")
        git_repo.git("add", "src/app.py")

        diff = resolve_diff(git_repo.root, mode=MODE_STAGED)

        assert diff.untracked == []
        assert not any("untracked" in note for note in diff.notes)


class TestParseRange:
    @pytest.mark.parametrize(
        ("spec", "expected"),
        [
            ("main..HEAD", ("main", "HEAD", False)),
            ("main...HEAD", ("main", "HEAD", True)),
            ("main...", ("main", "HEAD", True)),
            ("  main..feature  ", ("main", "feature", False)),
        ],
    )
    def test_shapes(self, spec: str, expected: tuple[str, str, bool]) -> None:
        assert parse_range(spec) == expected


# ---------------------------------------------------------------------------
# Repository identity
# ---------------------------------------------------------------------------


class TestRepoIdentity:
    @pytest.mark.parametrize(
        ("url", "expected"),
        [
            ("https://github.com/acme/widgets.git", ("github.com", "acme", "widgets")),
            ("https://github.com/acme/widgets", ("github.com", "acme", "widgets")),
            ("git@github.com:acme/widgets.git", ("github.com", "acme", "widgets")),
            ("ssh://git@gitlab.com/group/sub/proj.git", ("gitlab.com", "group/sub", "proj")),
            ("https://codeberg.org/acme/widgets.git", ("codeberg.org", "acme", "widgets")),
            ("/srv/git/widgets.git", ("", "srv/git", "widgets")),
            ("", ("", "", "")),
        ],
    )
    def test_split_remote_url(self, url: str, expected: tuple[str, str, str]) -> None:
        assert split_remote_url(url) == expected

    @pytest.mark.parametrize(
        ("host", "expected"),
        [
            ("github.com", "github"),
            ("gitlab.com", "gitlab"),
            ("gitlab.acme.internal", "gitlab"),
            ("codeberg.org", "forgejo"),
            ("git.acme.internal", "github"),
        ],
    )
    def test_platform_for_host(self, host: str, expected: str) -> None:
        assert platform_for_host(host, "github") == expected

    def test_identity_comes_from_the_configured_remote(self, git_repo: GitRepo) -> None:
        identity = identify_repo(git_repo.root, fallback_platform="github")

        assert identity.slug == "acme/widgets"
        assert identity.platform == "github"
        assert identity.branch == "main"
        assert identity.known

    def test_a_stated_slug_wins_over_the_remote(self, git_repo: GitRepo) -> None:
        identity = identify_repo(
            git_repo.root,
            fallback_platform="github",
            stated_slug="team/group/project",
            stated_platform="gitlab",
        )

        assert identity.owner == "team/group"
        assert identity.repo == "project"
        assert identity.platform == "gitlab"
        assert identity.stated

    def test_a_repository_with_no_remote_is_unidentified_rather_than_guessed(
        self, git_repo: GitRepo
    ) -> None:
        git_repo.git("remote", "remove", "origin")

        identity = identify_repo(git_repo.root, fallback_platform="github")

        assert not identity.known
        assert identity.slug == ""

    def test_a_missing_named_remote_is_an_error(self, git_repo: GitRepo) -> None:
        with pytest.raises(GitError, match="not configured"):
            identify_repo(git_repo.root, fallback_platform="github", remote="upstream")

    def test_a_malformed_stated_slug_is_an_error(self, git_repo: GitRepo) -> None:
        with pytest.raises(GitError, match="owner/repo"):
            identify_repo(git_repo.root, fallback_platform="github", stated_slug="widgets")


class TestRepoRoot:
    def test_a_subdirectory_resolves_to_the_root(self, git_repo: GitRepo) -> None:
        assert gitcmd.find_repo_root(git_repo.root / "src") == git_repo.root

    def test_outside_a_repository_is_an_error(self, tmp_path) -> None:
        outside = tmp_path / "not-a-repo"
        outside.mkdir()
        with pytest.raises(GitError, match="not inside a git work tree"):
            gitcmd.find_repo_root(outside)
