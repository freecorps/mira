"""Phase 7C — the history signals, and what they refuse to infer.

Three properties are tested here rather than described.

*An unattributed commit names nobody.* A git commit's author fields are written
by whoever made the commit. Where the platform cannot resolve a commit to an
account, the commit is counted and dropped, never turned into a name.

*A failed refresh is unavailable, not empty.* If the platform could not be
asked, the signal says so — even when stored history still produced somebody,
because a ranking built on a stale half of the data should say which half.

*A path that was never asked about is not a path with no history.* The fetch
marker exists exactly to keep those two apart.
"""

from __future__ import annotations

import time
from typing import Any

from mira.models import FileChangeStat, PathAuthorship, PRInfo
from mira.triage import history

NOW = 1_800_000_000.0
DAY = 86_400.0


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
        base_sha="base111",
        head_sha="head222",
    )


def _changes() -> list[FileChangeStat]:
    return [
        FileChangeStat(path="src/app.py", added_lines=80),
        FileChangeStat(path="src/util.py", added_lines=5),
        FileChangeStat(path="package-lock.json", added_lines=4000),
    ]


class FakeStore:
    def __init__(self, rows: list[dict[str, Any]] | None = None) -> None:
        self.rows = rows or []
        self.fetched: dict[str, float] = {}
        self.recorded: list[dict[str, Any]] = []
        self.marked: list[tuple[str, ...]] = []
        self.read_error: Exception | None = None

    def path_fetch_times(self, paths: list[str], *, platform: str = "github") -> dict[str, float]:
        return {path: self.fetched[path] for path in paths if path in self.fetched}

    def record_path_contributions(self, rows: list[dict[str, Any]]) -> int:
        self.recorded.extend(rows)
        self.rows.extend(rows)
        return len(rows)

    def mark_path_fetched(
        self, paths: list[str], *, platform: str = "github", entries: int = 0, at: float = 0.0
    ) -> None:
        self.marked.append(tuple(paths))
        for path in paths:
            self.fetched[path] = at or NOW

    def path_contributions(
        self, paths: list[str], *, since: float = 0.0, limit: int = 2000
    ) -> list[dict[str, Any]]:
        if self.read_error is not None:
            raise self.read_error
        return [
            row
            for row in sorted(self.rows, key=lambda r: -float(r.get("event_at") or 0))
            if row["path"] in paths and float(row.get("event_at") or 0) >= since
        ]


class FakeProvider:
    def __init__(
        self,
        by_path: dict[str, list[PathAuthorship]] | None = None,
        error: Exception | None = None,
    ) -> None:
        self.by_path = by_path or {}
        self.error = error
        self.asked: list[tuple[str, ...]] = []
        self.refs: list[str] = []

    async def get_path_authors(
        self, pr_info: PRInfo, paths: list[str], *, ref: str = "", max_per_path: int = 20
    ) -> dict[str, list[PathAuthorship]]:
        self.asked.append(tuple(paths))
        self.refs.append(ref)
        if self.error is not None:
            raise self.error
        return {path: self.by_path.get(path, []) for path in paths if self.by_path.get(path)}


def test_the_hottest_paths_are_asked_about_and_generated_ones_are_not() -> None:
    paths = history.ranked_paths(_changes(), limit=5)
    assert paths == ["src/app.py", "src/util.py"]


def test_the_path_budget_is_a_ceiling() -> None:
    changes = [FileChangeStat(path=f"src/{index}.py", added_lines=index) for index in range(30)]
    assert len(history.ranked_paths(changes, limit=4)) == 4


def test_recency_falls_off_across_the_window_and_never_to_nothing() -> None:
    assert history.recency(NOW, now=NOW, window_days=180) == 1.0
    halfway = history.recency(NOW - 90 * DAY, now=NOW, window_days=180)
    assert 0.45 < halfway < 0.55
    assert history.recency(NOW - 900 * DAY, now=NOW, window_days=180) == history.MIN_RECENCY


def test_a_path_never_asked_about_is_not_a_path_with_no_history() -> None:
    fetched = {"a.py": NOW - 3600}
    stale = history.stale_paths(fetched, ["a.py", "b.py"], now=NOW, refresh_hours=168)
    assert stale == ["b.py"]


async def test_commits_the_platform_could_not_attribute_name_nobody() -> None:
    provider = FakeProvider(
        by_path={
            "src/app.py": [
                PathAuthorship(path="src/app.py", login="dana", sha="aaa", at=NOW - DAY),
                PathAuthorship(path="src/app.py", login="", sha="bbb", at=NOW - DAY),
            ]
        }
    )
    store = FakeStore()
    outcome = await history.gather(provider, _pr(), _changes(), store=store, ref="base111", now=NOW)
    assert set(outcome.authored) == {"dana"}
    assert "1 commit(s) could not be attributed" in outcome.authored_report.detail
    assert provider.refs == ["base111"]


async def test_history_is_fetched_at_the_base_never_the_head() -> None:
    provider = FakeProvider()
    await history.gather(provider, _pr(), _changes(), store=FakeStore(), ref="base111", now=NOW)
    assert provider.refs == ["base111"]
    assert "head222" not in provider.refs


