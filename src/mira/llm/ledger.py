"""The usage ledger: one row per LLM call, written off the review's path.

Each call's tokens, estimated cost and plan credits (see
:mod:`mira.llm.accounting`) go into the application database's ``llm_usage``
table, which is what the dashboard's cost view sums and what the estimated
allowance meter of a subscription with no usage API is computed from.

The rule is the one the log capture (:mod:`mira.logs`) is built on: **nothing
on a review's path waits for a database.** :func:`submit` drops the row on a
bounded queue and returns; a daemon thread batches and inserts, and prunes
rows past the retention window (``MIRA_USAGE_RETENTION_DAYS``, 90 by default)
on a timer. A full queue drops rows and counts them; a failed insert drops
the batch and counts it. Either way the review carries on.

With no application database — a plain ``mira review`` from the CLI — rows
have nowhere to go and are discarded; the review's own totals still reach its
report. ``MIRA_USAGE_LEDGER=0`` switches the ledger off entirely.
"""

from __future__ import annotations

import atexit
import logging
import os
import queue
import threading
import time
from typing import Any, Protocol

logger = logging.getLogger(__name__)

_QUEUE_CAPACITY = 5_000
_BATCH_MAX = 200
_BATCH_INTERVAL = 2.0
_PRUNE_INTERVAL = 3600.0
_SHUTDOWN_SECONDS = 2.0
DEFAULT_RETENTION_DAYS = 90.0


class _Sink(Protocol):
    def record_llm_usage(self, entries: list[dict[str, Any]]) -> int: ...

    def prune_llm_usage(self, *, max_age_days: float) -> int: ...


def enabled() -> bool:
    raw = os.environ.get("MIRA_USAGE_LEDGER", "").strip().lower()
    return raw not in ("0", "false", "no", "off")


def retention_days() -> float:
    try:
        days = float(os.environ.get("MIRA_USAGE_RETENTION_DAYS", "") or DEFAULT_RETENTION_DAYS)
    except ValueError:
        return DEFAULT_RETENTION_DAYS
    return days if days > 0 else DEFAULT_RETENTION_DAYS


def _default_sink() -> _Sink | None:
    from mira.oauth.store import default_db

    db: Any = default_db()
    return db if db is not None and hasattr(db, "record_llm_usage") else None


class _Writer:
    """The queue and the thread draining it."""

    def __init__(self) -> None:
        self._queue: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=_QUEUE_CAPACITY)
        self._lock = threading.Lock()
        # Rows accepted and not yet written (or given up on), so a flush can
        # wait for the batch the thread is holding as well as the queue.
        self._pending = 0
        self._settled = threading.Condition(self._lock)
        self._thread: threading.Thread | None = None
        self._stopping = threading.Event()
        self.dropped = 0
        self.write_errors = 0
        # Tests (and a process that wants its own database) point this at a
        # sink; None means "the dashboard's database, if there is one".
        self.sink: _Sink | None = None

    def _resolve(self) -> _Sink | None:
        return self.sink if self.sink is not None else _default_sink()

    def submit(self, row: dict[str, Any]) -> None:
        with self._lock:
            try:
                self._queue.put_nowait(row)
            except queue.Full:
                self.dropped += 1
                return
            self._pending += 1
        self._ensure_thread()

    def _ensure_thread(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stopping.clear()
            self._thread = threading.Thread(
                target=self._drain, name="mira-usage-ledger", daemon=True
            )
            self._thread.start()

    def _take(self, block: bool) -> list[dict[str, Any]]:
        batch: list[dict[str, Any]] = []
        try:
            batch.append(
                self._queue.get(timeout=_BATCH_INTERVAL) if block else self._queue.get_nowait()
            )
        except queue.Empty:
            return batch
        while len(batch) < _BATCH_MAX:
            try:
                batch.append(self._queue.get_nowait())
            except queue.Empty:
                break
        return batch

    def _write(self, batch: list[dict[str, Any]]) -> None:
        if not batch:
            return
        try:
            sink = self._resolve()
            if sink is not None:
                sink.record_llm_usage(batch)
        except Exception as exc:  # noqa: BLE001 - a lost batch is not a crash
            with self._lock:
                self.write_errors += 1
            logger.warning("Could not record %d LLM calls in the usage ledger: %s", len(batch), exc)
        finally:
            with self._lock:
                self._pending = max(0, self._pending - len(batch))
                self._settled.notify_all()

    def _prune(self) -> None:
        try:
            sink = self._resolve()
            if sink is not None:
                sink.prune_llm_usage(max_age_days=retention_days())
        except Exception as exc:  # noqa: BLE001
            logger.warning("Could not prune the usage ledger: %s", exc)

    def _drain(self) -> None:
        next_prune = time.monotonic() + 60.0
        while not self._stopping.is_set():
            # A short pause before each batch lets a burst of calls (every
            # chunk of a review finishing at once) land as one insert.
            batch = self._take(block=True)
            if batch:
                time.sleep(min(_BATCH_INTERVAL, 0.25))
                batch += self._take(block=False)
            self._write(batch)
            if time.monotonic() >= next_prune:
                self._prune()
                next_prune = time.monotonic() + _PRUNE_INTERVAL

    def flush(self, timeout: float = _SHUTDOWN_SECONDS) -> None:
        """Write whatever is queued, and wait for the batch in flight, within ``timeout``."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            batch = self._take(block=False)
            if not batch:
                break
            self._write(batch)
        with self._lock:
            while self._pending and time.monotonic() < deadline:
                self._settled.wait(timeout=max(0.0, deadline - time.monotonic()))

    def stop(self) -> None:
        self._stopping.set()
        self.flush()


_writer = _Writer()
atexit.register(_writer.stop)


def submit(row: dict[str, Any]) -> None:
    """Queue one call's row. Never blocks and never raises."""
    if not enabled():
        return
    try:
        _writer.submit(row)
    except Exception as exc:  # noqa: BLE001 - the ledger must not fail a call
        logger.debug("Usage ledger rejected a row: %s", exc)


def flush(timeout: float = _SHUTDOWN_SECONDS) -> None:
    """Write what is queued now. For tests, and for a CLI run about to exit."""
    _writer.flush(timeout)


def set_sink(sink: _Sink | None) -> None:
    """Send rows to ``sink`` instead of the dashboard's database (None restores it)."""
    _writer.sink = sink


def stats() -> dict[str, int]:
    """Rows the ledger could not keep, for a page that wants to say so."""
    return {
        "dropped": _writer.dropped,
        "write_errors": _writer.write_errors,
        "queued": _writer._queue.qsize(),
    }
