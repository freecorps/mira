"""Dashboard routes for the captured log trail.

Every route here is admin-only, and unlike most of the admin surface that is
not a judgement call. Log lines are the least structured thing Mira stores:
they carry URLs, request bodies, model output and whatever an exception put in
its message, from every part of the system at once. Redaction runs before a
line is stored (see :mod:`mira.logs`), but redaction is a filter that fails
towards safety, not a guarantee, and the right audience for the residue is the
person who already administers the install.

The read routes are admin-only for the same reason the triage panel's are: this
is operational data about the whole org, and there is no per-repository slice
of it that would be safe to hand to somebody who can only see one repository —
a review of a private repository logs its name.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import TypedDict

from fastapi import Request
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel

from mira.dashboard.api import _require_admin, router

logger = logging.getLogger(__name__)

_MAX_PAGE = 500
_MAX_EXPORT = 5_000

# Level names the API accepts, and the numeric floor each one selects. "ALL"
# is spelled out rather than left as an empty string: a filter that means
# "everything" should say so in a URL somebody is pasting into a bug report.
_LEVEL_FLOORS = {
    "ALL": 0,
    "DEBUG": logging.DEBUG,
    "INFO": logging.INFO,
    "WARNING": logging.WARNING,
    "ERROR": logging.ERROR,
    "CRITICAL": logging.CRITICAL,
}


class AppLogEntry(BaseModel):
    id: int
    created_at: float
    level: str
    level_no: int
    logger: str
    message: str
    traceback: str = ""
    module: str = ""
    func_name: str = ""
    lineno: int = 0
    trace_id: str = ""
    repo: str = ""
    pr_number: int = 0
    thread: str = ""


class AppLogPage(BaseModel):
    entries: list[AppLogEntry]
    total: int
    limit: int
    offset: int
    # Capture state travels with the page so an empty result can explain
    # itself. "No logs" and "log capture is switched off" look identical in a
    # table, and only one of them is a reason to go and change an env var.
    capture_enabled: bool = True
    capture_level: str = "INFO"
    dropped: int = 0
    write_errors: int = 0


class AppLogLogger(BaseModel):
    logger: str
    count: int


class _LogFilters(TypedDict):
    """The filter set the table, the count and the export all share.

    Spelled out as a type rather than left as a plain dict so the three call
    sites cannot drift apart silently: an export that reads one fewer filter
    than the table above it shows more than the person clicking it asked for.
    """

    min_level: int
    logger_name: str
    query: str
    trace_id: str
    repo: str
    since: float


def _filters(
    level: str, logger_name: str, q: str, trace_id: str, repo: str, hours: float
) -> _LogFilters:
    return {
        "min_level": _floor(level),
        "logger_name": logger_name.strip(),
        "query": q.strip(),
        "trace_id": trace_id.strip(),
        "repo": repo.strip(),
        "since": _since(hours),
    }


def _page(limit: int, offset: int) -> tuple[int, int]:
    return max(1, min(limit, _MAX_PAGE)), max(0, offset)


def _floor(level: str) -> int:
    return _LEVEL_FLOORS.get((level or "").strip().upper(), logging.INFO)


def _since(hours: float) -> float:
    """Epoch cutoff for a trailing window, or 0 for "no lower bound"."""
    if hours <= 0:
        return 0.0
    return datetime.now(tz=UTC).timestamp() - hours * 3600


def _capture_state() -> tuple[bool, str, int, int]:
    from mira.logs import active_handler, capture_level

    handler = active_handler()
    if handler is None:
        return False, logging.getLevelName(capture_level()), 0, 0
    return (
        True,
        logging.getLevelName(handler.level),
        handler.dropped,
        handler.write_errors,
    )


@router.get("/api/logs", response_model=AppLogPage)
def list_logs(
    request: Request,
    level: str = "INFO",
    logger_name: str = "",
    q: str = "",
    trace_id: str = "",
    repo: str = "",
    hours: float = 24.0,
    limit: int = 200,
    offset: int = 0,
) -> AppLogPage:
    """The captured log trail, newest first, filtered. Admin only.

    ``hours`` defaults to a day rather than to everything: the common reason to
    open this page is something that just happened, and the retention window
    can hold a week of a busy install's output.
    """
    _require_admin(request)
    from mira.dashboard.api import _app_db

    enabled, capture_level_name, dropped, write_errors = _capture_state()
    if _app_db is None:  # pragma: no cover - only unconfigured installs
        return AppLogPage(
            entries=[],
            total=0,
            limit=limit,
            offset=offset,
            capture_enabled=enabled,
            capture_level=capture_level_name,
        )

    limit, offset = _page(limit, offset)
    filters = _filters(level, logger_name, q, trace_id, repo, hours)
    rows = _app_db.list_app_logs(**filters, limit=limit, offset=offset)
    return AppLogPage(
        entries=[AppLogEntry(**row) for row in rows],
        total=_app_db.count_app_logs(**filters),
        limit=limit,
        offset=offset,
        capture_enabled=enabled,
        capture_level=capture_level_name,
        dropped=dropped,
        write_errors=write_errors,
    )


@router.get("/api/logs/loggers", response_model=list[AppLogLogger])
def list_log_loggers(request: Request) -> list[AppLogLogger]:
    """Logger names present in the trail, busiest first — the filter menu."""
    _require_admin(request)
    from mira.dashboard.api import _app_db

    if _app_db is None:  # pragma: no cover - only unconfigured installs
        return []
    return [AppLogLogger(**row) for row in _app_db.list_app_log_loggers()]


@router.get("/api/logs/export", response_class=PlainTextResponse)
def export_logs(
    request: Request,
    level: str = "INFO",
    logger_name: str = "",
    q: str = "",
    trace_id: str = "",
    repo: str = "",
    hours: float = 24.0,
    limit: int = _MAX_EXPORT,
) -> PlainTextResponse:
    """The same trail as plain text, oldest first, for pasting into an issue.

    Oldest first because this output is read as a story rather than scanned as
    a table, and a stack trace that arrives before the thing that caused it
    reads backwards. Same filters as the table, so what gets exported is what
    was on screen — an export that quietly widens the filter is how a private
    repository's name ends up in a public bug report.
    """
    _require_admin(request)
    from mira.dashboard.api import _app_db

    if _app_db is None:  # pragma: no cover - only unconfigured installs
        return PlainTextResponse("")
    rows = _app_db.list_app_logs(
        **_filters(level, logger_name, q, trace_id, repo, hours),
        limit=max(1, min(limit, _MAX_EXPORT)),
    )
    return PlainTextResponse(
        format_log_text(list(reversed(rows))),
        headers={"Content-Disposition": 'attachment; filename="mira-logs.txt"'},
    )


def format_log_text(rows: list[dict]) -> str:
    """Render log rows as the plain text the export and the copy button share.

    One line per record with the traceback indented under it, so a paste into
    an issue keeps the shape a terminal would have given it.
    """
    lines: list[str] = []
    for row in rows:
        stamp = datetime.fromtimestamp(row.get("created_at") or 0.0, tz=UTC).isoformat(
            timespec="milliseconds"
        )
        context = ""
        trace_id = row.get("trace_id") or ""
        repo = row.get("repo") or ""
        pr_number = row.get("pr_number") or 0
        if trace_id or repo:
            where = f"{repo}#{pr_number}" if repo and pr_number else repo
            context = f" [{' '.join(p for p in (trace_id, where) if p)}]"
        lines.append(
            f"{stamp} {row.get('level', ''):<8} {row.get('logger', '')}{context} "
            f"{row.get('message', '')}"
        )
        tb = row.get("traceback") or ""
        if tb:
            lines.extend(f"    {tb_line}" for tb_line in tb.rstrip().splitlines())
    return "\n".join(lines)


@router.delete("/api/logs")
def clear_logs(request: Request) -> dict:
    """Delete the whole captured trail. Admin only.

    Kept because the alternative to a button is somebody deleting rows in
    psql, and because a trail that has just been filled by one runaway loop is
    more usefully empty than paged through.
    """
    _require_admin(request)
    from mira.dashboard.api import _app_db

    if _app_db is None:  # pragma: no cover - only unconfigured installs
        return {"ok": True, "deleted": 0}
    deleted = _app_db.clear_app_logs()
    logger.info("Captured log trail cleared (%d lines)", deleted)
    return {"ok": True, "deleted": deleted}
