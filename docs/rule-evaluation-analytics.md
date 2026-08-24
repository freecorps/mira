# Rule evaluation analytics (Phase 3)

Phase 2 let Mira learn rules. Phase 3 answers whether those rules were worth
learning: which reviews each rule reached, what humans did about the findings
it covered, and whether the reviews got better or worse after it was turned on.

The whole design turns on one rule: **an absence of feedback is never converted
into approval.** A merged pull request with untouched threads tells you nothing
about whether Mira was right, and every metric here is shaped so that silence
cannot become a compliment.

## What gets recorded

Rules retrieval selects for a review are recorded after the review has been
posted, in `rule_evaluations`. Each exposure produces:

- one **review-scoped** row per rule — the rule was in front of the model, even
  if it surfaced nothing. This matters: a suppression-style rule doing its job
  looks exactly like a rule that never fired.
- one **finding-scoped** row per finding the rule's scope covers.

Each row carries the rule and its version, the decision it produced
(`instruction`, `suppress`, `boost`), its origin (`manual` or `learned`), the
scope, the repository, the PR and its author, the head SHA, and the finding.

### Attribution is scope-based, and says so

We cannot know which line of the prompt the model actually leaned on. Rather
than guess, an evaluation records only what is verifiable: the rule was present
in this review, and this finding falls inside its declared scope. The drill-down
shows exactly those rows, so nobody has to trust an inference they can't check.

Scope matching for attribution is stricter than for retrieval. Retrieval selects
a language- or symbol-scoped rule when *any* changed file matches; attribution
links only the findings in a file that actually carries that language or symbol.
Path scopes match the finding's path; repo and org scopes cover the review, since
they genuinely do. When the per-file metadata is missing, language and symbol
scopes **fail closed** — losing a link is recoverable, because the review-scoped
exposure still records that the rule ran, whereas a wrong link silently corrupts
the score.

### Idempotency

`evaluation_key` is a SHA-256 of
`(platform, owner, repo, pr_number, head_sha, rule_id, rule_version, decision, finding_id)`
with a uniqueness constraint behind it. A retried review round, a redelivered
webhook, or two workers racing all collapse to one row.

`review_id` is deliberately **excluded** from the key: a retry allocates a fresh
review row but is still the same exposure. Linking an evaluation to a review
only ever fills a blank, so a retry cannot re-point history at a newer review.

## Outcome vocabulary

Every feedback event on a finding is bucketed, and the buckets reduce to one
outcome per finding with precedence `negative > positive > neutral >
unobserved`. One person pushing back outweighs a later thumbs-up from someone
else, because a disputed rule is what most needs surfacing.

| Outcome          | From                                                       |
| ---------------- | ---------------------------------------------------------- |
| `positive`       | `thumbs_up`, `reply_agree`, `fixed`, `resolved`             |
| `negative`       | `thumbs_down`, `reply_disagree`, `dismissed`, `rejected`    |
| `neutral`        | `reply_question`, `reply_other`, `reopened`                 |
| `unobserved`     | `unobserved`, or no event at all, or any unrecognized kind  |
| `not_applicable` | a review-scoped exposure — there is no finding to judge     |

An unrecognized event kind falls to `unobserved`. A signal we cannot interpret
must not be read as approval.

### `addressed` needs evidence

A finding counts as addressed only with concrete resolution evidence: a `fixed`
or `resolved` event, which Mira records only when the platform reports the
thread actually resolved, or a finding state of `fixed`/`resolved`.

The `outdated` state is **deliberately excluded**. It only means the diff moved
past the commented line — which is precisely what a silent merge looks like.
Counting it would smuggle merge-as-acceptance back in through the diff.

### How silence is kept neutral

- `acceptance_rate` = `positive / (positive + negative)`. Neutral and
  unobserved appear on neither side, so an unanswered finding can neither raise
  nor lower it. With no decisive feedback the rate is `null`, and the dashboard
  renders an em dash — never `0%`, which would read as a bad score rather than
  as no evidence.
- `addressed_rate` = `addressed / findings`. Unaddressed findings sit in the
  denominator only, so silence can lower it but never raise it. That direction
  is correct: no response is evidence of *absence* of resolution.
- Regression detection ignores rules with no decisive feedback entirely.

## Every number opens

The aggregate query and the drill-down query are generated from the same
outcome expression in `mira/feedback/evaluation.py`, rendered into SQL by
`mira/feedback/evaluation_sql.py` and shared verbatim by both backends. So
filtering the evaluation list by a bucket returns exactly the count the
aggregate reported — by construction, not by convention. The test suite asserts
this equality over a mixed fixture on both SQLite and Postgres.

Outcome buckets sum to `findings`. Review-scoped rows are reported separately
as `review_exposures`; `exposures` is the total of both.

