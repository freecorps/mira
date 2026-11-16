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

The thinking stage is the review part; everything around it can run on a
cheaper model. The defaults assume the review model stays where production
has it, **DeepSeek V4.1 Flash Eco**, and the measurements below say what that
costs and what another model buys.

| Setting (dashboard: Settings → Models) | Used for |
|---|---|
| Review model | the part reviews, the whole-PR pass, self-critique |
| Indexing model | indexing, summaries, the dependency pass |
| Security model | the security pass |

**An option, not the default:** `deepseek-v4.1-flash` as the review model
(Eco kept for indexing). It is the same model at about twice Eco's streaming
speed (≈180 vs ≈90 tok/s on OpenDesign), so far fewer parts run out of time
and are read without thinking — in the A/B below it kept today's recall
(18/51 against 11–13/51 on Eco) in 240 s at the median, at about today's plan
credits (Flash is billed 0.5× per token, Eco 0.27×). Nothing in this change
alters stored settings.

On Eco, a larger budget is not the lever: at `time_budget_seconds: 480` /
`call_timeout_seconds: 300` fewer parts degraded (30 of 59 instead of 48) but
recall did not improve (11/51) and the median review took 434 s.

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

Ten merged pull requests from three repositories (`freecorps/mira`,
`freecorps/ember`, `freecorps/mail`; 13–94 files), each replayed at the commit
Mira had reviewed, under the agentic pipeline (main's code, unchanged) and the
staged one, same settings (thinking `high`, `max_tokens` 32,768, ten requests
in flight per process, two reviews at a time). Ground truth: the 51 review
threads opened on that commit — by Mira or Cursor Bugbot — that the author
resolved by changing the code; a finding counts when it lands in the same
file within ten lines.

| | agentic, Eco | **staged, Eco (default)** | staged, Eco, 480 s budget | staged, Flash review |
|---|---|---|---|---|
| Wall time p50 / p90 / max | 533 / 1,270 / 1,271 s | **304 / 325 / 368 s** | 434 / 479 / 501 s | 240 / 304 / 312 s |
| Partial reviews (files unread) | 3 of 10 (12, 14, 27 files) | **0** | 0 | 0 |
| Parts read without thinking | — | 48 of 59 | 30 of 59 | 19 of 59 |
| Model calls (+ cut by a timeout) | 524 (+33) | **207 (+63)** | 227 (+33) | 244 (+14) |
| Prompt / cached / completion tokens | 18.3M / 13.1M / 2.06M | **3.96M / 1.16M / 0.60M** | 4.25M / 3.51M / 1.04M | 4.37M / 0.50M / 1.24M |
| Reasoning tokens | 1.88M | **0.46M** | 0.90M | 1.10M |
| Plan credits (metered) | 1.11 | **0.42** | 0.40 | 1.32 |
| Ground-truth threads found | 18 / 51 | **13 / 51** | 11 / 51 | 18 / 51 |

A call cut by its timeout reports no usage, so every credit figure leaves
those out: 6,995 s of cut calls under the agentic pipeline, 7,126 s under the
staged one on Eco at 300 s, 1,530 s on Flash. With 51 threads, a difference
of two or three is within run-to-run noise; the Eco rows sit below the
agentic pipeline on every run. `mira backtest --config A.yaml --config-b
B.yaml` (with `review.pipeline: agentic` in one) runs the same comparison on
merged diffs; see [backtest](backtest.md).
