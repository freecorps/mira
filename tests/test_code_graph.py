"""Tests for the AST code graph: parsing, blast radius and the regex fallback."""

from __future__ import annotations

import asyncio
import difflib
import json
import sys
import time
from unittest.mock import AsyncMock, MagicMock

import pytest

from mira.config import MiraConfig, ReviewConfig
from mira.core.diff_parser import parse_diff
from mira.index import code_graph as cg
from mira.index.code_graph import (
    BlastRadius,
    ChangedSymbol,
    CodeGraph,
    Reference,
    base_source,
    build_blast_radius,
    changed_symbols,
    match_path,
    parse_file,
    render_blast_radius,
)
from mira.models import FileChangeType


def _grammar_ready(grammar: str) -> bool:
    try:
        return cg._parser_for(grammar) is not None
    except Exception:  # noqa: BLE001
        return False


def needs_grammar(grammar: str):  # type: ignore[no-untyped-def]
    """Skip when tree-sitter, or the grammar (downloaded on first use), is unavailable."""
    return pytest.mark.skipif(
        not _grammar_ready(grammar), reason=f"tree-sitter grammar {grammar} unavailable"
    )


@pytest.fixture
def no_tree_sitter(monkeypatch):  # type: ignore[no-untyped-def]
    """tree-sitter as if it were not installed: the import fails."""
    monkeypatch.setitem(sys.modules, "tree_sitter_language_pack", None)
    monkeypatch.setattr(cg, "_ts_state", None)
    monkeypatch.setattr(cg, "_ts_module", None)
    yield
    # monkeypatch restores the module state; the next caller re-imports.


def _diff(path: str, before: str, after: str) -> str:
    lines = list(
        difflib.unified_diff(
            before.splitlines(keepends=True),
            after.splitlines(keepends=True),
            fromfile=f"a/{path}",
            tofile=f"b/{path}",
        )
    )
    return f"diff --git a/{path} b/{path}\n" + "".join(lines)


def _file(path: str, before: str, after: str):  # type: ignore[no-untyped-def]
    return parse_diff(_diff(path, before, after)).files[0]


class _Fetcher:
    def __init__(self, sources: dict[str, str]):
        self.sources = sources
        self.fetched: list[str] = []

    async def fetch(self, path: str) -> str | None:
        self.fetched.append(path)
        return self.sources.get(path)


class _SnapshotFetcher(_Fetcher):
    def __init__(self, sources: dict[str, str]):
        super().__init__(sources)
        from mira.platforms.fetch import RepoSnapshot

        self._snapshot = RepoSnapshot(files=dict(sources), paths=set(sources))

    def loaded(self):  # type: ignore[no-untyped-def]
        return self._snapshot


# ── Parsing ──


class TestParsePython:
    SOURCE = (
        "class Store:\n"
        "    def __init__(self, path):\n"
        "        self.path = path\n"
        "\n"
        "    def save(self, key: str, value: bytes) -> None:\n"
        "        write_blob(self.path, key, value)\n"
        "\n"
        "def write_blob(path, key, value):\n"
        "    store = Store(path)\n"
        "    return store.save(key, value)\n"
    )

    @needs_grammar("python")
    def test_definitions_are_qualified_with_signatures(self):
        graph = parse_file("pkg/store.py", self.SOURCE)
        assert graph is not None and graph.backend == "tree-sitter"
        by_name = {d.qualified_name: d for d in graph.definitions}
        assert set(by_name) == {"Store", "Store.__init__", "Store.save", "write_blob"}
        save = by_name["Store.save"]
        assert save.kind == "method"
        assert (save.start_line, save.end_line) == (5, 6)
        assert save.signature == "def save(self, key: str, value: bytes) -> None"
        assert by_name["write_blob"].kind == "function"
        assert by_name["Store"].kind == "class"

    @needs_grammar("python")
    def test_references_know_their_enclosing_definition(self):
        graph = parse_file("pkg/store.py", self.SOURCE)
        assert graph is not None
        refs = {(r.name, r.line, r.enclosing) for r in graph.references}
        assert ("write_blob", 6, "Store.save") in refs
        assert ("Store", 9, "write_blob") in refs
        assert ("save", 10, "write_blob") in refs
        call = next(r for r in graph.references if r.name == "Store")
        assert call.text == "store = Store(path)"

    @needs_grammar("python")
    def test_a_string_or_comment_is_not_a_call(self):
        graph = parse_file("a.py", "# write_blob(x)\nmsg = 'write_blob(y)'\n")
        assert graph is not None
        assert [r for r in graph.references if r.name == "write_blob"] == []


