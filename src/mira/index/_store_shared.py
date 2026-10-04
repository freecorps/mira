"""Methods shared verbatim between SQLite and Postgres index stores.

Both `IndexStore` and `PgIndexStore` mix this in. The methods here only call
primitives (`get_summary`, `_load_*`, etc.) that each backend implements;
they don't touch SQL themselves, so they live in one place.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from mira.feedback.evaluation import RuleAnalyticsRow, RuleOutcomeCounts
    from mira.index.store import (
        DirectorySummary,
        ExternalRef,
        FileSummary,
    )


_SEARCH_WORD_RE = re.compile(r"[a-z0-9_]+")
_SEARCH_STOPWORDS = frozenset(
    ["the", "a", "an", "of", "to", "in", "for", "and", "or", "is", "which", "where", "what"]
    + ["file", "files", "handles", "handle", "code", "does", "how", "who", "that", "with"]
)


@dataclass
class IndexMatch:
    """One indexed file matching an index search."""

    path: str
    summary: str = ""
    symbols: list[str] = field(default_factory=list)
    score: int = 0


class _StoreSharedMixin:
    # Parameter placeholder for the analytics SQL, which is written once and
    # run on both backends. SQLite keeps `?`; PgIndexStore overrides with `%s`.
    _analytics_placeholder = "?"

    def get_summaries(self, paths: list[str]) -> dict[str, FileSummary]:
        result: dict[str, FileSummary] = {}
        for path in paths:
            s = self.get_summary(path)  # type: ignore[attr-defined]
            if s is not None:
                result[path] = s
        return result

    def get_directory_summaries(self, paths: list[str]) -> dict[str, DirectorySummary]:
        result: dict[str, DirectorySummary] = {}
        for path in paths:
            ds = self.get_directory_summary(path)  # type: ignore[attr-defined]
            if ds is not None:
                result[path] = ds
        return result

    def upsert_batch(self, summaries: list[FileSummary]) -> None:
        for s in summaries:
            self.upsert_summary(s)  # type: ignore[attr-defined]

    def get_external_refs_for_paths(self, paths: list[str]) -> list[ExternalRef]:
        result: list[ExternalRef] = []
        for path in paths:
            result.extend(self._load_external_refs(path))  # type: ignore[attr-defined]
        return result

    def search_index(self, query: str, limit: int = 15) -> list[IndexMatch]:
        """Indexed files that best match ``query``, for "which file handles X?".

        A plain keyword search, not semantic: each word of the query is looked
        for in file paths, file summaries and symbol names, signatures and
        descriptions, and a file scores by how many distinct words it matches,
        then by where (a path or symbol-name hit counts more than a summary
        hit). The rows come from ``_index_search_rows``, which each backend
        implements; the ranking is here so both rank alike.
        """
        terms = [t for t in dict.fromkeys(_SEARCH_WORD_RE.findall(query.lower())) if len(t) >= 2]
        terms = [t for t in terms if t not in _SEARCH_STOPWORDS][:8]
        if not terms:
            return []
        file_rows, symbol_rows = self._index_search_rows(terms, 400)  # type: ignore[attr-defined]
        found: dict[str, IndexMatch] = {}
        hit_terms: dict[str, set[str]] = {}

        def _entry(path: str) -> IndexMatch:
            if path not in found:
                found[path] = IndexMatch(path=path)
                hit_terms[path] = set()
            return found[path]

        for path, summary in file_rows:
            entry = _entry(path)
            entry.summary = summary or ""
            lower_path, lower_summary = path.lower(), (summary or "").lower()
            for term in terms:
                if term in lower_path:
                    entry.score += 3
                    hit_terms[path].add(term)
                if term in lower_summary:
                    entry.score += 2
                    hit_terms[path].add(term)
        for path, name, signature, description in symbol_rows:
            entry = _entry(path)
            lower_name = (name or "").lower()
            rest = f"{signature or ''} {description or ''}".lower()
            matched = False
            for term in terms:
                if term in lower_name:
                    entry.score += 3
                    hit_terms[path].add(term)
                    matched = True
                elif term in rest:
                    entry.score += 1
                    hit_terms[path].add(term)
                    matched = True
            if matched and name and name not in entry.symbols and len(entry.symbols) < 6:
                entry.symbols.append(name)
        for path, entry in found.items():
            entry.score += 10 * len(hit_terms[path])
        ranked = sorted(found.values(), key=lambda m: (-m.score, m.path))
        return ranked[: max(1, limit)]

    def get_all_review_context_text(self) -> str:
        entries = self.list_review_context()  # type: ignore[attr-defined]
        if not entries:
            return ""
        parts = ["## Repository Documentation Context\n"]
        for entry in entries:
            parts.append(f"### {entry.title}\n{entry.content}\n")
        return "\n".join(parts)

    def get_learned_rules_for_review(
        self,
        paths: list[str] | None = None,
        languages: list[str] | None = None,
        symbols: list[str] | None = None,
        limit: int = 10,
    ) -> list[Any]:
        """The rules retrieval selected for this review, as rule rows.

        Phase 3 has to record *which* rules were exposed, not just the text
        that went into the prompt, so the engine takes the rows and renders
        them itself.
        """
        from mira.feedback.retrieval import retrieve_rules

        return retrieve_rules(
            self,
            paths=paths or [],
            languages=languages or [],
            symbols=symbols or [],
            limit=limit,
        )

    def get_learned_rules_text(
        self,
        paths: list[str] | None = None,
        languages: list[str] | None = None,
        symbols: list[str] | None = None,
        limit: int = 10,
    ) -> list[str]:
        from mira.feedback.retrieval import render_rule

        rules = self.get_learned_rules_for_review(
            paths=paths, languages=languages, symbols=symbols, limit=limit
        )
        return [render_rule(rule) for rule in rules]

    # ---------------------------------------------------------------- Phase 3
    # Evaluation analytics. The SQL comes from `feedback.evaluation_sql` and is
    # identical for both backends; only `_analytics_fetchall` differs, which is
    # what keeps SQLite and Postgres from drifting apart.

    def _analytics_fetchall(self, sql: str, params: tuple) -> list[tuple]:
        raise NotImplementedError  # pragma: no cover - backends implement this

    def _scoped_filters(self, filters: dict[str, Any] | None) -> dict[str, Any]:
        """Force the store's own repository onto every analytics filter.

        SQLite keeps one database per repository, so its rows are already
        scoped. Postgres shares one table across the whole install, where a
        caller that forgot the filter would silently read another repo's
        history - so `PgIndexStore` overrides this. A store opened with an
        empty owner/repo is the deliberate org-wide handle and stays unscoped.
        """
        return dict(filters or {})

    def _analytics_rules(self, keys: list[tuple[str, str, int]]) -> dict[tuple[str, str, int], Any]:
        """Batch-load rule metadata for a page of aggregate rows.

        One query per page, not per row: `export_rule_analytics` asks for up to
        5000 rules, and a per-row lookup would be 5000 round trips on Postgres.

        SQLite has one database per repository, so `rule_id` alone is
        unambiguous and the owner/repo in each key is only used to shape the
        result. `PgIndexStore` overrides this because its table is shared and
        the store's own scope is empty on the org-wide handle.
        """
        rules: dict[tuple[str, str, int], Any] = {}
        for owner, repo, rule_id in keys:
            rules[(owner, repo, rule_id)] = self.get_learned_rule(rule_id)  # type: ignore[attr-defined]
        return rules

    def _counts_from_row(self, row: tuple, offset: int) -> RuleOutcomeCounts:
        """Read the shared aggregate column block starting at `offset`."""
        from mira.feedback.evaluation import RuleOutcomeCounts

        return RuleOutcomeCounts(
            exposures=int(row[offset] or 0),
            review_exposures=int(row[offset + 1] or 0),
            findings=int(row[offset + 2] or 0),
            positive=int(row[offset + 3] or 0),
            negative=int(row[offset + 4] or 0),
            neutral=int(row[offset + 5] or 0),
            unobserved=int(row[offset + 6] or 0),
            addressed=int(row[offset + 7] or 0),
            thumbs_up=int(row[offset + 8] or 0),
            thumbs_down=int(row[offset + 9] or 0),
            reply_agree=int(row[offset + 10] or 0),
            reply_disagree=int(row[offset + 11] or 0),
        )

    def aggregate_rule_analytics(
        self,
        filters: dict[str, Any] | None = None,
        *,
        limit: int = 50,
        offset: int = 0,
        sort: str = "exposures",
        descending: bool = True,
    ) -> list[RuleAnalyticsRow]:
        """Per-rule outcome aggregation for one repository, paginated.

        Rule metadata (text, status, activation date) is looked up per row
        rather than joined, so the page size bounds the extra reads. That keeps
        the query cheap enough for the Orange Pi profile.
        """
        from mira.feedback.evaluation import RuleAnalyticsRow, origin_for_rule
        from mira.feedback.evaluation_sql import aggregate_rules_sql, repeated_false_positives_sql

        active_filters = self._scoped_filters(filters)
        sql, params = aggregate_rules_sql(
            self._analytics_placeholder,
            active_filters,
            limit=limit,
            offset=offset,
            sort=sort,
            descending=descending,
        )
        rows = self._analytics_fetchall(sql, params)
        repeat_sql, repeat_params = repeated_false_positives_sql(
            self._analytics_placeholder, active_filters
        )
        repeats = {
            (str(r[0]), str(r[1]), int(r[2])): int(r[3] or 0)
            for r in self._analytics_fetchall(repeat_sql, repeat_params)
        }

        keys = [(str(row[0]), str(row[1]), int(row[2])) for row in rows]
        metadata = self._analytics_rules(keys) if keys else {}

        results: list[RuleAnalyticsRow] = []
        for row, key in zip(rows, keys, strict=True):
            owner, repo, rule_id = key
            counts = self._counts_from_row(row, 9)
            counts.repeated_false_positives = repeats.get(key, 0)
            rule = metadata.get(key)
            results.append(
                RuleAnalyticsRow(
                    rule_id=rule_id,
                    owner=owner,
                    repo=repo,
                    platform=str(row[3] or "github"),
                    rule_text=getattr(rule, "rule_text", ""),
                    category=str(row[8] or ""),
                    scope_type=str(row[6] or "repo"),
                    scope_value=str(row[7] or ""),
                    # Prefer the recorded origin: it captures what the rule was
                    # when it ran, even if the rule has been edited since.
                    origin=str(row[5] or (origin_for_rule(rule) if rule else "learned")),
                    version=int(row[4] or 1),
                    status=getattr(rule, "status", "approved"),
                    active=bool(getattr(rule, "active", True)),
                    effective_from=float(getattr(rule, "effective_from", 0.0) or 0.0),
                    disabled_at=getattr(rule, "disabled_at", None),
                    counts=counts,
                    first_exposure_at=float(row[21] or 0.0),
                    last_exposure_at=float(row[22] or 0.0),
                )
            )
        return results

    def count_rule_analytics(self, filters: dict[str, Any] | None = None) -> int:
        from mira.feedback.evaluation_sql import count_rules_sql

        sql, params = count_rules_sql(self._analytics_placeholder, self._scoped_filters(filters))
        rows = self._analytics_fetchall(sql, params)
        return int(rows[0][0]) if rows else 0

    def list_rule_evaluations(
        self,
        filters: dict[str, Any] | None = None,
        *,
        limit: int = 50,
        offset: int = 0,
        outcome: str = "",
    ) -> list[dict]:
        """The auditable drill-down: the individual evaluations behind a number."""
        from mira.feedback.evaluation_sql import evaluation_details_sql

        sql, params = evaluation_details_sql(
            self._analytics_placeholder,
            self._scoped_filters(filters),
            limit=limit,
            offset=offset,
            outcome_filter=outcome,
        )
        columns = (
            "id",
            "evaluation_key",
            "review_id",
            "rule_id",
            "rule_version",
            "rule_origin",
            "scope_type",
            "scope_value",
            "category",
            "decision",
            "finding_id",
            "platform",
            "owner",
            "repo",
            "pr_number",
            "pr_author",
            "head_sha",
            "created_at",
            "finding_title",
            "finding_path",
            "finding_line",
            "finding_severity",
            "finding_state",
            "pr_url",
            "outcome",
            "addressed",
            "thumbs_up",
            "thumbs_down",
            "reply_agree",
            "reply_disagree",
        )
        results = []
        for row in self._analytics_fetchall(sql, params):
            # strict: the column names must stay aligned with the SELECT in
            # `evaluation_details_sql`. Drifting silently would drop fields
            # from every drill-down row and CSV export.
            record = dict(zip(columns, row, strict=True))
            record["addressed"] = bool(record.get("addressed"))
            results.append(record)
        return results

    def count_rule_evaluations(
        self, filters: dict[str, Any] | None = None, *, outcome: str = ""
    ) -> int:
        from mira.feedback.evaluation_sql import count_evaluations_sql

        sql, params = count_evaluations_sql(
            self._analytics_placeholder, self._scoped_filters(filters), outcome_filter=outcome
        )
        rows = self._analytics_fetchall(sql, params)
        return int(rows[0][0]) if rows else 0

    def rule_analytics_summary(
        self,
        filters: dict[str, Any] | None = None,
        *,
        dimension: str = "category",
        limit: int = 50,
        offset: int = 0,
    ) -> list[dict]:
        """Outcome mix grouped by category, repo, author, scope or origin."""
        from mira.feedback.evaluation_sql import summary_sql

        sql, params = summary_sql(
            self._analytics_placeholder,
            self._scoped_filters(filters),
            dimension=dimension,
            limit=limit,
            offset=offset,
        )
        buckets = []
        for row in self._analytics_fetchall(sql, params):
            values = self._counts_from_row(row, 1).as_dict()
            # Repeat detection needs per-rule grouping, which a bucket does not
            # have. Drop the key rather than report a zero we never computed.
            values.pop("repeated_false_positives", None)
            buckets.append({"bucket": str(row[0] or ""), **values})
        return buckets

    def rule_period_stats(
        self,
        *,
        owner: str,
        repo: str,
        category: str,
        scope_type: str,
        scope_value: str,
        start: float,
        end: float,
    ) -> dict:
        """Outcome mix of in-scope findings inside one time window."""
        from mira.feedback.evaluation import RuleOutcomeCounts
        from mira.feedback.evaluation_sql import glob_to_like, period_findings_sql

        path_like = glob_to_like(scope_value) if scope_type == "path" and scope_value else ""
        sql, params = period_findings_sql(
            self._analytics_placeholder,
            owner=owner,
            repo=repo,
            category=category,
            path_like=path_like,
            start=start,
            end=end,
        )
        rows = self._analytics_fetchall(sql, params)
        row = rows[0] if rows else (0, 0, 0, 0, 0, 0)
        counts = RuleOutcomeCounts(
            findings=int(row[0] or 0),
            positive=int(row[1] or 0),
            negative=int(row[2] or 0),
            neutral=int(row[3] or 0),
            unobserved=int(row[4] or 0),
            addressed=int(row[5] or 0),
        )
        return {"start": start, "end": end, **counts.as_dict()}
