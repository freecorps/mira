"""Definitions and call sites from real syntax trees, and the blast radius of a PR.

The regex extractor (``index/extract.py``) finds where a function starts and
roughly where it ends; it cannot say who calls it. This module parses source
with tree-sitter into a small definition/reference graph:

- a *definition* is a function, method or class, with its qualified name
  (``Class.method``, ``Receiver.Method``), its line span and its signature
  text — everything from the keyword to the body;
- a *reference* is a call (or constructor use) of a name, with the line it is
  on and the definition it sits in.

Two things are built on it. The *blast radius* of a pull request: symbols
whose signature the diff changes, or which it removes, and the call sites the
diff does not touch — what a reviewer reading only the diff cannot see. And
the reviewer's ``find_usages`` / ``find_definition`` tools.

tree-sitter is optional (``pip install mira-reviewer[graph]``; the Docker image has
it). Without it, or for a language it has no grammar for, every function here
answers from the regex extractor and a word-boundary search instead: less
precise — a mention in a comment counts — never an exception. Nothing in this
module may fail a review.
"""

from __future__ import annotations

import asyncio
import fnmatch
import logging
import re
import threading
import time
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import Any

from mira.index.extract import SymbolSpan, extract_symbols
from mira.models import FileChangeType, FileDiff

logger = logging.getLogger(__name__)

# ── Languages ──

# File extension → grammar name in tree-sitter-language-pack.
_EXT_GRAMMAR: dict[str, str] = {
    ".py": "python",
    ".pyi": "python",
    ".js": "javascript",
    ".jsx": "javascript",
    ".mjs": "javascript",
    ".cjs": "javascript",
    ".ts": "typescript",
    ".mts": "typescript",
    ".cts": "typescript",
    ".tsx": "tsx",
    ".go": "go",
    ".rs": "rust",
    ".java": "java",
}

# Grammars whose code can call each other: a TypeScript module imports a
# JavaScript one, a TSX component calls a TS helper.
_FAMILY: dict[str, str] = {
    "python": "python",
    "javascript": "js",
    "typescript": "js",
    "tsx": "js",
    "go": "go",
    "rust": "rust",
    "java": "java",
}


def grammar_for_path(path: str) -> str | None:
    """The tree-sitter grammar a path is parsed with, or None when unsupported."""
    return _EXT_GRAMMAR.get(PurePosixPath(path).suffix.lower())


def _family(path: str) -> str | None:
    grammar = grammar_for_path(path)
    return _FAMILY.get(grammar) if grammar else None


# ── tree-sitter, loaded lazily ──

_ts_lock = threading.Lock()
_ts_module: Any = None
_ts_state: bool | None = None  # None: not tried yet
_ts_broken: set[str] = set()  # grammars that failed to load
_ts_local = threading.local()  # one Parser per grammar per thread: they are not thread-safe


def _language_pack() -> Any:
    """The tree-sitter language pack, or None when it is not installed."""
    global _ts_module, _ts_state
    if _ts_state is None:
        with _ts_lock:
            if _ts_state is None:
                try:
                    import tree_sitter  # noqa: F401
                    import tree_sitter_language_pack

                    _ts_module = tree_sitter_language_pack
                    _ts_state = True
                except Exception as exc:  # noqa: BLE001 — ImportError, or a broken wheel
                    logger.info("tree-sitter unavailable, code graph uses regex: %s", exc)
                    _ts_state = False
    return _ts_module if _ts_state else None


def tree_sitter_available() -> bool:
    """Whether the optional tree-sitter dependency can be imported."""
    return _language_pack() is not None


def _parser_for(grammar: str) -> Any:
    """A parser for ``grammar`` owned by this thread, or None.

    The language pack downloads a grammar the first time it is asked for
    (the Docker image ships them pre-fetched); a grammar that cannot be had is
    remembered, so a review without network asks once and not per file.
    """
    pack = _language_pack()
    if pack is None or grammar in _ts_broken:
        return None
    parsers = getattr(_ts_local, "parsers", None)
    if parsers is None:
        parsers = _ts_local.parsers = {}
    parser = parsers.get(grammar)
    if parser is None:
        try:
            parser = pack.get_parser(grammar)
        except Exception as exc:  # noqa: BLE001 — download refused, ABI mismatch, …
            logger.info("tree-sitter grammar %s unavailable: %s", grammar, exc)
            _ts_broken.add(grammar)
            return None
        parsers[grammar] = parser
    return parser


# ── The graph ──


@dataclass(frozen=True)
class Definition:
    """A function, method or class definition."""

    path: str
    name: str
    qualified_name: str
    kind: str  # "function", "method", "class"
    start_line: int  # 1-based
    end_line: int  # 1-based, inclusive
    signature: str  # whitespace-collapsed text from the keyword to the body


@dataclass(frozen=True)
class Reference:
    """A call of ``name`` (or a constructor use of it)."""

    path: str
    name: str
    line: int  # 1-based
    enclosing: str  # qualified name of the definition it sits in, "" at top level
    text: str  # the source line, stripped and capped


