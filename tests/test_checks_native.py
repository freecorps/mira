"""Phase 6 — the five native checks, over real diffs.

Each check is driven with a diff built here and parsed by the real parser, so a
test that passes has exercised the same code path a pull request does. The
assertions are about the state *and* the evidence: a check that reported the
right verdict with a line number nobody can navigate to has failed the phase's
one promise, so "it objected" is never the whole assertion.
"""

from __future__ import annotations

import pytest

from mira.checks.context import CheckContext
from mira.checks.models import SkipReason
from mira.checks.native import breaking, description, docs, migrations
from mira.checks.native import paths as native_paths
from mira.checks.native import tests as tests_check
from mira.checks.policy import resolve_policy
from mira.config import ChecksConfig
from mira.core.diff_parser import parse_diff
from mira.models import FileChangeStat


def _policy(**overrides):
    overrides.setdefault("enabled", True)
    return resolve_policy(ChecksConfig(**overrides), "acme", "app")


def _diff(path: str, added: list[str], removed: list[str] | None = None, start: int = 1) -> str:
    """A one-hunk unified diff for one file."""
    removed = removed or []
    body = "".join(f"-{line}\n" for line in removed) + "".join(f"+{line}\n" for line in added)
    return (
        f"diff --git a/{path} b/{path}\n"
        f"--- a/{path}\n"
        f"+++ b/{path}\n"
        f"@@ -{start},{len(removed)} +{start},{len(added)} @@\n"
        f"{body}"
    )


def _ctx(diff_text: str = "", *, changes=None, policy=None, **overrides) -> CheckContext:
    patch_set = parse_diff(diff_text or "")
    if changes is None:
        changes = [
            FileChangeStat(path=f.path, added_lines=f.added_lines, deleted_lines=f.deleted_lines)
            for f in patch_set.files
        ]
    return CheckContext(
        policy=policy or _policy(),
        owner="acme",
        repo="app",
        pr_number=7,
        pr_url="https://github.com/acme/app/pull/7",
        head_sha="head123",
        changes=list(changes),
        patch_set=patch_set,
        diff_text=diff_text or "",
        **overrides,
    )


# ─────────────────────────────────────────────── title and description ──


@pytest.mark.parametrize(
    "title",
    ["fix", "WIP", "update", "#123", "chore", ""],
)
async def test_an_uninformative_title_is_a_violation_with_the_title_quoted(title: str) -> None:
    outcome = await description.run(
        _ctx(pr_title=title, pr_body="A real explanation of what changed and why.")
    )
    assert outcome.state == "violation"
    assert outcome.findings[0].evidence[0].source == "pr"


async def test_a_conventional_commit_prefix_does_not_count_as_the_title() -> None:
    """`fix(gate): stop approving on unknown CI` says something; `fix` does not."""
    good = await description.run(
        _ctx(
            pr_title="fix(gate): stop approving when CI state is unknown",
            pr_body="The gate treated an unreadable CI status as green. It now refuses.",
        )
    )
    assert good.state == "pass"


async def test_an_empty_description_is_a_violation() -> None:
    outcome = await description.run(
        _ctx(pr_title="Add rate limiting to the ingest endpoint", pr_body="")
    )
    assert outcome.state == "violation"
    assert any("no description" in f.title.lower() for f in outcome.findings)


async def test_an_unfilled_template_is_a_violation_pointing_at_the_line() -> None:
    body = "## Summary\n\n<!-- Describe your changes -->\n\n## Testing\n\nRan the suite."
    outcome = await description.run(
        _ctx(pr_title="Add rate limiting to the ingest endpoint", pr_body=body)
    )
    assert outcome.state == "violation"
    finding = next(f for f in outcome.findings if "template" in f.title.lower())
    assert finding.evidence[0].start_line == 3


async def test_a_description_that_is_only_a_checklist_is_a_violation() -> None:
    body = "## Checklist\n\n- [x] Tests added\n- [x] Docs updated\n- [ ] Changelog"
    outcome = await description.run(
        _ctx(pr_title="Add rate limiting to the ingest endpoint", pr_body=body)
    )
    assert outcome.state == "violation"
    assert any("checklist" in f.title.lower() for f in outcome.findings)


