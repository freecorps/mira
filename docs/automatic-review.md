# Automatic, dependency-aware review

Mira reviews all eligible changed files by default. File-count and total-diff
limits no longer leave the rest of a large PR waiting for `review-rest`.
Configured exclusions (including binary, generated and deleted-file rules) still
apply. Coverage refers to the eligible diff received from the hosting provider.

## Work allocation

The planner combines imports from both sides of the diff, full changed-file
source at the reviewed commit, and stored import relationships. Reading full
source catches imports that did not change and therefore are absent from the diff.
Connected components become review ownership groups, including transitive
dependencies and cycles. Small independent groups can share a call.

The resolver handles relative JS/TS imports, re-exports, CommonJS, dynamic imports,
index files, emitted `.js` imports referring to TypeScript, common `@/` and `~/`
aliases, Python imports, and Mira's existing Ruby/Java/Go import candidates.
Common aliases prefer the nearest ancestor and its `src` directory; ambiguous
matches are not guessed. When the repository tree is available, unchanged JS/TS
barrel exports are followed for up to three levels and 64 bridge files.
Custom build-time aliases and runtime dependency injection may require indexed
relationships or the reviewer's repository tools; this is static dependency
discovery, not a complete compiler/build-system graph.

If a group fits the token budget, its changed files go into one review call even
when it exceeds the soft file-count target. Otherwise, one logical agent processes
its parts sequentially, carrying bounded review notes forward. Other groups run
concurrently. This continuation uses summaries of earlier parts rather than an
unlimited conversation. Repository tools remain available when configured.

Oversized files are split by hunks and lines, preserving original coordinates.
An individual line larger than the window is labelled and split into fragments.
Every part must succeed before a file counts as reviewed. Findings are combined
through Mira's existing noise filtering, deduplication and critique pipeline.

## Configuration

```yaml
review:
  auto_complete: true
  agent_token_budget: 24000
  agent_max_files: 12
  max_concurrent_chunks: 5
  max_chunks_per_review: 5
  chunk_retries: 1
```

`agent_token_budget` bounds each call's diff allocation (including the chunker's
2,000-token allowance); the engine further reduces it to reserve prompt context,
review notes and the response within `llm.max_context_tokens`. `agent_max_files`
is a soft target. `filter.max_files` can further reduce that per-agent target.
`max_chunks_per_review` controls groups per automatic wave; it does not stop the
review after that many groups. `max_concurrent_chunks` bounds active main review
parts, while existing security and other auxiliary passes have their own limits.

`chunk_retries` applies only to failed parts, after the provider's transport
retries. Successful parts are not repeated. Persistent failures are reported as
incomplete and do not satisfy the all-files-reviewed approval condition. If every
part fails, the review fails. `review-rest` remains available to retry those files.
The review audit records the work plan, retries and failures.

Set `auto_complete: false` to retain legacy selection using `filter.max_files`,
`review.max_diff_size` and `review.max_file_size`. Automatic coverage may use more
total tokens on large PRs because the entire eligible diff is reviewed.