@pytest.mark.parametrize(
    ("path", "source", "definitions", "references"),
    [
        (
            "web/api.js",
            "export function load(id, opts) { return fetchJson(id); }\n"
            "const save = async (item) => { client.post(item); new Queue(1); };\n"
            "class Cache { constructor(size) {} get(key) { return this.lookup(key); } }\n",
            {"load", "save", "Cache", "Cache.constructor", "Cache.get"},
            {("fetchJson", "load"), ("post", "save"), ("Queue", "save"), ("lookup", "Cache.get")},
        ),
        (
            "web/api.ts",
            "export function load(id: string): Promise<Item> { return fetchJson(id); }\n"
            "interface Repo { find(id: string): Item }\n"
            "class Svc { run(x: number): void { helper(x); } }\n",
            {"load", "Repo", "Repo.find", "Svc", "Svc.run"},
            {("fetchJson", "load"), ("helper", "Svc.run")},
        ),
        (
            "web/Button.tsx",
            "export const Button = (p: Props) => <b onClick={() => track(p)} />;\n",
            {"Button"},
            {("track", "Button")},
        ),
        (
            "srv/handler.go",
            "package srv\n"
            "func Handle(w Writer, r *Request) error { return decode(r) }\n"
            "func (s *Server) Serve(addr string) { s.listen(addr); log.Printf(addr) }\n"
            "type Server struct { addr string }\n",
            {"Handle", "Server.Serve", "Server"},
            {("decode", "Handle"), ("listen", "Server.Serve"), ("Printf", "Server.Serve")},
        ),
        (
            "src/lib.rs",
            "pub fn parse(input: &str) -> Result<Ast> { lex(input); Ast::new(1) }\n"
            "impl Ast { pub fn new(n: i32) -> Self { build(n) } }\n"
            "trait Visit { fn visit(&self, n: i32); }\n",
            {"parse", "Ast.new", "Visit", "Visit.visit"},
            {("lex", "parse"), ("new", "parse"), ("build", "Ast.new")},
        ),
        (
            "src/Billing.java",
            "class Billing {\n"
            "  public Billing(int rate) {}\n"
            "  @Override public long charge(int cents) throws IOException {\n"
            "    audit(cents); ledger.post(cents); new Receipt(cents); return 0;\n"
            "  }\n"
            "}\n",
            {"Billing", "Billing.Billing", "Billing.charge"},
            {("audit", "Billing.charge"), ("post", "Billing.charge"), ("Receipt", "Billing.charge")},
        ),
    ],
)
def test_languages(path, source, definitions, references):  # type: ignore[no-untyped-def]
    grammar = cg.grammar_for_path(path)
    assert grammar is not None
    if not _grammar_ready(grammar):
        pytest.skip(f"tree-sitter grammar {grammar} unavailable")
    graph = parse_file(path, source)
    assert graph is not None and graph.backend == "tree-sitter"
    assert {d.qualified_name for d in graph.definitions} == definitions
    assert references <= {(r.name, r.enclosing) for r in graph.references}


@needs_grammar("go")
def test_go_signature_keeps_receiver_and_results():
    graph = parse_file("a.go", "package a\nfunc (s *Srv) Get(id int) (Item, error) {\n}\n")
    assert graph is not None
    assert graph.definitions[0].signature == "func (s *Srv) Get(id int) (Item, error)"


@needs_grammar("typescript")
def test_typescript_return_type_survives_signature_trim():
    graph = parse_file("a.ts", "function f(a: number): Promise<Map<string, number>> {\n}\n")
    assert graph is not None
    assert graph.definitions[0].signature == "function f(a: number): Promise<Map<string, number>>"


