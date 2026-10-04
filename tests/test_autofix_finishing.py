"""Finishing touches — `@mira generate tests` and `@mira generate docstrings`.

Both ride the autofix pipeline, so most of what defends them is defended by
the autofix suites already. What these tests pin down is what is *new*: that
the commands parse narrowly, that each is off until a deployment turns it on,
that a tests job can only ever write test files, that a docstrings job can
only ever change documentation, and that the job kind survives the database —
including one created before the column existed.
"""

from __future__ import annotations

import difflib
import sqlite3
from pathlib import Path
from typing import Any

import pytest
from fastapi import BackgroundTasks

from mira.autofix.commands import (
    handle_finishing_command,
    parse_finishing_command,
    parse_fix_command,
    render_finishing_reply,
)
from mira.autofix.finishing import (
    DOCSTRING_EXTENSIONS,
    FinishingRequest,
    TestLayout,
    check_docstring_only,
    detect_test_layout,
    public_python_symbols,
    related_tests,
    request_finishing_touch,
    suggest_test_path,
)
from mira.autofix.models import AutofixJob, ReasonCode, branch_name, job_key
from mira.autofix.patch import PatchRefused
from mira.autofix.policy import resolve_policy
from mira.autofix.service import RequestOutcome, run_job
from mira.config import AutofixConfig, MiraConfig
from mira.index.store import IndexStore
from tests.test_autofix_pipeline import FakeLLM, FakeProvider, _pr

# ── fixtures ─────────────────────────────────────────────────────────────────

BEFORE_MATH = "def _helper(x):\n    return x\n\n\ndef untouched(y):\n    return y\n"
MATH = (
    "def divide(a, b):\n"
    "    return a / b\n"
    "\n"
    "\n"
    "def _helper(x):\n"
    "    return x\n"
    "\n"
    "\n"
    "def untouched(y):\n"
    "    return y\n"
)
DOCUMENTED = MATH.replace(
    "def divide(a, b):\n", 'def divide(a, b):\n    """Return ``a`` divided by ``b``."""\n', 1
)


def _diff(path: str, before: str, after: str) -> str:
    body = "".join(
        difflib.unified_diff(
            before.splitlines(keepends=True),
            after.splitlines(keepends=True),
            fromfile=f"a/{path}",
            tofile=f"b/{path}",
        )
    )
    return f"diff --git a/{path} b/{path}\n{body}"


@pytest.fixture(autouse=True)
def isolated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MIRA_INDEX_DIR", str(tmp_path))
    monkeypatch.delenv("DATABASE_URL", raising=False)
    IndexStore.open("acme", "app").close()


class FinishingProvider(FakeProvider):
    """The pipeline's fake provider, plus a tree and a real diff."""

    def __init__(
        self,
        *,
        tree: list[str] | None = None,
        diff: str | None = None,
        **kwargs: Any,
    ) -> None:
        kwargs.setdefault("sources", {"src/math.py": MATH})
        super().__init__(**kwargs)
        self.tree = tree if tree is not None else ["src/math.py", "tests/conftest.py"]
        self.diff = diff if diff is not None else _diff("src/math.py", BEFORE_MATH, MATH)

    async def get_repo_tree(self, pr_info: Any, ref: str) -> list[str]:
        return list(self.tree)

    async def get_pr_diff(self, pr_info: Any) -> str:
        return self.diff


def _config(**autofix: Any) -> MiraConfig:
    settings: dict[str, Any] = {
        "mode": "on",
        "max_attempts": 2,
        "finishing_touches": {"tests": True, "docstrings": True},
    }
    settings.update(autofix)
    return MiraConfig(autofix=AutofixConfig(**settings))


TEST_PAYLOAD = {
    "edits": [
        {
            "path": "tests/test_math.py",
            "find": "",
            "replace": (
                "from src.math import divide\n\n\ndef test_divide():\n"
                "    assert divide(4, 2) == 2\n"
            ),
        }
    ],
    "summary": "add tests for divide",
    "rationale": "divide had no tests.",
}

DOC_PAYLOAD = {
    "edits": [
        {
            "path": "src/math.py",
            "find": "def divide(a, b):\n",
            "replace": 'def divide(a, b):\n    """Return ``a`` divided by ``b``."""\n',
        }
    ],
    "summary": "document divide",
}


async def _request(provider: FakeProvider, config: MiraConfig, kind: str, **kwargs: Any):
    return await request_finishing_touch(
        provider,
        _pr(),
        FinishingRequest(actor=kwargs.pop("actor", "alice"), job_kind=kind, **kwargs),
        config=config,
    )


