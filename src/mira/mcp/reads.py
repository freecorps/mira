"""The reads themselves: four kinds of stored data, and nothing else.

Every function here takes a repository the grant already resolved and returns
plain dictionaries. Two rules shape all of them.

**The projection is an allowlist, never a dump.** No `asdict`, no `SELECT *`.
A field added to `ReviewFinding` or to a rule row six months from now does not
start flowing out of this surface because somebody added it somewhere else;
exposing it is a decision made here, once, in the open. That is also where a
field is *withheld* - the evaluation rows carry the pull request's author, and
who wrote a pull request is not what a question about rule quality is asking.

**A repository with no index is not an error, and reading never makes one.**
Findings, rules, evaluations and file summaries all live in one per-repository
store, so a repository that has never been indexed has no data of any kind.
Creating that store to discover it is empty would be a write, on a surface
whose whole claim is that it does not write - so the SQLite store is opened in
a mode that raises rather than creating, and the absence comes back as an
honest empty answer.

Which is also why this does not go through `IndexStore.open`. That helper falls
back to SQLite when Postgres is unreachable, which is right for a server that
should keep working and wrong here twice over: a transient outage would turn a
read into a write, and would answer from whatever stale local file the fallback
found. A backend that is configured and unavailable is an error, not a quieter
source of data.
"""

from __future__ import annotations

import contextlib
import os
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from mira.index.store import IndexStore
from mira.mcp.authz import Repository

#: Ceiling on the neighbour lists a single indexed file comes back with. A
#: generated module can import hundreds of things and be depended on by
#: hundreds more, and `get_indexed_file` has no page to shrink if it does not
#: bound them here.
MAX_RELATED_ITEMS = 200


class NotIndexed(Exception):
    """This repository has no store to read. Not an error the client caused."""

    def __init__(self, repository: Repository) -> None:
        super().__init__(f"{repository.key} has not been indexed")
        self.repository = repository


def _postgres_url() -> str:
    url = os.environ.get("DATABASE_URL", "")
    return url if url.startswith(("postgresql://", "postgres://")) else ""


def is_indexed(repository: Repository) -> bool:
    """Whether this repository has a store to read, without making one.

    On Postgres every repository shares one set of tables, so there is nothing
    per-repository to create and nothing to check: the answer is yes, and an
    unindexed repository simply has no rows. A Postgres that is configured and
    down does not make this false - it makes the read fail, which `open_index`
    is where that happens.
    """
    if _postgres_url():
        return True
    return os.path.isfile(
        IndexStore.db_path_for(repository.owner, repository.repo, repository.platform)
    )


@contextmanager
def open_index(repository: Repository) -> Iterator[Any]:
    """Open this repository's index store for the length of one call.

    One connection per call rather than one per session: an MCP client can hold
    a server open for a working day, and a long-lived handle on a SQLite file
    is a lock somebody else's indexing run has to wait behind.

    Deliberately not `IndexStore.open`: this must never create a store and must
    never silently answer from a different backend than the one configured.
    Postgres is opened directly, so an outage raises here instead of falling
    back; SQLite is opened in read-write-no-create mode, so a missing index
    raises rather than being brought into existence. Neither is a check
    followed by an open, so there is no window between the two.
    """
    url = _postgres_url()
    if url:
        from mira.index.pg_store import PgIndexStore

        key_owner = (
            repository.owner
            if repository.platform == "github"
            else f"_{repository.platform}/{repository.owner}"
        )
        store: Any = PgIndexStore(key_owner, repository.repo, url)
    else:
        path = IndexStore.db_path_for(repository.owner, repository.repo, repository.platform)
        try:
            store = IndexStore(
                path,
                owner=repository.owner,
                repo=repository.repo,
                platform=repository.platform,
                create=False,
            )
        except sqlite3.OperationalError as exc:
            raise NotIndexed(repository) from exc
    try:
        yield store
    finally:
        with contextlib.suppress(Exception):  # closing a read handle cannot fail a read
            store.close()


