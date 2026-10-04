# Escaped bugs and real-world recall

**Status:** implemented, off by default
**Scope:** detecting reverts and hotfixes, linking them to the pull request
Mira reviewed, the recall figure that comes out of it, and how missed bugs
reach the learning loop.

Precision is easy to observe — people 👎 a bad finding. Recall is not: nobody
reacts to the comment Mira *didn't* write. Escaped-bug tracking measures it
from the other end. When a change is reverted or hotfixed, the defect it fixes
shipped through some earlier pull request; if Mira reviewed that pull request,
the question is simply whether Mira had said anything on those lines.

> **Real-world recall = escaped incidents Mira had flagged ÷ escaped incidents in pull requests Mira reviewed.**

An *incident* is one fix event against one original pull request; a fix that
touches three files of the same pull request is one incident, caught if Mira
flagged any of them.

---

## Turning it on

```yaml
escaped_bugs:
  enabled: true              # off by default; governs the webhooks
  repos: []                  # owner/repo allowlist; empty = all
  detect_reverts: true
  detect_hotfixes: true
  hotfix_labels: [hotfix, bug, bugfix, regression]
  hotfix_branch_prefixes: [hotfix/, hotfix-, bugfix/]
  bug_issue_labels: [bug, regression, defect]
  window_days: 90            # how far back the original PR may have merged
  line_tolerance: 3          # finding ↔ fixed line distance that still counts
  max_files: 20              # files of one fix that are traced
  max_original_prs: 5
  feed_learning: true        # missed bugs become pending learning candidates
```

To backfill history, or to try it before enabling the webhooks:

```bash
mira escaped-bugs scan --repo acme/app --since 2026-01-01 --limit 200
```

Running the command is the opt-in; it does not require `enabled`. Recording is
idempotent — each escape has a deterministic id from (fix, original PR,
file) — so scanning again, or a redelivered webhook, records nothing twice.

## What is a fix event

A merged pull request is a **revert** when its title starts with `Revert`, its
body says `This reverts commit <sha>`, or its head branch is GitHub's
`revert-<N>-…`. It is a **hotfix** when it carries one of `hotfix_labels`, its
head branch starts with one of `hotfix_branch_prefixes`, or it says
`fixes #N` / `closes #N` / `resolves #N` for an issue labelled with one of
`bug_issue_labels`.

A commit pushed straight to the default branch (no pull request) is checked
too: a revert message, or a message starting with `hotfix`. Merge commits and
commits that belong to a pull request are left to the merge event.

## Linking it to the original pull request

- **Reverts** name their target: the reverted sha is mapped to its pull
  request (`get_prs_for_commit`), or the `revert-<N>-` branch names it. The
  lines are those the revert removed — exactly the lines the original added.
- **Hotfixes** are traced line by line. The lines the fix changed are
  **blamed** at the commit just before it, each blamed commit is mapped to its
  pull request, and the fix's lines are attributed to it. GitHub (GraphQL) and
  GitLab support blame.
- Where the provider **cannot blame** (Forgejo has no blame API), the fallback
  compares diffs: recent pull requests Mira reviewed that touched the same
  file (from the review history) are fetched, and any whose added lines sit
  within `line_tolerance` of the fixed lines is linked.

Only original pull requests that **Mira reviewed** (a recorded review or
finding) and that merged within `window_days` before the fix are recorded. A
defect in code Mira never saw is not an escape *of Mira*.

Each record says how it was linked (`revert_sha`, `revert_branch`, `blame`,
`diff_overlap`), the line range, whether Mira **flagged** those lines (a
finding within `line_tolerance`), and — separately, so it cannot inflate
recall — whether Mira said anything **in the same file**.

Line numbers drift: Mira's findings are numbered at the original pull
request's head, the fix's lines at the commit before the fix. The tolerance
absorbs small drift; heavy churn in between can turn a catch into a miss.

## Where it runs

- **Merge webhooks** — after merge-time learning on GitHub and GitLab, and on
  Forgejo's `pull_request` `closed`+merged event. The configuration is checked
  before any token is minted.
- **Push webhooks** to the default branch, on all three platforms.
- **`mira escaped-bugs scan`** for history.

All of it is read-only on the platform: the detection runs behind the same
`ReadOnlyProvider` the backtest uses, and posts nothing.

## Feeding the learning loop

With `feed_learning` (and `learning.feedback_v2` and
`learning.learning_synthesis` on), each **missed** escape records a
`missed` feedback event (`source_event_id = escaped:<id>`) and a **pending**
learning candidate scoped to the exact file, with the escape as a positive
example: *"Review changes to `billing/totals.py` with extra care for defects:
code merged here in #812 was later hotfixed (#830: Fix totals) …"*. Like every
candidate it changes nothing until an admin approves it in **Learnings**,
where it can be edited first; equivalent candidates merge their evidence. A
caught escape teaches nothing new and creates no candidate.

## Where to see it

The dashboard's **Review quality** page (admin) shows the recall figure in
total and per repository, and the escaped bugs themselves — filterable to
missed or caught — with links to the fix, the original pull request and the
learning candidate. The API:

- `GET /api/quality/summary` — caught, missed, total and recall, per repo and in total
- `GET /api/quality/escaped-bugs?flagged=yes|no` — the records, newest first

Rows live in `quality_escaped_bugs` in each repository's store (SQLite) or the
shared Postgres database.
