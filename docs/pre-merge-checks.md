# Pre-merge checks

**Status:** implemented (Phase 6)
**Scope:** the check framework, the native checks, ticket and CI context,
deterministic analysers, deduplication, the dashboard surface, and how all of
it reaches the merge gate.

A pre-merge check answers one question about a pull request and says how it
knows. This document is about the one distinction the whole phase is built
around, because everything else follows from it:

> **A violation is a statement about your pull request. Everything else is a
> statement about Mira.**

A linter that is not installed, a model that timed out, a tracker that could
not be reached and a check that does not apply to this diff are four different
things, and none of them is "your change has a problem". Mira reports them as
four different things, in different words, in a different section of the
comment, under different states in the database — and a merge gate still
refuses to approve on any of them, because not knowing is not permission.

---

## The vocabulary

Every check produces exactly one of five states.

| State | Meaning | Whose problem |
|---|---|---|
| `pass` | The check ran, looked, and found nothing. | — |
| `violation` | The check ran, looked, and found something wrong. | Yours |
| `infrastructure_error` | The check could not run, or ran and could not conclude. | Mira's |
| `skipped` | The check had no business running here. | Mira's, or nobody's |
| `timeout` | The check was still running when its budget ran out. | Mira's |

`skipped` carries a reason, and the reason decides whether the question was
*answered*:

* **Answered** — `not_applicable`, `out_of_scope`, `disabled`, `kill_switch`.
  The check correctly decided it had no opinion.
* **Unanswered** — `tool_missing`, `pending`, `unsupported`,
  `budget_exhausted`, `no_evidence`, `ambiguous`. Something was missing, so the
  question stands open.

That second list is what makes `skipped` safe. A check whose linter is not
installed shows as a skip — a reader is not told their code is broken — and
still counts as unanswered, so a *blocking* check cannot be satisfied by
failing to run.

Each check also has a **mode**, from configuration and never from a check:

| Mode | Runs | Reports | Blocks a merge |
|---|---|---|---|
| `off` | no | recorded as `skipped: disabled` | no |
| `warning` | yes | yes | no |
| `error` | yes | yes | on a violation **or** on an unanswered result |

And a whole run has a **verdict** the merge gate reads: `pass`, `violation`,
`incomplete` or `not_run`. A run in which every check was switched off reports
`not_run` rather than `pass` — nothing looked at anything, and a row reading
"Passed — 0 passed" is the sort of small lie this phase exists to remove. A
check that ran and correctly decided it had no opinion is a different thing and
does count towards a pass.

---

## What a result carries

Every result is persisted with the things that make it reproducible and
auditable:

* `check_id` and `check_version` — which check, and which version of its logic.
* `config_digest` — a content hash of the configuration *this check* ran under,
  so two results are comparable exactly when the rules behind them were the
  same. Editing one natural-language rule changes that rule's digest and leaves
  `native.tests` alone.
* `mode`, `state`, `skip_reason`, `error`.
* `duration_seconds`.
* **Evidence** — what the check looked at: a path, line numbers, a quoted
  snippet, a link. A `pass` carries evidence too.
* **Findings** — the distinct problems, each with its own evidence and its own
  fingerprint.

A violation with no evidence is not recorded as a violation. The runner
downgrades it to `skipped: no_evidence`, keeps the check's own words so an
operator can see what it tried to say, and — because `no_evidence` is an
unanswered skip — still fails a blocking gate closed. A violation nobody can
look up is a guess, and this framework does not record guesses.

---

## The checks

### Native

Deterministic, diff-only, no model and no subprocess. Cheap enough to run on
every pull request on a four-core board.