async def test_a_failed_fetch_is_unavailable_even_when_stored_history_answered() -> None:
    store = FakeStore(
        [
            {
                "path": "src/app.py",
                "identity": "dana",
                "role": "authored",
                "source": "pull_request",
                "reference": "12",
                "url": "",
                "event_at": NOW - DAY,
            }
        ]
    )
    outcome = await history.gather(
        FakeProvider(error=RuntimeError("rate limited")),
        _pr(),
        _changes(),
        store=store,
        ref="base111",
        now=NOW,
    )
    assert outcome.authored_report.status == "unavailable"
    assert "rate limited" in outcome.authored_report.detail
    assert "were still used" in outcome.authored_report.detail
    # The stored history is still used — a degraded ranking beats none.
    assert set(outcome.authored) == {"dana"}


async def test_a_platform_that_cannot_attribute_commits_says_so_rather_than_empty() -> None:
    outcome = await history.gather(
        FakeProvider(),
        _pr(),
        _changes(),
        store=FakeStore(),
        can_attribute_commits=False,
        ref="base111",
        now=NOW,
    )
    assert outcome.authored_report.status == "unsupported"
    assert "does not attribute commits" in outcome.authored_report.detail


async def test_an_unreadable_store_is_unavailable_for_both_signals() -> None:
    store = FakeStore()
    store.read_error = RuntimeError("database is locked")
    outcome = await history.gather(
        FakeProvider(), _pr(), _changes(), store=store, ref="base111", now=NOW
    )
    assert outcome.authored_report.status == "unavailable"
    assert outcome.reviewed_report.status == "unavailable"


async def test_no_store_at_all_is_unavailable_not_empty() -> None:
    outcome = await history.gather(
        FakeProvider(), _pr(), _changes(), store=None, ref="base111", now=NOW
    )
    assert outcome.authored_report.status == "unavailable"


async def test_the_signal_can_be_switched_off() -> None:
    provider = FakeProvider()
    outcome = await history.gather(
        provider, _pr(), _changes(), store=FakeStore(), enabled=False, ref="base111", now=NOW
    )
    assert outcome.authored_report.status == "disabled"
    assert provider.asked == []


async def test_a_path_is_marked_fetched_even_when_nothing_came_back() -> None:
    """An empty answer is an answer, and re-asking it every push exhausts a
    rate limit to learn the same thing."""
    store = FakeStore()
    await history.gather(FakeProvider(), _pr(), _changes(), store=store, ref="base111", now=NOW)
    assert store.marked == [("src/app.py", "src/util.py")]

    # A second run inside the refresh window asks for nothing.
    provider = FakeProvider()
    await history.gather(provider, _pr(), _changes(), store=store, ref="base111", now=NOW)
    assert provider.asked == []


async def test_forty_commits_to_one_file_count_once() -> None:
    """Otherwise the ranking measures who rebases most."""
    rows = [
        {
            "path": "src/app.py",
            "identity": "dana",
            "role": "authored",
            "source": "commit",
            "reference": f"sha{index}",
            "url": "",
            "event_at": NOW - index * DAY,
        }
        for index in range(40)
    ]
    outcome = await history.gather(
        FakeProvider(), _pr(), _changes(), store=FakeStore(rows), ref="base111", now=NOW
    )
    assert len(outcome.authored["dana"]) == 1
    # And the one that counts is the most recent.
    assert outcome.authored["dana"][0].at == NOW


async def test_reviews_and_authorship_are_separate_signals() -> None:
    rows = [
        {
            "path": "src/app.py",
            "identity": "dana",
            "role": "authored",
            "source": "pull_request",
            "reference": "3",
            "url": "",
            "event_at": NOW - DAY,
        },
        {
            "path": "src/app.py",
            "identity": "sam",
            "role": "reviewed",
            "source": "review",
            "reference": "3",
            "url": "",
            "event_at": NOW - DAY,
        },
    ]
    outcome = await history.gather(
        FakeProvider(), _pr(), _changes(), store=FakeStore(rows), ref="base111", now=NOW
    )
    assert set(outcome.authored) == {"dana"}
    assert set(outcome.reviewed) == {"sam"}
    assert outcome.reviewed["sam"][0].evidence.detail == "reviewed pull request #3"


async def test_history_outside_the_window_is_not_counted() -> None:
    rows = [
        {
            "path": "src/app.py",
            "identity": "dana",
            "role": "authored",
            "source": "commit",
            "reference": "old",
            "url": "",
            "event_at": NOW - 400 * DAY,
        }
    ]
    outcome = await history.gather(
        FakeProvider(),
        _pr(),
        _changes(),
        store=FakeStore(rows),
        window_days=180,
        ref="base111",
        now=NOW,
    )
    assert outcome.authored == {}
    assert outcome.authored_report.status == "empty"


async def test_an_unknown_base_stops_the_fetch_rather_than_reading_the_head() -> None:
    """Same rule as ownership: history is never read from the branch itself."""
    provider = FakeProvider()
    outcome = await history.gather(provider, _pr(), _changes(), store=FakeStore(), ref="", now=NOW)
    assert provider.asked == []
    assert outcome.authored_report.status == "unavailable"
    assert "never read from the head" in outcome.authored_report.detail


def test_the_window_is_measured_against_the_clock_it_is_given() -> None:
    """Guards against a test that passes because it froze the wrong clock.

    One reading of the clock, not two: two calls are microseconds apart, which
    is a real age, and asserting it away with an exact 1.0 made this test fail
    on whichever machine was slower that day.
    """
    moment = time.time()
    assert history.recency(moment, now=moment, window_days=1) == 1.0


def test_a_touch_with_no_recorded_time_is_worth_the_floor() -> None:
    """A row that carries no timestamp is not a fresh row."""
    assert history.recency(0.0, now=NOW, window_days=180) == history.MIN_RECENCY
