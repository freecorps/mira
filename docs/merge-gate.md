# Merge gate (Phase 4)

The review verdict answers *is this code good?* The merge gate answers a
different, narrower question: **may Mira put its name on merging this?**

They are separate on purpose. A quality score is an opinion that can be wrong
and cost nothing. An approval is an action: on GitHub it counts toward a
branch-protection rule, and a wrong one is somebody merging unread code on
Mira's word. So the gate has its own domain (`mira.gate`), its own risk score,
its own persistence, and its own permission model.

The gate never merges anything, and it never replaces a human review.

## The one guarantee

**Nothing uncertain and nothing protected can result in a real approval.**

A missing input, a timed-out provider call, an unparseable CODEOWNERS file, a
pending CI run, an open blocker, a protected path, a repository whose index is
not ready — every one of them decides against approving, and every one of them
says which it was.

This is a property of the shape of the code, not of a handler that catches the
bad case. `decide()` cannot return `approved` at all: it returns
`would_approve` at best, and only a delivery the platform confirmed moves a
decision to `approved`. Fail-closed is a value that was never set.

## States

| State | Meaning |
|---|---|
| `skipped` | The gate had no business deciding. It is off for this repository, the PR is out of scope, or the diff carries nothing it can reason about. |
| `not_approved` | The gate looked and decided against approving. |
| `would_approve` | The gate would have approved and deliberately did not: shadow mode, or a platform that cannot record an approval. **Never rendered or reported as an approval.** |
| `approved` | A real approval was delivered and the platform confirmed it. |
| `error` | Evaluation or delivery failed. Never an approval, and never becomes one. |

## Modes

```yaml
gate:
  mode: "off"      # off | shadow | enforce
  kill_switch: false
```

- **`off`** — the gate does not run. The default, and the only default.
  Nothing is fetched from the platform, so an install that never turned it on
  pays nothing for it.
- **`shadow`** — evaluate, explain and record; never approve. The dry run.
- **`enforce`** — the same decision, plus a real approval when it says so.

`kill_switch: true` hard-disables the gate everywhere, independent of `mode`
and of every per-repository override. It exists so an operator can stop the
gate during an incident with one edit and reconstruct nothing afterwards.

### Rolling it out

1. Set `mode: shadow`. Decisions are recorded and explained; nothing is
   submitted.
2. Watch **Merge gate → History** in the dashboard. The headline number is
   *would have approved* — how many pull requests the gate would have put its
   name on.
3. Compare that set against what actually happened to those pull requests.
   That comparison is the false-approval rate, and it is only meaningful
   because a shadow run records the same decision it would have acted on.
4. Tighten `risk_threshold`, `protected_paths` and the eligibility lists until
   the rate is one you would accept.
5. Only then set `mode: enforce`.

## The eligibility matrix

Every check produces a reason with a kind:

- **`skip`** — out of scope. The decision is `skipped`.
- **`block`** — in scope and disqualified. The decision is `not_approved`.

The distinction is for measurement, not safety — both mean "no approval". It
is what lets a rollout tell *the gate never applies here* from *the gate
applies and keeps saying no*, which is the difference between a misconfigured
scope and a policy that is too strict.

All checks run; the decision lists every reason rather than stopping at the
first, because a decision that reports one of four problems sends someone round
the loop three more times.

### Out of scope (`skipped`)

| Reason code | When |
|---|---|
| `gate_off`, `repo_disabled`, `kill_switch` | The gate is not running here. |
| `pr_draft` | The pull request is a draft (`skip_draft_prs`). |
| `self_authored` | Mira opened the pull request itself. |
| `base_branch_out_of_scope` | The base branch is not in `allowed_base_branches`. |
| `author_not_in_allowlist` | The author is not in `allowed_authors`. |
| `missing_required_label` | A `required_labels` entry is absent. |
| `generated_only_diff` | Every changed file is generated output. |
| `human_already_approved` | A human has already approved. |

### Disqualifying (`not_approved`)