| Check | Asks | Objects when |
|---|---|---|
| `native.title_description` | Does the pull request say what it does? | Empty or uninformative title, empty description, an unfilled template, or a description that is only a checklist. A draft titled `WIP` is exempt. |
| `native.docs` | Did a documented surface change without documentation? | An interface file, a new public symbol or a new route changed and no document did. |
| `native.tests` | Did source change without a test changing? | Source files gained lines and nothing the repository recognises as a test was touched. Deletion-only and generated-only changes do not count. |
| `native.breaking_change` | Does this take away something callers depend on? | A public symbol or route removed and not re-added, a new *required* parameter on an existing function, or a key removed from `.env.example`. |
| `native.migrations` | What does this do to the database, and can it be undone? | Destructive DDL (`DROP`, `RENAME`, a new `NOT NULL` on an existing column), or a migration whose downgrade is missing or empty. |

Each one points at a path and a line from the parsed diff. `native.migrations`
reads the migration file at the head commit to judge reversibility; if it
cannot read it, the result is an `infrastructure_error`, not a pass — the check
never established reversibility and does not claim it.

### Context

Checks that read something the diff does not contain.

| Check | Asks |
|---|---|
| `context.ticket` | Does the pull request reference an issue, and does that issue exist? |
| `context.acceptance_criteria` | Does the linked issue state acceptance criteria? |
| `context.ci` | What does CI say about this commit, and what exactly failed? |

**Tickets.** References are extracted offline from the title, the body and the
branch name: `#123`, `owner/repo#123`, issue URLs (GitHub, GitLab, Forgejo),
and any extra regex an operator configures. Resolution is a separate step with
three outcomes, not two:

* the issue exists → `pass`;
* the tracker says there is no such issue → `violation`;
* nobody could ask → `infrastructure_error`.

That third case is why the adapter contract makes a provider return `None` for
"no such issue" and *raise* for everything else. A check that inferred absence
from an exception would report a revoked token as every open pull request
referencing a ghost.

