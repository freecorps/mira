"""The tools this server offers, which is a list that only shrinks.

Ten reads. There is no tool here that writes, approves, dismisses, triggers
a review, applies a fix, or runs a command, and that is a property of the
registry rather than of the current implementations: a tool is a name, a
schema, and a function in this module, so adding a side effect means adding it
here, in a file whose whole subject is that there are none.

Arguments are validated strictly, including rejecting names this server does
not know. The tempting alternative - ignore what you do not recognise - turns
a client's typo into a silent widening: `sevrity="blocker"` would quietly
return every finding of every severity, and the caller would read the result
as if it had been filtered.

Most tools read one repository and are reached through the grant. Two read
Mira's own log trail, which belongs to the install rather than to any
repository, and a grant cannot reach it: those tools require the `logs`
capability, which a session holds only when the HTTP transport authenticated an
admin's token. A session without it is not offered them in `tools/list` and is
refused if it calls one anyway.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from mira.mcp import reads
from mira.mcp.authz import Grant, Repository
from mira.mcp.limits import decode_anchored_cursor, decode_cursor, encode_cursor, page_size

#: The longest a string filter may be. Filters are bound parameters, so this
#: is about not doing pointless work rather than about injection.
MAX_FILTER_CHARS = 200

#: The capability that reaches Mira's own log trail.
LOGS = "logs"

#: The widest trailing window a log search may ask for. Retention is a week by
#: default; this only stops `hours` being a number nobody meant.
MAX_LOG_HOURS = 24 * 365

_LOG_LEVELS = {
    "DEBUG": logging.DEBUG,
    "INFO": logging.INFO,
    "WARNING": logging.WARNING,
    "ERROR": logging.ERROR,
    "CRITICAL": logging.CRITICAL,
}


class InvalidArguments(ValueError):
    """Arguments that cannot be acted on, named so the client can fix them."""


@dataclass
class Context:
    """What a handler is allowed to know: the grant, and the ceilings."""

    grant: Grant
    max_page_size: int = 50
    #: What this session may reach beyond its grant. Empty for a stdio session.
    capabilities: frozenset[str] = frozenset()
    #: The application database, for the tools that read the log trail.
    app_db: Any = None


@dataclass
class Result:
    """A handler's answer, plus what the audit trail records about it."""

    payload: dict[str, Any]
    count: int = 0
    repository: str = ""


# --------------------------------------------------------------------------
# Argument handling
# --------------------------------------------------------------------------


def _check_names(arguments: dict[str, Any], allowed: tuple[str, ...]) -> None:
    unknown = sorted(set(arguments) - set(allowed))
    if unknown:
        raise InvalidArguments(
            f"Unknown argument(s): {', '.join(unknown)}. "
            f"This tool takes: {', '.join(allowed) or 'no arguments'}."
        )


def _text(arguments: dict[str, Any], name: str, *, required: bool = False) -> str:
    value = arguments.get(name, "")
    if value is None or value == "":
        if required:
            raise InvalidArguments(f"{name} is required.")
        return ""
    if not isinstance(value, str):
        raise InvalidArguments(f"{name} must be a string.")
    if len(value) > MAX_FILTER_CHARS:
        raise InvalidArguments(f"{name} is longer than {MAX_FILTER_CHARS} characters.")
    return value


def _integer(arguments: dict[str, Any], name: str) -> int:
    value = arguments.get(name)
    if value in (None, ""):
        return 0
    if isinstance(value, bool) or not isinstance(value, int):
        raise InvalidArguments(f"{name} must be an integer.")
    if value < 0:
        raise InvalidArguments(f"{name} cannot be negative.")
    return value


def _paging(
    context: Context, arguments: dict[str, Any], query: dict[str, Any]
) -> tuple[int, int, dict[str, Any]]:
    """Resolve `limit` and `cursor` against the query they belong to."""
    size = page_size(arguments.get("limit"), configured=context.max_page_size)
    offset = decode_cursor(query, _text(arguments, "cursor"))
    return size, offset, query


