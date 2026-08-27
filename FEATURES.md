# Mira Features

Mira is a self-hostable, fully open-source AI code reviewer. Everything below is included — no paid tier, no license key, no upsell prompts. The project is licensed under [Apache 2.0](LICENSE).

## Review engine

- AI-powered inline PR comments with severity and confidence scoring
- PR walkthroughs / summaries with file coverage, comment breakdown, and per-severity stats
- Streaming walkthrough: placeholder posts within ~1s, narrative within ~10s, full review within a minute
- Multi-file reasoning across a diff
- Parallel chunk review (`asyncio.gather` with configurable concurrency)
- Cross-chunk and cross-file deduplication (Jaccard similarity on titles + bodies)
- Cross-PR overlap detection: flags other open PRs touching the same code (merge-conflict risk) or pursuing the same goal (duplicate effort) in the walkthrough
- GitHub suggestion blocks with runtime fence sanitization
- Noise filtering: confidence thresholds, severity sorting, per-PR comment caps
- Per-language file-type support
- Confidence score auto-clamped to match findings (a blocker forces "Do not merge" regardless of the LLM's initial read)

## Codebase intelligence

- Full-repo code index with per-file summaries
- Dependency graph and relationships across files
- Cross-repo relationships and blast-radius analysis
- Blast-radius SVG rendering and interactive ReactFlow graph
- Relationship overrides and custom edges
- External reference tracking
- Manifest-based package extraction (`package.json`, `requirements.txt`, `pyproject.toml`, `go.mod`, `composer.json`, `Dockerfile`) with version constraints — zero LLM cost

## Security

- **Vulnerability scanning** via OSV.dev — hourly background poll across every package in every repo. Surfaces critical/high/moderate/low CVEs with advisory links and fix versions.
- **Org-wide package search** — answer "which repos use lodash@4.17.20?" instantly. Built for incident response.
- Per-repo CVE badges inline with package listings
- Dashboard "Security alerts" widget showing org-wide open vulnerabilities by severity

## Custom rules

- Per-repo custom rules with full CRUD (unlimited)
- Global rules that apply to every repo in the org
- Both inject into the review prompt automatically
- Rules UI at `/rules` in the dashboard

## Learning from feedback

- Stable finding provenance for replies, explicit rejects, and supported 👍/👎 reactions
- Reply intent is classified before anything is recorded: "you're wrong" and "good catch, fixed below" mean opposite things and are treated as such
- Say you fixed a finding and Mira rereads the code before closing the thread — a claim it cannot verify leaves the thread open
- Explainable learning candidates with rationale, confidence, and linked evidence
- Semantic candidate deduplication with conservative path/symbol/language/repo/org scopes
- Admin approval queue: synthesized candidates are inactive by default
- Rule comparison, editing, rejection, versioning, disabling, and YAML import/export in the dashboard
- Scope-aware retrieval with manual and specific rules taking precedence
- Feature flags for feedback capture, synthesis, and opt-in auto-application
- Rule evaluation analytics: exposures, decisions, and outcomes recorded per rule and finding
- 👍/👎, agree/disagree replies, addressed rate, and repeated false positives, all traceable to the events behind them
- Before/after activation comparison so you can see whether a rule improved reviews
- Regression detection that suggests a downgrade but never disables a rule on its own
- CSV/JSON export and an audit trail for suggestions, overrides, and admin changes

## Merge gate

- A conservative approval decision, in its own domain and kept apart from the review's quality score
- Deterministic, explainable risk score: never a bare number, always the named factors that add up to it
- Explicit states — `approved`, `would_approve`, `not_approved`, `skipped`, `error` — where only a platform-confirmed delivery is ever `approved`
- Shadow mode records exactly the decision it would have acted on, so false approvals are measurable before rollout
- Eligibility by base branch, labels, author and association, size, generated files, CI status, review completeness and index readiness
- Protected paths as an absolute veto, with gitignore-shaped patterns and no override that can force one
- Optional CODEOWNERS integration, read conservatively — an owned path is a reason not to approve, never a reason to approve on the owner's behalf
- An open blocker finding is never approved, whatever the score says
- Optional `REQUEST_CHANGES` on blockers, never submitted over an existing human review
- Off by default, plus a kill switch that beats every per-repository override
- Idempotent decisions and delivery claims: a redelivered webhook or a second worker cannot approve twice
- Per-provider capabilities that degrade explicitly instead of reporting an approval nobody received
- Auditable overrides recording actor, reason, previous and new decision — and never touching the platform
- Dashboard panel for the decision history and the policy

## Assisted correction

- `@mira fix` on a review comment and `@mira fix all` on the pull request, resolved by durable finding id rather than by path and line
- Off by default; `suggest` mode generates and validates the patch and writes nothing
- Requires write permission on the repository, read from the platform — an unreadable permission is never treated as one
- Default delivery is Mira's own branch and a stacked pull request carrying the diff, the rationale, the model and the validation transcript
- The default branch is never written to, nothing is ever force-pushed, and no provider declares that Mira may merge
- Committing onto the pull request's own branch is a separate opt-in, refused outright for a fork or a default-branch head
- Path safety, protected paths and file/line/byte limits enforced on the applied patch rather than on what the model sent
- Structured model output with no field that could carry a command, and repository content framed as untrusted data
- Secrets and personal data redacted before anything reaches a model, and a patch that would commit a credential is refused
- Validation from a deployment-configured allowlist only, with no shell, wall-clock timeouts and POSIX CPU/memory ceilings
- A durable in-database queue with leases, attempts, backoff and a dead-letter state — no Redis, no second service
- Worker runs in the web process by default and separately (`mira autofix-worker`) for larger installs
- Idempotent on retry: one branch, one commit per distinct content, one pull request
- Bounded CI retry loop driven by the fix's own build, counted on the job row so a restart cannot reset it
- Optional handoff to an external agent through a one-method adapter, with a zero-dependency built-in
- Global kill switch that also stops queued work, plus per-repository opt-in and an admin cancel permission
- Dashboard panel for jobs, patches, validations and failures

## Pre-merge checks

- Five explicit states per check — `pass`, `violation`, `infrastructure_error`, `skipped`, `timeout` — where only `violation` is ever a statement about the pull request
- A missing linter, an unreachable tracker and a model that would not answer are reported as Mira's problem, in those words, in their own section
- Fail closed regardless: a blocking check that could not answer refuses a gate approval just as a violation does, under its own reason code
- Every result carries a check id, a check version, a digest of the configuration it ran under, its duration and its evidence
- Evidence points at files, lines and quoted snippets — a violation with none is downgraded rather than recorded
- Native checks: title and description, documentation, tests, possible breaking change, and migration reversibility
- Ticket validation against GitHub/GitLab/Forgejo issues, with acceptance-criteria parsing and an adapter interface for any other tracker — no external service required
- CI status and failing-job output summarised with the job, the step, a link and the quoted lines, redacted and truncated before storage
- Natural-language checks per path glob, with the rule as policy in the system prompt and every quote verified against the code before it is recorded
- "Not sure" is an answer: an ambiguous model verdict becomes a skip, never an invented finding
- Deterministic analysers behind a closed allowlist — Semgrep, Ruff, ESLint, Gitleaks and OSV — with argument lists, no shell, version pinning and rlimits
- OSV reuses the review's existing scan rather than repeating it, so nothing about the current analysis changes
- Deduplication across producers: a problem found by an analyser and by a model appears once, with both evidences kept and both sources named
- Modes per check (`off`, `warning`, `error`) with three-layer inheritance: global, organisation, repository
- Per-check timeouts and a whole-run budget, with concurrency capped for the Orange Pi profile
- Off by default, plus a kill switch that beats every per-scope override
- Idempotent runs: a redelivered webhook converges on one row, and a retry that succeeds replaces the error it recorded
- Dashboard panel for runs, per-check history, coverage catalog, policy and a policy-change audit trail

## Platform integrations

- GitHub App with webhook support — works against github.com and GitHub Enterprise Server (set `MIRA_GITHUB_API_URL`)
- GitLab with full feature parity — merge-request reviews, `@mention` commands, thread auto-resolution, indexing — via a group or project access token (`MIRA_GITLAB_TOKEN`); self-managed instances via `MIRA_GITLAB_API_URL`
- Forgejo / Codeberg — pull-request reviews and `@mention` commands via an access token (`MIRA_FORGEJO_TOKEN`); self-hosted instances via `MIRA_FORGEJO_API_URL`
- One `mira serve` deployment reviews on any combination of platforms, each on its own webhook route
- Bot chat: mention the bot on any PR or MR to ask questions
- Cancel-in-progress indexing from the UI

## Bring your own LLM

- Any provider available through OpenRouter — Anthropic, OpenAI, Google Gemini, DeepSeek, and more — so you pay your provider directly with no Mira markup
- Any OpenAI-compatible endpoint via `llm.base_url` — vLLM, Ollama, LiteLLM proxy, LocalAI, llama.cpp, Together, Fireworks, Groq
- AWS Bedrock as a direct backend (Converse API, standard AWS credential chain)
- Separate model configuration for indexing (cheap) vs review (powerful)
- Fallback-model chain
- Adjustable review thinking mode (`llm.review_reasoning_effort`) for models with extended reasoning

## Dashboard and analytics

- Org-level stats: total reviews, comments, tokens, per-severity counts
- Period-based time-series (daily / weekly / monthly) with bar and line charts
- Issue-severity stacked breakdown per period
- Issue-category breakdown per period
- Per-repo views: files indexed, dependencies, blast radius, packages, last-indexed timestamp
- Indexing status dashboard with cost estimates
- Review event stream
- Threaded PR activity timeline: each review pass with its comments and the human replies nested under them
- Review page (admin): stale/waiting PRs, reviewer-responsiveness leaderboard, throughput trends, rubber-stamp detection, open-PR status board
- Contributor analytics: authoring stats, year-long contribution heatmap, and Mira's review-quality signal per contributor
- Pending-uninstall review queue

## Configuration

- Repo-level `.mira.yaml` configuration file
- Per-repo context entries (architecture docs, coding guidelines)
- Confidence thresholds (global and per-category), severity thresholds, comment caps
- Exclude patterns and per-language overrides
- PR author allow/deny lists (`filter.allowed_authors` / `filter.blocked_authors`) for muting bots or scoping auto-review
- Cross-PR overlap tuning (`review.overlap`): candidate cap, confidence floor, title-similarity threshold
- Opt-in ensemble mode (`review.ensemble_runs`): review each chunk N times and keep majority-vote findings
- Thread auto-resolution toggle (`review.auto_resolve_conversations`)
- `context_lines`, `max_concurrent_chunks`, and `index.max_file_size` tuning knobs
- Merge gate policy (`gate`): mode, kill switch, protected paths, eligibility limits, risk threshold, and per-repository overrides
- Autofix policy (`autofix`): mode, kill switch, requester permissions, protected paths, size/attempt limits, the validation command allowlist, queue tuning, and per-repository overrides
- Pre-merge check policy (`checks`): per-check modes, kill switch, budgets and concurrency, the analyser allowlist, natural-language rules, ticket and CI settings, and per-organisation and per-repository overrides

## Storage and deployment

- SQLite backend (default, zero-config)
- PostgreSQL backend for horizontal scale
- Single-image Docker deployment
- Reference deploy configs: Railway, Fly.io, Render
- Self-hostable on any platform that runs Docker — no phone-home, no required telemetry

## Admin and setup

- Setup wizard for first-run GitHub App configuration
- Admin user management
- Model-selection UI for indexing and review models
- Background OSV vulnerability poller, indexing backfill, and webhook-driven re-indexing
