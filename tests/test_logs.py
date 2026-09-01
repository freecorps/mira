"""Tests for the captured log trail.

Covers the three pieces that have to hold together for the Logs page to be
worth anything: the handler (what gets captured, what is deliberately not, and
what happens when it cannot keep up), the store (filters, retention), and the
routes (admin-only, and an export that matches the table it came from).
"""

from __future__ import annotations

import contextlib
import logging
import sqlite3
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from mira.dashboard.db import AppDatabase
from mira.dashboard.routers.logs import (
    clear_logs,
    export_logs,
    format_log_text,
    list_log_loggers,
    list_logs,
)
from mira.logs import (
    LogCaptureHandler,
    current_trace_id,
    install_log_capture,
    log_context,
    new_trace_id,
    set_log_target,
    uninstall_log_capture,
)


def _admin_req():
    return SimpleNamespace(state=SimpleNamespace(user=SimpleNamespace(is_admin=True)))


def _user_req():
    return SimpleNamespace(state=SimpleNamespace(user=SimpleNamespace(is_admin=False)))


class _FakeSink:
    """Stands in for AppDatabase. Records batches; can be told to fail."""

    def __init__(self, fail: bool = False) -> None:
        self.batches: list[list[dict]] = []
        self.pruned = 0
        self.fail = fail
        self._lock = threading.Lock()

    def record_app_logs(self, entries: list[dict]) -> int:
        if self.fail:
            raise RuntimeError("disk is full")
        with self._lock:
            self.batches.append(list(entries))
        return len(entries)

    def prune_app_logs(self, *, max_age_days: float, max_rows: int) -> int:
        self.pruned += 1
        return 0

    @property
    def entries(self) -> list[dict]:
        with self._lock:
            return [e for batch in self.batches for e in batch]


def _entry_stub() -> dict:
    """A well-formed queue entry, for tests that need the queue occupied."""
    return {"created_at": 0.0, "message": "stub", "traceback": ""}


def _record(
    name: str = "mira.test",
    level: int = logging.INFO,
    msg: str = "hello",
    args: tuple = (),
    exc_info=None,
) -> logging.LogRecord:
    return logging.LogRecord(
        name=name,
        level=level,
        pathname="test.py",
        lineno=7,
        msg=msg,
        args=args,
        exc_info=exc_info,
        func="a_function",
    )


@pytest.fixture
def db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AppDatabase:
    """Fresh SQLite app database, swapped in for the module-level one."""
    monkeypatch.setenv("MIRA_INDEX_DIR", str(tmp_path))
    database = AppDatabase(url="", admin_password="admin")
    monkeypatch.setattr("mira.dashboard.api._app_db", database)
    return database


class TestTraceContext:
    def test_trace_id_is_64_bits_of_hex(self):
        """Width is the whole point: at 32 bits a week's worth of reviews
        collide often enough to interleave two of them behind one filter."""
        trace = new_trace_id()
        assert len(trace) == 16
        int(trace, 16)  # raises if it is not hex

    def test_trace_ids_do_not_repeat(self):
        assert len({new_trace_id() for _ in range(1000)}) == 1000

    def test_context_sets_and_restores(self):
        assert current_trace_id() == ""
        with log_context() as trace:
            assert current_trace_id() == trace
        assert current_trace_id() == ""

    def test_nested_context_restores_the_outer_id(self):
        with log_context() as outer:
            with log_context() as inner:
                assert current_trace_id() == inner
            assert current_trace_id() == outer

    def test_set_log_target_does_not_outlive_the_context(self):
        handler = LogCaptureHandler(_FakeSink(), level=logging.INFO)
        try:
            with log_context():
                set_log_target(repo="acme/widgets", pr_number=42)
                handler.emit(_record(msg="inside"))
            handler.emit(_record(msg="outside"))
            handler.close()
        finally:
            handler.close()
        by_message = {e["message"]: e for e in handler._sink.entries}  # type: ignore[attr-defined]
        assert by_message["inside"]["repo"] == "acme/widgets"
        assert by_message["inside"]["pr_number"] == 42
        # The reset restores what was there before the *outer* set, whatever
        # was set in between — this is the assertion that keeps one review's
        # log lines from being labelled with the previous review's PR.
        assert by_message["outside"]["repo"] == ""
        assert by_message["outside"]["pr_number"] == 0


