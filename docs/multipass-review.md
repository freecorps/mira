# Shuffled voting passes and the system pass

**Status:** implemented
**Scope:** how Mira reads each review part several times and votes on what it
found, the extra whole-PR pass that asks what the change breaks elsewhere,
what they cost, and how to measure them against the other review bots.

## Why

In pull requests Mira also reviewed, Cursor Bugbot found 403 issues Mira did
not (the author fixed 297 of them); CodeRabbit 177 (136 fixed) and CodeAnt
184 (149 fixed) — see [peer recall](peer-recall.md). Most were lifecycle,
state-machine and cross-file bugs: presence not cleared on `pagehide`, state
left behind after sign-out, a background task ended twice, an in-flight copy
racing a later write.

Two things in how Mira reviewed made those easy to miss:

- **One reading per part.** A part is read once, in one order, and what the
  model notices depends on what it read first. Bugbot runs several passes with
  the diff order randomised and keeps the findings that recur.
- **No pass looks at the whole change.** Mira reviews in parts (see
  [code graph](code-graph.md) for the callers each part is shown). Each part
  sees its own hunks; a setup in one part and its missing teardown in another
  are in no part's view.

## Shuffled voting passes

Each review part is read by `review.passes` independent agentic passes (3 by
default), side by side:

- **Pass 0** reads the part exactly as before — original file order, the
  configured `llm.temperature`. With `passes: 1` it is the only pass, and the
  review is the single-pass review it always was.
- **Passes 1…n−1** read the part's files in another order, and each file's
  hunks in another order too when its hunks do not touch (hunks that abut
  read as one block and keep their order). The order comes from a seed derived
  from the part's paths and the pass number, so replaying a review reads the
  same orders. Their temperature steps up evenly from `llm.temperature` to
  `llm.temperature + review.pass_temperature_spread` (0.3, capped at 1.0).

Every pass is a full agentic pass: its own tool executor (`read_file`,
`grep_repo`, `find_usages`, …), the same prompt, the same context, the same
`submit_by` nudge.

### The vote

The passes' findings are clustered by location: same file, lines within
three of each other, and the same category or a similar title. A pass counts
once per cluster however often it repeats itself. Then:

| Reported by | Kept? |
|---|---|
| ≥ `review.pass_vote_threshold` passes (2) | Yes. The finding carries `votes`/`vote_passes`, and its confidence becomes the mean of the passes that reported it. |
| Fewer, and one of them is a **blocker** | Kept for self-critique, marked: the critic must grade it `proven`, or it is dropped. With self-critique off, or failing, it is dropped. |
| Fewer, otherwise | Dropped (recorded in the audit with its vote count). |

The threshold is clamped to the passes that actually landed: if one of three
failed, the two left must agree; if only one landed, everything it found is
kept, as a single-pass review would.

A blocker is the costliest miss, which is why one pass is allowed to raise
it — but only proof keeps it, because a single high-temperature pass is also
the likeliest to invent one. When such a blocker carries a group of the same
problem elsewhere (`review.group_findings`), the next member
of the group the vote did carry is promoted instead of losing the group.

`review.ensemble_runs` (the older plain-review ensemble) only applies when
`passes` is 1.

### Degrading instead of failing

The passes share their part's `review.part_deadline_seconds`. A pass that
fails is left out of the vote. Passes still running when 90% of the part's
budget is gone are cancelled and left out, as long as one has landed; with
none landed yet, the first to land is kept. Only when every pass fails does
the part fail, with the first pass's error, under the usual retry rules.

## The system pass

One more agentic pass reads the whole pull request at once and asks one
question: **what does this change break elsewhere?** Its prompt
(`src/mira/llm/prompts/templates/system_review.jinja2`) lists exactly the
kinds of bugs above:

- **lifecycle and cleanup** — something started, subscribed, scheduled or
  marked present that is not undone on every way out (unmount, `pagehide`,
  sign-out, error, cancellation);
- **state machines and transitions** — a new state some transition or reader
  does not handle; a terminal step that can run twice;
- **concurrency and ordering** — an in-flight request racing a later one,
  check-then-act across an `await`, work never awaited or cancelled;
- **callers and contracts** — a changed signature, payload, default or
  invariant whose callers still assume the old one;
- **cross-file invariants** — two places that must change together where only
  one did.

It is told to leave line-level review to the parts and to anchor every
finding on the changed line that introduces the problem, naming the other
side of the interaction in the body.

Its input is budgeted to `review.system_pass_tokens` (40,000, and always
inside the context window): a line for every changed file (change type,
`+/-` counts, the functions its hunks are in), the callers of the changed
symbols from the code graph, then the hunks of as many files as fit, highest
priority first. Files whose hunks did not fit are marked so, and the pass
reads them with the repository tools.

