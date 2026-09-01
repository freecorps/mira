"""Capturing Mira's own log output so a failure can be traced from the dashboard.

Everything Mira does that can fail — a review, an index run, a webhook, a call
to a model — already says so through :mod:`logging`. The problem was never that
the information does not exist; it is that it exists on a container's stdout,
which is the one record reliably gone by the time somebody goes looking. A
restart takes it, and the person who needs it (whoever saw "Review failed" on a
pull request) is usually not the person who can reach the host.

This module attaches a handler to the root logger that puts every record it
sees into the application database, where the dashboard can filter, search and
copy it. Three decisions are worth naming.

**Nothing on a review's path waits for a database.** ``emit`` formats the
record and drops it on a bounded queue; a writer thread batches and inserts.
When the queue is full, records are dropped and counted rather than blocking a
review behind a slow disk, and the count is surfaced in the API so the gap is
visible instead of silent.

**A log line about a failed log write cannot be captured.** This module's own
logger is excluded, which is what lets the database layer raise on a failed
insert and be reported normally: the report goes to stdout and stops there,
instead of becoming the next batch's traffic.

**The trail is redacted before it is stored, not before it is shown.** Log
lines carry URLs, headers and model responses, and a credential in a database
somebody can read from a browser is worse than one on a terminal — the same
reasoning :mod:`mira.autofix.redact` is built on, applied to the same rules.

A ``trace_id`` is what makes the table worth more than ``docker logs``. Reviews
run concurrently and interleave their output; :func:`log_context` tags every
record a review emits with one id, the failure notice on the pull request
prints it, and "this pull request failed" becomes a single filter rather than a
hunt through timestamps.
"""

from __future__ import annotations

import atexit
import contextlib
import logging
import os
import queue
import secrets
import threading
import time
import traceback as _traceback
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any, Protocol

logger = logging.getLogger(__name__)

# Records from these loggers are never captured. `mira.logs` is this module —
# capturing it would let a database failure report itself into the queue that
# is failing to drain. `uvicorn.access` is one line per HTTP request, including
# every poll the logs page itself makes, which would crowd out the review lines
# the page exists to show.
_EXCLUDED_LOGGERS = ("mira.logs", "uvicorn.access")


def _excluded(name: str) -> bool:
    """Whether this logger is one of the excluded ones, or a child of it.

    Matched on the dotted hierarchy rather than by string prefix, so a future
    ``mira.logstash`` is not silently swallowed by the ``mira.logs`` entry.
    """
    return any(name == ex or name.startswith(f"{ex}.") for ex in _EXCLUDED_LOGGERS)


# Truncation limits. A model can return tens of kilobytes and a traceback from
# a deep async stack is not much smaller; the head of either answers the
# question, and storing the whole thing per line is how this table becomes the
# largest one in the database.
_MAX_MESSAGE = 8_000
_MAX_TRACEBACK = 20_000

# Queue depth. Deliberately generous — a review at DEBUG can emit hundreds of
# lines in a burst — and still bounded, because the alternative to dropping is
# holding them all in memory.
_QUEUE_CAPACITY = 20_000

# Batch shape: flush when either fills up. The interval is what keeps the logs
# page useful while something is still running; the size is what keeps a burst
# from becoming twenty thousand single-row inserts.
_BATCH_MAX = 250
_BATCH_INTERVAL = 1.0

# How often the writer prunes, in seconds. Pruning on a timer rather than per
# batch: the trim is two statements and neither belongs on every insert.
_PRUNE_INTERVAL = 300.0

_LEVELS = {
    "CRITICAL": logging.CRITICAL,
    "ERROR": logging.ERROR,
    "WARNING": logging.WARNING,
    "INFO": logging.INFO,
    "DEBUG": logging.DEBUG,
}


class _LogSink(Protocol):
    """The slice of ``AppDatabase`` the handler needs. Narrow on purpose: it
    keeps this module importable without the dashboard, and makes the writer
    thread trivial to test against a list."""

    def record_app_logs(self, entries: list[dict[str, Any]]) -> int: ...

    def prune_app_logs(self, *, max_age_days: float, max_rows: int) -> int: ...


# ── Correlation context ────────────────────────────────────────────

_trace_id: ContextVar[str] = ContextVar("mira_trace_id", default="")
_trace_repo: ContextVar[str] = ContextVar("mira_trace_repo", default="")
_trace_pr: ContextVar[int] = ContextVar("mira_trace_pr", default=0)


def new_trace_id() -> str:
    """A short, copy-pasteable correlation id.

    Eight hex characters, not a UUID: this id is printed in a pull request
    comment for a human to retype or paste into a filter box, and a 36-character
    one gets truncated by whoever passes it on. The collision risk that buys is
    irrelevant — ids only ever need to be distinct among the lines still in the
    retention window, not globally unique.
    """
    return secrets.token_hex(4)


