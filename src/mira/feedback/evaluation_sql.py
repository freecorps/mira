"""SQL shared verbatim by the SQLite and Postgres evaluation analytics.

Parity between the two backends is a hard requirement, and the cheapest way to
guarantee it is to not write the queries twice. Both `rule_evaluations`,
`review_findings` and `feedback_events_v2` carry the same column names in both
schemas, so a single query text works once the parameter placeholder is
swapped (``?`` for SQLite, ``%s`` for psycopg).

Every query here funnels through the same ``signals`` CTE and the same outcome
CASE expression from :mod:`mira.feedback.evaluation`. That is what makes an
aggregate number and its drill-down list provably describe the same rows.
"""

from __future__ import annotations

from typing import Any

from mira.feedback.evaluation import (
    addressed_case_sql,
    outcome_case_sql,
    signal_flags_sql,
)

# Columns a caller may sort the rule list by, mapped to their SQL expression.
# An allowlist, because the sort key arrives from a query string.
RULE_SORT_COLUMNS = {
    "exposures": "exposures",
    "negative": "negative",
    "positive": "positive",
    "findings": "findings",
    "last_exposure_at": "last_exposure_at",
    "rule_id": "e.rule_id",
}

SUMMARY_DIMENSIONS = {
    "category": "e.category",
    "repo": "e.owner || '/' || e.repo",
    "owner": "e.owner",
    "author": "e.pr_author",
    "scope_type": "e.scope_type",
    "origin": "e.rule_origin",
    "decision": "e.decision",
}

# Postgres and SQLite agree on `||` for concatenation, so the repo dimension
# needs no per-backend spelling.


def signals_cte() -> str:
    """Per-finding boolean signal flags, reduced from every feedback event."""
    return (
        "WITH signals AS (SELECT finding_id, "
        f"{signal_flags_sql()} "
        "FROM feedback_events_v2 WHERE finding_id IS NOT NULL GROUP BY finding_id)"
    )


class _Where:
    """Accumulates filter clauses and their bound parameters in order."""

    def __init__(self, placeholder: str) -> None:
        self._ph = placeholder
        self.clauses: list[str] = []
        self.params: list[Any] = []

    def add(self, clause: str, *params: Any) -> None:
        self.clauses.append(clause.replace("?", self._ph))
        self.params.extend(params)

    def eq(self, column: str, value: Any) -> None:
        if value not in (None, "", 0):
            self.add(f"{column} = ?", value)

    def render(self) -> str:
        return " AND ".join(self.clauses) if self.clauses else "1 = 1"


def _evaluation_filters(placeholder: str, filters: dict[str, Any]) -> _Where:
    where = _Where(placeholder)
    where.eq("e.owner", filters.get("owner"))
    where.eq("e.repo", filters.get("repo"))
    where.eq("e.platform", filters.get("platform"))
    where.eq("e.category", filters.get("category"))
    where.eq("e.rule_origin", filters.get("origin"))
    where.eq("e.decision", filters.get("decision"))
    where.eq("e.scope_type", filters.get("scope_type"))
    where.eq("e.pr_author", filters.get("pr_author"))
    rule_id = filters.get("rule_id")
    if rule_id:
        where.add("e.rule_id = ?", rule_id)
    since = filters.get("since")
    if since:
        where.add("e.created_at >= ?", float(since))
    until = filters.get("until")
    if until:
        where.add("e.created_at < ?", float(until))
    return where