def test_unsupported_files_are_not_parsed():
    assert parse_file("README.md", "# hi") is None
    assert parse_file("a.py", "") is None
    assert cg.grammar_for_path("x.rb") is None


# ── Fallback without tree-sitter ──


class TestRegexFallback:
    def test_missing_dependency_is_detected(self, no_tree_sitter):  # type: ignore[no-untyped-def]
        assert cg.tree_sitter_available() is False
        assert cg._parser_for("python") is None

    def test_parse_falls_back_to_regex(self, no_tree_sitter):  # type: ignore[no-untyped-def]
        graph = parse_file("pkg/store.py", TestParsePython.SOURCE)
        assert graph is not None and graph.backend == "regex"
        names = {d.name for d in graph.definitions}
        assert {"Store", "save", "write_blob"} <= names
        refs = {(r.name, r.line) for r in graph.references}
        assert ("write_blob", 6) in refs
        assert ("Store", 9) in refs
        save = next(d for d in graph.definitions if d.name == "save")
        assert save.signature == "def save(self, key: str, value: bytes) -> None"

    def test_regex_fallback_reads_one_line_definitions(self, no_tree_sitter):  # type: ignore[no-untyped-def]
        graph = parse_file("a.js", "export function f(a, b) { return g(a); }\n")
        assert graph is not None
        assert graph.definitions[0].signature == "export function f(a, b)"
        assert [r.name for r in graph.references] == ["g"]

    def test_a_grammar_that_fails_to_load_falls_back(self, monkeypatch):  # type: ignore[no-untyped-def]
        pack = MagicMock()
        pack.get_parser.side_effect = RuntimeError("download refused")
        monkeypatch.setattr(cg, "_ts_state", True)
        monkeypatch.setattr(cg, "_ts_module", pack)
        monkeypatch.setattr(cg, "_ts_broken", set())
        monkeypatch.setattr(cg, "_ts_local", type(cg._ts_local)())
        graph = parse_file("a.go", "package a\nfunc F() { G() }\n")
        assert graph is not None and graph.backend == "regex"
        assert "go" in cg._ts_broken
        # Asked once, not once per file.
        parse_file("b.go", "package a\nfunc H() {}\n")
        assert pack.get_parser.call_count == 1

    def test_a_parser_that_raises_falls_back(self, monkeypatch):  # type: ignore[no-untyped-def]
        def _boom(*_a, **_k):  # type: ignore[no-untyped-def]
            raise ValueError("bad tree")

        monkeypatch.setattr(cg, "_parse_tree_sitter", _boom)
        graph = parse_file("a.py", "def f():\n    g()\n")
        assert graph is not None and graph.backend == "regex"

    @pytest.mark.asyncio
    async def test_blast_radius_works_without_tree_sitter(self, no_tree_sitter):  # type: ignore[no-untyped-def]
        before = "def compute(a):\n    return a\n"
        after = "def compute(a, b):\n    return a + b\n"
        caller = "from m import compute\n\ndef run():\n    return compute(1)\n"
        fetcher = _SnapshotFetcher({"m.py": after, "app.py": caller})
        radius = await build_blast_radius([_file("m.py", before, after)], fetcher)
        assert radius.backend == "regex"
        [sym] = radius.symbols
        assert [(r.path, r.line) for r in sym.callers] == [("app.py", 4)]


# ── Diff helpers ──


