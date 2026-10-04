# Code graph: callers of changed symbols

A diff shows the function whose signature changed. It does not show the eleven
places that call it. Whether those callers were updated is decided by code the
diff leaves out, so a reviewer that only reads the diff cannot tell. Mira parses
the repository into a small graph of definitions and call sites to answer that,
and uses the graph in two places:

- **Callers of changed symbols.** When a pull request changes a function's
  parameters, return type or name, or removes it, each review part that holds
  the change gets the call sites this pull request does not touch, with the
  function each one sits in. The reviewer is told to check every caller against
  the new signature and to report a stale one on the changed definition.
- **Reviewer tools.** With `review.agentic_tools` on, the reviewer can call
  `find_usages(symbol, path?)` and `find_definition(symbol)` itself, and
  `grep_index(query)` when the repository is indexed.

## How it works

Source is parsed with [tree-sitter](https://tree-sitter.github.io/) into
definitions (functions, methods and classes, with qualified names such as
`Store.save` or `Server.Serve`, line spans and signatures) and references (calls
and constructor uses, with the line and the enclosing definition). Python,
JavaScript, TypeScript, TSX, Go, Rust and Java are supported.

For a pull request, Mira:

1. Reads each changed file at the head and rebuilds the file *before* the change
   by applying the diff backwards. That needs no extra download, and it is exact
   for the diff under review: a round-two review diffs against the last reviewed
   commit, and so does the "before". A diff that does not fit the head file is
   skipped rather than guessed at.
2. Compares the definitions on both sides. A signature that differs once
   whitespace, trailing commas and annotations are set aside is a *signature
   change*; a definition that is gone is *removed or renamed*. A body-only edit
   is neither. Methods with very common names (`get`, `run`, `__eq__`, …) are
   skipped, because their callers cannot be told apart by name.
3. Searches for callers. With the review's repository snapshot (see
   `review.repo_snapshot`), every source file is a candidate, narrowed by a
   whole-word search for the changed names before anything is parsed. Without
   one, the files that import the changed files (from the index) and their
   neighbours are read one by one, and the result says the search was partial.
4. Drops calls on lines the pull request adds, which are most likely already
   updated, and calls in a different language family. The rest are listed, up
   to `max_callers` per symbol.

The lookup starts with the review and runs alongside the walkthrough and the
index reads, bounded by `time_budget_seconds`, `max_files`, `max_file_kb` and
`max_total_mb`. A part waits for it only when its prompt has room for the
block, and the block only takes what room is left after the diff and the
changed functions. A lookup that fails or runs out of time costs that part the
block, never the review.

Callers are matched by name, not by type: `obj.save()` counts for every `save`
the pull request changes. The prompt says so, and the reviewer is asked to
confirm a caller before reporting it.

## Reviewer tools

| Tool | What it returns |
| --- | --- |
| `find_usages(symbol, path?)` | Call sites from syntax trees as `path:line in enclosing: line`, source files before tests, then other mentions (imports, a function passed as a value). `path` narrows to a directory, file or glob. |
| `find_definition(symbol)` | Each definition's file, line span, kind, signature and the first 15 lines of its body. `Class.method` or `Receiver.Method` narrows by container. |
| `grep_index(query)` | Indexed files ranked by how many query words their path, summary and symbols match, with the summary's first line and the matching symbols. Offered only when the repository has been indexed. |

The tools only consider paths in the PR head's tree, so a made-up path selects
nothing. Their output counts toward the same per-part output budget as
`read_file` and `grep_repo`, and a repeated call is answered from a cache. The
graph is shared by every part of a review, so a file is parsed once.

Without the repository snapshot, `find_usages` and `find_definition` fall back
to a word-boundary text search and say so.

## Installing tree-sitter

tree-sitter is an optional dependency:

```bash
pip install 'mira-reviewer[graph]'   # or: uv sync --extra graph
```

The `serve` extra and the Docker image include it. The language pack downloads
a grammar the first time it is used; the Docker image fetches the seven
grammars at build time (into `/app/.tree-sitter`, set by
`TREE_SITTER_LANGUAGE_PACK_CACHE_DIR`) so a review never waits for, or depends
on, that download. A grammar that cannot be loaded is remembered for the life
of the process.

Without tree-sitter, or for a grammar that cannot be had, Mira falls back to its
regex symbol extractor and a call-shaped text match. That is less precise — a
call inside a comment or a string counts — and the prompt and tool output say
which one was used. Nothing in the code graph can fail a review.

## Configuration

```yaml
review:
  code_graph:
    enabled: true
    callers_tokens: 2000      # per review part; 0 keeps only the tools
    max_symbols: 12           # changed symbols looked up per pull request
    max_callers: 8            # call sites listed per symbol
    max_files: 400            # files parsed (only those naming a changed symbol)
    max_file_kb: 300          # larger files are skipped, and the search marked partial
    max_total_mb: 24
    time_budget_seconds: 20
```

`review.code_graph: false` turns off both the callers block and the
`find_usages` / `find_definition` tools. `grep_index` follows
`review.agentic_tools` and whether the repository is indexed.