def aggregate_rules_sql(
    placeholder: str,
    filters: dict[str, Any],
    *,
    limit: int,
    offset: int,
    sort: str = "exposures",
    descending: bool = True,
) -> tuple[str, tuple[Any, ...]]:
    """Per-rule outcome aggregation, paginated.

    Grouped by (owner, repo, rule_id) rather than rule_id alone because rule
    IDs are per-repo in the SQLite layout and would otherwise collide once the
    org-wide caller merges several databases.
    """
    where = _evaluation_filters(placeholder, filters)
    outcome = outcome_case_sql()
    addressed = addressed_case_sql()
    sort_column = RULE_SORT_COLUMNS.get(sort, "exposures")
    direction = "DESC" if descending else "ASC"
    sql = (
        f"{signals_cte()} "
        "SELECT e.owner, e.repo, e.rule_id, "
        "MAX(e.platform) AS platform, "
        "MAX(e.rule_version) AS rule_version, "
        "MAX(e.rule_origin) AS rule_origin, "
        "MAX(e.scope_type) AS scope_type, "
        "MAX(e.scope_value) AS scope_value, "
        "MAX(e.category) AS category, "
        "COUNT(*) AS exposures, "
        "SUM(CASE WHEN e.finding_id IS NULL THEN 1 ELSE 0 END) AS review_exposures, "
        "SUM(CASE WHEN e.finding_id IS NOT NULL THEN 1 ELSE 0 END) AS findings, "
        f"SUM(CASE WHEN e.finding_id IS NOT NULL AND ({outcome}) = 'positive' "
        "THEN 1 ELSE 0 END) AS positive, "
        f"SUM(CASE WHEN e.finding_id IS NOT NULL AND ({outcome}) = 'negative' "
        "THEN 1 ELSE 0 END) AS negative, "
        f"SUM(CASE WHEN e.finding_id IS NOT NULL AND ({outcome}) = 'neutral' "
        "THEN 1 ELSE 0 END) AS neutral, "
        f"SUM(CASE WHEN e.finding_id IS NOT NULL AND ({outcome}) = 'unobserved' "
        "THEN 1 ELSE 0 END) AS unobserved, "
        f"SUM(CASE WHEN e.finding_id IS NOT NULL THEN ({addressed}) ELSE 0 END) AS addressed, "
        "SUM(COALESCE(s.n_thumbs_up, 0)) AS thumbs_up, "
        "SUM(COALESCE(s.n_thumbs_down, 0)) AS thumbs_down, "
        "SUM(COALESCE(s.n_reply_agree, 0)) AS reply_agree, "
        "SUM(COALESCE(s.n_reply_disagree, 0)) AS reply_disagree, "
        "MIN(e.created_at) AS first_exposure_at, "
        "MAX(e.created_at) AS last_exposure_at "
        "FROM rule_evaluations e "
        "LEFT JOIN review_findings f ON f.id = e.finding_id "
        "LEFT JOIN signals s ON s.finding_id = e.finding_id "
        f"WHERE {where.render()} "
        "GROUP BY e.owner, e.repo, e.rule_id "
        f"ORDER BY {sort_column} {direction}, e.rule_id ASC "
        f"LIMIT {placeholder} OFFSET {placeholder}"
    )
    return sql, (*where.params, limit, offset)


def count_rules_sql(placeholder: str, filters: dict[str, Any]) -> tuple[str, tuple[Any, ...]]:
    """Total distinct rules matching the filters, for pagination metadata."""
    where = _evaluation_filters(placeholder, filters)
    sql = (
        "SELECT COUNT(*) FROM (SELECT 1 FROM rule_evaluations e "
        f"WHERE {where.render()} GROUP BY e.owner, e.repo, e.rule_id) grouped"
    )
    return sql, tuple(where.params)


def repeated_false_positives_sql(
    placeholder: str, filters: dict[str, Any]
) -> tuple[str, tuple[Any, ...]]:
    """Count negative findings that repeat an equivalent earlier complaint.

    Equivalence is (category, path, title): the same objection raised again at
    the same place. Only occurrences *beyond the first* are counted, so a rule
    that misfired once is not punished as if it kept misfiring.
    """
    where = _evaluation_filters(placeholder, filters)
    outcome = outcome_case_sql()
    sql = (
        f"{signals_cte()}, negatives AS ("
        "SELECT e.owner, e.repo, e.rule_id, f.category AS fp_category, f.path AS fp_path, "
        "f.title AS fp_title, COUNT(DISTINCT e.finding_id) AS occurrences "
        "FROM rule_evaluations e "
        "JOIN review_findings f ON f.id = e.finding_id "
        "LEFT JOIN signals s ON s.finding_id = e.finding_id "
        f"WHERE {where.render()} AND e.finding_id IS NOT NULL AND ({outcome}) = 'negative' "
        "GROUP BY e.owner, e.repo, e.rule_id, f.category, f.path, f.title) "
        "SELECT owner, repo, rule_id, SUM(occurrences - 1) AS repeated "
        "FROM negatives WHERE occurrences > 1 GROUP BY owner, repo, rule_id"
    )
    return sql, tuple(where.params)