async def _run_one(provider: FakeProvider, config: MiraConfig, llm: FakeLLM):
    store = IndexStore.open("acme", "app")
    try:
        job = store.claim_autofix_job(worker="w1", lease_seconds=60)
        assert job is not None
        return await run_job(provider, job, config=config, llm=llm, store=store)
    finally:
        store.close()


# ── parsing ──────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("generate tests", ("tests", "branch_pr")),
        ("generate unit tests", ("tests", "branch_pr")),
        ("Generate the missing tests", ("tests", "branch_pr")),
        ("tests", ("tests", "branch_pr")),
        ("unit tests", ("tests", "branch_pr")),
        ("generate docstrings", ("docstrings", "branch_pr")),
        ("generate docs", ("docstrings", "branch_pr")),
        ("docstrings", ("docstrings", "branch_pr")),
        ("generate tests --on-branch", ("tests", "pr_branch")),
        ("docstrings --handoff", ("docstrings", "handoff")),
        # Words after the target are ignored, never read as a scope.
        ("generate tests for ../../etc/passwd", ("tests", "branch_pr")),
    ],
)
def test_finishing_commands_parse(text: str, expected: tuple[str, str]) -> None:
    assert parse_finishing_command(text) == expected


@pytest.mark.parametrize(
    "text",
    [
        "",
        "tests are failing on CI, why?",
        "docstrings look wrong here",
        "generate a summary",
        "generate",
        "fix all",
        "review",
        "testing",
    ],
)
def test_conversation_is_not_a_finishing_command(text: str) -> None:
    assert parse_finishing_command(text) is None


def test_fix_and_finishing_never_claim_the_same_text() -> None:
    assert parse_fix_command("generate tests") is None
    assert parse_finishing_command("fix all") is None


# ── policy refusals ──────────────────────────────────────────────────────────


async def test_off_unless_autofix_is_on() -> None:
    outcome = await _request(FinishingProvider(), _config(mode="off"), "tests")
    assert not outcome.accepted
    assert outcome.reasons[0].code == ReasonCode.AUTOFIX_OFF


async def test_the_kill_switch_stops_it() -> None:
    outcome = await _request(FinishingProvider(), _config(kill_switch=True), "docstrings")
    assert outcome.reasons[0].code == ReasonCode.KILL_SWITCH


async def test_a_repository_that_opted_out_is_refused() -> None:
    config = _config(repositories={"acme/app": {"enabled": False}})
    outcome = await _request(FinishingProvider(), config, "tests")
    assert outcome.reasons[0].code == ReasonCode.AUTOFIX_OFF


@pytest.mark.parametrize("kind", ["tests", "docstrings"])
async def test_each_touch_is_off_by_default_even_with_autofix_on(kind: str) -> None:
    config = MiraConfig(autofix=AutofixConfig(mode="on"))
    outcome = await _request(FinishingProvider(), config, kind)
    assert not outcome.accepted
    reason = outcome.reasons[0]
    assert reason.code == ReasonCode.FEATURE_DISABLED
    assert f"autofix.finishing_touches.{kind}" in reason.message


async def test_a_per_repo_override_can_turn_one_on() -> None:
    config = MiraConfig(
        autofix=AutofixConfig(
            mode="on",
            repositories={"acme/app": {"finishing_touches": {"docstrings": True}}},
        )
    )
    policy = resolve_policy(config.autofix, "acme", "app")
    assert policy.allows_job_kind("docstrings")
    assert not policy.allows_job_kind("tests")
    assert not resolve_policy(config.autofix, "acme", "other").allows_job_kind("docstrings")


async def test_a_reader_cannot_ask_for_tests() -> None:
    provider = FinishingProvider(permission="read")
    outcome = await _request(provider, _config(), "tests")
    assert not outcome.accepted
    assert outcome.reasons[0].code == ReasonCode.ACTOR_LACKS_WRITE
    assert provider.branches.keys() == {"main", "feature/divide"}


async def test_handoff_is_never_used_for_a_finishing_touch() -> None:
    config = _config(handoff={"adapter": "comment"})
    outcome = await _request(FinishingProvider(), config, "tests", mode="handoff")
    assert outcome.reasons[0].code == ReasonCode.MODE_NOT_PERMITTED


async def test_committing_to_the_pr_branch_still_needs_its_opt_in() -> None:
    outcome = await _request(FinishingProvider(), _config(), "tests", mode="pr_branch")
    assert outcome.reasons[0].code == ReasonCode.MODE_NOT_PERMITTED
    assert "allow_commit_to_pr_branch" in outcome.reasons[0].message


