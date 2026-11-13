# Execution-backed review

**Status:** implemented, off by default
**Scope:** running a pull request's code in a sandbox during its review —
tool output on the changed lines in the review prompt, and reproduction tests
for the strongest findings — and the threat model that makes that acceptable.

Every finding Mira posts used to rest on a model's reading of the diff, and the
only check on that reading was another model's opinion: the self-critique
grades each draft `proven`, `plausible` or `unsupported`, and in one week it
dropped 74 drafts, 50 of them graded only "plausible". Precision and recall
both hinged on a model's opinion. This feature adds the one kind of evidence
neither model can argue with: what the code does when it runs.

With `review.verify` configured for a repository, each review:

1. **Builds a workspace** — the pull request's head, in a sandbox — and runs
   the repository's `setup` steps in it (dependency install).
2. **Runs the configured tools** (`ruff`, `eslint`, `tsc --noEmit`,
   `pytest <related tests>`, …) and puts their diagnostics on the lines the
   pull request added into every review part's prompt, as a bounded
   *Tool output on changed lines* block. A tool the pre-merge checks do not
   already run also gets its diagnostics posted as findings.
3. **Verifies the strongest findings** after the self-critique: for the top
   `max_findings` blockers and warnings in logic categories, the review model
   writes a minimal reproduction test in the repository's test framework; Mira
   runs it and reads the answer from the process.
4. **Deletes the workspace**, whatever happened.

---

## Outcomes

| Outcome | What happened | What Mira does |
|---|---|---|
| `reproduced` | The test failed on its reproduction assertion, every run. | The comment gets a visible **✅ Reproduced** note with the test and its output collapsed in `<details>`, and its confidence is raised to at least 0.97. |
| `not_reproduced` | The test passed, every run, in a harness proven to report failures. | The comment is downgraded one severity level and says so (**🧪 Not reproduced — Downgraded from blocker**). If the critic had only graded it `plausible`, it is dropped instead: two weak signals against it and none for. |
| `inconclusive` | Anything else: the model judged the claim untestable in isolation, the test could not be built or imported, it failed somewhere other than its assertion, it was flaky, it timed out, the budget ran out, no sandbox was available. | Nothing. The comment is posted as the reviewer wrote it. |

**How "fails for the stated reason" is decided.** The model is told to put the
claim in one assertion whose failure message contains `MIRA-REPRODUCED`, built
in the source by concatenation (`"MIRA-" + "REPRODUCED"`). The literal text
therefore appears in the output only when that assertion *executed and
failed* — never because a runner printed the test's source next to an
`ImportError` or a `TypeError` three lines earlier. A test that spells the
marker out literally is refused.

**Flakiness.** Every test runs `runs` times (2 by default). Runs that disagree
are `inconclusive`.

The test is never committed or pushed. It is written into the workspace, run,
and deleted; the workspace is deleted when the review ends.

---

## Configuration

Everything is under `review.verify` in the deployment's `mira.yaml` (or the
dashboard overrides), with per-repository entries under
`review.verify.repositories`. Nothing is read from the repository's own
`.mira.yaml` — see the threat model below for why.

```yaml
review:
  verify:
    enabled: false            # global default; turn it on per repository below
    isolation: auto           # auto | bwrap | sandbox-exec | unshare | none

    repositories:
      acme/api:
        enabled: true
        # Install what the tests need. The only phase with network access.
        setup:
          - name: install
            command: ["uv", "sync", "--frozen"]
        # Tool evidence. `{files}` = changed files (narrowed by `extensions`),
        # `{tests}` = existing tests related to them. Skipped when either
        # expands to nothing.
        commands:
          - name: ruff
            command: ["ruff", "check", "--output-format", "json", "--no-cache", "{files}"]
            parser: ruff
            extensions: [".py"]
          - name: mypy
            command: [".venv/bin/mypy", "--no-error-summary", "{files}"]
            parser: text
            extensions: [".py"]
          - name: pytest
            command: [".venv/bin/python", "-m", "pytest", "-q", "-x", "{tests}"]
            parser: pytest
            post_findings: false
        # Reproduction tests. `{test}` = the file Mira writes, inside the
        # repository's own test directory.
        test_runners:
          - name: pytest
            extensions: [".py"]
            command: [".venv/bin/python", "-m", "pytest", "-q", "{test}"]

      acme/web:
        enabled: true
        setup:
          - command: ["npm", "ci", "--ignore-scripts"]
        commands:
          - name: tsc
            command: ["npx", "--no-install", "tsc", "--noEmit", "--pretty", "false"]
            parser: tsc
          - name: eslint
            command: ["npx", "--no-install", "eslint", "--format", "json", "{files}"]
            parser: eslint
            extensions: [".ts", ".tsx"]
        test_runners:
          - name: vitest
            extensions: [".ts", ".tsx"]
            command: ["npx", "--no-install", "vitest", "run", "{test}"]
            canary: |
              import { test } from "vitest";
              test("canary", () => { throw new Error("MIRA-" + "CANARY"); });
```

