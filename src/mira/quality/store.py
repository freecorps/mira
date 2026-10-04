"""Persistence for backtest runs and escaped bugs.

A thin layer over a repository's :class:`~mira.index.store.IndexStore` (SQLite
file per repository, or the shared Postgres store), written against the four
primitives the merge gate already relies on — ``_gate_query``, ``_gate_exec``,
``_gate_placeholder`` and ``_gate_scope`` — so one set of statements runs on
both backends and nothing in the store classes had to change.

The DDL is portable on purpose: text primary keys generated here (no
AUTOINCREMENT/BIGSERIAL split), ``DOUBLE PRECISION`` for timestamps (SQLite
reads it as REAL; Postgres would truncate a bare REAL epoch to float4), and
``ON CONFLICT`` clauses both dialects spell the same way. Tables are created
the first time a store is wrapped.
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from mira.quality.models import (
    BacktestRun,
    EscapedBug,
    PRBacktest,
    Score,
    ScoredFinding,
    Signal,
)

logger = logging.getLogger(__name__)

_SCHEMA = (
    """
    CREATE TABLE IF NOT EXISTS quality_backtest_runs (
        id TEXT PRIMARY KEY,
        platform TEXT NOT NULL DEFAULT 'github',
        owner TEXT NOT NULL DEFAULT '',
        repo TEXT NOT NULL DEFAULT '',
        status TEXT NOT NULL DEFAULT 'running',
        variants_json TEXT NOT NULL DEFAULT '{}',
        params_json TEXT NOT NULL DEFAULT '{}',
        summary_json TEXT NOT NULL DEFAULT '[]',
        notes_json TEXT NOT NULL DEFAULT '[]',
        estimated_cost_usd DOUBLE PRECISION NOT NULL DEFAULT 0,
        total_cost_usd DOUBLE PRECISION NOT NULL DEFAULT 0,
        created_at DOUBLE PRECISION NOT NULL DEFAULT 0,
        finished_at DOUBLE PRECISION NOT NULL DEFAULT 0
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS quality_backtest_results (
        id TEXT PRIMARY KEY,
        run_id TEXT NOT NULL,
        owner TEXT NOT NULL DEFAULT '',
        repo TEXT NOT NULL DEFAULT '',
        variant TEXT NOT NULL DEFAULT 'A',
        pr_number INTEGER NOT NULL DEFAULT 0,
        pr_title TEXT NOT NULL DEFAULT '',
        pr_url TEXT NOT NULL DEFAULT '',
        merged_at DOUBLE PRECISION NOT NULL DEFAULT 0,
        findings INTEGER NOT NULL DEFAULT 0,
        tp INTEGER NOT NULL DEFAULT 0,
        fp INTEGER NOT NULL DEFAULT 0,
        unlabelled INTEGER NOT NULL DEFAULT 0,
        positives INTEGER NOT NULL DEFAULT 0,
        positives_caught INTEGER NOT NULL DEFAULT 0,
        prompt_tokens BIGINT NOT NULL DEFAULT 0,
        completion_tokens BIGINT NOT NULL DEFAULT 0,
        cost_usd DOUBLE PRECISION NOT NULL DEFAULT 0,
        latency_ms BIGINT NOT NULL DEFAULT 0,
        error TEXT NOT NULL DEFAULT '',
        skipped_reason TEXT NOT NULL DEFAULT '',
        blocked_writes INTEGER NOT NULL DEFAULT 0,
        findings_json TEXT NOT NULL DEFAULT '[]',
        signals_json TEXT NOT NULL DEFAULT '[]',
        created_at DOUBLE PRECISION NOT NULL DEFAULT 0
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_quality_backtest_results_run
        ON quality_backtest_results(run_id, variant, pr_number)
    """,
    """
    CREATE TABLE IF NOT EXISTS quality_escaped_bugs (
        id TEXT PRIMARY KEY,
        platform TEXT NOT NULL DEFAULT 'github',
        owner TEXT NOT NULL DEFAULT '',
        repo TEXT NOT NULL DEFAULT '',
        kind TEXT NOT NULL DEFAULT 'hotfix',
        fix_ref TEXT NOT NULL DEFAULT '',
        fix_pr_number INTEGER NOT NULL DEFAULT 0,
        fix_sha TEXT NOT NULL DEFAULT '',
        fix_title TEXT NOT NULL DEFAULT '',
        fix_url TEXT NOT NULL DEFAULT '',
        original_pr_number INTEGER NOT NULL DEFAULT 0,
        original_pr_url TEXT NOT NULL DEFAULT '',
        original_merged_at DOUBLE PRECISION NOT NULL DEFAULT 0,
        path TEXT NOT NULL DEFAULT '',
        line_start INTEGER NOT NULL DEFAULT 0,
        line_end INTEGER NOT NULL DEFAULT 0,
        flagged INTEGER NOT NULL DEFAULT 0,
        flagged_file INTEGER NOT NULL DEFAULT 0,
        finding_ids_json TEXT NOT NULL DEFAULT '[]',
        link_method TEXT NOT NULL DEFAULT '',
        learning_candidate_id INTEGER NOT NULL DEFAULT 0,
        detail_json TEXT NOT NULL DEFAULT '{}',
        detected_at DOUBLE PRECISION NOT NULL DEFAULT 0
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_quality_escaped_bugs_original
        ON quality_escaped_bugs(owner, repo, original_pr_number)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_quality_escaped_bugs_detected
        ON quality_escaped_bugs(detected_at)
    """,
)

_RUN_COLUMNS = (
    "id, platform, owner, repo, status, variants_json, params_json, summary_json, "
    "notes_json, estimated_cost_usd, total_cost_usd, created_at, finished_at"
)

_RESULT_COLUMNS = (
    "variant, pr_number, pr_title, pr_url, merged_at, findings, tp, fp, unlabelled, "
    "positives, positives_caught, prompt_tokens, completion_tokens, cost_usd, latency_ms, "
    "error, skipped_reason, blocked_writes, findings_json, signals_json"
)

_BUG_COLUMNS = (
    "id, platform, owner, repo, kind, fix_ref, fix_pr_number, fix_sha, fix_title, fix_url, "
    "original_pr_number, original_pr_url, original_merged_at, path, line_start, line_end, "
    "flagged, flagged_file, finding_ids_json, link_method, learning_candidate_id, "
    "detail_json, detected_at"
)

_FINDING_COLUMNS = (
    "id, path, start_line, end_line, category, severity, title, body, state, confidence"
)


def _loads(blob: Any, default: Any) -> Any:
    try:
        value = json.loads(blob or "null")
    except (TypeError, ValueError):
        return default
    return default if value is None else value


def _dumps(value: Any) -> str:
    return json.dumps(value, sort_keys=True, default=str)


_QUALITY_TABLES = ("quality_backtest_runs", "quality_backtest_results", "quality_escaped_bugs")


class QualityStoreUnavailable(RuntimeError):
    """The quality tables do not exist and could not be created on this handle."""


class QualityStore:
    """Backtest and escaped-bug rows for one repository (or, on Postgres, all)."""

    def __init__(self, store: Any) -> None:
        self._store = store
        # Why the tables are unusable, or "" when they are fine.
        self.unavailable_reason = ""
        self._ensure_schema()

    @property
    def index_store(self) -> Any:
        return self._store

    # ----------------------------------------------------------- primitives

    def _sql(self, sql: str) -> str:
        return sql.replace("?", getattr(self._store, "_gate_placeholder", "?"))

    def _query(self, sql: str, params: tuple = ()) -> list[tuple]:
        return list(self._store._gate_query(self._sql(sql), params))

    def _exec(self, sql: str, params: tuple = ()) -> int:
        return int(self._store._gate_exec(self._sql(sql), params) or 0)

    def _scope(self) -> tuple[str, tuple[Any, ...]]:
        clause, params = self._store._gate_scope()
        return clause.replace("%s", "?"), tuple(params)

    def _owner(self) -> str:
        return str(getattr(self._store, "_owner", "") or "")

    def _repo(self) -> str:
        return str(getattr(self._store, "_repo", "") or "")

    def _ensure_schema(self) -> None:
        failures: list[str] = []
        for statement in _SCHEMA:
            try:
                self._exec(statement.strip())
            except Exception as exc:  # noqa: BLE001 - a read-only handle cannot create
                failures.append(str(exc))
        if not failures:
            return
        # A read-only handle refuses even ``CREATE ... IF NOT EXISTS``; that is
        # fine as long as the tables are already there.
        missing = []
        for table in _QUALITY_TABLES:
            try:
                self._query(f"SELECT 1 FROM {table} LIMIT 1")
            except Exception:  # noqa: BLE001
                missing.append(table)
        if missing:
            self.unavailable_reason = (
                f"quality tables unavailable ({', '.join(missing)}): {failures[0]}"
            )
            logger.error("Quality store unusable, nothing will be persisted: %s", failures[0])
        else:
            logger.warning("Quality schema statement(s) failed on an existing schema: %s", failures)

    def _require_schema(self) -> None:
        """Make a write fail loudly instead of vanishing when the tables are missing."""
        if self.unavailable_reason:
            raise QualityStoreUnavailable(self.unavailable_reason)

    # ------------------------------------------------------------ backtests

    def save_backtest_run(self, run: BacktestRun) -> None:
        """Insert or update the run row (not its results)."""
        self._require_schema()
        self._exec(
            "INSERT INTO quality_backtest_runs ("
            + _RUN_COLUMNS
            + ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(id) DO UPDATE SET status=excluded.status, "
            "summary_json=excluded.summary_json, notes_json=excluded.notes_json, "
            "total_cost_usd=excluded.total_cost_usd, finished_at=excluded.finished_at",
            (
                run.id,
                run.platform,
                self._owner() or run.owner,
                self._repo() or run.repo,
                run.status,
                _dumps(run.variants),
                _dumps(run.params),
                _dumps([s.to_dict() for s in run.summaries()]),
                _dumps(run.notes),
                float(run.estimated_cost_usd),
                float(run.total_cost_usd),
                float(run.created_at or time.time()),
                float(run.finished_at),
            ),
        )

    def save_backtest_result(
        self, run_id: str, result: PRBacktest, *, owner: str = "", repo: str = ""
    ) -> None:
        """Insert one result row; ``owner``/``repo`` are the run's, used when the
        handle itself is not pinned to a repository (as ``save_backtest_run`` does)."""
        self._require_schema()
        row_id = f"{run_id}:{result.variant}:{result.pr_number}"
        score = result.score
        self._exec(
            "INSERT INTO quality_backtest_results (id, run_id, owner, repo, "
            + _RESULT_COLUMNS
            + ", created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, "
            "?, ?, ?, ?, ?, ?, ?) ON CONFLICT(id) DO NOTHING",
            (
                row_id,
                run_id,
                self._owner() or owner,
                self._repo() or repo,
                result.variant,
                int(result.pr_number),
                result.pr_title,
                result.pr_url,
                float(result.merged_at),
                score.findings,
                score.tp,
                score.fp,
                score.unlabelled,
                score.positives,
                score.positives_caught,
                int(result.prompt_tokens),
                int(result.completion_tokens),
                float(result.cost_usd),
                int(result.latency_ms),
                result.error,
                result.skipped_reason,
                int(result.blocked_writes),
                _dumps([f.to_dict() for f in result.findings]),
                _dumps([s.to_dict() for s in result.signals]),
                time.time(),
            ),
        )

    def _run_from_row(self, row: tuple) -> BacktestRun:
        run = BacktestRun(
            id=str(row[0]),
            platform=str(row[1] or "github"),
            owner=str(row[2] or ""),
            repo=str(row[3] or ""),
            status=str(row[4] or ""),
            variants=_loads(row[5], {}),
            params=_loads(row[6], {}),
            notes=_loads(row[8], []),
            estimated_cost_usd=float(row[9] or 0.0),
            created_at=float(row[11] or 0.0),
            finished_at=float(row[12] or 0.0),
        )
        # Stored so a listing does not have to load every result to show totals.
        run.params["_summaries"] = _loads(row[7], [])
        run.params["_total_cost_usd"] = float(row[10] or 0.0)
        return run

    def list_backtest_runs(self, *, limit: int = 50, offset: int = 0) -> list[dict[str, Any]]:
        clause, params = self._scope()
        rows = self._query(
            f"SELECT {_RUN_COLUMNS} FROM quality_backtest_runs WHERE 1=1{clause} "
            "ORDER BY created_at DESC LIMIT ? OFFSET ?",
            (*params, int(limit), int(offset)),
        )
        return [self._run_listing(self._run_from_row(row)) for row in rows]

    @staticmethod
    def _run_listing(run: BacktestRun) -> dict[str, Any]:
        params = dict(run.params)
        summaries = params.pop("_summaries", [])
        total = params.pop("_total_cost_usd", 0.0)
        return {
            "id": run.id,
            "platform": run.platform,
            "owner": run.owner,
            "repo": run.repo,
            "status": run.status,
            "variants": run.variants,
            "params": params,
            "notes": run.notes,
            "estimated_cost_usd": run.estimated_cost_usd,
            "total_cost_usd": total,
            "summaries": summaries,
            "created_at": run.created_at,
            "finished_at": run.finished_at,
        }

    def get_backtest_run(self, run_id: str) -> dict[str, Any] | None:
        clause, params = self._scope()
        rows = self._query(
            f"SELECT {_RUN_COLUMNS} FROM quality_backtest_runs WHERE id = ?{clause}",
            (run_id, *params),
        )
        if not rows:
            return None
        listing = self._run_listing(self._run_from_row(rows[0]))
        result_rows = self._query(
            f"SELECT {_RESULT_COLUMNS} FROM quality_backtest_results WHERE run_id = ?{clause} "
            "ORDER BY pr_number DESC, variant ASC",
            (run_id, *params),
        )
        listing["results"] = [self._result_from_row(r).to_dict() for r in result_rows]
        return listing

    @staticmethod
    def _result_from_row(row: tuple) -> PRBacktest:
        findings = [ScoredFinding(**item) for item in _loads(row[18], []) if isinstance(item, dict)]
        signals = [Signal(**item) for item in _loads(row[19], []) if isinstance(item, dict)]
        return PRBacktest(
            variant=str(row[0] or "A"),
            pr_number=int(row[1] or 0),
            pr_title=str(row[2] or ""),
            pr_url=str(row[3] or ""),
            merged_at=float(row[4] or 0.0),
            findings=findings,
            signals=signals,
            score=Score(
                findings=int(row[5] or 0),
                tp=int(row[6] or 0),
                fp=int(row[7] or 0),
                unlabelled=int(row[8] or 0),
                positives=int(row[9] or 0),
                positives_caught=int(row[10] or 0),
            ),
            prompt_tokens=int(row[11] or 0),
            completion_tokens=int(row[12] or 0),
            cost_usd=float(row[13] or 0.0),
            latency_ms=int(row[14] or 0),
            error=str(row[15] or ""),
            skipped_reason=str(row[16] or ""),
            blocked_writes=int(row[17] or 0),
        )

    # -------------------------------------------------------- escaped bugs

    def record_escaped_bug(self, bug: EscapedBug) -> bool:
        """Insert unless this exact (fix, original PR, path) is known. True when new."""
        self._require_schema()
        bug.detected_at = bug.detected_at or time.time()
        changed = self._exec(
            "INSERT INTO quality_escaped_bugs ("
            + _BUG_COLUMNS
            + ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(id) DO NOTHING",
            (
                bug.id,
                bug.platform,
                self._owner() or bug.owner,
                self._repo() or bug.repo,
                bug.kind,
                bug.fix_ref,
                int(bug.fix_pr_number),
                bug.fix_sha,
                bug.fix_title,
                bug.fix_url,
                int(bug.original_pr_number),
                bug.original_pr_url,
                float(bug.original_merged_at),
                bug.path,
                int(bug.line_start),
                int(bug.line_end),
                1 if bug.flagged else 0,
                1 if bug.flagged_file else 0,
                _dumps(bug.finding_ids),
                bug.link_method,
                int(bug.learning_candidate_id),
                _dumps(bug.detail),
                float(bug.detected_at),
            ),
        )
        return changed > 0

    def set_escaped_bug_candidate(self, bug_id: str, candidate_id: int) -> None:
        self._require_schema()
        clause, params = self._scope()
        self._exec(
            f"UPDATE quality_escaped_bugs SET learning_candidate_id = ? WHERE id = ?{clause}",
            (int(candidate_id), bug_id, *params),
        )

    @staticmethod
    def _bug_from_row(row: tuple) -> EscapedBug:
        return EscapedBug(
            id=str(row[0]),
            platform=str(row[1] or "github"),
            owner=str(row[2] or ""),
            repo=str(row[3] or ""),
            kind=str(row[4] or ""),
            fix_ref=str(row[5] or ""),
            fix_pr_number=int(row[6] or 0),
            fix_sha=str(row[7] or ""),
            fix_title=str(row[8] or ""),
            fix_url=str(row[9] or ""),
            original_pr_number=int(row[10] or 0),
            original_pr_url=str(row[11] or ""),
            original_merged_at=float(row[12] or 0.0),
            path=str(row[13] or ""),
            line_start=int(row[14] or 0),
            line_end=int(row[15] or 0),
            flagged=bool(row[16]),
            flagged_file=bool(row[17]),
            finding_ids=[str(x) for x in _loads(row[18], [])],
            link_method=str(row[19] or ""),
            learning_candidate_id=int(row[20] or 0),
            detail=_loads(row[21], {}),
            detected_at=float(row[22] or 0.0),
        )

    def get_escaped_bug(self, bug_id: str) -> EscapedBug | None:
        clause, params = self._scope()
        rows = self._query(
            f"SELECT {_BUG_COLUMNS} FROM quality_escaped_bugs WHERE id = ?{clause}",
            (bug_id, *params),
        )
        return self._bug_from_row(rows[0]) if rows else None

    def list_escaped_bugs(
        self,
        *,
        flagged: bool | None = None,
        since: float = 0.0,
        limit: int = 50,
        offset: int = 0,
    ) -> list[EscapedBug]:
        clause, params = self._scope()
        where = ["1=1"]
        values: list[Any] = []
        if flagged is not None:
            where.append("flagged = ?")
            values.append(1 if flagged else 0)
        if since:
            where.append("detected_at >= ?")
            values.append(float(since))
        rows = self._query(
            f"SELECT {_BUG_COLUMNS} FROM quality_escaped_bugs WHERE {' AND '.join(where)}"
            f"{clause} ORDER BY detected_at DESC LIMIT ? OFFSET ?",
            (*values, *params, int(limit), int(offset)),
        )
        return [self._bug_from_row(row) for row in rows]

    def escaped_bug_counts(self, *, since: float = 0.0) -> dict[str, int]:
        """Distinct escaped *incidents* (fix, original PR), caught vs missed.

        A fix touching three files of one original pull request is one escape,
        not three: counted per (fix, original PR), and "caught" when Mira
        flagged lines in any of those files.
        """
        clause, params = self._scope()
        rows = self._query(
            "SELECT fix_ref, original_pr_number, MAX(flagged) FROM quality_escaped_bugs "
            f"WHERE detected_at >= ?{clause} GROUP BY fix_ref, original_pr_number",
            (float(since), *params),
        )
        caught = sum(1 for row in rows if int(row[2] or 0))
        return {"caught": caught, "missed": len(rows) - caught, "total": len(rows)}

    # ------------------------------------------- reads of review history

    def findings_for_pr(self, pr_number: int) -> list[dict[str, Any]]:
        """Every finding Mira persisted for a pull request, any pass."""
        clause, params = self._scope()
        try:
            rows = self._query(
                f"SELECT {_FINDING_COLUMNS} FROM review_findings WHERE pr_number = ?{clause}",
                (int(pr_number), *params),
            )
        except Exception as exc:  # noqa: BLE001 - an older index has no table yet
            logger.debug("review_findings unavailable: %s", exc)
            return []
        return [
            {
                "id": str(r[0]),
                "path": str(r[1] or ""),
                "start_line": int(r[2] or 0),
                "end_line": int(r[3] or 0) or int(r[2] or 0),
                "category": str(r[4] or ""),
                "severity": str(r[5] or ""),
                "title": str(r[6] or ""),
                "body": str(r[7] or ""),
                "state": str(r[8] or ""),
                "confidence": float(r[9] or 0.0),
            }
            for r in rows
        ]

    def was_reviewed(self, pr_number: int) -> bool:
        """Whether Mira reviewed this pull request (a recorded review or finding)."""
        try:
            if self._store.list_review_events_for_pr(int(pr_number)):
                return True
        except Exception as exc:  # noqa: BLE001
            logger.debug("review_events unavailable: %s", exc)
        return bool(self.findings_for_pr(pr_number))

    def reviewed_prs_touching(self, path: str, *, since: float, limit: int = 20) -> list[int]:
        """Pull requests Mira reviewed after ``since`` whose reviewed paths include ``path``.

        The fallback for providers that cannot blame: candidates are narrowed
        to reviewed pull requests that touched the file before anyone fetches
        a diff.
        """
        clause, params = self._scope()
        page = 500
        offset = 0
        out: list[int] = []
        # The window is filtered in the query and read page by page, so a busy
        # repository's in-window reviews are not cut off by a fixed row cap.
        while len(out) < limit:
            try:
                rows = self._query(
                    "SELECT pr_number, reviewed_paths FROM review_events "
                    f"WHERE created_at >= ?{clause} ORDER BY created_at DESC, id DESC "
                    "LIMIT ? OFFSET ?",
                    (float(since), *params, page, offset),
                )
            except Exception as exc:  # noqa: BLE001
                logger.debug("review_events unavailable: %s", exc)
                break
            for row in rows:
                number = int(row[0] or 0)
                paths = str(row[1] or "")
                listed = _loads(paths, None)
                names = (
                    {str(p) for p in listed} if isinstance(listed, list) else set(paths.split(","))
                )
                if path in names and number and number not in out:
                    out.append(number)
                    if len(out) >= limit:
                        break
            if len(rows) < page:
                break
            offset += page
        return out


@contextmanager
def open_quality_store(owner: str, repo: str, platform: str = "github") -> Iterator[QualityStore]:
    """Open the repository's store, wrap it, and always close it."""
    from mira.index.store import IndexStore

    store = IndexStore.open(owner, repo, platform=platform)
    try:
        yield QualityStore(store)
    finally:
        store.close()
