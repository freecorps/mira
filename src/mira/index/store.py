"""SQLite-backed storage for file summaries. One DB per repo."""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import time
from dataclasses import dataclass, field

from mira.feedback.evaluation import RuleEvaluation
from mira.feedback.models import FeedbackEventV2, LearningCandidate, ReviewFinding
from mira.feedback.provenance import finding_fingerprint, legacy_finding_id
from mira.gate.persistence import GateStoreMixin
from mira.index._store_shared import _StoreSharedMixin
from mira.models import PRFingerprint

logger = logging.getLogger(__name__)

_INDEX_DIR = os.environ.get("MIRA_INDEX_DIR", "/data/indexes")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS files (
    path TEXT PRIMARY KEY,
    language TEXT NOT NULL DEFAULT '',
    summary TEXT NOT NULL DEFAULT '',
    content_hash TEXT NOT NULL DEFAULT '',
    loc INTEGER NOT NULL DEFAULT 0,
    updated_at REAL NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS symbols (
    file_path TEXT NOT NULL,
    name TEXT NOT NULL,
    kind TEXT NOT NULL DEFAULT 'function',
    signature TEXT NOT NULL DEFAULT '',
    description TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (file_path, name),
    FOREIGN KEY (file_path) REFERENCES files(path) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS imports (
    source_path TEXT NOT NULL,
    target_path TEXT NOT NULL,
    PRIMARY KEY (source_path, target_path),
    FOREIGN KEY (source_path) REFERENCES files(path) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS symbol_refs (
    source_path TEXT NOT NULL,
    source_symbol TEXT NOT NULL,
    target_path TEXT NOT NULL,
    target_symbol TEXT NOT NULL,
    PRIMARY KEY (source_path, source_symbol, target_path, target_symbol),
    FOREIGN KEY (source_path) REFERENCES files(path) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS directories (
    path TEXT PRIMARY KEY,
    summary TEXT NOT NULL DEFAULT '',
    file_count INTEGER NOT NULL DEFAULT 0,
    updated_at REAL NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS external_refs (
    file_path TEXT NOT NULL,
    kind TEXT NOT NULL,
    target TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (file_path, kind, target),
    FOREIGN KEY (file_path) REFERENCES files(path) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS review_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    pr_number INTEGER NOT NULL DEFAULT 0,
    pr_title TEXT NOT NULL DEFAULT '',
    pr_url TEXT NOT NULL DEFAULT '',
    -- GitHub login of the PR author, so the blockers/warnings a person's PRs
    -- trigger can be attributed to them in contributor analytics.
    author TEXT NOT NULL DEFAULT '',
    comments_posted INTEGER NOT NULL DEFAULT 0,
    blockers INTEGER NOT NULL DEFAULT 0,
    warnings INTEGER NOT NULL DEFAULT 0,
    suggestions INTEGER NOT NULL DEFAULT 0,
    files_reviewed INTEGER NOT NULL DEFAULT 0,
    lines_changed INTEGER NOT NULL DEFAULT 0,
    tokens_used INTEGER NOT NULL DEFAULT 0,
    duration_ms INTEGER NOT NULL DEFAULT 0,
    categories TEXT NOT NULL DEFAULT '',
    author_avatar_url TEXT NOT NULL DEFAULT '',
    reviewed_paths TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL DEFAULT 0
);

-- Individual review comments Mira posted (one row per comment, per pass).
CREATE TABLE IF NOT EXISTS review_comments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    review_id INTEGER NOT NULL DEFAULT 0,
    pr_number INTEGER NOT NULL DEFAULT 0,
    pr_url TEXT NOT NULL DEFAULT '',
    path TEXT NOT NULL DEFAULT '',
    line INTEGER NOT NULL DEFAULT 0,
    severity TEXT NOT NULL DEFAULT '',
    category TEXT NOT NULL DEFAULT '',
    title TEXT NOT NULL DEFAULT '',
    body TEXT NOT NULL DEFAULT '',
    github_comment_id INTEGER NOT NULL DEFAULT 0,
    created_at REAL NOT NULL DEFAULT 0
);

-- Human replies on a PR (for the conversation timeline).
CREATE TABLE IF NOT EXISTS pr_replies (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    pr_number INTEGER NOT NULL DEFAULT 0,
    pr_url TEXT NOT NULL DEFAULT '',
    author TEXT NOT NULL DEFAULT '',
    author_avatar_url TEXT NOT NULL DEFAULT '',
    body TEXT NOT NULL DEFAULT '',
    comment_path TEXT NOT NULL DEFAULT '',
    comment_line INTEGER NOT NULL DEFAULT 0,
    github_comment_id INTEGER NOT NULL DEFAULT 0,
    in_reply_to_id INTEGER NOT NULL DEFAULT 0,
    created_at REAL NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS pr_fingerprints (
    pr_number INTEGER PRIMARY KEY,
    head_sha TEXT NOT NULL DEFAULT '',
    title TEXT NOT NULL DEFAULT '',
    body TEXT NOT NULL DEFAULT '',
    paths TEXT NOT NULL DEFAULT '[]',
    symbols TEXT NOT NULL DEFAULT '[]',
    updated_at REAL NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS review_context (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    content TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL DEFAULT 0,
    updated_at REAL NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS feedback_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    pr_number INTEGER NOT NULL DEFAULT 0,
    pr_url TEXT NOT NULL DEFAULT '',
    comment_path TEXT NOT NULL DEFAULT '',
    comment_line INTEGER NOT NULL DEFAULT 0,
    comment_category TEXT NOT NULL DEFAULT '',
    comment_severity TEXT NOT NULL DEFAULT '',
    comment_title TEXT NOT NULL DEFAULT '',
    signal TEXT NOT NULL DEFAULT '',
    actor TEXT NOT NULL DEFAULT '',
    -- GitHub login of the PR author (distinct from `actor`, who is the human
    -- that accepted/rejected). Lets accept/reject rate attribute to the author.
    pr_author TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL DEFAULT 0
);

-- Phase 1 feedback model. Findings are written before their platform comment,
-- then enriched with the remote comment/thread IDs after the API responds.
CREATE TABLE IF NOT EXISTS review_findings (
    id TEXT PRIMARY KEY,
    fingerprint TEXT NOT NULL,
    review_id INTEGER NOT NULL DEFAULT 0,
    platform TEXT NOT NULL DEFAULT 'github',
    owner TEXT NOT NULL DEFAULT '',
    repo TEXT NOT NULL DEFAULT '',
    pr_number INTEGER NOT NULL DEFAULT 0,
    pr_url TEXT NOT NULL DEFAULT '',
    base_sha TEXT NOT NULL DEFAULT '',
    head_sha TEXT NOT NULL DEFAULT '',
    path TEXT NOT NULL DEFAULT '',
    start_line INTEGER NOT NULL DEFAULT 0,
    end_line INTEGER NOT NULL DEFAULT 0,
    symbol TEXT NOT NULL DEFAULT '',
    category TEXT NOT NULL DEFAULT '',
    severity TEXT NOT NULL DEFAULT '',
    confidence REAL NOT NULL DEFAULT 0,
    title TEXT NOT NULL DEFAULT '',
    body TEXT NOT NULL DEFAULT '',
    suggestion TEXT NOT NULL DEFAULT '',
    detector TEXT NOT NULL DEFAULT '',
    prompt_model TEXT NOT NULL DEFAULT '',
    platform_comment_id TEXT NOT NULL DEFAULT '',
    platform_thread_id TEXT NOT NULL DEFAULT '',
    state TEXT NOT NULL DEFAULT 'open',
    created_at REAL NOT NULL DEFAULT 0,
    updated_at REAL NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_review_findings_comment
    ON review_findings(platform, platform_comment_id);
CREATE INDEX IF NOT EXISTS idx_review_findings_pr
    ON review_findings(pr_number, path, start_line);

CREATE TABLE IF NOT EXISTS feedback_events_v2 (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    finding_id TEXT,
    kind TEXT NOT NULL,
    actor TEXT NOT NULL DEFAULT '',
    actor_role TEXT NOT NULL DEFAULT '',
    raw_text TEXT NOT NULL DEFAULT '',
    rationale TEXT NOT NULL DEFAULT '',
    platform TEXT NOT NULL,
    source_event_id TEXT NOT NULL,
    head_sha TEXT NOT NULL DEFAULT '',
    thread_state TEXT NOT NULL DEFAULT '',
    provenance_complete INTEGER NOT NULL DEFAULT 0,
    audit_json TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL DEFAULT 0,
    FOREIGN KEY (finding_id) REFERENCES review_findings(id) ON DELETE SET NULL,
    UNIQUE(platform, source_event_id)
);

CREATE INDEX IF NOT EXISTS idx_feedback_v2_finding
    ON feedback_events_v2(finding_id, created_at);

CREATE TABLE IF NOT EXISTS learning_candidates (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    semantic_fingerprint TEXT NOT NULL,
    rule_text TEXT NOT NULL DEFAULT '',
    rationale TEXT NOT NULL DEFAULT '',
    scope_type TEXT NOT NULL DEFAULT 'repo',
    scope_value TEXT NOT NULL DEFAULT '',
    category TEXT NOT NULL DEFAULT '',
    language TEXT NOT NULL DEFAULT '',
    confidence REAL NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'pending',
    synthesizer_version TEXT NOT NULL DEFAULT 'phase2-v1',
    evidence_ids_json TEXT NOT NULL DEFAULT '[]',
    positive_examples_json TEXT NOT NULL DEFAULT '[]',
    negative_examples_json TEXT NOT NULL DEFAULT '[]',
    source_finding_id TEXT,
    source_feedback_id INTEGER,
    superseded_by_id INTEGER,
    cost_tokens INTEGER NOT NULL DEFAULT 0,
    created_at REAL NOT NULL DEFAULT 0,
    updated_at REAL NOT NULL DEFAULT 0,
    UNIQUE(semantic_fingerprint, scope_type, scope_value),
    FOREIGN KEY (source_finding_id) REFERENCES review_findings(id) ON DELETE SET NULL,
    FOREIGN KEY (source_feedback_id) REFERENCES feedback_events_v2(id) ON DELETE SET NULL
);

CREATE INDEX IF NOT EXISTS idx_learning_candidates_status
    ON learning_candidates(status, updated_at);

CREATE TABLE IF NOT EXISTS learned_rules (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    rule_text TEXT NOT NULL DEFAULT '',
    source_signal TEXT NOT NULL DEFAULT '',
    category TEXT NOT NULL DEFAULT '',
    path_pattern TEXT NOT NULL DEFAULT '',
    sample_count INTEGER NOT NULL DEFAULT 0,
    active INTEGER NOT NULL DEFAULT 1,
    -- 'pending' | 'approved' | 'rejected'. Auto-synthesized rules start
    -- 'pending' and only feed reviews once an admin approves them.
    status TEXT NOT NULL DEFAULT 'approved',
    -- Username of the admin who authored a manual rule; '' for synthesized.
    created_by TEXT NOT NULL DEFAULT '',
    version INTEGER NOT NULL DEFAULT 1,
    scope_type TEXT NOT NULL DEFAULT 'repo',
    scope_value TEXT NOT NULL DEFAULT '',
    origin_candidate_id INTEGER,
    rationale TEXT NOT NULL DEFAULT '',
    evidence_count INTEGER NOT NULL DEFAULT 0,
    effective_from REAL NOT NULL DEFAULT 0,
    disabled_at REAL,
    supersedes_rule_id INTEGER,
    semantic_fingerprint TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL DEFAULT 0,
    updated_at REAL NOT NULL DEFAULT 0,
    FOREIGN KEY (origin_candidate_id) REFERENCES learning_candidates(id) ON DELETE SET NULL,
    FOREIGN KEY (supersedes_rule_id) REFERENCES learned_rules(id) ON DELETE SET NULL
);

-- Phase 3 continuous evaluation. One row per (rule, decision, finding-or-review)
-- exposure. `evaluation_key` is a deterministic hash of the exposure identity,
-- so a retried review round re-inserts the same key and is ignored instead of
-- inflating the rule's exposure count.
CREATE TABLE IF NOT EXISTS rule_evaluations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    evaluation_key TEXT NOT NULL UNIQUE,
    review_id INTEGER NOT NULL DEFAULT 0,
    rule_id INTEGER NOT NULL,
    rule_version INTEGER NOT NULL DEFAULT 1,
    -- 'manual' | 'learned'. A human-authored rule is never scored or
    -- suggested for downgrade as if Mira had invented it.
    rule_origin TEXT NOT NULL DEFAULT 'learned',
    scope_type TEXT NOT NULL DEFAULT 'repo',
    scope_value TEXT NOT NULL DEFAULT '',
    category TEXT NOT NULL DEFAULT '',
    -- 'instruction' | 'suppress' | 'boost'
    decision TEXT NOT NULL DEFAULT 'instruction',
    finding_id TEXT,
    platform TEXT NOT NULL DEFAULT 'github',
    owner TEXT NOT NULL DEFAULT '',
    repo TEXT NOT NULL DEFAULT '',
    pr_number INTEGER NOT NULL DEFAULT 0,
    pr_author TEXT NOT NULL DEFAULT '',
    head_sha TEXT NOT NULL DEFAULT '',
    detail_json TEXT NOT NULL DEFAULT '{}',
    created_at REAL NOT NULL DEFAULT 0,
    FOREIGN KEY (finding_id) REFERENCES review_findings(id) ON DELETE SET NULL
);

CREATE INDEX IF NOT EXISTS idx_rule_evaluations_rule
    ON rule_evaluations(rule_id, created_at);
CREATE INDEX IF NOT EXISTS idx_rule_evaluations_finding
    ON rule_evaluations(finding_id);
CREATE INDEX IF NOT EXISTS idx_rule_evaluations_period
    ON rule_evaluations(created_at, category);
CREATE INDEX IF NOT EXISTS idx_rule_evaluations_author
    ON rule_evaluations(pr_author, created_at);

-- Administrative trail for Phase 3: regression suggestions Mira raised, the
-- overrides an admin applied to them, and every analytics-driven rule change.
CREATE TABLE IF NOT EXISTS learning_audit_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_type TEXT NOT NULL,
    rule_id INTEGER NOT NULL DEFAULT 0,
    actor TEXT NOT NULL DEFAULT '',
    summary TEXT NOT NULL DEFAULT '',
    detail_json TEXT NOT NULL DEFAULT '{}',
    created_at REAL NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_learning_audit_rule
    ON learning_audit_events(rule_id, created_at);
CREATE INDEX IF NOT EXISTS idx_learning_audit_created
    ON learning_audit_events(created_at);

-- Phase 4 merge gate. One row per distinct evaluation: `decision_key` hashes
-- the PR, head commit, resolved policy *and* the inputs, so a redelivered
-- webhook over unchanged facts converges here instead of stacking rows, while
-- a re-evaluation after CI turned green is recorded as the new decision it is.
CREATE TABLE IF NOT EXISTS gate_decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    decision_key TEXT NOT NULL UNIQUE,
    platform TEXT NOT NULL DEFAULT 'github',
    owner TEXT NOT NULL DEFAULT '',
    repo TEXT NOT NULL DEFAULT '',
    pr_number INTEGER NOT NULL DEFAULT 0,
    pr_url TEXT NOT NULL DEFAULT '',
    pr_author TEXT NOT NULL DEFAULT '',
    base_branch TEXT NOT NULL DEFAULT '',
    head_sha TEXT NOT NULL DEFAULT '',
    review_id INTEGER NOT NULL DEFAULT 0,
    -- 'off' | 'shadow' | 'enforce'
    mode TEXT NOT NULL DEFAULT 'off',
    -- 'approved' | 'would_approve' | 'not_approved' | 'skipped' | 'error'
    state TEXT NOT NULL DEFAULT 'skipped',
    risk_score INTEGER NOT NULL DEFAULT 0,
    risk_band TEXT NOT NULL DEFAULT 'low',
    policy_version TEXT NOT NULL DEFAULT '',
    request_changes INTEGER NOT NULL DEFAULT 0,
    inputs_json TEXT NOT NULL DEFAULT '{}',
    factors_json TEXT NOT NULL DEFAULT '[]',
    reasons_json TEXT NOT NULL DEFAULT '[]',
    capabilities_json TEXT NOT NULL DEFAULT '{}',
    delivery_state TEXT NOT NULL DEFAULT 'not_attempted',
    delivery_ref TEXT NOT NULL DEFAULT '',
    delivery_attempts INTEGER NOT NULL DEFAULT 0,
    error TEXT NOT NULL DEFAULT '',
    -- Set when an admin moved this decision by hand; the trail is in
    -- gate_overrides, this column just keeps the list view honest.
    overridden_by TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL DEFAULT 0,
    updated_at REAL NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_gate_decisions_pr
    ON gate_decisions(pr_number, created_at);
CREATE INDEX IF NOT EXISTS idx_gate_decisions_state
    ON gate_decisions(state, created_at);
CREATE INDEX IF NOT EXISTS idx_gate_decisions_created
    ON gate_decisions(created_at);
CREATE INDEX IF NOT EXISTS idx_gate_decisions_head
    ON gate_decisions(pr_number, head_sha);

-- The claim that keeps a retried webhook from approving twice. Scoped to the
-- pull request and head commit rather than to a decision: two evaluations of
-- the same commit must still produce at most one approval.
CREATE TABLE IF NOT EXISTS gate_deliveries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    delivery_key TEXT NOT NULL UNIQUE,
    decision_key TEXT NOT NULL DEFAULT '',
    platform TEXT NOT NULL DEFAULT 'github',
    owner TEXT NOT NULL DEFAULT '',
    repo TEXT NOT NULL DEFAULT '',
    pr_number INTEGER NOT NULL DEFAULT 0,
    head_sha TEXT NOT NULL DEFAULT '',
    -- 'approval' | 'request_changes'
    kind TEXT NOT NULL DEFAULT '',
    -- 'pending' | 'in_flight' | 'delivered' | 'failed'
    state TEXT NOT NULL DEFAULT 'pending',
    ref TEXT NOT NULL DEFAULT '',
    attempts INTEGER NOT NULL DEFAULT 0,
    error TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL DEFAULT 0,
    updated_at REAL NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_gate_deliveries_pr
    ON gate_deliveries(pr_number, head_sha);

-- Administrative overrides. Append-only: the previous state is part of the
-- record, so a decision's history reads as a sequence of who changed what and
-- why, not as a final value with no provenance.
CREATE TABLE IF NOT EXISTS gate_overrides (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    override_key TEXT NOT NULL UNIQUE,
    decision_id INTEGER NOT NULL DEFAULT 0,
    decision_key TEXT NOT NULL DEFAULT '',
    platform TEXT NOT NULL DEFAULT 'github',
    owner TEXT NOT NULL DEFAULT '',
    repo TEXT NOT NULL DEFAULT '',
    pr_number INTEGER NOT NULL DEFAULT 0,
    head_sha TEXT NOT NULL DEFAULT '',
    actor TEXT NOT NULL DEFAULT '',
    reason TEXT NOT NULL DEFAULT '',
    previous_state TEXT NOT NULL DEFAULT '',
    new_state TEXT NOT NULL DEFAULT '',
    previous_risk INTEGER NOT NULL DEFAULT 0,
    detail_json TEXT NOT NULL DEFAULT '{}',
    created_at REAL NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_gate_overrides_decision
    ON gate_overrides(decision_id, created_at);
CREATE INDEX IF NOT EXISTS idx_gate_overrides_created
    ON gate_overrides(created_at);

CREATE TABLE IF NOT EXISTS package_manifests (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    kind TEXT NOT NULL DEFAULT '',
    version TEXT NOT NULL DEFAULT '',
    file_path TEXT NOT NULL DEFAULT '',
    is_dev INTEGER NOT NULL DEFAULT 0,
    updated_at REAL NOT NULL DEFAULT 0,
    UNIQUE(name, kind, file_path)
);

CREATE INDEX IF NOT EXISTS idx_pkg_manifest_name ON package_manifests(name);

CREATE TABLE IF NOT EXISTS vulnerabilities (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    package_name TEXT NOT NULL,
    ecosystem TEXT NOT NULL,
    package_version TEXT NOT NULL DEFAULT '',
    cve_id TEXT NOT NULL,
    summary TEXT NOT NULL DEFAULT '',
    severity TEXT NOT NULL DEFAULT 'unknown',
    advisory_url TEXT NOT NULL DEFAULT '',
    fixed_in TEXT NOT NULL DEFAULT '',
    first_seen_at REAL NOT NULL DEFAULT 0,
    last_seen_at REAL NOT NULL DEFAULT 0,
    UNIQUE(package_name, ecosystem, package_version, cve_id)
);

CREATE INDEX IF NOT EXISTS idx_vuln_package
    ON vulnerabilities(package_name, ecosystem, package_version);
CREATE INDEX IF NOT EXISTS idx_vuln_severity ON vulnerabilities(severity);
"""


@dataclass
class SymbolInfo:
    name: str
    kind: str  # "function", "class", "method", "constant"
    signature: str  # e.g. "def authenticate(token: str) -> Session"
    description: str  # one-line description


@dataclass
class FileSummary:
    path: str
    language: str
    summary: str
    symbols: list[SymbolInfo] = field(default_factory=list)
    imports: list[str] = field(default_factory=list)
    # (source_symbol, target_path, target_symbol)
    symbol_refs: list[tuple[str, str, str]] = field(default_factory=list)
    external_refs: list[ExternalRef] = field(default_factory=list)
    content_hash: str = ""
    loc: int = 0
    updated_at: float = 0.0


@dataclass
class DirectorySummary:
    path: str
    summary: str
    file_count: int
    updated_at: float = 0.0


@dataclass
class ExternalRef:
    file_path: str
    kind: str  # terraform_module, docker_image, api_endpoint, go_import, git_url, npm_package, pip_package
    target: str
    description: str = ""


@dataclass
class ReviewEvent:
    id: int
    pr_number: int
    pr_title: str
    pr_url: str
    author: str
    comments_posted: int
    blockers: int
    warnings: int
    suggestions: int
    files_reviewed: int
    lines_changed: int
    tokens_used: int
    duration_ms: int
    categories: str  # comma-separated: "bug,security,performance"
    created_at: float = 0.0
    author_avatar_url: str = ""
    reviewed_paths: str = ""  # JSON array of filenames reviewed this pass


@dataclass
class ReviewCommentRow:
    id: int
    review_id: int
    pr_number: int
    pr_url: str
    path: str
    line: int
    severity: str
    category: str
    title: str
    body: str
    github_comment_id: int = 0
    created_at: float = 0.0


@dataclass
class ReplyRow:
    id: int
    pr_number: int
    pr_url: str
    author: str
    author_avatar_url: str
    body: str
    comment_path: str
    comment_line: int
    github_comment_id: int = 0
    in_reply_to_id: int = 0
    created_at: float = 0.0


@dataclass
class ReviewContext:
    id: int
    title: str
    content: str
    created_at: float = 0.0
    updated_at: float = 0.0


@dataclass
class FeedbackEventRow:
    id: int
    pr_number: int
    pr_url: str
    comment_path: str
    comment_line: int
    comment_category: str
    comment_severity: str
    comment_title: str
    signal: str
    actor: str
    pr_author: str = ""
    created_at: float = 0.0


@dataclass
class LearnedRuleRow:
    id: int
    rule_text: str
    source_signal: str
    category: str
    path_pattern: str
    sample_count: int
    active: bool = True
    status: str = "approved"  # 'pending' | 'approved' | 'rejected'
    created_by: str = ""
    version: int = 1
    scope_type: str = "repo"
    scope_value: str = ""
    origin_candidate_id: int | None = None
    rationale: str = ""
    evidence_count: int = 0
    effective_from: float = 0.0
    disabled_at: float | None = None
    supersedes_rule_id: int | None = None
    semantic_fingerprint: str = ""
    created_at: float = 0.0
    updated_at: float = 0.0


@dataclass
class PackageManifestRow:
    id: int
    name: str
    kind: str  # "npm" | "pip" | "docker" | "go" | "rust" | "composer"
    version: str
    file_path: str
    is_dev: bool = False
    updated_at: float = 0.0


@dataclass
class VulnerabilityRow:
    id: int
    package_name: str
    ecosystem: str  # Mira's internal kind ("npm" | "pip" | "go" | "rust")
    package_version: str
    cve_id: str
    summary: str
    severity: str  # "critical" | "high" | "moderate" | "low" | "unknown"
    advisory_url: str
    fixed_in: str
    first_seen_at: float = 0.0
    last_seen_at: float = 0.0


@dataclass
class BlastRadiusEntry:
    path: str
    summary: str
    affected_symbols: list[str]
    depth: int


class IndexStore(_StoreSharedMixin, GateStoreMixin):
    """SQLite-backed index for a single repository."""

    def __init__(
        self,
        db_path: str,
        owner: str = "",
        repo: str = "",
        platform: str = "github",
    ) -> None:
        self._db_path = db_path
        self._owner = owner
        self._repo = repo
        self._platform = platform
        self._conn = sqlite3.connect(db_path)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.executescript(_SCHEMA)
        # Lightweight migration for the loc column added post-schema.
        cols = {r[1] for r in self._conn.execute("PRAGMA table_info(files)").fetchall()}
        if "loc" not in cols:
            self._conn.execute("ALTER TABLE files ADD COLUMN loc INTEGER NOT NULL DEFAULT 0")
        # Columns added to review_events post-schema (PR author + reviewed files).
        re_cols = {r[1] for r in self._conn.execute("PRAGMA table_info(review_events)").fetchall()}
        for col in ("author", "author_avatar_url", "reviewed_paths"):
            if col not in re_cols:
                self._conn.execute(
                    f"ALTER TABLE review_events ADD COLUMN {col} TEXT NOT NULL DEFAULT ''"
                )
        feedback_cols = {
            r[1] for r in self._conn.execute("PRAGMA table_info(feedback_events)").fetchall()
        }
        if "pr_author" not in feedback_cols:
            self._conn.execute(
                "ALTER TABLE feedback_events ADD COLUMN pr_author TEXT NOT NULL DEFAULT ''"
            )
        # learned_rules.status added post-schema. Default 'approved' so existing
        # rules keep feeding reviews; new synthesized rules are inserted 'pending'.
        lr_cols = {r[1] for r in self._conn.execute("PRAGMA table_info(learned_rules)").fetchall()}
        if "status" not in lr_cols:
            self._conn.execute(
                "ALTER TABLE learned_rules ADD COLUMN status TEXT NOT NULL DEFAULT 'approved'"
            )
        if "created_by" not in lr_cols:
            self._conn.execute(
                "ALTER TABLE learned_rules ADD COLUMN created_by TEXT NOT NULL DEFAULT ''"
            )
        learning_rule_columns = {
            "version": "INTEGER NOT NULL DEFAULT 1",
            "scope_type": "TEXT NOT NULL DEFAULT 'repo'",
            "scope_value": "TEXT NOT NULL DEFAULT ''",
            "origin_candidate_id": "INTEGER",
            "rationale": "TEXT NOT NULL DEFAULT ''",
            "evidence_count": "INTEGER NOT NULL DEFAULT 0",
            "effective_from": "REAL NOT NULL DEFAULT 0",
            "disabled_at": "REAL",
            "supersedes_rule_id": "INTEGER",
            "semantic_fingerprint": "TEXT NOT NULL DEFAULT ''",
        }
        for column, ddl in learning_rule_columns.items():
            if column not in lr_cols:
                self._conn.execute(f"ALTER TABLE learned_rules ADD COLUMN {column} {ddl}")
        self._conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_learned_rules_candidate_once "
            "ON learned_rules(origin_candidate_id) WHERE origin_candidate_id IS NOT NULL "
            "AND status = 'approved'"
        )
        self._conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_learned_rules_successor_once "
            "ON learned_rules(supersedes_rule_id) WHERE supersedes_rule_id IS NOT NULL "
            "AND status = 'approved'"
        )
        self._conn.execute(
            "UPDATE learned_rules SET scope_type = CASE WHEN path_pattern <> '' "
            "AND scope_type = 'repo' AND scope_value = '' THEN 'path' ELSE scope_type END, "
            "scope_value = CASE WHEN path_pattern <> '' AND scope_value = '' "
            "THEN path_pattern ELSE scope_value END, evidence_count = CASE "
            "WHEN evidence_count = 0 THEN sample_count ELSE evidence_count END, "
            "effective_from = CASE WHEN effective_from = 0 AND status = 'approved' "
            "AND active = 1 THEN created_at ELSE effective_from END"
        )
        self._conn.commit()
        self._backfill_feedback_v2()

    @classmethod
    def open(cls, owner: str, repo: str, platform: str = "github"):  # type: ignore[no-untyped-def]
        """Open (or create) the index store for a repo.

        Returns a PgIndexStore if DATABASE_URL is set, otherwise an IndexStore
        backed by a per-repo SQLite file. Non-GitHub platforms are namespaced
        (``_{platform}/{owner}``) so a same-named repo on another platform gets
        its own store; GitHub paths are unchanged for back-compat.
        """
        key_owner = owner if platform == "github" else f"_{platform}/{owner}"
        db_url = os.environ.get("DATABASE_URL", "")
        if db_url.startswith("postgresql://") or db_url.startswith("postgres://"):
            try:
                from mira.index.pg_store import PgIndexStore

                return PgIndexStore(key_owner, repo, db_url)
            except Exception as exc:
                logger.warning("Postgres store unavailable (%s), falling back to SQLite", exc)

        index_dir = os.environ.get("MIRA_INDEX_DIR", _INDEX_DIR)
        repo_dir = os.path.join(index_dir, key_owner)
        os.makedirs(repo_dir, exist_ok=True)
        db_path = os.path.join(repo_dir, f"{repo}.db")
        return cls(db_path, owner=owner, repo=repo, platform=platform)

    def get_summary(self, path: str) -> FileSummary | None:
        """Get the summary for a single file."""
        row = self._conn.execute(
            "SELECT path, language, summary, content_hash, loc, updated_at "
            "FROM files WHERE path = ?",
            (path,),
        ).fetchone()
        if row is None:
            return None
        fs = FileSummary(
            path=row[0],
            language=row[1],
            summary=row[2],
            content_hash=row[3],
            loc=row[4] or 0,
            updated_at=row[5],
        )
        fs.symbols = self._load_symbols(path)
        fs.imports = self._load_imports(path)
        fs.symbol_refs = self._load_symbol_refs(path)
        fs.external_refs = self._load_external_refs(path)
        return fs

    def get_dependents(self, path: str) -> list[str]:
        """Files that import this path."""
        rows = self._conn.execute(
            "SELECT source_path FROM imports WHERE target_path = ?", (path,)
        ).fetchall()
        return [r[0] for r in rows]

    def get_directory_summary(self, path: str) -> DirectorySummary | None:
        """Get summary for a single directory."""
        row = self._conn.execute(
            "SELECT path, summary, file_count, updated_at FROM directories WHERE path = ?",
            (path,),
        ).fetchone()
        if row is None:
            return None
        return DirectorySummary(path=row[0], summary=row[1], file_count=row[2], updated_at=row[3])

    def upsert_summary(self, summary: FileSummary) -> None:
        """Insert or update a file summary and its related data."""
        now = time.time()
        self._conn.execute(
            """INSERT INTO files (path, language, summary, content_hash, loc, updated_at)
               VALUES (?, ?, ?, ?, ?, ?)
               ON CONFLICT(path) DO UPDATE SET
                 language=excluded.language,
                 summary=excluded.summary,
                 content_hash=excluded.content_hash,
                 loc=excluded.loc,
                 updated_at=excluded.updated_at""",
            (
                summary.path,
                summary.language,
                summary.summary,
                summary.content_hash,
                summary.loc,
                now,
            ),
        )
        self._conn.execute("DELETE FROM symbols WHERE file_path = ?", (summary.path,))
        for sym in summary.symbols:
            # Two symbols can share a name in one file (overloads, or LLM dupes)
            # and collide on the PK — keep the last, don't raise.
            self._conn.execute(
                "INSERT OR REPLACE INTO symbols (file_path, name, kind, signature, description) "
                "VALUES (?, ?, ?, ?, ?)",
                (summary.path, sym.name, sym.kind, sym.signature, sym.description),
            )
        self._conn.execute("DELETE FROM imports WHERE source_path = ?", (summary.path,))
        for target in set(summary.imports):
            self._conn.execute(
                "INSERT OR IGNORE INTO imports (source_path, target_path) VALUES (?, ?)",
                (summary.path, target),
            )
        self._conn.execute("DELETE FROM symbol_refs WHERE source_path = ?", (summary.path,))
        for src_sym, tgt_path, tgt_sym in set(summary.symbol_refs):
            self._conn.execute(
                "INSERT OR IGNORE INTO symbol_refs "
                "(source_path, source_symbol, target_path, target_symbol) "
                "VALUES (?, ?, ?, ?)",
                (summary.path, src_sym, tgt_path, tgt_sym),
            )
        self._conn.execute("DELETE FROM external_refs WHERE file_path = ?", (summary.path,))
        seen_refs: set[tuple[str, str]] = set()
        for ref in summary.external_refs:
            key = (ref.kind, ref.target)
            if key in seen_refs:
                continue
            seen_refs.add(key)
            self._conn.execute(
                "INSERT OR IGNORE INTO external_refs (file_path, kind, target, description) "
                "VALUES (?, ?, ?, ?)",
                (summary.path, ref.kind, ref.target, ref.description),
            )
        self._conn.commit()

    def upsert_directory(self, summary: DirectorySummary) -> None:
        """Insert or update a directory summary."""
        now = time.time()
        self._conn.execute(
            """INSERT INTO directories (path, summary, file_count, updated_at)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(path) DO UPDATE SET
                 summary=excluded.summary,
                 file_count=excluded.file_count,
                 updated_at=excluded.updated_at""",
            (summary.path, summary.summary, summary.file_count, now),
        )
        self._conn.commit()

    def remove_paths(self, paths: list[str]) -> None:
        """Remove files (and their symbols/imports via CASCADE) from the index."""
        for path in paths:
            self._conn.execute("DELETE FROM files WHERE path = ?", (path,))
        self._conn.commit()

    def all_paths(self) -> set[str]:
        """Return all indexed file paths."""
        rows = self._conn.execute("SELECT path FROM files").fetchall()
        return {r[0] for r in rows}

    def get_call_graph(self, path: str, symbol: str) -> list[tuple[str, str]]:
        """Who calls this symbol? Returns list of (file_path, calling_symbol)."""
        rows = self._conn.execute(
            "SELECT source_path, source_symbol FROM symbol_refs "
            "WHERE target_path = ? AND target_symbol = ?",
            (path, symbol),
        ).fetchall()
        return [(r[0], r[1]) for r in rows]

    def get_reverse_deps(self, path: str, max_depth: int = 3) -> list[str]:
        """All files that (transitively) depend on this file, up to max_depth."""
        visited: set[str] = set()
        frontier = {path}
        for _ in range(max_depth):
            next_frontier: set[str] = set()
            for p in frontier:
                if p in visited:
                    continue
                visited.add(p)
                for dep in self.get_dependents(p):
                    if dep not in visited:
                        next_frontier.add(dep)
            frontier = next_frontier
            if not frontier:
                break
        visited.discard(path)
        return sorted(visited)

    def get_inbound_edge_counts(self, paths: list[str]) -> dict[str, int]:
        """Count how many other files reference each path via symbol_refs or imports.

        Used to rank files by importance — files with more callers/importers
        should get priority in the context budget.
        """
        counts: dict[str, int] = {}
        for path in paths:
            # Count import dependents
            import_count = self._conn.execute(
                "SELECT COUNT(*) FROM imports WHERE target_path = ?", (path,)
            ).fetchone()[0]
            # Count symbol_ref callers
            ref_count = self._conn.execute(
                "SELECT COUNT(DISTINCT source_path) FROM symbol_refs WHERE target_path = ?",
                (path,),
            ).fetchone()[0]
            counts[path] = import_count + ref_count
        return counts

    def get_blast_radius(self, changed_paths: list[str]) -> list[BlastRadiusEntry]:
        """For changed files, compute which files + symbols are affected."""
        entries: dict[str, BlastRadiusEntry] = {}

        for changed_path in changed_paths:
            # Get all symbols in the changed file
            symbols = self._load_symbols(changed_path)
            for sym in symbols:
                callers = self.get_call_graph(changed_path, sym.name)
                for caller_path, caller_symbol in callers:
                    if caller_path in changed_paths:
                        continue
                    if caller_path not in entries:
                        # Fetch summary for the caller file
                        row = self._conn.execute(
                            "SELECT summary FROM files WHERE path = ?", (caller_path,)
                        ).fetchone()
                        summary = row[0] if row else ""
                        entries[caller_path] = BlastRadiusEntry(
                            path=caller_path, summary=summary, affected_symbols=[], depth=1
                        )
                    entry = entries[caller_path]
                    if caller_symbol not in entry.affected_symbols:
                        entry.affected_symbols.append(caller_symbol)

        # Depth 2: callers of callers
        depth1_paths = list(entries.keys())
        for d1_path in depth1_paths:
            d1_entry = entries[d1_path]
            for affected_sym in list(d1_entry.affected_symbols):
                callers = self.get_call_graph(d1_path, affected_sym)
                for caller_path, caller_symbol in callers:
                    if caller_path in changed_paths or caller_path in depth1_paths:
                        continue
                    if caller_path not in entries:
                        row = self._conn.execute(
                            "SELECT summary FROM files WHERE path = ?", (caller_path,)
                        ).fetchone()
                        summary = row[0] if row else ""
                        entries[caller_path] = BlastRadiusEntry(
                            path=caller_path, summary=summary, affected_symbols=[], depth=2
                        )
                    entry = entries[caller_path]
                    if caller_symbol not in entry.affected_symbols:
                        entry.affected_symbols.append(caller_symbol)

        return sorted(entries.values(), key=lambda e: (e.depth, e.path))

    def close(self) -> None:
        """Close the database connection."""
        self._conn.close()

    def record_review(
        self,
        pr_number: int,
        pr_title: str,
        pr_url: str,
        comments_posted: int,
        blockers: int,
        warnings: int,
        suggestions: int = 0,
        files_reviewed: int = 0,
        lines_changed: int = 0,
        tokens_used: int = 0,
        duration_ms: int = 0,
        categories: str = "",
        created_at: float | None = None,
        author: str = "",
        author_avatar_url: str = "",
        reviewed_paths: str = "",
    ) -> ReviewEvent:
        now = created_at if created_at is not None else time.time()
        self._conn.execute(
            "INSERT INTO review_events "
            "(pr_number, pr_title, pr_url, author, comments_posted, blockers, warnings, "
            "suggestions, files_reviewed, lines_changed, tokens_used, duration_ms, "
            "categories, author_avatar_url, reviewed_paths, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                pr_number,
                pr_title,
                pr_url,
                author,
                comments_posted,
                blockers,
                warnings,
                suggestions,
                files_reviewed,
                lines_changed,
                tokens_used,
                duration_ms,
                categories,
                author_avatar_url,
                reviewed_paths,
                now,
            ),
        )
        self._conn.commit()
        row_id = self._conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        return ReviewEvent(
            id=row_id,
            pr_number=pr_number,
            pr_title=pr_title,
            pr_url=pr_url,
            author=author,
            comments_posted=comments_posted,
            blockers=blockers,
            warnings=warnings,
            suggestions=suggestions,
            files_reviewed=files_reviewed,
            lines_changed=lines_changed,
            tokens_used=tokens_used,
            duration_ms=duration_ms,
            categories=categories,
            created_at=now,
            author_avatar_url=author_avatar_url,
            reviewed_paths=reviewed_paths,
        )

    def list_review_events(self, limit: int = 100) -> list[ReviewEvent]:
        rows = self._conn.execute(
            "SELECT id, pr_number, pr_title, pr_url, author, comments_posted, blockers, warnings, "
            "suggestions, files_reviewed, lines_changed, tokens_used, duration_ms, "
            "categories, created_at, author_avatar_url, reviewed_paths "
            "FROM review_events ORDER BY created_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [
            ReviewEvent(
                id=r[0],
                pr_number=r[1],
                pr_title=r[2],
                pr_url=r[3],
                author=r[4],
                comments_posted=r[5],
                blockers=r[6],
                warnings=r[7],
                suggestions=r[8],
                files_reviewed=r[9],
                lines_changed=r[10],
                tokens_used=r[11],
                duration_ms=r[12],
                categories=r[13],
                created_at=r[14],
                author_avatar_url=r[15],
                reviewed_paths=r[16],
            )
            for r in rows
        ]

    def list_review_events_for_pr(self, pr_number: int) -> list[ReviewEvent]:
        rows = self._conn.execute(
            "SELECT id, pr_number, pr_title, pr_url, author, comments_posted, blockers, warnings, "
            "suggestions, files_reviewed, lines_changed, tokens_used, duration_ms, "
            "categories, created_at, author_avatar_url, reviewed_paths "
            "FROM review_events WHERE pr_number = ? ORDER BY created_at DESC",
            (pr_number,),
        ).fetchall()
        return [
            ReviewEvent(
                id=r[0],
                pr_number=r[1],
                pr_title=r[2],
                pr_url=r[3],
                author=r[4],
                comments_posted=r[5],
                blockers=r[6],
                warnings=r[7],
                suggestions=r[8],
                files_reviewed=r[9],
                lines_changed=r[10],
                tokens_used=r[11],
                duration_ms=r[12],
                categories=r[13],
                created_at=r[14],
                author_avatar_url=r[15],
                reviewed_paths=r[16],
            )
            for r in rows
        ]

    def add_review_comments(
        self, review_id: int, pr_number: int, pr_url: str, comments: list[dict]
    ) -> None:
        """Persist the individual comments Mira posted for a review pass.

        Each dict carries: path, line, severity, category, title, body, and
        optionally github_comment_id.
        """
        if not comments:
            return
        now = time.time()
        self._conn.executemany(
            "INSERT INTO review_comments "
            "(review_id, pr_number, pr_url, path, line, severity, category, "
            "title, body, github_comment_id, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    review_id,
                    pr_number,
                    pr_url,
                    c.get("path", ""),
                    c.get("line", 0),
                    c.get("severity", ""),
                    c.get("category", ""),
                    c.get("title", ""),
                    c.get("body", ""),
                    c.get("github_comment_id", 0),
                    now,
                )
                for c in comments
            ],
        )
        self._conn.commit()

    def list_review_comments(self, pr_number: int) -> list[ReviewCommentRow]:
        rows = self._conn.execute(
            "SELECT id, review_id, pr_number, pr_url, path, line, severity, category, "
            "title, body, github_comment_id, created_at "
            "FROM review_comments WHERE pr_number = ? ORDER BY created_at",
            (pr_number,),
        ).fetchall()
        return [
            ReviewCommentRow(
                id=r[0],
                review_id=r[1],
                pr_number=r[2],
                pr_url=r[3],
                path=r[4],
                line=r[5],
                severity=r[6],
                category=r[7],
                title=r[8],
                body=r[9],
                github_comment_id=r[10],
                created_at=r[11],
            )
            for r in rows
        ]

    def record_reply(
        self,
        pr_number: int,
        pr_url: str,
        author: str,
        body: str,
        author_avatar_url: str = "",
        comment_path: str = "",
        comment_line: int = 0,
        github_comment_id: int = 0,
        in_reply_to_id: int = 0,
        created_at: float | None = None,
    ) -> ReplyRow:
        """Record a human reply on a PR for the conversation timeline."""
        now = created_at if created_at is not None else time.time()
        cur = self._conn.execute(
            "INSERT INTO pr_replies "
            "(pr_number, pr_url, author, author_avatar_url, body, comment_path, "
            "comment_line, github_comment_id, in_reply_to_id, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                pr_number,
                pr_url,
                author,
                author_avatar_url,
                body,
                comment_path,
                comment_line,
                github_comment_id,
                in_reply_to_id,
                now,
            ),
        )
        self._conn.commit()
        return ReplyRow(
            id=cur.lastrowid or 0,
            pr_number=pr_number,
            pr_url=pr_url,
            author=author,
            author_avatar_url=author_avatar_url,
            body=body,
            comment_path=comment_path,
            comment_line=comment_line,
            github_comment_id=github_comment_id,
            in_reply_to_id=in_reply_to_id,
            created_at=now,
        )

    def list_replies(self, pr_number: int) -> list[ReplyRow]:
        rows = self._conn.execute(
            "SELECT id, pr_number, pr_url, author, author_avatar_url, body, "
            "comment_path, comment_line, github_comment_id, in_reply_to_id, created_at "
            "FROM pr_replies WHERE pr_number = ? ORDER BY created_at",
            (pr_number,),
        ).fetchall()
        return [
            ReplyRow(
                id=r[0],
                pr_number=r[1],
                pr_url=r[2],
                author=r[3],
                author_avatar_url=r[4],
                body=r[5],
                comment_path=r[6],
                comment_line=r[7],
                github_comment_id=r[8],
                in_reply_to_id=r[9],
                created_at=r[10],
            )
            for r in rows
        ]

    def get_review_quality_by_author(self, author: str, since: float | None = None) -> dict:
        """Sum the blockers/warnings/suggestions raised on one author's PRs.

        Mira's differentiated signal: how much review attention a contributor's
        code drew. Keyed by GitHub login recorded on each review event.
        """
        where = "WHERE author = ?"
        params: list = [author]
        if since:
            where += " AND created_at >= ?"
            params.append(since)
        row = self._conn.execute(
            "SELECT COUNT(*), COALESCE(SUM(blockers),0), COALESCE(SUM(warnings),0), "
            "COALESCE(SUM(suggestions),0), COALESCE(SUM(comments_posted),0) "
            f"FROM review_events {where}",
            tuple(params),
        ).fetchone()
        return {
            "reviews": int(row[0] or 0),
            "blockers": int(row[1] or 0),
            "warnings": int(row[2] or 0),
            "suggestions": int(row[3] or 0),
            "comments_posted": int(row[4] or 0),
        }

    def get_feedback_quality_by_author(self, pr_author: str) -> dict:
        """Accept/reject tallies for Mira's feedback on one author's PRs."""
        rows = self._conn.execute(
            "SELECT signal, COUNT(*) FROM feedback_events WHERE pr_author = ? GROUP BY signal",
            (pr_author,),
        ).fetchall()
        counts = {signal: int(count) for signal, count in rows}
        accepted = counts.get("accepted", 0)
        rejected = counts.get("rejected", 0)
        return {
            "accepted": accepted,
            "rejected": rejected,
            "human_review": counts.get("human_review", 0),
        }

    def get_review_stats(self, since: float | None = None) -> dict:
        """Aggregate review statistics, optionally filtered to events after *since* (epoch)."""
        where = " WHERE created_at >= ?" if since else ""
        params: tuple = (since,) if since else ()

        row = self._conn.execute(
            "SELECT COUNT(*), COALESCE(SUM(comments_posted),0), COALESCE(SUM(blockers),0), "
            "COALESCE(SUM(warnings),0), COALESCE(SUM(suggestions),0), "
            "COALESCE(SUM(files_reviewed),0), COALESCE(SUM(lines_changed),0), "
            "COALESCE(SUM(tokens_used),0), COALESCE(AVG(duration_ms),0) "
            f"FROM review_events{where}",
            params,
        ).fetchone()

        # Aggregate categories
        cat_where = f" WHERE categories != ''{' AND created_at >= ?' if since else ''}"
        cat_params: tuple = (since,) if since else ()
        cat_rows = self._conn.execute(
            f"SELECT categories FROM review_events{cat_where}",
            cat_params,
        ).fetchall()
        cat_counts: dict[str, int] = {}
        for (cats,) in cat_rows:
            for c in cats.split(","):
                c = c.strip()
                if c:
                    cat_counts[c] = cat_counts.get(c, 0) + 1

        return {
            "total_reviews": row[0],
            "total_comments": row[1],
            "total_blockers": row[2],
            "total_warnings": row[3],
            "total_suggestions": row[4],
            "total_files_reviewed": row[5],
            "total_lines_changed": row[6],
            "total_tokens": row[7],
            "avg_duration_ms": int(row[8]),
            "categories": cat_counts,
        }

    # Fingerprints untouched this long belong to closed/abandoned PRs — prune
    # them on write so the table doesn't grow forever.
    _FINGERPRINT_TTL = 30 * 86400

    def upsert_pr_fingerprint(self, fp: PRFingerprint) -> None:
        """Insert or update the change fingerprint for a PR in this repo."""
        now = fp.updated_at or time.time()
        self._conn.execute(
            "DELETE FROM pr_fingerprints WHERE updated_at < ?",
            (now - self._FINGERPRINT_TTL,),
        )
        self._conn.execute(
            """INSERT INTO pr_fingerprints
                   (pr_number, head_sha, title, body, paths, symbols, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(pr_number) DO UPDATE SET
                 head_sha=excluded.head_sha,
                 title=excluded.title,
                 body=excluded.body,
                 paths=excluded.paths,
                 symbols=excluded.symbols,
                 updated_at=excluded.updated_at""",
            (
                fp.pr_number,
                fp.head_sha,
                fp.title,
                fp.body,
                json.dumps(fp.paths),
                json.dumps(fp.symbols),
                now,
            ),
        )
        self._conn.commit()

    def list_pr_fingerprints(self) -> list[PRFingerprint]:
        """Return every cached PR fingerprint for this repo."""
        rows = self._conn.execute(
            "SELECT pr_number, head_sha, title, body, paths, symbols, updated_at "
            "FROM pr_fingerprints"
        ).fetchall()
        return [
            PRFingerprint(
                pr_number=r[0],
                head_sha=r[1],
                title=r[2],
                body=r[3],
                paths=json.loads(r[4] or "[]"),
                symbols=json.loads(r[5] or "[]"),
                updated_at=r[6],
            )
            for r in rows
        ]

    def list_review_context(self) -> list[ReviewContext]:
        """List all review context entries."""
        rows = self._conn.execute(
            "SELECT id, title, content, created_at, updated_at FROM review_context ORDER BY updated_at DESC"
        ).fetchall()
        return [
            ReviewContext(id=r[0], title=r[1], content=r[2], created_at=r[3], updated_at=r[4])
            for r in rows
        ]

    def get_review_context(self, context_id: int) -> ReviewContext | None:
        row = self._conn.execute(
            "SELECT id, title, content, created_at, updated_at FROM review_context WHERE id = ?",
            (context_id,),
        ).fetchone()
        if row is None:
            return None
        return ReviewContext(
            id=row[0], title=row[1], content=row[2], created_at=row[3], updated_at=row[4]
        )

    def upsert_review_context(
        self, title: str, content: str, context_id: int | None = None
    ) -> ReviewContext:
        """Create or update a review context entry."""
        now = time.time()
        if context_id is not None:
            self._conn.execute(
                "UPDATE review_context SET title = ?, content = ?, updated_at = ? WHERE id = ?",
                (title, content, now, context_id),
            )
        else:
            self._conn.execute(
                "INSERT INTO review_context (title, content, created_at, updated_at) VALUES (?, ?, ?, ?)",
                (title, content, now, now),
            )
            context_id = self._conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        self._conn.commit()
        return self.get_review_context(context_id)  # type: ignore[return-value]

    def delete_review_context(self, context_id: int) -> None:
        self._conn.execute("DELETE FROM review_context WHERE id = ?", (context_id,))
        self._conn.commit()

    def get_files_referencing(self, target: str) -> list[ExternalRef]:
        """Find all external refs whose target contains the given string."""
        rows = self._conn.execute(
            "SELECT file_path, kind, target, description FROM external_refs WHERE target LIKE ?",
            (f"%{target}%",),
        ).fetchall()
        return [ExternalRef(file_path=r[0], kind=r[1], target=r[2], description=r[3]) for r in rows]

    def get_all_external_targets(self) -> list[str]:
        """Return all unique external ref targets."""
        rows = self._conn.execute("SELECT DISTINCT target FROM external_refs").fetchall()
        return [r[0] for r in rows]

    def _load_external_refs(self, path: str) -> list[ExternalRef]:
        rows = self._conn.execute(
            "SELECT file_path, kind, target, description FROM external_refs WHERE file_path = ?",
            (path,),
        ).fetchall()
        return [ExternalRef(file_path=r[0], kind=r[1], target=r[2], description=r[3]) for r in rows]

    def _load_symbols(self, path: str) -> list[SymbolInfo]:
        rows = self._conn.execute(
            "SELECT name, kind, signature, description FROM symbols WHERE file_path = ?",
            (path,),
        ).fetchall()
        return [SymbolInfo(name=r[0], kind=r[1], signature=r[2], description=r[3]) for r in rows]

    def _load_imports(self, path: str) -> list[str]:
        rows = self._conn.execute(
            "SELECT target_path FROM imports WHERE source_path = ?", (path,)
        ).fetchall()
        return [r[0] for r in rows]

    def _load_symbol_refs(self, path: str) -> list[tuple[str, str, str]]:
        rows = self._conn.execute(
            "SELECT source_symbol, target_path, target_symbol "
            "FROM symbol_refs WHERE source_path = ?",
            (path,),
        ).fetchall()
        return [(r[0], r[1], r[2]) for r in rows]

    # ── Provenance-complete findings and feedback ──

    def save_review_finding(self, finding: ReviewFinding) -> ReviewFinding:
        """Persist a finding before its platform comment is posted."""
        now = time.time()
        if not finding.created_at:
            finding.created_at = now
        finding.updated_at = now
        self._conn.execute(
            "INSERT INTO review_findings "
            "(id, fingerprint, review_id, platform, owner, repo, pr_number, pr_url, "
            "base_sha, head_sha, path, start_line, end_line, symbol, category, severity, "
            "confidence, title, body, suggestion, detector, prompt_model, "
            "platform_comment_id, platform_thread_id, state, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, "
            "?, ?, ?, ?, ?) "
            "ON CONFLICT(id) DO UPDATE SET fingerprint=excluded.fingerprint, "
            "review_id=excluded.review_id, platform_comment_id=excluded.platform_comment_id, "
            "platform_thread_id=excluded.platform_thread_id, state=excluded.state, "
            "updated_at=excluded.updated_at",
            (
                finding.id,
                finding.fingerprint,
                finding.review_id,
                finding.platform,
                finding.owner,
                finding.repo,
                finding.pr_number,
                finding.pr_url,
                finding.base_sha,
                finding.head_sha,
                finding.path,
                finding.start_line,
                finding.end_line,
                finding.symbol,
                finding.category,
                finding.severity,
                finding.confidence,
                finding.title,
                finding.body,
                finding.suggestion,
                finding.detector,
                finding.prompt_model,
                finding.platform_comment_id,
                finding.platform_thread_id,
                finding.state,
                finding.created_at,
                finding.updated_at,
            ),
        )
        self._conn.commit()
        return finding

    def update_review_finding_posted(
        self,
        finding_id: str,
        platform_comment_id: str | int = "",
        platform_thread_id: str = "",
    ) -> None:
        """Attach IDs returned by the hosting platform to a persisted finding."""
        self._conn.execute(
            "UPDATE review_findings SET platform_comment_id = CASE WHEN ? <> '' THEN ? "
            "ELSE platform_comment_id END, platform_thread_id = CASE WHEN ? <> '' THEN ? "
            "ELSE platform_thread_id END, updated_at = ? WHERE id = ?",
            (
                str(platform_comment_id or ""),
                str(platform_comment_id or ""),
                platform_thread_id,
                platform_thread_id,
                time.time(),
                finding_id,
            ),
        )
        self._conn.commit()

    def link_review_findings(self, finding_ids: list[str], review_id: int) -> None:
        if not finding_ids:
            return
        self._conn.executemany(
            "UPDATE review_findings SET review_id = ?, updated_at = ? WHERE id = ?",
            [(review_id, time.time(), finding_id) for finding_id in finding_ids],
        )
        self._conn.commit()

    @staticmethod
    def _finding_from_row(row: tuple) -> ReviewFinding:
        return ReviewFinding(
            id=row[0],
            fingerprint=row[1],
            review_id=row[2],
            platform=row[3],
            owner=row[4],
            repo=row[5],
            pr_number=row[6],
            pr_url=row[7],
            base_sha=row[8],
            head_sha=row[9],
            path=row[10],
            start_line=row[11],
            end_line=row[12],
            symbol=row[13],
            category=row[14],
            severity=row[15],
            confidence=row[16],
            title=row[17],
            body=row[18],
            suggestion=row[19],
            detector=row[20],
            prompt_model=row[21],
            platform_comment_id=row[22],
            platform_thread_id=row[23],
            state=row[24],
            created_at=row[25],
            updated_at=row[26],
        )

    def get_review_finding(self, finding_id: str) -> ReviewFinding | None:
        row = self._conn.execute(
            "SELECT id, fingerprint, review_id, platform, owner, repo, pr_number, pr_url, "
            "base_sha, head_sha, path, start_line, end_line, symbol, category, severity, "
            "confidence, title, body, suggestion, detector, prompt_model, "
            "platform_comment_id, platform_thread_id, state, created_at, updated_at "
            "FROM review_findings WHERE id = ?",
            (finding_id,),
        ).fetchone()
        return self._finding_from_row(row) if row else None

    def find_review_finding(
        self,
        *,
        platform_comment_id: str | int = "",
        platform_thread_id: str = "",
        pr_number: int = 0,
        path: str = "",
        line: int = 0,
    ) -> ReviewFinding | None:
        clauses: list[str] = []
        params: list[object] = []
        if platform_comment_id:
            clauses.append("platform_comment_id = ?")
            params.append(str(platform_comment_id))
        if platform_thread_id:
            clauses.append("platform_thread_id = ?")
            params.append(platform_thread_id)
        if not clauses and pr_number and path:
            clauses.extend(("pr_number = ?", "path = ?"))
            params.extend((pr_number, path))
            if line:
                clauses.append("start_line = ?")
                params.append(line)
        if not clauses:
            return None
        row = self._conn.execute(
            "SELECT id, fingerprint, review_id, platform, owner, repo, pr_number, pr_url, "
            "base_sha, head_sha, path, start_line, end_line, symbol, category, severity, "
            "confidence, title, body, suggestion, detector, prompt_model, "
            "platform_comment_id, platform_thread_id, state, created_at, updated_at "
            f"FROM review_findings WHERE {' AND '.join(clauses)} "
            "ORDER BY created_at DESC LIMIT 1",
            tuple(params),
        ).fetchone()
        return self._finding_from_row(row) if row else None

    def update_review_finding_state(self, finding_id: str, state: str) -> None:
        self._conn.execute(
            "UPDATE review_findings SET state = ?, updated_at = ? WHERE id = ?",
            (state, time.time(), finding_id),
        )
        self._conn.commit()

    def record_feedback_v2(self, event: FeedbackEventV2) -> tuple[FeedbackEventV2, bool]:
        """Insert once per platform webhook event and report whether it was new."""
        if not event.source_event_id:
            raise ValueError("source_event_id is required for idempotent feedback")
        now = event.created_at or time.time()
        finding = self.get_review_finding(event.finding_id) if event.finding_id else None
        finding_exists = finding is not None
        event.provenance_complete = bool(
            finding and finding.path and finding.category and finding.head_sha
        )
        cur = self._conn.execute(
            "INSERT OR IGNORE INTO feedback_events_v2 "
            "(finding_id, kind, actor, actor_role, raw_text, rationale, platform, "
            "source_event_id, head_sha, thread_state, provenance_complete, audit_json, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                event.finding_id if finding_exists else None,
                event.kind,
                event.actor,
                event.actor_role,
                event.raw_text,
                event.rationale,
                event.platform,
                event.source_event_id,
                event.head_sha,
                event.thread_state,
                int(event.provenance_complete),
                event.audit_json,
                now,
            ),
        )
        self._conn.commit()
        row = self._conn.execute(
            "SELECT id, finding_id, kind, actor, actor_role, raw_text, rationale, platform, "
            "source_event_id, head_sha, thread_state, provenance_complete, audit_json, created_at "
            "FROM feedback_events_v2 WHERE platform = ? AND source_event_id = ?",
            (event.platform, event.source_event_id),
        ).fetchone()
        if row is None:
            raise RuntimeError("feedback event insert did not return a row")
        stored = FeedbackEventV2(
            id=row[0],
            finding_id=row[1],
            kind=row[2],
            actor=row[3],
            actor_role=row[4],
            raw_text=row[5],
            rationale=row[6],
            platform=row[7],
            source_event_id=row[8],
            head_sha=row[9],
            thread_state=row[10],
            provenance_complete=bool(row[11]),
            audit_json=row[12],
            created_at=row[13],
        )
        return stored, cur.rowcount == 1

    def list_feedback_v2(
        self, *, finding_id: str = "", pr_number: int = 0, limit: int = 500
    ) -> list[FeedbackEventV2]:
        where: list[str] = []
        params: list[object] = []
        if finding_id:
            where.append("e.finding_id = ?")
            params.append(finding_id)
        if pr_number:
            where.append("f.pr_number = ?")
            params.append(pr_number)
        clause = f"WHERE {' AND '.join(where)}" if where else ""
        params.append(limit)
        rows = self._conn.execute(
            "SELECT e.id, e.finding_id, e.kind, e.actor, e.actor_role, e.raw_text, "
            "e.rationale, e.platform, e.source_event_id, e.head_sha, e.thread_state, "
            "e.provenance_complete, e.audit_json, e.created_at FROM feedback_events_v2 e "
            "LEFT JOIN review_findings f ON f.id = e.finding_id "
            f"{clause} ORDER BY e.created_at DESC LIMIT ?",
            tuple(params),
        ).fetchall()
        return [
            FeedbackEventV2(
                id=r[0],
                finding_id=r[1],
                kind=r[2],
                actor=r[3],
                actor_role=r[4],
                raw_text=r[5],
                rationale=r[6],
                platform=r[7],
                source_event_id=r[8],
                head_sha=r[9],
                thread_state=r[10],
                provenance_complete=bool(r[11]),
                audit_json=r[12],
                created_at=r[13],
            )
            for r in rows
        ]

    def _backfill_feedback_v2(self) -> None:
        """Best-effort migration of legacy review comments and feedback rows."""
        try:
            comments = self._conn.execute(
                "SELECT id, review_id, pr_number, pr_url, path, line, severity, category, "
                "title, body, github_comment_id, created_at FROM review_comments"
            ).fetchall()
            for row in comments:
                finding_id = legacy_finding_id(self._db_path, "review_comment", row[0])
                fingerprint = finding_fingerprint(
                    owner=self._owner,
                    repo=self._repo,
                    pr_number=row[2],
                    base_sha="",
                    head_sha="",
                    path=row[4],
                    symbol="",
                    category=row[7],
                    detector="legacy",
                    problem=f"{row[8]} {row[9]}",
                )
                self._conn.execute(
                    "INSERT OR IGNORE INTO review_findings "
                    "(id, fingerprint, review_id, platform, owner, repo, pr_number, pr_url, "
                    "path, start_line, end_line, category, severity, title, body, detector, "
                    "platform_comment_id, state, created_at, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'legacy', ?, "
                    "'open', ?, ?)",
                    (
                        finding_id,
                        fingerprint,
                        row[1],
                        self._platform,
                        self._owner,
                        self._repo,
                        row[2],
                        row[3],
                        row[4],
                        row[5],
                        row[5],
                        row[7],
                        row[6],
                        row[8],
                        row[9],
                        str(row[10] or ""),
                        row[11],
                        row[11],
                    ),
                )

            old_events = self._conn.execute(
                "SELECT id, pr_number, comment_path, comment_line, signal, actor, created_at "
                "FROM feedback_events"
            ).fetchall()
            kind_map = {"accepted": "unobserved", "rejected": "reply_disagree"}
            for row in old_events:
                kind = kind_map.get(row[4], row[4])
                matched_finding_id: str | None = None
                if row[4] in kind_map:
                    match = self._conn.execute(
                        "SELECT id FROM review_findings WHERE pr_number = ? AND path = ? "
                        "AND start_line = ? ORDER BY created_at DESC LIMIT 1",
                        (row[1], row[2], row[3]),
                    ).fetchone()
                    matched_finding_id = match[0] if match else None
                self._conn.execute(
                    "INSERT OR IGNORE INTO feedback_events_v2 "
                    "(finding_id, kind, actor, platform, source_event_id, "
                    "provenance_complete, audit_json, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        matched_finding_id,
                        kind,
                        row[5],
                        self._platform,
                        f"legacy-feedback:{row[0]}",
                        0,
                        '{"backfilled":true}',
                        row[6],
                    ),
                )
            self._conn.commit()
        except Exception as exc:
            self._conn.rollback()
            logger.warning("Feedback v2 backfill skipped: %s", exc)

    def record_feedback(
        self,
        pr_number: int,
        pr_url: str,
        comment_path: str,
        comment_line: int,
        comment_category: str,
        comment_severity: str,
        comment_title: str,
        signal: str,
        actor: str,
        pr_author: str = "",
    ) -> FeedbackEventRow:
        now = time.time()
        cur = self._conn.execute(
            "INSERT INTO feedback_events "
            "(pr_number, pr_url, comment_path, comment_line, comment_category, "
            "comment_severity, comment_title, signal, actor, pr_author, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                pr_number,
                pr_url,
                comment_path,
                comment_line,
                comment_category,
                comment_severity,
                comment_title,
                signal,
                actor,
                pr_author,
                now,
            ),
        )
        self._conn.commit()
        row_id = cur.lastrowid
        if row_id is None:
            raise RuntimeError("INSERT into feedback_events did not return a row id")
        return FeedbackEventRow(
            id=row_id,
            pr_number=pr_number,
            pr_url=pr_url,
            comment_path=comment_path,
            comment_line=comment_line,
            comment_category=comment_category,
            comment_severity=comment_severity,
            comment_title=comment_title,
            signal=signal,
            actor=actor,
            pr_author=pr_author,
            created_at=now,
        )

    def record_bulk_feedback(self, events: list[dict]) -> int:
        """Insert multiple feedback events in a single transaction.

        Each dict must contain the same keys as record_feedback's parameters.
        Returns the number of rows inserted.
        """
        if not events:
            return 0
        now = time.time()
        rows = [
            (
                e["pr_number"],
                e["pr_url"],
                e["comment_path"],
                e["comment_line"],
                e["comment_category"],
                e["comment_severity"],
                e["comment_title"],
                e["signal"],
                e["actor"],
                e.get("pr_author", ""),
                now,
            )
            for e in events
        ]
        self._conn.executemany(
            "INSERT INTO feedback_events "
            "(pr_number, pr_url, comment_path, comment_line, comment_category, "
            "comment_severity, comment_title, signal, actor, pr_author, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            rows,
        )
        self._conn.commit()
        return len(rows)

    def list_feedback(self, limit: int = 500) -> list[FeedbackEventRow]:
        rows = self._conn.execute(
            "SELECT id, pr_number, pr_url, comment_path, comment_line, "
            "comment_category, comment_severity, comment_title, signal, actor, pr_author, created_at "
            "FROM feedback_events ORDER BY created_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [
            FeedbackEventRow(
                id=r[0],
                pr_number=r[1],
                pr_url=r[2],
                comment_path=r[3],
                comment_line=r[4],
                comment_category=r[5],
                comment_severity=r[6],
                comment_title=r[7],
                signal=r[8],
                actor=r[9],
                pr_author=r[10],
                created_at=r[11],
            )
            for r in rows
        ]

    def get_feedback_stats(self) -> dict:
        """Aggregate feedback counts by signal, category, and path directory."""
        rows = self._conn.execute(
            "SELECT signal, comment_category, comment_path, COUNT(*) "
            "FROM feedback_events GROUP BY signal, comment_category, comment_path"
        ).fetchall()
        stats: dict[str, dict[str, int]] = {}
        for signal, category, _path, count in rows:
            key = f"{signal}:{category}"
            stats.setdefault(key, {"total": 0})
            stats[key]["total"] += count
        return stats

    # ── Governed learning candidates ──

    _LC_COLS = (
        "id, semantic_fingerprint, rule_text, rationale, scope_type, scope_value, "
        "category, language, confidence, status, synthesizer_version, "
        "evidence_ids_json, positive_examples_json, negative_examples_json, "
        "source_finding_id, source_feedback_id, superseded_by_id, cost_tokens, "
        "created_at, updated_at"
    )

    @staticmethod
    def _row_to_learning_candidate(row: tuple) -> LearningCandidate:
        return LearningCandidate(
            id=row[0],
            semantic_fingerprint=row[1],
            rule_text=row[2],
            rationale=row[3],
            scope_type=row[4],
            scope_value=row[5],
            category=row[6],
            language=row[7],
            confidence=row[8],
            status=row[9],
            synthesizer_version=row[10],
            evidence_ids_json=row[11],
            positive_examples_json=row[12],
            negative_examples_json=row[13],
            source_finding_id=row[14],
            source_feedback_id=row[15],
            superseded_by_id=row[16],
            cost_tokens=row[17],
            created_at=row[18],
            updated_at=row[19],
        )

    @staticmethod
    def _merge_json_lists(current: str, incoming: str) -> str:
        merged: list[object] = []
        seen: set[str] = set()
        for raw in (current, incoming):
            try:
                values = json.loads(raw or "[]")
            except (TypeError, json.JSONDecodeError):
                values = []
            if not isinstance(values, list):
                continue
            for value in values:
                key = json.dumps(value, sort_keys=True)
                if key not in seen:
                    seen.add(key)
                    merged.append(value)
        return json.dumps(merged, sort_keys=True)

    def upsert_learning_candidate(
        self, candidate: LearningCandidate
    ) -> tuple[LearningCandidate, bool]:
        """Insert a proposal or merge equivalent evidence without changing governance."""
        now = time.time()
        candidate.created_at = candidate.created_at or now
        candidate.updated_at = now
        try:
            # Acquire the write lock before determining whether this identity
            # exists. Concurrent webhook workers then serialize instead of
            # racing through a SELECT followed by INSERT.
            self._conn.execute("BEGIN IMMEDIATE")
            cur = self._conn.execute(
                "INSERT OR IGNORE INTO learning_candidates "
                "(semantic_fingerprint, rule_text, rationale, scope_type, scope_value, "
                "category, language, confidence, status, synthesizer_version, "
                "evidence_ids_json, positive_examples_json, negative_examples_json, "
                "source_finding_id, source_feedback_id, superseded_by_id, cost_tokens, "
                "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, "
                "?, ?, ?, ?, ?)",
                (
                    candidate.semantic_fingerprint,
                    candidate.rule_text,
                    candidate.rationale,
                    candidate.scope_type,
                    candidate.scope_value,
                    candidate.category,
                    candidate.language,
                    candidate.confidence,
                    candidate.status,
                    candidate.synthesizer_version,
                    candidate.evidence_ids_json,
                    candidate.positive_examples_json,
                    candidate.negative_examples_json,
                    candidate.source_finding_id,
                    candidate.source_feedback_id,
                    candidate.superseded_by_id,
                    candidate.cost_tokens,
                    candidate.created_at,
                    candidate.updated_at,
                ),
            )
            if cur.rowcount == 1:
                row_id = cur.lastrowid
                if row_id is None:
                    raise RuntimeError("INSERT into learning_candidates did not return a row id")
                self._conn.commit()
                inserted = self.get_learning_candidate(row_id)
                if inserted is None:
                    raise RuntimeError("Inserted learning candidate could not be reloaded")
                return inserted, True

            existing = self._conn.execute(
                f"SELECT {self._LC_COLS} FROM learning_candidates "
                "WHERE semantic_fingerprint = ? AND scope_type = ? AND scope_value = ?",
                (candidate.semantic_fingerprint, candidate.scope_type, candidate.scope_value),
            ).fetchone()
            if existing is None:
                raise RuntimeError("Candidate conflict did not return the existing row")
            current = self._row_to_learning_candidate(existing)
            evidence_json = self._merge_json_lists(
                current.evidence_ids_json, candidate.evidence_ids_json
            )
            positives_json = self._merge_json_lists(
                current.positive_examples_json, candidate.positive_examples_json
            )
            negatives_json = self._merge_json_lists(
                current.negative_examples_json, candidate.negative_examples_json
            )
            self._conn.execute(
                "UPDATE learning_candidates SET rule_text = ?, rationale = ?, category = ?, "
                "language = ?, confidence = ?, evidence_ids_json = ?, "
                "positive_examples_json = ?, negative_examples_json = ?, cost_tokens = ?, "
                "updated_at = ? WHERE id = ?",
                (
                    candidate.rule_text or current.rule_text,
                    candidate.rationale or current.rationale,
                    candidate.category or current.category,
                    candidate.language or current.language,
                    max(current.confidence, candidate.confidence),
                    evidence_json,
                    positives_json,
                    negatives_json,
                    current.cost_tokens + candidate.cost_tokens,
                    now,
                    current.id,
                ),
            )
            self._conn.commit()
        except Exception:
            self._conn.rollback()
            raise
        merged = self.get_learning_candidate(current.id)
        if merged is None:
            raise RuntimeError("Merged learning candidate could not be reloaded")
        return merged, False

    def get_learning_candidate(self, candidate_id: int) -> LearningCandidate | None:
        row = self._conn.execute(
            f"SELECT {self._LC_COLS} FROM learning_candidates WHERE id = ?",
            (candidate_id,),
        ).fetchone()
        return self._row_to_learning_candidate(row) if row else None

    def list_learning_candidates(
        self, status: str | None = None, limit: int = 500
    ) -> list[LearningCandidate]:
        if status:
            rows = self._conn.execute(
                f"SELECT {self._LC_COLS} FROM learning_candidates WHERE status = ? "
                "ORDER BY updated_at DESC LIMIT ?",
                (status, limit),
            ).fetchall()
        else:
            rows = self._conn.execute(
                f"SELECT {self._LC_COLS} FROM learning_candidates ORDER BY updated_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [self._row_to_learning_candidate(row) for row in rows]

    def update_learning_candidate(
        self,
        candidate_id: int,
        *,
        rule_text: str,
        rationale: str,
        scope_type: str,
        scope_value: str,
        category: str,
        language: str,
        semantic_fingerprint: str,
    ) -> None:
        self._conn.execute(
            "UPDATE learning_candidates SET rule_text = ?, rationale = ?, scope_type = ?, "
            "scope_value = ?, category = ?, language = ?, semantic_fingerprint = ?, "
            "updated_at = ? WHERE id = ?",
            (
                rule_text,
                rationale,
                scope_type,
                scope_value,
                category,
                language,
                semantic_fingerprint,
                time.time(),
                candidate_id,
            ),
        )
        self._conn.commit()

    def set_learning_candidate_status(
        self, candidate_id: int, status: str, superseded_by_id: int | None = None
    ) -> None:
        self._conn.execute(
            "UPDATE learning_candidates SET status = ?, superseded_by_id = ?, "
            "updated_at = ? WHERE id = ?",
            (status, superseded_by_id, time.time(), candidate_id),
        )
        self._conn.commit()

    def approve_learning_candidate_atomic(
        self, candidate_id: int, *, actor: str, min_evidence: int
    ) -> LearnedRuleRow:
        """Approve a candidate and create its rule in one write transaction."""
        now = time.time()
        try:
            self._conn.execute("BEGIN IMMEDIATE")
            row = self._conn.execute(
                f"SELECT {self._LC_COLS} FROM learning_candidates WHERE id = ?",
                (candidate_id,),
            ).fetchone()
            if row is None:
                raise LookupError("Learning candidate not found")
            candidate = self._row_to_learning_candidate(row)

            existing = self._conn.execute(
                f"SELECT {self._LR_COLS} FROM learned_rules "
                "WHERE origin_candidate_id = ? AND status = 'approved' "
                "ORDER BY id LIMIT 1",
                (candidate_id,),
            ).fetchone()
            if existing is not None:
                self._conn.execute(
                    "UPDATE learning_candidates SET status = 'approved', updated_at = ? "
                    "WHERE id = ?",
                    (now, candidate_id),
                )
                self._conn.commit()
                return self._row_to_learned_rule(existing)

            if candidate.status == "approved":
                raise ValueError("Approved candidate has no active rule")
            if candidate.status not in {"collecting", "pending"}:
                raise ValueError(f"Cannot approve candidate in state '{candidate.status}'")
            if not candidate.rule_text.strip():
                raise ValueError("rule_text is required")
            if not candidate.scope_value.strip():
                raise ValueError("scope_value is required")
            if candidate.evidence_count < min_evidence:
                raise ValueError(
                    f"Scope '{candidate.scope_type}' requires at least "
                    f"{min_evidence} evidence events"
                )

            cur = self._conn.execute(
                "INSERT OR IGNORE INTO learned_rules "
                "(rule_text, source_signal, category, path_pattern, sample_count, active, "
                "status, created_by, version, scope_type, scope_value, origin_candidate_id, "
                "rationale, evidence_count, effective_from, disabled_at, "
                "supersedes_rule_id, semantic_fingerprint, created_at, updated_at) "
                "VALUES (?, 'feedback_v2', ?, ?, ?, 1, 'approved', ?, 1, ?, ?, ?, ?, ?, ?, "
                "NULL, NULL, ?, ?, ?)",
                (
                    candidate.rule_text,
                    candidate.category,
                    candidate.scope_value if candidate.scope_type == "path" else "",
                    candidate.evidence_count,
                    actor,
                    candidate.scope_type,
                    candidate.scope_value,
                    candidate.id,
                    candidate.rationale,
                    candidate.evidence_count,
                    now,
                    candidate.semantic_fingerprint,
                    now,
                    now,
                ),
            )
            rule_id = cur.lastrowid if cur.rowcount == 1 else None
            if rule_id is None:
                conflict = self._conn.execute(
                    "SELECT id FROM learned_rules WHERE origin_candidate_id = ? "
                    "AND status = 'approved' ORDER BY id LIMIT 1",
                    (candidate_id,),
                ).fetchone()
                if conflict is None:
                    raise RuntimeError("Candidate approval conflicted without an approved rule")
                rule_id = conflict[0]
            self._conn.execute(
                "UPDATE learning_candidates SET status = 'approved', updated_at = ? WHERE id = ?",
                (now, candidate_id),
            )
            self._conn.commit()
        except Exception:
            self._conn.rollback()
            raise
        approved = self.get_learned_rule(rule_id)
        if approved is None:
            raise RuntimeError("Approved learning rule could not be reloaded")
        return approved

    def upsert_learned_rule(
        self,
        rule_text: str,
        source_signal: str,
        category: str,
        path_pattern: str,
        sample_count: int,
        status: str = "pending",
    ) -> LearnedRuleRow:
        now = time.time()
        existing = self._conn.execute(
            "SELECT id, status FROM learned_rules WHERE category = ? AND path_pattern = ?",
            (category, path_pattern),
        ).fetchone()
        if existing:
            # Keep the existing approval status — re-synthesis (more samples)
            # must never silently re-activate a rejected rule or auto-approve.
            self._conn.execute(
                "UPDATE learned_rules SET rule_text = ?, source_signal = ?, "
                "sample_count = ?, evidence_count = ?, scope_type = ?, scope_value = ?, "
                "updated_at = ? WHERE id = ?",
                (
                    rule_text,
                    source_signal,
                    sample_count,
                    sample_count,
                    "path" if path_pattern else "repo",
                    path_pattern,
                    now,
                    existing[0],
                ),
            )
            self._conn.commit()
            return LearnedRuleRow(
                id=existing[0],
                rule_text=rule_text,
                source_signal=source_signal,
                category=category,
                path_pattern=path_pattern,
                sample_count=sample_count,
                status=existing[1],
                created_at=now,
                updated_at=now,
            )
        cur = self._conn.execute(
            "INSERT INTO learned_rules "
            "(rule_text, source_signal, category, path_pattern, sample_count, "
            "active, status, scope_type, scope_value, evidence_count, effective_from, "
            "created_at, updated_at) VALUES (?, ?, ?, ?, ?, 1, ?, ?, ?, ?, ?, ?, ?)",
            (
                rule_text,
                source_signal,
                category,
                path_pattern,
                sample_count,
                status,
                "path" if path_pattern else "repo",
                path_pattern,
                sample_count,
                now if status == "approved" else 0.0,
                now,
                now,
            ),
        )
        self._conn.commit()
        row_id = cur.lastrowid
        if row_id is None:
            raise RuntimeError("INSERT into learned_rules did not return a row id")
        return LearnedRuleRow(
            id=row_id,
            rule_text=rule_text,
            source_signal=source_signal,
            category=category,
            path_pattern=path_pattern,
            sample_count=sample_count,
            status=status,
            created_at=now,
            updated_at=now,
        )

    @staticmethod
    def _row_to_learned_rule(r: tuple) -> LearnedRuleRow:
        return LearnedRuleRow(
            id=r[0],
            rule_text=r[1],
            source_signal=r[2],
            category=r[3],
            path_pattern=r[4],
            sample_count=r[5],
            active=bool(r[6]),
            status=r[7],
            created_by=r[8],
            version=r[9],
            scope_type=r[10],
            scope_value=r[11],
            origin_candidate_id=r[12],
            rationale=r[13],
            evidence_count=r[14],
            effective_from=r[15],
            disabled_at=r[16],
            supersedes_rule_id=r[17],
            semantic_fingerprint=r[18],
            created_at=r[19],
            updated_at=r[20],
        )

    _LR_COLS = (
        "id, rule_text, source_signal, category, path_pattern, "
        "sample_count, active, status, created_by, version, scope_type, scope_value, "
        "origin_candidate_id, rationale, evidence_count, effective_from, disabled_at, "
        "supersedes_rule_id, semantic_fingerprint, created_at, updated_at"
    )

    def list_active_learned_rules(self) -> list[LearnedRuleRow]:
        # Only approved + enabled rules feed reviews.
        rows = self._conn.execute(
            f"SELECT {self._LR_COLS} FROM learned_rules "
            "WHERE active = 1 AND status = 'approved' ORDER BY sample_count DESC"
        ).fetchall()
        rules = [self._row_to_learned_rule(r) for r in rows]

        # SQLite keeps one database per repository. Pull only organization-
        # scoped rules from sibling databases so broader rules behave the same
        # way as the shared PostgreSQL backend.
        if not self._owner or not self._repo:
            return rules
        rules.extend(
            _list_active_org_rules_for_repo_sqlite(
                platform=self._platform,
                owner=self._owner,
                exclude_repo=self._repo,
            )
        )
        return rules

    def list_learned_rules(self, status: str | None = None) -> list[LearnedRuleRow]:
        """List learned rules, optionally filtered by approval status."""
        if status:
            rows = self._conn.execute(
                f"SELECT {self._LR_COLS} FROM learned_rules WHERE status = ? "
                "ORDER BY updated_at DESC",
                (status,),
            ).fetchall()
        else:
            rows = self._conn.execute(
                f"SELECT {self._LR_COLS} FROM learned_rules ORDER BY updated_at DESC"
            ).fetchall()
        return [self._row_to_learned_rule(r) for r in rows]

    def get_learned_rule(self, rule_id: int) -> LearnedRuleRow | None:
        row = self._conn.execute(
            f"SELECT {self._LR_COLS} FROM learned_rules WHERE id = ?", (rule_id,)
        ).fetchone()
        return self._row_to_learned_rule(row) if row else None

    def create_learned_rule(
        self,
        rule_text: str,
        category: str,
        path_pattern: str = "",
        source_signal: str = "manual",
        status: str = "approved",
        active: bool = True,
        created_by: str = "",
        version: int = 1,
        scope_type: str = "repo",
        scope_value: str = "",
        origin_candidate_id: int | None = None,
        rationale: str = "",
        evidence_count: int = 0,
        supersedes_rule_id: int | None = None,
        semantic_fingerprint: str = "",
    ) -> LearnedRuleRow:
        """Insert an admin-authored rule (not deduped against existing)."""
        now = time.time()
        cur = self._conn.execute(
            "INSERT INTO learned_rules "
            "(rule_text, source_signal, category, path_pattern, sample_count, "
            "active, status, created_by, version, scope_type, scope_value, "
            "origin_candidate_id, rationale, evidence_count, effective_from, disabled_at, "
            "supersedes_rule_id, semantic_fingerprint, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                rule_text,
                source_signal,
                category,
                path_pattern,
                evidence_count,
                int(active),
                status,
                created_by,
                version,
                scope_type,
                scope_value,
                origin_candidate_id,
                rationale,
                evidence_count,
                now if status == "approved" and active else 0.0,
                None if active else now,
                supersedes_rule_id,
                semantic_fingerprint,
                now,
                now,
            ),
        )
        self._conn.commit()
        row_id = cur.lastrowid
        if row_id is None:
            raise RuntimeError("INSERT into learned_rules did not return a row id")
        return self.get_learned_rule(row_id)  # type: ignore[return-value]

    def version_learned_rule_atomic(
        self,
        rule_id: int,
        *,
        rule_text: str,
        category: str,
        scope_type: str,
        scope_value: str,
        rationale: str,
        actor: str,
        semantic_fingerprint: str,
    ) -> LearnedRuleRow:
        """Create one successor and supersede its prior version atomically."""
        now = time.time()
        try:
            self._conn.execute("BEGIN IMMEDIATE")
            previous_row = self._conn.execute(
                f"SELECT {self._LR_COLS} FROM learned_rules WHERE id = ?", (rule_id,)
            ).fetchone()
            if previous_row is None:
                raise LookupError("Learning not found")
            previous = self._row_to_learned_rule(previous_row)
            existing_row = self._conn.execute(
                f"SELECT {self._LR_COLS} FROM learned_rules "
                "WHERE supersedes_rule_id = ? AND status = 'approved' ORDER BY id LIMIT 1",
                (rule_id,),
            ).fetchone()
            if existing_row is not None:
                existing = self._row_to_learned_rule(existing_row)
                if existing.semantic_fingerprint != semantic_fingerprint:
                    raise ValueError("Learning was already versioned with different content")
                self._conn.execute(
                    "UPDATE learned_rules SET active = 0, status = 'superseded', "
                    "disabled_at = COALESCE(disabled_at, ?), updated_at = ? WHERE id = ?",
                    (now, now, rule_id),
                )
                self._conn.commit()
                return existing
            if previous.status != "approved":
                raise ValueError("Only approved rules can be versioned")

            cur = self._conn.execute(
                "INSERT OR IGNORE INTO learned_rules "
                "(rule_text, source_signal, category, path_pattern, sample_count, active, "
                "status, created_by, version, scope_type, scope_value, origin_candidate_id, "
                "rationale, evidence_count, effective_from, disabled_at, "
                "supersedes_rule_id, semantic_fingerprint, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, 1, 'approved', ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?, ?, ?)",
                (
                    rule_text,
                    previous.source_signal,
                    category,
                    scope_value if scope_type == "path" else "",
                    previous.evidence_count,
                    actor or previous.created_by,
                    previous.version + 1,
                    scope_type,
                    scope_value,
                    previous.origin_candidate_id,
                    rationale or previous.rationale,
                    previous.evidence_count,
                    now,
                    previous.id,
                    semantic_fingerprint,
                    now,
                    now,
                ),
            )
            replacement_id = cur.lastrowid if cur.rowcount == 1 else None
            if replacement_id is None:
                conflict = self._conn.execute(
                    "SELECT id, semantic_fingerprint FROM learned_rules "
                    "WHERE supersedes_rule_id = ? AND status = 'approved' "
                    "ORDER BY id LIMIT 1",
                    (rule_id,),
                ).fetchone()
                if conflict is None:
                    raise RuntimeError("Rule version conflicted without a successor")
                if conflict[1] != semantic_fingerprint:
                    raise ValueError("Learning was already versioned with different content")
                replacement_id = conflict[0]
            self._conn.execute(
                "UPDATE learned_rules SET active = 0, status = 'superseded', "
                "disabled_at = ?, updated_at = ? WHERE id = ?",
                (now, now, rule_id),
            )
            self._conn.commit()
        except Exception:
            self._conn.rollback()
            raise
        replacement = self.get_learned_rule(replacement_id)
        if replacement is None:
            raise RuntimeError("Versioned learning rule could not be reloaded")
        return replacement

    def update_learned_rule(
        self,
        rule_id: int,
        rule_text: str,
        category: str,
        path_pattern: str,
        *,
        scope_type: str,
        scope_value: str,
        rationale: str,
        semantic_fingerprint: str,
    ) -> None:
        self._conn.execute(
            "UPDATE learned_rules SET rule_text = ?, category = ?, path_pattern = ?, "
            "scope_type = ?, scope_value = ?, rationale = ?, semantic_fingerprint = ?, "
            "updated_at = ? WHERE id = ?",
            (
                rule_text,
                category,
                path_pattern,
                scope_type,
                scope_value,
                rationale,
                semantic_fingerprint,
                time.time(),
                rule_id,
            ),
        )
        self._conn.commit()

    def set_learned_rule_status(self, rule_id: int, status: str) -> None:
        now = time.time()
        self._conn.execute(
            "UPDATE learned_rules SET status = ?, effective_from = CASE "
            "WHEN ? = 'approved' AND effective_from = 0 THEN ? ELSE effective_from END, "
            "updated_at = ? WHERE id = ?",
            (status, status, now, now, rule_id),
        )
        self._conn.commit()

    def set_learned_rule_active(self, rule_id: int, active: bool) -> None:
        self._conn.execute(
            "UPDATE learned_rules SET active = ?, disabled_at = ?, updated_at = ? WHERE id = ?",
            (int(active), None if active else time.time(), time.time(), rule_id),
        )
        self._conn.commit()

    def supersede_learned_rule(self, rule_id: int, replacement_id: int) -> None:
        now = time.time()
        self._conn.execute(
            "UPDATE learned_rules SET active = 0, status = 'superseded', disabled_at = ?, "
            "updated_at = ? WHERE id = ?",
            (now, now, rule_id),
        )
        self._conn.execute(
            "UPDATE learned_rules SET supersedes_rule_id = ?, updated_at = ? WHERE id = ?",
            (rule_id, now, replacement_id),
        )
        self._conn.commit()

    def delete_learned_rule(self, rule_id: int) -> None:
        self._conn.execute("DELETE FROM learned_rules WHERE id = ?", (rule_id,))
        self._conn.commit()

    # ---------------------------------------------------------------- Phase 3

    def _analytics_fetchall(self, sql: str, params: tuple) -> list[tuple]:
        return self._conn.execute(sql, params).fetchall()

    # ---------------------------------------------------------------- Phase 4
    # The two primitives `GateStoreMixin` needs. Everything else about gate
    # persistence is written once, in that mixin, for both backends.

    def _gate_query(self, sql: str, params: tuple = ()) -> list[tuple]:
        return self._conn.execute(sql, params).fetchall()

    def _gate_exec(self, sql: str, params: tuple = ()) -> int:
        cursor = self._conn.execute(sql, params)
        self._conn.commit()
        return int(cursor.rowcount or 0)

    def record_rule_evaluations(self, evaluations: list[RuleEvaluation]) -> int:
        """Persist rule exposures idempotently; return how many were new.

        `INSERT OR IGNORE` against the unique `evaluation_key` makes a retried
        review round a no-op instead of a double-count, and makes two workers
        racing on the same review safe without a lock.
        """
        if not evaluations:
            return 0
        now = time.time()
        rows = [
            (
                evaluation.evaluation_key,
                evaluation.review_id,
                evaluation.rule_id,
                evaluation.rule_version,
                evaluation.rule_origin,
                evaluation.scope_type,
                evaluation.scope_value,
                evaluation.category,
                evaluation.decision,
                evaluation.finding_id,
                evaluation.platform,
                evaluation.owner or self._owner,
                evaluation.repo or self._repo,
                evaluation.pr_number,
                evaluation.pr_author,
                evaluation.head_sha,
                evaluation.detail_json,
                evaluation.created_at or now,
            )
            for evaluation in evaluations
        ]
        cur = self._conn.executemany(
            "INSERT OR IGNORE INTO rule_evaluations "
            "(evaluation_key, review_id, rule_id, rule_version, rule_origin, scope_type, "
            "scope_value, category, decision, finding_id, platform, owner, repo, pr_number, "
            "pr_author, head_sha, detail_json, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            rows,
        )
        self._conn.commit()
        return int(cur.rowcount or 0)

    def link_rule_evaluations(self, evaluation_keys: list[str], review_id: int) -> None:
        """Attach evaluations to their review once the review row exists.

        Only fills in a missing link; an evaluation already tied to a review is
        left alone so a retry cannot re-point history at a newer review row.
        """
        if not evaluation_keys or not review_id:
            return
        self._conn.executemany(
            "UPDATE rule_evaluations SET review_id = ? WHERE evaluation_key = ? AND review_id = 0",
            [(review_id, key) for key in evaluation_keys],
        )
        self._conn.commit()

    def record_learning_audit_event(
        self,
        *,
        event_type: str,
        rule_id: int = 0,
        actor: str = "",
        summary: str = "",
        detail: dict | None = None,
    ) -> int:
        cur = self._conn.execute(
            "INSERT INTO learning_audit_events "
            "(event_type, rule_id, actor, summary, detail_json, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                event_type,
                rule_id,
                actor,
                summary,
                json.dumps(detail or {}, sort_keys=True),
                time.time(),
            ),
        )
        self._conn.commit()
        return int(cur.lastrowid or 0)

    def list_learning_audit_events(
        self, *, rule_id: int = 0, limit: int = 100, offset: int = 0
    ) -> list[dict]:
        where, params = ("WHERE rule_id = ?", [rule_id]) if rule_id else ("", [])
        rows = self._conn.execute(
            "SELECT id, event_type, rule_id, actor, summary, detail_json, created_at "
            f"FROM learning_audit_events {where} ORDER BY created_at DESC, id DESC "
            "LIMIT ? OFFSET ?",
            (*params, limit, offset),
        ).fetchall()
        return [
            {
                "id": r[0],
                "event_type": r[1],
                "rule_id": r[2],
                "actor": r[3],
                "summary": r[4],
                "detail_json": r[5],
                "created_at": r[6],
                "owner": self._owner,
                "repo": self._repo,
            }
            for r in rows
        ]

    def replace_manifest_packages(
        self,
        file_path: str,
        packages: list[dict],
    ) -> int:
        """Replace all package entries for a manifest file atomically.

        Called after an indexing pass re-reads the manifest; we want the DB
        to exactly mirror the file. Returns the number of rows inserted.
        """
        now = time.time()
        self._conn.execute(
            "DELETE FROM package_manifests WHERE file_path = ?",
            (file_path,),
        )
        if packages:
            rows = [
                (
                    p["name"],
                    p["kind"],
                    p["version"],
                    p["file_path"],
                    1 if p.get("is_dev") else 0,
                    now,
                )
                for p in packages
            ]
            self._conn.executemany(
                "INSERT OR REPLACE INTO package_manifests "
                "(name, kind, version, file_path, is_dev, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                rows,
            )
        self._conn.commit()
        return len(packages)

    def list_manifest_packages(self) -> list[PackageManifestRow]:
        rows = self._conn.execute(
            "SELECT id, name, kind, version, file_path, is_dev, updated_at "
            "FROM package_manifests ORDER BY name COLLATE NOCASE"
        ).fetchall()
        return [
            PackageManifestRow(
                id=r[0],
                name=r[1],
                kind=r[2],
                version=r[3],
                file_path=r[4],
                is_dev=bool(r[5]),
                updated_at=r[6],
            )
            for r in rows
        ]

    def clear_manifest_packages_for_missing_files(self, live_paths: set[str]) -> int:
        """Drop entries for manifest files that no longer exist in the repo.

        Called during indexing when we've finished the manifest pass and know
        which manifest file paths are still present.
        """
        existing = {
            r[0]
            for r in self._conn.execute(
                "SELECT DISTINCT file_path FROM package_manifests"
            ).fetchall()
        }
        stale = existing - live_paths
        if not stale:
            return 0
        self._conn.executemany(
            "DELETE FROM package_manifests WHERE file_path = ?",
            [(p,) for p in stale],
        )
        self._conn.commit()
        return len(stale)

    def replace_vulnerabilities_for_package(
        self,
        package_name: str,
        ecosystem: str,
        package_version: str,
        vulns: list[dict],
    ) -> int:
        """Atomically replace the vulnerability rows for a single (package,
        ecosystem, version). Called after each OSV poll for that combination.

        Each dict must have keys cve_id, summary, severity, advisory_url, fixed_in.
        Empty list clears all vulns for that combination (i.e. package no
        longer affected).
        """
        now = time.time()
        existing = {
            r[0]: r[1]  # cve_id → first_seen_at
            for r in self._conn.execute(
                "SELECT cve_id, first_seen_at FROM vulnerabilities "
                "WHERE package_name = ? AND ecosystem = ? AND package_version = ?",
                (package_name, ecosystem, package_version),
            ).fetchall()
        }
        self._conn.execute(
            "DELETE FROM vulnerabilities "
            "WHERE package_name = ? AND ecosystem = ? AND package_version = ?",
            (package_name, ecosystem, package_version),
        )
        if vulns:
            rows = [
                (
                    package_name,
                    ecosystem,
                    package_version,
                    v["cve_id"],
                    v.get("summary", ""),
                    v.get("severity", "unknown"),
                    v.get("advisory_url", ""),
                    v.get("fixed_in", ""),
                    existing.get(v["cve_id"], now),  # preserve first_seen_at
                    now,
                )
                for v in vulns
            ]
            self._conn.executemany(
                "INSERT INTO vulnerabilities "
                "(package_name, ecosystem, package_version, cve_id, summary, "
                "severity, advisory_url, fixed_in, first_seen_at, last_seen_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                rows,
            )
        self._conn.commit()
        return len(vulns)

    def prune_stale_vulnerabilities(self, active_keys: set[tuple[str, str, str]]) -> int:
        """Delete vulnerability rows whose (name, ecosystem, version) tuple
        is no longer in this repo's dependency set.

        Called by the OSV poller before each scan so stale advisories from
        previous package versions (e.g. `litellm 1.30` after `uv.lock`
        resolves to `1.81.10`) don't linger.
        """
        rows = self._conn.execute(
            "SELECT DISTINCT package_name, ecosystem, package_version FROM vulnerabilities"
        ).fetchall()
        stale = [(n, e, v) for n, e, v in rows if (n, e, v) not in active_keys]
        if not stale:
            return 0
        self._conn.executemany(
            "DELETE FROM vulnerabilities WHERE package_name=? AND ecosystem=? AND package_version=?",
            stale,
        )
        self._conn.commit()
        return len(stale)

    def list_vulnerabilities(self) -> list[VulnerabilityRow]:
        rows = self._conn.execute(
            "SELECT id, package_name, ecosystem, package_version, cve_id, "
            "summary, severity, advisory_url, fixed_in, first_seen_at, last_seen_at "
            "FROM vulnerabilities "
            "ORDER BY CASE severity "
            "WHEN 'critical' THEN 0 WHEN 'high' THEN 1 "
            "WHEN 'moderate' THEN 2 WHEN 'low' THEN 3 ELSE 4 END, "
            "package_name COLLATE NOCASE"
        ).fetchall()
        return [
            VulnerabilityRow(
                id=r[0],
                package_name=r[1],
                ecosystem=r[2],
                package_version=r[3],
                cve_id=r[4],
                summary=r[5],
                severity=r[6],
                advisory_url=r[7],
                fixed_in=r[8],
                first_seen_at=r[9],
                last_seen_at=r[10],
            )
            for r in rows
        ]

    def count_vulnerabilities_by_severity(self) -> dict[str, int]:
        rows = self._conn.execute(
            "SELECT severity, COUNT(*) FROM vulnerabilities GROUP BY severity"
        ).fetchall()
        return {r[0]: r[1] for r in rows}


# Org-wide aggregation over per-repo SQLite stores. Mirrors the Postgres
# helpers in pg_store.py for self-host setups where each repo is a
# separate SQLite file under MIRA_INDEX_DIR.


def _iter_repo_dbs(index_dir: str) -> list[tuple[str, str, str, str]]:
    """Yield (platform, owner, repo, db_path) for each repo SQLite file.

    GitHub repos live flat at ``{owner}/{repo}.db`` (back-compat); other
    platforms are namespaced under ``_{platform}/`` and may have nested-group
    owners, so those are walked recursively.
    """
    out: list[tuple[str, str, str, str]] = []
    if not os.path.isdir(index_dir):
        return out
    for entry in sorted(os.listdir(index_dir)):
        entry_path = os.path.join(index_dir, entry)
        if not os.path.isdir(entry_path) or entry.startswith("."):
            continue
        if entry.startswith("_"):
            platform = entry[1:]
            for root, _dirs, files in os.walk(entry_path):
                for fname in sorted(files):
                    if not fname.endswith(".db"):
                        continue
                    rel = os.path.relpath(os.path.join(root, fname), entry_path)
                    # Provider repository names are platform-independent and
                    # always use '/'. Normalize Windows' path separator before
                    # returning nested GitLab/Forgejo owners.
                    owner = os.path.dirname(rel).replace(os.sep, "/")
                    out.append((platform, owner, fname[:-3], os.path.join(root, fname)))
            continue
        for fname in sorted(os.listdir(entry_path)):
            if fname.endswith(".db"):
                out.append(("github", entry, fname[:-3], os.path.join(entry_path, fname)))
    return out


def list_packages_org_wide_sqlite() -> list[dict]:
    """SQLite equivalent of pg_store.list_packages_org_wide."""
    index_dir = os.environ.get("MIRA_INDEX_DIR", _INDEX_DIR)
    out: list[dict] = []
    for platform, owner, repo, db_path in _iter_repo_dbs(index_dir):
        try:
            conn = sqlite3.connect(db_path)
            try:
                rows = conn.execute(
                    "SELECT DISTINCT kind, name, version, file_path FROM package_manifests "
                    "WHERE kind IN ('npm', 'pip', 'go', 'rust', 'composer') AND version != ''"
                ).fetchall()
            finally:
                conn.close()
        except sqlite3.Error:
            continue
        for kind, name, version, file_path in rows:
            out.append(
                {
                    "platform": platform,
                    "owner": owner,
                    "repo": repo,
                    "kind": kind,
                    "name": name,
                    "version": version,
                    "file_path": file_path,
                }
            )
    return out


def search_packages_org_wide_sqlite(
    name: str | None = None,
    version: str | None = None,
    kind: str | None = None,
    is_dev: bool | None = None,
    limit: int = 500,
) -> list[dict]:
    """SQLite equivalent of pg_store.search_packages_org_wide."""
    index_dir = os.environ.get("MIRA_INDEX_DIR", _INDEX_DIR)
    name_l = name.lower() if name else None
    version_l = version.lower() if version else None

    rows: list[dict] = []
    for platform, owner, repo, db_path in _iter_repo_dbs(index_dir):
        try:
            conn = sqlite3.connect(db_path)
            try:
                cur = conn.execute(
                    "SELECT name, kind, version, file_path, is_dev FROM package_manifests"
                )
                for r_name, r_kind, r_version, r_file, r_dev in cur.fetchall():
                    if name_l and name_l not in r_name.lower():
                        continue
                    if version_l and version_l not in r_version.lower():
                        continue
                    if kind and r_kind != kind:
                        continue
                    if is_dev is not None and bool(r_dev) != is_dev:
                        continue
                    rows.append(
                        {
                            "platform": platform,
                            "owner": owner,
                            "repo": repo,
                            "name": r_name,
                            "kind": r_kind,
                            "version": r_version,
                            "file_path": r_file,
                            "is_dev": bool(r_dev),
                        }
                    )
            finally:
                conn.close()
        except sqlite3.Error:
            continue

    rows.sort(key=lambda r: (r["name"].lower(), r["owner"], r["repo"]))
    return rows[:limit]


def list_vulnerabilities_org_wide_sqlite(limit: int = 1000) -> list[dict]:
    """SQLite equivalent of pg_store.list_vulnerabilities_org_wide."""
    index_dir = os.environ.get("MIRA_INDEX_DIR", _INDEX_DIR)
    severity_order = {"critical": 0, "high": 1, "moderate": 2, "low": 3}
    rows: list[dict] = []
    for platform, owner, repo, db_path in _iter_repo_dbs(index_dir):
        try:
            conn = sqlite3.connect(db_path)
            try:
                cur = conn.execute(
                    "SELECT package_name, ecosystem, package_version, cve_id, summary, "
                    "severity, advisory_url, fixed_in, last_seen_at FROM vulnerabilities"
                )
                for r in cur.fetchall():
                    rows.append(
                        {
                            "platform": platform,
                            "owner": owner,
                            "repo": repo,
                            "package_name": r[0],
                            "ecosystem": r[1],
                            "package_version": r[2],
                            "cve_id": r[3],
                            "summary": r[4],
                            "severity": r[5],
                            "advisory_url": r[6],
                            "fixed_in": r[7],
                            "last_seen_at": r[8],
                        }
                    )
            finally:
                conn.close()
        except sqlite3.Error:
            continue

    rows.sort(key=lambda r: (severity_order.get(r["severity"], 4), r["package_name"].lower()))
    return rows[:limit]


def list_learning_candidates_org_wide_sqlite(
    limit: int = 500, status: str | None = None
) -> list[dict]:
    """List governed candidates across per-repository SQLite databases."""
    index_dir = os.environ.get("MIRA_INDEX_DIR", _INDEX_DIR)
    rows: list[dict] = []
    for platform, owner, repo, db_path in _iter_repo_dbs(index_dir):
        try:
            conn = sqlite3.connect(db_path)
            try:
                tables = {
                    row[0]
                    for row in conn.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'"
                    ).fetchall()
                }
                if "learning_candidates" not in tables:
                    continue
                where = ""
                params: tuple[object, ...] = ()
                if status:
                    where, params = " WHERE status = ?", (status,)
                query = (
                    "SELECT id, semantic_fingerprint, rule_text, rationale, scope_type, "
                    "scope_value, category, language, confidence, status, "
                    "synthesizer_version, evidence_ids_json, positive_examples_json, "
                    "negative_examples_json, source_finding_id, source_feedback_id, "
                    "superseded_by_id, cost_tokens, created_at, updated_at "
                    f"FROM learning_candidates{where} ORDER BY updated_at DESC"
                )
                for row in conn.execute(query, params).fetchall():
                    candidate = IndexStore._row_to_learning_candidate(row)
                    rows.append(
                        {
                            **candidate.__dict__,
                            "evidence_count": candidate.evidence_count,
                            "platform": platform,
                            "owner": owner,
                            "repo": repo,
                        }
                    )
            finally:
                conn.close()
        except sqlite3.Error:
            continue
    rows.sort(key=lambda row: -(row["updated_at"] or 0.0))
    return rows[:limit]


def _list_active_org_rules_for_repo_sqlite(
    *, platform: str, owner: str, exclude_repo: str
) -> list[LearnedRuleRow]:
    """Read only applicable org rules from sibling SQLite repositories.

    Filtering happens inside each database before any dashboard pagination is
    applied, so a busy installation cannot evict a valid organization rule
    behind thousands of unrelated recent rows.
    """
    index_dir = os.environ.get("MIRA_INDEX_DIR", _INDEX_DIR)
    rules: list[LearnedRuleRow] = []
    for db_platform, db_owner, db_repo, db_path in _iter_repo_dbs(index_dir):
        if db_platform != platform or db_owner != owner or db_repo == exclude_repo:
            continue
        try:
            conn = sqlite3.connect(db_path)
            try:
                cols = {
                    row[1] for row in conn.execute("PRAGMA table_info(learned_rules)").fetchall()
                }
                # Pre-governance databases cannot contain organization-scoped
                # rules, so there is nothing applicable to retrieve from them.
                if not {"scope_type", "status"}.issubset(cols):
                    continue
                rows = conn.execute(
                    f"SELECT {IndexStore._LR_COLS} FROM learned_rules "
                    "WHERE active = 1 AND status = 'approved' AND scope_type = 'org' "
                    "ORDER BY sample_count DESC"
                ).fetchall()
                rules.extend(IndexStore._row_to_learned_rule(row) for row in rows)
            finally:
                conn.close()
        except sqlite3.Error:
            continue
    return rules


def list_learned_rules_org_wide_sqlite(limit: int = 500, status: str | None = None) -> list[dict]:
    """SQLite equivalent of pg_store.list_learned_rules_org_wide.

    Returns every rule with its id/active/status (optionally filtered by
    status) so the dashboard can approve/reject and CRUD specific rules.
    """
    index_dir = os.environ.get("MIRA_INDEX_DIR", _INDEX_DIR)
    rows: list[dict] = []
    for platform, owner, repo, db_path in _iter_repo_dbs(index_dir):
        try:
            conn = sqlite3.connect(db_path)
            try:
                cols = {r[1] for r in conn.execute("PRAGMA table_info(learned_rules)").fetchall()}
                has_status = "status" in cols
                # Pre-migration DBs have no status column → treat all as approved.
                if status and not has_status and status != "approved":
                    continue
                status_sel = "status" if has_status else "'approved'"
                created_by_sel = "created_by" if "created_by" in cols else "''"
                version_sel = "version" if "version" in cols else "1"
                scope_type_sel = (
                    "scope_type"
                    if "scope_type" in cols
                    else "CASE WHEN path_pattern <> '' THEN 'path' ELSE 'repo' END"
                )
                scope_value_sel = "scope_value" if "scope_value" in cols else "path_pattern"
                origin_sel = "origin_candidate_id" if "origin_candidate_id" in cols else "NULL"
                rationale_sel = "rationale" if "rationale" in cols else "''"
                evidence_sel = "evidence_count" if "evidence_count" in cols else "sample_count"
                effective_sel = "effective_from" if "effective_from" in cols else "created_at"
                disabled_sel = "disabled_at" if "disabled_at" in cols else "NULL"
                supersedes_sel = "supersedes_rule_id" if "supersedes_rule_id" in cols else "NULL"
                fingerprint_sel = "semantic_fingerprint" if "semantic_fingerprint" in cols else "''"
                where = ""
                params: tuple[object, ...] = ()
                if status and has_status:
                    where, params = " WHERE status = ?", (status,)
                cur = conn.execute(
                    "SELECT id, rule_text, source_signal, category, path_pattern, "
                    f"sample_count, active, {status_sel}, {created_by_sel}, {version_sel}, "
                    f"{scope_type_sel}, {scope_value_sel}, {origin_sel}, {rationale_sel}, "
                    f"{evidence_sel}, {effective_sel}, {disabled_sel}, {supersedes_sel}, "
                    f"{fingerprint_sel}, "
                    "created_at, updated_at "
                    f"FROM learned_rules{where} ORDER BY updated_at DESC",
                    params,
                )
                for r in cur.fetchall():
                    rows.append(
                        {
                            "id": r[0],
                            "platform": platform,
                            "owner": owner,
                            "repo": repo,
                            "rule_text": r[1],
                            "source_signal": r[2],
                            "category": r[3],
                            "path_pattern": r[4],
                            "sample_count": r[5],
                            "active": bool(r[6]),
                            "status": r[7],
                            "created_by": r[8],
                            "version": r[9],
                            "scope_type": r[10],
                            "scope_value": r[11],
                            "origin_candidate_id": r[12],
                            "rationale": r[13],
                            "evidence_count": r[14],
                            "effective_from": r[15],
                            "disabled_at": r[16],
                            "supersedes_rule_id": r[17],
                            "semantic_fingerprint": r[18],
                            "created_at": r[19],
                            "updated_at": r[20],
                        }
                    )
            finally:
                conn.close()
        except sqlite3.Error:
            continue
    rows.sort(key=lambda r: -(r["updated_at"] or 0.0))
    return rows[:limit]