async def test_nothing_to_test_when_only_docs_changed() -> None:
    provider = FinishingProvider(sources={"README.md": "# hi\n"}, changed=["README.md"], diff="")
    outcome = await _request(provider, _config(), "tests")
    assert not outcome.accepted
    codes = [reason.code for reason in outcome.reasons]
    assert ReasonCode.NOTHING_TO_TEST in codes
    body = render_finishing_reply(outcome, actor="alice", job_kind="tests")
    assert "did not start" in body
    assert "1 changed file(s) are tests, documentation" in body


async def test_nothing_to_document_when_only_private_code_changed() -> None:
    before = "def public():\n    return 1\n"
    after = "def public():\n    return 1\n\n\ndef _private():\n    return 2\n"
    provider = FinishingProvider(
        sources={"src/math.py": after}, diff=_diff("src/math.py", before, after)
    )
    outcome = await _request(provider, _config(), "docstrings")
    assert not outcome.accepted
    assert outcome.reasons[-1].code == ReasonCode.NOTHING_TO_DOCUMENT


async def test_protected_paths_are_left_out_with_a_reason() -> None:
    provider = FinishingProvider(
        sources={"src/math.py": MATH, ".github/workflows/ci.py": "x = 1\n"},
        changed=["src/math.py", ".github/workflows/ci.py"],
    )
    outcome = await _request(provider, _config(), "docstrings")
    assert outcome.accepted
    assert [path for path, _ in outcome.skipped] == [".github/workflows/ci.py"]
    assert outcome.skipped[0][1].code == ReasonCode.PATH_PROTECTED


# ── test layout ──────────────────────────────────────────────────────────────


def test_python_tests_go_where_the_repository_keeps_them() -> None:
    layout = detect_test_layout(["src/a.py", "tests/unit/test_a.py", "tests/conftest.py"])
    assert "pytest" in layout.frameworks[0]
    assert suggest_test_path("src/pkg/math.py", layout) == "tests/test_math.py"


def test_go_tests_sit_beside_the_code() -> None:
    assert suggest_test_path("pkg/math/div.go", TestLayout()) == "pkg/math/div_test.go"


def test_js_follows_the_existing_infix_and_placement() -> None:
    layout = detect_test_layout(["src/a.ts", "src/a.spec.ts", "src/b.ts", "src/b.spec.ts"])
    assert layout.js_infix == "spec"
    assert layout.js_placement == "colocated"
    assert suggest_test_path("src/c.ts", layout) == "src/c.spec.ts"


def test_java_mirrors_main_into_test() -> None:
    assert (
        suggest_test_path("src/main/java/acme/Div.java", TestLayout())
        == "src/test/java/acme/DivTest.java"
    )


# ── the docstring-only guard ─────────────────────────────────────────────────


def _added(before: str, after: str, path: str = "src/math.py") -> set[int]:
    from mira.autofix.finishing import added_lines_by_path

    return added_lines_by_path(_diff(path, before, after))[path]


def test_a_docstring_on_a_changed_public_function_passes() -> None:
    check_docstring_only("src/math.py", MATH, DOCUMENTED, changed=_added(BEFORE_MATH, MATH))


def test_a_code_change_disguised_as_documentation_is_refused() -> None:
    sneaky = DOCUMENTED.replace("return a / b", "return a // b")
    with pytest.raises(PatchRefused) as caught:
        check_docstring_only("src/math.py", MATH, sneaky, changed=None)
    assert caught.value.reason.code == ReasonCode.BEHAVIOUR_CHANGED


def test_a_reordered_function_is_a_behaviour_change() -> None:
    swapped = MATH.replace("def _helper(x):\n    return x\n\n\n", "") + (
        "\n\ndef _helper(x):\n    return x\n"
    )
    with pytest.raises(PatchRefused):
        check_docstring_only("src/math.py", MATH, swapped)


def test_a_private_function_is_not_documented() -> None:
    after = MATH.replace("def _helper(x):\n", 'def _helper(x):\n    """Helper."""\n')
    with pytest.raises(PatchRefused) as caught:
        check_docstring_only("src/math.py", MATH, after)
    assert caught.value.reason.code == ReasonCode.OUT_OF_SCOPE


def test_an_unchanged_function_is_out_of_scope() -> None:
    after = MATH.replace("def untouched(y):\n", 'def untouched(y):\n    """Return y."""\n')
    with pytest.raises(PatchRefused) as caught:
        check_docstring_only("src/math.py", MATH, after, changed=_added(BEFORE_MATH, MATH))
    assert caught.value.reason.code == ReasonCode.OUT_OF_SCOPE