def _page(
    rows: list[dict[str, Any]], *, query: dict[str, Any], size: int, offset: int
) -> tuple[list[dict[str, Any]], str]:
    """Cut the over-read down to a page and say whether another follows."""
    items = rows[:size]
    more = len(rows) > size
    return items, (encode_cursor(query, offset + len(items)) if more else "")


# --------------------------------------------------------------------------
# Handlers
# --------------------------------------------------------------------------


def _repository(context: Context, arguments: dict[str, Any]) -> Repository:
    return context.grant.resolve(_text(arguments, "repository", required=True))


def list_repositories(context: Context, arguments: dict[str, Any]) -> Result:
    _check_names(arguments, ())
    items = [
        {
            "repository": repository.key,
            "platform": repository.platform,
            "owner": repository.owner,
            "repo": repository.repo,
            "indexed": reads.is_indexed(repository),
        }
        for repository in context.grant.repositories
    ]
    return Result(
        payload={
            "items": items,
            "note": (
                "These are the only repositories this server can read. "
                "Anything else is refused, whatever it is called."
            ),
        },
        count=len(items),
    )


def list_findings(context: Context, arguments: dict[str, Any]) -> Result:
    _check_names(
        arguments,
        (
            "repository",
            "pr_number",
            "state",
            "category",
            "severity",
            "path_prefix",
            "limit",
            "cursor",
        ),
    )
    repository = _repository(context, arguments)
    query: dict[str, Any] = {
        "tool": "list_findings",
        "repository": repository.key,
        "pr_number": _integer(arguments, "pr_number"),
        "state": _text(arguments, "state"),
        "category": _text(arguments, "category"),
        "severity": _text(arguments, "severity"),
        "path_prefix": _text(arguments, "path_prefix"),
    }
    size, offset, query = _paging(context, arguments, query)
    rows = reads.list_findings(
        repository,
        pr_number=int(query["pr_number"]),
        state=str(query["state"]),
        category=str(query["category"]),
        severity=str(query["severity"]),
        path_prefix=str(query["path_prefix"]),
        limit=size,
        offset=offset,
    )
    items, cursor = _page(rows, query=query, size=size, offset=offset)
    return Result(
        payload={
            "repository": repository.key,
            "indexed": True,
            "items": items,
            "next_cursor": cursor,
        },
        count=len(items),
        repository=repository.key,
    )


def get_finding(context: Context, arguments: dict[str, Any]) -> Result:
    _check_names(arguments, ("repository", "finding_id"))
    repository = _repository(context, arguments)
    finding_id = _text(arguments, "finding_id", required=True)
    finding = reads.get_finding(repository, finding_id)
    return Result(
        payload={
            "repository": repository.key,
            "indexed": True,
            "finding": finding,
            "found": finding is not None,
        },
        count=1 if finding else 0,
        repository=repository.key,
    )


def list_rules(context: Context, arguments: dict[str, Any]) -> Result:
    _check_names(arguments, ("repository", "limit", "cursor"))
    repository = _repository(context, arguments)
    query: dict[str, Any] = {"tool": "list_rules", "repository": repository.key}
    size, offset, query = _paging(context, arguments, query)
    rows = reads.list_rules(repository, limit=size, offset=offset)
    items, cursor = _page(rows, query=query, size=size, offset=offset)
    return Result(
        payload={
            "repository": repository.key,
            "indexed": True,
            "items": items,
            "next_cursor": cursor,
            "note": "Approved and active rules only. Candidates awaiting a human are not shown.",
        },
        count=len(items),
        repository=repository.key,
    )


