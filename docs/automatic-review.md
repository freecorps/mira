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
when it exceeds the soft file-count target. Otherwise its parts are reviewed side
by side, each told which files the rest of the group holds so it can read them
with the repository tools when a contract crosses the split. Every part of every
group is dispatched at once, and `max_concurrent_chunks` bounds how many are with
the model at a time. `sequential_parts: true` restores the older ordering, where
one logical agent processes a group's parts one after another and carries bounded
review notes forward — thorough on paper, and on a large PR most of its wall time.

## What each part reads

The review reads the pull request's head commit from one archive download, shared
by code context, dependency discovery, the repository tools and the manifest scan.
The download starts as soon as the reviewed files are known and overlaps the rest
of the preparation. It is decoded as it arrives and only text files are kept, so
the cap bounds transfer rather than memory (kept text is capped separately, and a
snapshot that hits that cap says so). A read waits for the archive at most 15
seconds, then goes to the API while the download carries on for `grep_repo`. A
repository whose archive passes `repo_snapshot_max_mb`, or a platform without an
archive endpoint, is read one file at a time instead, as before. With the archive
in hand, `grep_repo` searches the whole repository.

Every diff line in the prompt starts with its line number in the file after the
change, so the model files comments against numbers it reads rather than counts.
Each part also receives the post-change source of every function or class its
changed lines sit in (nested functions included, long ones as windows around the
change), within `enclosing_context_tokens`.

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
  sequential_parts: false
  repo_snapshot: true
  repo_snapshot_max_mb: 250
  enclosing_context_tokens: 6000
```

`agent_token_budget` bounds each call's diff allocation (including the chunker's
2,000-token allowance); the engine further reduces it to reserve prompt context,
review notes and the response within `llm.max_context_tokens`. `agent_max_files`
is a soft target. `filter.max_files` can further reduce that per-agent target.
`max_chunks_per_review` controls groups per automatic wave when `sequential_parts`
is on; it does not stop the review after that many groups. `max_concurrent_chunks`
bounds active main review parts, while existing security and other auxiliary
passes have their own limits.

Each review logs one line with its timing — preparation, thread verification,
review, critique and summary — and every LLM call logs its purpose, model and
duration, so a slow review can be read off its trace.

`chunk_retries` applies only to failed parts, after the provider's transport
retries. Successful parts are not repeated. Persistent failures are reported as
incomplete and do not satisfy the all-files-reviewed approval condition. If every
part fails, the review fails. `review-rest` remains available to retry those files.
The review audit records the work plan, retries and failures.

Set `auto_complete: false` to retain legacy selection using `filter.max_files`,
`review.max_diff_size` and `review.max_file_size`. Automatic coverage may use more
total tokens on large PRs because the entire eligible diff is reviewed.