@dataclass
class FileGraph:
    path: str
    backend: str  # "tree-sitter" or "regex"
    definitions: list[Definition] = field(default_factory=list)
    references: list[Reference] = field(default_factory=list)
    lines: list[str] = field(default_factory=list)


_MAX_REF_TEXT = 200
_CALL_RE = re.compile(r"\b([A-Za-z_$][\w$]*)\s*(?:<[^<>()]*>)?\s*\(")
# Words that look like calls to the regex fallback but are syntax.
_NOT_CALLS = frozenset(
    [
        "if",
        "elif",
        "while",
        "for",
        "switch",
        "catch",
        "return",
        "function",
        "func",
        "fn",
        "def",
        "class",
        "new",
        "typeof",
        "sizeof",
        "await",
        "yield",
        "print",
        "super",
        "assert",
        "lambda",
        "match",
        "with",
        "except",
        "not",
        "and",
        "or",
        "in",
        "is",
        "else",
        "do",
        "try",
        "synchronized",
    ]
)
_WS_RE = re.compile(r"\s+")
# What is left of a body opener after cutting at the body: `{`, `:`, `=>`, `=`.
_SIG_TAIL_RE = re.compile(r"(?:\s*(?:=>|[{:=;]))+\s*$")

# Per grammar: definition node types → kind, and call node types.
_DEF_TYPES: dict[str, dict[str, str]] = {
    "python": {"function_definition": "function", "class_definition": "class"},
    "javascript": {
        "function_declaration": "function",
        "generator_function_declaration": "function",
        "method_definition": "method",
        "class_declaration": "class",
    },
    "typescript": {
        "function_declaration": "function",
        "generator_function_declaration": "function",
        "function_signature": "function",
        "method_definition": "method",
        "method_signature": "method",
        "abstract_method_signature": "method",
        "class_declaration": "class",
        "abstract_class_declaration": "class",
        "interface_declaration": "class",
    },
    "go": {
        "function_declaration": "function",
        "method_declaration": "method",
        "method_elem": "method",
        "type_spec": "class",
    },
    "rust": {
        "function_item": "function",
        "function_signature_item": "function",
        "struct_item": "class",
        "enum_item": "class",
        "trait_item": "class",
    },
    "java": {
        "method_declaration": "method",
        "constructor_declaration": "method",
        "class_declaration": "class",
        "interface_declaration": "class",
        "enum_declaration": "class",
        "record_declaration": "class",
    },
}
_DEF_TYPES["tsx"] = _DEF_TYPES["typescript"]

_CALL_TYPES: dict[str, frozenset[str]] = {
    "python": frozenset({"call"}),
    "javascript": frozenset({"call_expression", "new_expression"}),
    "typescript": frozenset({"call_expression", "new_expression"}),
    "tsx": frozenset({"call_expression", "new_expression"}),
    "go": frozenset({"call_expression"}),
    "rust": frozenset({"call_expression"}),
    "java": frozenset({"method_invocation", "object_creation_expression"}),
}

# Function values bound to a name: `const f = (x) => …`.
_JS_FUNCTION_VALUES = frozenset({"arrow_function", "function_expression", "function"})


def _text(node: Any) -> str:
    raw = node.text
    return raw.decode("utf-8", "replace") if isinstance(raw, bytes) else str(raw or "")


def _strip_generics(name: str) -> str:
    return re.sub(r"<.*", "", name).replace("*", "").replace("&", "").strip()


def _signature_of(node: Any, source: bytes) -> str:
    """Everything from the definition's start to its body, whitespace-collapsed."""
    body = node.child_by_field_name("body")
    if body is None and node.type == "variable_declarator":
        value = node.child_by_field_name("value")
        body = value.child_by_field_name("body") if value is not None else None
    end = body.start_byte if body is not None else node.end_byte
    sig = source[node.start_byte : end].decode("utf-8", "replace")
    return _SIG_TAIL_RE.sub("", _WS_RE.sub(" ", sig).strip())[:300]


def _callee(node: Any, grammar: str) -> str:
    """The called name of a call node: the last segment of `a.b.c(…)`."""
    if grammar == "java":
        if node.type == "object_creation_expression":
            target = node.child_by_field_name("type")
            return _strip_generics(_text(target)).rsplit(".", 1)[-1] if target else ""
        name = node.child_by_field_name("name")
        return _text(name) if name is not None else ""
    if node.type == "new_expression":
        target = node.child_by_field_name("constructor")
    else:
        target = node.child_by_field_name("function")
    if target is None:
        return ""
    # Unwrap `f<T>(…)` and `obj.f::<T>(…)`.
    while target.type in ("generic_function", "instantiation_expression"):
        inner = target.child_by_field_name("function") or (
            target.children[0] if target.children else None
        )
        if inner is None:
            break
        target = inner
    # `obj.attr` (Python), `obj.prop` (JS), `x.Field` (Go, Rust), `path::name` (Rust).
    for field_name in ("attribute", "property", "field", "name"):
        part = target.child_by_field_name(field_name)
        if part is not None:
            return _text(part)
    if target.type in ("identifier", "type_identifier", "field_identifier", "property_identifier"):
        return _text(target)
    return ""


