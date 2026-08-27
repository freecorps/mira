"""Storage for check runs and check results — one implementation.

SQLite keeps a database per repository and Postgres keeps one for the whole
install, but the check tables carry the same columns in both, so the queries
are written once here and mixed into both stores. Parity is then a property of
the code rather than a promise in a docstring: there is no second copy to
drift.

Each backend supplies two primitives — a placeholder and a read, and a write —
and nothing else.

**Retries converge rather than accumulate.** Both tables are keyed on a
content-derived identity: a run on the pull request, the commit, the policy and
the facts; a result on the run and the check. A redelivered webhook, a manual
re-run and a second worker all land on the same rows.

They *update* those rows rather than being ignored, which is the one place this
differs from the gate's decision table, and the reason is the difference
between the two records. A gate decision is an act — it may already have been
delivered as an approval, and rewriting it could erase an administrative
override. A check result is an *answer to a question*: when the same question
is asked again over identical facts, the newest answer is the one worth
keeping, and a run that failed because the network was down should read as
resolved once it is retried successfully rather than staying wrong forever.
``attempts`` records that it took more than one go.
"""

from __future__ import annotations

import time
from typing import Any

from mira.checks.models import (
    CheckResult,
    CheckRun,
    CheckRunInputs,
    dumps,
    evidence_from,
    findings_from,
    loads,
)

_RUN_COLUMNS = (
    "id",
    "run_key",
    "platform",
    "owner",
    "repo",
    "pr_number",
    "pr_url",
    "pr_author",
    "base_branch",
    "head_sha",
    "review_id",
    "policy_version",
    "verdict",
    "inputs_json",
    "counts_json",
    "duration_seconds",
    "error",
    "attempts",
    "created_at",
    "updated_at",
)

_RESULT_COLUMNS = (
    "id",
    "result_key",
    "run_id",
    "run_key",
    "platform",
    "owner",
    "repo",
    "pr_number",
    "head_sha",
    "check_id",
    "check_version",
    "title",
    "origin",
    "mode",
    "state",
    "summary",
    "skip_reason",
    "error",
    "duration_seconds",
    "config_digest",
    "evidence_json",
    "findings_json",
    "sources_json",
    "incomplete",
    "blocking",
    "created_at",
)

# Sortable columns exposed to the API. An allowlist, because the value arrives
# from a query string.
RUN_SORT_COLUMNS = {
    "created_at": "created_at",
    "pr_number": "pr_number",
    "verdict": "verdict",
    "duration_seconds": "duration_seconds",
}

RESULT_SORT_COLUMNS = {
    "created_at": "created_at",
    "check_id": "check_id",
    "state": "state",
    "duration_seconds": "duration_seconds",
}


def _inputs_from_json(blob: str) -> CheckRunInputs:
    data = loads(blob, {})
    if not isinstance(data, dict):
        return CheckRunInputs()
    known = set(CheckRunInputs.__dataclass_fields__)
    return CheckRunInputs(**{key: value for key, value in data.items() if key in known})


def result_from_row(row: tuple) -> CheckResult:
    """Rehydrate a result from either backend's row tuple."""
    data = dict(zip(_RESULT_COLUMNS, row, strict=False))
    sources = loads(str(data.get("sources_json") or "[]"), [])
    return CheckResult(
        id=int(data.get("id") or 0),
        result_key=str(data.get("result_key") or ""),
        check_id=str(data.get("check_id") or ""),
        check_version=str(data.get("check_version") or "1"),
        title=str(data.get("title") or ""),
        origin=str(data.get("origin") or "native"),  # type: ignore[arg-type]
        mode=str(data.get("mode") or "off"),  # type: ignore[arg-type]
        state=str(data.get("state") or "skipped"),  # type: ignore[arg-type]
        summary=str(data.get("summary") or ""),
        evidence=evidence_from(loads(str(data.get("evidence_json") or "[]"), [])),
        findings=findings_from(loads(str(data.get("findings_json") or "[]"), [])),
        skip_reason=str(data.get("skip_reason") or ""),
        error=str(data.get("error") or ""),
        duration_seconds=float(data.get("duration_seconds") or 0.0),
        config_digest=str(data.get("config_digest") or ""),
        sources=[str(source) for source in sources] if isinstance(sources, list) else [],
        created_at=float(data.get("created_at") or 0.0),
    )


