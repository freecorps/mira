"""Storage for gate decisions, deliveries and overrides — one implementation.

SQLite keeps a database per repository and Postgres keeps one for the whole
install, but the gate's tables carry the same columns in both, so the queries
are written once here and mixed into both stores. Parity is then a property of
the code rather than a promise in a docstring: there is no second copy to
drift.

Each backend supplies four primitives — a placeholder, a read, a write, and its
spelling of "insert unless it already exists" — and nothing else.
"""

from __future__ import annotations

import time
from typing import Any

from mira.gate.models import (
    CIState,
    GateDecision,
    GateInputs,
    Reason,
    RiskFactor,
    dumps,
    loads,
)

_DECISION_COLUMNS = (
    "id",
    "decision_key",
    "platform",
    "owner",
    "repo",
    "pr_number",
    "pr_url",
    "pr_author",
    "base_branch",
    "head_sha",
    "review_id",
    "mode",
    "state",
    "risk_score",
    "risk_band",
    "policy_version",
    "request_changes",
    "inputs_json",
    "factors_json",
    "reasons_json",
    "capabilities_json",
    "delivery_state",
    "delivery_ref",
    "delivery_attempts",
    "error",
    "overridden_by",
    "created_at",
    "updated_at",
)

_OVERRIDE_COLUMNS = (
    "id",
    "override_key",
    "decision_id",
    "decision_key",
    "platform",
    "owner",
    "repo",
    "pr_number",
    "head_sha",
    "actor",
    "reason",
    "previous_state",
    "new_state",
    "previous_risk",
    "detail_json",
    "created_at",
)

# Sortable/filterable columns exposed to the API. An allowlist, because the
# value arrives from a query string.
DECISION_SORT_COLUMNS = {
    "created_at": "created_at",
    "risk_score": "risk_score",
    "pr_number": "pr_number",
    "state": "state",
}


def _inputs_from_json(blob: str) -> GateInputs:
    data = loads(blob, {})
    if not isinstance(data, dict):
        return GateInputs()
    ci_data = data.pop("ci", {}) or {}
    known = {field for field in GateInputs.__dataclass_fields__ if field != "ci"}
    inputs = GateInputs(**{key: value for key, value in data.items() if key in known})
    if isinstance(ci_data, dict):
        inputs.ci = CIState(
            state=str(ci_data.get("state", "unknown")),
            total=int(ci_data.get("total", 0) or 0),
            failing=list(ci_data.get("failing") or []),
            pending=list(ci_data.get("pending") or []),
        )
    return inputs


def decision_from_row(row: tuple) -> GateDecision:
    """Rehydrate a decision from either backend's row tuple."""
    data = dict(zip(_DECISION_COLUMNS, row, strict=False))
    decision = GateDecision(
        id=int(data.get("id") or 0),
        decision_key=str(data.get("decision_key") or ""),
        state=str(data.get("state") or "skipped"),  # type: ignore[arg-type]
        mode=str(data.get("mode") or "off"),  # type: ignore[arg-type]
        risk_score=int(data.get("risk_score") or 0),
        risk_band=str(data.get("risk_band") or "low"),
        policy_version=str(data.get("policy_version") or ""),
        request_changes=bool(data.get("request_changes")),
        delivery_state=str(data.get("delivery_state") or "not_attempted"),
        delivery_ref=str(data.get("delivery_ref") or ""),
        delivery_attempts=int(data.get("delivery_attempts") or 0),
        error=str(data.get("error") or ""),
        overridden_by=str(data.get("overridden_by") or ""),
        created_at=float(data.get("created_at") or 0.0),
        updated_at=float(data.get("updated_at") or 0.0),
    )
    decision.inputs = _inputs_from_json(str(data.get("inputs_json") or "{}"))
    # Columns win over the inputs blob: they are what the queries filter on, so
    # a row that somehow disagrees with its own payload still lists correctly.
    decision.inputs.platform = str(data.get("platform") or decision.inputs.platform)
    decision.inputs.owner = str(data.get("owner") or decision.inputs.owner)
    decision.inputs.repo = str(data.get("repo") or decision.inputs.repo)
    decision.inputs.pr_number = int(data.get("pr_number") or decision.inputs.pr_number)
    decision.inputs.pr_url = str(data.get("pr_url") or decision.inputs.pr_url)
    decision.inputs.pr_author = str(data.get("pr_author") or decision.inputs.pr_author)
    decision.inputs.head_sha = str(data.get("head_sha") or decision.inputs.head_sha)
    decision.inputs.review_id = int(data.get("review_id") or decision.inputs.review_id)
    decision.reasons = [
        Reason(
            code=str(item.get("code", "")),
            message=str(item.get("message", "")),
            kind=str(item.get("kind", "block")),
        )
        for item in loads(str(data.get("reasons_json") or "[]"), [])
        if isinstance(item, dict)
    ]
    decision.factors = [
        RiskFactor(
            code=str(item.get("code", "")),
            label=str(item.get("label", "")),
            points=int(item.get("points", 0) or 0),
            detail=str(item.get("detail", "")),
        )
        for item in loads(str(data.get("factors_json") or "[]"), [])
        if isinstance(item, dict)
    ]
    capabilities = loads(str(data.get("capabilities_json") or "{}"), {})
    decision.capabilities = capabilities if isinstance(capabilities, dict) else {}
    return decision


