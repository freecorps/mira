"""Shared setup for the MCP tests: a repository with something in it.

Not a test module. The MCP surface reads four kinds of stored data out of one
per-repository store, so almost every test needs a store that already has some,
and building one by hand in each file would make the tests about the fixture.
"""

from __future__ import annotations

import time
from typing import Any

from mira.feedback.evaluation import RuleEvaluation
from mira.feedback.models import ReviewFinding
from mira.index.store import FileSummary, IndexStore, SymbolInfo
from mira.mcp.audit import AuditLog
from mira.mcp.authz import Grant
from mira.mcp.server import MiraMcpServer


def finding(
    *,
    owner: str = "acme",
    repo: str = "widgets",
    finding_id: str = "f-1",
    pr_number: int = 7,
    path: str = "src/app.py",
    category: str = "bug",
    severity: str = "warning",
    title: str = "Incorrect fallback",
    body: str = "This branch returns the wrong value.",
    state: str = "open",
    created_at: float = 1_700_000_000.0,
) -> ReviewFinding:
    return ReviewFinding(
        id=finding_id,
        fingerprint=f"fp-{finding_id}",
        review_id=0,
        platform="github",
        owner=owner,
        repo=repo,
        pr_number=pr_number,
        pr_url=f"https://github.com/{owner}/{repo}/pull/{pr_number}",
        base_sha="base123",
        head_sha="head123",
        path=path,
        start_line=10,
        end_line=12,
        symbol="run",
        category=category,
        severity=severity,
        confidence=0.92,
        title=title,
        body=body,
        suggestion="return expected",
        detector="main",
        prompt_model="test-model",
        state=state,
        created_at=created_at,
        updated_at=created_at,
    )


def file_summary(
    path: str = "src/app.py",
    summary: str = "Entry point.",
    imports: list[str] | None = None,
) -> FileSummary:
    return FileSummary(
        path=path,
        language="python",
        summary=summary,
        symbols=[SymbolInfo(name="run", kind="function", signature="run()", description="Runs.")],
        imports=["src/util.py"] if imports is None else imports,
        content_hash="hash",
        loc=42,
        updated_at=1_700_000_000.0,
    )


def populate(
    owner: str = "acme",
    repo: str = "widgets",
    *,
    platform: str = "github",
    findings: list[ReviewFinding] | None = None,
    files: list[FileSummary] | None = None,
    rules: list[dict[str, Any]] | None = None,
    evaluations: list[RuleEvaluation] | None = None,
) -> None:
    """Write a repository's stored knowledge, then let go of the store."""
    store = IndexStore.open(owner, repo, platform=platform)
    try:
        for item in findings or []:
            store.save_review_finding(item)
        for item in files or []:
            store.upsert_summary(item)
        for rule in rules or []:
            row = store.upsert_learned_rule(
                rule.get("rule_text", "Prefer explicit returns."),
                rule.get("source_signal", "feedback"),
                rule.get("category", "bug"),
                rule.get("path_pattern", ""),
                rule.get("sample_count", 3),
                status=rule.get("status", "approved"),
            )
            if rule.get("status", "approved") != "approved":
                continue
            store._conn.execute(  # noqa: SLF001 - the governed path needs a candidate
                "UPDATE learned_rules SET status='approved', active=1 WHERE id=?", (row.id,)
            )
            store._conn.commit()  # noqa: SLF001
        if evaluations:
            store.record_rule_evaluations(evaluations)
    finally:
        store.close()


def evaluation(
    *,
    owner: str = "acme",
    repo: str = "widgets",
    rule_id: int = 1,
    key: str = "e-1",
    decision: str = "instruction",
    finding_id: str | None = None,
    pr_author: str = "someone",
) -> RuleEvaluation:
    return RuleEvaluation(
        evaluation_key=key,
        review_id=0,
        rule_id=rule_id,
        rule_version=1,
        rule_origin="learned",
        scope_type="repo",
        scope_value=f"{owner}/{repo}",
        category="bug",
        decision=decision,
        finding_id=finding_id,
        platform="github",
        owner=owner,
        repo=repo,
        pr_number=7,
        pr_author=pr_author,
        head_sha="head123",
        created_at=time.time(),
    )


class SilentAudit(AuditLog):
    """An audit log that keeps its rows in memory.

    The real one opens Mira's application database, which is a second store to
    isolate in every test that does not care. Tests that *do* care read
    `entries` instead of the database.
    """

    def __init__(self, enabled: bool = True) -> None:
        super().__init__(enabled=enabled, db=None)
        self._db_attempted = True  # never reach for the application database
        self.entries: list[dict[str, Any]] = []

    def record(self, **kwargs: Any) -> None:
        if self.enabled:
            self.entries.append(kwargs)


def server(*repositories: str, config: Any = None, audit: AuditLog | None = None) -> MiraMcpServer:
    from mira.config import McpConfig

    return MiraMcpServer(
        grant=Grant.from_specs(repositories),
        config=config or McpConfig(enabled=True, repositories=list(repositories)),
        audit=audit if audit is not None else SilentAudit(),
    )


def call(session: MiraMcpServer, tool: str, **arguments: Any) -> dict[str, Any]:
    return session.call_tool({"name": tool, "arguments": arguments})


def text_of(response: dict[str, Any]) -> str:
    return response["content"][0]["text"]


def payload_of(response: dict[str, Any]) -> dict[str, Any]:
    """The JSON a successful response carries, unwrapped from its block."""
    import json

    body = text_of(response)
    start = body.index("\n{")
    end = body.rindex("}") + 1
    return json.loads(body[start:end])