def run_from_row(row: tuple, results: list[CheckResult] | None = None) -> CheckRun:
    data = dict(zip(_RUN_COLUMNS, row, strict=False))
    run = CheckRun(
        id=int(data.get("id") or 0),
        run_key=str(data.get("run_key") or ""),
        policy_version=str(data.get("policy_version") or ""),
        duration_seconds=float(data.get("duration_seconds") or 0.0),
        error=str(data.get("error") or ""),
        created_at=float(data.get("created_at") or 0.0),
        updated_at=float(data.get("updated_at") or 0.0),
        results=list(results or []),
    )
    run.inputs = _inputs_from_json(str(data.get("inputs_json") or "{}"))
    # Columns win over the inputs blob: they are what the queries filter on, so
    # a row that somehow disagrees with its own payload still lists correctly.
    run.inputs.platform = str(data.get("platform") or run.inputs.platform)
    run.inputs.owner = str(data.get("owner") or run.inputs.owner)
    run.inputs.repo = str(data.get("repo") or run.inputs.repo)
    run.inputs.pr_number = int(data.get("pr_number") or run.inputs.pr_number)
    run.inputs.pr_url = str(data.get("pr_url") or run.inputs.pr_url)
    run.inputs.pr_author = str(data.get("pr_author") or run.inputs.pr_author)
    run.inputs.head_sha = str(data.get("head_sha") or run.inputs.head_sha)
    run.inputs.review_id = int(data.get("review_id") or run.inputs.review_id)
    return run