| Reason code | When |
|---|---|
| `blocked_label` | A `blocked_labels` entry is on the PR. |
| `blocked_base_branch` | The base branch never receives an automatic approval. |
| `author_blocked` | The author is in `blocked_authors`. |
| `author_association_unknown` | The platform would not say who the author is to this repository. |
| `author_association_insufficient` | The association is not in `allowed_author_associations`. |
| `pr_too_many_files`, `pr_too_many_lines` | Above the size limits. |
| `protected_path` | A protected path was touched. |
| `codeowners_path`, `codeowners_unreadable` | CODEOWNERS assigns an owner, or could not be parsed. |
| `ci_failing`, `ci_pending`, `ci_unknown` | CI is failing, has not finished, or produced no passing result — including a commit nothing ran on and one whose status could not be read. |
| `review_incomplete` | Files in the PR were never reviewed. |
| `review_failed` | The review did not complete (LLM failure, parse failure). |
| `index_not_ready` | The repository index is incomplete, so cross-file context was partial. |
| `open_blocker` | A blocker finding is still open. |
| `severity_above_ceiling` | The worst open finding exceeds `approve_max_severity`. |
| `human_changes_requested` | A human asked for changes. |
| `risk_above_threshold` | The risk score is above `risk_threshold`. |

## Risk score

A deterministic integer, 0–100, that is never a number on its own — it is a
list of named factors that add up to it, each carrying its own explanation.
Integer arithmetic only, so the same inputs give the same score on every
replica and a stored decision can be recomputed and checked.

This is **not** the review's quality score and does not read from it. Quality
asks whether the code is good; risk asks how much is riding on being wrong
about that. A flawless 4,000-line change to the deployment pipeline is high
quality and high risk at once, and the gate has to be able to say so.

No LLM is involved. On the Orange Pi profile the gate has to be effectively
free next to the review, so scoring is arithmetic over facts the review already
gathered — the engine hands it the parsed diff's per-file counts rather than
fetching anything a second time.

Those counts come from the **whole, unfiltered** diff — not from the list of
files Mira reviewed, and not from the incremental diff a second review round
looks at. Deletions, binaries and `filter.exclude_patterns` matches are gone
from the review's list, and a round-2 review only looks at the newest commits.
Whether a file was reviewed has nothing to do with whether it is protected, and
answering the second question from the first is how a deleted CI workflow gets
approved.

Factors, with their default weights:

| Factor | Points | |
|---|---:|---|
| `open_blocker` | 100 | An open blocker outranks everything. |
| `human_changes_requested` | 100 | |
| `protected_path` | 100 | |
| `codeowner_path` | 40 | Only scored when the CODEOWNERS integration is on. |
| `ci_not_success` | 30 | |
| `unknown_association` | 25 | |
| `security_findings` | 15 | Any finding in the security category. |
| `first_time_contributor` | 15 | |
| `unreviewed_paths` | 15 | |
| `index_not_ready` | 10 | |
| `warning_findings` | 8 each, capped at 32 | |
| `dependency_manifest` | 8 | The dependency surface moved. |
| `generated_heavy` | 5 | Most of the diff is machine output. |
| `size_files` | 1 per file over 5, capped at 20 | Generated files are excluded. |
| `size_lines` | 2 per 100 lines over 100, capped at 20 | So are their lines. |
| `suggestion_findings` | 1 each, capped at 6 | |

Risk is scored for **every in-scope pull request**, including ones already
disqualified. A rollout needs the score of the PRs the gate refused as much as
the score of the ones it would have approved, or the threshold can only be
tuned from half the data.

## Protected paths

A protected path is an absolute veto: **no protected path is ever
auto-approved**, however clean the pull request and however low the score. No
administrative override can force one either.

Patterns are gitignore-shaped and spelled out, because an operator has to be
able to read one and know exactly what it covers:

| Pattern | Covers |
|---|---|
| `infra/**` | everything under `infra/`, and `infra` itself |
| `infra/` | the same, written as a directory |
| `*.tf` | a `.tf` file in *any* directory |
| `/deploy/*.yaml` | `.yaml` files directly in the root `deploy/` |
| `**/migrations/**` | anything under any `migrations` directory |

A pattern with no separator matches on the basename in any directory (the
reading most people expect from `*.pem`). A pattern *with* a separator is
matched against the whole path, anchored at the repository root. `*` never
crosses a `/`; `**` does.