class TestBaseSource:
    BEFORE = "".join(f"line {i}\n" for i in range(1, 41))

    def test_rebuilds_the_old_file_from_several_hunks(self):
        lines = self.BEFORE.splitlines(keepends=True)
        lines[2] = "changed 3\n"
        lines.insert(20, "inserted\n")
        del lines[35]
        after = "".join(lines)
        file = _file("a.py", self.BEFORE, after)
        assert len(file.hunks) >= 2
        assert base_source(after, file) == self.BEFORE

    def test_additions_at_the_start_and_end(self):
        after = "first\n" + self.BEFORE + "last\n"
        assert base_source(after, _file("a.py", self.BEFORE, after)) == self.BEFORE

    def test_a_head_that_does_not_fit_the_diff_gives_none(self):
        after = self.BEFORE.replace("line 10\n", "line ten\n")
        file = _file("a.py", self.BEFORE, after)
        assert base_source(after.replace("line 9\n", "something else\n"), file) is None

    def test_deleted_and_added_files(self):
        deleted = parse_diff(
            "diff --git a/old.py b/old.py\ndeleted file mode 100644\n--- a/old.py\n+++ /dev/null\n"
            "@@ -1,2 +0,0 @@\n-def gone(a):\n-    pass\n"
        ).files[0]
        assert deleted.change_type == FileChangeType.DELETED
        assert base_source(None, deleted) == "def gone(a):\n    pass"
        added = _file("new.py", "", "x = 1\n")
        added.change_type = FileChangeType.ADDED
        assert base_source("x = 1\n", added) == ""

    def test_added_lines(self):
        after = self.BEFORE.replace("line 5\n", "five\n")
        assert cg.added_lines(_file("a.py", self.BEFORE, after)) == {5}


# ── Changed symbols ──


class TestChangedSymbols:
    def _changes(self, path: str, before: str, after: str) -> list[ChangedSymbol]:
        return changed_symbols([_file(path, before, after)], {path: after})

    def test_a_new_parameter_is_a_signature_change(self):
        [sym] = self._changes(
            "m.py",
            "def compute(a):\n    return a\n",
            "def compute(a, b):\n    return a + b\n",
        )
        assert (sym.qualified_name, sym.change) == ("compute", "signature")
        assert sym.old_signature == "def compute(a)"
        assert sym.new_signature == "def compute(a, b)"

    def test_a_body_edit_is_not(self):
        assert self._changes("m.py", "def f(a):\n    return a\n", "def f(a):\n    return -a\n") == []

    def test_a_reformatted_signature_is_not(self):
        assert (
            self._changes(
                "m.py",
                "def compute(alpha, beta):\n    pass\n",
                "def compute(\n    alpha,\n    beta,\n):\n    pass\n",
            )
            == []
        )

    def test_a_removed_function_and_class(self):
        changes = self._changes(
            "m.py",
            "class Parser:\n    pass\n\ndef tokenize(s):\n    pass\n\ndef keep():\n    pass\n",
            "def keep():\n    pass\n",
        )
        assert {(s.qualified_name, s.change, s.kind) for s in changes} == {
            ("Parser", "removed", "class"),
            ("tokenize", "removed", "function"),
        }

    def test_common_method_names_are_skipped(self):
        assert (
            self._changes(
                "m.py",
                "class A:\n    def get(self, k):\n        pass\n",
                "class A:\n    def get(self, k, default):\n        pass\n",
            )
            == []
        )

    def test_constructor_is_called_by_its_class(self):
        [sym] = self._changes(
            "m.py",
            "class Client:\n    def __init__(self, url):\n        pass\n",
            "class Client:\n    def __init__(self, url, token):\n        pass\n",
        )
        assert sym.callee_name == "Client"

    def test_cap(self):
        before = "".join(f"def func_{i}(a):\n    pass\n" for i in range(20))
        after = "".join(f"def func_{i}(a, b):\n    pass\n" for i in range(20))
        assert len(changed_symbols([_file("m.py", before, after)], {"m.py": after})) == 12


# ── Blast radius ──


BEFORE_LIB = "def compute(a):\n    return a\n\ndef other():\n    return 1\n"
AFTER_LIB = "def compute(a, b):\n    return a + b\n\ndef other():\n    return 1\n"


