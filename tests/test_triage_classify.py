"""Phase 7C — what kind of change this is, from the diff and nothing else.

The classification is observational: it reads paths and line counts. It does
not read the title, the branch name or a label — a pull request that says "docs
only" and touches ``src/auth`` is exactly the one a reviewer most needs
classified correctly.
"""

from __future__ import annotations

from mira.models import FileChangeStat
from mira.triage.classify import area_for, classify, is_ci, is_dependency, kinds_for


def _change(path: str, added: int = 1, deleted: int = 0) -> FileChangeStat:
    return FileChangeStat(path=path, added_lines=added, deleted_lines=deleted)


def test_an_empty_change_classifies_as_nothing() -> None:
    result = classify([])
    assert result.changed_files == 0
    assert result.kinds == []


def test_a_file_can_be_several_kinds_at_once() -> None:
    """Collapsing a path to one label throws away the part a reviewer needs."""
    assert kinds_for("tests/test_migrations.py") == ["tests"]
    assert kinds_for("db/migrations/0042_add_column.sql") == ["migration"]
    assert kinds_for("docs/guide.md") == ["docs"]
    assert kinds_for("src/mira/engine.py") == ["code"]
    assert "dependencies" in kinds_for("package.json")
    assert "ci" in kinds_for(".github/workflows/ci.yml")


def test_dependency_and_ci_files_are_recognised_by_name_and_by_place() -> None:
    assert is_dependency("ui/mira/package-lock.json") is True
    assert is_dependency("src/app/packages.py") is False
    assert is_ci(".gitlab-ci.yml") is True
    assert is_ci(".github/workflows/release.yml") is True
    assert is_ci("src/ci.py") is False


def test_an_area_is_a_directory_not_a_file() -> None:
    assert area_for("src/mira/checks/runner.py") == "src/mira"
    assert area_for("README.md") == "(root)"
    assert area_for("docs/triage.md") == "docs"


def test_a_regenerated_lockfile_makes_a_change_long_not_large() -> None:
    """Generated lines are counted and do not decide the size.

    Calling a lockfile bump ``xl`` would train everybody to ignore the size,
    which is the one number a reviewer reads before deciding when to start.
    """
    result = classify([_change("src/app.py", added=5), _change("package-lock.json", added=4000)])
    assert result.size == "xs"
    assert result.changed_files == 2
    assert result.changed_lines == 4005
    assert "generated" in result.kinds


def test_size_buckets_follow_the_real_lines() -> None:
    assert classify([_change("a.py", added=5)]).size == "xs"
    assert classify([_change("a.py", added=40)]).size == "s"
    assert classify([_change("a.py", added=200)]).size == "m"
    assert classify([_change("a.py", added=900)]).size == "l"
    assert classify([_change("a.py", added=5000)]).size == "xl"


def test_areas_are_ordered_by_how_much_changed_in_them() -> None:
    result = classify(
        [
            _change("ui/mira/src/app.tsx", added=2),
            _change("src/mira/checks/runner.py", added=90),
            _change("src/mira/checks/models.py", added=10),
        ]
    )
    assert result.areas[0] == "src/mira"
    assert "ui/mira" in result.areas


def test_the_same_files_always_classify_the_same_way() -> None:
    """Reproducibility, in the smallest place it can be checked."""
    changes = [
        _change("src/a.py", added=3),
        _change("docs/b.md", added=3),
        _change("tests/test_c.py", added=3),
    ]
    first = classify(changes)
    second = classify(list(reversed(changes)))
    assert first.as_dict() == second.as_dict()


def test_an_unrecognised_file_is_other_rather_than_nothing() -> None:
    result = classify([_change("Makefile", added=3)])
    assert result.kinds == ["other"]
