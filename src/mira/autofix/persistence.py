"""Storage and the durable queue for autofix — one implementation.

SQLite keeps a database per repository and Postgres keeps one for the whole
install, but the autofix tables carry the same columns in both, so the queries
are written once here and mixed into both stores. Parity is then a property of
the code rather than a promise in a docstring: there is no second copy to
drift.

**The queue is a table, not a broker.** There is no Redis, no AMQP and nothing
else to install: a job is a row, a worker takes a lease on it, and the lease
expiring is what makes a crashed worker's job available again. That is the
whole mechanism, and it is chosen because the deployment profile this project
targets is one container on a small board — a queue that needs a second service
is a queue that will not be running when the fix is requested.

Each backend supplies five primitives — a placeholder, a read, a write, its
spelling of "insert unless it already exists", and its spelling of "take one
row nobody else is taking" — and nothing else. The last one is separate
precisely because it is the one statement the two engines cannot share:
Postgres needs ``FOR UPDATE SKIP LOCKED`` to keep two workers off one row, and
SQLite, which serialises writers anyway, must not be handed syntax it does not
have.
"""

from __future__ import annotations

import time
from typing import Any

from mira.autofix.models import (
    LEASED_STATES,
    TERMINAL_STATES,
    AutofixAttempt,
    AutofixJob,
    Reason,
    ValidationResult,
    dumps,
    reasons_from_json,
    validation_from_json,
)
from mira.db.sqltext import like_prefix

_JOB_COLUMNS = (
    "id",
    "job_key",
    "state",
    "mode",
    "request_kind",
    "platform",
    "owner",
    "repo",
    "pr_number",
    "pr_url",
    "base_branch",
    "head_branch",
    "head_sha",
    "finding_id",
    "finding_title",
    "requested_by",
    "request_id",
    "policy_version",
    "attempts",
    "max_attempts",
    "ci_attempts",
    "max_ci_attempts",
    "available_at",
    "lease_owner",
    "lease_expires_at",
    "branch_name",
    "commit_sha",
    "child_pr_url",
    "child_pr_number",
    "model",
    "patch_digest",
    "diff",
    "reasons_json",
    "validation_json",
    "handoff_ref",
    "cancelled_by",
    "error",
    "created_at",
    "updated_at",
)

_ATTEMPT_COLUMNS = (
    "id",
    "job_id",
    "job_key",
    "attempt",
    "phase",
    "outcome",
    "model",
    "prompt_digest",
    "patch_digest",
    "diff",
    "reasons_json",
    "validation_json",
    "detail",
    "duration_seconds",
    "created_at",
)

# Sortable/filterable columns exposed to the API. An allowlist, because the
# value arrives from a query string.
JOB_SORT_COLUMNS = {
    "created_at": "created_at",
    "updated_at": "updated_at",
    "pr_number": "pr_number",
    "state": "state",
    "attempts": "attempts",
}