def test_a_module_docstring_is_out_of_scope() -> None:
    with pytest.raises(PatchRefused) as caught:
        check_docstring_only("src/math.py", MATH, '"""Maths."""\n' + MATH)
    assert caught.value.reason.code == ReasonCode.OUT_OF_SCOPE


def test_removing_a_docstring_is_refused() -> None:
    with pytest.raises(PatchRefused) as caught:
        check_docstring_only("src/math.py", DOCUMENTED, MATH)
    assert caught.value.reason.code == ReasonCode.BEHAVIOUR_CHANGED


def test_comments_alone_may_change_in_python() -> None:
    after = DOCUMENTED.replace("    return a / b\n", "    # plain division\n    return a / b\n")
    check_docstring_only("src/math.py", MATH, after)


def test_public_symbols_follow_the_diff() -> None:
    assert public_python_symbols(MATH, _added(BEFORE_MATH, MATH)) == ["divide"]
    assert public_python_symbols(MATH, None) == ["divide", "untouched"]


TS = "export function add(a: number, b: number) {\n  return a + b\n}\n"


def test_a_jsdoc_block_passes_in_typescript() -> None:
    after = "/**\n * Add two numbers.\n * @returns the sum\n */\n" + TS
    check_docstring_only("src/add.ts", TS, after)


def test_a_code_line_in_typescript_is_refused() -> None:
    after = "// Add two numbers.\n" + TS.replace("a + b", "a - b")
    with pytest.raises(PatchRefused) as caught:
        check_docstring_only("src/add.ts", TS, after)
    assert caught.value.reason.code == ReasonCode.BEHAVIOUR_CHANGED


def test_commenting_code_out_is_refused() -> None:
    after = "/*\n" + TS + "*/\n"
    with pytest.raises(PatchRefused) as caught:
        check_docstring_only("src/add.ts", TS, after)
    assert caught.value.reason.code == ReasonCode.BEHAVIOUR_CHANGED


@pytest.mark.parametrize(
    ("path", "before", "after"),
    [
        # Changing the interpreter.
        ("bin/run.sh", "#!/bin/sh\necho hi\n", "#!/bin/bash\necho hi\n"),
        # Writing a comment above it, which stops it being an interpreter line.
        ("bin/run.sh", "#!/bin/sh\necho hi\n", "# Runs it.\n#!/bin/sh\necho hi\n"),
        # Adding one where there was none.
        ("bin/run.sh", "echo hi\n", "#!/bin/bash\necho hi\n"),
        # Python compares syntax trees, where a shebang is invisible.
        ("tool.py", "#!/usr/bin/env python3\nx = 1\n", "#!/usr/bin/python2\nx = 1\n"),
    ],
)
def test_the_interpreter_line_is_not_documentation(path: str, before: str, after: str) -> None:
    with pytest.raises(PatchRefused) as caught:
        check_docstring_only(path, before, after)
    assert caught.value.reason.code == ReasonCode.BEHAVIOUR_CHANGED


def test_a_comment_below_the_interpreter_line_is_fine() -> None:
    check_docstring_only("bin/run.sh", "#!/bin/sh\necho hi\n", "#!/bin/sh\n# Says hi.\necho hi\n")


def test_stub_files_are_offered_for_docstrings() -> None:
    assert ".pyi" in DOCSTRING_EXTENSIONS


@pytest.mark.parametrize(
    ("source", "tree", "expected"),
    [
        ("src/contest.py", ["src/contest.py", "tests/test_contest.py"], ["tests/test_contest.py"]),
        ("src/latest.ts", ["src/latest.ts", "src/latest.spec.ts"], ["src/latest.spec.ts"]),
        (
            "src/main/java/acme/Div.java",
            ["src/main/java/acme/Div.java", "src/test/java/acme/DivTest.java"],
            ["src/test/java/acme/DivTest.java"],
        ),
        ("src/Div.cs", ["src/Div.cs", "tests/DivTests.cs"], ["tests/DivTests.cs"]),
    ],
)
def test_related_tests_match_the_source_they_test(
    source: str, tree: list[str], expected: list[str]
) -> None:
    assert related_tests(source, detect_test_layout(tree)) == expected


def test_a_language_without_a_known_comment_syntax_is_refused() -> None:
    with pytest.raises(PatchRefused):
        check_docstring_only("src/thing.ex", "x\n", "# y\nx\n")


# ── job kind: identity and persistence ───────────────────────────────────────


