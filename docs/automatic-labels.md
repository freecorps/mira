# Automatic PR labels

Open **Repositories → a repository → Automatic Labels** as a dashboard admin.
Each repository and hosting platform has an independent workflow. No LLM call
is needed to evaluate it.

## Getting started

1. Click **Add preset** on PR size, PR author, or Documentation. Presets add
   editable nodes to the current graph; they can be combined with custom rules.
2. Select a condition to change its attribute, comparison and value. Select a
   label to change its name, color, description and removal behavior.
3. Use **Simulate a PR** to see additions, removals and matching labels without
   touching a real pull request. Simulation works while the workflow is disabled.
4. Check **Enabled** and **Save rules**. Saved rules run on the next relevant
   PR event. Saving does not backfill every existing PR.

To start from another repository, choose it under **Copy rules from another
repository**, then **Copy into editor**. This replaces the editor's draft with
an independent, disabled copy. Review it, enable it, and save for the destination.
Subsequent edits to either repository do not change the other.

## Size labels

The default preset counts **additions + deletions for the complete PR**, including
new commits. It does not use only the latest push or the AI review's filtered diff.

| Label | Total lines |
| --- | --- |
| `size/XS` | 0–10 |
| `size/S` | 11–100 |
| `size/M` | 101–500 |
| `size/L` | 501–1,000 |
| `size/XL` | 1,001+ |

The No output of each threshold leads to the next threshold, so only one size
label matches. At 500 → 501 lines, Mira removes `size/M` and adds `size/L`.
At 501 → 500 it removes `size/L` and restores `size/M`. Thresholds, names and
colors are editable; for example, rename them to `mediana` and `large`.

## Custom graphs

- The PR event is the graph's entry point. Connect its output to one or more rules.
- Conditions have **Yes** and **No** outputs. Chain conditions to require **AND**;
  connect alternative paths to the same action to express **OR**.
- Actions apply a label. Multiple matched actions can add different labels; a
  shared label is applied once. Actions for the same label must have identical settings.
- Add nodes with the toolbar, connect handles by dragging, and select nodes to
  edit them. Select an edge and press Delete or Backspace to disconnect it.
- Every node must be connected. Cycles, invalid operators, duplicate IDs and
  inconsistent label actions are rejected before saving or simulation.

Conditions support total lines, additions, deletions, changed-file count, author,
title, description, target branch, source branch, changed paths and draft state.
Numbers support `=`, `≠`, `>`, `≥`, `<`, `≤`. Text supports equality, inequality,
contains, starts-with and glob patterns. Draft state supports equality/inequality.

Author logins ignore letter case and a leading `@`; this is the PR's author, not
the user who delivered the push. Other text comparisons are case-sensitive.
Path conditions match **any** changed path; inequality requires all paths to
differ. Globs use Python `fnmatchcase`: `*` spans separators, `?` matches one
character, and brackets match character sets. For example, `docs/**` matches
files beneath `docs/`, and `*.tsx` matches TSX paths at any depth.

## Label ownership and execution

**Remove this label** synchronizes a label with the graph's current result. Use
dedicated labels: this workflow controls configured synchronized labels even if
someone initially adds them manually. Labels outside the workflow are preserved.
**Keep this label** adds it when matched and keeps it if the rule stops matching.
Changing a synchronized action to Keep hands that label back to the user.

The PR's previously managed labels are stored so renaming or deleting a rule can
remove its old label on the next evaluation. Disabling the workflow pauses it and
leaves labels as they are. Re-enabling resumes reconciliation on the next event.
Missing repository labels are created using the configured color and description;
existing label definitions are preserved. Label names cannot contain commas,
control characters or leading/trailing whitespace.

GitHub and Forgejo listen to opens, reopens, pushes, edits and draft-state changes.
GitLab listens to opens, reopens, pushes and relevant metadata updates. Label-only
events do not trigger the workflow, avoiding feedback loops. Review author
filters, paused reviews and `review_on_synchronize` do not disable label workflows;
use the workflow's own Enabled switch to pause them.

Mira reads current PR data rather than trusting an old webhook snapshot. Repeated
deliveries do not reapply unchanged labels. Concurrent deliveries for the same PR
are queued within the server process. A deployment running multiple webhook worker
processes should route a PR's events to the same worker; coordination is not
distributed across processes. If provider statistics are incomplete or a request
fails, the failure is logged under `mira.labels`; the AI review still proceeds.
Successful operations can be partial when a provider rejects a later write, and
the next relevant event reconciles again. There is no periodic retry/backfill job.

GitHub's changed-file API is capped, and GitLab can truncate large diffs; detected
incomplete statistics stop evaluation instead of assigning a misleading size.
Forgejo must expose complete file statistics. Provider tokens need permission to
create repository labels and add/remove PR labels. See the provider references:
[GitHub labels](https://docs.github.com/en/rest/issues/labels),
[GitLab labels](https://docs.gitlab.com/api/labels/),
[GitLab MR updates](https://docs.gitlab.com/api/merge_requests/#update-a-merge-request),
and [Forgejo API schema](https://codeberg.org/swagger.v1.json).

Configuration is stored in the existing SQLite/Postgres settings store, scoped by
platform, owner and repository. Admin changes enter the configuration audit trail.
PR-controlled files cannot change workflows.

## API

All endpoints require an administrator session and use the dashboard's existing
origin protection for mutations.

| Method | Path | Purpose |
| --- | --- | --- |
| GET | `/api/labels/presets` | Editable starter graphs |
| GET / PUT | `/api/labels/workflow?platform=github&owner=acme&repo=app` | Read/save one repository's workflow |
| POST | `/api/labels/preview` | Simulate `{workflow, facts, current_labels}` without writes |
| POST | `/api/labels/copy` | Fetch `{platform, owner, repo}` as a disabled draft |

Graphs contain `version: 1`, `enabled`, `nodes` and `edges`. Node kinds are `start`,
`condition` and `label`; edge branches are `next`, `true` and `false`. The API
accepts at most 100 nodes and 300 edges per repository.