def list_evaluations(context: Context, arguments: dict[str, Any]) -> Result:
    _check_names(
        arguments, ("repository", "rule_id", "category", "decision", "outcome", "limit", "cursor")
    )
    repository = _repository(context, arguments)
    query: dict[str, Any] = {
        "tool": "list_evaluations",
        "repository": repository.key,
        "rule_id": _integer(arguments, "rule_id"),
        "category": _text(arguments, "category"),
        "decision": _text(arguments, "decision"),
        "outcome": _text(arguments, "outcome"),
    }
    size, offset, query = _paging(context, arguments, query)
    rows = reads.list_evaluations(
        repository,
        rule_id=int(query["rule_id"]),
        category=str(query["category"]),
        decision=str(query["decision"]),
        outcome=str(query["outcome"]),
        limit=size,
        offset=offset,
    )
    items, cursor = _page(rows, query=query, size=size, offset=offset)
    return Result(
        payload={
            "repository": repository.key,
            "indexed": True,
            "items": items,
            "next_cursor": cursor,
        },
        count=len(items),
        repository=repository.key,
    )


def list_indexed_files(context: Context, arguments: dict[str, Any]) -> Result:
    _check_names(arguments, ("repository", "path_prefix", "limit", "cursor"))
    repository = _repository(context, arguments)
    query: dict[str, Any] = {
        "tool": "list_indexed_files",
        "repository": repository.key,
        "path_prefix": _text(arguments, "path_prefix"),
    }
    size, offset, query = _paging(context, arguments, query)
    rows = reads.list_indexed_files(
        repository, path_prefix=str(query["path_prefix"]), limit=size, offset=offset
    )
    items, cursor = _page(rows, query=query, size=size, offset=offset)
    return Result(
        payload={
            "repository": repository.key,
            "indexed": True,
            "items": items,
            "next_cursor": cursor,
        },
        count=len(items),
        repository=repository.key,
    )


def get_indexed_file(context: Context, arguments: dict[str, Any]) -> Result:
    _check_names(arguments, ("repository", "path"))
    repository = _repository(context, arguments)
    path = _text(arguments, "path", required=True)
    file = reads.get_indexed_file(repository, path)
    return Result(
        payload={
            "repository": repository.key,
            "indexed": True,
            "file": file,
            "found": file is not None,
        },
        count=1 if file else 0,
        repository=repository.key,
    )


def list_reviews(context: Context, arguments: dict[str, Any]) -> Result:
    _check_names(arguments, ("repository", "pr_number", "limit", "cursor"))
    repository = _repository(context, arguments)
    query: dict[str, Any] = {
        "tool": "list_reviews",
        "repository": repository.key,
        "pr_number": _integer(arguments, "pr_number"),
    }
    size, offset, query = _paging(context, arguments, query)
    rows = reads.list_reviews(
        repository, pr_number=int(query["pr_number"]), limit=size, offset=offset
    )
    items, cursor = _page(rows, query=query, size=size, offset=offset)
    return Result(
        payload={
            "repository": repository.key,
            "indexed": True,
            "items": items,
            "next_cursor": cursor,
            "note": (
                "Review passes that finished. A pass that failed leaves no row "
                "here; its trace ID is on the pull request, and its lines are "
                "in the log trail."
            ),
        },
        count=len(items),
        repository=repository.key,
    )


def _level(arguments: dict[str, Any], default: str) -> int:
    name = (_text(arguments, "level") or default).strip().upper()
    if name not in _LOG_LEVELS:
        raise InvalidArguments(f"level must be one of {', '.join(_LOG_LEVELS)}.")
    return _LOG_LEVELS[name]


def _hours(arguments: dict[str, Any], default: float) -> float:
    value = arguments.get("hours")
    if value in (None, ""):
        return default
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise InvalidArguments("hours must be a number.")
    if value < 0 or value > MAX_LOG_HOURS:
        raise InvalidArguments(f"hours must be between 0 and {MAX_LOG_HOURS}.")
    return float(value)