A pattern the matcher cannot compile **fails the configuration load**. There is
no safe runtime interpretation: ignoring it silently un-protects a path, and
vetoing everything takes the install down on a typo.

The built-in list covers CI definitions, container and deployment topology,
database migrations, Terraform/Helm/Kubernetes, credentials and key material,
`CODEOWNERS`, and Mira's own `.mira.yaml`. Replace it with `protected_paths` or
extend it with `extra_protected_paths`.

## CODEOWNERS

Optional, and off by default — it is an integration, not a requirement.

```yaml
gate:
  codeowners: "off"   # off | risk | block
```

- **`off`** — CODEOWNERS is never read.
- **`risk`** — an owned path adds risk but is not on its own disqualifying.
- **`block`** — an owned path is never auto-approved. The conservative reading,
  and what "on" should mean for most deployments.

A CODEOWNERS entry is a repository saying *a specific human signs off on this
file*. Mira is not that human, so ownership is only ever a reason **not** to
approve — never a reason to approve on the owner's behalf, and never a
substitute for their review.

The parser is strict. A file it cannot fully understand produces `unreadable`,
which in `block` mode is a veto: guessing at an ownership rule is exactly how a
protected file gets approved by accident. GitLab section headers (`[Backend]`,
`^[Optional]`, `[Backend][2]`) are understood and skipped. A trailing rule with
no owners genuinely un-owns a path, because writing one is a deliberate
statement — that is the single place this module resolves toward "not owned".

## Blockers and REQUEST_CHANGES

**A pull request with an open blocker finding is never approved**, whatever the
overall score says.

A finding counts as open unless something explicitly closed it (`fixed`,
`resolved`, `dismissed`). `outdated` is deliberately *not* closed: it only
means the diff moved past the line the comment was anchored to, which is
exactly what an unaddressed blocker looks like after a rebase. Blockers from
earlier rounds count too — a gate that only looked at the newest round could be
out-waited.

```yaml
gate:
  request_changes_on_blockers: false
```

When enabled, and only in `enforce` mode, and only for open blockers, the gate
submits a `REQUEST_CHANGES` review event. It is **never** submitted when a human
has already reviewed the pull request, in either direction: a `REQUEST_CHANGES`
over someone's `APPROVE` is as much of an overwrite as an `APPROVE` over their
`CHANGES_REQUESTED`.

## Provider capabilities

Capabilities are declared, not probed — a probe costs a round trip per pull
request on a device that has none to spare, and one that fails transiently
would silently downgrade a working install.

| | GitHub | GitLab | Forgejo |
|---|---|---|---|
| Approve | ✅ | ✅ (tier-dependent) | ✅ |
| Request changes | ✅ | ❌ | ✅ |
| Status check | ✅ check run | ❌ (see below) | ✅ commit status |
| Read CI | ✅ check runs + statuses | ✅ head pipeline | ✅ commit statuses |
| Read association | ✅ | ✅ access levels | ✅ permissions |
| Read review states | ✅ | ✅ approvals only | ✅ |
| Read labels | ✅ | ✅ | ✅ |

When something is not supported, the decision **degrades explicitly** to
`would_approve` or `skipped` and says why (`provider_cannot_approve`,
`provider_cannot_request_changes`). It never reports an approval nobody
received — that is the one failure an operator could not notice from the
dashboard. A missing status check degrades the same way: the decision is still
reached, recorded and explained, it simply is not announced on the commit.

GitLab has no `REQUEST_CHANGES` review event. The gate says so rather than
approximating one with a comment no merge rule reads. GitLab merge-request
approvals are a tiered feature and can be switched off per project, so a
refusal at delivery time is expected: the delivery is recorded as failed and
the decision stays `would_approve`.

A provider may *narrow* the platform's capabilities (a token with reduced
scopes) but never widen them.

Two of these are not optional. A provider that cannot read labels, when the
policy consults labels, and a provider that cannot report human review states
both produce an `error` decision rather than a guess — an empty label list
reads as "no `do-not-merge` label" and an empty review-state mapping reads as
"nobody objected". CI state and author association, by contrast, have a safe
unknown, so they report it and the gate treats it as not good enough.