def test_a_fix_keeps_the_key_it_always_had() -> None:
    import hashlib

    legacy = hashlib.sha256(
        "\x1f".join(["github", "acme", "app", "7", "h", "f1", "branch_pr"]).encode()
    ).hexdigest()
    args = {
        "platform": "github",
        "owner": "acme",
        "repo": "app",
        "pr_number": 7,
        "head_sha": "h",
        "finding_id": "f1",
        "mode": "branch_pr",
    }
    assert job_key(**args) == legacy
    assert job_key(**args, job_kind="fix") == legacy
    tests_key = job_key(**{**args, "finding_id": ""}, job_kind="tests")
    docs_key = job_key(**{**args, "finding_id": ""}, job_kind="docstrings")
    assert len({legacy, tests_key, docs_key}) == 3


def test_finishing_branches_name_the_work_and_the_commit() -> None:
    name = branch_name(
        prefix="mira/fix",
        pr_number=7,
        finding_id="",
        job_kind="tests",
        head_sha="head456abcdef",
        title="../../ evil $(rm -rf /)",
    )
    assert name == "mira/fix/pr-7/tests-head456abcde"


def test_two_heads_sharing_seven_characters_get_two_branches() -> None:
    names = {
        branch_name(prefix="mira/fix", pr_number=7, finding_id="", job_kind="tests", head_sha=sha)
        for sha in ("abcdef1111111", "abcdef1222222")
    }
    assert len(names) == 2


def _job(kind: str) -> AutofixJob:
    return AutofixJob(
        job_key=job_key(
            platform="github",
            owner="acme",
            repo="app",
            pr_number=7,
            head_sha="h",
            finding_id="",
            mode="branch_pr",
            job_kind=kind,
        ),
        job_kind=kind,  # type: ignore[arg-type]
        owner="acme",
        repo="app",
        pr_number=7,
        head_sha="h",
    )


def test_the_job_kind_survives_the_sqlite_store_and_filters() -> None:
    store = IndexStore.open("acme", "app")
    try:
        store.enqueue_autofix_job(_job("tests"))
        store.enqueue_autofix_job(_job("docstrings"))
        kinds = sorted(job.job_kind for job in store.list_autofix_jobs())
        assert kinds == ["docstrings", "tests"]
        only = store.list_autofix_jobs({"job_kind": "tests"})
        assert [job.job_kind for job in only] == ["tests"]
        assert only[0].as_dict()["job_kind"] == "tests"
    finally:
        store.close()


def test_an_existing_sqlite_database_gains_the_column_and_reads_as_fix() -> None:
    path = IndexStore.db_path_for("acme", "legacy")
    IndexStore.open("acme", "legacy").close()
    # Recreate the table exactly as it was before the column existed.
    conn = sqlite3.connect(path)
    conn.execute("ALTER TABLE autofix_jobs DROP COLUMN job_kind")
    conn.execute(
        "INSERT INTO autofix_jobs (job_key, state, owner, repo) VALUES ('old', 'opened', 'a', 'b')"
    )
    conn.commit()
    columns = {row[1] for row in conn.execute("PRAGMA table_info(autofix_jobs)")}
    assert "job_kind" not in columns
    conn.close()

    store = IndexStore.open("acme", "legacy")
    try:
        old = store.get_autofix_job("old")
        assert old is not None and old.job_kind == "fix"
        store.enqueue_autofix_job(_job("docstrings"))
        assert store.list_autofix_jobs({"job_kind": "docstrings"})[0].job_kind == "docstrings"
    finally:
        store.close()


def test_the_job_kind_survives_the_postgres_store(monkeypatch: pytest.MonkeyPatch) -> None:
    from mira.index import pg_store
    from mira.index.pg_store import PgIndexStore
    from tests.test_autofix_queue import _FakeConn

    conn = _FakeConn()
    monkeypatch.setattr(pg_store, "_get_conn", lambda url, **_kwargs: conn)
    monkeypatch.setattr(pg_store, "_new_pg_conn", lambda url: conn)
    store = PgIndexStore("acme", "app", "postgresql://fake")
    store.enqueue_autofix_job(_job("tests"))
    assert store.list_autofix_jobs({"job_kind": "tests"})[0].job_kind == "tests"