async def test_a_draft_titled_wip_is_not_objected_to() -> None:
    """A draft saying WIP is telling the truth."""
    outcome = await description.run(
        _ctx(
            pr_title="WIP",
            pr_body="Early sketch of the ingest rate limiter, pushed for a second opinion.",
            draft=True,
        )
    )
    assert outcome.state == "pass"


# ──────────────────────────────────────────────────────────────── tests ──


async def test_source_changed_with_no_test_is_a_violation_naming_the_files() -> None:
    diff = _diff("src/mira/core/engine.py", ["    if new_branch:", "        do_something()"])
    outcome = await tests_check.run(_ctx(diff))
    assert outcome.state == "violation"
    evidence_paths = {e.path for e in outcome.findings[0].evidence}
    assert evidence_paths == {"src/mira/core/engine.py"}


async def test_a_changed_test_satisfies_the_check() -> None:
    diff = _diff("src/mira/core/engine.py", ["    x = 1"]) + _diff(
        "tests/test_engine.py", ["def test_x():", "    assert True"]
    )
    outcome = await tests_check.run(_ctx(diff))
    assert outcome.state == "pass"


async def test_a_go_style_sibling_test_file_counts() -> None:
    diff = _diff("pkg/ingest/rate.go", ["func Limit() {}"]) + _diff(
        "pkg/ingest/rate_test.go", ["func TestLimit(t *testing.T) {}"]
    )
    outcome = await tests_check.run(_ctx(diff))
    assert outcome.state == "pass"


async def test_a_documentation_only_change_is_skipped_not_failed() -> None:
    diff = _diff("docs/pre-merge-checks.md", ["A new paragraph."])
    outcome = await tests_check.run(_ctx(diff))
    assert outcome.state == "skipped"
    assert outcome.skip_reason == SkipReason.NOT_APPLICABLE


async def test_a_lockfile_bump_is_not_source() -> None:
    diff = _diff("uv.lock", ['version = "2"'])
    outcome = await tests_check.run(_ctx(diff))
    assert outcome.state == "skipped"


async def test_a_deletion_only_change_does_not_demand_a_test() -> None:
    diff = _diff("src/mira/core/old.py", [], ["def gone():", "    pass"])
    outcome = await tests_check.run(_ctx(diff))
    assert outcome.state == "pass"


# ───────────────────────────────────────────────────────────────── docs ──


async def test_a_new_public_symbol_with_no_doc_change_is_a_violation() -> None:
    diff = _diff("src/mira/core/engine.py", ["def review_everything(pr):", "    return None"])
    outcome = await docs.run(_ctx(diff))
    assert outcome.state == "violation"
    assert outcome.findings[0].evidence[0].path == "src/mira/core/engine.py"
    assert outcome.findings[0].evidence[0].start_line > 0


async def test_a_private_helper_does_not_demand_documentation() -> None:
    diff = _diff("src/mira/core/engine.py", ["def _helper(x):", "    return x"])
    outcome = await docs.run(_ctx(diff))
    assert outcome.state == "skipped"


async def test_a_doc_change_alongside_satisfies_the_check() -> None:
    diff = _diff("src/mira/cli.py", ["def new_command():", "    pass"]) + _diff(
        "docs/cli.md", ["## new-command"]
    )
    outcome = await docs.run(_ctx(diff))
    assert outcome.state == "pass"


async def test_a_changed_interface_file_counts_even_with_no_new_symbol() -> None:
    diff = _diff(".env.example", ["MIRA_NEW_SETTING=1"])
    outcome = await docs.run(_ctx(diff))
    assert outcome.state == "violation"


# ─────────────────────────────────────────────────────── breaking change ──


async def test_a_removed_public_function_is_reported_with_the_removed_line() -> None:
    diff = _diff("src/mira/core/engine.py", [], ["def review_diff(self, text):", "    ..."])
    outcome = await breaking.run(_ctx(diff))
    assert outcome.state == "violation"
    finding = outcome.findings[0]
    assert "review_diff" in finding.title
    assert finding.evidence[0].snippet.startswith("def review_diff")


