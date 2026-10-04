# Delivery analytics: hotspots and DORA

Two dashboard features built from data Mira already stores about the pull
requests it reviews and sees merge:

- **Change-frequency hotspots** — per repository, which files keep changing,
  are big or dense, and keep drawing findings. A treemap heatmap and a ranked
  table on the repository's *Hotspots* tab, and a short note in the review of
  any PR that touches a top hotspot.
- **DORA metrics and cycle time** — deployment frequency, lead time for
  changes, change failure rate and time to restore, plus the PR cycle-time
  breakdown, on the *Reviewers* (review-health) page. Admin-only, like the
  rest of that page.

Everything is a proxy. Mira sees pull requests and their merges, not a deploy
pipeline or an incident tracker; the sections below say exactly what each
number measures so nobody reads more into it than it holds.

## Hotspots

### Score

For each file changed in the window:

| Signal | Definition |
|---|---|
| `changes` | the larger of: distinct default-branch changes recorded for the file (commits from history, merged PRs Mira watched), and distinct PRs Mira reviewed that touched it |
| `lines_changed` | added + deleted lines across those changes (where the platform reports them) |
| `churn` | `changes + lines_changed / 100` |
| `complexity` | `1 + log2(1 + loc / 100 + symbols / 5)` from the code index; `1.0` for an unindexed file |
| `finding_density` | Mira findings on the file in the window (dismissed ones excluded) / `changes` |
| **`score`** | `churn × complexity × (1 + finding_density)` |

Taking the max of the two change counts (rather than the sum) keeps a PR that
was both reviewed and merged from counting twice, while letting either source
alone produce a usable heatmap. Complexity is logarithmic so size breaks ties
without dominating: a 5,000-line file is not fifty times a 100-line one.

Directories aggregate their files' scores (two levels deep) and act as a
filter above the treemap.

### Where the data comes from

1. **Reviews** — every review already records the paths it reviewed
   (`review_events.reviewed_paths`), so the heatmap shows something before any
   merge.
2. **Merges** — when Mira sees a PR merge (GitHub, GitLab, Forgejo), the PR's
   per-file line counts are stored in the index table `file_churn`, keyed by
   `pr:<number>` so a redelivered webhook is a no-op.
3. **History backfill** — the first merge Mira sees for a repository also
   pulls up to `history_max_commits` commits of the default branch from the
   last `window_days`, via the provider's `get_commit_churn`, keyed by
   `commit:<sha>`. Merge commits are skipped (their first-parent diff would
   count the merged work twice). The backfill runs once per repository; if it
   fails it is retried on the next merge.
4. **Findings and index** — `review_findings` per path, and `files.loc` /
   symbol counts from the code index.

### Review note

When `review_note` is on and a PR changes a file that is among the top
`review_top_n` hotspots with at least `review_min_changes` changes in the
window, the shared review context gains:

```
### Change Hotspots
This PR touches files that change often in this repository and have drawn
findings before. Scrutinize these closely for regressions, edge cases and
missing tests:
- `src/billing/invoice.py` (hotspot #2: 14 changes in 90d, 3 prior finding(s))
```

At most five files are listed. It is read from the index store only — no
platform calls — and any failure leaves the note out rather than delaying the
review.

### Provider methods

| Method | GitHub | GitLab | Forgejo |
|---|---|---|---|
| `get_commit_churn(pr_info, since=, ref=, max_commits=)` | list commits, then `GET /commits/{sha}` per commit for file stats | `repository/commits` + `commits/{sha}/diff` (lines counted from the hunks) | `/commits` + `git/commits/{sha}.diff`; falls back to the listed file names with zero line counts |
| `list_deployment_releases(pr_info, since=)` | `/releases` (drafts skipped) | `/releases` | `/releases` (drafts skipped) |
| `get_pr_first_commit_at(pr_info)` | `/pulls/{n}/commits` | `/merge_requests/{iid}/commits` | `/pulls/{n}/commits` |

All three are best-effort: an HTTP error yields an empty result, and per-commit
requests run at most four at a time.

## DORA and cycle time

Computed from `pull_requests` (and `deployments` in releases mode) for the
selected period and the period before it, optionally narrowed to one
repository.

| Metric | Proxy |
|---|---|
| **Deployment frequency** | `merges` (default): PRs merged into the repository's default branch — a merge into another branch does not count; a PR whose base or default branch is unknown counts. `releases`: published releases, recorded from GitHub `release` webhooks and synced from the platform after each merge. |
| **Lead time for changes** | median of first commit on the PR → merge; PR creation stands in when the first commit is unknown |
| **Change failure rate** | failure-fix PRs / merges into the default branch. A failure fix is a merged PR whose title starts with a `failure_title_prefixes` entry (`Revert`, `hotfix`, `rollback`; also `[hotfix]`) or carries a `failure_labels` label |
| **Time to restore (MTTR proxy)** | for a revert whose title names the reverted PR (`Revert "<title>"`, GitHub's format): reverted PR's merge → revert's merge. Otherwise the fix PR's own open → merge. Median |
| **Cycle time** | medians of coding time (first commit → PR opened), time to first review (opened → first human review), time to approval (opened → first approval), time to merge (opened → merge) and total cycle time (first commit, or open, → merge) |

Bands follow the State of DevOps tiers, simplified:

| Band | Deployments | Lead time | Failure rate | Restore |
|---|---|---|---|---|
| elite | ≥ 1 / day | < 1 day | ≤ 5% | < 1 hour |
| high | ≥ 1 / week | < 1 week | ≤ 15% | < 1 day |
| medium | ≥ 1 / month | < 1 month | ≤ 30% | < 1 week |
| low | less | longer | higher | longer |

The chart buckets daily for periods up to 31 days and weekly beyond.

### Stored fields

`pull_requests` gained `base_branch`, `default_branch`, `labels` (comma-joined,
lowercase), `first_commit_at` and `first_approval_at`, added in place on both
SQLite and Postgres. A new `deployments` table holds `(owner, repo, kind, ref,
url, deployed_at)`.

- **GitHub** keeps the row current from `pull_request` / `pull_request_review`
  webhooks (branches, labels, first approval) and the contributor backfill
  (which also reads the first commit of merged PRs). Subscribe the GitHub App
  to **Release** events for releases mode.
- **GitLab and Forgejo** create or update the row when the merge is seen,
  reading the first commit, default branch and labels from the platform. Their
  time-to-first-review and approval columns stay empty, so those cycle-time
  medians cover GitHub PRs only.

## Configuration

```yaml
analytics:
  hotspots:
    enabled: true              # heatmap, merge-time churn recording, review note
    window_days: 90            # default dashboard window and review-note window
    history_max_commits: 200   # one-time backfill on the first merge; 0 = off
    review_note: true          # add "Change Hotspots" to the review context
    review_top_n: 10           # files that count as "top hotspots"
    review_min_changes: 3      # a single edit is not a pattern
  dora:
    enabled: true
    deployment_source: merges  # merges | releases
    failure_title_prefixes: [revert, hotfix, rollback]
    failure_labels: [hotfix, revert, incident, rollback]
```

## API

| Route | Access | Returns |
|---|---|---|
| `GET /api/repos/{owner}/{repo}/hotspots?days=90&limit=100` | signed-in users | `files` (ranked), `directories`, `max_score`, `total_files`, `sources` |
| `GET /api/review-insights/dora?days=30&repo=owner/repo` | admin | `current` / `previous` windows (metrics, bands, `cycle`), `series`, `failures`, `repos` |