If a delivery claim is left `in_flight` by a worker that crashed mid-call, the
decision stays un-approved and the claim is not retried. That is the safe
direction, and the `in_flight` state is visible on the decision so it can be
told from a delivery that simply has not been attempted.

## Idempotency and concurrency

Two levels, because they answer different questions.

**Decisions** are identified by a hash of platform, owner, repository, PR
number, head commit, resolved policy *and* the inputs. A webhook redelivered
over unchanged facts converges on the row that already exists; a re-evaluation
after CI turned green is recorded as the new decision it is. Recording is
insert-only, so a repeated evaluation can never erase an administrative
override or re-arm a delivery that already happened.

**Side effects** are claimed. The claim key is scoped to the pull request and
head commit — deliberately coarser than the decision — so two decisions over the
same commit still produce at most one approval. `claim_gate_delivery` returns
true exactly once; a redelivered webhook, a second worker and a retried
background task race there and the losers do nothing. A *failed* attempt is
reclaimable, so a transient error still gets a second chance.

Status checks and PR comments are not claimed: they are updates to a named
artifact (a fixed check name, a comment marker), so re-sending one replaces it.

The gate re-evaluates on `check_suite`/`check_run` completion and on
label/draft changes, because those are the two things that make a decision
stale without changing a line of code. Re-evaluation costs no LLM call, and the
policy is resolved *before* an installation token is minted, so an install that
never turned the gate on pays nothing for every check suite that finishes.

On GitHub and Forgejo the gate's own status check is excluded from the CI
reading. Counting it would let the gate read its own verdict back as a failing
build, and would change the check count on every pass — which changes the
inputs digest, which would manufacture a fresh decision row each time.

**GitLab gets no status check at all**, and that is the interesting case.

A GitLab commit status is not a neutral annotation surface: posting one through
the API *joins the head pipeline*. Two things follow, and both are disqualifying
for a merge gate.

The gate would be writing into the exact signal it reads back. On a project
using merged-results pipelines the real jobs live on the merge-ref commit, so
once the gate had posted its own status the head commit would carry nothing
else — and any guard clever enough to notice would then conclude there is no CI
here at all.

Worse, on a project with no other CI and "Pipelines must succeed" enabled, a
green status *becomes* a green pipeline, which can satisfy the merge
restriction. The gate would be manufacturing the very permission it had just
refused to give. That is the worst outcome available here, so it writes nothing
and declares the capability missing, exactly like any other.

Turn on `gate.comment` to put the explanation on the merge request; the
dashboard always has it. `get_ci_state` then reads `head_pipeline` plainly,
which was always the right way to read GitLab CI — a run blocked on a manual
gate reports green rather than pending forever, and a merged-results pipeline
is found — and needs no guard at all once nothing writes there.

## Overrides

Every override records **who, why, the previous decision and the new one**, in
an append-only trail alongside the decision.

An override changes *Mira's record*. It never submits or retracts a review
event on the platform. That boundary is the point: if an override could reach
through to an approval, "who may administer Mira" and "who may approve this
pull request" would collapse into one permission, and the platform's own review
would stop being the thing that gates the merge.

```yaml
gate:
  allow_overrides: true            # revoking a decision by hand
  allow_approval_override: false   # forcing one — a separate power
  override_admins: []              # empty = every admin
```

The asymmetry is deliberate. Revoking is always available to an authorized
admin; forcing an approval is its own opt-in, and it is **refused outright**
past a hard veto — a protected path, a CODEOWNERS-owned path, an open blocker,
a human's requested changes, or a failed evaluation.

## Permissions

Three, kept apart:

- **Reading a decision** — admin. A decision quotes PR authors, protected paths
  and CI failures across the whole install; that is governance data.
- **Editing the policy** — admin, and the only way policy changes at all.
- **Overriding a decision** — admin *plus* membership of `override_admins` when
  that list is non-empty.

Every mutating route also passes the dashboard's origin check, so a session
cookie alone is not enough to move a decision from another site.

## What cannot change the policy

Nothing in a pull request. Not the title, the body, the diff, a comment, a CI
log, or a label.