def _definition_name(node: Any, grammar: str) -> str:
    if node.type == "variable_declarator":
        name = node.child_by_field_name("name")
        return _text(name) if name is not None and name.type == "identifier" else ""
    name = node.child_by_field_name("name")
    return _text(name) if name is not None else ""


def _container_of(node: Any, grammar: str) -> str | None:
    """The name a node adds to its children's qualified names, if it is a container."""
    if grammar == "rust" and node.type == "impl_item":
        target = node.child_by_field_name("type")
        return _strip_generics(_text(target)) if target is not None else None
    return None


def _go_receiver(node: Any) -> str:
    receiver = node.child_by_field_name("receiver")
    if receiver is None:
        return ""
    for param in receiver.children:
        if param.type == "parameter_declaration":
            kind = param.child_by_field_name("type")
            if kind is not None:
                return _strip_generics(_text(kind).split("[", 1)[0])
    return ""


def _parse_tree_sitter(path: str, source: str, grammar: str) -> FileGraph | None:
    parser = _parser_for(grammar)
    if parser is None:
        return None
    data = source.encode("utf-8", "replace")
    tree = parser.parse(data)
    lines = source.split("\n")
    graph = FileGraph(path=path, backend="tree-sitter", lines=lines)
    def_types = _DEF_TYPES[grammar]
    call_types = _CALL_TYPES[grammar]

    # (node, qualifier prefix, enclosing definition, inside a class body)
    stack: list[tuple[Any, str, str, bool]] = [(tree.root_node, "", "", False)]
    while stack:
        node, prefix, enclosing, in_class = stack.pop()
        node_type = node.type
        child_prefix, child_enclosing, child_in_class = prefix, enclosing, in_class

        kind = def_types.get(node_type)
        is_js_binding = (
            node_type == "variable_declarator"
            and grammar in ("javascript", "typescript", "tsx")
            and (value := node.child_by_field_name("value")) is not None
            and value.type in _JS_FUNCTION_VALUES
        )
        if is_js_binding:
            kind = "function"
        if kind is not None:
            name = _definition_name(node, grammar)
            if name:
                if grammar == "go" and node_type == "method_declaration":
                    receiver = _go_receiver(node)
                    qualified = f"{receiver}.{name}" if receiver else name
                else:
                    qualified = f"{prefix}.{name}" if prefix else name
                if kind == "function" and (in_class or prefix):
                    kind = "method"
                graph.definitions.append(
                    Definition(
                        path=path,
                        name=name,
                        qualified_name=qualified,
                        kind=kind,
                        start_line=node.start_point[0] + 1,
                        end_line=node.end_point[0] + 1,
                        signature=(
                            _class_header(_signature_of(node, data))
                            if kind == "class"
                            else _signature_of(node, data)
                        ),
                    )
                )
                child_enclosing = qualified
                if kind == "class":
                    child_prefix = qualified
                    child_in_class = True
                else:
                    # A function nested in a method is not itself a method.
                    child_in_class = False
        else:
            container = _container_of(node, grammar)
            if container:
                child_prefix = f"{prefix}.{container}" if prefix else container
                child_in_class = True

        if node_type in call_types:
            callee = _callee(node, grammar)
            if callee:
                line = node.start_point[0] + 1
                text = lines[line - 1].strip() if 0 < line <= len(lines) else ""
                graph.references.append(
                    Reference(
                        path=path,
                        name=callee,
                        line=line,
                        enclosing=enclosing,
                        text=text[:_MAX_REF_TEXT],
                    )
                )

        children = node.children
        for child in reversed(children):
            stack.append((child, child_prefix, child_enclosing, child_in_class))

    graph.definitions.sort(key=lambda d: (d.start_line, -d.end_line))
    graph.references.sort(key=lambda r: r.line)
    return graph


def _regex_signature(span: SymbolSpan) -> str:
    """The header of a regex-extracted symbol: its first non-decorator line(s)."""
    header: list[str] = []
    for line in span.source.split("\n"):
        stripped = line.strip()
        if not header and (not stripped or stripped.startswith("@")):
            continue
        header.append(stripped)
        if stripped.endswith(("{", ":", ")")) or len(header) >= 4:
            break
    # A one-line definition carries its body: cut at the body's opener.
    text = _WS_RE.sub(" ", " ".join(header)).split("{", 1)[0].split("=>", 1)[0]
    return _class_header(text.strip())[:300]


def _class_header(sig: str) -> str:
    """A header without its members (a Go `type_spec` has no body field)."""
    return _SIG_TAIL_RE.sub("", sig.split("{", 1)[0]).strip()