def test_postgres_adds_the_column_to_an_existing_table(monkeypatch: pytest.MonkeyPatch) -> None:
    from mira.index import pg_store

    executed: list[str] = []

    class Cursor:
        def execute(self, sql, params=()):  # noqa: ANN001, ANN202
            executed.append(sql)
            return self

        def fetchall(self):  # noqa: ANN202
            return []

        def fetchone(self):  # noqa: ANN202
            return None

        def __enter__(self):  # noqa: ANN204
            return self

        def __exit__(self, *_args: Any) -> None:
            return None

    class Conn:
        def cursor(self):  # noqa: ANN202
            return Cursor()

        def commit(self) -> None:
            return None

    monkeypatch.setattr(pg_store, "_pg_conn", None, raising=False)
    monkeypatch.setattr(pg_store, "_pg_conn_read_only", None, raising=False)
    monkeypatch.setattr(pg_store, "_schema_initialized", False, raising=False)
    monkeypatch.setattr("mira.db.postgres.connect", lambda _url: Conn())
    pg_store.PgIndexStore("acme", "app", "postgresql://fake")
    assert any(
        "ALTER TABLE autofix_jobs ADD COLUMN IF NOT EXISTS job_kind" in sql for sql in executed
    )


# ── end to end ───────────────────────────────────────────────────────────────


async def test_generate_tests_opens_a_stacked_pr_that_touches_only_tests() -> None:
    provider = FinishingProvider()
    config = _config()
    outcome = await _request(provider, config, "tests")
    assert outcome.ok, outcome.reasons
    job = outcome.accepted[0]
    assert job.job_kind == "tests"
    assert any("tests/test_math.py" in line for line in outcome.scope)

    result = await _run_one(provider, config, FakeLLM(TEST_PAYLOAD))
    assert result.job.state == "opened", result.reasons
    assert result.job.job_kind == "tests"
    assert result.job.branch_name == "mira/fix/pr-7/tests-head456"
    branch, message, files = provider.commits[0]
    assert set(files) == {"tests/test_math.py"}
    # The source under test is exactly as it was.
    assert provider.branch_contents[branch]["src/math.py"] == MATH
    assert "Mira-Task: tests" in message
    pull = provider.pulls[0]
    assert pull["base"] == "feature/divide"
    assert pull["title"].startswith("test: ")
    assert "Only test files are touched" in pull["body"]
    assert provider.merges == []


async def test_a_tests_job_that_edits_source_writes_nothing() -> None:
    provider = FinishingProvider()
    config = _config()
    await _request(provider, config, "tests")
    payload = {
        "edits": [{"path": "src/math.py", "find": "return a / b", "replace": "return 0"}],
        "summary": "make the tests pass",
    }
    result = await _run_one(provider, config, FakeLLM(payload))
    assert result.job.state in {"failed", "dead_letter"}
    assert result.reasons[0].code == ReasonCode.NOT_A_TEST_FILE
    assert provider.commits == []
    assert provider.pulls == []


async def test_an_existing_test_file_cannot_be_replaced_wholesale() -> None:
    existing = "def test_old():\n    assert True\n"
    provider = FinishingProvider(
        sources={"src/math.py": MATH, "tests/test_math.py": existing},
        tree=["src/math.py", "tests/test_math.py"],
    )
    config = _config()
    outcome = await _request(provider, config, "tests")
    assert outcome.ok
    result = await _run_one(provider, config, FakeLLM(TEST_PAYLOAD))
    assert result.reasons[0].code == ReasonCode.PATCH_INVALID
    assert provider.commits == []


async def test_generate_docstrings_opens_a_documentation_only_pr() -> None:
    provider = FinishingProvider()
    config = _config()
    outcome = await _request(provider, config, "docstrings")
    assert outcome.ok, outcome.reasons
    assert outcome.scope == ["`src/math.py` (divide)"]

    result = await _run_one(provider, config, FakeLLM(DOC_PAYLOAD))
    assert result.job.state == "opened", result.reasons
    _branch, message, files = provider.commits[0]
    assert files == {"src/math.py": DOCUMENTED}
    assert "Mira-Task: docstrings" in message
    assert provider.pulls[0]["title"].startswith("docs: ")


async def test_a_docstring_patch_that_changes_code_never_reaches_a_branch() -> None:
    provider = FinishingProvider()
    config = _config(max_attempts=1)
    await _request(provider, config, "docstrings")
    payload = {
        "edits": [
            {
                "path": "src/math.py",
                "find": "def divide(a, b):\n    return a / b\n",
                "replace": (
                    'def divide(a, b):\n    """Divide safely."""\n    return a / b if b else 0\n'
                ),
            }
        ],
        "summary": "document divide",
    }
    result = await _run_one(provider, config, FakeLLM(payload))
    assert result.job.state == "dead_letter"
    assert result.reasons[0].code == ReasonCode.BEHAVIOUR_CHANGED
    assert provider.commits == []
    assert len(provider.branches) == 2


