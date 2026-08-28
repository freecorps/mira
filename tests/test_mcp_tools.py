"""What the read tools return, and the ceilings they return it under.

Three things are being pinned here. The projections, because a field that
starts flowing out of this surface because somebody added it to a dataclass is
a leak nobody decided on. The paging, because an offset cursor replayed against
different filters walks a different result set from a meaningless position. And
the scoping of the shared-table backend, where a missing WHERE term reads the
whole install rather than one repository.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from mira.config import McpConfig
from mira.index.pg_store import PgIndexStore
from mira.mcp import reads
from mira.mcp.authz import Repository
from tests.mcp_support import (
    SilentAudit,
    call,
    evaluation,
    file_summary,
    finding,
    payload_of,
    populate,
    server,
    text_of,
)

# The Postgres store's own SQL, run against SQLite by the stand-in that
# `test_pg_store` already maintains. Imported rather than copied: a second
# copy would be a second thing to keep in step with the schema.
from tests.test_pg_store import _FakeConn  # noqa: PLC2701


class TestFindings:
    def test_the_projection_is_an_allowlist(self) -> None:
        # Not `asdict`. A field added to ReviewFinding later must not start
        # leaving the building because it was added somewhere else.
        populate(findings=[finding()])

        item = payload_of(
            call(server("acme/widgets"), "mira_list_findings", repository="acme/widgets")
        )["items"][0]

        assert set(item) == {
            "id",
            "pr_number",
            "pr_url",
            "path",
            "start_line",
            "end_line",
            "symbol",
            "category",
            "severity",
            "confidence",
            "title",
            "body",
            "suggestion",
            "state",
            "detector",
            "head_sha",
            "created_at",
            "updated_at",
        }

    @pytest.mark.parametrize(
        ("filters", "expected"),
        [
            ({"severity": "blocker"}, ["blocking"]),
            ({"pr_number": 9}, ["other pr"]),
            ({"state": "resolved"}, ["done"]),
            ({"category": "style"}, ["styled"]),
            ({"path_prefix": "src/deep"}, ["deep"]),
        ],
    )
    def test_each_filter_narrows(self, filters: dict, expected: list[str]) -> None:
        populate(
            findings=[
                finding(finding_id="a", title="blocking", severity="blocker"),
                finding(finding_id="b", title="other pr", pr_number=9),
                finding(finding_id="c", title="done", state="resolved"),
                finding(finding_id="d", title="styled", category="style"),
                finding(finding_id="e", title="deep", path="src/deep/thing.py"),
            ]
        )

        payload = payload_of(
            call(server("acme/widgets"), "mira_list_findings", repository="acme/widgets", **filters)
        )

        assert [item["title"] for item in payload["items"]] == expected

    def test_a_path_prefix_is_literal(self) -> None:
        # `_` and `%` are LIKE wildcards and both are ordinary in a path. An
        # unescaped prefix would quietly widen the filter, and the caller would
        # read the result as if it had been narrowed.
        populate(
            findings=[
                finding(finding_id="a", title="wanted", path="src/_internal/thing.py"),
                finding(finding_id="b", title="unwanted", path="src/xinternal/thing.py"),
            ]
        )

        payload = payload_of(
            call(
                server("acme/widgets"),
                "mira_list_findings",
                repository="acme/widgets",
                path_prefix="src/_internal",
            )
        )

        assert [item["title"] for item in payload["items"]] == ["wanted"]

    def test_one_finding_carries_its_feedback_without_naming_the_actor(self) -> None:
        from mira.feedback.models import FeedbackEventV2
        from mira.index.store import IndexStore

        populate(findings=[finding(finding_id="f-1")])
        store = IndexStore.open("acme", "widgets")
        store.record_feedback_v2(
            FeedbackEventV2(
                id=0,
                finding_id="f-1",
                kind="thumbs_down",
                actor="octocat",
                actor_role="author",
                raw_text="no",
                rationale="This is a false positive.",
                platform="github",
                source_event_id="evt-1",
                head_sha="head123",
                thread_state="open",
                provenance_complete=True,
            )
        )
        store.close()

        payload = payload_of(
            call(
                server("acme/widgets"),
                "mira_get_finding",
                repository="acme/widgets",
                finding_id="f-1",
            )
        )

        assert payload["finding"]["feedback"][0]["kind"] == "thumbs_down"
        assert payload["finding"]["feedback"][0]["actor_role"] == "author"
        assert "octocat" not in text_of(
            call(
                server("acme/widgets"),
                "mira_get_finding",
                repository="acme/widgets",
                finding_id="f-1",
            )
        )

    def test_a_finding_that_is_not_there_is_not_an_error(self) -> None:
        populate(findings=[finding()])

        payload = payload_of(
            call(
                server("acme/widgets"),
                "mira_get_finding",
                repository="acme/widgets",
                finding_id="nope",
            )
        )

        assert payload["found"] is False


class TestRules:
    def test_only_approved_and_active_rules_are_returned(self) -> None:
        # A candidate is a proposal nobody agreed to. Returning it as a rule
        # would describe a review Mira does not actually do.
        populate(
            rules=[
                {"rule_text": "Approved rule", "path_pattern": "src/", "status": "approved"},
                {"rule_text": "Pending rule", "path_pattern": "docs/", "status": "pending"},
            ]
        )

        payload = payload_of(
            call(server("acme/widgets"), "mira_list_rules", repository="acme/widgets")
        )

        assert [item["rule_text"] for item in payload["items"]] == ["Approved rule"]


class TestEvaluations:
    def test_the_author_of_the_pull_request_is_not_returned(self) -> None:
        # The row has it. A question about how a rule performed is not a
        # question about whose code it landed on, and a read-only surface is a
        # bad place to start assembling that.
        populate(evaluations=[evaluation(pr_author="octocat")])

        response = call(server("acme/widgets"), "mira_list_evaluations", repository="acme/widgets")

        assert "octocat" not in text_of(response)
        assert "pr_author" not in payload_of(response)["items"][0]


class TestIndexedContext:
    def test_a_file_carries_its_symbols_and_neighbours(self) -> None:
        populate(
            files=[
                file_summary(),
                file_summary(path="src/util.py", summary="Helpers.", imports=[]),
            ]
        )

        payload = payload_of(
            call(
                server("acme/widgets"),
                "mira_get_indexed_file",
                repository="acme/widgets",
                path="src/util.py",
            )
        )

        assert payload["file"]["summary"] == "Helpers."
        assert payload["file"]["dependents"] == ["src/app.py"]

    def test_files_are_listed_by_path(self) -> None:
        populate(files=[file_summary(path="b.py"), file_summary(path="a.py")])

        payload = payload_of(
            call(server("acme/widgets"), "mira_list_indexed_files", repository="acme/widgets")
        )

        assert [item["path"] for item in payload["items"]] == ["a.py", "b.py"]


class TestPaging:
    def _five(self) -> None:
        populate(
            findings=[
                finding(finding_id=f"f-{n}", title=f"finding {n}", created_at=1_700_000_000.0 + n)
                for n in range(5)
            ]
        )

    def test_a_page_stops_at_the_limit_and_says_there_is_more(self) -> None:
        self._five()

        payload = payload_of(
            call(server("acme/widgets"), "mira_list_findings", repository="acme/widgets", limit=2)
        )

        assert len(payload["items"]) == 2
        assert payload["next_cursor"]

    def test_the_cursor_continues_where_the_page_stopped(self) -> None:
        self._five()
        session = server("acme/widgets")

        seen: list[str] = []
        cursor = ""
        for _ in range(5):
            payload = payload_of(
                call(
                    session,
                    "mira_list_findings",
                    repository="acme/widgets",
                    limit=2,
                    cursor=cursor,
                )
            )
            seen.extend(item["id"] for item in payload["items"])
            cursor = payload["next_cursor"]
            if not cursor:
                break

        # Newest first, every row once.
        assert seen == ["f-4", "f-3", "f-2", "f-1", "f-0"]
        assert cursor == ""

    def test_a_cursor_from_another_query_is_refused(self) -> None:
        # The bug this prevents is silent: an offset means nothing outside the
        # result set it was taken from, and replaying it returns real rows from
        # a meaningless position.
        self._five()
        session = server("acme/widgets")
        cursor = payload_of(
            call(session, "mira_list_findings", repository="acme/widgets", limit=2)
        )["next_cursor"]

        response = call(
            session,
            "mira_list_findings",
            repository="acme/widgets",
            severity="blocker",
            limit=2,
            cursor=cursor,
        )

        assert response["isError"] is True
        assert "different query" in text_of(response)

    def test_a_cursor_mira_did_not_write_is_refused(self) -> None:
        self._five()

        response = call(
            server("acme/widgets"), "mira_list_findings", repository="acme/widgets", cursor="50"
        )

        assert response["isError"] is True

    def test_asking_for_more_than_the_ceiling_gets_the_ceiling(self) -> None:
        # Not an error: the ceiling is Mira's, and the client has no way to
        # learn it before asking.
        populate(
            findings=[
                finding(finding_id=f"f-{n}", created_at=1_700_000_000.0 + n) for n in range(8)
            ]
        )
        session = server("acme/widgets", config=McpConfig(enabled=True, max_page_size=3))

        payload = payload_of(
            call(session, "mira_list_findings", repository="acme/widgets", limit=1000)
        )

        assert len(payload["items"]) == 3


class TestArgumentsAreCheckedStrictly:
    def test_an_unknown_argument_is_refused(self) -> None:
        # Ignoring it would turn `sevrity="blocker"` into an unfiltered list
        # the caller reads as filtered.
        populate(findings=[finding()])

        response = call(
            server("acme/widgets"),
            "mira_list_findings",
            repository="acme/widgets",
            sevrity="blocker",
        )

        assert response["isError"] is True
        assert "sevrity" in text_of(response)

    def test_a_missing_repository_is_refused(self) -> None:
        response = call(server("acme/widgets"), "mira_list_findings")

        assert response["isError"] is True

    def test_a_wrongly_typed_argument_is_refused(self) -> None:
        response = call(
            server("acme/widgets"),
            "mira_list_findings",
            repository="acme/widgets",
            pr_number="seven",
        )

        assert response["isError"] is True
        assert "integer" in text_of(response)

    def test_an_unknown_tool_is_refused_and_recorded(self) -> None:
        audit = SilentAudit()
        session = server("acme/widgets", audit=audit)

        response = session.call_tool({"name": "mira_approve_rule", "arguments": {}})

        assert response["isError"] is True
        assert audit.entries[-1]["outcome"] == "refused"


class TestTheResponseHasACeiling:
    def test_a_page_of_long_findings_is_cut_down_to_fit(self) -> None:
        # Every field is already capped, so a page hits the ceiling by being
        # wide rather than by any one row being huge. Fewer rows first: it
        # keeps the fields whole, and the cursor still points at the row after
        # the last one actually returned.
        populate(
            findings=[
                finding(finding_id=f"f-{n}", body="x" * 3_000, created_at=1_700_000_000.0 + n)
                for n in range(20)
            ]
        )
        session = server(
            "acme/widgets",
            config=McpConfig(enabled=True, max_page_size=20, max_response_bytes=20_000),
        )

        response = call(session, "mira_list_findings", repository="acme/widgets", limit=20)
        payload = payload_of(response)

        assert len(text_of(response).encode("utf-8")) <= 20_000
        assert 0 < len(payload["items"]) < 20
        assert payload["next_cursor"]

    def test_the_page_that_comes_back_is_the_page_the_cursor_continues_from(self) -> None:
        populate(
            findings=[
                finding(finding_id=f"f-{n}", body="x" * 3_000, created_at=1_700_000_000.0 + n)
                for n in range(20)
            ]
        )
        session = server(
            "acme/widgets",
            config=McpConfig(enabled=True, max_page_size=20, max_response_bytes=20_000),
        )

        first = payload_of(call(session, "mira_list_findings", repository="acme/widgets", limit=20))
        second = payload_of(
            call(
                session,
                "mira_list_findings",
                repository="acme/widgets",
                limit=20,
                cursor=first["next_cursor"],
            )
        )

        first_ids = [item["id"] for item in first["items"]]
        assert first_ids[-1] not in [item["id"] for item in second["items"]]

    def test_a_single_oversized_record_is_shortened_rather_than_dropped(self) -> None:
        # `mira_get_finding` returns one thing and has no page to shrink, so
        # the only way down is shorter fields. Truncation is marked.
        populate(findings=[finding(finding_id="f-1", body="y" * 50_000)])
        session = server(
            "acme/widgets",
            config=McpConfig(enabled=True, max_text_chars=40_000, max_response_bytes=8_000),
        )

        response = call(session, "mira_get_finding", repository="acme/widgets", finding_id="f-1")

        assert len(text_of(response).encode("utf-8")) <= 8_000
        assert "[truncated]" in text_of(response)


class TestTheSharedTableBackendStaysScoped:
    """One Postgres install, one set of tables, many repositories.

    On SQLite the scoping is the file layout and cannot be got wrong. Here a
    missing WHERE term reads every repository in the install, which is exactly
    the isolation this surface promises.
    """

    @pytest.fixture
    def two_repositories(self, monkeypatch: pytest.MonkeyPatch):
        from mira.index import pg_store

        conn = _FakeConn()
        monkeypatch.setattr(pg_store, "_get_conn", lambda url, **_kwargs: conn)
        monkeypatch.setattr(pg_store, "_new_pg_conn", lambda url: conn)
        ours = PgIndexStore("acme", "widgets", "postgresql://fake")
        theirs = PgIndexStore("other", "thing", "postgresql://fake")
        ours.save_review_finding(finding(finding_id="ours", title="ours"))
        theirs.save_review_finding(
            finding(owner="other", repo="thing", finding_id="theirs", title="theirs")
        )
        return ours, theirs

    def test_a_listing_returns_only_this_store_s_findings(self, two_repositories) -> None:
        ours, _theirs = two_repositories

        rows = ours.list_review_findings(limit=50, offset=0)

        assert [row.title for row in rows] == ["ours"]

    def test_a_listing_returns_only_this_store_s_files(self, two_repositories) -> None:
        ours, theirs = two_repositories
        ours.upsert_summary(file_summary(path="ours.py"))
        theirs.upsert_summary(file_summary(path="theirs.py"))

        rows = ours.list_indexed_files(limit=50, offset=0)

        assert [row.path for row in rows] == ["ours.py"]


class TestOpeningTheIndex:
    def test_postgres_installs_are_always_considered_indexed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # There is no per-repository file to look for: the tables are shared,
        # nothing is created by connecting, and an unindexed repository simply
        # has no rows.
        monkeypatch.setenv("DATABASE_URL", "postgresql://example/db")

        assert reads.is_indexed(Repository("github", "acme", "widgets")) is True


class TestTheConfiguredBackendIsTheOneRead:
    """A backend that is configured and down is an error, not a quieter source.

    `IndexStore.open` falls back to SQLite when Postgres is unreachable, which
    is right for a server that should keep working. Here it would be wrong
    twice: the fallback creates a per-repository file, so a read becomes a
    write, and it then answers from whatever stale local index it found.
    """

    def test_a_postgres_outage_does_not_create_a_sqlite_index(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from mira.index import pg_store
        from mira.index.store import IndexStore

        monkeypatch.setenv("DATABASE_URL", "postgresql://example/db")

        def unreachable(*_args, **_kwargs):
            raise ConnectionError("could not connect to server")

        monkeypatch.setattr(pg_store.PgIndexStore, "__init__", unreachable)

        response = call(server("acme/widgets"), "mira_list_findings", repository="acme/widgets")

        assert response["isError"] is True
        assert not os.path.exists(IndexStore.db_path_for("acme", "widgets"))

    def test_a_postgres_outage_does_not_answer_from_a_stale_local_index(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The worse half: a local file left over from before the install moved
        # to Postgres would otherwise be served as if it were current.
        from mira.index import pg_store

        populate(findings=[finding(title="stale local data")])
        monkeypatch.setenv("DATABASE_URL", "postgresql://example/db")

        def unreachable(*_args, **_kwargs):
            raise ConnectionError("could not connect to server")

        monkeypatch.setattr(pg_store.PgIndexStore, "__init__", unreachable)

        response = call(server("acme/widgets"), "mira_list_findings", repository="acme/widgets")

        assert response["isError"] is True
        assert "stale local data" not in text_of(response)

    def test_a_missing_sqlite_index_is_not_created_by_opening_it(self) -> None:
        # No check before the open, so no window between them: the connection
        # is made in a mode that raises rather than creating.
        from mira.index.store import IndexStore
        from mira.mcp import reads as reads_module
        from mira.mcp.authz import Repository

        repository = Repository("github", "acme", "widgets")
        with pytest.raises(reads_module.NotIndexed), reads_module.open_index(repository):
            pass  # pragma: no cover - the open is what raises

        assert not os.path.exists(IndexStore.db_path_for("acme", "widgets"))


class TestCursorsCannotBeForged:
    def _five(self) -> None:
        populate(
            findings=[
                finding(finding_id=f"f-{n}", created_at=1_700_000_000.0 + n) for n in range(5)
            ]
        )

    def test_an_edited_offset_is_rejected(self) -> None:
        # A cursor is base64, so a client can decode one, put any number in it
        # and re-encode. The signature covers the offset, so the edit shows.
        import base64
        import json

        self._five()
        session = server("acme/widgets")
        cursor = payload_of(
            call(session, "mira_list_findings", repository="acme/widgets", limit=2)
        )["next_cursor"]

        raw = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4))
        envelope = json.loads(raw)
        payload = json.loads(envelope["p"])
        payload["o"] = 4
        envelope["p"] = json.dumps(payload, separators=(",", ":"))
        forged = (
            base64.urlsafe_b64encode(json.dumps(envelope, separators=(",", ":")).encode())
            .decode()
            .rstrip("=")
        )

        response = call(
            session, "mira_list_findings", repository="acme/widgets", limit=2, cursor=forged
        )

        assert response["isError"] is True
        assert "not issued by this server" in text_of(response)

    def test_a_cursor_past_the_offset_ceiling_is_rejected(self) -> None:
        # An offset with no ceiling is a `LIMIT ... OFFSET` scan a client can
        # make as expensive as it likes.
        from mira.mcp.limits import MAX_OFFSET, encode_cursor

        self._five()
        query = {
            "tool": "list_findings",
            "repository": "github:acme/widgets",
            "pr_number": 0,
            "state": "",
            "category": "",
            "severity": "",
            "path_prefix": "",
        }
        cursor = encode_cursor(query, MAX_OFFSET + 1)

        response = call(
            server("acme/widgets"), "mira_list_findings", repository="acme/widgets", cursor=cursor
        )

        assert response["isError"] is True
        assert "does not page past" in text_of(response)


class TestTheCeilingIsAlwaysHeld:
    def test_a_file_with_too_many_neighbours_is_bounded(self) -> None:
        # `mira_get_indexed_file` has no page to shrink, and shortening every
        # entry of a list does not make the list shorter.
        from mira.index.store import IndexStore, SymbolInfo

        populate(files=[file_summary()])
        store = IndexStore.open("acme", "widgets")
        summary = file_summary(
            imports=[f"src/dep{n}.py" for n in range(5_000)],
        )
        summary.symbols = [
            SymbolInfo(name=f"symbol_{n}", kind="function", signature="()", description="x")
            for n in range(5_000)
        ]
        store.upsert_summary(summary)
        store.close()

        payload = payload_of(
            call(
                server("acme/widgets"),
                "mira_get_indexed_file",
                repository="acme/widgets",
                path="src/app.py",
            )
        )

        assert len(payload["file"]["symbols"]) == 200
        assert len(payload["file"]["imports"]) == 200
        assert payload["file"]["omitted"]["symbols"] == 4_800

    def test_a_response_that_still_will_not_fit_is_refused_rather_than_sent(self) -> None:
        # The ceiling is a ceiling. Sending the oversized response anyway would
        # make it something Mira aims at. Two hundred symbols is under the
        # neighbour bound and still well over four kilobytes however short each
        # name is cut, which is the case per-field truncation cannot answer.
        from mira.index.store import IndexStore, SymbolInfo

        populate(files=[file_summary()])
        store = IndexStore.open("acme", "widgets")
        summary = file_summary()
        summary.symbols = [
            SymbolInfo(name=f"symbol_{n}", kind="function", signature="()", description="x")
            for n in range(200)
        ]
        store.upsert_summary(summary)
        store.close()
        session = server(
            "acme/widgets",
            config=McpConfig(enabled=True, max_text_chars=4_000, max_response_bytes=4_096),
        )

        response = call(
            session,
            "mira_get_indexed_file",
            repository="acme/widgets",
            path="src/app.py",
        )

        assert response["isError"] is True
        assert "response ceiling" in text_of(response)
        assert len(text_of(response).encode("utf-8")) <= 4_096


class TestTruncationRespectsTheLimitItWasGiven:
    def test_a_cut_field_is_no_longer_than_the_limit(self) -> None:
        # Appending the mark on top of `limit` characters puts every truncated
        # field over the ceiling it was cut to satisfy, and makes the
        # response-size arithmetic that depends on this limit wrong the wrong
        # way.
        from mira.mcp.limits import TRUNCATION_MARK, truncate

        cut = truncate("x" * 1_000, limit=100)

        assert len(cut) == 100
        assert cut.endswith(TRUNCATION_MARK)

    def test_a_field_that_already_fits_is_untouched(self) -> None:
        from mira.mcp.limits import truncate

        assert truncate("short", limit=100) == "short"

    def test_a_body_in_a_response_obeys_the_configured_field_ceiling(self) -> None:
        populate(findings=[finding(body="z" * 10_000)])
        session = server("acme/widgets", config=McpConfig(enabled=True, max_text_chars=500))

        item = payload_of(call(session, "mira_list_findings", repository="acme/widgets"))["items"][
            0
        ]

        assert len(item["body"]) == 500


class TestReadingAnIndexDoesNotWriteToIt:
    """`create=False` is the read-only boundary, so it has to actually be one.

    Not creating the file was only half of it: the constructor went on to set
    the journal mode, run the schema script and apply column migrations, all of
    which are writes to somebody else's index performed by a caller whose whole
    contract is that it does not write.
    """

    def _existing_index(self):
        from mira.index.store import IndexStore

        populate(findings=[finding()])
        return IndexStore.db_path_for("acme", "widgets")

    def test_the_connection_itself_refuses_writes(self) -> None:
        import sqlite3

        from mira.index.store import IndexStore

        path = self._existing_index()
        store = IndexStore(path, owner="acme", repo="widgets", create=False)
        try:
            with pytest.raises(sqlite3.OperationalError, match="readonly"):
                store._conn.execute("DELETE FROM review_findings")  # noqa: SLF001
        finally:
            store.close()

    def test_no_schema_statement_runs_on_the_way_in(self) -> None:
        # The proof: drop a table, open read-only, and find it still missing.
        # If the schema script ran, it would be back.
        import sqlite3

        from mira.index.store import IndexStore

        path = self._existing_index()
        scratch = sqlite3.connect(path)
        scratch.execute("DROP TABLE directories")
        scratch.commit()
        scratch.close()

        store = IndexStore(path, owner="acme", repo="widgets", create=False)
        try:
            tables = {
                row[0]
                for row in store._conn.execute(  # noqa: SLF001
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
        finally:
            store.close()

        assert "directories" not in tables
        assert "review_findings" in tables

    def test_the_database_file_is_unchanged_by_being_read(self) -> None:
        # The claim, stated as bytes. SQLite may still touch the -shm sidecar
        # to take a read lock on a WAL database, which is how reading works and
        # not a change to the index; what must not move is the index itself.
        import hashlib

        path = self._existing_index()
        before = hashlib.sha256(Path(path).read_bytes()).hexdigest()

        call(server("acme/widgets"), "mira_list_findings", repository="acme/widgets")

        assert hashlib.sha256(Path(path).read_bytes()).hexdigest() == before

    def test_a_read_only_store_still_answers(self) -> None:
        payload = payload_of(
            call(server("acme/widgets"), "mira_list_findings", repository="acme/widgets")
            if self._existing_index()
            else None
        )

        assert len(payload["items"]) == 1


class TestABackendFailureIsNotAnAbsence:
    def test_an_index_that_cannot_be_opened_is_reported_as_a_failure(self) -> None:
        # A directory where the database should be: the open fails, but the
        # path exists, so this is a broken backend rather than a repository
        # nobody has indexed. Reporting `indexed: false` would hide it behind
        # an answer the caller reads as an answer.
        from mira.index.store import IndexStore

        path = IndexStore.db_path_for("acme", "widgets")
        os.makedirs(path, exist_ok=True)

        response = call(server("acme/widgets"), "mira_list_findings", repository="acme/widgets")

        assert response["isError"] is True
        assert "indexed" not in text_of(response)