def _parse_regex(path: str, source: str, language: str) -> FileGraph:
    lines = source.split("\n")
    graph = FileGraph(path=path, backend="regex", lines=lines)
    try:
        spans = extract_symbols(source, language)
    except Exception as exc:  # noqa: BLE001 — a heuristic parser, never a reason to fail
        logger.debug("Regex symbol extraction failed for %s: %s", path, exc)
        spans = []
    for span in spans:
        if span.kind == "impl":
            continue  # a Rust `impl` block defines nothing callable itself
        kind = span.kind if span.kind in ("function", "method") else "class"
        graph.definitions.append(
            Definition(
                path=path,
                name=span.name,
                qualified_name=span.qualified_name or span.name,
                kind=kind,
                start_line=span.start_line,
                end_line=span.end_line,
                signature=_regex_signature(span),
            )
        )
    # A definition's own name on its first line is not a call of it.
    def_names = {(d.start_line, d.name) for d in graph.definitions}
    for lineno, line in enumerate(lines, start=1):
        if "(" not in line:
            continue
        stripped = line.strip()
        if stripped.startswith(("#", "//", "*", "/*")):
            continue
        for match in _CALL_RE.finditer(line):
            name = match.group(1)
            if name in _NOT_CALLS or (lineno, name) in def_names:
                continue
            holding = [d for d in graph.definitions if d.start_line <= lineno <= d.end_line]
            enclosing = (
                min(holding, key=lambda d: d.end_line - d.start_line).qualified_name
                if holding
                else ""
            )
            graph.references.append(
                Reference(
                    path=path,
                    name=name,
                    line=lineno,
                    enclosing=enclosing,
                    text=stripped[:_MAX_REF_TEXT],
                )
            )
    return graph


def parse_file(path: str, source: str, *, use_tree_sitter: bool = True) -> FileGraph | None:
    """The definitions and references of one file, or None for an unsupported file.

    tree-sitter when it is installed and has the grammar; the regex extractor
    otherwise. Never raises.
    """
    grammar = grammar_for_path(path)
    if grammar is None or not source:
        return None
    if use_tree_sitter:
        try:
            parsed = _parse_tree_sitter(path, source, grammar)
            if parsed is not None:
                return parsed
        except Exception as exc:  # noqa: BLE001 — fall back to the regex extractor
            logger.debug("tree-sitter parse of %s failed: %s", path, exc)
    language = {"tsx": "typescript"}.get(grammar, grammar)
    return _parse_regex(path, source, language)


class CodeGraph:
    """Parsed files, cached by path, shared by whoever asks during one review.

    Parsing runs in worker threads (see :meth:`add_files`); the cache is a
    plain dict, and two threads parsing the same file only waste the work.
    """

    def __init__(self, *, use_tree_sitter: bool = True) -> None:
        self.use_tree_sitter = use_tree_sitter
        self._files: dict[str, FileGraph | None] = {}

    def __contains__(self, path: str) -> bool:
        return path in self._files

    def get(self, path: str) -> FileGraph | None:
        return self._files.get(path)

    def add_file(self, path: str, source: str) -> FileGraph | None:
        if path in self._files:
            return self._files[path]
        parsed = parse_file(path, source, use_tree_sitter=self.use_tree_sitter)
        self._files[path] = parsed
        return parsed

    def add_files(
        self, sources: Iterable[tuple[str, str]], deadline: float | None = None
    ) -> tuple[int, bool]:
        """Parse ``sources`` until ``deadline`` (monotonic). CPU-bound: call in a thread.

        Returns how many files are in the graph from this batch, and whether
        the deadline stopped it before the end.
        """
        done = 0
        for path, source in sources:
            if deadline is not None and time.monotonic() > deadline:
                return done, True
            self.add_file(path, source)
            done += 1
        return done, False

    def files(self) -> list[FileGraph]:
        return [g for g in self._files.values() if g is not None]

    def definitions_of(self, symbol: str) -> list[Definition]:
        """Definitions named ``symbol``, plain (`bar`) or qualified (`Foo.bar`)."""
        name = symbol.rsplit(".", 1)[-1].rsplit("::", 1)[-1]
        qualified = symbol.replace("::", ".")
        out: list[Definition] = []
        for graph in self.files():
            for d in graph.definitions:
                if d.name != name:
                    continue
                if qualified != name and not (
                    d.qualified_name == qualified or d.qualified_name.endswith("." + qualified)
                ):
                    continue
                out.append(d)
        return out

    def references_to(self, name: str) -> list[Reference]:
        return [r for g in self.files() for r in g.references if r.name == name]


# ── Diff helpers ──

_HUNK_HEADER_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