def current_trace_id() -> str:
    """The trace id of the work running on this task, or ``""`` outside one."""
    return _trace_id.get()


def set_log_target(*, repo: str = "", pr_number: int = 0) -> None:
    """Name the pull request the current work is about, mid-context.

    A review opens its log context before it knows what it is reviewing — the
    trace id has to exist for the lines emitted while fetching the pull request
    — so the repo and number are filled in once they are known. The enclosing
    :func:`log_context` still owns the reset, and a reset restores the value
    from before *its* set whatever happened in between, so this cannot leak
    into whatever runs next.
    """
    _trace_repo.set(repo or "")
    _trace_pr.set(int(pr_number or 0))


@contextmanager
def log_context(*, repo: str = "", pr_number: int = 0, trace_id: str = "") -> Iterator[str]:
    """Tag every log record emitted inside this block with a trace id.

    Uses context variables rather than thread locals because the work being
    tagged is asyncio: a review is a task tree that hops threads freely, and a
    thread local would tag whichever review happened to be on that thread when
    a callback fired.
    """
    trace = trace_id or new_trace_id()
    tokens = (
        _trace_id.set(trace),
        _trace_repo.set(repo or ""),
        _trace_pr.set(int(pr_number or 0)),
    )
    try:
        yield trace
    finally:
        _trace_id.reset(tokens[0])
        _trace_repo.reset(tokens[1])
        _trace_pr.reset(tokens[2])


# ── The handler ────────────────────────────────────────────────────


class LogCaptureHandler(logging.Handler):
    """Root-logger handler that batches records into the application database."""

    def __init__(
        self,
        sink: _LogSink,
        *,
        level: int = logging.INFO,
        capacity: int = _QUEUE_CAPACITY,
        max_age_days: float = 7.0,
        max_rows: int = 200_000,
    ) -> None:
        super().__init__(level=level)
        self._sink = sink
        self._max_age_days = max_age_days
        self._max_rows = max_rows
        self._queue: queue.Queue[dict[str, Any] | None] = queue.Queue(maxsize=capacity)
        self._dropped = 0
        self._write_errors = 0
        self._counter_lock = threading.Lock()
        self._stopping = threading.Event()
        self._thread = threading.Thread(target=self._drain, name="mira-log-writer", daemon=True)
        self._thread.start()

    @property
    def dropped(self) -> int:
        """Records the queue had no room for. Non-zero means the page is
        showing an incomplete trail, which is worth saying out loud."""
        with self._counter_lock:
            return self._dropped

    @property
    def write_errors(self) -> int:
        """Batches the database refused. Same reasoning as ``dropped``."""
        with self._counter_lock:
            return self._write_errors

    def emit(self, record: logging.LogRecord) -> None:
        """Queue one record. Runs on the caller's thread, so it never blocks."""
        if _excluded(record.name):
            return
        try:
            entry = _entry_from(record)
        except Exception:  # noqa: BLE001 - a broken record must not break the caller
            return
        try:
            self._queue.put_nowait(entry)
        except queue.Full:
            with self._counter_lock:
                self._dropped += 1

    def close(self) -> None:
        """Stop the writer and flush what is queued. Bounded wait: shutdown is
        not allowed to hang on a database that has stopped answering."""
        if not self._stopping.is_set():
            self._stopping.set()
            with contextlib.suppress(queue.Full):
                self._queue.put_nowait(None)
            self._thread.join(timeout=3.0)
        super().close()

    # ── writer thread ──

    def _drain(self) -> None:
        next_prune = time.monotonic() + _PRUNE_INTERVAL
        while True:
            batch, stop = self._collect_batch()
            if batch:
                self._write(batch)
            if time.monotonic() >= next_prune:
                self._prune()
                next_prune = time.monotonic() + _PRUNE_INTERVAL
            if stop:
                return

    def _collect_batch(self) -> tuple[list[dict[str, Any]], bool]:
        """Block for one record, then take whatever else arrives within the
        flush window. Returns the batch and whether a stop sentinel was seen."""
        batch: list[dict[str, Any]] = []
        try:
            first = self._queue.get(timeout=_BATCH_INTERVAL)
        except queue.Empty:
            return batch, False
        if first is None:
            return batch, True
        batch.append(first)
        deadline = time.monotonic() + _BATCH_INTERVAL
        while len(batch) < _BATCH_MAX:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            try:
                item = self._queue.get(timeout=remaining)
            except queue.Empty:
                break
            if item is None:
                return batch, True
            batch.append(item)
        return batch, False

    def _write(self, batch: list[dict[str, Any]]) -> None:
        """Redact and store one batch. Everything fallible is inside the try.

        The writer thread is not restarted if it dies, so an escaping exception
        here does not cost a batch — it costs every line the process logs from
        then on, silently. That makes the boundary worth drawing wide: the
        redaction pass belongs inside it as much as the insert does.
        """
        from mira.autofix.redact import redact

        try:
            for entry in batch:
                entry["message"] = redact(entry.get("message") or "")
                if entry.get("traceback"):
                    entry["traceback"] = redact(entry["traceback"])
            self._sink.record_app_logs(batch)
        except Exception as exc:  # noqa: BLE001 - a dropped batch is not a crash
            with self._counter_lock:
                self._write_errors += 1
            # Safe to log: this module's logger is excluded from capture, so
            # the report goes to stdout and stops there.
            logger.warning("Could not persist %d captured log lines: %s", len(batch), exc)

    def _prune(self) -> None:
        try:
            self._sink.prune_app_logs(max_age_days=self._max_age_days, max_rows=self._max_rows)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Could not prune the captured log trail: %s", exc)