async def test_suggest_mode_writes_nothing() -> None:
    provider = FinishingProvider()
    config = _config(mode="suggest")
    outcome = await _request(provider, config, "docstrings")
    body = render_finishing_reply(outcome, actor="alice", job_kind="docstrings")
    assert "suggest" in body
    result = await _run_one(provider, config, FakeLLM(DOC_PAYLOAD))
    assert result.job.state == "opened"
    assert result.job.diff
    assert provider.commits == [] and provider.pulls == []


async def test_asking_twice_queues_one_job() -> None:
    provider = FinishingProvider()
    config = _config()
    first = await _request(provider, config, "tests")
    second = await _request(provider, config, "tests", actor="bob")
    assert first.accepted[0].job_key == second.accepted[0].job_key
    store = IndexStore.open("acme", "app")
    try:
        assert store.count_autofix_jobs({"job_kind": "tests"}) == 1
    finally:
        store.close()


async def test_a_finished_request_says_so_when_asked_again() -> None:
    provider = FinishingProvider()
    config = _config()
    await _request(provider, config, "docstrings")
    await _run_one(provider, config, FakeLLM(DOC_PAYLOAD))
    again = await _request(provider, config, "docstrings")
    assert not again.accepted
    assert again.reasons[0].code == ReasonCode.REUSED_EXISTING