def base_source(head: str | None, file: FileDiff) -> str | None:
    """The file before the change, rebuilt by applying the diff backwards to ``head``.

    No extra download, and exact for the diff that is being reviewed — a
    round-two review diffs against the last reviewed commit, not the base
    branch, and so does this. None when the diff does not fit ``head`` (a
    truncated hunk, a head read from a different commit): a wrong "before"
    would invent signature changes.
    """
    if file.change_type == FileChangeType.ADDED:
        return ""
    if file.change_type == FileChangeType.DELETED:
        removed: list[str] = []
        for hunk in file.hunks:
            for line in hunk.content.split("\n"):
                if line.startswith("-") and not line.startswith("---"):
                    removed.append(line[1:])
        return "\n".join(removed) if removed else None
    if head is None:
        return None
    head_lines = head.split("\n")
    out: list[str] = []
    pos = 0  # next unconsumed head line, 0-based
    for hunk in sorted(file.hunks, key=lambda h: h.target_start):
        body: list[str] = []
        segments: list[tuple[int, int, list[str]]] = []
        start, length = hunk.target_start, hunk.target_length
        for line in hunk.content.split("\n"):
            header = _HUNK_HEADER_RE.match(line)
            if header:
                if body:
                    segments.append((start, length, body))
                    body = []
                start = int(header.group(3))
                length = int(header.group(4)) if header.group(4) is not None else 1
                continue
            body.append(line)
        if body:
            segments.append((start, length, body))
        for start, length, lines in segments:
            # A hunk that leaves nothing in the new file names the line *before* it.
            index = start if length == 0 else start - 1
            if index < pos or index > len(head_lines):
                return None
            out.extend(head_lines[pos:index])
            pos = index
            # unidiff ends a hunk's text with a newline, which splits into a final "".
            while lines and lines[-1] == "":
                lines = lines[:-1]
            for line in lines:
                if line.startswith("\\"):
                    continue
                marker, text = (line[:1], line[1:]) if line else (" ", "")
                if marker == "-":
                    out.append(text)
                elif marker in (" ", "+"):
                    if pos >= len(head_lines) or head_lines[pos].rstrip("\r") != text.rstrip("\r"):
                        return None
                    if marker == " ":
                        out.append(text)
                    pos += 1
                else:
                    return None
    out.extend(head_lines[pos:])
    return "\n".join(out)


def added_lines(file: FileDiff) -> set[int]:
    """New-file line numbers the diff adds."""
    lines: set[int] = set()
    for hunk in file.hunks:
        new = hunk.target_start
        for line in hunk.content.split("\n"):
            header = _HUNK_HEADER_RE.match(line)
            if header:
                new = int(header.group(3))
                continue
            if line.startswith("\\") or line.startswith("-"):
                continue
            if line.startswith("+"):
                lines.add(new)
            new += 1
    return lines


# ── Blast radius ──

# Method names too common to look up by name alone: every `.get(` in the
# repository is not a caller of the one `get` that changed.
_COMMON_NAMES = frozenset(
    [
        "get",
        "set",
        "put",
        "add",
        "run",
        "call",
        "apply",
        "update",
        "delete",
        "remove",
        "close",
        "open",
        "read",
        "write",
        "send",
        "emit",
        "init",
        "new",
        "start",
        "stop",
        "next",
        "load",
        "save",
        "parse",
        "format",
        "render",
        "handle",
        "process",
        "execute",
        "validate",
        "build",
        "create",
        "append",
        "extend",
        "items",
        "keys",
        "values",
        "copy",
        "clear",
        "pop",
        "push",
        "join",
        "split",
        "map",
        "filter",
        "reduce",
        "find",
        "match",
        "test",
        "equals",
        "hashCode",
        "toString",
        "compareTo",
        "String",
        "Error",
        "main",
        "setup",
        "teardown",
        "setUp",
        "tearDown",
        "dispatch",
        "__call__",
        "__str__",
        "__repr__",
        "__eq__",
        "__hash__",
        "__enter__",
        "__exit__",
        "__iter__",
        "__len__",
        "__getitem__",
        "__setitem__",
        "__contains__",
    ]
)


@dataclass
class ChangedSymbol:
    """A definition whose signature a diff changes, or which it removes."""

    path: str
    name: str
    qualified_name: str
    kind: str
    change: str  # "signature" or "removed"
    old_signature: str
    new_signature: str = ""
    callers: list[Reference] = field(default_factory=list)
    # Call sites on lines this PR adds: most likely already updated.
    updated_callers: int = 0
    # Call sites found past the listing cap.
    more_callers: int = 0

    @property
    def callee_name(self) -> str:
        """The name its callers call it by: a constructor is called by its class."""
        if self.name in ("__init__", "constructor") and "." in self.qualified_name:
            return self.qualified_name.rsplit(".", 2)[-2]
        return self.name


@dataclass
class BlastRadius:
    symbols: list[ChangedSymbol] = field(default_factory=list)
    files_searched: int = 0
    # False when the search could not cover the repository (no archive,
    # a file or time cap hit): "no callers" then proves nothing.
    complete: bool = True
    backend: str = "regex"