def evaluation_details_sql(
    placeholder: str,
    filters: dict[str, Any],
    *,
    limit: int,
    offset: int,
    outcome_filter: str = "",
) -> tuple[str, tuple[Any, ...]]:
    """The drill-down behind every aggregate: one row per evaluation.

    Uses the identical ``outcome`` expression as the aggregate, so filtering
    this list by an outcome yields exactly the count the aggregate reported.
    """
    where = _evaluation_filters(placeholder, filters)
    outcome = outcome_case_sql()
    addressed = addressed_case_sql()
    if outcome_filter:
        where.add(f"({outcome}) = ?", outcome_filter)
    sql = (
        f"{signals_cte()} "
        "SELECT e.id, e.evaluation_key, e.review_id, e.rule_id, e.rule_version, "
        "e.rule_origin, e.scope_type, e.scope_value, e.category, e.decision, "
        "e.finding_id, e.platform, e.owner, e.repo, e.pr_number, e.pr_author, "
        "e.head_sha, e.created_at, "
        "COALESCE(f.title, '') AS finding_title, "
        "COALESCE(f.path, '') AS finding_path, "
        "COALESCE(f.start_line, 0) AS finding_line, "
        "COALESCE(f.severity, '') AS finding_severity, "
        "COALESCE(f.state, '') AS finding_state, "
        "COALESCE(f.pr_url, '') AS pr_url, "
        f"({outcome}) AS outcome, "
        f"({addressed}) AS addressed, "
        "COALESCE(s.n_thumbs_up, 0) AS thumbs_up, "
        "COALESCE(s.n_thumbs_down, 0) AS thumbs_down, "
        "COALESCE(s.n_reply_agree, 0) AS reply_agree, "
        "COALESCE(s.n_reply_disagree, 0) AS reply_disagree "
        "FROM rule_evaluations e "
        "LEFT JOIN review_findings f ON f.id = e.finding_id "
        "LEFT JOIN signals s ON s.finding_id = e.finding_id "
        f"WHERE {where.render()} "
        "ORDER BY e.created_at DESC, e.id DESC "
        f"LIMIT {placeholder} OFFSET {placeholder}"
    )
    return sql, (*where.params, limit, offset)


def count_evaluations_sql(
    placeholder: str, filters: dict[str, Any], *, outcome_filter: str = ""
) -> tuple[str, tuple[Any, ...]]:
    where = _evaluation_filters(placeholder, filters)
    outcome = outcome_case_sql()
    if outcome_filter:
        where.add(f"({outcome}) = ?", outcome_filter)
    sql = (
        f"{signals_cte()} "
        "SELECT COUNT(*) FROM rule_evaluations e "
        "LEFT JOIN review_findings f ON f.id = e.finding_id "
        "LEFT JOIN signals s ON s.finding_id = e.finding_id "
        f"WHERE {where.render()}"
    )
    return sql, tuple(where.params)


