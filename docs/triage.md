# Reviewer triage

**Status:** implemented (Phase 7C)
**Scope:** classifying a change, ranking who is closest to it, where the two
signals come from, what is stored about people, and the surfaces that show it.

Triage answers two questions about a pull request and shows its working for
both: *what kind of change is this*, and *who is likely to be the right human
to look at it*. It is the smallest useful version of that on purpose.

> **Mira suggests. A human decides.**

Nothing here assigns a reviewer, requests a review, adds an assignee, applies a
label or mentions anybody. There is no provider method in this codebase that
could, and a test asserts there is none. The output is a comment somebody
reads, and the act of asking a colleague to review your code stays an act a
person performs.

The second rule is the one Phase 6 established, applied to people:

> **"Nobody obvious" and "we could not tell" are different answers.**

A repository with two contributors, one of whom opened the pull request, quite
correctly produces no suggestion. A CODEOWNERS file that could not be read
produces no suggestion either, and saying the same sentence about both would be
a lie in the second case. They are separate states, rendered in different
words, in different sections, and the second one is written in Mira's name.

---

## The vocabulary

A run has exactly one status:

| Status | Meaning | Whose problem |
|---|---|---|
| `ok` | At least one candidate, ranked, each with evidence. | — |
| `no_candidates` | Every signal was read, and nobody qualified. | Nobody's |
| `unavailable` | Something this run depends on could not be read. | Mira's |
| `not_run` | Triage is off here, or the kill switch is on. | — |

The status is *derived*, not stored as an opinion: no candidates **plus** a
signal that could not answer is `unavailable`, never `no_candidates`. Mira does
not get to say "there is nobody obvious" on the strength of a lookup that
failed.

Each signal reports separately, and a run can be `ok` and degraded at once —
two names found, a third source unavailable. The comment says so, and the
dashboard has a filter for exactly that set.

| Signal status | Meaning | Answered? |
|---|---|---|
| `available` | Read, and produced something. | yes |
| `empty` | Read, and produced nobody. | yes |
| `unavailable` | Could not be read. An outage. | **no** |
| `unsupported` | This platform cannot supply it. Permanent. | **no** |
| `disabled` | Configuration turned it off. | yes |

---

## The two signals

### CODEOWNERS — what the repository declares

Parsed with the same strict parser the merge gate uses. A file with a line it
cannot understand is `unavailable`, not "no owners": a half-understood
ownership map must never read as an unowned repository.

**It is read at the pull request's base commit, never at its head.** This is
the security property of the phase and it is not subtle. CODEOWNERS is
repository policy; the pull request is the thing being measured against it. A
branch that could add

```
src/auth/ @an-account-i-control
```

and then be ranked under that line would be nominating its own reviewer, in one
commit. So the ref is the merge target's, the run records which ref was used
(visible on the dashboard as *ownership read at*), and a pull request whose base
commit cannot be determined gets **no** ownership signal rather than a
head-read one. A provider whose `get_codeowners` cannot be pointed at a ref
reports `unsupported` for the same reason.

The merge gate still reads CODEOWNERS at the *head*, and that stays correct:
there, an owner declared on the branch can only ever add a veto, and adding
vetoes to your own change is not an attack.

Owners identified by an email address rather than a login are ranked and shown
with the address masked in the public comment (`d***@acme.example`). The file
is in the repository already, so nothing is hidden from anybody who can read
the code — but a pull-request thread on a public repository is indexed and
mirrored in ways a file in a directory tree is not, and republishing a
colleague's address into one to make a suggestion slightly prettier is not a
trade worth making. The dashboard shows it in full to an admin.

### History — what Mira has observed

Two sources feed one table.

**What Mira saw.** Every pull request Mira reviews and that then merges leaves
a row per changed path for its author and for each person who left a review.
Those identities arrive on a webhook the platform signed, which makes them the
strongest evidence available on any platform — and the only kind available on
GitLab.

**What the platform can attribute.** On GitHub and Forgejo a commit resolves to
the *account* that made it, so a file's history can be fetched directly. That
is what gives a fresh install a useful suggestion before it has watched a single
pull request merge.

The distinction between those two is a security one, not a convenience one. A
git commit's author name and email are written by whoever made the commit and
are verified by nobody; on a repository that takes pull requests from
strangers, a contributor can author their commits as anyone. So only the
platform's *resolved account* is used. A commit GitHub or Forgejo cannot
resolve is counted and dropped — the signal reports "3 commits could not be
attributed to an account and were ignored" — and never turned into a name.

GitLab's commit API returns only the fields inside the commit, so it declares
`can_attribute_commits: false`, the commit half of the signal reports
`unsupported`, and authorship there comes from the merge requests Mira watched
merge.

