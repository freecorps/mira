"""The CI helpers in scripts/ci: which jobs a change runs, and test shards."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts" / "ci"


def _load(name: str):
    spec = importlib.util.spec_from_file_location(f"ci_{name}", SCRIPTS / f"{name}.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


affected = _load("affected")
pytest_shard = _load("pytest_shard")


def _runs(files: list[str] | None) -> dict[str, bool]:
    _, jobs = affected.decide(files)
    return {job: hits is None or bool(hits) for job, hits in jobs.items()}


class TestDockerfileSources:
    def test_the_real_dockerfile_yields_its_build_context(self) -> None:
        sources = affected.dockerfile_sources(affected.ROOT / "Dockerfile")
        for expected in ("src/", "ui/mira/", "pyproject.toml", "uv.lock", "README.md"):
            assert expected in sources

    def test_every_copy_form_is_read_and_stage_copies_are_not(self, tmp_path: Path) -> None:
        dockerfile = tmp_path / "Dockerfile"
        dockerfile.write_text(
            "FROM scratch AS base\n"
            "COPY --chown=1:1 conf/*.yaml /etc/app/\n"
            'COPY ["with space", "/dest"]\n'
            "ADD one \\\n    two/ /dest/\n"
            "COPY --from=base /built /app\n"
            "COPY ./data ./data\n"
        )
        assert affected.dockerfile_sources(dockerfile) == [
            "conf/",
            "with space",
            "one",
            "two/",
            "data",
        ]

    def test_copying_the_whole_context_matches_everything(self, tmp_path: Path) -> None:
        dockerfile = tmp_path / "Dockerfile"
        dockerfile.write_text("COPY . /app\n")
        assert affected.matches(
            "anything/at/all.md", tuple(affected.dockerfile_sources(dockerfile))
        )


class TestMatches:
    def test_a_directory_matches_what_is_under_it_with_or_without_a_slash(self) -> None:
        assert affected.matches("data/seed.json", ("data",))
        assert affected.matches("data/seed.json", ("data/",))

    def test_an_entry_does_not_match_a_sibling_sharing_its_prefix(self) -> None:
        assert not affected.matches("uv.lock.bak", ("uv.lock",))
        assert not affected.matches("database/x.py", ("data",))


class TestDecide:
    def test_a_docs_only_change_runs_no_heavy_job(self) -> None:
        assert _runs(["docs/logs.md", "CHANGELOG.md"]) == {
            "python": False,
            "ui": False,
            "docker": False,
        }

    @pytest.mark.parametrize(
        "path",
        ["deploy/orangepi/mira-update.sh", "scripts/import_golden_comments.py"],
    )
    def test_files_the_tests_exercise_outside_src_run_them(self, path: str) -> None:
        assert _runs([path])["python"]

    def test_a_ui_change_runs_the_ui_build_and_the_image(self) -> None:
        assert _runs(["ui/mira/src/main.tsx"]) == {"python": False, "ui": True, "docker": True}

    @pytest.mark.parametrize("files", [None, [".github/workflows/ci.yml"]])
    def test_no_base_or_a_workflow_change_runs_everything(self, files: list[str] | None) -> None:
        assert all(_runs(files).values())

    def test_an_unreadable_dockerfile_runs_everything(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        (tmp_path / "Dockerfile").write_text('COPY ["unterminated\n')
        monkeypatch.setattr(affected, "ROOT", tmp_path)
        assert all(_runs(["docs/logs.md"]).values())


class TestShards:
    def test_every_test_lands_in_exactly_one_shard(self) -> None:
        ids = [f"tests/test_{n % 40}.py::test_case_{n}" for n in range(3000)]
        shards = [pytest_shard.shard_of(nodeid, 3) for nodeid in ids]
        assert set(shards) == {1, 2, 3}
        assert shards == [pytest_shard.shard_of(nodeid, 3) for nodeid in ids]
        assert min(shards.count(k) for k in (1, 2, 3)) > 900

    @pytest.mark.parametrize("value", ["0/3", "4/3", "x/3", "3"])
    def test_a_shard_outside_the_suite_is_refused(
        self, value: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("MIRA_TEST_SHARD", value)
        with pytest.raises(pytest.UsageError):
            pytest_shard._shard()