class TestBlastRadius:
    @pytest.mark.asyncio
    async def test_finds_callers_elsewhere_and_skips_updated_ones(self):
        app_before = "from lib import compute\n\ndef run():\n    return compute(1)\n"
        app_after = "from lib import compute\n\ndef run():\n    return compute(1, 2)\n"
        sources = {
            "lib.py": AFTER_LIB,
            "app.py": app_after,
            "jobs/worker.py": "import lib\n\ndef work():\n    lib.compute(3)\n",
            "web/compute.js": "compute(1)\n",  # another language: not a caller
            "docs/notes.py": "x = 1\n",
        }
        files = [_file("lib.py", BEFORE_LIB, AFTER_LIB), _file("app.py", app_before, app_after)]
        radius = await build_blast_radius(files, _SnapshotFetcher(sources))
        assert radius.complete
        [sym] = radius.symbols
        assert sym.qualified_name == "compute"
        assert [(r.path, r.line, r.enclosing) for r in sym.callers] == [
            ("jobs/worker.py", 4, "work")
        ]
        assert sym.updated_callers == 1
        assert radius.files_searched == 4  # only files that mention the name (not notes.py)

    @pytest.mark.asyncio
    async def test_no_snapshot_reads_dependents_and_neighbours(self):
        sources = {
            "pkg/lib.py": AFTER_LIB,
            "pkg/sibling.py": "from .lib import compute\ncompute(1)\n",
            "far/user.py": "from pkg.lib import compute\ncompute(2)\n",
            "far/unrelated.py": "compute(3)\n",
        }
        fetcher = _Fetcher(sources)

        async def _dependents(paths: list[str]) -> list[str]:
            assert paths == ["pkg/lib.py"]
            return ["far/user.py"]

        radius = await build_blast_radius(
            [_file("pkg/lib.py", BEFORE_LIB, AFTER_LIB)],
            fetcher,
            repo_tree=list(sources),
            dependents=_dependents,
        )
        assert not radius.complete
        callers = {r.path for r in radius.symbols[0].callers}
        assert callers == {"pkg/sibling.py", "far/user.py"}
        assert "far/unrelated.py" not in fetcher.fetched

    @pytest.mark.asyncio
    async def test_caps_mark_the_search_incomplete(self):
        sources = {"lib.py": AFTER_LIB} | {f"u{i}.py": "compute(1)\n" for i in range(10)}
        radius = await build_blast_radius(
            [_file("lib.py", BEFORE_LIB, AFTER_LIB)],
            _SnapshotFetcher(sources),
            max_files=4,
            max_callers=2,
        )
        assert not radius.complete
        sym = radius.symbols[0]
        assert len(sym.callers) == 2 and sym.more_callers == 1

    @pytest.mark.asyncio
    async def test_time_budget_stops_parsing(self, monkeypatch):  # type: ignore[no-untyped-def]
        sources = {"lib.py": AFTER_LIB} | {f"u{i}.py": "compute(1)\n" for i in range(5)}
        real = CodeGraph.add_files

        def _late(self, items, deadline=None):  # type: ignore[no-untyped-def]
            return real(self, items, time.monotonic() - 1)

        monkeypatch.setattr(CodeGraph, "add_files", _late)
        radius = await build_blast_radius(
            [_file("lib.py", BEFORE_LIB, AFTER_LIB)], _SnapshotFetcher(sources)
        )
        assert not radius.complete
        assert radius.files_searched == 0

    @pytest.mark.asyncio
    async def test_a_failing_fetcher_never_fails_the_review(self):
        class _Broken:
            async def fetch(self, path: str) -> str | None:
                raise RuntimeError("network down")

        radius = await build_blast_radius([_file("lib.py", BEFORE_LIB, AFTER_LIB)], _Broken())
        assert radius.symbols == []

    @pytest.mark.asyncio
    async def test_an_internal_error_is_contained(self, monkeypatch):  # type: ignore[no-untyped-def]
        def _boom(*_a, **_k):  # type: ignore[no-untyped-def]
            raise RuntimeError("bug")

        monkeypatch.setattr(cg, "changed_symbols", _boom)
        radius = await build_blast_radius(
            [_file("lib.py", BEFORE_LIB, AFTER_LIB)], _SnapshotFetcher({"lib.py": AFTER_LIB})
        )
        assert radius.symbols == [] and not radius.complete

    @pytest.mark.asyncio
    async def test_only_added_or_unparseable_files_have_nothing(self):
        added = _file("new.py", "", "def f(a):\n    pass\n")
        added.change_type = FileChangeType.ADDED
        readme = _file("README.md", "a\n", "b\n")
        radius = await build_blast_radius([added, readme], _SnapshotFetcher({}))
        assert radius.symbols == []

    @pytest.mark.asyncio
    async def test_a_removed_symbol_and_its_callers(self):
        before = "def legacy_load(p):\n    pass\n\ndef keep():\n    pass\n"
        after = "def keep():\n    pass\n"
        sources = {"lib.py": after, "cli.py": "from lib import legacy_load\nlegacy_load('x')\n"}
        radius = await build_blast_radius([_file("lib.py", before, after)], _SnapshotFetcher(sources))
        [sym] = radius.symbols
        assert sym.change == "removed"
        assert [(r.path, r.line) for r in sym.callers] == [("cli.py", 2)]


