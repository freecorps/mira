# The review pipeline: stages, budgets and settings

**Status:** implemented (`review.pipeline: staged`, the default)
**Scope:** how one pull request review is split into stages, how long each may
take, what happens when one runs out of time, what it costs, and the settings
that tune it. The older agentic pipeline is still available
(`review.pipeline: agentic`) and is described in
[multipass review](multipass-review.md).

## Why it changed

Three days of production on DeepSeek V4.1 Flash Eco (thinking `high`, on
OpenDesign Cloud) measured the agentic pipeline at:

| | |
|---|---|
| Review duration | p50 472 s, p90 1,004 s, max 1,078 s (86 reviews) |
| Agentic hop latency | p50 27 s, p90 102 s, p99 250 s (5,290 hops) |
| Reasoning | ~2.9k reasoning tokens per call; 90% of output tokens were reasoning; 60+ calls per large review |
| Reliability | 75 of 86 reviews had a part hit its 480 s deadline; 20 were **partial** (files left unread); 99 passes discarded at the soft deadline after spending their tokens; the whole-PR pass failed 43 of 80 times |
| Voting | 461 findings drafted → 154 kept; in 45 parts the vote dropped everything |

Every agentic hop re-read the whole growing conversation and reasoned over it
again, including the hops that only decided which file to read next. On this
endpoint the reasoning level cannot be lowered per call (every
`reasoning_effort` other than `none` is ignored), so the lookup hops thought
as hard as the one that wrote the findings. Most of what they looked up was
knowable before the model was called.

A part that timed out was also **read again from scratch**, after the others
had finished: the retry loop in the engine fell through to its next attempt
after logging the failure, so a part that ran out its 480 s deadline got a
second 480 s run, and its files were counted unread even when that second
run landed. That alone took one review to 18 minutes.

## The stages

```
prepare ──► parts (+ whole-PR pass, security, dependencies) ──► verify ──► summary
            ◄──────────── 70% of the budget ─────────────►   ◄─ 90% ─►
```

1. **Prepare** — unchanged: the diff is parsed and filtered, the repository
   snapshot starts downloading, index context, file history, review
   instructions, learned rules and the callers of changed signatures (the
   [code graph](code-graph.md)) are read side by side, and the walkthrough
   starts.

2. **Context pack, per part** (`core/staged_review.py:build_context_pack`).
   Before the model is called, each part's prompt gets — in whatever room the
   prompt has left, up to `review.context_pack_tokens`:
   - the changed functions in full (as before, `enclosing_context_tokens`);
   - the callers of changed *signatures* (as before, `code_graph`);
   - **new:** the call sites of every function the part changes (ranked by how
     much of it changed, up to 6 functions × 6 callers), the definitions of
     what the changed lines call (signature and first lines, up to 8), and the
     test files named after the changed files that the PR does not touch.

   It is built from the code graph over the reviewed commit's snapshot, so
   no model call is spent on it, and it is bounded by
   `review.context_pack_seconds`: what is ready by then goes in.

3. **At most two thinking calls per part.** The first call has the part's
   prompt — with everything above — and is *offered* the lookup tools
   (`read_file`, `grep_repo`, `find_usages`, `find_definition`) beside
   `submit_review`. It either submits straight away, or asks, in one turn,
   for every lookup it still needs. Those run **side by side**,
   deterministically, and the second call is offered `submit_review` alone:
   it can only answer, and it carries the first call's reasoning over, so it
   goes on from where the first stopped. No lookup runs on the last turn —
   the old loop's last-turn nudge could be ignored and more lookups run.

   Why offer tools at all, if the context is gathered first? Because on a
   thinking model a tool call is where it stops thinking. Measured on DeepSeek
   V4.1 Flash Eco through OpenDesign (where no reasoning level but `none` is
   honoured): the hops of the old loop reasoned 1.8k tokens at the median
   (p90 9.9k), while one call asked to review a whole module outright spent
   all of a 16k-token budget reasoning and never answered — five times out of
   six, with or without being told to think briefly. The lookups' results also
   go to verification with the part's findings (below).

4. **Never partial, never re-run.** A thinking call that times out
   (`review.call_timeout_seconds`), errors, runs out of output budget, or
   answers with something that does not parse is followed by **one call
   without thinking** over the same prompt (`reasoning_effort: none` on an
   OpenAI-compatible endpoint, thinking `off` elsewhere), given what is left
   of the part's time and at least `review.degraded_call_seconds`. A part
   whose turn comes after the parts' deadline is read that way directly.
   Only when the degraded read fails too are the part's files reported
   unread — and then they are listed as skipped, as before, so the verdict and
   the `mira/review` status never call a partly read review complete. A part
   is never retried from scratch.