def _finding_dict(finding: Any) -> dict[str, Any]:
    return {
        "id": finding.id,
        "pr_number": finding.pr_number,
        "pr_url": finding.pr_url,
        "path": finding.path,
        "start_line": finding.start_line,
        "end_line": finding.end_line,
        "symbol": finding.symbol,
        "category": finding.category,
        "severity": finding.severity,
        "confidence": finding.confidence,
        "title": finding.title,
        "body": finding.body,
        "suggestion": finding.suggestion,
        "state": finding.state,
        "detector": finding.detector,
        "head_sha": finding.head_sha,
        "created_at": finding.created_at,
        "updated_at": finding.updated_at,
    }


def list_findings(
    repository: Repository,
    *,
    pr_number: int = 0,
    state: str = "",
    category: str = "",
    severity: str = "",
    path_prefix: str = "",
    limit: int,
    offset: int,
) -> list[dict[str, Any]]:
    """Findings for one repository, newest first. Reads one row past the page.

    `include_closed` is on because a resolved finding is part of what Mira
    recorded about this repository, and a caller asking "what has been found
    here" is asking about the history rather than about a work queue. The
    autofix caller of this same query wants the opposite, which is why the
    default stays where it was.
    """
    with open_index(repository) as store:
        rows = store.list_review_findings(
            pr_number=pr_number,
            include_closed=True,
            state=state,
            category=category,
            severity=severity,
            path_prefix=path_prefix,
            limit=limit + 1,
            offset=offset,
        )
    return [_finding_dict(row) for row in rows]


def get_finding(repository: Repository, finding_id: str) -> dict[str, Any] | None:
    """One finding, with the feedback recorded against it.

    The feedback is what makes a finding worth reading after the fact: whether
    a human agreed with it is the difference between a comment and a verdict.
    Actor logins are dropped - the question a rule or a review asks is whether
    the finding held up, not who said so.
    """
    with open_index(repository) as store:
        finding = store.get_review_finding(finding_id)
        if finding is None:
            return None
        events = store.list_feedback_v2(finding_id=finding_id, limit=50)
        payload = _finding_dict(finding)
        payload["feedback"] = [
            {
                "kind": event.kind,
                "actor_role": event.actor_role,
                "rationale": event.rationale,
                "thread_state": event.thread_state,
                "provenance_complete": event.provenance_complete,
                "created_at": event.created_at,
            }
            for event in events
        ]
    return payload


def list_rules(repository: Repository, *, limit: int, offset: int) -> list[dict[str, Any]]:
    """The approved, active learned rules for a repository.

    Only approved and active ones. A synthesised candidate is a proposal that
    no human has agreed to, and a rejected rule is one a human refused; neither
    describes how Mira reviews this repository, which is the question this tool
    answers.

    Install-wide global rules are deliberately absent. They belong to the
    deployment rather than to any repository, so returning them through a
    repository-scoped tool would hand a grant for one repository a view of
    configuration that applies to every other.

    Paged in memory: approved rules are governed one at a time by a human and
    number in the tens, so the page is cut after the read rather than in SQL.
    """
    with open_index(repository) as store:
        rules = store.list_active_learned_rules()
    window = rules[offset : offset + limit + 1]
    return [
        {
            "id": rule.id,
            "rule_text": rule.rule_text,
            "rationale": rule.rationale,
            "category": rule.category,
            "path_pattern": rule.path_pattern,
            "scope_type": rule.scope_type,
            "scope_value": rule.scope_value,
            "status": rule.status,
            "active": bool(rule.active),
            "version": rule.version,
            "evidence_count": rule.evidence_count,
            "sample_count": rule.sample_count,
            "source_signal": rule.source_signal,
            "effective_from": rule.effective_from,
            "created_at": rule.created_at,
            "updated_at": rule.updated_at,
        }
        for rule in window
    ]