Everything is bounded: the hottest `history_max_paths` changed files (generated
files excluded — a lockfile's history is a machine's), `history_max_per_path`
commits each, a window in days, and one fetch per path per
`history_refresh_hours`. A marker row records *that* a path was asked about,
separately from what came back, so "asked, and nobody has touched this file"
and "never asked" stay different facts — otherwise every push would re-fetch
everything, forever.

---

## How the ranking works

**A person scores once per changed file they are connected to, weighted by how
that connection was made and how recently.**

That is the whole rule. Owning four of the changed files beats having edited
one of them last week; having edited four beats having edited one. Forty
commits to the same file count **once** — forty commits to one file is one
person who knows one file, and counting them all would rank whoever rebases
most at the top of every list.

```
score = Σ over signals ( weight × Σ over changed files ( recency ) )
        − load_penalty × open reviews waiting on them
```

* **Weights** default to `codeowners: 3`, `reviewed: 1.5`, `authored: 1`.
  Declared ownership beats observed history because the first is the repository
  *stating* who reviews a file and the second is Mira *inferring* it.
* **Recency** is linear across the window and floors at a fifth: a touch from
  today is worth 1.0, one from halfway through the window about 0.5, one at the
  far edge 0.2. Linear rather than exponential because this number is explained
  to people, and "half the window ago, so worth about half" is checkable
  against the dates in the evidence. Ownership has no recency — it is a current
  statement, not a past event.
* **`min_score`** defaults to 0.75, just under the weight of one authorship, so
  one file you changed recently is enough and the same file changed most of a
  window ago is not. (In practice: a lone authored file counts while it is
  younger than about a quarter of the window.)
* **Load** subtracts `load_penalty` per pull request already open and waiting on
  that person, install-wide, excluding this one. A dampener, never a cap: the
  most qualified reviewer is still the most qualified reviewer when they are
  busy — they just stop being the only name on every list. If the review table
  cannot be read, nothing is dampened and the run says so in a note rather than
  quietly ranking without it.

Ties break on the number of distinct signals, then alphabetically, so the same
inputs always produce the same order.

### Who is never suggested

Checked in a fixed order, and every one is **recorded** rather than applied
silently — "Dana owns three of these files but opened this pull request" is a
more useful thing to show than an empty list, and it is the only way to debug a
ranking that surprised you.

| Reason | Meaning |
|---|---|
| `author` | They opened this pull request. |
| `bot` | The identity ends in `[bot]`, or is on the `bots` list. |
| `opted_out` | They are on `triage.exclude`. |
| `no_evidence` | Defensive: a name no signal could justify is dropped, not shown. |
| `below_threshold` | Scored under `min_score`. |
| `not_top_ranked` | Qualified, and finished outside `max_suggestions`. |

Teams (`@org/team`) are ranked and shown but are never excluded for being the
author and carry no review load: Mira does not resolve team membership and will
not guess at it.

There is no `-bot` substring heuristic. `robot-oncall` is a team of people, and
excluding a human for having the wrong name is worse than including one
machine.

---

## What it looks like

On the pull request, as one comment updated in place:

```markdown
### Reviewer suggestions

**s** · 4 file(s), 61 line(s) · code, tests · `src/mira/checks`

Who is closest to these files, and why:

- `dana` — listed in CODEOWNERS (owns 3 of the changed file(s)), has changed
  these files (changed 2 of the changed file(s))
  - `src/mira/checks/runner.py:12` — .github/CODEOWNERS:12 — src/mira/checks/
  - `src/mira/checks/models.py` — commit a1b2c3d4
- `ari` — has reviewed these files (reviewed 2 of the changed file(s))
  - `src/mira/checks/runner.py` — reviewed pull request #241 ([link](…))

_A suggestion, not a review request — Mira does not assign reviewers._
```

Identities are rendered as literal text, never as `@mentions`. That is
deliberate: a suggestion that pings four people has notified three of them
about work they never agreed to, on every push, and a noisy suggestion is a
suggestion that gets switched off. The reader picks a name and requests the
review themselves — which is also the moment a human makes the decision.

A comment is **created** only for a run that has something to suggest. Other
statuses update a comment that already exists and create none: a repository
where triage can never find anybody should not collect a comment on every pull
request saying so, and a suggestion that went stale after a force-push should
not be left standing. Drafts are recorded and not announced; marking one ready
for review re-runs triage and the comment appears then.

When something could not be read, the comment says so in Mira's name:

```markdown
**Mira could not work out who to suggest.** This is a problem with Mira, not
with this pull request, and it is not a statement that nobody is available:

- **CODEOWNERS**: CODEOWNERS could not be read at 4f2a9c11: 502 from the API
```

**No status is published.** A suggestion is not a check, cannot appear in a
branch protection rule, and must never be something a merge waits on. The merge
gate does not read triage at all. A ranking that could hold up a merge would
have to be *right*; a ranking that can only be read has to be *useful*, which
is a much better contract for something built on inference.

---

## Configuration

Off by default, in every layer.