def _log_page(
    context: Context,
    arguments: dict[str, Any],
    query: dict[str, Any],
    *,
    hours: float,
    oldest_first: bool,
) -> tuple[list[dict[str, Any]], str]:
    """One page of log lines, pinned to the moment the first page was read.

    The trail grows while it is being paged. Newest first, every line written
    between two calls would push a row the last page returned onto the next
    one, so the first page fixes an upper bound and the cursor carries it: page
    two reads the trail as it stood when page one was read. The trailing window
    is measured back from that same moment, so it does not slide either.
    """
    if context.app_db is None:
        raise RuntimeError("the application database is not available to this session")
    size = page_size(arguments.get("limit"), configured=context.max_page_size)
    offset, anchor = decode_anchored_cursor(query, _text(arguments, "cursor"))
    until = anchor or time.time()
    rows = reads.list_logs(
        context.app_db,
        min_level=int(query.get("min_level", 0)),
        logger_name=str(query.get("logger", "")),
        query=str(query.get("query", "")),
        trace_id=str(query.get("trace_id", "")),
        repo=str(query.get("repository", "")),
        since=until - hours * 3600 if hours else 0.0,
        until=until,
        oldest_first=oldest_first,
        limit=size,
        offset=offset,
    )
    items = rows[:size]
    more = len(rows) > size
    cursor = encode_cursor(query, offset + len(items), anchor=until) if more else ""
    return items, cursor


def search_logs(context: Context, arguments: dict[str, Any]) -> Result:
    _check_names(
        arguments,
        ("level", "logger", "query", "trace_id", "repository", "hours", "limit", "cursor"),
    )
    hours = _hours(arguments, 24.0)
    query: dict[str, Any] = {
        "tool": "search_logs",
        "min_level": _level(arguments, "INFO"),
        "logger": _text(arguments, "logger").strip(),
        "query": _text(arguments, "query").strip(),
        "trace_id": _text(arguments, "trace_id").strip(),
        # A filter on the trail, not a read of a repository: the lines of a
        # repository since removed from Mira are still worth finding, and the
        # session that reaches this tool is an admin's.
        "repository": _text(arguments, "repository").strip(),
        "hours": hours,
    }
    items, cursor = _log_page(context, arguments, query, hours=hours, oldest_first=False)
    return Result(
        payload={
            "items": items,
            "next_cursor": cursor,
            "capture": reads.log_capture_state(),
            "note": (
                "Newest first. level is a floor, so ERROR includes CRITICAL. "
                "hours=0 searches everything the trail still holds."
            ),
        },
        count=len(items),
    )


def get_trace(context: Context, arguments: dict[str, Any]) -> Result:
    _check_names(arguments, ("trace_id", "limit", "cursor"))
    trace_id = _text(arguments, "trace_id", required=True).strip()
    query: dict[str, Any] = {"tool": "get_trace", "trace_id": trace_id}
    items, cursor = _log_page(context, arguments, query, hours=0.0, oldest_first=True)
    payload: dict[str, Any] = {
        "trace_id": trace_id,
        "items": items,
        "next_cursor": cursor,
        "capture": reads.log_capture_state(),
    }
    if not items and not _text(arguments, "cursor"):
        payload["note"] = (
            "No captured line carries this trace ID. It may be older than the "
            "retention window, or log capture may have been off when it ran."
        )
    return Result(payload=payload, count=len(items))


def _unindexed(repository: Repository) -> Result:
    """The answer for a granted repository Mira has never indexed.

    Not an error: the grant is valid and the question was well formed. There
    is simply nothing stored, and saying so is better than an empty list that
    reads as "this repository has no findings".
    """
    return Result(
        payload={
            "repository": repository.key,
            "indexed": False,
            "items": [],
            "next_cursor": "",
            "note": (
                "Mira has no index for this repository, so it has no findings, "
                "rules, evaluations or file summaries either."
            ),
        },
        repository=repository.key,
    )


# --------------------------------------------------------------------------
# The registry
# --------------------------------------------------------------------------

