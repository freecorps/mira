"""Phase 7C — recording what Mira watched merge.

The history a suggestion ranks on has to come from somewhere, and the honest
somewhere is a merge Mira saw happen: the identities arrive on a webhook the
platform signed, which is true on all three platforms and stronger than any
commit's own author fields.

The rule worth testing is the one about *not* collecting: a repository that has
not turned triage on does not get rows written about who works on which of its
files. "In case it is useful later" is how an install stops being trustworthy.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from mira.config import MiraConfig, TriageConfig
from mira.models import FileChangeStat, PRInfo
from mira.triage.contributions import (
    MAX_PATHS_PER_EVENT,
    record_merged_pull_request,
    rows_for,
)

NOW = 1_800_000_000.0


@pytest.fixture(autouse=True)
def isolated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MIRA_INDEX_DIR", str(tmp_path))
    monkeypatch.delenv("DATABASE_URL", raising=False)


def _pr() -> PRInfo:
    return PRInfo(
        title="t",
        description="",
        base_branch="main",
        head_branch="feature",
        url="https://github.com/acme/app/pull/7",
        number=7,
        owner="acme",
        repo="app",
        author="Dana",
        head_sha="head222",
    )


class FakeProvider:
    def __init__(
        self,
        changes: list[FileChangeStat] | None = None,
        reviewers: dict[str, str] | None = None,
        review_error: Exception | None = None,
    ) -> None:
        self.changes = changes or [
            FileChangeStat(path="src/app.py", added_lines=3),
            FileChangeStat(path="package-lock.json", added_lines=900),
        ]
        self.reviewers = {"sam": "approved"} if reviewers is None else reviewers
        self.review_error = review_error

    async def get_pr_change_stats(self, pr_info: PRInfo) -> list[FileChangeStat]:
        return self.changes

    async def get_review_states(self, pr_info: PRInfo) -> dict[str, str]:
        if self.review_error is not None:
            raise self.review_error
        return self.reviewers


def _config(enabled: bool = True) -> MiraConfig:
    config = MiraConfig()
    config.triage = TriageConfig(enabled=enabled)
    return config


def test_a_merged_pull_request_becomes_one_row_per_person_per_file() -> None:
    rows = rows_for(
        platform="github",
        paths=["src/app.py", "src/util.py"],
        author="dana",
        reviewers={"sam": "approved", "ari": "commented"},
        pr_number=7,
        pr_url="https://github.com/acme/app/pull/7",
        event_at=NOW,
    )
    assert len(rows) == 6
    assert {row["role"] for row in rows} == {"authored", "reviewed"}
    assert {row["reference"] for row in rows} == {"7"}


def test_a_review_nobody_left_is_not_a_review() -> None:
    rows = rows_for(
        platform="github",
        paths=["src/app.py"],
        author="dana",
        reviewers={"sam": "dismissed", "ari": ""},
        pr_number=7,
        pr_url="",
        event_at=NOW,
    )
    assert [row["identity"] for row in rows] == ["dana"]


def test_reviewing_your_own_pull_request_is_not_a_second_signal() -> None:
    rows = rows_for(
        platform="github",
        paths=["src/app.py"],
        author="dana",
        reviewers={"dana": "approved"},
        pr_number=7,
        pr_url="",
        event_at=NOW,
    )
    assert [(row["identity"], row["role"]) for row in rows] == [("dana", "authored")]


async def test_nothing_is_recorded_where_triage_is_off() -> None:
    """Who works on which file is data about people. It is collected because
    an operator turned suggestions on, not in case they might."""
    written = await record_merged_pull_request(FakeProvider(), _pr(), config=_config(enabled=False))
    assert written == 0

    from mira.index.store import IndexStore

    store = IndexStore.open("acme", "app")
    assert store.path_contributions(["src/app.py"], since=0) == []
    store.close()


async def test_a_merge_records_the_author_and_the_reviewers() -> None:
    written = await record_merged_pull_request(FakeProvider(), _pr(), config=_config())
    assert written > 0

    from mira.index.store import IndexStore

    store = IndexStore.open("acme", "app")
    rows = store.path_contributions(["src/app.py"], since=0)
    assert {(row["identity"], row["role"]) for row in rows} == {
        ("dana", "authored"),
        ("sam", "reviewed"),
    }
    # A generated file tells you nothing about who knows the code.
    assert store.path_contributions(["package-lock.json"], since=0) == []
    store.close()


async def test_review_states_that_cannot_be_read_still_record_the_author() -> None:
    provider = FakeProvider(review_error=RuntimeError("API down"))
    written = await record_merged_pull_request(provider, _pr(), config=_config())
    assert written > 0

    from mira.index.store import IndexStore

    store = IndexStore.open("acme", "app")
    roles = {row["role"] for row in store.path_contributions(["src/app.py"], since=0)}
    assert roles == {"authored"}
    store.close()


async def test_a_provider_that_cannot_list_files_records_nothing_and_does_not_raise() -> None:
    class Broken(FakeProvider):
        async def get_pr_change_stats(self, pr_info: PRInfo) -> list[FileChangeStat]:
            raise RuntimeError("gone")

    assert await record_merged_pull_request(Broken(), _pr(), config=_config()) == 0


async def test_a_giant_refactor_does_not_write_a_row_for_every_file() -> None:
    changes = [FileChangeStat(path=f"src/module_{index}.py", added_lines=1) for index in range(400)]
    provider = FakeProvider(changes=changes, reviewers={})
    written = await record_merged_pull_request(provider, _pr(), config=_config())
    assert written == MAX_PATHS_PER_EVENT


async def test_recording_the_same_merge_twice_is_a_no_op() -> None:
    provider = FakeProvider()
    first = await record_merged_pull_request(provider, _pr(), config=_config())
    second = await record_merged_pull_request(provider, _pr(), config=_config())
    assert first > 0
    assert second == 0


def test_the_author_is_stored_in_the_spelling_everything_compares_on() -> None:
    rows = rows_for(
        platform="github",
        paths=["a.py"],
        author="Dana",
        reviewers={"SAM": "approved"},
        pr_number=1,
        pr_url="",
        event_at=NOW,
    )
    assert {row["identity"] for row in rows} == {"dana", "sam"}


async def test_the_caller_may_hand_in_its_own_store() -> None:
    """So the merge handler does not open a second connection to write two rows."""

    class RecordingStore:
        def __init__(self) -> None:
            self.rows: list[dict[str, Any]] = []

        def record_path_contributions(self, rows: list[dict[str, Any]]) -> int:
            self.rows.extend(rows)
            return len(rows)

    store = RecordingStore()
    written = await record_merged_pull_request(FakeProvider(), _pr(), config=_config(), store=store)
    assert written == len(store.rows) > 0