Policy comes from deployment configuration only: the global `gate` block, the
admin-editable overrides in the dashboard database, and the per-repository
entries under `gate.repositories`. The server resolves its configuration from
its own working directory, so a `.mira.yaml` committed *inside a reviewed
repository* never reaches the gate.

Labels and branches *are* consulted — because an operator configured them as
policy inputs. Consulting them can only take a pull request out of scope or
disqualify it. No label grants an approval, and a required label is necessary,
never sufficient.

The resolved policy is hashed into every decision key, so a decision made under
one policy can never be reused as though it had been made under another.

## Per-repository policy

```yaml
gate:
  mode: "shadow"
  repositories:
    acme/payments:
      mode: "off"
    acme/docs:
      mode: "enforce"
      max_changed_files: 50
      blocked_labels: []          # explicitly empty, not "inherit"
      extra_protected_paths:
        - "content/legal/**"
```

`null` (an absent key) inherits. Anything else overrides — including `[]`,
which is how a repository opts out of an inherited requirement rather than
inheriting it forever.

## Storage

`gate_decisions`, `gate_deliveries` and `gate_overrides` exist in SQLite and
PostgreSQL with identical columns, and the queries are written **once** in
`mira.gate.persistence` and mixed into both stores. Parity is a property of the
code rather than a promise in a docstring — there is no second copy to drift.

Each backend supplies four primitives: a placeholder, a read, a write, and its
spelling of "insert unless it already exists".

Migrations are additive. Both stores run `CREATE TABLE IF NOT EXISTS` on every
connection, so an existing database picks the tables up on the next start with
no migration step and no downtime.

## Full configuration

```yaml
gate:
  mode: "off"                    # off | shadow | enforce
  kill_switch: false             # hard global disable
  policy_version: "gate-v1"      # recorded with every decision

  # Eligibility. Empty allowlists mean "any"; blocklists always win.
  allowed_base_branches: []
  blocked_base_branches: []
  required_labels: []
  blocked_labels: ["do-not-merge", "wip", "hold", "mira-paused"]
  allowed_authors: []
  blocked_authors: []
  allowed_author_associations: ["OWNER", "MEMBER", "COLLABORATOR"]
  skip_draft_prs: true
  max_changed_files: 20
  max_changed_lines: 500
  generated_paths: null          # null = the built-in list; [] = nothing
  size_excludes_generated: true

  # Protected paths and CODEOWNERS.
  protected_paths: null          # null = the built-in list
  extra_protected_paths: []
  codeowners: "off"              # off | risk | block

  # Completeness. Each resolves an unknown to "no approval".
  require_ci_success: true
  require_all_files_reviewed: true
  require_index_ready: true
  approve_max_severity: "suggestion"

  # Risk.
  risk_threshold: 25
  risk_medium_at: 20
  risk_high_at: 50

  # Actions.
  request_changes_on_blockers: false
  publish_status: true           # neutral in shadow mode
  comment: false

  # Overrides.
  allow_overrides: true
  allow_approval_override: false
  override_admins: []

  # Budget. Exceeding it is an `error` decision, which never approves.
  timeout_seconds: 20.0

  repositories: {}
```

## Dashboard

**Merge gate → History** lists every recorded decision with its state, risk
score and mode. Expanding one shows the reasons, the scored factors that add up
to the risk, and the override trail. The headline tiles lead with *would have
approved* next to *actually approved* — the false-approval measurement.

**Merge gate → Policy** edits the gate section of the admin override blob. Only
that section is written; the review and filter overrides set on the settings
page are read back and rewritten unchanged, so two panels editing one document
cannot clobber each other. Policy is validated against the real model before it
is stored, so a typo fails the request rather than the next pull request.

`PUT /api/gate/config` replaces the whole `gate` section rather than merging
into it, which is what makes `blocked_labels: []` expressible at all — a merge
could not tell an empty list from "leave it alone". The caller therefore resends
what it wants kept, and the panel does that by spreading the loaded overrides
under its own form values, so per-repository policy and risk weights it does not
render survive a save.

## Not in this phase

Autofix. `@mira fix` and the fix-and-verify loop are Phase 5; the gate decides
whether a change may be approved, and does not change any code.
