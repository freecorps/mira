"""The tools this server offers, which is a list that only shrinks.

Seven reads. There is no tool here that writes, approves, dismisses, triggers
a review, applies a fix, or runs a command, and that is a property of the
registry rather than of the current implementations: a tool is a name, a
schema, and a function in this module, so adding a side effect means adding it
here, in a file whose whole subject is that there are none.

Arguments are validated strictly, including rejecting names this server does
not know. The tempting alternative - ignore what you do not recognise - turns
a client's typo into a silent widening: `sevrity="blocker"` would quietly
return every finding of every severity, and the caller would read the result
as if it had been filtered.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from mira.mcp import reads
from mira.mcp.authz import Grant, Repository
from mira.mcp.limits import decode_cursor, encode_cursor, page_size

#: The longest a string filter may be. Filters are bound parameters, so this
#: is about not doing pointless work rather than about injection.
MAX_FILTER_CHARS = 200


class InvalidArguments(ValueError):
    """Arguments that cannot be acted on, named so the client can fix them."""


@dataclass
class Context:
    """What a handler is allowed to know: the grant, and the ceilings."""

    grant: Grant
    max_page_size: int = 50


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
    if not reads.is_indexed(repository):
        return _unindexed(repository)
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
    if not reads.is_indexed(repository):
        return _unindexed(repository)
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
    if not reads.is_indexed(repository):
        return _unindexed(repository)
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
    if not reads.is_indexed(repository):
        return _unindexed(repository)
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
    if not reads.is_indexed(repository):
        return _unindexed(repository)
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
    if not reads.is_indexed(repository):
        return _unindexed(repository)
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
)

BY_NAME: dict[str, Tool] = {tool.name: tool for tool in TOOLS}


def descriptors() -> list[dict[str, Any]]:
    return [tool.descriptor() for tool in TOOLS]