Its findings carry `source_pass: system` (the finding row's `detector`) and
go through the same noise filter, grouping, self-critique and comment cap as
every other finding. It has its own deadline,
`review.system_pass_deadline_seconds` (480), and anything that goes wrong —
a timeout, a provider error, an unreadable answer — costs only its findings.

## When they run

Both are skipped on small changes:

- below `review.multipass_min_changed_lines` (30) changed lines — added plus
  removed, over the reviewed files — on a first review;
- below `review.multipass_min_changed_lines_incremental` (120) on a follow-up
  round, which reviews only the new commits' diff.

Then every part gets one pass and there is no system pass: the old review,
at the old price. The `multipass_plan` audit entry records the decision.

## Cost and latency

Wall clock stays roughly flat: a part's passes run concurrently inside the
part's slot of `review.max_concurrent_chunks`, and the system pass runs
alongside the parts. Expect up to `max_concurrent_chunks × passes + 1`
agentic loops in flight (16 with the defaults).

Tokens are not flat. Per review, above the thresholds:

| Stage | Before | Now |
|---|---|---|
| Part reviews | 1 agentic loop per part | `passes` loops per part (3×) |
| System pass | — | 1 agentic loop over ≤ 40k tokens of the change |
| Walkthrough, self-critique, summary, security pass | unchanged | unchanged |

The part reviews are most of a review's tokens, so a default review above the
thresholds costs roughly **2.5–3.5× the single-pass review** in tokens, and
a review below them costs the same as before. On the review model this fork
runs (DeepSeek V4.1 Flash / muse-spark through OpenCode Go, cheap per call),
that is the intended trade.

Model calls are bounded. Each agentic loop makes at most 6 calls, and
`review.max_pass_runs` (30) bounds part × pass loops per review: a review
with more parts than `max_pass_runs / passes` gets fewer passes per part (at
least one). So a review's part and system loops make at most
`6 × (min(parts × passes, max(parts, max_pass_runs)) + 1)` calls — 186 at
the defaults on a pull request of up to 30 parts — on top of the fixed walkthrough,
critique and summary calls.

`mira backtest`'s cost estimate counts the extra passes and the system pass
(see below), so a plan shows the multiplier before anything is spent.

## What gets recorded

- **Audit** (the review's `audit` list, on the review event):
  `multipass_plan` (passes per part, changed lines, whether the system pass
  ran); per part, `pass_vote` with every pass's time and status, the
  threshold, the kept findings with their vote counts, and the dropped ones
  with theirs; `agentic` entries carry their `pass`; `system_pass` with its
  status, time, files shown and omitted; `drafted` for chunk `system`; and a
  `pass_vote` drop for a single-vote blocker the critic did not prove.
- **Findings**: `review_findings.votes` and `vote_passes` (0/0 for a single
  pass, the security pass or the system pass), migrated in place on SQLite
  and Postgres.
- **Comments**: a hidden `<!-- mira:votes:2/3 -->` marker on a voted
  finding's comment, and `source_pass`, `votes` and `vote_passes` per comment
  in `mira review --output json`.
- **Logs**: one line per pass (`Review part 2 pass 3/3: 4 finding(s) in
  61.2s`), one per part's vote and one for the system pass.

To tune the threshold, compare how findings fare by vote count:

```sql
SELECT votes, vote_passes, state, COUNT(*)
FROM review_findings
WHERE vote_passes > 1
GROUP BY votes, vote_passes, state
ORDER BY votes, state;
```

If findings at `votes = 2` are resolved about as often as those at `3`, the
threshold is right; if they are mostly dismissed, raise it (or drop the
temperature spread).

## Measuring it

Use the [backtest](backtest.md) harness — it replays reviews without posting
anything — on the pull requests where the other bots caught what Mira missed:

```bash
# Record the other bots' threads first (or let `mira serve` do it daily).
mira peer-recall scan --repo acme/app

# Plan: those PRs, single pass (A) vs voting passes + system pass (B).
mira backtest --repo acme/app --peer-misses --compare-passes --limit 15

# Run it.
mira backtest --repo acme/app --peer-misses --compare-passes --limit 15 --confirm \
  --format json --output passes.json
```

- `--compare-passes` builds both configurations from `--config`: A with
  `passes: 1` and no system pass, B with `review.passes` (3 when the config
  says 1) and the system pass. Nothing else differs.
- `--peer-misses` replays the pull requests the peer-recall scan recorded a
  missed-and-addressed thread on (`--prs` overrides the selection).
- Those threads are scored as `peer_addressed` ground truth, next to human
  comments, fix commits and feedback, and the report's **Recall by signal
  kind** table shows each kind per configuration — the `peer_addressed` row
  is the number this feature is meant to move.

Treat the result as indicative: the backtest replays the current engine on
old diffs, and line tolerance (`backtest.line_tolerance`) decides what
"landed on" means.

The offline counterpart lives in `tests/test_multipass_eval.py`: a recorded
case — a pull request, its diff and a peer thread Mira missed — replayed
through the real engine with a scripted model, single-pass against voting
passes. It runs in the test suite and never calls a model.

## Configuration

```yaml
review:
  passes: 3                       # 1 = single pass (the old review)
  pass_vote_threshold: 2          # passes that must agree; clamped to those that landed
  pass_temperature_spread: 0.3    # pass temperatures step up to llm.temperature + this
  max_pass_runs: 30               # part × pass loops per review, at most
  multipass_min_changed_lines: 30
  multipass_min_changed_lines_incremental: 120
  system_pass: true
  system_pass_deadline_seconds: 480
  system_pass_tokens: 40000
```
