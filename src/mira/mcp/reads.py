"""The reads themselves: four kinds of stored data, and nothing else.

Every function here takes a repository the grant already resolved and returns
plain dictionaries. Two rules shape all of them.

**The projection is an allowlist, never a dump.** No `asdict`, no `SELECT *`.
A field added to `ReviewFinding` or to a rule row six months from now does not
start flowing out of this surface because somebody added it somewhere else;
exposing it is a decision made here, once, in the open. That is also where a
field is *withheld* - the evaluation rows carry the pull request's author, and
who wrote a pull request is not what a question about rule quality is asking.

**A repository with no index is not an error.** Findings, rules, evaluations
and file summaries all live in one per-repository store, so a repository that
has never been indexed has no data of any kind. Opening the store would create
it, which is a write, on a surface whose whole claim is that it does not write.
So the store is opened only when it already exists, and otherwise the answer
is an honest empty one that says the repository has not been indexed.
"""

from __future__ import annotations

import contextlib
import os
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from mira.index.store import IndexStore
from mira.mcp.authz import Repository


def is_indexed(repository: Repository) -> bool:
    """Whether this repository has a store to read, without making one.

    On Postgres every repository shares one set of tables, so there is nothing
    to create and nothing to check: the answer is yes, and an unindexed
    repository simply has no rows.
    """
    url = os.environ.get("DATABASE_URL", "")
    if url.startswith("postgresql://") or url.startswith("postgres://"):
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
    """
    store = IndexStore.open(repository.owner, repository.repo, platform=repository.platform)
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


def get_indexed_file(repository: Repository, path: str) -> dict[str, Any] | None:
    """One file's indexed context: its summary, symbols and neighbours."""
    with open_index(repository) as store:
        summary = store.get_summary(path)
        if summary is None:
            return None
        dependents = store.get_dependents(path)
    return {
        "path": summary.path,
        "language": summary.language,
        "summary": summary.summary,
        "loc": summary.loc,
        "updated_at": summary.updated_at,
        "symbols": [
            {
                "name": symbol.name,
                "kind": symbol.kind,
                "signature": symbol.signature,
                "description": symbol.description,
            }
            for symbol in summary.symbols
        ],
        "imports": list(summary.imports),
        "dependents": list(dependents),
    }