| Key | Default | Meaning |
|---|---|---|
| `enabled` | `false` | Turn the feature on. With neither `commands` nor `test_runners` it does nothing. |
| `isolation` | `auto` | How commands are fenced in; see below. |
| `extra_read_only_paths` | `[]` | Host paths made readable inside bubblewrap, for toolchains outside `/usr`, `/opt` and the venv a binary resolves into. |
| `max_workspace_mb` | `200` | A head commit with more text than this is not written. |
| `setup` | `[]` | Install steps, in order; the first that fails stops the rest. |
| `setup_network` | `true` | Whether setup may use the network. Nothing else ever can. |
| `setup_timeout_seconds` | `300` | Wall clock for all setup steps together. |
| `memory_limit_mb`, `cpu_seconds` | `4096`, `300` | rlimits on every command. Node and the JVM reserve far more address space than they use. |
| `commands` | `[]` | Tool evidence commands: `name`, `command`, `parser` (`ruff`, `eslint`, `tsc`, `pytest`, `text`), `extensions`, `timeout_seconds`, `post_findings`. |
| `command_timeout_seconds` | `60` | Per command. |
| `evidence_wait_seconds` | `90` | How long a review part waits for the tools, counted from the start of the review. Past it the part goes without the block. |
| `max_evidence_chars` | `6000` | Tool output per part's prompt. |
| `max_tool_findings` | `10` | Tool diagnostics posted as findings per review. |
| `test_runners` | `[]` | Reproduction runners: `name`, `extensions`, `command` with `{test}`, optional `canary`. |
| `categories` | bug, security, error-handling, race-condition, concurrency, resource-leak | Findings eligible for a reproduction test. |
| `min_severity` | `warning` | `warning` or `blocker`. |
| `max_findings` | `3` | Findings verified per review, most severe and most confident first. |
| `test_timeout_seconds` | `60` | One run of one test. |
| `budget_seconds` | `180` | The whole verification step, writing the tests included. |
| `max_concurrent` | `3` | Verifications in flight at once. |
| `runs` | `2` | Runs per test that must agree. |

A repository entry may override `enabled`, `setup`, `setup_network`,
`setup_timeout_seconds`, `commands`, `test_runners`, `max_findings`,
`test_timeout_seconds` and `budget_seconds`. `None` (absent) inherits; `[]`
means "none here".

### Latency

The workspace, setup and tools start with the review and run alongside the
walkthrough and context building; a part waits for them at most until
`evidence_wait_seconds` after the review started. Verification runs after the
critic and adds at most `budget_seconds`, with up to `max_concurrent` tests in
flight. A review where nothing is eligible adds nothing.

### What the workspace is

The review's repository snapshot — the archive of the head commit it already
downloads for code context — written to a temporary directory. There is no
`git clone`, because a clone needs a token and a token in the workspace is a
token the pull request's code can read. The snapshot holds the repository's
text files; binaries, `node_modules/`, `vendor/`, `dist/`, `build/` and files
over the snapshot's size limit are not in it, so `setup` has to install what
the tests import, and a test that needs a binary fixture is `inconclusive`.

Layout: `mira-verify-XXXX/repo` (the checkout and every command's working
directory), `home/` (`$HOME`) and `tmp/` (`$TMPDIR`). `.venv/bin`, `venv/bin`
and `node_modules/.bin` inside the checkout come first on `PATH`, so a bare
`pytest` is the project's own once setup has installed it. Workspaces older
than six hours left by a process that died are swept when the next one is
created.

---

## Threat model

**The pull request's code is untrusted, and it runs.** That is the feature:
`pytest` imports the pull request's `conftest.py`, `npm ci` runs what the
lockfile installs, a test calls the changed function. Whoever opened the pull
request chose that code. Everything below follows from treating it as an
attacker's.

**It runs only in the sandbox.** Every command — setup, tools, tests, the
canary — goes through `mira.sandbox.run_argv` (no shell, an environment
allowlist, rlimits, a process-group kill on timeout) wrapped in an isolation
layer:

* **`bwrap`** (Linux, bubblewrap; what `auto` picks there). New mount, PID,
  IPC, UTS, user and network namespaces. The root contains `/usr`, `/bin`,
  `/lib*`, `/opt` read-only, a short list of `/etc` entries a toolchain needs
  (certificates, `resolv.conf`, `passwd`, the loader cache — not `/etc` as a
  whole, which may hold Mira's configuration), the virtualenv and interpreter
  a configured binary resolves into, read-only, and the workspace — the only
  writable path. Mira's data directory, database, GitHub App key, home
  directory and process environment are not in it.
* **`sandbox-exec`** (macOS; what `auto` picks there). No network, no writes
  outside the workspace, and the contents of the home directory unreadable
  except the workspace and the toolchain. The rest of the filesystem stays
  readable, so this mode is for running Mira locally, not for a deployment.
* **`unshare`** removes the network only — the filesystem is the host's — and
  **`none`** removes nothing. Both are used only when named, and logged as such.

`auto` probes each candidate once per process by running a trivial command
through it: a bubblewrap that is installed but denied user namespaces fails
the probe, not a review. **When no isolation works, nothing runs**: every
eligible finding is `inconclusive` with the reason, and the review is otherwise
unchanged. On Debian or Ubuntu, `apt-get install bubblewrap`. Inside a
container, bubblewrap needs user namespaces, which Docker's default seccomp
profile denies; run Mira on the host, or give the container a profile that
allows `clone`/`unshare` with `CLONE_NEWUSER`, rather than falling back to
`none`.

**Secrets never enter the workspace.** The environment is the sandbox's
allowlist (`PATH`, `LANG`, …) with `HOME`, `TMPDIR` and `PATH` pointed into
the workspace — no platform token, database URL or model key. The checkout is
written from the snapshot Mira already holds, so no credential is needed to
create it.

**No network after setup.** Setup may reach the network when `setup_network`
is on (the default — installing dependencies needs a registry). Every tool,
test and canary runs with no network namespace at all under `bwrap` and
`unshare`, and with `network*` denied under `sandbox-exec`.

**What comes back is data.** Tool output reaches the review model inside a
`<<<MIRA-UNTRUSTED-TOOL>>>` block after redaction, and the finding, the file and
the example test reach the test-writing model inside `FINDING` and `FILE`
blocks. Output shown on a comment is redacted and truncated, and the test and
output are in code fences, where a mention pings nobody. A tool diagnostic
posted as a finding has its `@` handles broken.

**Nothing from the pull request chooses what runs.** The commands, setup steps
and runners come from deployment configuration. A repository's own
`.mira.yaml` is part of what is being reviewed; if it could name a command,
the author of the pull request would choose it.

### What the pull request *can* do, and the guards

**Make every test pass.** "Not reproduced" is the outcome that costs a finding
its severity, and the harness is the pull request's: a `conftest.py` that
turns every failure into a pass would erase every finding on the change that
added it. Two guards:

* A **canary** — a test that must fail with `MIRA-CANARY`, written at a path
  shaped like a real reproduction's — runs once per runner before a pass is
  believed. A harness that reports it as passing makes every pass
  `inconclusive`. Python runners get a built-in canary; configure `canary` for
  others, or their passes are trusted on the second guard alone.
* A pull request that changes the harness itself — `conftest.py`,
  `pytest.ini`, `tox.ini`, `noxfile.py`, jest/vitest/karma/mocha/playwright
  configuration — gets no `not_reproduced` at all.

A harness written to recognise Mira's reproductions by their content (the
concatenated marker) and pass only those would defeat both. The damage is
bounded by design: one severity level, or a finding the critic already rated
only plausible.

**Make a test fail.** That can only turn a finding into `reproduced` on the
author's own pull request, which is against their interest.

**Exhaust resources.** Bounded per command by wall clock, CPU seconds,
address space, file size and process count, and per review by
`setup_timeout_seconds` and `budget_seconds`. A command still running when the
review ends keeps the workspace until its own timeout kills its process group;
the workspace is removed then, in the background.

---

## The audit trail and analytics

Each review's audit (`ReviewResult.audit`) gains:

* one `verify` entry per finding a test was attempted for — outcome, reason,
  runner, seconds, the critic's grade, the severity before a downgrade, and
  whether the finding was dropped;
* one `verify_summary` entry — the isolation used, why verification was
  unavailable if it was, each setup step's status, each tool's status and
  diagnostic count, and the outcome counts;
* a `drafted` entry with `chunk: tools` when tool diagnostics were posted.

The outcome is persisted on the finding (`review_findings.verification` and
`verification_detail`, added in place on SQLite and Postgres).
`IndexStore.finding_verification_stats()` groups findings by outcome
(`reproduced`, `not_reproduced`, `inconclusive`, `unverified`) with the same
positive/negative counts and `acceptance_rate` as rule analytics, so the
precision of verified findings can be read against unverified ones.