def _entry_from(record: logging.LogRecord) -> dict[str, Any]:
    """One database row's worth of a log record, read on the caller's thread.

    The context variables have to be read here rather than in the writer: by
    the time the batch is flushed the review that emitted the line is long out
    of scope, and the writer thread has no context of its own to read.
    """
    try:
        message = record.getMessage()
    except Exception:  # noqa: BLE001 - '%d' with a string arg, and similar
        message = str(record.msg)
    tb = ""
    if record.exc_info:
        tb = "".join(_traceback.format_exception(*record.exc_info))
    elif record.exc_text:
        tb = record.exc_text
    if record.stack_info:
        tb = f"{tb}\n{record.stack_info}" if tb else record.stack_info
    return {
        "created_at": record.created,
        "level": record.levelname,
        "level_no": record.levelno,
        "logger": record.name,
        "message": message[:_MAX_MESSAGE],
        "traceback": tb[:_MAX_TRACEBACK],
        "module": record.module or "",
        "func_name": record.funcName or "",
        "lineno": record.lineno or 0,
        "trace_id": _trace_id.get(),
        "repo": _trace_repo.get(),
        "pr_number": _trace_pr.get(),
        "thread": record.threadName or "",
    }


# ── Installation ───────────────────────────────────────────────────

_handler: LogCaptureHandler | None = None
_install_lock = threading.Lock()


def _env_flag(name: str, default: bool) -> bool:
    raw = os.environ.get(name, "").strip().lower()
    if not raw:
        return default
    return raw not in ("0", "false", "no", "off")


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, "") or default)
    except ValueError:
        return default


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, "") or default)
    except ValueError:
        return default


def capture_level() -> int:
    """The level at and above which records are stored."""
    name = os.environ.get("MIRA_LOG_CAPTURE_LEVEL", "").strip().upper()
    return _LEVELS.get(name, logging.INFO)


def install_log_capture(sink: _LogSink) -> LogCaptureHandler | None:
    """Attach the capture handler to the root logger. Idempotent.

    Returns the installed handler, or ``None`` when capture is switched off
    with ``MIRA_LOG_CAPTURE=0``.

    Lowering the root logger's level to reach the capture level would also make
    stdout that much noisier, which is a change nobody asked for by turning on
    a dashboard page. So the existing handlers are pinned to the level the root
    logger had first, and only the new one sees the extra records.
    """
    global _handler
    with _install_lock:
        if _handler is not None:
            return _handler
        if not _env_flag("MIRA_LOG_CAPTURE", True):
            return None
        level = capture_level()
        handler = LogCaptureHandler(
            sink,
            level=level,
            max_age_days=_env_float("MIRA_LOG_RETENTION_DAYS", 7.0),
            max_rows=_env_int("MIRA_LOG_MAX_ROWS", 200_000),
        )
        root = logging.getLogger()
        effective = root.level or logging.WARNING
        if effective > level:
            for existing in root.handlers:
                if existing.level == logging.NOTSET:
                    existing.setLevel(effective)
            root.setLevel(level)
        root.addHandler(handler)
        _handler = handler
        atexit.register(uninstall_log_capture)
        logger.info("Log capture enabled at %s", logging.getLevelName(level))
        return handler


def uninstall_log_capture() -> None:
    """Detach and flush the handler. Used at shutdown and by tests."""
    global _handler
    with _install_lock:
        handler = _handler
        _handler = None
    if handler is None:
        return
    logging.getLogger().removeHandler(handler)
    handler.close()


def active_handler() -> LogCaptureHandler | None:
    """The installed handler, if capture is on. The API reads its counters."""
    return _handler
