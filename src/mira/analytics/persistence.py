"""Index-store reads and writes for the change-frequency heatmap.

Shared by the SQLite and the Postgres store. It rides on the triage mixin's
primitives (``_triage_query`` / ``_triage_exec`` / ``_tph`` / ``_triage_scope``)
rather than declaring another copy of them: both stores already implement
those for exactly this shape of statement — the same text on either backend,
pinned to one repository on the backend that shares its tables. The mixin must
therefore come *after* ``TriageStoreMixin`` in a store's bases.
"""

from __future__ import annotations

import json
import time
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from mira.triage.persistence import TriageStoreMixin as _Primitives
else:
    _Primitives = object

# A ceiling on rows one aggregate read returns. The heatmap shows a few
# hundred files at most; a monorepo's long tail is not worth the transfer.
MAX_CHURN_PATHS = 5000


class HotspotStoreMixin(_Primitives):
    """Churn, complexity and finding-density reads for hotspot ranking."""

    # ── writes ──

    def record_file_churn(self, rows: list[dict[str, Any]]) -> int:
        """Record per-file changes. Idempotent on (path, reference); returns new rows."""
        owner = self._triage_owner()
        repo = self._triage_repo()
        written = 0
        for row in rows:
            path = str(row.get("path") or "").strip()
            reference = str(row.get("reference") or "").strip()
            if not path or not reference:
                continue
            written += self._triage_exec(
                self._tph(
                    "INSERT INTO file_churn (platform, owner, repo, path, reference, source, "
                    "additions, deletions, event_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) "
                    "ON CONFLICT DO NOTHING"
                ),
                (
                    str(row.get("platform") or "github"),
                    owner,
                    repo,
                    path,
                    reference,
                    str(row.get("source") or "commit"),
                    max(0, int(row.get("additions") or 0)),
                    max(0, int(row.get("deletions") or 0)),
                    float(row.get("event_at") or 0.0),
                ),
            )
        return written

    def churn_history_fetched_at(self, platform: str = "github") -> float:
        """When commit history was pulled for this repository, or 0.0 if never."""
        clause, params = self._triage_scope()
        rows = self._triage_query(
            self._tph(f"SELECT fetched_at FROM churn_history_fetches WHERE platform = ?{clause}"),
            (platform or "github", *params),
        )
        return float(rows[0][0] or 0.0) if rows else 0.0

    def mark_churn_history_fetched(
        self, platform: str = "github", *, commits: int = 0, at: float = 0.0
    ) -> None:
        self._triage_exec(
            self._tph(
                "INSERT INTO churn_history_fetches (platform, owner, repo, fetched_at, commits) "
                "VALUES (?, ?, ?, ?, ?) ON CONFLICT (platform, owner, repo) DO UPDATE SET "
                "fetched_at = EXCLUDED.fetched_at, commits = EXCLUDED.commits"
            ),
            (
                platform or "github",
                self._triage_owner(),
                self._triage_repo(),
                float(at or time.time()),
                int(commits),
            ),
        )

    # ── reads ──

    def file_churn_stats(self, since: float) -> list[dict[str, Any]]:
        """Per path: distinct changes, lines changed, last change — since ``since``."""
        clause, params = self._triage_scope()
        rows = self._triage_query(
            self._tph(
                "SELECT path, COUNT(DISTINCT reference), SUM(additions + deletions), "
                f"MAX(event_at) FROM file_churn WHERE event_at >= ?{clause} "
                "GROUP BY path ORDER BY COUNT(DISTINCT reference) DESC LIMIT ?"
            ),
            (float(since), *params, MAX_CHURN_PATHS),
        )
        return [
            {
                "path": str(r[0]),
                "changes": int(r[1] or 0),
                "lines": int(r[2] or 0),
                "last_changed_at": float(r[3] or 0.0),
            }
            for r in rows
        ]

    def reviewed_pr_paths(self, since: float) -> dict[str, set[int]]:
        """Path → PR numbers Mira reviewed that touched it, since ``since``.

        From ``review_events.reviewed_paths`` — data every review already
        writes, so the heatmap has something to show before any merge.
        """
        clause, params = self._triage_scope()
        rows = self._triage_query(
            self._tph(
                "SELECT pr_number, reviewed_paths FROM review_events "
                f"WHERE created_at >= ? AND reviewed_paths != ''{clause}"
            ),
            (float(since), *params),
        )
        out: dict[str, set[int]] = {}
        for number, blob in rows:
            try:
                paths = json.loads(blob or "[]")
            except (TypeError, ValueError):
                continue
            if not isinstance(paths, list):
                continue
            for path in paths:
                if isinstance(path, str) and path:
                    out.setdefault(path, set()).add(int(number or 0))
        return out

    def finding_counts_by_path(self, since: float) -> dict[str, int]:
        """Mira findings per file since ``since``, excluding dismissed ones."""
        clause, params = self._triage_scope()
        rows = self._triage_query(
            self._tph(
                "SELECT path, COUNT(*) FROM review_findings "
                f"WHERE created_at >= ? AND path != '' AND state != 'dismissed'{clause} "
                "GROUP BY path"
            ),
            (float(since), *params),
        )
        return {str(r[0]): int(r[1] or 0) for r in rows}

    def file_complexity(self) -> dict[str, tuple[int, int]]:
        """Path → (lines of code, symbol count) from the index."""
        clause, params = self._triage_scope()
        out: dict[str, tuple[int, int]] = {}
        for path, loc in self._triage_query(
            self._tph(f"SELECT path, loc FROM files WHERE 1 = 1{clause}"), params
        ):
            out[str(path)] = (int(loc or 0), 0)
        for path, count in self._triage_query(
            self._tph(
                f"SELECT file_path, COUNT(*) FROM symbols WHERE 1 = 1{clause} GROUP BY file_path"
            ),
            params,
        ):
            loc, _ = out.get(str(path), (0, 0))
            out[str(path)] = (loc, int(count or 0))
        return out
