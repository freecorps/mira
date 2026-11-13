# Peer recall and positive learning

**Status:** implemented, on by default (GitHub only; reads, never posts)
**Scope:** measuring Mira's recall against the other review bots on the same
pull requests, and turning what they caught and Mira missed into "look for …"
rules.

[Escaped bugs](escaped-bugs.md) measure recall on defects bad enough to be
reverted or hotfixed — a handful a month. The review threads other bots open
are a much larger labelled set that arrives within a day of a merge. On this
organisation's history, in pull requests Mira also reviewed:

| Peer bot | Threads | Mira had a finding within 15 lines | Missed | Missed, then fixed by the author |
| --- | ---: | ---: | ---: | ---: |
| Cursor Bugbot (`cursor[bot]`) | 490 | 87 | 403 | 297 |
| CodeRabbit (`coderabbitai[bot]`) | 215 | 38 | 177 | 136 |
| CodeAnt (`codeant-ai[bot]`) | 223 | 39 | 184 | 149 |

What Mira misses is mostly lifecycle, state-machine and cross-file bugs, and
until now the learning loop could not help: its only evidence was pushback on
findings Mira *did* write, so every rule it produced was a negation ("do not
report X"). Peer recall gives it the other half.

---

## What is collected

Every run (daily inside `mira serve`, or on demand), for each covered GitHub
repository:

1. The pull requests Mira recorded a review of in the last
   `peer_recall.lookback_days` (30) are listed from the repository's store.
2. Each one that has **closed** (merged or not) and was not read since closing
   is read with one GraphQL query: its state, head and every review thread with
   its replies. Threads whose first comment is by a login in
   `peer_recall.bots` are kept. An open pull request is left for a later run;
   its threads are not settled yet.
3. Per thread, a row is written (idempotently — the id is derived from the
   repository, pull request and thread, and a re-read updates it):
   repository, pull request, bot, path, line, the first line of the comment as
   its title, a coarse category, a 4,000-character excerpt, and two verdicts:

   * **Mira flagged it** — Mira persisted a finding on that pull request in the
     same file within `peer_recall.match_line_tolerance` (15) lines. A thread
     on a whole file matches any finding in the file.
   * **Addressed** — see below.

The pull request is then marked as read, threads or not, so the next day's run
does not ask GitHub about it again. `mira peer-recall scan --rescan` reads
everything in the window again.

### When a thread counts as addressed

These threads become learning evidence, so the rule is deliberately strict: a
thread the author dismissed would teach Mira to repeat another bot's false
positive, while a missed fix only costs one example.

A thread is **addressed** only when all of these hold:

1. The pull request was **merged**. On a pull request closed without merging,
   nothing shipped and nothing was fixed.
2. **No reply in the thread dismisses it**: *won't fix*, *not an issue*,
   *false positive*, *by design*, *intended*, *on purpose*, *out of scope*,
   *separate PR*, *follow-up* (and the Portuguese equivalents). Any match
   vetoes, even next to a fix claim.
3. One of:
   * `reply` — a reply from anyone but Mira itself claims the fix (*fixed*,
     *addressed*, *corrigido*, *in the latest commit*, a commit sha — the same
     pattern the pushback loop uses);
   * `resolved` — the thread is resolved **and** the code within
     `peer_recall.addressed_line_tolerance` (5) lines of it changed between the
     commit the bot reviewed and the merged head;
   * `outdated` — the thread is outdated **and** that same check holds.

Resolution alone is not enough, because resolving is also how people clear bot
noise. Outdatedness alone is not either: it only says a later push moved the
diff, which is what any edit nearby looks like (the rule-evaluation analytics
exclude it for the same reason). The change check is one compare per distinct
commit the threads were opened on, never one per thread; when the compare
cannot be read, only a reply can make the thread addressed.

### Category

A keyword match on the title, then on the start of the body: `security`,
`concurrency`, `lifecycle` (state machines, stale state, cleanup,
invalidation, idempotency), `error-handling`, `null-safety`, `resource`,
`performance`, `api-contract`, `logic`, else `other`. It is for slicing the
report; positive learning asks the model for a category of its own.

## The numbers

> **Recall vs a peer bot = its addressed threads where Mira had a finding nearby ÷ its addressed threads.**

Only addressed threads are in the denominator: those are the ones that pointed
at something real. **Overlap** — flagged ÷ every thread, noise included — is
shown alongside for context.

* **Dashboard** → *Review quality* → *Recall vs peer review bots*: the totals,
  weekly recall per bot (by the week each pull request closed), tables per bot,
  per category and per repository, and the drill-down of every
  missed-and-addressed thread with links to the pull request and the thread
  and what learning did with it. Clicking a bot or a category filters the
  drill-down. *Run now* starts a run in the background (one at a time).
* **CLI:**

  ```bash
  mira peer-recall scan --repo acme/app            # collect + learn (GITHUB_TOKEN)
  mira peer-recall scan --repo acme/app --no-learn # collect only
  mira peer-recall report                          # every repository
  mira peer-recall report --repo acme/app --since 2026-10-01 --bot cursor[bot]
  mira peer-recall report --output json
  ```

* **API** (admin): `GET /api/quality/peer-recall/summary`,
  `GET /api/quality/peer-recall/missed?bot=&category=&limit=&offset=`,
  `POST /api/quality/peer-recall/run` with `{}` or `{"owner": …, "repo": …}`.

## Positive learning

Each missed-and-addressed thread is processed once (its row records the
outcome: `skipped`, `collecting`, `active` or `error`):

1. **The change that addressed it** is read — the compare from the commit the
   bot reviewed to the merged head, cut to that file, at most 6,000 characters.
2. **The review model** (`llm_config_for("review")`) is shown the bot's comment
   and that change, both framed as untrusted data with
   `mira.llm.untrusted.block` (the comment is a third-party service's text and
   can carry instructions), plus up to 30 patterns already learned for the
   repository. It answers with one call to `submit_review_pattern`: `pattern`,
   `not_generalizable` (a typo, a one-off value) or `not_a_defect`; a pattern
   sentence that starts with *Look for*; `same_as`, the id of an existing
   pattern it is an instance of; a finding category; `repo` or `language`
   scope; and its confidence.
3. **Gates.** Anything but `pattern`, a confidence under
   `peer_recall.min_confidence` (0.7), or a sentence worded as a negation
   (*do not*, *never*, *avoid* …) is skipped.
4. **Dedup.** The miss joins the candidate the model named in `same_as`, else
   an equivalent one by the learning loop's semantic dedup
   (`find_equivalent_candidate`), else a new candidate (`status: collecting`,
   `synthesizer_version: peer-recall-v1`). Joining keeps the candidate's wording
   and scope; each miss is evidence for it, not a chance to rewrite it.
5. **Independent support.** Evidence is counted in distinct pull requests: two
   bots flagging one bug on one pull request is one miss. At
   `peer_recall.min_support` (2) pull requests the candidate is accepted
   automatically as `mira-auto` (`learning.auto_accept`; with it off, the
   candidate moves to the pending queue instead). Later misses reinforce the
   rule, which restarts its expiry clock.
6. **Bounds.** Automatic rules expire after `learning.auto_rule_ttl_days` (60)
   without reinforcement and count toward `learning.max_auto_rules_per_repo`
   (30) like the pushback loop's. Positive rules also have their own cap,
   `peer_recall.max_positive_rules_per_repo` (10), so "look for" rules cannot
   crowd out what pushback taught. Each run makes at most
   `peer_recall.max_syntheses_per_run` (20) model calls per repository.

Scope is the repository (`owner/repo`) unless the model says the pattern is an
idiom of the file's language and the language is known, in which case it is
that language within the repository's rules.

### How the rules reach a review

An approved positive rule has `source_signal: positive_pattern`. Retrieval
selects it like any other rule (same scope matching, same TTL), and the review
prompt renders it in its own section after *Team Preferences*:

> **Also Check For (Learned from Missed Issues)** — Other reviewers caught
> defects of these kinds in this repository, the team fixed them, and earlier
> reviews missed them. When the diff touches code where one of these could
> apply, also check for it … report one only when the code in front of you
> actually has the problem, with the same evidence any other finding needs.

They are deliberately **not** passed to self-critique, where team preferences
protect the findings that enforce them: a "look for" rule must make Mira look,
not exempt what it finds from the evidence check.

A candidate from the escaped-bug tracker (`escaped-bug-v1`) is also a positive
rule once an admin approves it — it always read as "flag similar mistakes".

### Analytics

Rule evaluation records positive rules like any other: a review-scoped
exposure per review, plus one per finding in the rule's scope and category.
For a "look for" rule that is the right signal: findings it plausibly produced
that get fixed count for it, ones that get dismissed count against it, and the
existing regression suggestions apply. Automatically accepted rules
(`created_by: mira-auto`) are now scored as `learned`; before this change they
were misread as hand-written and exempt from regression suggestions.

## Configuration

```yaml
peer_recall:
  enabled: true            # the daily scheduler in `mira serve`
  schedule: daily          # or weekly (Mondays)
  hour: 4                  # UTC
  repos: []                # owner/repo allowlist; empty = every registered GitHub repo
  bots:
    - cursor[bot]
    - coderabbitai[bot]
    - codeant-ai[bot]
    - chatgpt-codex-connector[bot]
    - qodo-merge-pro[bot]
    - graphite-app[bot]
  lookback_days: 30
  max_prs_per_run: 200
  match_line_tolerance: 15
  addressed_line_tolerance: 5
  positive_learning: true  # also needs learning.feedback_v2 and learning.learning_synthesis
  min_support: 2
  min_confidence: 0.7
  max_syntheses_per_run: 20
  max_positive_rules_per_repo: 10
```

Add any other bot by its exact GitHub login; `[bot]` and case are ignored when
matching. A login that never comments costs nothing.

## Storage

Two tables in each repository's store, created on open on SQLite and on
Postgres with the same statements as the other quality tables:
`quality_peer_threads` (one row per thread; the assessment columns are
rewritten on a re-read, the learning columns only by learning) and
`quality_peer_scans` (pull requests already read after closing). The last
run's report is kept in the application settings under
`peer_recall:last_run`, and the scheduler's claim under
`peer_recall:last_boundary`.

## Limits

* GitHub only. GitLab and Forgejo report neither resolution by thread nor
  outdatedness in a comparable form; those repositories are skipped.
* Matching is by file and line distance. A Mira finding on the caller of a
  function a peer bot flagged at its definition does not count as caught.
* A thread whose learning call failed is marked `error` and not retried
  automatically.