async def test_a_rename_inside_the_same_pull_request_is_not_reported() -> None:
    diff = _diff(
        "src/mira/core/engine.py",
        ["def review_diff(self, text):", "    ..."],
        ["def review_diff(self, text):", "    ..."],
    )
    outcome = await breaking.run(_ctx(diff))
    assert outcome.state == "skipped"


async def test_a_new_required_parameter_is_reported() -> None:
    diff = _diff(
        "src/mira/core/engine.py",
        ["def review(self, text, mode):"],
        ["def review(self, text):"],
    )
    outcome = await breaking.run(_ctx(diff))
    assert outcome.state == "violation"
    assert any("mode" in f.title for f in outcome.findings)


async def test_a_new_optional_parameter_is_not_a_break() -> None:
    diff = _diff(
        "src/mira/core/engine.py",
        ['def review(self, text, mode="fast"):'],
        ["def review(self, text):"],
    )
    outcome = await breaking.run(_ctx(diff))
    assert outcome.state == "skipped"


async def test_a_removed_route_is_reported() -> None:
    diff = _diff("src/mira/dashboard/routers/core.py", [], ['@router.get("/api/legacy")'])
    outcome = await breaking.run(_ctx(diff))
    assert outcome.state == "violation"
    assert any("/api/legacy" in f.title for f in outcome.findings)


async def test_a_removed_environment_key_is_reported() -> None:
    diff = _diff(".env.example", [], ["MIRA_OLD_SETTING=1"])
    outcome = await breaking.run(_ctx(diff))
    assert outcome.state == "violation"
    assert any("MIRA_OLD_SETTING" in f.title for f in outcome.findings)


async def test_a_removed_test_helper_is_not_a_breaking_change() -> None:
    diff = _diff("tests/test_engine.py", [], ["def helper():", "    pass"])
    outcome = await breaking.run(_ctx(diff))
    assert outcome.state == "skipped"


# ──────────────────────────────────────────────────────────── migrations ──


async def test_a_destructive_migration_is_reported_at_its_own_line() -> None:
    diff = _diff("db/migrations/0042_drop.sql", ["ALTER TABLE users DROP COLUMN email;"])
    outcome = await migrations.run(_ctx(diff))
    assert outcome.state == "violation"
    finding = outcome.findings[0]
    assert "drop" in finding.title.lower()
    assert finding.evidence[0].path == "db/migrations/0042_drop.sql"
    assert finding.evidence[0].start_line == 1


async def test_a_change_with_no_schema_statement_is_skipped() -> None:
    diff = _diff("src/mira/core/engine.py", ["x = 1"])
    outcome = await migrations.run(_ctx(diff))
    assert outcome.state == "skipped"
    assert outcome.skip_reason == SkipReason.NOT_APPLICABLE


class _Files:
    """A provider that only knows how to hand back file bodies."""

    def __init__(self, files: dict[str, str]) -> None:
        self.files = files

    async def get_file_content(self, _pr_info, path, _ref):
        return self.files.get(path, "")


async def test_a_migration_with_an_empty_downgrade_is_reported() -> None:
    diff = _diff("alembic/versions/0042_add.py", ["    op.add_column('users', sa.Column('x'))"])
    ctx = _ctx(
        diff,
        provider=_Files(
            {
                "alembic/versions/0042_add.py": (
                    "def upgrade():\n    op.add_column('users', sa.Column('x'))\n\n"
                    "def downgrade():\n    pass\n"
                )
            }
        ),
        pr_info=object(),
    )
    outcome = await migrations.run(ctx)
    assert outcome.state == "violation"
    assert any("no way back" in f.title for f in outcome.findings)


async def test_a_reversible_additive_migration_passes() -> None:
    diff = _diff("alembic/versions/0042_add.py", ["    op.add_column('users', sa.Column('x'))"])
    ctx = _ctx(
        diff,
        provider=_Files(
            {
                "alembic/versions/0042_add.py": (
                    "def upgrade():\n    op.add_column('users', sa.Column('x'))\n\n"
                    "def downgrade():\n    op.drop_column('users', 'x')\n"
                )
            }
        ),
        pr_info=object(),
    )
    outcome = await migrations.run(ctx)
    assert outcome.state == "pass"


