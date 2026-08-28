"""The record of what was read.

A read-only surface leaves no other trace. Nothing it does changes a finding,
a rule or a review, so if the question "what did that agent read, and when?"
has an answer, this is where it comes from. That makes the log part of the
security of the feature rather than an operational nicety, and it is why
refusals are recorded as carefully as successes: an agent asking repeatedly
for a repository it was not granted is the only attack shape this surface has,
and it is invisible unless the refusals are written down.

Two places, always both. The row goes to Mira's application database, next to
the configuration trail; the line goes to stderr, because the server is
launched as a subprocess by an MCP client and stderr is where a client and an
operator can both see it. A database that is unreachable therefore degrades
the log rather than silencing it.

What is never written: the content that was returned. The trail says which
tool was called, for which repository, with which arguments, and how many rows
came back. Copying the rows here would make the audit log a second, permanent,
unredacted copy of everything the surface exists to hand out carefully.
"""

from __future__ import annotations

import logging
import os
import time
import uuid
from contextlib import contextmanager
from typing import Any

from mira.autofix.redact import redact

logger = logging.getLogger("mira.mcp.audit")

OK = "ok"
REFUSED = "refused"
FAILED = "failed"


def _redact_deeply(value: Any) -> Any:
    """Run the redaction filter over every string in a nested structure."""
    if isinstance(value, str):
        return redact(value)
    if isinstance(value, dict):
        return {str(key): _redact_deeply(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_redact_deeply(item) for item in value]
    return value


def _open_app_db() -> Any:
    """The application database, or None if this install has none reachable.

    Imported here rather than at module scope: the dashboard package pulls in
    optional server dependencies, and the MCP server runs on installs that
    have none of them.
    """
    from mira.dashboard.db import AppDatabase

    return AppDatabase(
        os.environ.get("DATABASE_URL", ""),
        admin_password=os.environ.get("ADMIN_PASSWORD", ""),
    )


class AuditLog:
    """Records one line and one row per tool call. Never raises."""

    def __init__(self, *, enabled: bool = True, client: str = "", db: Any = None) -> None:
        self.enabled = enabled
        self.client = client
        self.session_id = uuid.uuid4().hex[:16]
        self._db = db
        self._db_attempted = db is not None
        self._db_broken = False

    def _database(self) -> Any:
        if self._db is not None or self._db_broken:
            return self._db
        if not self._db_attempted:
            self._db_attempted = True
            try:
                self._db = _open_app_db()
            except Exception as exc:  # noqa: BLE001 - an audit sink is not a dependency
                self._db_broken = True
                logger.warning(
                    "MCP audit rows are going to stderr only: the application "
                    "database is not available (%s)",
                    exc,
                )
        return self._db

    def record(
        self,
        *,
        tool: str,
        repository: str = "",
        arguments: dict[str, Any] | None = None,
        outcome: str = OK,
        detail: str = "",
        result_count: int = 0,
        duration_ms: float = 0.0,
    ) -> None:
        if not self.enabled:
            return
        # Arguments are client-supplied and arbitrary: `tools/call` takes any
        # JSON object, and a call is audited before - and even without - a tool
        # validating its shape. So the filter goes all the way down. Redacting
        # only the top level would leave `{"metadata": {"token": "ghp_..."}}`
        # in the database verbatim, which is a secret stored by the very
        # feature that exists to keep track of secrets not leaving.
        safe_arguments = _redact_deeply(arguments or {})
        logger.info(
            "mcp %s tool=%s repository=%s outcome=%s rows=%d ms=%.1f %s",
            self.session_id,
            tool,
            repository or "-",
            outcome,
            result_count,
            duration_ms,
            redact(detail)[:200],
        )
        database = self._database()
        if database is None:
            return
        try:
            database.record_mcp_audit(
                session_id=self.session_id,
                client=self.client,
                tool=tool,
                repository=repository,
                arguments=safe_arguments,
                outcome=outcome,
                detail=redact(detail),
                result_count=result_count,
                duration_ms=duration_ms,
            )
        except Exception as exc:  # noqa: BLE001 - see the module docstring
            self._db_broken = True
            logger.warning("Could not write an MCP audit row (%s)", exc)

    @contextmanager
    def call(self, tool: str, arguments: dict[str, Any] | None = None):  # type: ignore[no-untyped-def]
        """Time one call and record it however it ends.

        The caller fills in the repository and the row count as it learns them;
        an exception on the way is recorded as a failure and re-raised, so a
        call that crashed is in the trail rather than missing from it.
        """
        started = time.monotonic()
        state: dict[str, Any] = {"repository": "", "outcome": OK, "detail": "", "result_count": 0}
        try:
            yield state
        except Exception as exc:
            state["outcome"] = FAILED
            state["detail"] = f"{type(exc).__name__}: {exc}"
            raise
        finally:
            self.record(
                tool=tool,
                repository=state.get("repository", ""),
                arguments=arguments,
                outcome=state.get("outcome", OK),
                detail=state.get("detail", ""),
                result_count=int(state.get("result_count", 0)),
                duration_ms=(time.monotonic() - started) * 1000.0,
            )