def _normalize_signature(sig: str) -> str:
    # Annotations (`@Override`, `@Deprecated("…")`) do not change how it is called.
    sig = re.sub(r"@[\w.]+(?:\([^)]*\))?", " ", sig)
    sig = _WS_RE.sub(" ", sig).strip()
    sig = re.sub(r",\s*([)\]>])", r"\1", sig)  # a trailing comma is not a change
    sig = re.sub(r"\s*([(),:<>\[\]=])\s*", r"\1", sig)
    return sig


def _comparable(defs: Iterable[Definition]) -> dict[str, Definition]:
    """Definitions by qualified name; an overloaded name is left out (ambiguous)."""
    seen: dict[str, Definition] = {}
    dupes: set[str] = set()
    for d in defs:
        if d.qualified_name in seen:
            dupes.add(d.qualified_name)
        seen[d.qualified_name] = d
    for name in dupes:
        seen.pop(name, None)
    return seen


def _interesting(defn: Definition) -> bool:
    name = defn.name
    if len(name) < 3 and name != "New":
        return False
    if defn.kind == "method" and name in _COMMON_NAMES:
        return False
    return not (name.startswith("test") or name.startswith("Test"))


def changed_symbols(
    files: list[FileDiff],
    heads: dict[str, str | None],
    *,
    use_tree_sitter: bool = True,
    max_symbols: int = 12,
) -> list[ChangedSymbol]:
    """Symbols of ``files`` whose signature changes or which are removed.

    ``heads`` maps a path to its source after the change. Each file's source
    before the change is rebuilt from the diff (:func:`base_source`). CPU-bound.
    """
    found: list[ChangedSymbol] = []
    for file in files:
        if file.is_binary or not file.hunks or file.change_type == FileChangeType.ADDED:
            continue
        old_path = file.old_path or file.path
        if grammar_for_path(file.path) is None and grammar_for_path(old_path) is None:
            continue
        head = heads.get(file.path) if file.change_type != FileChangeType.DELETED else ""
        if head is None:
            continue
        before = base_source(head, file)
        if not before:
            continue
        old_graph = parse_file(old_path, before, use_tree_sitter=use_tree_sitter)
        new_graph = parse_file(file.path, head, use_tree_sitter=use_tree_sitter) if head else None
        if old_graph is None:
            continue
        old_defs = _comparable(d for d in old_graph.definitions if d.kind != "class")
        # Classes count only for removal: their "signature" is a header line.
        old_classes = _comparable(d for d in old_graph.definitions if d.kind == "class")
        new_defs = _comparable(new_graph.definitions if new_graph else [])
        new_names = {d.qualified_name for d in (new_graph.definitions if new_graph else [])}

        for qualified, old in old_defs.items():
            if not _interesting(old):
                continue
            new = new_defs.get(qualified)
            if new is None:
                if qualified in new_names:
                    continue  # now overloaded: not a removal we can reason about
                found.append(
                    ChangedSymbol(
                        path=file.path,
                        name=old.name,
                        qualified_name=qualified,
                        kind=old.kind,
                        change="removed",
                        old_signature=old.signature,
                    )
                )
            elif _normalize_signature(old.signature) != _normalize_signature(new.signature):
                found.append(
                    ChangedSymbol(
                        path=file.path,
                        name=new.name,
                        qualified_name=qualified,
                        kind=new.kind,
                        change="signature",
                        old_signature=old.signature,
                        new_signature=new.signature,
                    )
                )
        for qualified, old in old_classes.items():
            if qualified not in new_names and _interesting(old):
                found.append(
                    ChangedSymbol(
                        path=file.path,
                        name=old.name,
                        qualified_name=qualified,
                        kind="class",
                        change="removed",
                        old_signature=old.signature,
                    )
                )
    # Removals and top-level functions first: they break the most callers.
    found.sort(key=lambda s: (s.change != "removed", s.kind == "method"))
    return found[:max_symbols]


_SKIP_DIRS = ("node_modules/", "vendor/", "dist/", "build/", ".git/", "third_party/")
_LATE_MARKERS = ("test", "spec", "fixture", "mock", "__snapshots__", "example")


def graph_searchable(path: str) -> bool:
    if grammar_for_path(path) is None:
        return False
    return not any(path.startswith(d) or f"/{d}" in path for d in _SKIP_DIRS)


def _search_order(path: str) -> tuple[int, str]:
    lower = path.lower()
    return (1 if any(m in lower for m in _LATE_MARKERS) else 0, path)


def name_pattern(names: Iterable[str]) -> re.Pattern[str] | None:
    """A whole-word regex for any of ``names``; `$` and `_` count as word characters."""
    escaped = sorted({re.escape(n) for n in names if n}, key=len, reverse=True)
    if not escaped:
        return None
    return re.compile(r"(?<![\w$])(?:" + "|".join(escaped) + r")(?![\w$])")


