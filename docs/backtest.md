# Backtesting reviews against history

**Status:** implemented
**Scope:** replaying the review engine over merged pull requests, scoring it
against what happened afterwards, A/B comparison of two configurations, the
limits on spend, and where the results go.

`mira backtest` answers "how good would Mira have been on *our* pull
requests?" without waiting for new ones. It takes merged pull requests from a
repository's history, reviews each diff again with the current engine — or
two configurations side by side — and scores the findings against evidence
that already exists: what humans said in review, what was later fixed or
reverted, and the feedback recorded on Mira's own comments.

> **A backtest never posts anything.**

That is guaranteed twice. The engine runs with `dry_run=True` through
`review_diff`, which records no review, finding or rule exposure. And the
provider it is handed is a `ReadOnlyProvider`: a wrapper built from an
*allowlist of read methods*. Every other method on the provider interface —
`post_review`, `post_comment`, `submit_verdict`, `publish_*_status`,
`add_label`, `commit_files`, `create_pull_request` and any write added later —
is replaced by a recorder that never reaches the platform. The report counts
blocked attempts per pull request; anything above zero is a bug, and the CLI
exits with status 2 if it ever happens.

---

## Running it

```bash
# 1. Plan: list the PRs, fetch the diffs, estimate the cost. Nothing is reviewed.
mira backtest --repo acme/app --since 2026-07-01 --limit 20

# 2. Run the plan.
mira backtest --repo acme/app --since 2026-07-01 --limit 20 --confirm

# Specific pull requests, JSON report to a file
mira backtest --repo acme/app --prs 812,815,822 --confirm --format json --output bt.json

# A/B: the same PRs under two configurations
mira backtest --repo acme/app --prs 812,815 \
  --config .mira.yaml --config-b experiments/chill.mira.yaml --confirm
```

| Option | Meaning |
|---|---|
| `--repo owner/name` | Repository to replay (GitLab: `group/sub/name`). |
| `--platform` | `github`, `gitlab` or `forgejo`; defaults to `provider.type`. |
| `--prs 1,2,3` | Exactly these merged pull requests. |
| `--since DATE --limit N` | The `N` most recently merged since `DATE`. |
| `--config`, `--config-b` | Configuration A, and optionally B. |
| `--confirm` / `--yes` | Actually run. Without it only the plan is printed. |
| `--max-prs`, `--max-concurrency`, `--max-cost` | Override the limits below for this run. |
| `--format markdown\|json`, `--output FILE` | Report format and destination. |
| `--no-save` | Do not store the run for the dashboard. |

The token comes from `--token`, `MIRA_GIT_TOKEN` or `GITHUB_TOKEN`, as with
`mira review`. Models come from the configuration exactly as a live review
resolves them, including dashboard model settings when the dashboard database
is reachable — so for an A/B test of *models*, set them in the two config
files and make sure no dashboard override shadows them.

## Limits

```yaml
backtest:
  max_prs: 25                  # hard ceiling per run, whatever --limit says
  max_concurrency: 2           # reviews in flight at once
  max_estimated_cost_usd: 5.0  # refuse to start above this; stop starting reviews once spent
  line_tolerance: 3            # lines between a finding and a signal that still "match"
  fix_window_days: 30          # how long after the merge a fix commit counts
  max_files_for_fix_search: 15 # files per PR whose later history is searched
  max_fix_commits_per_file: 10
  store_results: true
```

Spend is bounded three ways. The plan estimates every review before anything
is spent — pessimistically, pricing every token at the review model's rate —
and a plan above `max_estimated_cost_usd` is refused. Nothing runs without
`--confirm`. And during the run, once the *actual* spend (each client's tokens
at its own model's price) reaches the limit, no further review is started;
those pull requests are reported as skipped with "spend limit reached" and the
run's status is `stopped`.

## What counts as ground truth

Each pull request is scored against signals gathered once and shared by both
configurations:

| Signal | Source | Meaning |
|---|---|---|
| `human_comment` | Line comments by people on the pull request (the author's own replies and one-word comments such as "LGTM" are ignored) | Positive: a person found those lines worth a remark. |
| `fix_commit` | Commits after the merge, within `fix_window_days`, touching a file the PR changed, whose message looks like a fix (`fix`, `bug`, `hotfix`, `regression`, …) | Positive: the lines it changed, on the old side, needed fixing. |
| `revert` | The same, for `Revert …` / `This reverts commit …` | Positive. |
| `feedback_positive` | Mira's stored findings on that PR with 👍, an agreeing reply or a resolved thread | Positive. |
| `feedback_negative` | Mira's stored findings with 👎, a disagreeing reply or a dismissal | Negative: a finding there was wrong. |

A finding **matches** a signal when it is in the same file and its lines are
within `line_tolerance` of the signal's. Then:

- a finding on any positive signal is a **true positive**;
- a finding only on negative signals is a **false positive**;
- anything else is **unlabelled** — the evidence says nothing about it.

**Strict precision** is true positives over *all* findings (unlabelled count
against). **Labelled precision** is true positives over true + false
positives (unlabelled are ignored). The truth is somewhere between them, and
the report shows both rather than pick one. **Recall** is the share of positive
signals some finding landed on.

These are honest proxies, not an oracle. Reviewers comment on style; fix
commits sometimes touch neighbouring lines; line numbers drift between the
reviewed head and the fix. Compare configurations on the *same* pull requests
and treat absolute numbers as indicative.

## The report

Markdown (default) or JSON with: a per-configuration summary (findings, TP,
FP, unlabelled, both precisions, recall, tokens, cost, mean latency, errors),
an **A/B comparison** table with B − A deltas when two configurations ran, one
row per pull request and configuration, every finding with its label and the
signal kinds it matched, and notes on anything that could not be read (for
example a provider that cannot search history).

## Where results go

With `store_results` (the default) the run and every per-PR result are stored
in the repository's index store — the per-repo SQLite file, or the shared
Postgres database — in `quality_backtest_runs` and `quality_backtest_results`.
The dashboard's **Review quality** page (admin) lists runs with their
per-configuration summaries and expands into per-PR results. The API is
read-only: `GET /api/quality/backtests` and
`GET /api/quality/backtests/{owner}/{repo}/{run_id}`. Backtests are started
from the CLI only; a button that spends model budget for minutes does not
belong in the dashboard.

## Provider support

The backtest lists merged pull requests with `list_landed_pull_requests` (the
method digests use, which also reports each PR's head branch and base/head
shas), and uses read-only history methods on all three providers:
`get_landed_pull_request`, `list_path_commits`, `get_commit`,
`get_commit_diff` and `get_prs_for_commit`. ("Landed" rather
than "merged": no provider attribute may contain "merge", so that a method
which *performs* one can never look like a read.) GitHub, GitLab and Forgejo
implement all of them.