class TestRender:
    def _radius(self) -> BlastRadius:
        sym = ChangedSymbol(
            path="lib.py",
            name="compute",
            qualified_name="compute",
            kind="function",
            change="signature",
            old_signature="def compute(a)",
            new_signature="def compute(a, b)",
            callers=[Reference("app.py", "compute", 4, "run", "return compute(1)")],
            updated_callers=2,
        )
        quiet = ChangedSymbol(
            path="lib.py",
            name="quiet",
            qualified_name="quiet",
            kind="function",
            change="signature",
            old_signature="def quiet()",
            new_signature="def quiet(x)",
        )
        return BlastRadius(symbols=[sym, quiet], files_searched=7, complete=True)

    def test_renders_the_part_s_own_symbols(self):
        text = render_blast_radius(self._radius(), ["lib.py"], 4000)
        assert "## Callers of changed symbols" in text
        assert "`compute` in `lib.py` — signature changed" in text
        assert "- before: `def compute(a)`" in text and "- after: `def compute(a, b)`" in text
        assert "`app.py:4` in `run`: `return compute(1)`" in text
        assert "2 more on lines this PR changes" in text
        assert "all 7 files" in text
        # A signature change with no caller anywhere is not worth the tokens.
        assert "quiet" not in text

    def test_other_parts_symbols_and_tiny_budgets_render_nothing(self):
        assert render_blast_radius(self._radius(), ["other.py"], 4000) == ""
        assert render_blast_radius(self._radius(), ["lib.py"], 50) == ""
        assert render_blast_radius(self._radius(), ["lib.py"], 0) == ""

    def test_partial_search_says_so(self):
        radius = self._radius()
        radius.complete = False
        assert "an absent caller proves nothing" in render_blast_radius(radius, ["lib.py"], 4000)


def test_match_path():
    assert match_path("src/a/b.py", None)
    assert match_path("src/a/b.py", "src/a/")
    assert match_path("src/a/b.py", "src/a")
    assert not match_path("src/ab/c.py", "src/a")
    assert match_path("src/a/b.py", "src/a/b.py")
    assert match_path("src/a/b.py", "**/*.py")
    assert not match_path("src/a/b.ts", "*.py")
    assert match_path(".github/x.py", ".github/")
    assert match_path("src/a.py", "./src/")


def test_graph_definitions_of_qualified_names():
    graph = CodeGraph()
    graph.add_file("a.py", "class A:\n    def run_job(self):\n        pass\n")
    graph.add_file("b.py", "class B:\n    def run_job(self):\n        pass\n")
    assert {d.path for d in graph.definitions_of("run_job")} == {"a.py", "b.py"}
    assert [d.path for d in graph.definitions_of("B.run_job")] == ["b.py"]


# ── Config ──


def test_config_shorthand_and_limits():
    assert ReviewConfig(code_graph=False).code_graph.enabled is False
    assert ReviewConfig().code_graph.enabled is True
    assert ReviewConfig(code_graph={"max_files": 5}).code_graph.max_files == 5
    with pytest.raises(ValueError):
        ReviewConfig(code_graph={"max_symbols": 0})


# ── Engine ──


def _engine_config(enabled: bool = True) -> MiraConfig:
    cfg = MiraConfig()
    for knob in (
        "walkthrough",
        "security_pass",
        "dependency_overlap",
        "self_critique",
        "include_summary",
        "code_context",
        "agentic_tools",
    ):
        setattr(cfg.review, knob, False)
    cfg.review.code_graph.enabled = enabled
    return cfg