async def test_a_job_whose_pull_request_moved_is_asked_again(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = FinishingProvider()
    config = _config()
    await _request(provider, config, "tests")

    async def moved(pr_url: str) -> Any:
        return _pr(url=pr_url, head_sha="newer789")

    monkeypatch.setattr(provider, "get_pr_info", moved)
    result = await _run_one(provider, config, FakeLLM(TEST_PAYLOAD))
    assert result.job.state == "dead_letter"
    assert result.reasons[0].code == ReasonCode.HEAD_MOVED
    assert provider.commits == []


async def test_turning_the_toggle_off_stops_a_queued_job() -> None:
    provider = FinishingProvider()
    await _request(provider, _config(), "tests")
    off = _config(finishing_touches={"tests": False, "docstrings": True})
    result = await _run_one(provider, off, FakeLLM(TEST_PAYLOAD))
    assert result.job.state == "dead_letter"
    assert result.reasons[0].code == ReasonCode.FEATURE_DISABLED
    assert provider.commits == []


# ── the reply and the webhooks ───────────────────────────────────────────────


async def test_the_reply_always_answers(monkeypatch: pytest.MonkeyPatch) -> None:
    provider = FinishingProvider()
    outcome = await handle_finishing_command(
        provider,
        _pr(),
        actor="alice",
        job_kind="tests",
        config=MiraConfig(autofix=AutofixConfig(mode="on")),
    )
    assert not outcome.accepted
    assert "generate tests" in provider.comments[0]
    assert "finishing_touches.tests" in provider.comments[0]


def _auth() -> Any:
    from unittest.mock import AsyncMock

    auth = AsyncMock()
    auth.get_bot_identity = AsyncMock(return_value="mira-bot")
    auth.get_installation_token = AsyncMock(return_value="tok")
    auth.get_token = AsyncMock(return_value="tok")
    return auth


def _issue_comment(body: str) -> dict:
    return {
        "action": "created",
        "repository": {"owner": {"login": "acme"}, "name": "app", "full_name": "acme/app"},
        "issue": {"number": 7, "pull_request": {}, "title": "A pull request"},
        "comment": {"id": 5, "body": body, "user": {"login": "alice", "type": "User"}},
        "installation": {"id": 1},
    }


async def test_github_routes_generate_to_the_writing_handler() -> None:
    from mira.platforms.github.webhook import (
        dispatch_github_event,
        handle_comment,
        handle_fix_request,
    )

    tasks = BackgroundTasks()
    await dispatch_github_event(
        "issue_comment", _issue_comment("@mira generate tests"), _auth(), "mira", tasks
    )
    assert tasks.tasks[0].func is handle_fix_request

    chat = BackgroundTasks()
    await dispatch_github_event(
        "issue_comment", _issue_comment("@mira tests are failing, why?"), _auth(), "mira", chat
    )
    assert chat.tasks[0].func is handle_comment


async def test_github_hands_generate_to_the_finishing_handler(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import mira.platforms.github.webhook as gh

    provider = FinishingProvider()
    monkeypatch.setattr(gh, "create_provider", lambda platform, token: provider)
    captured: dict[str, Any] = {}

    async def fake_handle(provider_, pr_info, **kwargs):  # noqa: ANN001
        captured.update(kwargs)
        return RequestOutcome()

    monkeypatch.setattr("mira.autofix.commands.handle_finishing_command", fake_handle)
    await gh.handle_fix_request(
        _issue_comment("@mira generate docstrings"), _auth(), "mira", inline=False
    )
    assert captured == {"actor": "alice", "job_kind": "docstrings", "mode": "branch_pr"}


async def test_gitlab_routes_generate_to_the_finishing_handler(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from mira.platforms.gitlab import webhook as gl

    provider = FinishingProvider()
    monkeypatch.setattr(gl, "create_provider", lambda platform, token: provider)
    captured: dict[str, Any] = {}

    async def fake_handle(provider_, pr_info, **kwargs):  # noqa: ANN001
        captured.update(kwargs)
        return RequestOutcome()

    monkeypatch.setattr("mira.autofix.commands.handle_finishing_command", fake_handle)
    payload = {
        "object_attributes": {"note": "@mira generate unit tests", "id": 1},
        "project": {"path_with_namespace": "acme/app", "web_url": "https://gitlab.com/acme/app"},
        "merge_request": {"iid": 7, "url": "https://gitlab.com/acme/app/-/merge_requests/7"},
        "user": {"username": "alice"},
    }
    await gl.handle_gitlab_note(payload, _auth(), "mira")
    assert captured == {"actor": "alice", "job_kind": "tests", "mode": "branch_pr"}


async def test_forgejo_routes_generate_to_the_finishing_handler(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from mira.platforms.forgejo import webhook as fj

    provider = FinishingProvider()
    monkeypatch.setattr(fj, "create_provider", lambda platform, token: provider)
    captured: dict[str, Any] = {}

    async def fake_handle(provider_, pr_info, **kwargs):  # noqa: ANN001
        captured.update(kwargs)
        return RequestOutcome()

    monkeypatch.setattr("mira.autofix.commands.handle_finishing_command", fake_handle)
    payload = {
        "action": "created",
        "is_pull": True,
        "repository": {"full_name": "acme/app", "html_url": "https://forge.dev/acme/app"},
        "issue": {"number": 7},
        "comment": {"id": 5, "body": "@mira docstrings", "user": {"username": "alice"}},
    }
    await fj.handle_forgejo_note(payload, _auth(), "mira")
    assert captured == {"actor": "alice", "job_kind": "docstrings", "mode": "branch_pr"}


def test_the_help_lists_both_commands() -> None:
    from mira.platforms.handlers import _help_message

    body = _help_message("mira")
    assert "`@mira generate tests`" in body
    assert "`@mira generate docstrings`" in body


def test_colocated_python_tests_stay_beside_the_code() -> None:
    layout = detect_test_layout(["pkg/a.py", "pkg/a_test.py"])
    assert layout.python_style == "suffix"
    assert suggest_test_path("pkg/b.py", layout) == "pkg/b_test.py"


async def test_without_a_tree_no_file_is_taken_for_new() -> None:
    """Some providers answer a failed read with an empty body. Without the tree
    to confirm a file is absent, offering it as new could overwrite it whole."""
    provider = FinishingProvider(tree=[])
    outcome = await _request(provider, _config(), "tests")
    assert not outcome.accepted
    assert outcome.reasons[-1].code == ReasonCode.NOTHING_TO_TEST


async def test_an_unaddressed_thread_reply_is_not_a_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """GitLab and Forgejo deliver thread replies without a mention; a bare
    "tests" there is conversation, not a request to write any."""
    from mira.feedback.provenance import finding_marker
    from mira.platforms.forgejo import webhook as fj

    class Provider(FinishingProvider):
        async def get_comment_body(self, pr_info: Any, comment_id: int) -> str:
            return f"{finding_marker('f' * 32)}\n**A finding**"

    provider = Provider()
    monkeypatch.setattr(fj, "create_provider", lambda platform, token: provider)
    called: list[Any] = []

    async def fake_handle(provider_, pr_info, **kwargs):  # noqa: ANN001
        called.append(kwargs)
        return RequestOutcome()

    async def quiet(*_args: Any, **_kwargs: Any) -> None:
        return None

    monkeypatch.setattr("mira.autofix.commands.handle_finishing_command", fake_handle)
    monkeypatch.setattr("mira.platforms.handlers.run_thread_reply", quiet)
    payload = {
        "action": "created",
        "is_pull": True,
        "repository": {"full_name": "acme/app", "html_url": "https://forge.dev/acme/app"},
        "issue": {"number": 7},
        "comment": {
            "id": 6,
            "body": "tests",
            "user": {"username": "alice"},
            "in_reply_to_id": 5,
        },
    }
    await fj.handle_forgejo_note(payload, _auth(), "mira")
    assert called == []