def job_from_row(row: tuple) -> AutofixJob:
    """Rehydrate a job from either backend's row tuple."""
    data = dict(zip(_JOB_COLUMNS, row, strict=False))
    return AutofixJob(
        id=int(data.get("id") or 0),
        job_key=str(data.get("job_key") or ""),
        state=str(data.get("state") or "queued"),  # type: ignore[arg-type]
        mode=str(data.get("mode") or "branch_pr"),  # type: ignore[arg-type]
        request_kind=str(data.get("request_kind") or "single"),  # type: ignore[arg-type]
        platform=str(data.get("platform") or "github"),
        owner=str(data.get("owner") or ""),
        repo=str(data.get("repo") or ""),
        pr_number=int(data.get("pr_number") or 0),
        pr_url=str(data.get("pr_url") or ""),
        base_branch=str(data.get("base_branch") or ""),
        head_branch=str(data.get("head_branch") or ""),
        head_sha=str(data.get("head_sha") or ""),
        finding_id=str(data.get("finding_id") or ""),
        finding_title=str(data.get("finding_title") or ""),
        requested_by=str(data.get("requested_by") or ""),
        request_id=str(data.get("request_id") or ""),
        policy_version=str(data.get("policy_version") or ""),
        attempts=int(data.get("attempts") or 0),
        max_attempts=int(data.get("max_attempts") or 1),
        ci_attempts=int(data.get("ci_attempts") or 0),
        max_ci_attempts=int(data.get("max_ci_attempts") or 0),
        available_at=float(data.get("available_at") or 0.0),
        lease_owner=str(data.get("lease_owner") or ""),
        lease_expires_at=float(data.get("lease_expires_at") or 0.0),
        branch_name=str(data.get("branch_name") or ""),
        commit_sha=str(data.get("commit_sha") or ""),
        child_pr_url=str(data.get("child_pr_url") or ""),
        child_pr_number=int(data.get("child_pr_number") or 0),
        model=str(data.get("model") or ""),
        patch_digest=str(data.get("patch_digest") or ""),
        diff=str(data.get("diff") or ""),
        reasons=reasons_from_json(str(data.get("reasons_json") or "[]")),
        validation=validation_from_json(str(data.get("validation_json") or "{}")),
        handoff_ref=str(data.get("handoff_ref") or ""),
        cancelled_by=str(data.get("cancelled_by") or ""),
        error=str(data.get("error") or ""),
        created_at=float(data.get("created_at") or 0.0),
        updated_at=float(data.get("updated_at") or 0.0),
    )


def attempt_from_row(row: tuple) -> AutofixAttempt:
    data = dict(zip(_ATTEMPT_COLUMNS, row, strict=False))
    return AutofixAttempt(
        id=int(data.get("id") or 0),
        job_id=int(data.get("job_id") or 0),
        job_key=str(data.get("job_key") or ""),
        attempt=int(data.get("attempt") or 0),
        phase=str(data.get("phase") or ""),
        outcome=str(data.get("outcome") or ""),
        model=str(data.get("model") or ""),
        prompt_digest=str(data.get("prompt_digest") or ""),
        patch_digest=str(data.get("patch_digest") or ""),
        diff=str(data.get("diff") or ""),
        reasons=reasons_from_json(str(data.get("reasons_json") or "[]")),
        validation=validation_from_json(str(data.get("validation_json") or "{}")),
        detail=str(data.get("detail") or ""),
        duration_seconds=float(data.get("duration_seconds") or 0.0),
        created_at=float(data.get("created_at") or 0.0),
    )