async def _review_prompt(config: MiraConfig) -> str:
    from mira.core.engine import ReviewEngine
    from mira.models import PRInfo
    from mira.platforms.fetch import RepoSnapshot

    files = {
        "src/lib.py": AFTER_LIB,
        "src/app.py": "from .lib import compute\n\ndef run():\n    return compute(1)\n",
    }
    provider = MagicMock()
    provider.get_repo_snapshot = AsyncMock(
        return_value=RepoSnapshot(files=files, paths=set(files))
    )
    provider.get_file_content = AsyncMock(return_value="")
    provider.get_repo_tree = AsyncMock(return_value=list(files))
    model = MagicMock()
    model.count_tokens.side_effect = lambda text: (len(text) + 3) // 4
    model.review = AsyncMock(return_value=json.dumps({"comments": [], "summary": "ok"}))
    model.usage = {}
    scope = PRInfo(
        title="t",
        description="",
        base_branch="main",
        head_branch="feature",
        url="https://github.com/o/r/pull/1",
        number=1,
        owner="o",
        repo="r",
        head_sha="abc123",
    )
    engine = ReviewEngine(config=config, llm=model, provider=provider)
    await engine.review_diff(_diff("src/lib.py", BEFORE_LIB, AFTER_LIB), repo_scope=scope)
    assert engine._blast_radius_task is None  # cleaned up with the review
    return model.review.call_args.args[0][-1]["content"]


@pytest.mark.asyncio
async def test_engine_puts_callers_in_the_part_prompt():
    prompt = await _review_prompt(_engine_config())
    assert "## Callers of changed symbols" in prompt
    assert "`src/app.py:4` in `run`: `return compute(1)`" in prompt


@pytest.mark.asyncio
async def test_engine_without_code_graph_has_no_callers_block():
    prompt = await _review_prompt(_engine_config(enabled=False))
    assert "Callers of changed symbols" not in prompt


@pytest.mark.asyncio
async def test_engine_callers_block_respects_zero_tokens():
    config = _engine_config()
    config.review.code_graph.callers_tokens = 0
    prompt = await _review_prompt(config)
    assert "Callers of changed symbols" not in prompt


# ── Index search ──


def test_index_search_ranks_by_distinct_terms(tmp_path):  # type: ignore[no-untyped-def]
    from mira.index.store import FileSummary, IndexStore, SymbolInfo

    store = IndexStore(str(tmp_path / "r.db"))
    try:
        store.upsert_summary(
            FileSummary(
                path="src/limits/rate_limiter.py",
                language="python",
                summary="Token bucket rate limiting for API requests.",
                symbols=[SymbolInfo("TokenBucket", "class", "class TokenBucket", "A bucket")],
            )
        )
        store.upsert_summary(
            FileSummary(
                path="src/api/routes.py",
                language="python",
                summary="HTTP routes; applies the limiter.",
                symbols=[SymbolInfo("handle_request", "function", "def handle_request()", "")],
            )
        )
        store.upsert_summary(
            FileSummary(path="src/odd_%_name.py", language="python", summary="unrelated")
        )
        matches = store.search_index("which file handles rate limiting?", 5)
        assert matches[0].path == "src/limits/rate_limiter.py"
        assert matches[0].summary.startswith("Token bucket")
        assert store.search_index("tokenbucket", 5)[0].symbols == ["TokenBucket"]
        assert store.search_index("the which", 5) == []
        # LIKE wildcards in the query are literal.
        assert [m.path for m in store.search_index("%", 5)] == []
    finally:
        store.close()


def test_snapshot_wait_is_bounded():
    """`repo_snapshot` returns None, not hangs, when the archive never lands."""

    class _Slow:
        def loaded(self):  # type: ignore[no-untyped-def]
            return None

        async def snapshot(self, max_wait=None):  # type: ignore[no-untyped-def]
            await asyncio.sleep(max_wait or 0)
            return None

    assert asyncio.run(cg.repo_snapshot(_Slow(), 0.01)) is None