def override_from_row(row: tuple) -> dict[str, Any]:
    data = dict(zip(_OVERRIDE_COLUMNS, row, strict=False))
    detail = loads(str(data.get("detail_json") or "{}"), {})
    data["detail"] = detail if isinstance(detail, dict) else {}
    data.pop("detail_json", None)
    return data


class GateStoreMixin:
    """Gate persistence shared verbatim by the SQLite and Postgres stores."""

    # Backends override these three.
    _gate_placeholder = "?"
    _gate_insert_ignore = "INSERT OR IGNORE INTO"

    def _gate_query(self, sql: str, params: tuple = ()) -> list[tuple]:
        raise NotImplementedError  # pragma: no cover - backends implement this

    def _gate_exec(self, sql: str, params: tuple = ()) -> int:
        raise NotImplementedError  # pragma: no cover - backends implement this

    # ------------------------------------------------------------------ util

    def _ph(self, sql: str) -> str:
        return sql.replace("?", self._gate_placeholder)

    def _gate_scope(self) -> tuple[str, tuple[Any, ...]]:
        """Extra WHERE clause pinning reads to this store's repository.

        SQLite has one file per repository so its rows are already scoped;
        Postgres shares one table across the install and overrides this. An
        empty owner/repo is the deliberate org-wide handle.
        """
        return "", ()

    def _gate_owner(self) -> str:
        return str(getattr(self, "_owner", "") or "")

    def _gate_repo(self) -> str:
        return str(getattr(self, "_repo", "") or "")

    # ---------------------------------------------------------------- findings

    # A finding is "open" unless something explicitly closed it. `outdated` is
    # deliberately *not* closed: it only means the diff moved past the line the
    # comment was anchored to, which is exactly what an unaddressed blocker
    # looks like after a rebase. Phase 3 refuses to count `outdated` as
    # addressed for the same reason, and the gate cannot afford to be laxer
    # than the analytics.
    _CLOSED_FINDING_STATES = ("fixed", "resolved", "dismissed")

    def gate_finding_counts(self, pr_number: int) -> dict[str, Any]:
        """Open findings for one PR, bucketed by severity and category.

        Counts every round, not just the current head commit: a blocker raised
        two pushes ago and never resolved is still open, and a gate that only
        looked at the newest round would approve a PR by out-waiting it.
        """
        placeholders = ", ".join("?" for _ in self._CLOSED_FINDING_STATES)
        clause, scope_params = self._gate_scope()
        rows = self._gate_query(
            self._ph(
                "SELECT severity, category, COUNT(*) FROM review_findings "
                f"WHERE pr_number = ? AND state NOT IN ({placeholders})"
                f"{clause} GROUP BY severity, category"
            ),
            (int(pr_number), *self._CLOSED_FINDING_STATES, *scope_params),
        )
        counts = {
            "blockers": 0,
            "warnings": 0,
            "suggestions": 0,
            "security": 0,
            "open": 0,
            "worst": "",
        }
        rank = {"blocker": 4, "warning": 3, "suggestion": 2, "nitpick": 1}
        worst_rank = 0
        for severity, category, count in rows:
            severity = str(severity or "").lower()
            count = int(count or 0)
            counts["open"] += count
            if severity == "blocker":
                counts["blockers"] += count
            elif severity == "warning":
                counts["warnings"] += count
            else:
                counts["suggestions"] += count
            if str(category or "").lower() == "security":
                counts["security"] += count
            if rank.get(severity, 0) > worst_rank:
                worst_rank = rank[severity]
                counts["worst"] = severity
        return counts

    # -------------------------------------------------------------- decisions

    def record_gate_decision(self, decision: GateDecision) -> tuple[GateDecision, bool]:
        """Persist one decision. Returns ``(stored, created)``.

        Insert-only by design. An existing row with the same key was reached
        from the same PR, the same commit, the same policy and the same facts,
        so re-deciding it would at best rewrite it with itself — and at worst
        erase an administrative override or re-arm a delivery that already
        happened. The caller gets the stored row back and can see from
        ``created`` whether this evaluation was the first.
        """
        now = time.time()
        inputs = decision.inputs
        created_at = decision.created_at or now
        params = (
            decision.decision_key,
            inputs.platform,
            inputs.owner or self._gate_owner(),
            inputs.repo or self._gate_repo(),
            inputs.pr_number,
            inputs.pr_url,
            inputs.pr_author,
            inputs.base_branch,
            inputs.head_sha,
            inputs.review_id,
            decision.mode,
            decision.state,
            int(decision.risk_score),
            decision.risk_band,
            decision.policy_version,
            1 if decision.request_changes else 0,
            dumps(inputs.as_dict()),
            dumps([factor.as_dict() for factor in decision.factors]),
            dumps([reason.as_dict() for reason in decision.reasons]),
            dumps(decision.capabilities),
            decision.delivery_state,
            decision.delivery_ref,
            int(decision.delivery_attempts),
            decision.error,
            created_at,
            decision.updated_at or created_at,
        )
        inserted = self._gate_exec(
            self._ph(
                f"{self._gate_insert_ignore} gate_decisions "
                "(decision_key, platform, owner, repo, pr_number, pr_url, pr_author, "
                "base_branch, head_sha, review_id, mode, state, risk_score, risk_band, "
                "policy_version, request_changes, inputs_json, factors_json, reasons_json, "
                "capabilities_json, delivery_state, delivery_ref, delivery_attempts, error, "
                "created_at, updated_at) VALUES "
                "(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
            ),
            params,
        )
        stored = self.get_gate_decision(decision.decision_key)
        if stored is None:  # pragma: no cover - only a vanished row
            return decision, bool(inserted)
        return stored, bool(inserted)

    def get_gate_decision(self, decision_key: str) -> GateDecision | None:
        clause, scope_params = self._gate_scope()
        rows = self._gate_query(
            self._ph(
                f"SELECT {', '.join(_DECISION_COLUMNS)} FROM gate_decisions "
                f"WHERE decision_key = ?{clause}"
            ),
            (decision_key, *scope_params),
        )
        return decision_from_row(rows[0]) if rows else None

    def get_gate_decision_by_id(self, decision_id: int) -> GateDecision | None:
        clause, scope_params = self._gate_scope()
        rows = self._gate_query(
            self._ph(
                f"SELECT {', '.join(_DECISION_COLUMNS)} FROM gate_decisions WHERE id = ?{clause}"
            ),
            (int(decision_id), *scope_params),
        )
        return decision_from_row(rows[0]) if rows else None

    def _decision_filters(self, filters: dict[str, Any] | None) -> tuple[str, list[Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        active = filters or {}
        for column, key in (
            ("state", "state"),
            ("mode", "mode"),
            ("platform", "platform"),
            ("owner", "owner"),
            ("repo", "repo"),
            ("pr_author", "pr_author"),
            ("risk_band", "risk_band"),
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
        since = active.get("since")
        if since:
            clauses.append("created_at >= ?")
            params.append(float(since))
        until = active.get("until")
        if until:
            clauses.append("created_at < ?")
            params.append(float(until))
        scope_clause, scope_params = self._gate_scope()
        where = " AND ".join(clauses) if clauses else "1 = 1"
        if scope_clause:
            # `_gate_scope` renders as " AND owner = ? AND repo = ?".
            where = where + scope_clause
            params.extend(scope_params)
        return where, params

    def list_gate_decisions(
        self,
        filters: dict[str, Any] | None = None,
        *,
        limit: int = 50,
        offset: int = 0,
        sort: str = "created_at",
        descending: bool = True,
    ) -> list[GateDecision]:
        where, params = self._decision_filters(filters)
        column = DECISION_SORT_COLUMNS.get(sort, "created_at")
        direction = "DESC" if descending else "ASC"
        rows = self._gate_query(
            self._ph(
                f"SELECT {', '.join(_DECISION_COLUMNS)} FROM gate_decisions WHERE {where} "
                f"ORDER BY {column} {direction}, id {direction} LIMIT ? OFFSET ?"
            ),
            (*params, int(limit), int(offset)),
        )
        return [decision_from_row(row) for row in rows]

    def count_gate_decisions(self, filters: dict[str, Any] | None = None) -> int:
        where, params = self._decision_filters(filters)
        rows = self._gate_query(
            self._ph(f"SELECT COUNT(*) FROM gate_decisions WHERE {where}"), tuple(params)
        )
        return int(rows[0][0]) if rows else 0

    def summarize_gate_decisions(self, filters: dict[str, Any] | None = None) -> list[dict]:
        """Counts by state and mode — the shadow rollout's headline numbers."""
        where, params = self._decision_filters(filters)
        rows = self._gate_query(
            self._ph(
                "SELECT state, mode, COUNT(*), "
                "SUM(CASE WHEN state = 'approved' THEN 1 ELSE 0 END), "
                "AVG(risk_score) FROM gate_decisions "
                f"WHERE {where} GROUP BY state, mode ORDER BY state, mode"
            ),
            tuple(params),
        )
        return [
            {
                "state": row[0],
                "mode": row[1],
                "count": int(row[2] or 0),
                "approved": int(row[3] or 0),
                "average_risk": round(float(row[4] or 0.0), 2),
            }
            for row in rows
        ]

    def update_gate_decision_delivery(
        self,
        decision_key: str,
        *,
        delivery_state: str,
        delivery_ref: str = "",
        error: str = "",
        state: str | None = None,
        bump_attempts: bool = False,
        reasons: list[Reason] | None = None,
    ) -> None:
        """Record the outcome of a delivery attempt on the decision row.

        ``state`` is only ever moved to ``approved`` by the service, and only
        after the platform confirmed the approval — which is why the transition
        lives here as an explicit parameter rather than being inferred from a
        successful delivery.

        ``reasons`` exists for the same reason: a platform that *refuses* an
        approval has told the decision something new, and a stored decision
        whose reasons predate its own delivery cannot explain itself.
        """
        # An empty `delivery_ref` means "I have none to give", not "forget the
        # one you have": a comment never yields a reference, and a retry where
        # only the comment succeeded must not erase the recorded check-run id.
        sets = [
            "delivery_state = ?",
            "delivery_ref = CASE WHEN ? <> '' THEN ? ELSE delivery_ref END",
            "error = ?",
            "updated_at = ?",
        ]
        params: list[Any] = [delivery_state, delivery_ref, delivery_ref, error, time.time()]
        if bump_attempts:
            sets.append("delivery_attempts = delivery_attempts + 1")
        if state is not None:
            sets.append("state = ?")
            params.append(state)
        if reasons is not None:
            sets.append("reasons_json = ?")
            params.append(dumps([reason.as_dict() for reason in reasons]))
        clause, scope_params = self._gate_scope()
        self._gate_exec(
            self._ph(f"UPDATE gate_decisions SET {', '.join(sets)} WHERE decision_key = ?{clause}"),
            (*params, decision_key, *scope_params),
        )

    # ------------------------------------------------------------- deliveries

    def claim_gate_delivery(
        self,
        *,
        delivery_key: str,
        decision_key: str,
        platform: str,
        owner: str,
        repo: str,
        pr_number: int,
        head_sha: str,
        kind: str,
    ) -> bool:
        """Try to become the one worker that performs this side effect.

        Returns True exactly once per ``delivery_key`` until the attempt fails.
        A redelivered webhook, a concurrent worker and a retried background task
        all race here; the loser does nothing rather than approving a second
        time. A previous *failed* attempt is reclaimable — the failure mode we
        want is "tried again", not "gave up silently".
        """
        now = time.time()
        self._gate_exec(
            self._ph(
                f"{self._gate_insert_ignore} gate_deliveries "
                "(delivery_key, decision_key, platform, owner, repo, pr_number, head_sha, "
                "kind, state, attempts, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'pending', 0, ?, ?)"
            ),
            (
                delivery_key,
                decision_key,
                platform,
                owner or self._gate_owner(),
                repo or self._gate_repo(),
                int(pr_number),
                head_sha,
                kind,
                now,
                now,
            ),
        )
        claimed = self._gate_exec(
            self._ph(
                "UPDATE gate_deliveries SET state = 'in_flight', attempts = attempts + 1, "
                "decision_key = ?, updated_at = ? "
                "WHERE delivery_key = ? AND state IN ('pending', 'failed')"
            ),
            (decision_key, now, delivery_key),
        )
        return claimed > 0

    def finish_gate_delivery(
        self, delivery_key: str, *, state: str, ref: str = "", error: str = ""
    ) -> None:
        self._gate_exec(
            self._ph(
                "UPDATE gate_deliveries SET state = ?, ref = ?, error = ?, updated_at = ? "
                "WHERE delivery_key = ?"
            ),
            (state, ref, error, time.time(), delivery_key),
        )

    def get_gate_delivery(self, delivery_key: str) -> dict[str, Any] | None:
        rows = self._gate_query(
            self._ph(
                "SELECT delivery_key, decision_key, platform, owner, repo, pr_number, "
                "head_sha, kind, state, ref, attempts, error, created_at, updated_at "
                "FROM gate_deliveries WHERE delivery_key = ?"
            ),
            (delivery_key,),
        )
        if not rows:
            return None
        columns = (
            "delivery_key",
            "decision_key",
            "platform",
            "owner",
            "repo",
            "pr_number",
            "head_sha",
            "kind",
            "state",
            "ref",
            "attempts",
            "error",
            "created_at",
            "updated_at",
        )
        return dict(zip(columns, rows[0], strict=False))

    # --------------------------------------------------------------- overrides

    def record_gate_override(
        self,
        *,
        override_key: str,
        decision: GateDecision,
        actor: str,
        reason: str,
        new_state: str,
        detail: dict | None = None,
    ) -> tuple[dict[str, Any] | None, bool]:
        """Append an override to the trail and move the decision.

        Both halves matter. The trail records who, why, from what and to what;
        the decision row carries the new state so the list view does not lie.
        Idempotent on ``override_key``: a retried request records one override,
        and the decision is only moved when the override row is genuinely new.
        """
        now = time.time()
        inserted = self._gate_exec(
            self._ph(
                f"{self._gate_insert_ignore} gate_overrides "
                "(override_key, decision_id, decision_key, platform, owner, repo, pr_number, "
                "head_sha, actor, reason, previous_state, new_state, previous_risk, "
                "detail_json, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
            ),
            (
                override_key,
                int(decision.id),
                decision.decision_key,
                decision.inputs.platform,
                decision.inputs.owner or self._gate_owner(),
                decision.inputs.repo or self._gate_repo(),
                int(decision.inputs.pr_number),
                decision.inputs.head_sha,
                actor,
                reason,
                decision.state,
                new_state,
                int(decision.risk_score),
                dumps(detail or {}),
                now,
            ),
        )
        if inserted:
            clause, scope_params = self._gate_scope()
            self._gate_exec(
                self._ph(
                    "UPDATE gate_decisions SET state = ?, overridden_by = ?, updated_at = ? "
                    f"WHERE decision_key = ?{clause}"
                ),
                (new_state, actor, now, decision.decision_key, *scope_params),
            )
        rows = self._gate_query(
            self._ph(
                f"SELECT {', '.join(_OVERRIDE_COLUMNS)} FROM gate_overrides WHERE override_key = ?"
            ),
            (override_key,),
        )
        return (override_from_row(rows[0]) if rows else None), bool(inserted)

    def list_gate_overrides(
        self,
        *,
        decision_id: int = 0,
        pr_number: int = 0,
        limit: int = 100,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if decision_id:
            clauses.append("decision_id = ?")
            params.append(int(decision_id))
        if pr_number:
            clauses.append("pr_number = ?")
            params.append(int(pr_number))
        scope_clause, scope_params = self._gate_scope()
        where = " AND ".join(clauses) if clauses else "1 = 1"
        if scope_clause:
            where += scope_clause
            params.extend(scope_params)
        rows = self._gate_query(
            self._ph(
                f"SELECT {', '.join(_OVERRIDE_COLUMNS)} FROM gate_overrides WHERE {where} "
                "ORDER BY created_at DESC, id DESC LIMIT ? OFFSET ?"
            ),
            (*params, int(limit), int(offset)),
        )
        return [override_from_row(row) for row in rows]