def list_evaluations(
    repository: Repository,
    *,
    rule_id: int = 0,
    category: str = "",
    decision: str = "",
    outcome: str = "",
    limit: int,
    offset: int,
) -> list[dict[str, Any]]:
    """How a rule actually did: one row per recorded exposure.

    `pr_author` is in the underlying row and is not returned. A rule's evidence
    is the outcome, not the person whose pull request it landed on, and a
    read-only surface is a bad place to start building a picture of who gets
    corrected most often.
    """
    filters: dict[str, Any] = {
        "platform": repository.platform,
        "owner": repository.owner,
        "repo": repository.repo,
    }
    if rule_id:
        filters["rule_id"] = int(rule_id)
    if category:
        filters["category"] = category
    if decision:
        filters["decision"] = decision
    with open_index(repository) as store:
        rows = store.list_rule_evaluations(filters, limit=limit + 1, offset=offset, outcome=outcome)
    return [
        {
            "id": row.get("id"),
            "rule_id": row.get("rule_id"),
            "rule_version": row.get("rule_version"),
            "rule_origin": row.get("rule_origin"),
            "scope_type": row.get("scope_type"),
            "category": row.get("category"),
            "decision": row.get("decision"),
            "outcome": row.get("outcome"),
            "addressed": bool(row.get("addressed")),
            "finding_id": row.get("finding_id"),
            "finding_title": row.get("finding_title"),
            "finding_path": row.get("finding_path"),
            "finding_severity": row.get("finding_severity"),
            "finding_state": row.get("finding_state"),
            "pr_number": row.get("pr_number"),
            "thumbs_up": row.get("thumbs_up"),
            "thumbs_down": row.get("thumbs_down"),
            "reply_agree": row.get("reply_agree"),
            "reply_disagree": row.get("reply_disagree"),
            "created_at": row.get("created_at"),
        }
        for row in rows
    ]


def list_indexed_files(
    repository: Repository, *, path_prefix: str = "", limit: int, offset: int
) -> list[dict[str, Any]]:
    """The indexed files of a repository, by path.

    Summaries, not source. What Mira stores about a file is a description of
    it; the file itself is in the repository the caller already has.
    """
    with open_index(repository) as store:
        rows = store.list_indexed_files(path_prefix=path_prefix, limit=limit + 1, offset=offset)
    return [
        {
            "path": row.path,
            "language": row.language,
            "summary": row.summary,
            "loc": row.loc,
            "updated_at": row.updated_at,
        }
        for row in rows
    ]


def _bounded(items: list[Any], name: str, omitted: dict[str, int]) -> list[Any]:
    """Cut a neighbour list to the ceiling, recording what was left out.

    This tool returns one file and so has no page for the response ceiling to
    shrink; without a bound here, a generated module with thousands of symbols
    would be an unbounded payload that only per-field truncation could attack,
    and shortening every name in a list of ten thousand does not make the list
    shorter.
    """
    if len(items) <= MAX_RELATED_ITEMS:
        return items
    omitted[name] = len(items) - MAX_RELATED_ITEMS
    return items[:MAX_RELATED_ITEMS]


def get_indexed_file(repository: Repository, path: str) -> dict[str, Any] | None:
    """One file's indexed context: its summary, symbols and neighbours."""
    with open_index(repository) as store:
        summary = store.get_summary(path)
        if summary is None:
            return None
        dependents = store.get_dependents(path)
    omitted: dict[str, int] = {}
    return {
        "path": summary.path,
        "language": summary.language,
        "summary": summary.summary,
        "loc": summary.loc,
        "updated_at": summary.updated_at,
        "symbols": _bounded(
            [
                {
                    "name": symbol.name,
                    "kind": symbol.kind,
                    "signature": symbol.signature,
                    "description": symbol.description,
                }
                for symbol in summary.symbols
            ],
            "symbols",
            omitted,
        ),
        "imports": _bounded(list(summary.imports), "imports", omitted),
        "dependents": _bounded(list(dependents), "dependents", omitted),
        # Present only when something was cut, and naming how much: a list that
        # silently stops is a file that reads as having fewer neighbours.
        "omitted": omitted,
    }
