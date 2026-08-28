"""Persistence for triage runs, candidates and the path history they rank on.

Shared verbatim by the SQLite and the Postgres store: the statements are the
same on both, and the two things that are not — the parameter placeholder and
whether a read has to be scoped to one repository — are the two hooks each
backend overrides. A second implementation would eventually disagree with the
first about something subtle, and the thing it would disagree about is who
gets suggested to review other people's code.

Three tables, and the third exists for a reason worth stating.

``triage_runs``          one row per triage of one pull request, keyed on the
                         content-derived run key, so a redelivered webhook
                         updates a row instead of stacking a second suggestion.
``triage_candidates``    one row per suggested identity per run, so "who does
                         Mira keep suggesting, and did anyone act on it" is a
                         query rather than a JSON scan.
``path_history_fetches`` when each path's history was last fetched — and
                         nothing else.

That last one is the same distinction the whole phase turns on, applied to a
cache. Without it, "we asked the platform who has touched this file and it said
nobody" and "we have never asked" are the same empty result set, so every run
would re-fetch every path forever, or — worse — would treat an unasked question
as an answered one and rank a file's history as empty.
"""

from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from mira.triage.models import (
    ReviewerCandidate,
    TriageRun,
    candidates_from,
    classification_from,
    exclusions_from,
    inputs_from,
    reports_from,
)

# Columns of a run row, in the order every SELECT in this module asks for them.
_RUN_COLUMNS = (
    "id, run_key, platform, owner, repo, pr_number, pr_url, pr_author, base_sha, head_sha, "
    "review_id, policy_version, status, degraded, inputs_json, classification_json, "
    "candidates_json, signals_json, excluded_json, notes_json, counts_json, duration_seconds, "
    "error, attempts, created_at, updated_at"
)

# How many contribution rows one read will return. A ceiling rather than a
# page, because the ranker only needs enough evidence to rank: a file with four
# thousand recorded touches does not produce a better suggestion than the same
# file with two hundred, and the Orange Pi pays for the difference.
MAX_CONTRIBUTION_ROWS = 2000


def dumps(value: Any) -> str:
    return json.dumps(value, sort_keys=True, default=str)


def _loads(blob: str) -> Any:
    try:
        return json.loads(blob or "null")
    except (TypeError, ValueError):
        return None


def candidate_key(run_key: str, identity: str) -> str:
    """Identity of one candidate within one run, so a retry updates its row."""
    return hashlib.sha256(f"{run_key}\x1f{identity.lower()}".encode()).hexdigest()[:32]


def run_from_row(row: tuple) -> TriageRun:
    return TriageRun(
        run_id=int(row[0] or 0),
        run_key=str(row[1] or ""),
        policy_version=str(row[11] or ""),
        inputs=inputs_from(_loads(str(row[14] or "{}"))),
        classification=classification_from(_loads(str(row[15] or "{}"))),
        candidates=candidates_from(_loads(str(row[16] or "[]"))),
        signals=reports_from(_loads(str(row[17] or "[]"))),
        excluded=exclusions_from(_loads(str(row[18] or "[]"))),
        notes=[str(item) for item in (_loads(str(row[19] or "[]")) or [])],
        duration_seconds=float(row[21] or 0.0),
        error=str(row[22] or ""),
        attempts=int(row[23] or 1),
        created_at=float(row[24] or 0.0),
        updated_at=float(row[25] or 0.0),
    )