def select_candidates(
    files: dict[str, str],
    rx: re.Pattern[str],
    paths: Iterable[str],
    *,
    max_files: int,
    max_file_bytes: int,
    max_total_bytes: int,
    presorted: bool = False,
) -> tuple[list[tuple[str, str]], bool]:
    """Files among ``paths`` whose text matches ``rx``, within the caps. CPU-bound.

    Source before tests unless ``presorted``. Returns the files and whether a
    cap left a matching file out.
    """
    out: list[tuple[str, str]] = []
    total = 0
    skipped = False
    for path in paths if presorted else sorted(paths, key=_search_order):
        content = files.get(path)
        if not content or rx.search(content) is None:
            continue
        if len(content) > max_file_bytes:
            skipped = True  # generated or minified, most likely; not parsed
            continue
        if len(out) >= max_files or total + len(content) > max_total_bytes:
            return out, True
        out.append((path, content))
        total += len(content)
    return out, skipped


async def repo_snapshot(source_fetcher: Any, max_wait: float) -> Any:
    """The review's in-memory snapshot of the repository, if it has (or soon gets) one."""
    loaded = getattr(source_fetcher, "loaded", None)
    snapshot = loaded() if callable(loaded) else None
    waiter = getattr(source_fetcher, "snapshot", None)
    if snapshot is None and callable(waiter):
        try:
            snapshot = await waiter(max_wait=max_wait)
        except Exception:  # noqa: BLE001 — no archive: per-file reads instead
            snapshot = None
    files = getattr(snapshot, "files", None)
    if isinstance(files, dict) and files:
        return snapshot
    return None


async def build_blast_radius(
    files: list[FileDiff],
    source_fetcher: Any,
    *,
    repo_tree: Iterable[str] | None = None,
    dependents: Callable[[list[str]], Awaitable[list[str]]] | None = None,
    graph: CodeGraph | None = None,
    use_tree_sitter: bool = True,
    max_symbols: int = 12,
    max_callers: int = 8,
    max_files: int = 400,
    max_file_bytes: int = 300_000,
    max_total_bytes: int = 24 * 1024 * 1024,
    fallback_files: int = 40,
    time_budget: float = 20.0,
) -> BlastRadius:
    """Changed symbols of ``files`` and the call sites the diff leaves alone.

    With the review's repository snapshot every source file is a candidate,
    narrowed by a whole-word search for the changed names before anything is
    parsed. Without one, the files that import the changed files (from the
    index, via ``dependents``) and their neighbours are read one by one, up
    to ``fallback_files`` — and the result says it is incomplete.
    Bounded by ``time_budget`` seconds; never raises.
    """
    started = time.monotonic()
    deadline = started + time_budget
    result = BlastRadius(
        backend="tree-sitter" if use_tree_sitter and tree_sitter_available() else "regex"
    )
    try:
        targets = [
            f
            for f in files
            if not f.is_binary
            and f.hunks
            and f.change_type != FileChangeType.ADDED
            and (grammar_for_path(f.path) or grammar_for_path(f.old_path or ""))
        ]
        if not targets or source_fetcher is None:
            return result
        reads = await asyncio.gather(
            *(
                source_fetcher.fetch(f.path)
                if f.change_type != FileChangeType.DELETED
                else asyncio.sleep(0, result="")
                for f in targets
            ),
            return_exceptions=True,
        )
        heads = {
            f.path: (r if isinstance(r, str) else None) for f, r in zip(targets, reads, strict=True)
        }
        symbols = await asyncio.to_thread(
            changed_symbols,
            targets,
            heads,
            use_tree_sitter=use_tree_sitter,
            max_symbols=max_symbols,
        )
        if not symbols:
            return result
        result.symbols = symbols

        names = {s.callee_name for s in symbols}
        rx = name_pattern(names)
        if rx is None:
            return result
        graph = graph or CodeGraph(use_tree_sitter=use_tree_sitter)
        snapshot = await repo_snapshot(
            source_fetcher, max(0.0, min(10.0, deadline - time.monotonic()))
        )
        candidates: list[tuple[str, str]]
        if snapshot is not None:
            pool = [p for p in (repo_tree or snapshot.files.keys()) if graph_searchable(p)]
            candidates, capped = await asyncio.to_thread(
                select_candidates,
                snapshot.files,
                rx,
                pool,
                max_files=max_files,
                max_file_bytes=max_file_bytes,
                max_total_bytes=max_total_bytes,
            )
            result.complete = not capped and not getattr(snapshot, "partial", False)
        else:
            changed_paths = [f.path for f in targets]
            wanted: list[str] = []
            if dependents is not None:
                try:
                    wanted.extend(await dependents(changed_paths))
                except Exception as exc:  # noqa: BLE001
                    logger.debug("Index dependents lookup failed: %s", exc)
            tree = [p for p in (repo_tree or []) if graph_searchable(p)]
            for path in changed_paths:
                folder = path.rsplit("/", 1)[0] if "/" in path else ""
                wanted.extend(
                    p for p in tree if (p.rsplit("/", 1)[0] if "/" in p else "") == folder
                )
            wanted = [p for p in dict.fromkeys(wanted) if graph_searchable(p)][:fallback_files]
            contents = await asyncio.gather(
                *(source_fetcher.fetch(p) for p in wanted), return_exceptions=True
            )
            fetched = {p: c for p, c in zip(wanted, contents, strict=True) if isinstance(c, str)}
            candidates, _ = select_candidates(
                fetched,
                rx,
                wanted,
                max_files=max_files,
                max_file_bytes=max_file_bytes,
                max_total_bytes=max_total_bytes,
            )
            result.complete = False

        parsed, timed_out = await asyncio.to_thread(graph.add_files, candidates, deadline)
        result.files_searched = parsed
        if timed_out:
            result.complete = False

        added = {f.path: added_lines(f) for f in files}
        families = {s.path: _family(s.path) for s in symbols}
        own_spans: dict[tuple[str, str], tuple[int, int]] = {}
        for s in symbols:
            fg = graph.get(s.path)
            for d in fg.definitions if fg else []:
                if d.qualified_name == s.qualified_name:
                    own_spans[(s.path, s.qualified_name)] = (d.start_line, d.end_line)
        for s in symbols:
            family = families.get(s.path)
            span = own_spans.get((s.path, s.qualified_name))
            for ref in graph.references_to(s.callee_name):
                if _family(ref.path) != family:
                    continue
                if ref.path == s.path and span and span[0] <= ref.line <= span[1]:
                    continue  # its own recursion
                if ref.line in added.get(ref.path, set()):
                    s.updated_callers += 1
                    continue
                if len(s.callers) < max_callers:
                    s.callers.append(ref)
                else:
                    s.more_callers += 1
        return result
    except Exception as exc:  # noqa: BLE001 — optional context, never a failed review
        logger.warning("Blast radius lookup failed, continuing without: %s", exc)
        result.complete = False
        return result
    finally:
        logger.debug(
            "Blast radius: %d symbol(s), %d file(s) searched in %.1fs",
            len(result.symbols),
            result.files_searched,
            time.monotonic() - started,
        )