5. **Beside the parts** — all bounded by the parts' deadline, and none of
   them can fail the review (a failure or a timeout costs only its findings,
   recorded as `stage_left_out` in the audit):
   - the **whole-PR pass** ([system pass](multipass-review.md#the-system-pass))
     reads the PR map (every changed file, the callers of changed symbols, as
     many hunks as fit in `staged_system_pass_tokens`) with the same two calls
     a part gets, and runs only when the PR is split into two or more parts:
     one part already sees the whole change. If it fails it is dropped, not
     read without thinking;
   - the security pass and the dependency-overlap pass.

6. **Verify** — self-critique grades every candidate against the hunk and
   the code around it, **as soon as its part lands** (beside the parts still
   running), in batches of `review.critique_batch_size` side by side,
   bounded by 90% of the budget; the side passes' findings are graded after
   them. A part's findings are graded with the lookups it made, shown once
   per batch, and a finding filed below the confidence floor after them
   (from `evidence_min_confidence`) goes to the critic instead of being
   filtered out, posted only if graded `proven`. A batch that does not
   finish keeps its drafts, as a failed critique always has. This replaces
   the vote as the precision step: one pass by default (`review.passes: 1`).

7. **Summary**, grouping, the comment cap, verification (`mira.verify`),
   walkthrough, check-run status — unchanged.

### Hedging

A thinking call whose reply has not started streaming after
`review.hedge_after_seconds` was queued at the gateway or stalled. The same
call goes out a second time, the first answer wins and the other is
cancelled. Only on streaming endpoints (`stream: true` in `providers.json`),
where the first event is an early sign of life; elsewhere the first sign is
the whole answer. The audit marks a hedged part with `hedged: true`.

## The budget

`review.time_budget_seconds` (300 by default) runs from the start of the
review. Every stage takes its deadline from what is left of it:

| Stage | Deadline |
|---|---|
| Parts, whole-PR pass, security and dependency passes | 70% of the budget |
| One thinking call | `call_timeout_seconds`, or what is left of the parts' time if less |
| Submitting turn | what is left of the parts' time, if at least 30 s; else the part is read without thinking with the lookups' results |
| Degraded read | what is left, but never less than `degraded_call_seconds` |
| Verification (self-critique batches) | 90% of the budget |

The degraded read's floor is what lets a very large pull request run past
the budget: a part whose turn comes late is still read, not dropped, so the
budget is a target and `degraded_call_seconds` is the most a late part adds.
`time_budget_seconds: 0` turns the budget off; the per-part
(`part_deadline_seconds`) and per-call bounds still apply.

Streamed replies are cut when a call's time is up, which is also what bounds
the tokens a runaway reasoning call can burn: at ~90 tokens/s on Flash Eco,
a 180 s call cannot spend the 32k output tokens that eight production calls
spent on reasoning without an answer.

## Choosing models

The thinking stage is the review part. Everything around it can run on a
cheaper or faster model:

| Setting (dashboard: Settings → Models) | Used for | Recommendation on OpenDesign |
|---|---|---|
| Review model | the part reviews, the whole-PR pass, self-critique | `deepseek-v4.1-flash` — about 2× Eco's streaming speed (≈180 vs ≈90 tok/s), and the review stage is where the time goes |
| Indexing model | indexing, summaries, the dependency pass | `deepseek-v4.1-flash-eco` (0.27× credits) |
| Security model | the security pass | the review model, or Eco to save credits |

`review.critique_model: indexing` moves verification to the indexing model.
Nothing in this change alters stored settings.

## What gets recorded

Ledger stages (`llm_usage.stage`):

| Stage | Calls |
|---|---|
| `pass` | a part's first thinking call |
| `pass_followup` | its submitting turn, after the lookups |
| `pass_degraded` | a part read without thinking |
| `system`, `system_followup` | the whole-PR pass's two calls |
| `security`, `dependency`, `critique`, `summary`, `walkthrough`, `threads` | as before |

Audit entries in the review's audit trail:

- `context_pack` — per part: the functions whose callers were listed, the
  callees shown, the related tests, its size, how long it took and whether it
  ran out of time.
- `staged_part` — per part (and `chunk: system` for the whole-PR pass):
  `thinking_calls` (0–2), `degraded`, the `lookups` run, `hedged`, errors and
  seconds.
- `stage_left_out` — a side pass or a verification batch that failed or ran
  out of time.

## Settings

```yaml
review:
  pipeline: staged              # or `agentic` for the old tool loop
  time_budget_seconds: 300      # 0 = off
  call_timeout_seconds: 180     # one thinking call
  degraded_call_seconds: 60     # always granted to the no-thinking read
  degraded_reasoning_effort: none   # or `inherit`
  max_lookups: 6                # one round per part; 0 = no tools, one call
  evidence_min_confidence: 0.3  # below the floor after lookups: posted only if proven
  staged_part_tokens: 16000     # diff tokens per part (the agentic pipeline: agent_token_budget)
  staged_part_files: 10
  staged_system_pass_tokens: 12000
  context_pack_tokens: 5000
  context_pack_seconds: 20
  hedge_after_seconds: 60       # 0 = no hedging
  critique_batch_size: 10
  passes: 1                     # >1 turns the vote back on (see multipass-review.md)
  max_concurrent_chunks: 8      # the endpoint's own limit still applies
```

`agentic_max_hops`, `lookup_reasoning_effort`, `chunk_retries` and
`system_pass_deadline_seconds` belong to the agentic pipeline; under the
staged one, `part_deadline_seconds` still caps a part, inside the budget.

## Measuring it

The A/B in the pull request that introduced this pipeline replayed ten merged
pull requests from three repositories, each at the commit Mira had reviewed,
under the old pipeline (main's code, unchanged) and the new one, with the same
model and settings, and scored both against the threads opened on that commit
that the author then resolved by changing the code. `mira backtest
--config A.yaml --config-b B.yaml` (with `review.pipeline: agentic` in one)
runs the same comparison on merged diffs; see [backtest](backtest.md).
