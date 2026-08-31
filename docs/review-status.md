# Review status and approvals

Two things a pull request can learn from Mira without reading a single comment:
**whether the review has finished**, and **whether Mira would merge it**.

The first is the `mira/review` check on the head commit. The second is a review
event — an APPROVE, or nothing. They are separate signals with separate rules,
and neither of them is the [merge gate](merge-gate.md), which answers the third
and narrowest question: *may Mira put its name on merging this?*

## The check

```
mira/review   ● Reviewing…                    (pending, published at the start)
mira/review   ✓ No findings                   (green, review finished clean)
mira/review   ✓ 2 suggestions                 (green, findings are inline)
mira/review   ✗ 1 blocker, 2 warnings         (red, by default)
mira/review   ● Mira could not finish this review   (neutral, a Mira failure)
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
