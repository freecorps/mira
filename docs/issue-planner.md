# Issue planner

Mira can answer a new issue with an implementation plan: what is being asked,
which files a change would most likely touch, the steps in order, what could
go wrong, and what to test. It is a starting point for whoever picks the issue
up, not a spec.

```yaml
issue_planner:
  enabled: false        # off by default
  auto_on_open: true    # plan when an issue is opened
  labels: []            # only plan opened issues carrying one of these (empty: all)
  ignore_labels: []     # never plan issues carrying one of these
  max_files: 8          # likely files listed, 1-25
```

The section lives in `mira.yaml` or in a repository's own `.mira.yaml`, like
every other setting.

## When a plan is written

| Trigger | Condition |
|---|---|
| An issue is opened | `enabled` and `auto_on_open` are true, the issue is open, it carries one of `labels` (when that list is set) and none of `ignore_labels`, and its author passes `filter.allowed_authors` / `filter.blocked_authors`. Issues opened by bot accounts are skipped. |
| `@mira plan` commented on an issue | `enabled` is true, the issue carries none of `ignore_labels`, and the commenter passes the author filters. The `labels` allowlist, `auto_on_open` and the issue's state do not apply: a person asked. |

`@mira ignore` anywhere in the issue body stops both. A refused `@mira plan`
is answered with the reason (including "the planner is off"), so the command
never disappears silently. `plan this`, `implementation plan` and
`coding plan` work as well as `plan`. Pull requests are not issues here: the
command on a pull request is an ordinary question.

## What the plan says

```markdown
## Implementation plan

**Summary.** The webhook retry loop never backs off …

### Likely files
| # | File | Why |
|---|---|---|
| 1 | `src/app/webhooks/retry.py` | Holds the retry loop. |
| 2 | `src/app/webhooks/backoff.py` *(new)* | The backoff policy. |

### Steps
1. …
### Risks
- …
### Open questions
- …
### Suggested tests
- …
```

Sections with nothing in them are left out.

## Where the files come from

**An indexed repository.** Every indexed file — up to 20,000 — is scored on
how its path and its index summary match the issue's words: the title counts
double, identifiers are split (`RetryPolicy` matches `retry_policy.py`), common
words are ignored, and a path written in the issue (`src/app/auth.py`) ranks
first. The best few are then re-scored on their symbols. The model is shown
the ranked list with summaries and symbol names, and chooses from it.

**A repository that was never indexed.** The same scoring runs over the file
tree from the platform's API, on paths alone. The plan's footer says which
source it used.

**What the model cannot do** is name an existing file that is not in the
repository: such a path is dropped. It may propose a new file (marked
*(new)*), which must be a relative path inside the repository. If none of the
files it chose survive, the ranking's own top files are listed instead.

## One comment, edited in place

The plan starts with a hidden `<!-- mira:issue-plan -->` marker. A re-run —
another `@mira plan` — finds Mira's own comment carrying it and edits it
rather than posting a second. Only comments Mira wrote count, so a human who
quotes the marker never has their comment taken over; if the edit fails, a new
comment is posted.

## Safety

- The issue title and body are written by anyone who can open an issue. They
  reach the model inside untrusted blocks, redacted, and bounded to 300 and
  8,000 characters. Index summaries are quoted the same way.
- The output is a structured object with fields that only describe; nothing in
  it is executed or followed.
- The comment drops the `@` from every handle it would write, so a plan pings
  nobody and can never contain a command to Mira. Mira's own comments never
  trigger it: bot comments are ignored on every platform.
- Every failure is a log line, never a failed webhook. A failed `@mira plan`
  says it could not write a plan.

## Platforms

| Platform | Opened issue | `@mira plan` |
|---|---|---|
| GitHub | `issues` event, action `opened`. The App needs **Issues: read & write** and the **Issues** event subscription. | `issue_comment` on an issue |
| GitLab | `Issue Hook`, action `open` — enable *Issues events* on the webhook | `Note Hook` on an issue — *Comments* events |
| Forgejo | `issues` event, action `opened` | `issue_comment` on an issue |

The plan uses the review model (`llm.review_model`), one structured call per
plan.