def summary_sql(
    placeholder: str,
    filters: dict[str, Any],
    *,
    dimension: str,
    limit: int,
) -> tuple[str, tuple[Any, ...]]:
    """Aggregate outcomes grouped by one dimension (category/repo/author/...)."""
    column = SUMMARY_DIMENSIONS.get(dimension)
    if column is None:
        raise ValueError(f"unsupported summary dimension: {dimension}")
    where = _evaluation_filters(placeholder, filters)
    outcome = outcome_case_sql()
    addressed = addressed_case_sql()
    sql = (
        f"{signals_cte()} "
        f"SELECT {column} AS bucket, COUNT(*) AS exposures, "
        "SUM(CASE WHEN e.finding_id IS NULL THEN 1 ELSE 0 END) AS review_exposures, "
        "SUM(CASE WHEN e.finding_id IS NOT NULL THEN 1 ELSE 0 END) AS findings, "
        f"SUM(CASE WHEN e.finding_id IS NOT NULL AND ({outcome}) = 'positive' "
        "THEN 1 ELSE 0 END) AS positive, "
        f"SUM(CASE WHEN e.finding_id IS NOT NULL AND ({outcome}) = 'negative' "
        "THEN 1 ELSE 0 END) AS negative, "
        f"SUM(CASE WHEN e.finding_id IS NOT NULL AND ({outcome}) = 'neutral' "
        "THEN 1 ELSE 0 END) AS neutral, "
        f"SUM(CASE WHEN e.finding_id IS NOT NULL AND ({outcome}) = 'unobserved' "
        "THEN 1 ELSE 0 END) AS unobserved, "
        f"SUM(CASE WHEN e.finding_id IS NOT NULL THEN ({addressed}) ELSE 0 END) AS addressed, "
        "SUM(COALESCE(s.n_thumbs_up, 0)) AS thumbs_up, "
        "SUM(COALESCE(s.n_thumbs_down, 0)) AS thumbs_down, "
        "SUM(COALESCE(s.n_reply_agree, 0)) AS reply_agree, "
        "SUM(COALESCE(s.n_reply_disagree, 0)) AS reply_disagree "
        "FROM rule_evaluations e "
        "LEFT JOIN review_findings f ON f.id = e.finding_id "
        "LEFT JOIN signals s ON s.finding_id = e.finding_id "
        f"WHERE {where.render()} "
        f"GROUP BY {column} ORDER BY exposures DESC "
        f"LIMIT {placeholder}"
    )
    return sql, (*where.params, limit)


def glob_to_like(pattern: str) -> str:
    """Translate a rule scope glob into a SQL LIKE pattern.

    Only the two forms scopes actually use are handled - ``*`` and ``**`` both
    become ``%``, and ``?`` becomes ``_``. Literal ``%``/``_`` are escaped so a
    path containing them cannot widen the match.
    """
    escaped = pattern.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return escaped.replace("**", "*").replace("*", "%").replace("?", "_")


def period_findings_sql(
    placeholder: str,
    *,
    owner: str,
    repo: str,
    category: str,
    path_like: str,
    start: float,
    end: float,
) -> tuple[str, tuple[Any, ...]]:
    """Outcome mix of the findings a rule's scope covers, inside one window.

    This is the before/after comparison's building block. It intentionally
    measures *findings in the rule's scope*, not the rule's own exposures - the
    rule had none before it was activated, so comparing exposures would compare
    a number against zero and prove nothing.
    """
    where = _Where(placeholder)
    where.add("f.owner = ?", owner)
    where.add("f.repo = ?", repo)
    where.add("f.created_at >= ?", start)
    where.add("f.created_at < ?", end)
    if category:
        where.add("f.category = ?", category)
    if path_like:
        where.add("f.path LIKE ? ESCAPE '\\'", path_like)
    outcome = outcome_case_sql()
    addressed = addressed_case_sql()
    sql = (
        f"{signals_cte()} "
        "SELECT COUNT(*) AS findings, "
        f"SUM(CASE WHEN ({outcome}) = 'positive' THEN 1 ELSE 0 END) AS positive, "
        f"SUM(CASE WHEN ({outcome}) = 'negative' THEN 1 ELSE 0 END) AS negative, "
        f"SUM(CASE WHEN ({outcome}) = 'neutral' THEN 1 ELSE 0 END) AS neutral, "
        f"SUM(CASE WHEN ({outcome}) = 'unobserved' THEN 1 ELSE 0 END) AS unobserved, "
        f"SUM({addressed}) AS addressed "
        "FROM review_findings f "
        "LEFT JOIN signals s ON s.finding_id = f.id "
        f"WHERE {where.render()}"
    )
    return sql, tuple(where.params)
