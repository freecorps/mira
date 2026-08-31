<p align="center">
  <img src=".github/assets/logo.png" alt="Mira logo" width="120" />
</p>

<h1 align="center">Mira</h1>

<p align="center">
  <strong>Self-hosted AI code review. Your code, your dashboard, your LLM key.</strong>
</p>

<p align="center">
  <a href="https://docs.miracode.ai"><img src="https://img.shields.io/badge/Docs-docs.miracode.ai-orange?style=flat&logo=readthedocs&logoColor=white" alt="Documentation" /></a>
  <a href="https://discord.gg/uEU6qvYhgm"><img src="https://img.shields.io/badge/Discord-Join-5865F2?style=flat&logo=discord&logoColor=white" alt="Join our Discord" /></a>
</p>

<p align="center">
  <a href="https://docs.miracode.ai">Docs</a> ·
  <a href="https://discord.gg/uEU6qvYhgm">Community</a> ·
  <a href="https://docs.miracode.ai/deployment"><strong>Self-Host Guide »</strong></a> ·
  <a href="#benchmark">Benchmark</a>
</p>

Self-host every feature: full review engine, codebase indexing, vulnerability scanning, custom rules, org-wide package search, dashboard, learning loop. No paid tier, no license key, no SaaS upsell.

Mira reviews your pull requests using your choice of LLM (via [OpenRouter](https://openrouter.ai), which fronts Anthropic, OpenAI, Google, DeepSeek, and more) and posts concise, actionable feedback. The noise filter, confidence clamping, and learning loop ensure you only see comments that matter. See [`FEATURES.md`](FEATURES.md) for the full surface.

## Why Teams Choose Mira

- **Model agnostic** — Run Claude, GPT, Gemini, DeepSeek, Llama, or any OpenAI-compatible endpoint: OpenRouter, vLLM, Ollama, Together, Groq, Fireworks, or AWS Bedrock direct. Per-provider quirks are config, not code, so adding a provider is a one-line entry.
- **Zero markup on LLM costs** — Bring your own key. You pay the model provider directly; Mira never proxies your spend or adds a multiplier. The dashboard shows real per-repo, per-model cost — not estimates.
- **Learns from your context** — Mira synthesizes rules from your merged PRs: rejected comments and human review patterns become team rules that shape future reviews.
- **You set the rules** — Define custom and org-wide review rules in plain language, per-repo via `.mira.yaml` or from the dashboard.
- **Privacy first** — Self-hosted by default. Diffs, indexes, review history, and CVE data live in your SQLite or Postgres, on infra you own. No phone-home, no required telemetry, no "is this used for training?"
- **Low-noise reviews** — Confidence thresholds, dedup, a self-critique pass, and per-PR caps mean every comment is one worth reading — and Mira is the fastest tool on the public [Code Review Bench](#benchmark).
- **Catches PRs stepping on each other** — While reviewing, Mira checks the repo's other open PRs and flags merge-conflict risk and duplicate effort right in the walkthrough.
- **Indexed, cross-file context** — A full-repo code index gives the model real project context, not just the diff — plus org-wide package search and hourly OSV.dev CVE scanning across every repo.
- **GitHub, GitLab, and Forgejo** — Auto-reviews every PR and merge request and answers `@miracodeai` questions inline, with full feature parity across GitHub, GitLab, and Forgejo (incl. Codeberg). A Bitbucket adapter is next; the engine, indexer, and dashboard are provider-agnostic, so a new host is a data entry plus one provider class.
- **Self-host on day one** — Docker image with Railway / Fly.io / Render configs, SQLite or Postgres. Every feature included.

## Dashboard

![Mira dashboard](.github/assets/Dashboard.png)

## Your data, your dashboard

Most AI reviewers are SaaS: your diffs (and often the full surrounding code) leave for a third-party server, and the only "view" you get is the comments that come back on a PR. Mira flips both halves of that:

- **Your code never leaves your infra.** Diffs, embeddings, indexes, review history, vulnerability data, all stored in your SQLite or Postgres, on infrastructure you own. No phone-home, no required telemetry, no "is this used for training?" question.
- **The dashboard you see above is yours.** It's not a marketing screenshot of someone else's view of your code. CodeRabbit, Greptile, and similar SaaS reviewers don't expose anything like it. Mira's dashboard surfaces signals you don't get anywhere else:
  - **Org-wide package inventory**: answer "which repos use `lodash@4.17.20`?" in one query. Stack it next to your CVE feed for instant blast-radius checks.
  - **CVE alerts on every dependency**: hourly OSV.dev poll, severity + advisory link + fix version surfaced inline next to the package.
  - **Dependency + blast-radius graphs**: see exactly which files and repos depend on a symbol before you change it.
  - **Per-repo review event stream**: every webhook, every chunk, every cost figure, in one place for live troubleshooting.
  - **Cost & token telemetry**: actual spend per repo and per model, not estimates, because you control the LLM key.
  - **Review-health page**: stale/waiting PRs, a reviewer-responsiveness leaderboard, throughput trends, and rubber-stamp detection (approvals with no substantive review) — plus per-contributor analytics with a year-long heatmap and Mira's review-quality signal.
  - **Coming soon, change-frequency heatmaps**: surface the files that bug fixes keep landing on so you can target review attention.

If your engineering team needs answers like *"which of our repos are exposed to this CVE?"* or *"what's the blast radius of changing this function?"*, those questions stop being multi-day investigations and start being one-click dashboard pages.

## Benchmark

Mira is **the fastest tool measured** on the public [Code Review Bench](https://codereview.withmartian.com/?mode=offline), and the only one on the speed/quality Pareto frontier: every tool that scores higher on F1 takes **5–14× longer per PR**.

![Median review time per PR, Mira vs every published competitor](.github/assets/benchmark-frontier.svg)

Plotted against every published competitor on the same subset, Mira sits in the upper-left corner: everything to the right is slower; everything above it pays 5–14× the wall time for the extra F1.

![Speed vs quality: Mira on the Pareto frontier](.github/assets/benchmark-by-language.svg)

Measured on the same 50-PR offline benchmark, judged by Claude Sonnet 4.6.

| | **Mira** | Cubic-v2 | Greptile | CodeRabbit | GitHub Copilot |
|---|---:|---:|---:|---:|---:|
| F1 | **44** | 56 | 35 | 32 | 31 |
| Precision | **43%** | 50% | 32% | 24% | 24% |
| Recall | **46%** | 65% | 40% | 50% | 43% |
| Median time / PR | **~77s** | ~9m | ~5m | ~5m | ~10m |

> Methodology: scores measured against the [Martian Code Review Bench](https://codereview.withmartian.com/?mode=offline) offline dataset with Claude Sonnet 4.6 as the judge.

## Quick Start

Run Mira self-hosted to auto-review every PR and merge request and answer `@miracodeai` questions inline. GitHub (as a GitHub App), GitLab (via a group/project access token), and Forgejo/Codeberg (via an access token) are all fully supported; Bitbucket is next.

**1. Deploy** — one-click on Railway, or with Docker:

[![Deploy on Railway](https://railway.com/button.svg)](https://railway.com/workspace/templates/05874bad-2d98-43f4-aa93-332f394e9ebd)

```yaml
# mira.yaml — deployment-wide defaults. Every key is optional.
llm:
  model: "anthropic/claude-sonnet-4-6"
  indexing_model: "anthropic/claude-haiku-4-5"
```

```bash
# .env — secrets only.
MIRA_GITHUB_APP_ID=123456
MIRA_GITHUB_PRIVATE_KEY="$(cat private-key.pem)"
MIRA_WEBHOOK_SECRET=your-secret
OPENROUTER_API_KEY=sk-or-...
```

```bash
docker run -p 8000:8000 --env-file .env \
  -v "$(pwd)/mira.yaml:/app/mira.yaml" \
  ghcr.io/miracodeai/mira:latest --config /app/mira.yaml
```

**2. Install the app** on your repos — every PR gets reviewed.

→ Full walkthrough: [creating the GitHub App & quickstart](https://docs.miracode.ai/quickstart) · [GitLab setup](https://docs.miracode.ai/gitlab) · [deploy options](https://docs.miracode.ai/deployment) · [choosing models, custom endpoints & AWS Bedrock](https://docs.miracode.ai/configuration/models)

## Review before you push

The same review, against a change that is not a pull request yet:

```bash
pip install mira-reviewer

mira local review                      # everything uncommitted
mira local review --staged             # what a commit would contain
mira local review --range main...HEAD  # what a pull request would show
```

Read-only, needs no forge credentials, and refuses to send your code to any
model endpoint other than the one your `.mira.yaml` configures. Exit codes are
documented for CI (`--explain-exit-codes`) and `--output json` is stable.

→ [docs/local-cli.md](docs/local-cli.md)

## Give your agent Mira's memory

An agent can already read your code. What it cannot see is Mira's history with
it — which findings were raised and whether they held up, which conventions a
human approved as rules, and how those rules have performed since.

```bash
mira mcp serve --repo acme/widgets
```

A read-only MCP server over stdio: seven tools, all reads, no writes, no
approvals, no command execution. Off by default, and when it is on it reads
only the repositories your configuration names — an enabled server with an
empty list refuses everything. Every answer is redacted and framed as data, and
every call, answered or refused, is written to an audit trail.

→ [docs/mcp.md](docs/mcp.md)

## Configuration

`mira.yaml` (loaded via `--config`) holds deployment-wide defaults. Drop a `.mira.yaml` in any repo — or use the dashboard — to override per-repo; both deep-merge over `mira.yaml` for that repo only:

```yaml
# .mira.yaml — optional per-repo override
filter:
  confidence_threshold: 0.5  # noisier repo → lower bar
  max_comments: 10

learning:
  feedback_v2: true
  learning_synthesis: true
  learning_auto_apply: false  # keep human approval in the loop
  evaluation_analytics: true  # record which rules ran and what came of it
  min_exposures_for_regression: 20  # exposures needed before flagging a rule
  min_decisive_for_regression: 5    # and this many people actually responding
```

Set `MIRA_DASHBOARD_URL` to the externally reachable dashboard base URL (for
example, `https://mira.example.com`) so feedback acknowledgements can link
directly to their candidate and evidence.

Rule analytics live at `/learnings/analytics` (admin only). Setting
`learning.evaluation_analytics: false` stops the recording entirely; reviews
behave identically either way, since exposures are written only after the
review has already been posted.

Every review publishes a **`mira/review` check** on the head commit — pending
while it works, then green, red or neutral — and **approves** pull requests it
read clean:

```yaml
review:
  verdict:
    mode: "approve"             # off | approve | request_changes
    approve_max_severity: "suggestion"
    approve_min_confidence: 4   # the walkthrough's own 1–5 merge-readiness score
  status:
    fail_on: "blocker"          # never | blocker | above_ceiling
```

Two independent conditions gate an approval: nothing was found above the
severity ceiling, and Mira rated its own read of the change at least 4/5. It
never approves over a human who requested changes, its own pull request, or a
diff it only partly read. `request_changes` stays opt-in — an approval adds a
signal you can dismiss, a rejection takes the merge button away. Mira's own
failures show as a *neutral* check naming the failure, never red.

→ [Review status and approvals](docs/review-status.md)

The **merge gate** is a separate, conservative approval decision — the review
verdict says whether the code is good, the gate says whether Mira may put its
name on merging it. It ships off. Turn it on in shadow first, which records the
decision it *would* have acted on so you can measure false approvals before
letting it act:

```yaml
gate:
  mode: "shadow"          # off | shadow | enforce
  risk_threshold: 25
  codeowners: "block"     # an owned path is never auto-approved
```

Gate policy is deployment configuration and nothing in a pull request can
change it. A protected path, an open blocker, a pending CI run or an unreadable
input never results in an approval. The history and policy panels live at
`/merge-gate` (admin only).

→ [Merge gate docs](docs/merge-gate.md)

**Autofix** closes the loop. Reply `@mira fix` to one of Mira's review comments
and it writes the change on a branch of its own and opens a stacked pull
request with the diff, the rationale and the validation it passed:

```yaml
autofix:
  mode: "suggest"         # off | suggest | on
  max_files: 3
  max_fixes_per_request: 3
```

It ships off, and `suggest` shows you the patch it would have written without
writing anything. Asking requires **write permission on the repository**, read
from the platform — not a dashboard login. Mira never writes to the default
branch, never force pushes, and never merges. Jobs, patches and validation
transcripts live at `/autofix` (admin only).

→ [Autofix docs](docs/autofix.md)

**Pre-merge checks** cover what the diff alone does not prove: whether the pull
request says what it does, whether tests and docs moved with the code, whether
a migration can be undone, whether the linked issue exists, and what CI
actually printed when it went red. Each check reports its own state, its own
duration and the file and line it is talking about:

```yaml
checks:
  enabled: true
  default_mode: "warning"   # off | warning | error
  modes:
    native.tests: "error"
```

The distinction the whole framework is built around: **a violation is a
statement about your pull request, and everything else is a statement about
Mira.** A linter that is not installed, a model that timed out and a tracker
that could not be reached are reported as exactly that — never as a problem
with your change — and the merge gate still refuses to approve on any of them,
because not knowing is not permission. Deterministic analysers (Semgrep, Ruff,
ESLint, Gitleaks, OSV) come from a closed allowlist, and a finding two of them
share appears once with both sources named. Runs, evidence and policy history
live at `/checks` (admin only).

→ [Pre-merge check docs](docs/pre-merge-checks.md)

**Reviewer triage** answers the other question a fresh pull request raises: who
should look at this. Mira classifies the change from the diff alone — size,
areas, whether it carries tests or a migration — and ranks who is closest to the
files, from what the repository *declares* in CODEOWNERS and what Mira has
*observed* about who writes and reviews them:

```yaml
triage:
  enabled: true
  max_suggestions: 3
  exclude: [somebody-who-asked-not-to-be]
```

Every name carries the evidence that produced it: the CODEOWNERS line, the
commit, the pull request somebody reviewed. **Nothing is assigned, requested or
mentioned** — the suggestion is text a human reads, and asking a colleague to
review stays something a person does. CODEOWNERS is read at the pull request's
**base** commit, so a branch cannot add itself an owner and be ranked under it,
and a lookup that fails is reported as Mira's failure rather than as "nobody is
available". Runs, evidence and policy live at `/triage` (admin only).

→ [Reviewer triage docs](docs/triage.md)

→ Full schema and every key: [Configuration docs](https://docs.miracode.ai/configuration).

## Development

```bash
git clone https://github.com/miracodeai/mira.git
cd mira
uv sync --locked --extra dev --extra serve --extra bedrock

# Run tests
uv run pytest tests/ -v

# Run the regression suite (hits real GitHub + LLM, ~$1, ~3 min).
# Pinned PRs whose findings have flickered across iterations. Run before
# merging changes that touch prompts, the noise filter, or the engine.
OPENROUTER_API_KEY=... GITHUB_TOKEN=... uv run pytest -m eval -v

# Lint, format, and repository hygiene
uv run pre-commit run --all-files

# Advisory type check (the current upstream baseline is not yet clean)
uv run pre-commit run --all-files --hook-stage manual mypy
```

Supported runtimes are Python 3.11 and 3.12 on Linux; local development is
pinned to 3.12 by `.python-version`.

## License

Apache 2.0. See [LICENSE](LICENSE).