class TriageStoreMixin:
    """Triage persistence shared by both backends."""

    _triage_placeholder = "?"

    def _triage_query(self, sql: str, params: tuple = ()) -> list[tuple]:
        raise NotImplementedError  # pragma: no cover - backends implement this

    def _triage_exec(self, sql: str, params: tuple = ()) -> int:
        raise NotImplementedError  # pragma: no cover - backends implement this

    @contextmanager
    def _triage_atomic(self) -> Iterator[None]:
        """Everything written inside becomes visible at once, or not at all."""
        yield

    # ------------------------------------------------------------------ util

    def _tph(self, sql: str) -> str:
        return sql.replace("?", self._triage_placeholder)

    def _triage_scope(self) -> tuple[str, tuple[Any, ...]]:
        """Extra WHERE clause pinning reads to this store's repository.

        SQLite keeps a file per repository, so its rows are already scoped;
        Postgres shares one table and overrides this.
        """
        return "", ()

    def _triage_owner(self) -> str:
        """The owner this store scopes on, which wins over a run's own.

        The same ordering the check and gate stores use, and for the same
        reason: ``IndexStore.open`` namespaces a non-GitHub owner as
        ``_{platform}/{owner}``, while a run's inputs carry the plain
        spelling. Writing one and reading with the other would make a row
        invisible to the store that wrote it.
        """
        return str(getattr(self, "_owner", "") or "")

    def _triage_repo(self) -> str:
        return str(getattr(self, "_repo", "") or "")

    # ------------------------------------------------------------------- runs

    def record_triage_run(self, run: TriageRun) -> tuple[TriageRun, bool]:
        """Persist a run and its candidates. Returns ``(stored, created)``.

        One transaction, candidates first, run row last — the same ordering the
        check runs use and for the same reason. A reader that arrives mid-write
        finds no run rather than a run whose candidate list is half written,
        and a half-written candidate list is a suggestion with somebody
        missing from it.

        Candidates the newest attempt did not produce are deleted. Across a
        re-run whose history has grown, yesterday's third name may not be
        today's, and a stale row would go on being counted as a suggestion this
        run never made.
        """
        now = time.time()
        inputs = run.inputs
        owner = self._triage_owner() or inputs.owner
        repo = self._triage_repo() or inputs.repo
        created_at = run.created_at or now

        with self._triage_atomic():
            self._prune_triage_candidates(run)
            for rank, candidate in enumerate(run.candidates, start=1):
                self._record_triage_candidate(
                    candidate, run=run, rank=rank, owner=owner, repo=repo, now=now
                )

            self._triage_exec(
                self._tph(
                    "INSERT INTO triage_runs "
                    "(run_key, platform, owner, repo, pr_number, pr_url, pr_author, base_sha, "
                    "head_sha, review_id, policy_version, status, degraded, inputs_json, "
                    "classification_json, candidates_json, signals_json, excluded_json, "
                    "notes_json, counts_json, duration_seconds, error, attempts, created_at, "
                    "updated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, "
                    "1, ?, ?) "
                    "ON CONFLICT (run_key) DO UPDATE SET "
                    "status = EXCLUDED.status, degraded = EXCLUDED.degraded, "
                    "classification_json = EXCLUDED.classification_json, "
                    "candidates_json = EXCLUDED.candidates_json, "
                    "signals_json = EXCLUDED.signals_json, "
                    "excluded_json = EXCLUDED.excluded_json, "
                    "notes_json = EXCLUDED.notes_json, "
                    "counts_json = EXCLUDED.counts_json, "
                    "duration_seconds = EXCLUDED.duration_seconds, error = EXCLUDED.error, "
                    "attempts = triage_runs.attempts + 1, updated_at = EXCLUDED.updated_at"
                ),
                (
                    run.run_key,
                    inputs.platform,
                    owner,
                    repo,
                    int(inputs.pr_number),
                    inputs.pr_url,
                    inputs.pr_author,
                    inputs.base_sha,
                    inputs.head_sha,
                    int(inputs.review_id),
                    run.policy_version,
                    run.status,
                    1 if run.degraded else 0,
                    dumps(inputs.as_dict()),
                    dumps(run.classification.as_dict()),
                    dumps([candidate.as_dict() for candidate in run.candidates]),
                    dumps([report.as_dict() for report in run.signals]),
                    dumps([item.as_dict() for item in run.excluded]),
                    dumps(list(run.notes)),
                    dumps(run.counts()),
                    float(run.duration_seconds),
                    run.error,
                    created_at,
                    now,
                ),
            )

        stored = self.get_triage_run(run.run_key)
        if stored is None:  # pragma: no cover - only a vanished row
            return run, True
        self._backfill_candidate_run_ids(run.run_key, stored.run_id)
        return stored, self._triage_run_attempts(run.run_key) <= 1

    def _prune_triage_candidates(self, run: TriageRun) -> None:
        keys = [candidate_key(run.run_key, c.identity) for c in run.candidates if c.identity]
        clause, scope_params = self._triage_scope()
        if not keys:
            self._triage_exec(
                self._tph(f"DELETE FROM triage_candidates WHERE run_key = ?{clause}"),
                (run.run_key, *scope_params),
            )
            return
        placeholders = ", ".join("?" for _ in keys)
        self._triage_exec(
            self._tph(
                f"DELETE FROM triage_candidates WHERE run_key = ?{clause} "
                f"AND candidate_key NOT IN ({placeholders})"
            ),
            (run.run_key, *scope_params, *keys),
        )

    def _backfill_candidate_run_ids(self, run_key: str, run_id: int) -> None:
        if not run_id:  # pragma: no cover - only a vanished row
            return
        clause, scope_params = self._triage_scope()
        self._triage_exec(
            self._tph(
                f"UPDATE triage_candidates SET run_id = ? WHERE run_key = ?{clause} AND run_id = 0"
            ),
            (run_id, run_key, *scope_params),
        )

    def _triage_run_attempts(self, run_key: str) -> int:
        clause, scope_params = self._triage_scope()
        rows = self._triage_query(
            self._tph(f"SELECT attempts FROM triage_runs WHERE run_key = ?{clause}"),
            (run_key, *scope_params),
        )
        return int(rows[0][0]) if rows else 1

    def _record_triage_candidate(
        self,
        candidate: ReviewerCandidate,
        *,
        run: TriageRun,
        rank: int,
        owner: str,
        repo: str,
        now: float,
    ) -> None:
        self._triage_exec(
            self._tph(
                "INSERT INTO triage_candidates "
                "(candidate_key, run_id, run_key, platform, owner, repo, pr_number, head_sha, "
                "identity, kind, rank, score, load_penalty, open_reviews, signals, "
                "contributions_json, created_at) "
                "VALUES (?, 0, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT (candidate_key) DO UPDATE SET "
                "rank = EXCLUDED.rank, score = EXCLUDED.score, "
                "load_penalty = EXCLUDED.load_penalty, open_reviews = EXCLUDED.open_reviews, "
                "signals = EXCLUDED.signals, "
                "contributions_json = EXCLUDED.contributions_json"
            ),
            (
                candidate_key(run.run_key, candidate.identity),
                run.run_key,
                run.inputs.platform,
                owner,
                repo,
                int(run.inputs.pr_number),
                run.inputs.head_sha,
                # Lower-cased, because this column is what "was I suggested?"
                # and "who gets suggested most?" are answered from, and the
                # spelling varies with its source: CODEOWNERS carries whatever
                # the file says, a platform login whatever the account says.
                # The candidate blob keeps the display spelling.
                candidate.identity.lower(),
                candidate.kind,
                int(rank),
                float(candidate.score),
                float(candidate.load_penalty),
                int(candidate.open_reviews),
                ",".join(candidate.signals),
                dumps([item.as_dict() for item in candidate.contributions]),
                now,
            ),
        )

    def get_triage_run(self, run_key: str) -> TriageRun | None:
        clause, scope_params = self._triage_scope()
        rows = self._triage_query(
            self._tph(f"SELECT {_RUN_COLUMNS} FROM triage_runs WHERE run_key = ?{clause}"),
            (run_key, *scope_params),
        )
        return run_from_row(rows[0]) if rows else None

    def get_triage_run_by_id(self, run_id: int) -> TriageRun | None:
        clause, scope_params = self._triage_scope()
        rows = self._triage_query(
            self._tph(f"SELECT {_RUN_COLUMNS} FROM triage_runs WHERE id = ?{clause}"),
            (int(run_id), *scope_params),
        )
        return run_from_row(rows[0]) if rows else None

    def latest_triage_run(self, *, pr_number: int, head_sha: str = "") -> TriageRun | None:
        """The newest run for a pull request, optionally pinned to one commit.

        Pinned by default at the call sites that matter: a suggestion computed
        against the previous push was computed against a different set of
        files, and presenting it as current would name people for code they
        were never shown.
        """
        clause, scope_params = self._triage_scope()
        head_clause = " AND head_sha = ?" if head_sha else ""
        params: tuple[Any, ...] = (int(pr_number), *((head_sha,) if head_sha else ()))
        rows = self._triage_query(
            self._tph(
                f"SELECT {_RUN_COLUMNS} FROM triage_runs WHERE pr_number = ?{head_clause}"
                f"{clause} ORDER BY created_at DESC, id DESC LIMIT 1"
            ),
            (*params, *scope_params),
        )
        return run_from_row(rows[0]) if rows else None

    _RUN_SORTS = {
        "created_at": "created_at",
        "pr_number": "pr_number",
        "status": "status",
        "duration_seconds": "duration_seconds",
    }

    def _triage_run_filters(self, filters: dict[str, Any] | None) -> tuple[str, list[Any]]:
        active = dict(filters or {})
        clauses: list[str] = []
        params: list[Any] = []
        for column in ("platform", "status", "pr_author", "head_sha", "owner", "repo"):
            value = active.get(column)
            if value:
                clauses.append(f" AND {column} = ?")
                params.append(value)
        if active.get("pr_number"):
            clauses.append(" AND pr_number = ?")
            params.append(int(active["pr_number"]))
        if active.get("degraded"):
            clauses.append(" AND degraded = 1")
        if active.get("since"):
            clauses.append(" AND created_at >= ?")
            params.append(float(active["since"]))
        if active.get("until"):
            clauses.append(" AND created_at <= ?")
            params.append(float(active["until"]))
        if active.get("identity"):
            # Through the candidate table rather than a LIKE on the blob: a
            # substring match on a JSON list would match `dana` inside
            # `dana-ops`, and being told you were suggested when you were not
            # is the kind of wrong that gets a feature switched off.
            clauses.append(
                " AND run_key IN (SELECT run_key FROM triage_candidates WHERE identity = ?)"
            )
            params.append(str(active["identity"]))
        scope_clause, scope_params = self._triage_scope()
        return "".join(clauses) + scope_clause, [*params, *scope_params]

    def list_triage_runs(
        self,
        filters: dict[str, Any] | None = None,
        *,
        limit: int = 50,
        offset: int = 0,
        sort: str = "created_at",
        descending: bool = True,
    ) -> list[TriageRun]:
        where, params = self._triage_run_filters(filters)
        column = self._RUN_SORTS.get(sort, "created_at")
        order = "DESC" if descending else "ASC"
        rows = self._triage_query(
            self._tph(
                f"SELECT {_RUN_COLUMNS} FROM triage_runs WHERE 1=1{where} "
                f"ORDER BY {column} {order}, id {order} LIMIT ? OFFSET ?"
            ),
            (*params, int(limit), int(offset)),
        )
        return [run_from_row(row) for row in rows]

    def count_triage_runs(self, filters: dict[str, Any] | None = None) -> int:
        where, params = self._triage_run_filters(filters)
        rows = self._triage_query(
            self._tph(f"SELECT COUNT(*) FROM triage_runs WHERE 1=1{where}"), tuple(params)
        )
        return int(rows[0][0]) if rows else 0

    def summarize_triage_candidates(
        self, filters: dict[str, Any] | None = None
    ) -> list[dict[str, Any]]:
        """How often each identity was suggested, and at what rank.

        The question an operator asks before turning suggestions on for a
        second repository: is this naming three people over and over, or is it
        spreading across the team.
        """
        active = dict(filters or {})
        clauses: list[str] = []
        params: list[Any] = []
        for column in ("platform", "identity", "owner", "repo"):
            value = active.get(column)
            if value:
                clauses.append(f" AND {column} = ?")
                params.append(value)
        if active.get("since"):
            clauses.append(" AND created_at >= ?")
            params.append(float(active["since"]))
        if active.get("until"):
            clauses.append(" AND created_at <= ?")
            params.append(float(active["until"]))
        scope_clause, scope_params = self._triage_scope()
        rows = self._triage_query(
            self._tph(
                "SELECT identity, kind, COUNT(*), AVG(rank), AVG(score) FROM triage_candidates "
                f"WHERE 1=1{''.join(clauses)}{scope_clause} GROUP BY identity, kind "
                "ORDER BY COUNT(*) DESC, identity ASC"
            ),
            (*params, *scope_params),
        )
        return [
            {
                "identity": str(row[0] or ""),
                "kind": str(row[1] or "user"),
                "count": int(row[2] or 0),
                "average_rank": round(float(row[3] or 0.0), 3),
                "average_score": round(float(row[4] or 0.0), 3),
            }
            for row in rows
        ]

    # ------------------------------------------------------------ path history

    def record_path_contributions(self, rows: list[dict[str, Any]]) -> int:
        """Record who has touched which files. Idempotent, returns how many were new.

        Insert-or-ignore against the natural key, so re-observing a merged pull
        request or overlapping a history fetch with live events costs nothing
        and cannot double-count somebody into the top of a ranking.
        """
        if not rows:
            return 0
        now = time.time()
        owner = self._triage_owner()
        repo = self._triage_repo()
        written = 0
        for row in rows:
            identity = str(row.get("identity") or "").strip().lower()
            path = str(row.get("path") or "").strip()
            if not identity or not path:
                continue
            written += self._triage_exec(
                self._tph(
                    "INSERT INTO path_contributions "
                    "(platform, owner, repo, path, identity, role, source, reference, url, "
                    "event_at, recorded_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                    "ON CONFLICT DO NOTHING"
                ),
                (
                    str(row.get("platform") or "github"),
                    owner or str(row.get("owner") or ""),
                    repo or str(row.get("repo") or ""),
                    path,
                    identity,
                    str(row.get("role") or "authored"),
                    str(row.get("source") or "commit"),
                    str(row.get("reference") or ""),
                    str(row.get("url") or ""),
                    float(row.get("event_at") or 0.0),
                    now,
                ),
            )
        return written

    def path_contributions(
        self, paths: list[str], *, since: float = 0.0, limit: int = MAX_CONTRIBUTION_ROWS
    ) -> list[dict[str, Any]]:
        """Every recorded touch of these paths since ``since``, newest first."""
        if not paths:
            return []
        placeholders = ", ".join("?" for _ in paths)
        clause, scope_params = self._triage_scope()
        rows = self._triage_query(
            self._tph(
                "SELECT path, identity, role, source, reference, url, event_at "
                f"FROM path_contributions WHERE path IN ({placeholders}) AND event_at >= ?"
                f"{clause} ORDER BY event_at DESC, id DESC LIMIT ?"
            ),
            (*paths, float(since), *scope_params, int(limit)),
        )
        return [
            {
                "path": str(row[0] or ""),
                "identity": str(row[1] or ""),
                "role": str(row[2] or ""),
                "source": str(row[3] or ""),
                "reference": str(row[4] or ""),
                "url": str(row[5] or ""),
                "event_at": float(row[6] or 0.0),
            }
            for row in rows
        ]

    def path_fetch_times(self, paths: list[str], *, platform: str = "github") -> dict[str, float]:
        """When each path's history was last fetched, for paths that were."""
        if not paths:
            return {}
        placeholders = ", ".join("?" for _ in paths)
        clause, scope_params = self._triage_scope()
        rows = self._triage_query(
            self._tph(
                "SELECT path, fetched_at FROM path_history_fetches "
                f"WHERE path IN ({placeholders}) AND platform = ?{clause}"
            ),
            (*paths, platform or "github", *scope_params),
        )
        return {str(row[0]): float(row[1] or 0.0) for row in rows}

    def mark_path_fetched(
        self,
        paths: list[str],
        *,
        platform: str = "github",
        entries: int = 0,
        at: float = 0.0,
    ) -> None:
        """Record that these paths were asked about, whatever came back.

        Called even when the platform returned nothing, because "asked, and
        nobody has touched this file in the window" is an answer worth keeping
        for a week — and re-asking it on every push is how a suggestion feature
        exhausts an installation's rate limit.
        """
        if not paths:
            return
        now = at or time.time()
        owner = self._triage_owner()
        repo = self._triage_repo()
        for path in paths:
            self._triage_exec(
                self._tph(
                    "INSERT INTO path_history_fetches "
                    "(platform, owner, repo, path, fetched_at, entries) VALUES (?, ?, ?, ?, ?, ?) "
                    "ON CONFLICT (platform, owner, repo, path) DO UPDATE SET "
                    "fetched_at = EXCLUDED.fetched_at, entries = EXCLUDED.entries"
                ),
                (
                    platform or "github",
                    owner,
                    repo,
                    path,
                    float(now),
                    int(entries),
                ),
            )