No external tracker is required. `checks.ticket.provider` defaults to `auto`,
which asks the hosting platform Mira is already authenticated against;
`"none"` disables lookups entirely and leaves reference extraction working.
Registering another adapter is a few lines — see
[Adding a ticket adapter](#adding-a-ticket-adapter).

**CI.** The check reads the head commit's CI state and, when it is red, the
failing jobs with an excerpt of each. Every value is bounded before it is read
(`checks.ci.max_jobs`, `checks.ci.max_log_bytes`), redacted before it is
stored, and quoted with the job name, the step and a link. The states map
straight onto the vocabulary: green is `pass`, red is `violation`, still
running is `skipped: pending` — which is unanswered, so a blocking CI check
cannot be satisfied by asking early — and unreadable is
`infrastructure_error`.

Summarising a failure with a model is **off by default**
(`checks.ci.summarize_with_llm`). When it is on, the log goes into a delimited
untrusted block and the summary is rendered as prose and parsed for nothing:
the state was decided from the CI status before the model was called, and the
model cannot reach it.

### Natural language

Rules a team writes as instructions:

```yaml
checks:
  natural_language:
    - id: rate-limit
      title: Endpoints declare a rate limit
      instruction: Every new HTTP endpoint must declare a rate limit.
      paths: ["src/api/**/*.py"]
      mode: warning
      version: "1"
```

The id is namespaced `nl.<id>`, so a repository cannot define a rule called
`native.tests` and have its instruction answer for the compiled check of that
name.

Four properties make this safe, layered so no single one has to hold alone:

1. **The policy is not in the data.** The instruction is deployment
   configuration and goes in the system message. The title, the description,
   the diff and any file body go in delimited untrusted blocks, under a
   standing instruction that content inside them is data and never an order.
2. **The output schema has nowhere to put an attack.** The model fills three
   fields: a verdict from a closed set, an explanation, and a list of quotes.
   There is no field that names a check, a mode, a path glob or a command, so
   the strongest thing an injected instruction can do is make one rule's
   verdict wrong.
3. **Every quote is verified.** Evidence is checked against the file at the
   head commit and against the diff before anything is recorded, and evidence
   from a path outside the rule's own scope is discarded. A violation with no
   surviving evidence becomes `skipped: no_evidence` — a model that invents a
   line produces silence, not an accusation.
4. **"Not sure" is an answer.** The schema has an `uncertain` verdict and the
   prompt says to use it. It becomes `skipped: ambiguous`, which for a rule in
   `error` mode still fails a gate closed — so saying "I do not know" is never
   a way to get a merge through.

### Deterministic analysers

| Tool | Runs | Notes |
|---|---|---|
| `semgrep` | pattern analysis over changed files | needs a ruleset; skips with the reason when none is named |
| `ruff` | changed Python files | |
| `eslint` | changed JS/TS files | needs a config; skips when none is named |
| `gitleaks` | all changed files | runs with `--redact`, so no credential is quoted back |
| `osv` | changed dependency manifests | not a subprocess — see below |

The tool name comes from a **closed allowlist** checked at config load, and
extra arguments come from deployment configuration as an argument *list*. There
is no shell, so there is nothing to inject into, and there is no path from a
repository to the name of a binary that gets executed. The only
repository-derived values in an argv are file paths the diff already contains,
written into a scratch directory a traversing path never escapes.

**A tool Mira did not run is never a pass.** A missing binary is
`skipped: tool_missing` with the binary named. A version that does not match
`require_version` is a skip with both versions named — a pin is a promise that
the rules being enforced are the ones somebody reviewed. A crash is an
`infrastructure_error`; a kill is a `timeout`.

**The workspace is not a checkout.** It holds the changed files at the head
commit. That is enough for a linter with a config file and is not enough for a
rule that resolves across the whole tree, so an analyser will find fewer things
here than it does in CI. Saying so is the honest version; a check that silently
under-reports is worse than one that says what it looked at.

**OSV reuses the existing scan.** `tool.osv` calls the same
`mira.security.pr_scan.scan_manifest_changes` the review pass calls, over the
same parsed manifests and the same OSV client, and re-renders the answer as
check findings. Turning it on adds a second *view* for the gate, the dashboard
and deduplication — not a second scan of record — and turning it off leaves the
review's inline OSV comments exactly where they were.

---

## Deduplication

Two producers will sometimes find the same thing. Reported twice, a reader has
to work out that "hardcoded credential at `config.py:14`" from gitleaks and "a
secret is committed here" from a rule are one issue before acting on either —
and the count at the top of the run, which is the number people read, is wrong.

A finding's identity is its **path plus its normalised description**, and
deliberately not its line number: two producers rarely agree to the line, and
any bucketing scheme fails at its own boundaries. The consequence is that one
rule violated three times in one file is one finding carrying three pieces of
evidence, which is also how a reader would rather read it.

When two producers match, the finding is merged:

* it appears **once**;
* `sources` names **both**;
* the evidence lists are **concatenated**, deduplicated on their own content —
  gitleaks knows the rule id and the exact span, the model knows why it matters
  in this file, and dropping either would throw away the half the reader
  wanted;
* ownership goes to a fixed precedence — native, tool, natural language,
  context, then check id — so two identical runs never attribute the same
  finding to different checks;
* the *other* producer keeps its own state. It really did find something, and
  rewriting it to `pass` would be a lie that happens to make the summary
  tidier; its summary says where the finding is reported instead.

---

## Configuration

Everything below is deployment configuration. Nothing in a pull request — its
title, body, diff, labels, CI logs or linked ticket — can change a mode, a
rule, a path glob or an argument vector. That is the security model, and it is
why the tool allowlist and the argument lists are shaped the way they are.

Three layers, resolved in this order:

1. the global `checks` block;
2. `checks.organizations[<owner>]`;
3. `checks.repositories[<owner>/<repo>]`.

`modes` **merge** key by key, so a repository changing one check does not
restate the other twelve. Lists **replace**, so `natural_language: []`
genuinely means "no language rules here" — the same `None`-inherits /
`[]`-is-empty sentinel the gate and autofix policies use. Tools **merge by
name**, so disabling one analyser does not drop the others.

```yaml
checks:
  enabled: true            # off by default
  kill_switch: false       # hard global disable, independent of everything else
  policy_version: checks-v1
  default_mode: warning    # off | warning | error

  modes:
    native.tests: error
    context.ci: error
    tool.gitleaks: error

  # Budget. The defaults assume a four-core board also serving webhooks.
  max_concurrency: 2
  check_timeout_seconds: 60
  total_timeout_seconds: 300
  max_evidence_per_check: 10

  publish_status: true     # a check run / commit status, where supported
  comment: false           # a PR comment, updated in place

  ticket:
    provider: auto         # auto | none | <registered adapter>
    require_reference: true
    require_acceptance_criteria: false
    reference_patterns: ["(?P<key>ACME-\\d+)"]
    exempt_labels: [no-ticket, chore, dependencies]

  ci:
    max_jobs: 3
    max_log_bytes: 16000
    max_evidence_lines: 20
    summarize_with_llm: false

  tools:
    - name: ruff
      enabled: true
      mode: warning
    - name: gitleaks
      enabled: true
      mode: error
    - name: semgrep
      enabled: true
      config_path: .semgrep.yml     # repository-relative, validated at load
      require_version: "1.9"        # substring of `semgrep --version`

  natural_language:
    - id: rate-limit
      instruction: Every new HTTP endpoint must declare a rate limit.
      paths: ["src/api/**/*.py"]

  organizations:
    acme:
      default_mode: warning
      modes: { native.docs: error }

  repositories:
    acme/legacy-service:
      modes: { native.tests: off }
      tools:
        - name: ruff
          enabled: false
```

A configuration Mira cannot read fails at **load**, never at check time: an
unknown analyser, an absolute or traversing `config_path`, an unparseable path
glob, a mode nobody can parse, a duplicate rule id, an invalid reference regex.
Each has no safe runtime interpretation — ignoring it silently removes a check
somebody believes is running, and refusing everything takes the install down on
a typo.

**The kill switch** (`checks.kill_switch`) stops every check in the install in
one edit, independent of `enabled` and of every per-scope override. It is
recorded on the resolved policy as `killed`, so a run row shows that a switch —
not a policy rewrite — made the framework inert.

---

## Budgets and concurrency

* `max_concurrency` (default 2) bounds how many checks run at once. The
  reference deployment is an Orange Pi also serving webhooks; a linter fan-out
  that saturates it turns every review into a timeout.
* `check_timeout_seconds` (default 60) bounds one check. Exceeding it is a
  `timeout` result.
* `total_timeout_seconds` (default 300) bounds the run. A check that never
  started because the budget was spent is `skipped: budget_exhausted` — which
  is unanswered, so it does not quietly satisfy a blocking gate.

A check that queues behind others has its ceiling measured when it starts, not
when it was scheduled: otherwise a late check would inherit an early check's
budget and blow the run's total.

---

## What the merge gate does with it

The relationship is one-directional. Checks never approve, never request
changes and never merge anything. They produce a verdict; the gate reads it as
one input among several.

| Run verdict | Gate |
|---|---|
| `pass` | adds nothing |
| `not_run` | adds nothing |
| `violation` | `not_approved`, reason `checks_violation`, naming the checks |
| `incomplete` | `not_approved`, reason `checks_incomplete`, saying it does not know |

Two reason codes rather than one, because the two facts are different and
reporting the second as the first is the exact confusion this phase removes.
`checks_incomplete` is a **hard veto** — an admin cannot override an approval
past "we do not know", the same treatment `evaluation_error` already gets.
`checks_violation` is not: a check that answered and objected is a judgement an
admin may override, with the override recorded.

The gate only ever consults this for what it *refuses*, so turning checks on
can never make it more willing to approve than it was before. It reads the
newest run **for the head commit**: a pull request pushed to since its last
check run reports `not_run`, and a clean run against the previous commit does
not stand in for one against this one. `gate.require_checks_pass` (default
`true`) turns the consultation off; it costs nothing while every check is in
`warning` mode, because `blocking` is false for those.

Checks run before the gate in both places that trigger them: the review
pipeline, and the CI/label webhook that re-evaluates a pull request without
re-reviewing it.

---

## The dashboard

| Route | What it answers |
|---|---|
| `GET /api/checks/runs` | Every run, filtered by repo, verdict, PR, author, commit, time. |
| `GET /api/checks/runs/{owner}/{repo}/{id}` | One run: every check, its evidence, its origin, its duration, plus both explanations. |
| `GET /api/checks/results` | One check's history — filter by `check_id`, `state`, `origin`, `blocking`, `incomplete`. |
| `GET /api/checks/summary` | Counts by check and state, plus `inconclusive` against `total`. |
| `GET /api/checks/catalog` | Every check that *would* run here, with its version, mode and config digest. |
| `GET`/`PUT /api/checks/config` | The stored override and the effective policy. |
| `GET /api/checks/config/audit` | Who changed the policy, when, from what and to what. |

All admin-only, reads included: a check result quotes diff lines, CI output and
ticket titles across every repository in the install, which is governance data
rather than per-repo browsing data. Every mutating route passes the dashboard's
origin check, so a session cookie alone is not enough to change a policy from
another site.

Two of these deserve a note.

**`incomplete=true`** is the filter to reach for after an incident. It selects
exactly the results that were *not* statements about a pull request — the set a
noisy-check investigation must exclude and an infrastructure investigation must
start from.

**The audit trail is separate from the policy** because the settings blob only
ever holds the current value. A policy that was loosened for an afternoon and
tightened again leaves no trace in it, and that is precisely the change
somebody comes looking for.

**The catalog answers the coverage question** the run history cannot: a check
that never appears in a result row might be off, might not apply, or might not
exist in this version of Mira, and only the catalog distinguishes the third.

---

## Storage, migration and rollback

Two tables, `check_runs` and `check_results`, with the same columns on SQLite
and Postgres. The queries are written once in `mira.checks.persistence` and
mixed into both stores, so parity is a property of the code rather than a
promise in a docstring.

**Identity.** A run is keyed on the pull request, the head commit, the resolved
policy *and* the facts it was run over. A redelivered webhook, a manual re-run
and a second worker converge on the same row; a push, a policy edit or a
changed fact records the new run it is.

**Retries update rather than being ignored.** This is the one place the check
tables differ from `gate_decisions`, and the reason is what the two records
are. A gate decision is an *act* — it may already have been delivered as an
approval, and rewriting it could erase an administrative override. A check
result is an *answer to a question*, and when the same question is asked again
over identical facts the newest answer is the one worth keeping: a run that
failed because the network was down should read as resolved once it is retried
successfully, not stay wrong forever. `attempts` on the run row records that it
took more than one go.

**Migration is opening the file.** The schema is `CREATE TABLE IF NOT EXISTS`
and runs on every connection, so an existing database gains the two tables when
a Phase 6 build first opens it. There is no migration step to run, nothing to
sequence, and no maintenance window — an update is a container restart.

**Rollback is a tag change.** A database a Phase 6 build wrote to carries two
tables the previous release has never heard of. Nothing in the older code
selects from them, so the rows sit unread and every earlier query still
answers. Downgrading needs no dump and no restore. The `gate.require_checks_pass`
setting and the two new reason codes degrade the same way: an older gate simply
does not consult them.

Both properties have tests (`tests/test_checks_store.py`), and the ARM64 smoke
job in CI exercises the same sequence for real: it creates a SQLite database
with the *deployed* image, starts the candidate against it, writes and re-reads
a check run through the new tables (asserting the retry converges on one row),
then runs the deployed image against the same volume again and checks it still
answers. Migration and rollback on `linux/arm64`, on the actual images, rather
than a claim in this file.

---

## Provider support

| | GitHub | GitLab | Forgejo |
|---|---|---|---|
| Read issues | ✅ | ✅ | ✅ |
| Read CI state | ✅ | ✅ | ✅ |
| Read failing job output | ✅ (check-run output) | ✅ (job traces) | ❌ (statuses carry no log) |
| Publish a status | ✅ | ❌ (deliberate) | ✅ |

Capabilities are **declared, not probed**: a probe costs a round trip per pull
request on a device that has none to spare, and one that failed transiently
would silently remove a check an operator believes is running. A provider may
narrow the table — a token with reduced scopes — and may never widen it.

Two deliberate gaps:

* **Mira publishes no commit status on GitLab.** A status posted through the
  API joins the head pipeline, so it would corrupt the CI signal these checks
  read back, and on a project with "pipelines must succeed" a green one could
  satisfy the very restriction a failing check just refused to satisfy. Turn on
  `checks.comment` to surface the summary on the merge request. The merge gate
  reached the same conclusion for the same reason.
* **Forgejo reports CI as commit statuses**, which carry a description and a
  link and no job output. The CI check quotes what the status says and records
  that the log itself was not available, rather than quoting an empty string as
  evidence.

Every check-run name Mira publishes — the gate's and this framework's — is
filtered out of the CI those same providers report back. Without it the CI
check would read Mira's own red status as a failing build, publish a red status
saying so, and do it again on the next event.

---

## Extending it

### Adding a check

A check is a coroutine from `CheckContext` to `CheckOutcome`, plus a
`CheckSpec` in `mira.checks.registry`. It cannot reach the store, cannot reach
the platform's write surface, cannot see another check's result, and cannot
decide whether it blocks — the mode comes from configuration it never sees.

```python
async def run(ctx: CheckContext) -> CheckOutcome:
    if not ctx.changed_paths:
        return CheckOutcome.skipped("nothing changed", SkipReason.NOT_APPLICABLE)
    return CheckOutcome.passed("all good", [Evidence(path=ctx.changed_paths[0])])
```

Return `CheckOutcome.failed(...)` when *Mira* could not answer. The default
summary says so in those words, and it is worth keeping.

### Adding an analyser

Subclass `SubprocessTool`, supply `argv` and `parse`, register it in
`mira.checks.tools`, and add its name to `TOOL_ALLOWLIST`. Adding an entry to
the allowlist is deliberately more work than editing a config string: "run
whatever the repository asks for" is the failure mode the design exists to make
impossible.

### Adding a ticket adapter

```python
from mira.checks.external.tickets import TicketLookupError, register_adapter

class JiraAdapter:
    name = "jira"

    async def fetch(self, ref, ctx):
        if ref.kind != "external":
            raise TicketLookupError("not a Jira reference")
        ...   # return an IssueInfo, or None if Jira says there is no such issue
              # raise TicketLookupError for anything that stopped you asking

register_adapter("jira", JiraAdapter)
```

The `None`-versus-raise distinction is the contract, and it is the whole reason
the interface exists in this shape. The built-in `auto`, `platform` and `none`
adapters cannot be replaced — an install that could rebind `none` to something
that makes network calls would have a kill switch that does not switch
anything off.

---

## Rollout

1. **Enabled, everything `warning`.** Nothing blocks. The dashboard fills with
   results and the summary shows which checks are noisy and which cannot run.
2. **Watch `inconclusive` against `total`.** A rollout where that climbs has an
   infrastructure problem, and no per-check violation count would show it.
3. **Move one check to `error`** on one repository, via
   `checks.repositories`. Remember that `error` mode makes an *unanswered*
   result block too — that is the point, and it is worth knowing before the
   first pull request meets it.
4. **Turn on `gate.require_checks_pass`** — it is on by default and costs
   nothing until a check is in `error` mode.
5. `checks.kill_switch` stops all of it in one edit if anything goes wrong.
