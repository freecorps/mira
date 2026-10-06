# Review status and approvals

Two things a pull request can learn from Mira without reading a single comment:
**whether the review has finished**, and **whether Mira would merge it**.

The first is the `mira/review` check on the head commit. The second is a review
event — an APPROVE, or nothing. They are separate signals with separate rules,
and neither of them is the [merge gate](merge-gate.md), which answers the third
and narrowest question: *may Mira put its name on merging this?*

## The check

```
mira/review   ● Queued                        (pending, waiting for a review slot)
mira/review   ● Reviewing…                    (pending, published at the start)
mira/review   ✓ No findings                   (green, review finished clean)
mira/review   ✓ 2 suggestions                 (green, findings are inline)
mira/review   ✗ 1 blocker, 2 warnings         (red, by default)
mira/review   ● Mira could not finish this review   (neutral, a Mira failure)
mira/review   ● Superseded by a newer push    (neutral, an older head nobody needs reviewed)
mira/review   ✓ No findings (carried over)    (the previous verdict, on a rebase that changed nothing)
```

It exists because until the first comment lands, a pull request says nothing
about Mira at all — and "still reviewing" and "not installed here" look
identical from the outside. On a large diff that gap is minutes long.

### Green, red, and the difference that matters

`fail_on` decides what a **finding** does to the colour, and only a finding:

| `fail_on` | Red when |
|---|---|
| `never` | never — the check reports that the review ran, nothing more |
| `blocker` *(default)* | at least one blocker was posted |
| `above_ceiling` | anything above `verdict.approve_max_severity` was posted |

**Mira's own failures are never red.** A timeout, a rate limit, a model outage:
all of them publish `neutral` with a message naming the failure as Mira's. Red
on a pull request is read as a statement about the change, and a check that
goes red when an API is having a bad afternoon is a check people learn to
scroll past — which costs exactly the signal this feature adds.

Two more states are deliberately not green:

- A review where **every file was excluded** (config filters, size limits)
  publishes `neutral` naming the reason. "Nothing was reviewed" and "nothing
  was found" are different answers.
- A review that **only read part of the diff** is still green — the review did
  run — but the summary says so: *"3 of 11 changed files were reviewed"*, with
  the `review-rest` command that covers the remainder.

### The pending status always gets settled

A `pending` check that never resolves is worse than no check at all: it is the
state a required check would block on forever. So the terminal state is
published as soon as the review itself is out — before the pre-merge checks,
the gate and reviewer triage run, since those publish their own contexts and a
crash in one of them is not a review failure. If the review raises, whoever
caught the exception calls `report_review_failure`, and the pending status
becomes the neutral one.

That covers a review that *fails*. It does not cover a process that *dies* —
killed for memory, restarted by a deploy or a watchdog — because then nobody is
left to catch anything. On GitHub that case is covered by the review queue
(below): every review is a row in Mira's database, and the row, not the
process, remembers which check it left queued or in progress. On startup:

- a review that was running is **started again** on the same head, and its
  check — still "Reviewing…" — is taken over by the new run;
- if the pull request **moved on** while Mira was down, the old head's check is
  closed as neutral, *"Review interrupted by a restart; superseded"*, and the
  new head is reviewed;
- a review that has already been interrupted `review.queue.max_attempts` times
  (3) is **not started again** — a review that takes the process down with it
  would otherwise do so on every boot — and its check is closed as neutral,
  *"Mira could not finish this review"*;
- an open pull request whose head carries a `mira/review` check still queued
  or in progress that no row knows about — from before the queue existed, or
  from a lost database — gets a review queued (`review.queue.reconcile_on_boot`).
  The scan covers up to 200 registered repositories and the 50 most recently
  updated open pull requests of each; a check beyond that is settled by the
  next push, `@mira review` or Re-run.

Whatever happens to a request, a check it published is closed by the queue if
the review did not close it itself. A status that could not be published is
retried a few times with a growing delay, and given up on with a warning in the
logs rather than retried for ever.

## The review queue

Each webhook used to start its review on the spot. A restacked chain of
fifteen pull requests, force-pushed in the same second, started fifteen reviews
in one process; the process ran out of memory, the webhooks that arrived while
it restarted were lost, and the checks of the reviews it had been running said
"Reviewing…" for good. Now, on GitHub:

**Durable.** A review request — repository, pull request, head commit, reason —
is written to the application database (SQLite or Postgres, the same one the
dashboard uses) *before* the webhook is answered, and a worker in the same
process runs it from there. If the write itself fails, the webhook falls back
to reviewing directly rather than dropping the event.

**Bounded.** At most `max_concurrent_reviews` reviews run at once (default 2),
and at most `max_concurrent_per_installation` per GitHub installation (0: no
separate limit). The rest wait, and their pull requests show **Queued**
straight away, so "waiting its turn" and "not installed here" still look
different.

**Coalesced.** A new push to a pull request that is still waiting or being
reviewed replaces the older request: a waiting one is dropped, a running one is
cancelled, and the older head's check is closed as neutral, *"Superseded by a
newer push"* — never red, because it says nothing about the change. A
redelivered webhook, or `opened` racing `synchronize`, is the same request and
is not queued twice. During a restack each pull request is reviewed once, at
its newest head.

**Stack-aware.** Requests for one repository that arrive within
`settle_seconds` (5 s) of each other form one batch, and the batch waits until
the burst has gone quiet before any of it starts — but never longer than six
times `settle_seconds` (and at least 30 s) after a request's own arrival.
Within the batch, a pull request whose base branch is another queued request's
head branch goes after it: the chain is reviewed from its base up.