_REPOSITORY_ARG = {
    "type": "string",
    "description": "Repository to read, as owner/repo or platform:owner/repo. "
    "Must be one that mira_list_repositories returns.",
}
_LIMIT_ARG = {
    "type": "integer",
    "minimum": 1,
    "description": "Rows per page. Capped by the server; asking for more returns the cap.",
}
_CURSOR_ARG = {
    "type": "string",
    "description": "next_cursor from a previous page of this same query. "
    "Cursors are not valid across different filters.",
}


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    schema: dict[str, Any]
    handler: Callable[[Context, dict[str, Any]], Result]
    #: Every field the descriptor advertises is here, so `tools/list` cannot
    #: drift from what a handler actually accepts.
    annotations: dict[str, Any] = field(default_factory=dict)
    #: The capability a session needs to be offered this tool, or "" for none.
    requires: str = ""

    def available_to(self, context: Context) -> bool:
        return not self.requires or self.requires in context.capabilities

    def run(self, context: Context, arguments: dict[str, Any]) -> Result:
        """Run the tool, turning "there is nothing stored" into an answer.

        A repository with no index raises out of `open_index` rather than being
        checked for first, because a check followed by an open is a window in
        which the answer can change. Handled here, once, so every tool reports
        the absence the same way.
        """
        try:
            return self.handler(context, arguments)
        except reads.NotIndexed as exc:
            return _unindexed(exc.repository)

    def descriptor(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "inputSchema": self.schema,
            "annotations": {
                # Advertised so a client can reason about the server without
                # calling it. `readOnlyHint` is the whole feature in one flag.
                "readOnlyHint": True,
                "destructiveHint": False,
                "idempotentHint": True,
                "openWorldHint": False,
                **self.annotations,
            },
        }


def _object(properties: dict[str, Any], required: tuple[str, ...] = ()) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": properties,
        "required": list(required),
        # Mirrors `_check_names`. Clients that validate locally then reject the
        # same calls the server would, instead of learning about it in a round
        # trip.
        "additionalProperties": False,
    }