def render_blast_radius(radius: BlastRadius, paths: Iterable[str], char_budget: int) -> str:
    """The prompt block for the changed symbols defined in ``paths``, or ""."""
    wanted = set(paths)
    symbols = [
        s for s in radius.symbols if s.path in wanted and (s.callers or s.change == "removed")
    ]
    if char_budget <= 0 or not symbols:
        return ""
    blocks: list[str] = []
    used = 0
    for s in symbols:
        what = "removed or renamed" if s.change == "removed" else "signature changed"
        lines = [f"#### `{s.qualified_name}` in `{s.path}` — {what}"]
        lines.append(f"- before: `{s.old_signature}`")
        if s.new_signature:
            lines.append(f"- after: `{s.new_signature}`")
        if s.callers:
            note = (
                f" ({s.updated_callers} more on lines this PR changes)" if s.updated_callers else ""
            )
            lines.append(f"- call sites this PR does not change{note}:")
            for ref in s.callers:
                where = f" in `{ref.enclosing}`" if ref.enclosing else ""
                lines.append(f"  - `{ref.path}:{ref.line}`{where}: `{ref.text}`")
            if s.more_callers:
                lines.append(f"  - … and {s.more_callers} more")
        elif s.updated_callers:
            lines.append(
                f"- all {s.updated_callers} call site(s) found are on lines this PR changes"
            )
        else:
            searched = "the whole repository" if radius.complete else "the files searched"
            lines.append(f"- no call sites found in {searched}")
        block = "\n".join(lines) + "\n"
        if used + len(block) > char_budget:
            break
        blocks.append(block)
        used += len(block)
    if not blocks:
        return ""
    scope = (
        f"all {radius.files_searched} files that mention these names"
        if radius.complete
        else f"{radius.files_searched} files (a partial search: an absent caller proves nothing)"
    )
    how = "syntax trees" if radius.backend == "tree-sitter" else "a text search"
    return (
        "\n\n## Callers of changed symbols\n\n"
        "This part changes the signature of, or removes, the definitions below. "
        f"Their call sites elsewhere were found by name with {how} over {scope}, so a "
        "same-named function in another module can appear. Check each caller "
        "against the new signature: one that passes the old arguments, or still "
        "calls a removed symbol, is a bug the diff alone does not show. Comment on "
        "the changed definition in the diff and name the stale caller there.\n\n"
        + "\n".join(blocks)
    )


def match_path(path: str, scope: str | None) -> bool:
    """Whether ``path`` is inside ``scope``: a glob, a directory or a file path."""
    if not scope:
        return True
    scope = scope.strip()
    while scope.startswith(("./", "/")):
        scope = scope[1:] if scope.startswith("/") else scope[2:]
    if any(ch in scope for ch in "*?["):
        return fnmatch.fnmatch(path, scope)
    return path == scope or path.startswith(scope.rstrip("/") + "/")