**Rebases that change nothing are not reviewed again.** Every finished review
records its verdict and the *patch id* of the pull request's own diff against
its base — what each file adds and removes, with line numbers, blob ids,
context lines and trailing whitespace left out, much as `git patch-id` does
(indentation is kept: in Python it changes what the code does). When a
`synchronize` arrives and the new head's diff has the same patch id, the push
only moved the base: the previous verdict is republished on the new head with
the note *"Rebased without changes; previous review carried over"*, no model is
called, and the merge gate is re-evaluated for the new head. An explicit
request (`@mira review`, Re-run) is always a real review.
`carry_over_unchanged_rebase: false` turns this off.

**Memory.** A review keeps up to 150 MB of a repository's decoded source in
memory for the agentic tools and the code graph, which is fine for one review
and was most of what five concurrent reviews of one large repository cost.
`snapshot_memory_mb` (320) caps that text across all running reviews together:
a review that finds the budget spent reads files one by one through the API,
as reviews did before snapshots, and a review returns its share when it ends.

```yaml
review:
  queue:
    max_concurrent_reviews: 2
    max_concurrent_per_installation: 0
    settle_seconds: 5
    max_attempts: 3
    carry_over_unchanged_rebase: true
    reconcile_on_boot: true
    snapshot_memory_mb: 320
```

`GET /health/reviews` answers with `reviews_running`, `reviews_queued`,
`max_concurrent_reviews`, `snapshot_bytes_held` and `rss_bytes` — enough for a
liveness probe or a dashboard panel to see a queue backing up before the
process does.

GitLab and Forgejo still start each review from its webhook; the queue covers
GitHub, where the stack feature and the incident that prompted it live.

## Asking for the review again

| How | What runs |
|---|---|
| `@mira review` | The review again; after a first pass, only what changed since Mira's last review. |
| `@mira full review` | The whole pull request from scratch, as a first pass would. |
| **Re-run** on the `mira/review` check (GitHub) | The same as `@mira review`. |

All three go through the queue like a push does, and none of them is carried
over. Re-run arrives as a `check_run` webhook, which the GitHub App already
subscribes to for the merge gate. This is the way to unstick a check that looks wrong; closing and
reopening the pull request is never needed.

### The name is fixed

`mira/review` is a constant, not a setting. Providers filter Mira's own status
contexts out of the CI they read back; a name that could be changed in the
database is a name that exclusion list cannot know, and the loop that follows
is a real one the merge gate hit first: Mira reads its own red status as a
failing build, reports CI as failing, publishes red, and does it again on the
next event.

### Per platform

| Platform | What is published |
|---|---|
| GitHub | A check run, updated in place. Needs `checks:write`; without it the status is refused, logged, and the review is unaffected. |
| Forgejo | A commit status keyed by context. `pending` / `success` / `failure`, and `error` for a review that could not finish — Forgejo has a state for exactly that. |
| GitLab | **Nothing, deliberately.** A GitLab commit status joins the head pipeline: a pending one would hold the merge request on a build Mira never runs, and a green one can satisfy a "pipelines must succeed" rule nobody asked Mira to answer. |

## The approval

```yaml
review:
  verdict:
    mode: "approve"             # off | approve | request_changes
    approve_max_severity: "suggestion"
    approve_min_confidence: 4
    require_all_files_reviewed: true
```

`approve` is the default. `request_changes` is not, and the asymmetry is the
point: an approval **adds** a signal that a human can ignore, dismiss or
override, while a REQUEST_CHANGES **removes** the ability to merge until
somebody dismisses it. One of those is a reasonable thing to inherit from a
default config; the other is a decision a deployment makes on purpose.

Note that on GitHub an APPROVE from Mira counts toward a branch-protection
approval requirement. If your protection rule requires one approval and Mira
can supply it, set `mode: "off"` — or require two.

### Two conditions, asking two different questions

An approval needs **both**:

1. **Nothing above `approve_max_severity`.** Did Mira find a problem?
2. **A merge-readiness confidence of at least `approve_min_confidence`** (1–5,
   default 4). Did Mira understand the change well enough for "found nothing"
   to mean anything?

The second is the walkthrough's own score, after the engine has clamped it
against the findings — so ≥4 also implies no blockers and at most two warnings
whatever the model first thought. A 40-file refactor the model rated 2/5 is not
an approval, however empty the comment list is.

A review with **no** score — walkthrough disabled, or a model that omitted the
field — is judged on severity alone. The floor is evidence Mira uses when it
has it; treating its absence as a failing score would silently stop approvals
on installs that never opted into anything. Set `approve_min_confidence: 0` to
turn the floor off entirely.

### Every other reason to stay quiet

Silence is the default answer to doubt, because silence is recoverable and a
wrong verdict is somebody merging on Mira's word.

- A human requested changes → Mira does not approve over them.
- The pull request is Mira's own → GitHub would refuse it anyway; Mira does not ask.
- Files were skipped for size → `require_all_files_reviewed` blocks the approval.
- The review itself was skipped → nothing to approve on.
- The provider could not report review states → not knowing is a reason to stay quiet, not a reason to proceed.
- `mode: "approve"` and findings exceed the ceiling → **nothing is submitted**. Opting into approvals is not opting into rejections.

### Rejections

`mode: "request_changes"` adds one behaviour: findings above the ceiling get a
REQUEST_CHANGES event. It is never submitted over an existing human review, and
a low confidence score is never a reason to submit one — a number the model
wrote about itself does not get to hold a merge.

## Turning it off

```yaml
review:
  verdict:
    mode: "off"     # no review events at all
  status:
    enabled: false  # no check run either
```

Both are also settable per install from the dashboard's settings panel, which
writes them as global overrides.