class TestCaptureHandler:
    def test_captures_message_level_and_location(self):
        sink = _FakeSink()
        handler = LogCaptureHandler(sink, level=logging.INFO)
        handler.emit(_record(msg="reviewing %s", args=("acme/widgets#1",)))
        handler.close()
        (entry,) = sink.entries
        assert entry["message"] == "reviewing acme/widgets#1"
        assert entry["level"] == "INFO"
        assert entry["logger"] == "mira.test"
        assert entry["func_name"] == "a_function"
        assert entry["lineno"] == 7

    def test_records_the_traceback(self):
        sink = _FakeSink()
        handler = LogCaptureHandler(sink, level=logging.INFO)
        try:
            raise ValueError("boom")
        except ValueError:
            import sys

            handler.emit(_record(level=logging.ERROR, msg="failed", exc_info=sys.exc_info()))
        handler.close()
        (entry,) = sink.entries
        assert "ValueError: boom" in entry["traceback"]

    def test_secrets_are_redacted_before_they_are_stored(self):
        sink = _FakeSink()
        handler = LogCaptureHandler(sink, level=logging.INFO)
        handler.emit(_record(msg='calling with token = "ghp_abcdefghijklmnopqrstuvwxyz01"'))
        handler.close()
        (entry,) = sink.entries
        assert "ghp_abcdefghijklmnopqrstuvwxyz01" not in entry["message"]
        assert "REDACTED" in entry["message"]

    def test_its_own_logger_is_never_captured(self):
        """The loop this prevents: a failed write is reported through logging,
        that report is captured, and it becomes the next batch to fail."""
        sink = _FakeSink()
        handler = LogCaptureHandler(sink, level=logging.INFO)
        handler.emit(_record(name="mira.logs", msg="could not persist"))
        handler.emit(_record(name="mira.logs.writer", msg="child logger too"))
        handler.emit(_record(name="uvicorn.access", msg="GET /api/logs"))
        handler.emit(_record(name="mira.logstash", msg="not the same logger"))
        handler.close()
        assert [e["message"] for e in sink.entries] == ["not the same logger"]

    def test_a_broken_format_string_does_not_reach_the_caller(self):
        sink = _FakeSink()
        handler = LogCaptureHandler(sink, level=logging.INFO)
        handler.emit(_record(msg="expected %d args", args=("not a number",)))
        handler.close()
        # The record is kept — a line that cannot be formatted is still
        # evidence that the code path ran.
        assert sink.entries

    def test_a_full_queue_drops_and_counts_instead_of_blocking(self):
        sink = _FakeSink()
        handler = LogCaptureHandler(sink, level=logging.INFO, capacity=1)
        # Fill the single slot, then keep emitting. The point is that `emit`
        # returns rather than blocking a review behind a slow writer.
        handler._queue.put(_entry_stub())
        for _ in range(5):
            handler.emit(_record(msg="dropped"))
        assert handler.dropped == 5
        handler.close()

    def test_a_malformed_entry_does_not_kill_the_writer(self):
        """The writer thread is never restarted, so an exception escaping it
        costs every later line, not just the batch it was handling."""
        sink = _FakeSink()
        handler = LogCaptureHandler(sink, level=logging.INFO)
        handler._queue.put({"created_at": 0.0})  # missing every other key
        time.sleep(1.5)
        handler.emit(_record(msg="still working"))
        handler.close()
        assert "still working" in [e.get("message") for e in sink.entries]

    def test_a_failed_write_is_counted_and_does_not_raise(self):
        sink = _FakeSink(fail=True)
        handler = LogCaptureHandler(sink, level=logging.INFO)
        handler.emit(_record(msg="anything"))
        handler.close()
        assert handler.write_errors == 1

    def test_long_messages_are_truncated(self):
        sink = _FakeSink()
        handler = LogCaptureHandler(sink, level=logging.INFO)
        handler.emit(_record(msg="x" * 50_000))
        handler.close()
        (entry,) = sink.entries
        assert len(entry["message"]) == 8_000