class AutofixStoreMixin:
    """Autofix persistence shared verbatim by the SQLite and Postgres stores."""

    # Backends override these.
    _autofix_placeholder = "?"
    _autofix_insert_ignore = "INSERT OR IGNORE INTO"

    def _autofix_query(self, sql: str, params: tuple = ()) -> list[tuple]:
        raise NotImplementedError  # pragma: no cover - backends implement this

    def _autofix_exec(self, sql: str, params: tuple = ()) -> int:
        raise NotImplementedError  # pragma: no cover - backends implement this

    def _autofix_exec_guarded(self, sql: str, params: tuple = (), *, lock_key: str) -> int:
        """Run an admission-control write with writers for one repository serialised.

        SQLite needs nothing extra: a write takes the database lock, so the
        capacity subquery and the insert that depends on it cannot interleave
        with another writer's. Postgres overrides this, because there a
        statement takes a snapshot rather than a lock — two `INSERT … SELECT …
        WHERE (SELECT COUNT(*)) < n` running at once would both read the same
        free capacity and both fill it, which is exactly the ceiling not
        holding. ``lock_key`` names the repository the write belongs to, so
        contention is between requests for the same repository and nowhere else.
        """
        return self._autofix_exec(sql, params)

    def _autofix_claim_ids(self, sql: str, params: tuple) -> list[int]:
        """Pick one claimable job id, in whatever way this engine keeps two
        workers off the same row.

        Postgres adds ``FOR UPDATE SKIP LOCKED`` and runs it inside the
        claiming transaction; SQLite runs it plainly, because its writers are
        already serialised and the conditional UPDATE that follows is the
        arbiter either way.
        """
        return [int(row[0]) for row in self._autofix_query(sql, params)]

    # ------------------------------------------------------------------ util

    def _ph(self, sql: str) -> str:
        return sql.replace("?", self._autofix_placeholder)

    def _autofix_scope(self) -> tuple[str, tuple[Any, ...]]:
        """Extra WHERE clause pinning reads to this store's repository.

        SQLite has one file per repository so its rows are already scoped;
        Postgres shares one table across the install and overrides this. An
        empty owner/repo is the deliberate org-wide handle.
        """
        return "", ()

    def _autofix_owner(self) -> str:
        """The owner this store scopes its reads on.

        It **wins** over the owner carried on a job, and that ordering is
        load-bearing on Postgres: `IndexStore.open` namespaces a non-GitHub
        owner as ``_{platform}/{owner}`` and hands that spelling to the store,
        while a job carries the plain one. Writing the plain owner and reading
        with the namespaced scope means the row is invisible to the store that
        wrote it — so on Postgres a GitLab or Forgejo job would be enqueued and
        then never claimed by anybody. SQLite is unaffected either way: it gets
        the plain owner and its file already scopes the rows.
        """
        return str(getattr(self, "_owner", "") or "")

    def _autofix_repo(self) -> str:
        return str(getattr(self, "_repo", "") or "")

    # -------------------------------------------------------------- findings

    # Column order matches `get_review_finding` in both stores, so the tuple a
    # backend hands back rehydrates the same way here as it does there.
    _FINDING_COLUMNS = (
        "id, fingerprint, review_id, platform, owner, repo, pr_number, pr_url, "
        "base_sha, head_sha, path, start_line, end_line, symbol, category, severity, "
        "confidence, title, body, suggestion, detector, prompt_model, "
        "platform_comment_id, platform_thread_id, state, created_at, updated_at"
    )

    def list_review_findings(
        self,
        *,
        pr_number: int = 0,
        include_closed: bool = False,
        state: str = "",
        category: str = "",
        severity: str = "",
        path_prefix: str = "",
        limit: int = 200,
        offset: int = 0,
    ) -> list[Any]:
        """Findings, newest first, narrowed by whatever was asked for.

        Lives here rather than beside `get_review_finding` because ``fix all``
        was the first caller that needed a *set* of findings, and putting the
        query in a shared mixin is what keeps the two backends answering it
        identically. The read-only MCP surface is the second caller, which is
        why the filters and the offset exist: one query with more ways to narrow
        it beats two queries that can disagree about scope, ordering or which
        states count as closed.

        `outdated` counts as open on purpose: it means the diff moved past the
        anchored line, which is what an unaddressed finding looks like after a
        rebase — not evidence that anybody dealt with it.

        Ordered by `created_at DESC, id DESC`. The timestamp alone is not a
        total order — a review records its findings in one pass and they can
        share a second — and a page boundary falling inside a tie would repeat
        or drop rows.
        """
        from mira.feedback.models import ReviewFinding

        clauses: list[str] = []
        params: list[Any] = []
        if pr_number:
            clauses.append("pr_number = ?")
            params.append(int(pr_number))
        if not include_closed:
            closed = ("fixed", "resolved", "dismissed")
            clauses.append(f"state NOT IN ({', '.join('?' for _ in closed)})")
            params.extend(closed)
        for column, value in (("state", state), ("category", category), ("severity", severity)):
            if value:
                clauses.append(f"{column} = ?")
                params.append(value)
        if path_prefix:
            clauses.append("path LIKE ? ESCAPE '\\'")
            params.append(like_prefix(path_prefix))
        scope_clause, scope_params = self._autofix_scope()
        # `_autofix_scope` returns a clause that begins with " AND ", so there
        # has to be something for it to be joined to. An unfiltered listing is
        # a real request from the MCP surface, not a caller that forgot.
        where = " AND ".join(clauses or ["1=1"]) + scope_clause
        params.extend(scope_params)
        rows = self._autofix_query(
            self._ph(
                f"SELECT {self._FINDING_COLUMNS} FROM review_findings WHERE {where} "
                "ORDER BY created_at DESC, id DESC LIMIT ? OFFSET ?"
            ),
            (*params, int(limit), max(0, int(offset))),
        )
        return [ReviewFinding(*row) for row in rows]

    # -------------------------------------------------------------- enqueue

    def enqueue_autofix_job(
        self, job: AutofixJob, *, max_active: int = 0
    ) -> tuple[AutofixJob, bool]:
        """Persist one job. Returns ``(stored, created)``.

        Insert-only by design. An existing row with the same key describes the
        same finding on the same commit under the same delivery mode, so
        re-enqueuing it would at best rewrite it with itself — and at worst
        re-arm a job that has already opened a pull request, or resurrect one
        an admin cancelled. The caller gets the stored row back and can see
        from ``created`` whether this request was the first.

        ``max_active`` is the per-repository ceiling, and it is enforced *in the
        insert* rather than by a count the caller took earlier. Counting first
        and inserting later leaves a window several ``await`` points wide, and
        two `fix all` requests arriving together would each read the same free
        capacity and each fill it. Here the count is a subquery: on SQLite the
        write lock makes that exact, and on Postgres it narrows the window from
        the length of a request to the length of one statement. Zero means no
        ceiling, which is what every call that is not admission control passes.
        """
        now = time.time()
        created_at = job.created_at or now
        params = (
            job.job_key,
            job.state,
            job.mode,
            job.request_kind,
            job.platform,
            self._autofix_owner() or job.owner,
            self._autofix_repo() or job.repo,
            int(job.pr_number),
            job.pr_url,
            job.base_branch,
            job.head_branch,
            job.head_sha,
            job.finding_id,
            job.finding_title,
            job.requested_by,
            job.request_id,
            job.policy_version,
            int(job.attempts),
            int(job.max_attempts),
            int(job.ci_attempts),
            int(job.max_ci_attempts),
            float(job.available_at or created_at),
            job.model,
            dumps([reason.as_dict() for reason in job.reasons]),
            dumps(job.validation.as_dict()),
            created_at,
            job.updated_at or created_at,
        )
        columns = (
            "(job_key, state, mode, request_kind, platform, owner, repo, pr_number, "
            "pr_url, base_branch, head_branch, head_sha, finding_id, finding_title, "
            "requested_by, request_id, policy_version, attempts, max_attempts, "
            "ci_attempts, max_ci_attempts, available_at, model, reasons_json, "
            "validation_json, created_at, updated_at)"
        )
        values = ", ".join("?" for _ in params)
        if max_active > 0:
            owner = self._autofix_owner() or job.owner
            repo = self._autofix_repo() or job.repo
            room_sql, room_params = self._autofix_room_clause(owner=owner, repo=repo)
            inserted = self._autofix_exec_guarded(
                self._ph(
                    f"{self._autofix_insert_ignore} autofix_jobs {columns} "
                    f"SELECT {values} WHERE ({room_sql}) < ?"
                ),
                (*params, *room_params, int(max_active)),
                lock_key=f"autofix:{job.platform}:{owner}/{repo}",
            )
        else:
            inserted = self._autofix_exec(
                self._ph(f"{self._autofix_insert_ignore} autofix_jobs {columns} VALUES ({values})"),
                params,
            )
        stored = self.get_autofix_job(job.job_key)
        if stored is None:
            # No row and no conflict: the ceiling refused the insert. The job
            # handed back is the one that was *not* persisted, and its `id` of
            # zero is how the caller tells "already there" from "no room".
            return job, bool(inserted)
        return stored, bool(inserted)

    # ------------------------------------------------------------------ read

    def get_autofix_job(self, job_key: str) -> AutofixJob | None:
        clause, scope_params = self._autofix_scope()
        rows = self._autofix_query(
            self._ph(
                f"SELECT {', '.join(_JOB_COLUMNS)} FROM autofix_jobs WHERE job_key = ?{clause}"
            ),
            (job_key, *scope_params),
        )
        return job_from_row(rows[0]) if rows else None

    def get_autofix_job_by_id(self, job_id: int) -> AutofixJob | None:
        clause, scope_params = self._autofix_scope()
        rows = self._autofix_query(
            self._ph(f"SELECT {', '.join(_JOB_COLUMNS)} FROM autofix_jobs WHERE id = ?{clause}"),
            (int(job_id), *scope_params),
        )
        return job_from_row(rows[0]) if rows else None

    def _job_filters(self, filters: dict[str, Any] | None) -> tuple[str, list[Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        active = filters or {}
        for column, key in (
            ("state", "state"),
            ("mode", "mode"),
            ("platform", "platform"),
            ("owner", "owner"),
            ("repo", "repo"),
            ("requested_by", "requested_by"),
            ("finding_id", "finding_id"),
            ("request_id", "request_id"),
            ("head_sha", "head_sha"),
            ("request_kind", "request_kind"),
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
        states = active.get("states")
        if states:
            placeholders = ", ".join("?" for _ in states)
            clauses.append(f"state IN ({placeholders})")
            params.extend(states)
        scope_clause, scope_params = self._autofix_scope()
        where = " AND ".join(clauses) if clauses else "1 = 1"
        if scope_clause:
            # `_autofix_scope` renders as " AND owner = ? AND repo = ?".
            where = where + scope_clause
            params.extend(scope_params)
        return where, params

    def list_autofix_jobs(
        self,
        filters: dict[str, Any] | None = None,
        *,
        limit: int = 50,
        offset: int = 0,
        sort: str = "created_at",
        descending: bool = True,
    ) -> list[AutofixJob]:
        where, params = self._job_filters(filters)
        column = JOB_SORT_COLUMNS.get(sort, "created_at")
        direction = "DESC" if descending else "ASC"
        rows = self._autofix_query(
            self._ph(
                f"SELECT {', '.join(_JOB_COLUMNS)} FROM autofix_jobs WHERE {where} "
                f"ORDER BY {column} {direction}, id {direction} LIMIT ? OFFSET ?"
            ),
            (*params, int(limit), int(offset)),
        )
        return [job_from_row(row) for row in rows]

    def count_autofix_jobs(self, filters: dict[str, Any] | None = None) -> int:
        where, params = self._job_filters(filters)
        rows = self._autofix_query(
            self._ph(f"SELECT COUNT(*) FROM autofix_jobs WHERE {where}"), tuple(params)
        )
        return int(rows[0][0]) if rows else 0

    def summarize_autofix_jobs(self, filters: dict[str, Any] | None = None) -> list[dict]:
        """Counts by state and mode — what a rollout is watched through."""
        where, params = self._job_filters(filters)
        rows = self._autofix_query(
            self._ph(
                "SELECT state, mode, COUNT(*), SUM(attempts), "
                "SUM(CASE WHEN child_pr_url <> '' THEN 1 ELSE 0 END) "
                f"FROM autofix_jobs WHERE {where} GROUP BY state, mode ORDER BY state, mode"
            ),
            tuple(params),
        )
        return [
            {
                "state": row[0],
                "mode": row[1],
                "count": int(row[2] or 0),
                "attempts": int(row[3] or 0),
                "opened": int(row[4] or 0),
            }
            for row in rows
        ]

    # ----------------------------------------------------------------- queue

    # States that count against the per-repository ceiling: everything a worker
    # might still pick up. `failed` is in there because a failed job with
    # attempts left is queued work wearing a different label.
    _ACTIVE_STATES = ("queued", "running", "validating", "publishing", "failed")

    def _autofix_room_clause(self, *, owner: str = "", repo: str = "") -> tuple[str, tuple]:
        """``(subquery, params)`` counting the jobs that occupy the ceiling.

        Shared by the standalone count and by the conditional insert, so a
        change to what "active" means cannot apply to one and not the other.
        """
        placeholders = ", ".join("?" for _ in self._ACTIVE_STATES)
        clauses = [f"state IN ({placeholders})"]
        params: list[Any] = list(self._ACTIVE_STATES)
        if owner:
            clauses.append("owner = ?")
            params.append(owner)
        if repo:
            clauses.append("repo = ?")
            params.append(repo)
        scope_clause, scope_params = self._autofix_scope()
        where = " AND ".join(clauses) + scope_clause
        params.extend(scope_params)
        return f"SELECT COUNT(*) FROM autofix_jobs WHERE {where}", tuple(params)

    def count_active_autofix_jobs(self, *, owner: str = "", repo: str = "") -> int:
        """Jobs neither finished nor abandoned, for the per-repository ceiling."""
        sql, params = self._autofix_room_clause(owner=owner, repo=repo)
        rows = self._autofix_query(self._ph(sql), params)
        return int(rows[0][0]) if rows else 0

    def claim_autofix_job(
        self, *, worker: str, lease_seconds: float, now: float | None = None
    ) -> AutofixJob | None:
        """Take the lease on one runnable job, or return None.

        Runnable means one of two things, and the second is the whole reason a
        lease exists: a job that is `queued` (or `failed` and past its backoff)
        and due, **or** a job whose worker took a lease and never came back.
        The expired lease is what turns a crashed process from a stuck job into
        a retried one — nothing has to notice the crash, because the deadline
        passing *is* noticing it.

        The claim is a conditional UPDATE, so two workers that pick the same id
        cannot both win: the second one updates zero rows and looks again.
        """
        moment = time.time() if now is None else now
        deadline = moment + max(1.0, float(lease_seconds))
        clause, scope_params = self._autofix_scope()
        candidates = self._autofix_claim_ids(
            self._ph(
                "SELECT id FROM autofix_jobs WHERE "
                "((state IN ('queued', 'failed') AND available_at <= ?) "
                " OR (state IN ('running', 'validating', 'publishing') AND lease_expires_at < ?)) "
                f"AND attempts < max_attempts{clause} "
                "ORDER BY available_at ASC, id ASC LIMIT 5"
            ),
            (moment, moment, *scope_params),
        )
        for job_id in candidates:
            claimed = self._autofix_exec(
                self._ph(
                    "UPDATE autofix_jobs SET state = 'running', lease_owner = ?, "
                    "lease_expires_at = ?, attempts = attempts + 1, updated_at = ? "
                    "WHERE id = ? AND attempts < max_attempts AND "
                    "((state IN ('queued', 'failed') AND available_at <= ?) "
                    " OR (state IN ('running', 'validating', 'publishing') "
                    "     AND lease_expires_at < ?))"
                ),
                (worker, deadline, moment, job_id, moment, moment),
            )
            if claimed:
                return self.get_autofix_job_by_id(job_id)
        return None

    def renew_autofix_lease(self, job_key: str, *, worker: str, lease_seconds: float) -> bool:
        """Extend a lease this worker still holds. False if it lost it."""
        now = time.time()
        renewed = self._autofix_exec(
            self._ph(
                "UPDATE autofix_jobs SET lease_expires_at = ?, updated_at = ? "
                "WHERE job_key = ? AND lease_owner = ?"
            ),
            (now + max(1.0, float(lease_seconds)), now, job_key, worker),
        )
        return bool(renewed)

    def release_autofix_lease(self, job_key: str, *, worker: str = "") -> None:
        """Hand a job back to the queue without consuming another attempt."""
        now = time.time()
        clauses = ["job_key = ?"]
        params: list[Any] = [job_key]
        if worker:
            clauses.append("lease_owner = ?")
            params.append(worker)
        self._autofix_exec(
            self._ph(
                "UPDATE autofix_jobs SET state = 'queued', lease_owner = '', "
                "lease_expires_at = 0, updated_at = ? "
                f"WHERE {' AND '.join(clauses)}"
            ),
            (now, *params),
        )

    def reap_expired_autofix_leases(self, *, now: float | None = None) -> int:
        """Move every abandoned lease back to `queued`. Returns how many.

        `claim_autofix_job` already picks these up, so this is bookkeeping
        rather than recovery: it exists so a dashboard shows an abandoned job
        as waiting instead of as running forever, and so an operator can see
        that a worker died without reading a log.
        """
        moment = time.time() if now is None else now
        placeholders = ", ".join("?" for _ in sorted(LEASED_STATES))
        return self._autofix_exec(
            self._ph(
                "UPDATE autofix_jobs SET state = 'queued', lease_owner = '', "
                "lease_expires_at = 0, updated_at = ? "
                f"WHERE state IN ({placeholders}) AND lease_expires_at > 0 "
                "AND lease_expires_at < ? AND attempts < max_attempts"
            ),
            (moment, *sorted(LEASED_STATES), moment),
        )

    # ---------------------------------------------------------------- update

    def update_autofix_job(
        self,
        job_key: str,
        *,
        state: str | None = None,
        reasons: list[Reason] | None = None,
        validation: ValidationResult | None = None,
        branch_name: str | None = None,
        commit_sha: str | None = None,
        child_pr_url: str | None = None,
        child_pr_number: int | None = None,
        model: str | None = None,
        patch_digest: str | None = None,
        diff: str | None = None,
        handoff_ref: str | None = None,
        error: str | None = None,
        available_at: float | None = None,
        clear_lease: bool = False,
        bump_ci_attempts: bool = False,
        extra_attempts: int = 0,
        cancelled_by: str | None = None,
    ) -> AutofixJob | None:
        """Move one job forward, writing only what the caller named.

        Every field is optional and ``None`` means "leave it": a publish step
        that records a branch must not blank the diff a generate step stored,
        and a validation failure must not erase the branch a previous attempt
        legitimately created.
        """
        sets = ["updated_at = ?"]
        params: list[Any] = [time.time()]

        def _set(column: str, value: Any) -> None:
            sets.append(f"{column} = ?")
            params.append(value)

        if state is not None:
            _set("state", state)
        if reasons is not None:
            _set("reasons_json", dumps([reason.as_dict() for reason in reasons]))
        if validation is not None:
            _set("validation_json", dumps(validation.as_dict()))
        if branch_name is not None:
            _set("branch_name", branch_name)
        if commit_sha is not None:
            _set("commit_sha", commit_sha)
        if child_pr_url is not None:
            _set("child_pr_url", child_pr_url)
        if child_pr_number is not None:
            _set("child_pr_number", int(child_pr_number))
        if model is not None:
            _set("model", model)
        if patch_digest is not None:
            _set("patch_digest", patch_digest)
        if diff is not None:
            _set("diff", diff)
        if handoff_ref is not None:
            _set("handoff_ref", handoff_ref)
        if error is not None:
            _set("error", error)
        if available_at is not None:
            _set("available_at", float(available_at))
        if cancelled_by is not None:
            _set("cancelled_by", cancelled_by)
        if clear_lease:
            sets.extend(("lease_owner = ?", "lease_expires_at = ?"))
            params.extend(("", 0.0))
        if bump_ci_attempts:
            sets.append("ci_attempts = ci_attempts + 1")
        if extra_attempts:
            # Raises the ceiling rather than resetting the counter, so the
            # attempts already spent stay visible in the audit trail. A job
            # requeued after a red CI run needs a fresh attempt to be claimable
            # at all, and rewriting `attempts` to zero would erase the evidence
            # that it has been round twice.
            sets.append("max_attempts = max_attempts + ?")
            params.append(int(extra_attempts))

        clause, scope_params = self._autofix_scope()
        # `cancelled` is final, and it is made final *here* rather than by every
        # caller remembering to check. A worker that was already generating when
        # an admin cancelled will still try to record its progress, and a
        # read-then-write in the worker cannot win that race — an update that
        # matches no row can.
        self._autofix_exec(
            self._ph(
                f"UPDATE autofix_jobs SET {', '.join(sets)} "
                f"WHERE job_key = ? AND state <> 'cancelled'{clause}"
            ),
            (*params, job_key, *scope_params),
        )
        return self.get_autofix_job(job_key)

    def cancel_autofix_job(self, job_key: str, *, actor: str, reason: str) -> AutofixJob | None:
        """Stop a job, unless it has already finished.

        Conditional on the state in SQL rather than read-then-write, so a
        cancellation racing a worker's final update cannot rewrite an `opened`
        job into a `cancelled` one and lose the pull request it produced.
        """
        now = time.time()
        placeholders = ", ".join("?" for _ in sorted(TERMINAL_STATES))
        clause, scope_params = self._autofix_scope()
        self._autofix_exec(
            self._ph(
                "UPDATE autofix_jobs SET state = 'cancelled', cancelled_by = ?, error = ?, "
                "lease_owner = '', lease_expires_at = 0, updated_at = ? "
                f"WHERE job_key = ? AND state NOT IN ({placeholders}){clause}"
            ),
            (actor, reason, now, job_key, *sorted(TERMINAL_STATES), *scope_params),
        )
        return self.get_autofix_job(job_key)

    def dead_letter_autofix_job(
        self, job_key: str, *, reasons: list[Reason], error: str
    ) -> AutofixJob | None:
        """Park a job nobody should retry, with why."""
        return self.update_autofix_job(
            job_key,
            state="dead_letter",
            reasons=reasons,
            error=error,
            clear_lease=True,
        )

    # -------------------------------------------------------------- attempts

    def record_autofix_attempt(self, attempt: AutofixAttempt) -> AutofixAttempt:
        """Append one attempt to the audit trail."""
        now = attempt.created_at or time.time()
        self._autofix_exec(
            self._ph(
                "INSERT INTO autofix_attempts "
                "(job_id, job_key, attempt, phase, outcome, model, prompt_digest, "
                "patch_digest, diff, reasons_json, validation_json, detail, "
                "duration_seconds, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
            ),
            (
                int(attempt.job_id),
                attempt.job_key,
                int(attempt.attempt),
                attempt.phase,
                attempt.outcome,
                attempt.model,
                attempt.prompt_digest,
                attempt.patch_digest,
                attempt.diff,
                dumps([reason.as_dict() for reason in attempt.reasons]),
                dumps(attempt.validation.as_dict()),
                attempt.detail,
                float(attempt.duration_seconds),
                now,
            ),
        )
        attempt.created_at = now
        return attempt

    def list_autofix_attempts(
        self, *, job_key: str = "", job_id: int = 0, limit: int = 100, offset: int = 0
    ) -> list[AutofixAttempt]:
        clauses: list[str] = []
        params: list[Any] = []
        if job_key:
            clauses.append("job_key = ?")
            params.append(job_key)
        if job_id:
            clauses.append("job_id = ?")
            params.append(int(job_id))
        where = " AND ".join(clauses) if clauses else "1 = 1"
        rows = self._autofix_query(
            self._ph(
                f"SELECT {', '.join(_ATTEMPT_COLUMNS)} FROM autofix_attempts WHERE {where} "
                "ORDER BY created_at ASC, id ASC LIMIT ? OFFSET ?"
            ),
            (*params, int(limit), int(offset)),
        )
        return [attempt_from_row(row) for row in rows]