## Before/after activation

The comparison measures **findings in the rule's scope**, not the rule's own
exposures. Before activation the rule had none, so comparing exposures would
compare a number against zero and prove nothing about review quality.

The window defaults to `learning.evaluation_window_days` (30). The response
reports `comparable: false`, with a reason, when the rule has no activation
timestamp, when the rule row has been deleted, or when the "after" window has
not finished filling — a partial window is never presented as a verdict.

The in-scope set is narrowed by the rule's category, plus its path pattern for
path scopes. Language and symbol scopes fall back to category alone, because
`review_findings` stores neither a language nor a reliable symbol; the applied
scope is returned in the response's `scope` field so the comparison's basis is
never implicit.

## Regression suggestions

A rule is flagged when **all** of the following hold:

- it has at least `learning.min_exposures_for_regression` exposures (default 20);
- the negative share of *decisive* signals is at least
  `learning.regression_negative_rate` (default 0.5);
- it is a learned rule, not a manual one.

Above `learning.regression_disable_rate` (default 0.8) the suggestion escalates
from `downgrade` to `disable`.

**Nothing is ever disabled automatically.** Phase 3 stops at the suggestion.
Accepting, deferring or dismissing one writes an audit entry and changes nothing
else; the rule itself is only ever modified from the Learnings page, so the
trail always shows a human made the call. Manual rules are skipped entirely — a
rule a person wrote deliberately is theirs to keep.

## Storage and cost

Both backends aggregate inside SQL and return one page at a time; the review
history is never materialized in memory. Postgres holds every repository in one
table and answers a filtered page directly. SQLite keeps one file per
repository, so an org-wide question visits each file and merges already-reduced
rows.

`rule_evaluations` is indexed on `(rule_id, created_at)`, `finding_id`,
`(created_at, category)` and `(pr_author, created_at)`. Page sizes are capped
server-side. No Redis, no pgvector, no extra service.

When the repository registry is unreachable, analytics returns **503**
rather than defaulting to a platform guess. Guessing GitHub would query the
unnamespaced owner while a GitLab or Forgejo repo's rows live under
`_{platform}/{owner}`, producing an empty result that looks like an answer —
the same conversion of "we could not look" into "there is nothing" that the
outcome model refuses elsewhere.

On Postgres the store pins analytics reads to its own repository. An empty
owner/repo is the explicit org-wide handle the analytics service opens on
purpose; anything else cannot read another repository's history.

## Migration and rollback

The new tables are created by the existing `CREATE TABLE IF NOT EXISTS` pass on
both backends, so an old database picks them up on first open with its data
intact. Nothing existing is altered or dropped, so the previous release runs
against an upgraded database unchanged — it simply ignores the two new tables.
Rollback is preserved for at least one release.

## Configuration

```yaml
learning:
  evaluation_analytics: true # kill switch for the whole recording path
  min_exposures_for_regression: 20
  regression_negative_rate: 0.5
  regression_disable_rate: 0.8
  evaluation_window_days: 30
```

With `evaluation_analytics: false` no exposures are recorded and the analytics
pages stay empty. Reviews are unaffected either way: recording happens after the
comments have been published and swallows its own errors, so it can neither
change nor break a review.

## API

All routes are admin-only. The evaluation history spans every repository and
carries finding titles, PR authors and reviewer reactions — governance data,
gated like rule approval itself.

| Route                                                        | Purpose                                   |
| ------------------------------------------------------------ | ----------------------------------------- |
| `GET /api/analytics/rules`                                     | Per-rule aggregates, filtered + paginated |
| `GET /api/analytics/rules/{owner}/{repo}/{rule_id}`            | Detail + before/after + any suggestion    |
| `GET /api/analytics/rules/{owner}/{repo}/{rule_id}/evaluations` | The evidence behind the numbers          |
| `GET /api/analytics/summary`                                   | Grouped by category/repo/author/scope     |
| `GET /api/analytics/regressions`                               | Advisory suggestions                      |
| `POST /api/analytics/regressions/{owner}/{repo}/{rule_id}/ack` | Record an admin decision (audit only)     |
| `GET /api/analytics/audit`                                     | The audit trail                           |
| `GET /api/analytics/export`                                    | CSV/JSON of the rule table                |
| `GET /api/analytics/rules/{owner}/{repo}/{rule_id}/export`     | CSV/JSON of one rule's evidence           |

## Dashboard

`/learnings/analytics`, admin only. A rules table with search, origin and sort
filters; a breakdown tab; and a per-rule view with the activation comparison and
the paginated evidence list. The "no response" bucket is always shown next to
agreement and disagreement — hiding it is what makes a rule with three
thumbs-up and ninety silences look loved.