class TestInstall:
    def test_capture_can_be_switched_off(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("MIRA_LOG_CAPTURE", "0")
        try:
            assert install_log_capture(_FakeSink()) is None
        finally:
            uninstall_log_capture()

    def test_install_is_idempotent_and_uninstall_detaches(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.delenv("MIRA_LOG_CAPTURE", raising=False)
        sink = _FakeSink()
        try:
            first = install_log_capture(sink)
            second = install_log_capture(_FakeSink())
            assert first is not None
            assert first is second
            assert first in logging.getLogger().handlers
        finally:
            uninstall_log_capture()
        assert all(not isinstance(h, LogCaptureHandler) for h in logging.getLogger().handlers)

    def test_stdout_handlers_keep_their_verbosity(self, monkeypatch: pytest.MonkeyPatch):
        """Turning on capture at DEBUG must not make the console noisier: the
        root logger drops to DEBUG so the new handler sees those records, and
        the handlers that were already there are pinned where they were."""
        monkeypatch.delenv("MIRA_LOG_CAPTURE", raising=False)
        monkeypatch.setenv("MIRA_LOG_CAPTURE_LEVEL", "DEBUG")
        root = logging.getLogger()
        console = logging.StreamHandler()
        root.addHandler(console)
        original_level = root.level
        root.setLevel(logging.INFO)
        try:
            install_log_capture(_FakeSink())
            assert root.level == logging.DEBUG
            assert console.level == logging.INFO
        finally:
            uninstall_log_capture()
            root.removeHandler(console)
            root.setLevel(original_level)


class TestLogStore:
    def test_round_trip(self, db: AppDatabase):
        db.record_app_logs(
            [
                {
                    "created_at": 1000.0,
                    "level": "ERROR",
                    "level_no": 40,
                    "logger": "mira.llm.base",
                    "message": "Tool call failed",
                    "traceback": "Traceback…",
                    "module": "base",
                    "func_name": "complete_with_tools",
                    "lineno": 660,
                    "trace_id": "abc12345",
                    "repo": "acme/widgets",
                    "pr_number": 7,
                    "thread": "MainThread",
                }
            ]
        )
        (row,) = db.list_app_logs(since=0)
        assert row["message"] == "Tool call failed"
        assert row["trace_id"] == "abc12345"
        assert row["pr_number"] == 7

    def test_filters(self, db: AppDatabase):
        now = time.time()
        db.record_app_logs(
            [
                {
                    "created_at": now,
                    "level": "INFO",
                    "level_no": 20,
                    "logger": "mira.core.engine",
                    "message": "Review starting",
                    "trace_id": "aaaa1111",
                    "repo": "acme/widgets",
                },
                {
                    "created_at": now,
                    "level": "ERROR",
                    "level_no": 40,
                    "logger": "mira.llm.base",
                    "message": "Tool call failed on GLM",
                    "traceback": "LLMError: nope",
                    "trace_id": "bbbb2222",
                    "repo": "acme/other",
                },
            ]
        )
        assert db.count_app_logs(min_level=logging.ERROR) == 1
        assert db.count_app_logs(trace_id="aaaa1111") == 1
        assert db.count_app_logs(logger_name="llm") == 1
        assert db.count_app_logs(repo="ACME/Widgets") == 1
        # Case-insensitive on both backends, and tracebacks are searched too.
        assert db.count_app_logs(query="TOOL CALL") == 1
        assert db.count_app_logs(query="llmerror") == 1
        assert db.count_app_logs(since=now + 60) == 0

    def test_newest_first_and_paging(self, db: AppDatabase):
        db.record_app_logs(
            [
                {"created_at": float(i), "level": "INFO", "level_no": 20, "message": f"m{i}"}
                for i in range(5)
            ]
        )
        rows = db.list_app_logs(since=0, limit=2)
        assert [r["message"] for r in rows] == ["m4", "m3"]
        rows = db.list_app_logs(since=0, limit=2, offset=2)
        assert [r["message"] for r in rows] == ["m2", "m1"]

    def test_prune_by_age_and_by_row_count(self, db: AppDatabase):
        now = time.time()
        db.record_app_logs(
            [
                {"created_at": now - 30 * 86400, "level": "INFO", "level_no": 20, "message": "old"},
                *[
                    {"created_at": now, "level": "INFO", "level_no": 20, "message": f"new{i}"}
                    for i in range(5)
                ],
            ]
        )
        assert db.prune_app_logs(max_age_days=7, max_rows=0) == 1
        assert db.count_app_logs(since=0) == 5
        db.prune_app_logs(max_age_days=0, max_rows=2)
        assert db.count_app_logs(since=0) == 2

    def test_logger_menu_is_busiest_first(self, db: AppDatabase):
        db.record_app_logs(
            [
                {"created_at": 1.0, "level": "INFO", "level_no": 20, "logger": "quiet"},
                *[
                    {"created_at": 1.0, "level": "INFO", "level_no": 20, "logger": "loud"}
                    for _ in range(3)
                ],
            ]
        )
        assert [r["logger"] for r in db.list_app_log_loggers()] == ["loud", "quiet"]

    def test_clear(self, db: AppDatabase):
        db.record_app_logs([{"created_at": 1.0, "level": "INFO", "level_no": 20}])
        assert db.clear_app_logs() == 1
        assert db.count_app_logs(since=0) == 0


class TestLogRoutes:
    def test_reads_are_admin_only(self, db: AppDatabase):
        for call in (
            lambda: list_logs(_user_req()),
            lambda: list_log_loggers(_user_req()),
            lambda: export_logs(_user_req()),
            lambda: clear_logs(_user_req()),
        ):
            with pytest.raises(HTTPException) as exc:
                call()
            assert exc.value.status_code == 403

    def test_page_carries_the_capture_state(self, db: AppDatabase):
        page = list_logs(_admin_req())
        # Nothing is installed under test, so the page says so rather than
        # showing an empty table with no explanation.
        assert page.capture_enabled is False
        assert page.entries == []

    def test_level_and_trace_filters_reach_the_store(self, db: AppDatabase):
        now = time.time()
        db.record_app_logs(
            [
                {
                    "created_at": now,
                    "level": "INFO",
                    "level_no": 20,
                    "message": "fine",
                    "trace_id": "aaaa1111",
                },
                {
                    "created_at": now,
                    "level": "ERROR",
                    "level_no": 40,
                    "message": "broken",
                    "trace_id": "bbbb2222",
                },
            ]
        )
        page = list_logs(_admin_req(), level="ERROR")
        assert [e.message for e in page.entries] == ["broken"]
        assert page.total == 1
        page = list_logs(_admin_req(), trace_id="aaaa1111")
        assert [e.message for e in page.entries] == ["fine"]

    def test_hours_zero_means_everything_kept(self, db: AppDatabase):
        db.record_app_logs(
            [{"created_at": 1.0, "level": "INFO", "level_no": 20, "message": "ancient"}]
        )
        assert list_logs(_admin_req()).total == 0
        assert list_logs(_admin_req(), hours=0).total == 1

    def test_export_is_oldest_first_and_indents_tracebacks(self, db: AppDatabase):
        now = time.time()
        db.record_app_logs(
            [
                {"created_at": now - 1, "level": "INFO", "level_no": 20, "message": "first"},
                {
                    "created_at": now,
                    "level": "ERROR",
                    "level_no": 40,
                    "message": "second",
                    "traceback": "Traceback:\n  ValueError",
                },
            ]
        )
        body = export_logs(_admin_req()).body.decode()
        assert body.index("first") < body.index("second")
        assert "    Traceback:" in body

    def test_export_respects_the_filters(self, db: AppDatabase):
        """An export that quietly widens the filter is how a private repo's
        name ends up in a public bug report."""
        now = time.time()
        db.record_app_logs(
            [
                {
                    "created_at": now,
                    "level": "INFO",
                    "level_no": 20,
                    "message": "public repo line",
                    "repo": "acme/open",
                },
                {
                    "created_at": now,
                    "level": "INFO",
                    "level_no": 20,
                    "message": "private repo line",
                    "repo": "acme/secret",
                },
            ]
        )
        body = export_logs(_admin_req(), repo="acme/open").body.decode()
        assert "public repo line" in body
        assert "private repo line" not in body

    def test_clear_reports_what_it_deleted(self, db: AppDatabase):
        db.record_app_logs([{"created_at": 1.0, "level": "INFO", "level_no": 20}])
        assert clear_logs(_admin_req()) == {"ok": True, "deleted": 1}


class TestExportFormat:
    def test_context_is_rendered_when_present(self):
        text = format_log_text(
            [
                {
                    "created_at": 0.0,
                    "level": "ERROR",
                    "logger": "mira.llm.base",
                    "message": "Tool call failed",
                    "trace_id": "abc12345",
                    "repo": "acme/widgets",
                    "pr_number": 7,
                    "traceback": "",
                }
            ]
        )
        assert "[abc12345 acme/widgets#7]" in text
        assert "Tool call failed" in text

    def test_a_line_with_no_review_behind_it_carries_no_brackets(self):
        text = format_log_text(
            [{"created_at": 0.0, "level": "INFO", "logger": "mira.cli", "message": "starting"}]
        )
        assert "[" not in text


class TestFailureNoticeCarriesTheTrace:
    """The loop that makes any of this usable: a review fails, the notice on
    the pull request names a trace id, and that id filters the Logs page."""

    def test_notice_names_the_trace_inside_a_context(self):
        from mira.core.engine import ReviewEngine
        from mira.exceptions import LLMError

        exc = LLMError("tool_call_failed", model="some/model", error="boom")
        with log_context() as trace:
            notice = ReviewEngine._format_failure_notice(exc)
        assert f"`{trace}`" in notice
        # The safe message is still what a public pull request sees: no model
        # name, no underlying error.
        assert "some/model" not in notice
        assert "LLM tool-call failed" in notice

    def test_notice_omits_the_trace_outside_a_context(self):
        from mira.core.engine import ReviewEngine

        notice = ReviewEngine._format_failure_notice(RuntimeError("nope"))
        assert "Trace ID" not in notice


class TestWriterIsolation:
    """The log writer is a permanent background thread committing on a timer.
    Sharing a SQLite connection with the request path means its commits land in
    the middle of the dashboard's multi-statement writes."""

    def test_the_writer_gets_its_own_sqlite_connection(self, db: AppDatabase):
        db.record_app_logs([{"created_at": 1.0, "level": "INFO", "level_no": 20}])
        assert db._sqlite_log_conn is not None
        assert db._sqlite_log_conn is not db._sqlite_conn

    def test_a_writer_commit_cannot_commit_a_request_transaction(self, db: AppDatabase):
        """The failure this fixes: on a shared connection the log writer's
        `commit()` makes a half-written dashboard row durable, and the rollback
        that should have discarded it finds nothing left to discard.

        Separated, the writer either waits for the request or gives up on the
        batch — and a dropped batch is counted and reported, where a wrongly
        committed row is not.
        """
        assert db._sqlite_conn is not None
        db._sqlite_conn.execute(
            "INSERT INTO global_rules (title, content) VALUES ('half written', '')"
        )
        with contextlib.suppress(sqlite3.OperationalError):
            db.record_app_logs([{"created_at": 1.0, "level": "INFO", "level_no": 20}])
        db._sqlite_conn.rollback()
        assert [r.title for r in db.list_global_rules()] == []

    def test_the_writer_recovers_once_the_request_transaction_ends(self, db: AppDatabase):
        """A blocked batch is not a broken writer: the next one goes in."""
        assert db._sqlite_conn is not None
        db._sqlite_conn.execute(
            "INSERT INTO global_rules (title, content) VALUES ('half written', '')"
        )
        db._sqlite_conn.rollback()
        db.record_app_logs([{"created_at": 1.0, "level": "INFO", "level_no": 20}])
        assert db.count_app_logs(since=0) == 1

    def test_concurrent_writes_and_reads_stay_consistent(self, db: AppDatabase):
        """A smoke test for the shape the race actually takes: the writer
        inserting while request threads read and write other tables."""
        errors: list[Exception] = []

        def write_logs() -> None:
            try:
                for i in range(40):
                    db.record_app_logs([{"created_at": float(i), "level": "INFO", "level_no": 20}])
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        def use_dashboard() -> None:
            try:
                for i in range(40):
                    db.set_setting(f"key{i}", str(i))
                    db.get_setting(f"key{i}")
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=write_logs), threading.Thread(target=use_dashboard)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)
        assert errors == []
        assert db.count_app_logs(since=0) == 40
        assert db.get_setting("key39") == "39"


class TestExportCeiling:
    def test_the_export_ceiling_is_the_store_ceiling(self):
        """These two used to disagree — the route offered 5,000 lines and the
        query clamped at 2,000 — so an export stopped short without saying so."""
        from mira.dashboard.db import MAX_APP_LOG_ROWS
        from mira.dashboard.routers.logs import _MAX_EXPORT

        assert _MAX_EXPORT == MAX_APP_LOG_ROWS

    def test_a_request_at_the_ceiling_is_not_silently_trimmed(self, db: AppDatabase):
        from mira.dashboard.db import MAX_APP_LOG_ROWS

        wanted = 2_500  # above the old 2,000 clamp, below the ceiling
        assert wanted < MAX_APP_LOG_ROWS
        db.record_app_logs(
            [
                {"created_at": float(i), "level": "INFO", "level_no": 20, "message": f"m{i}"}
                for i in range(wanted)
            ]
        )
        assert len(db.list_app_logs(since=0, limit=wanted)) == wanted