TOOLS: tuple[Tool, ...] = (
    Tool(
        name="mira_list_repositories",
        description=(
            "List the repositories this Mira MCP server is allowed to read. "
            "Start here: every other tool takes one of these names."
        ),
        schema=_object({}),
        handler=list_repositories,
    ),
    Tool(
        name="mira_list_findings",
        description=(
            "List review findings Mira recorded for a repository, newest first. "
            "A finding is one issue raised on one pull request, with the "
            "severity and confidence Mira gave it and whether it is still open."
        ),
        schema=_object(
            {
                "repository": _REPOSITORY_ARG,
                "pr_number": {"type": "integer", "minimum": 1, "description": "One pull request."},
                "state": {"type": "string", "description": "Finding state, e.g. open or resolved."},
                "category": {"type": "string", "description": "Finding category, e.g. bug."},
                "severity": {
                    "type": "string",
                    "description": "blocker, warning, suggestion or nitpick.",
                },
                "path_prefix": {"type": "string", "description": "Only findings under this path."},
                "limit": _LIMIT_ARG,
                "cursor": _CURSOR_ARG,
            },
            required=("repository",),
        ),
        handler=list_findings,
    ),
    Tool(
        name="mira_get_finding",
        description=(
            "One finding in full, with the feedback recorded against it - "
            "whether a human agreed, disagreed, or addressed it."
        ),
        schema=_object(
            {
                "repository": _REPOSITORY_ARG,
                "finding_id": {"type": "string", "description": "Finding id from a listing."},
            },
            required=("repository", "finding_id"),
        ),
        handler=get_finding,
    ),
    Tool(
        name="mira_list_rules",
        description=(
            "The approved, active rules Mira has learned for a repository, with "
            "the rationale and the evidence count behind each. Proposals nobody "
            "has approved are not included."
        ),
        schema=_object(
            {"repository": _REPOSITORY_ARG, "limit": _LIMIT_ARG, "cursor": _CURSOR_ARG},
            required=("repository",),
        ),
        handler=list_rules,
    ),
    Tool(
        name="mira_list_evaluations",
        description=(
            "How a rule performed: one row per recorded exposure, with the "
            "outcome and the feedback signals behind it. This is the evidence "
            "for keeping, downgrading or retiring a rule."
        ),
        schema=_object(
            {
                "repository": _REPOSITORY_ARG,
                "rule_id": {"type": "integer", "minimum": 1, "description": "One rule."},
                "category": {"type": "string", "description": "Rule category."},
                "decision": {
                    "type": "string",
                    "description": "What the rule did in that review, e.g. instruction.",
                },
                "outcome": {
                    "type": "string",
                    "description": "Filter to one outcome, e.g. positive or negative.",
                },
                "limit": _LIMIT_ARG,
                "cursor": _CURSOR_ARG,
            },
            required=("repository",),
        ),
        handler=list_evaluations,
    ),
    Tool(
        name="mira_list_indexed_files",
        description=(
            "The files Mira has indexed for a repository, with the summary it "
            "holds for each. Summaries of the code, not the code itself."
        ),
        schema=_object(
            {
                "repository": _REPOSITORY_ARG,
                "path_prefix": {"type": "string", "description": "Only paths under this prefix."},
                "limit": _LIMIT_ARG,
                "cursor": _CURSOR_ARG,
            },
            required=("repository",),
        ),
        handler=list_indexed_files,
    ),
    Tool(
        name="mira_get_indexed_file",
        description=(
            "The indexed context for one file: its summary, the symbols in it, "
            "what it imports, and which files depend on it."
        ),
        schema=_object(
            {
                "repository": _REPOSITORY_ARG,
                "path": {"type": "string", "description": "Repository-relative path."},
            },
            required=("repository", "path"),
        ),
        handler=get_indexed_file,
    ),
    Tool(
        name="mira_list_reviews",
        description=(
            "The review passes Mira ran on a repository, newest first: which "
            "pull request, how many files and lines, what it posted by "
            "severity, and the tokens and time it took."
        ),
        schema=_object(
            {
                "repository": _REPOSITORY_ARG,
                "pr_number": {"type": "integer", "minimum": 1, "description": "One pull request."},
                "limit": _LIMIT_ARG,
                "cursor": _CURSOR_ARG,
            },
            required=("repository",),
        ),
        handler=list_reviews,
    ),
    Tool(
        name="mira_search_logs",
        description=(
            "Search Mira's own captured log trail, newest first: errors, "
            "warnings, model retries and fallbacks, provider failures. Filter "
            "by level floor, logger, text (matched in messages and "
            "tracebacks), trace ID, repository and a trailing window in hours."
        ),
        schema=_object(
            {
                "level": {
                    "type": "string",
                    "enum": list(_LOG_LEVELS),
                    "description": "Lowest level to include. Default INFO.",
                },
                "logger": {
                    "type": "string",
                    "description": "Logger name, matched as a substring, e.g. mira.llm.",
                },
                "query": {
                    "type": "string",
                    "description": "Text to find in the message or the traceback.",
                },
                "trace_id": {"type": "string", "description": "One review's trace ID."},
                "repository": {
                    "type": "string",
                    "description": "Only lines logged for this owner/repo.",
                },
                "hours": {
                    "type": "number",
                    "minimum": 0,
                    "description": "Trailing window. Default 24; 0 for everything kept.",
                },
                "limit": _LIMIT_ARG,
                "cursor": _CURSOR_ARG,
            }
        ),
        handler=search_logs,
        requires=LOGS,
    ),
    Tool(
        name="mira_get_trace",
        description=(
            "Every log line one review emitted, oldest first, so it reads as "
            "the story of what happened. A failed review prints its trace ID "
            "in the failure notice on the pull request."
        ),
        schema=_object(
            {
                "trace_id": {"type": "string", "description": "The trace ID to follow."},
                "limit": _LIMIT_ARG,
                "cursor": _CURSOR_ARG,
            },
            required=("trace_id",),
        ),
        handler=get_trace,
        requires=LOGS,
    ),
)

BY_NAME: dict[str, Tool] = {tool.name: tool for tool in TOOLS}


def available(context: Context) -> tuple[Tool, ...]:
    """The tools a session with this context is offered."""
    return tuple(tool for tool in TOOLS if tool.available_to(context))


def descriptors(context: Context | None = None) -> list[dict[str, Any]]:
    """Descriptors for a session's tools, or for every tool when none is given."""
    offered = TOOLS if context is None else available(context)
    return [tool.descriptor() for tool in offered]