class ChecksStoreMixin:
    """Check persistence shared verbatim by the SQLite and Postgres stores."""

    # Backends override these two.
    _checks_placeholder = "?"

    def _checks_query(self, sql: str, params: tuple = ()) -> list[tuple]:
        raise NotImplementedError  # pragma: no cover - backends implement this

    def _checks_exec(self, sql: str, params: tuple = ()) -> int:
        raise NotImplementedError  # pragma: no cover - backends implement this

    # ------------------------------------------------------------------ util

    def _cph(self, sql: str) -> str:
        return sql.replace("?", self._checks_placeholder)

    def _checks_scope(self) -> tuple[str, tuple[Any, ...]]:
        """Extra WHERE clause pinning reads to this store's repository.

        SQLite has one file per repository so its rows are already scoped;
        Postgres shares one table across the install and overrides this. An
        empty owner/repo is the deliberate org-wide handle.
        """
        return "", ()

    def _checks_owner(self) -> str:
        """The owner this store scopes its reads on.

        Wins over the owner carried on a run, and that ordering is load-bearing
        on Postgres for the same reason it is in the gate's store:
        ``IndexStore.open`` namespaces a non-GitHub owner as
        ``_{platform}/{owner}`` and hands that spelling to the store, while a
        run's inputs carry the plain one. Writing the plain owner and reading
        with the namespaced scope would make the row invisible to the store
        that wrote it — no idempotency and no history.
        """
        return str(getattr(self, "_owner", "") or "")

    def _checks_repo(self) -> str:
        return str(getattr(self, "_repo", "") or "")

    # ------------------------------------------------------------------- runs

    def record_check_run(self, run: CheckRun) -> tuple[CheckRun, bool]:
        """Persist a run and every result in it. Returns ``(stored, created)``.

        Upserts on the content-derived keys, so a redelivered webhook or a
        manual re-run over identical facts converges on one run and one row per
        check instead of stacking a second set. ``attempts`` on the run row
        records that it happened more than once.

        **Results are written before the run row, and that ordering is load
        bearing.** Neither backend gives this method a transaction — each write
        commits on its own — so a reader can arrive mid-write. A verdict is not
        stored; it is computed from the results that are present. Writing the
        run first would therefore expose a row whose results were still
        arriving, and a gate reading it would compute a verdict from *some* of
        the checks — quite possibly `pass`, from the half that happened to be
        written. Writing the results first means a reader either finds no run
        (which an active policy treats as incomplete, and refuses on) or finds
        one with everything it needs.
        """
        now = time.time()
        inputs = run.inputs
        owner = self._checks_owner() or inputs.owner
        repo = self._checks_repo() or inputs.repo
        created_at = run.created_at or now

        for result in run.results:
            self._record_check_result(result, run=run, owner=owner, repo=repo)

        self._checks_exec(
            self._cph(
                "INSERT INTO check_runs "
                "(run_key, platform, owner, repo, pr_number, pr_url, pr_author, base_branch, "
                "head_sha, review_id, policy_version, verdict, inputs_json, counts_json, "
                "duration_seconds, error, attempts, created_at, updated_at) VALUES "
                "(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?) "
                "ON CONFLICT (run_key) DO UPDATE SET "
                "verdict = EXCLUDED.verdict, counts_json = EXCLUDED.counts_json, "
                "duration_seconds = EXCLUDED.duration_seconds, error = EXCLUDED.error, "
                "attempts = check_runs.attempts + 1, updated_at = EXCLUDED.updated_at"
            ),
            (
                run.run_key,
                inputs.platform,
                owner,
                repo,
                int(inputs.pr_number),
                inputs.pr_url,
                inputs.pr_author,
                inputs.base_branch,
                inputs.head_sha,
                int(inputs.review_id),
                run.policy_version,
                run.verdict,
                dumps(inputs.as_dict()),
                dumps(run.counts()),
                float(run.duration_seconds),
                run.error,
                created_at,
                now,
            ),
        )

        stored = self.get_check_run(run.run_key)
        if stored is None:  # pragma: no cover - only a vanished row
            return run, True
        # Results were written before the run row existed, so they carry no
        # `run_id` yet. Backfilled here rather than threaded through: it is a
        # convenience column for joins, and nothing reads a result by it.
        self._backfill_result_run_ids(run.run_key, stored.id)
        # `attempts` is 1 on the row that was just inserted and higher on one
        # that already existed, which is a more reliable "was this new" than a
        # rowcount that both backends report differently for an upsert.
        created = self._check_run_attempts(run.run_key) <= 1
        return stored, created

    def _backfill_result_run_ids(self, run_key: str, run_id: int) -> None:
        if not run_id:  # pragma: no cover - only a vanished row
            return
        self._checks_exec(
            self._cph("UPDATE check_results SET run_id = ? WHERE run_key = ? AND run_id <> ?"),
            (int(run_id), run_key, int(run_id)),
        )

    def _check_run_attempts(self, run_key: str) -> int:
        clause, scope_params = self._checks_scope()
        rows = self._checks_query(
            self._cph(f"SELECT attempts FROM check_runs WHERE run_key = ?{clause}"),
            (run_key, *scope_params),
        )
        return int(rows[0][0] or 0) if rows else 0

    def _record_check_result(
        self, result: CheckResult, *, run: CheckRun, owner: str, repo: str
    ) -> None:
        now = time.time()
        self._checks_exec(
            self._cph(
                "INSERT INTO check_results "
                "(result_key, run_id, run_key, platform, owner, repo, pr_number, head_sha, "
                "check_id, check_version, title, origin, mode, state, summary, skip_reason, "
                "error, duration_seconds, config_digest, evidence_json, findings_json, "
                "sources_json, incomplete, blocking, created_at) VALUES "
                "(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT (result_key) DO UPDATE SET "
                "state = EXCLUDED.state, summary = EXCLUDED.summary, "
                "skip_reason = EXCLUDED.skip_reason, error = EXCLUDED.error, "
                "mode = EXCLUDED.mode, duration_seconds = EXCLUDED.duration_seconds, "
                "config_digest = EXCLUDED.config_digest, evidence_json = EXCLUDED.evidence_json, "
                "findings_json = EXCLUDED.findings_json, sources_json = EXCLUDED.sources_json, "
                "incomplete = EXCLUDED.incomplete, blocking = EXCLUDED.blocking"
            ),
            (
                result.result_key,
                # Zero until the run row exists; `_backfill_result_run_ids`
                # fills it in afterwards. Nothing reads a result by it.
                0,
                run.run_key,
                run.inputs.platform,
                owner,
                repo,
                int(run.inputs.pr_number),
                run.inputs.head_sha,
                result.check_id,
                result.check_version,
                result.title,
                result.origin,
                result.mode,
                result.state,
                result.summary,
                result.skip_reason,
                result.error,
                float(result.duration_seconds),
                result.config_digest,
                dumps([item.as_dict() for item in result.evidence]),
                dumps([finding.as_dict() for finding in result.findings]),
                dumps(list(result.sources)),
                1 if result.incomplete else 0,
                1 if result.blocking else 0,
                result.created_at or now,
            ),
        )

    def get_check_run(self, run_key: str, *, with_results: bool = True) -> CheckRun | None:
        clause, scope_params = self._checks_scope()
        rows = self._checks_query(
            self._cph(
                f"SELECT {', '.join(_RUN_COLUMNS)} FROM check_runs WHERE run_key = ?{clause}"
            ),
            (run_key, *scope_params),
        )
        if not rows:
            return None
        results = self.list_check_results(run_key=run_key) if with_results else []
        return run_from_row(rows[0], results)

    def get_check_run_by_id(self, run_id: int) -> CheckRun | None:
        clause, scope_params = self._checks_scope()
        rows = self._checks_query(
            self._cph(f"SELECT {', '.join(_RUN_COLUMNS)} FROM check_runs WHERE id = ?{clause}"),
            (int(run_id), *scope_params),
        )
        if not rows:
            return None
        run = run_from_row(rows[0])
        run.results = self.list_check_results(run_key=run.run_key)
        return run

    def _run_filters(self, filters: dict[str, Any] | None) -> tuple[str, list[Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        active = filters or {}
        for column, key in (
            ("verdict", "verdict"),
            ("platform", "platform"),
            ("owner", "owner"),
            ("repo", "repo"),
            ("pr_author", "pr_author"),
            ("head_sha", "head_sha"),
            ("policy_version", "policy_version"),
        ):
            value = active.get(key)
            if value:
                clauses.append(f"{column} = ?")
                params.append(value)
        pr_number = active.get("pr_number")
        if pr_number:
            clauses.append("pr_number = ?")
            params.append(int(pr_number))
        since = active.get("since")
        if since:
            clauses.append("created_at >= ?")
            params.append(float(since))
        until = active.get("until")
        if until:
            clauses.append("created_at < ?")
            params.append(float(until))
        scope_clause, scope_params = self._checks_scope()
        where = " AND ".join(clauses) if clauses else "1 = 1"
        if scope_clause:
            where += scope_clause
            params.extend(scope_params)
        return where, params

    def list_check_runs(
        self,
        filters: dict[str, Any] | None = None,
        *,
        limit: int = 50,
        offset: int = 0,
        sort: str = "created_at",
        descending: bool = True,
        with_results: bool = False,
    ) -> list[CheckRun]:
        where, params = self._run_filters(filters)
        column = RUN_SORT_COLUMNS.get(sort, "created_at")
        direction = "DESC" if descending else "ASC"
        rows = self._checks_query(
            self._cph(
                f"SELECT {', '.join(_RUN_COLUMNS)} FROM check_runs WHERE {where} "
                f"ORDER BY {column} {direction}, id {direction} LIMIT ? OFFSET ?"
            ),
            (*params, int(limit), int(offset)),
        )
        runs = [run_from_row(row) for row in rows]
        if with_results:
            for run in runs:
                run.results = self.list_check_results(run_key=run.run_key)
        return runs

    def count_check_runs(self, filters: dict[str, Any] | None = None) -> int:
        where, params = self._run_filters(filters)
        rows = self._checks_query(
            self._cph(f"SELECT COUNT(*) FROM check_runs WHERE {where}"), tuple(params)
        )
        return int(rows[0][0] or 0) if rows else 0

    def latest_check_run(
        self, *, pr_number: int, head_sha: str = "", with_results: bool = True
    ) -> CheckRun | None:
        """The most recent run for one pull request, optionally at one commit.

        What a gate reads. Scoped to the head commit when one is given, because
        a decision about *this* commit must not be satisfied by a clean run
        against the commit before it.
        """
        filters: dict[str, Any] = {"pr_number": int(pr_number)}
        if head_sha:
            filters["head_sha"] = head_sha
        runs = self.list_check_runs(filters, limit=1, with_results=with_results)
        return runs[0] if runs else None

    # ---------------------------------------------------------------- results

    def _result_filters(self, filters: dict[str, Any] | None) -> tuple[str, list[Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        active = filters or {}
        for column, key in (
            ("run_key", "run_key"),
            ("check_id", "check_id"),
            ("origin", "origin"),
            ("state", "state"),
            ("mode", "mode"),
            ("platform", "platform"),
            ("owner", "owner"),
            ("repo", "repo"),
            ("head_sha", "head_sha"),
        ):
            value = active.get(key)
            if value:
                clauses.append(f"{column} = ?")
                params.append(value)
        pr_number = active.get("pr_number")
        if pr_number:
            clauses.append("pr_number = ?")
            params.append(int(pr_number))
        if active.get("blocking"):
            clauses.append("blocking = 1")
        if active.get("incomplete"):
            clauses.append("incomplete = 1")
        since = active.get("since")
        if since:
            clauses.append("created_at >= ?")
            params.append(float(since))
        until = active.get("until")
        if until:
            clauses.append("created_at < ?")
            params.append(float(until))
        scope_clause, scope_params = self._checks_scope()
        where = " AND ".join(clauses) if clauses else "1 = 1"
        if scope_clause:
            where += scope_clause
            params.extend(scope_params)
        return where, params

    def list_check_results(
        self,
        filters: dict[str, Any] | None = None,
        *,
        run_key: str = "",
        limit: int = 200,
        offset: int = 0,
        sort: str = "check_id",
        descending: bool = False,
    ) -> list[CheckResult]:
        active = dict(filters or {})
        if run_key:
            active["run_key"] = run_key
        where, params = self._result_filters(active)
        column = RESULT_SORT_COLUMNS.get(sort, "check_id")
        direction = "DESC" if descending else "ASC"
        rows = self._checks_query(
            self._cph(
                f"SELECT {', '.join(_RESULT_COLUMNS)} FROM check_results WHERE {where} "
                f"ORDER BY {column} {direction}, id {direction} LIMIT ? OFFSET ?"
            ),
            (*params, int(limit), int(offset)),
        )
        return [result_from_row(row) for row in rows]

    def count_check_results(self, filters: dict[str, Any] | None = None) -> int:
        where, params = self._result_filters(filters)
        rows = self._checks_query(
            self._cph(f"SELECT COUNT(*) FROM check_results WHERE {where}"), tuple(params)
        )
        return int(rows[0][0] or 0) if rows else 0

    def summarize_check_results(self, filters: dict[str, Any] | None = None) -> list[dict]:
        """Counts by check and state — the framework's headline numbers.

        Per check rather than per run, because the question an operator asks of
        this table is "which check is noisy, and which one keeps failing to
        run" — and both halves of that are invisible in a per-run total.
        """
        where, params = self._result_filters(filters)
        rows = self._checks_query(
            self._cph(
                "SELECT check_id, origin, state, mode, COUNT(*), AVG(duration_seconds), "
                # Summed rather than grouped on: `incomplete` is what a gate
                # reads, and it covers unanswered *skips* — a missing linter, a
                # pending CI run — as well as errors and timeouts. A caller
                # deriving the health number from states alone would undercount
                # it by exactly the cases this phase added.
                "SUM(incomplete) "
                f"FROM check_results WHERE {where} "
                "GROUP BY check_id, origin, state, mode ORDER BY check_id, state"
            ),
            tuple(params),
        )
        return [
            {
                "check_id": row[0],
                "origin": row[1],
                "state": row[2],
                "mode": row[3],
                "count": int(row[4] or 0),
                "average_duration": round(float(row[5] or 0.0), 4),
                "incomplete": int(row[6] or 0),
            }
            for row in rows
        ]