```yaml
triage:
  enabled: true
  kill_switch: false          # stops everything, everywhere, in one edit
  comment: true               # post the suggestion; there is no status option

  max_suggestions: 3
  min_score: 0.75

  codeowners: true            # the declared signal
  history: true               # the observed signals

  history_days: 180
  history_max_paths: 12       # files asked about per run
  history_max_per_path: 20    # commits fetched per file
  history_refresh_hours: 168  # how long a fetched history stays usable

  exclude: [dana]             # never suggested. The answer to "please stop"
  exclude_bots: true
  bots: [release-train]       # machines whose names do not say so

  load_penalty: 0.25
  budget_seconds: 30

  weights:
    codeowners: 3.0
    reviewed: 1.5
    authored: 1.0

  organizations:
    acme:
      max_suggestions: 2
  repositories:
    acme/infra:
      history: false          # CODEOWNERS only here
```

Three layers resolve in order — global, `organizations[<owner>]`,
`repositories[<owner>/<repo>]` — with the sentinel every policy in this
codebase uses: `null` inherits, anything else overrides, and an explicit `[]`
means "empty at this scope" rather than "inherit". Weights are all-or-nothing:
a scope that sets them sets all three, because a half-overridden weighting is a
ranking nobody can predict by reading the configuration.

`mira triage policy --repo acme/infra` prints the resolved answer, which is not
something to work out by reading YAML.

Every value here comes from deployment configuration — the `mira.yaml` an
operator wrote and the admin-editable overrides in the dashboard. **Nothing in
a pull request reaches any of it.** In particular, a `cc @somebody` in a
description is not a signal: the pull request supplies the files it changed, and
nothing else.

---

## What is stored about people

Turning triage on starts two kinds of record.

`triage_runs` and `triage_candidates`
: One row per triage of one pull request, and one per suggested identity —
  what was suggested, at what score, on what evidence, and who was passed over
  and why.

`path_contributions`
: One row per (path, identity, role) — who authored and who reviewed which
  files, from merges Mira watched and from commit history it fetched.

**Nothing is recorded for a repository where triage is off.** Who works on
which file is data about people, and collecting it "in case it is useful later"
is exactly the habit that makes an install untrustworthy. Turning triage on
starts the collection; a repository that never turns it on never has the rows.

History is recorded at *merge*, not at review: an abandoned pull request is not
evidence that anybody knows the file.

Every read of this data is admin-only, including the summary of who gets
suggested most — that is the endpoint it would be most tempting to open up, and
the one that should not be.

---

## Compatibility

| | GitHub | GitLab | Forgejo |
|---|---|---|---|
| CODEOWNERS at the base commit | ✅ | ✅ | ✅ |
| Commit history attributed to an account | ✅ | ❌ `unsupported` | ✅ |
| History from merges Mira watched | ✅ | ✅ | ✅ |
| Suggestion comment | ✅ | ✅ | ✅ |
| Assigns anybody | ❌ | ❌ | ❌ |

Both stores carry the same three tables with the same statements; SQLite keeps
a file per repository and Postgres one table for the install, scoped by owner
and repo on every read. Nothing new is required on ARM64 — there is no extra
service, no model call and no subprocess anywhere in this phase.

---

## Rolling it out

1. **Turn it on for one repository**, with `comment: false`. Runs are recorded
   and nothing is posted.
2. **Read the runs.** The dashboard's *Runs* tab shows what would have been
   said; the *Who gets suggested* tab answers the question that actually
   matters — is this naming the same two people over and over, or spreading
   across the team? A concentrated list usually means the load penalty is too
   low, or that CODEOWNERS says the same thing about every file.
3. **Watch the degraded filter.** On a fresh install the history signal is
   thin and honest about it. It improves on its own as pull requests merge.
4. **Turn on `comment`** when the ranking looks like something you would say
   out loud.
5. **`kill_switch: true`** stops all of it in one edit if it ever becomes
   noise.

Somebody who asks not to be suggested goes on `triage.exclude` and is never
named again, whatever the signals say. That is the whole mechanism, and it is
matched case-insensitively with or without the `@`, because an opt-out that
silently fails to match is worse than none.

---

## From the command line

```bash
mira triage suggest --pr https://github.com/acme/app/pull/7
mira triage suggest --pr … --verbose      # the arithmetic and everyone dropped
mira triage policy --repo acme/app        # what actually applies here
```

`suggest` reads the pull request and records the run exactly as the server
would, and **does not comment** — it is the command to run while deciding
whether to turn the feature on, and a trial run that announced itself on
somebody's pull request would be a poor way to start. It exits non-zero only
when the status is `unavailable`, which is Mira's failure; `no_candidates` is
an answer and exits zero.

---

## Related

* [Pre-merge checks](pre-merge-checks.md) — where the "a failure of ours is
  never a finding against you" vocabulary comes from.
* [The merge gate](merge-gate.md) — which reads CODEOWNERS at the head, for the
  opposite and equally deliberate reason.