async def test_a_migration_that_declares_no_downgrade_at_all_has_no_way_back() -> None:
    """Not "the downgrade is not empty" — there is no downgrade."""
    diff = _diff("alembic/versions/0042_add.py", ["    op.add_column('users', sa.Column('x'))"])
    ctx = _ctx(
        diff,
        provider=_Files(
            {
                "alembic/versions/0042_add.py": (
                    "def upgrade():\n    op.add_column('users', sa.Column('x'))\n"
                )
            }
        ),
        pr_info=object(),
    )
    outcome = await migrations.run(ctx)
    assert outcome.state == "violation"
    assert any("no way back" in f.title for f in outcome.findings)


async def test_a_sql_migration_with_no_rollback_convention_is_not_claimed_either_way() -> None:
    """Its rollback is conventionally a second file this check does not know."""
    diff = _diff("db/migrations/0042_add.sql", ["ALTER TABLE users ADD COLUMN nickname TEXT;"])
    ctx = _ctx(
        diff,
        provider=_Files({"db/migrations/0042_add.sql": "ALTER TABLE users ADD COLUMN x TEXT;\n"}),
        pr_info=object(),
    )
    outcome = await migrations.run(ctx)
    # Not a pass: nothing established that it can be undone. Not a violation
    # either: nothing established that it cannot.
    assert outcome.state == "skipped"
    assert outcome.skip_reason == SkipReason.UNSUPPORTED
    assert "0042_add.sql" in outcome.summary


async def test_an_unassessable_migration_still_keeps_a_blocking_gate_closed() -> None:
    from mira.checks.models import UNANSWERED_SKIPS

    assert SkipReason.UNSUPPORTED in UNANSWERED_SKIPS


async def test_a_destructive_sql_migration_is_still_reported() -> None:
    """The rollback convention being unknown does not excuse a DROP."""
    diff = _diff("db/migrations/0042_drop.sql", ["ALTER TABLE users DROP COLUMN email;"])
    ctx = _ctx(
        diff,
        provider=_Files({"db/migrations/0042_drop.sql": "ALTER TABLE users DROP COLUMN email;\n"}),
        pr_info=object(),
    )
    outcome = await migrations.run(ctx)
    assert outcome.state == "violation"
    assert "not assessed" in outcome.summary


async def test_an_unreadable_migration_is_an_infrastructure_error_not_a_pass() -> None:
    """The check never established reversibility, so it must not claim it."""
    diff = _diff("alembic/versions/0042_add.py", ["    op.add_column('users', sa.Column('x'))"])
    ctx = _ctx(diff, provider=_Files({}), pr_info=object())
    outcome = await migrations.run(ctx)
    assert outcome.state == "infrastructure_error"
    assert "Mira problem" in outcome.summary


# ─────────────────────────────────────────────────────── path classifier ──


@pytest.mark.parametrize(
    "path,is_test,is_doc,is_generated,is_migration",
    [
        ("tests/test_engine.py", True, False, False, False),
        ("src/app/engine_test.go", True, False, False, False),
        ("ui/src/App.test.tsx", True, False, False, False),
        ("docs/guide.md", False, True, False, False),
        ("README.md", False, True, False, False),
        ("uv.lock", False, False, True, False),
        ("ui/dist/main.js", False, False, True, False),
        ("db/migrations/0001.sql", False, False, False, True),
        ("alembic/versions/abc.py", False, False, False, True),
        ("src/mira/core/engine.py", False, False, False, False),
    ],
)
def test_the_path_classifier_agrees_with_itself(
    path: str, is_test: bool, is_doc: bool, is_generated: bool, is_migration: bool
) -> None:
    assert native_paths.is_test(path) is is_test
    assert native_paths.is_doc(path) is is_doc
    assert native_paths.is_generated(path) is is_generated
    assert native_paths.is_migration(path) is is_migration


def test_a_generated_file_is_never_counted_as_source() -> None:
    assert native_paths.is_source("uv.lock") is False
    assert native_paths.is_source("ui/dist/main.js") is False
    assert native_paths.is_source("src/mira/core/engine.py") is True
