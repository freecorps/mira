# Digests and release notes

Mira can tell a team what landed on a repository's default branch: a digest of
the merged pull requests and direct commits in a period, grouped by area of the
codebase and summarised per area, delivered on a schedule to Slack, Discord,
Teams, a generic webhook or email. The same pipeline writes release notes for
the pull requests between two tags.

Everything here only reads. Nothing writes to a repository.

```yaml
digests:
  enabled: false          # the scheduler; the CLI and API work regardless
  schedule: "weekly"      # "weekly" | "daily"
  day: "monday"           # weekly only
  hour: 9                 # UTC
  scope: "repo"           # "repo" | "org"
  repositories: []        # [platform:]owner/repo; empty = every registered repo
  areas: {}               # name -> path patterns
  area_depth: 1
  include_direct_commits: true
  max_pull_requests: 100
  max_commits: 200
  max_files_per_change: 100
  max_input_chars: 40000
  use_llm: true
  skip_empty: true
  email:
    recipients: []
```

Like the gate, `digests` is deployment configuration: set it in the server's
`mira.yaml` or the dashboard settings override, not in a reviewed repository.

## What a digest contains

For each repository Mira asks the platform for:

- **Merged pull requests** into the default branch whose merge time falls in
  the period, with their labels and changed files (GitHub pull requests, GitLab
  merge requests, Forgejo pull requests).
- **Direct commits**: commits on the branch's first-parent chain that no pull
  request in the period accounts for. Walking the first parent separates a
  commit pushed straight to the branch from the commits a merge brought in. A
  squash merge is recognised by its merge commit or by the `(#123)` in its
  title; a rebase merge, whose commits all land on the chain, by their commit
  time sitting within a minute of the merge. Turn this off with
  `include_direct_commits: false`.

Each change is then placed in **areas** by the files it touched. An area named
in `digests.areas` claims the paths its patterns match; any other path falls in
an area named after its first `area_depth` directories (`src/`, or `src/api/`
with depth 2), and files at the top of the repository fall in
`(repository root)`. A change that touched three areas is listed under all
three. Patterns use the merge gate's gitignore-shaped syntax:

```yaml
digests:
  areas:
    API: ["src/api/**", "openapi.yaml"]
    Frontend: ["ui/**"]
    Docs: ["docs/**", "*.md"]
```

At most 20 areas are shown; the smallest ones past that are folded into
*Other areas*. Where the repository is indexed, the index's directory summary
for a path-named area is given to the model as context.

One structured call on the **indexing** model writes an overview of the period
and, per area, a one-to-three sentence summary and up to three highlights.
With `use_llm: false`, or when the model call fails, the digest still lists
every change per area with a counted overview.

`scope: org` builds one digest per owner instead of one per repository, with
each area prefixed by its repository (`api: src/`). A repository in the group
that cannot be read becomes a note rather than failing the digest.

### Bounds

Every list is capped: `max_pull_requests` per repository (the most recently
merged), `max_commits` checked for direct pushes, `max_files_per_change` paths
per change, 40 direct commits whose files are looked up individually, and
`max_input_chars` of pull request text quoted to the model (each description
trimmed to 600 characters, and quoted once even when the change is in several
areas). Whatever a cap cuts is said in the digest's notes, so a short digest is
never mistaken for a quiet week.

### Untrusted text

Pull request titles, descriptions and commit messages are written by
contributors. They reach the model inside `<<<MIRA-UNTRUSTED-CHANGES>>>`
blocks that cannot be closed from inside, after secret redaction, under a
system prompt that says the content is data and never instructions. What comes
back is matched to the areas Mira sent (anything else is dropped) and cleaned:
no links, no HTML, no mentions, bounded length. Titles in the Markdown are
escaped and have their `@mentions` neutralised, and Discord deliveries disable
mentions outright, so a pull request titled `@channel` pings nobody.

## Schedule and delivery

`mira serve` runs a small loop that checks every five minutes whether a
schedule boundary has passed — every `day` at `hour` UTC for `weekly`, every
day at `hour` for `daily`. The digest for a boundary covers the period that
ends there (a Monday 09:00 digest covers the previous Monday 09:00 up to that
moment). The last boundary run is stored in the database and claimed with a
compare-and-set before any work starts, so a restart neither repeats nor skips
a period and two server processes cannot both deliver it. A period is
delivered at most once: a run that dies half way leaves the rest of that
period undelivered rather than sending it twice. When digests are first
enabled, the most recent completed period is delivered at the next tick.

Each digest is stored (the dashboard's **Digests** page lists them) and sent:

- to every outbound webhook subscribed to the **Digest ready** event
  (`digest.ready`), configured under **Settings → Webhooks**. Slack and Teams
  get a formatted message, a Discord webhook URL
  (`https://discord.com/api/webhooks/…`) an embed, and any other URL the
  generic JSON envelope with the overview, counts and per-area summaries.
- by email, when `digests.email.recipients` is set and `MIRA_SMTP_HOST` is in
  the server's environment. `MIRA_SMTP_PORT` (default 587; 465 uses implicit
  TLS), `MIRA_SMTP_USER` / `MIRA_SMTP_PASSWORD`, `MIRA_SMTP_FROM` and
  `MIRA_SMTP_STARTTLS=false` complete it. The message is the digest's Markdown
  as plain text.

`skip_empty` (the default) neither stores nor sends a digest for a period in
which nothing landed. Delivery is best effort, like every outbound webhook: a
failing endpoint is a log line, and storing and sending are independent.

The scheduler reads with the credentials `mira serve` already has: the GitHub
App installation that owns each repository (or `GITHUB_TOKEN`), and the GitLab
and Forgejo tokens.

## On demand

```bash
# The last 7 days of a repository, as Markdown
mira digest --repo acme/api --token "$GITHUB_TOKEN"

# A fixed window, JSON, without a model call
mira digest --repo acme/api --since 2026-09-01 --until 2026-10-01 --no-llm --output json

# GitLab, and store + deliver it like a scheduled one
mira digest --platform gitlab --repo group/sub/project --token "$GITLAB_TOKEN" --deliver
```

The dashboard's **Digests** page lets an admin generate one for a repository
and a trailing period (stored, not delivered). The API:

| Route | Who | |
|---|---|---|
| `GET /api/digests` | any signed-in user or API token | list, newest period first; `owner`, `repo`, `platform`, `limit`, `offset` filter |
| `GET /api/digests/{id}` | any signed-in user or API token | one digest: `markdown` and structured `data` |
| `POST /api/digests/generate` | admin | `{platform, owner, repo, days, deliver}` |

## Release notes

```bash
# Between two tags (the --to default is the default branch)
mira release-notes --repo acme/api --from v1.4.0 --to v1.5.0 --token "$GITHUB_TOKEN"

# Everything merged since a date
mira release-notes --repo acme/api --since 2026-09-01 --no-llm
```

Between two refs, Mira asks the platform's compare for the commits in
`from..to`, then takes the pull requests merged in the span of those commits'
dates whose merge commit is among them — so a pull request merged into another
branch in the same week is not included. Commits in the range no pull request
accounts for are listed as direct commits. A platform that reports no merge
commit for any pull request falls back to the date span, and the notes say so.

Entries are sorted into **Breaking changes**, **Features**, **Fixes**,
**Dependencies** and **Other**, deterministically first:

1. labels, as configured under `digests.release_notes` (`bug` → Fixes,
   `enhancement` → Features, `breaking-change` → Breaking, `dependencies` →
   Dependencies; `skip-changelog` leaves the pull request out);
2. a conventional-commit title: `feat:` → Features, `fix:`/`perf:` → Fixes,
   `feat!:` or a `BREAKING CHANGE:` footer → Breaking, `chore(deps):` →
   Dependencies, `docs:`/`ci:`/`refactor:`… → Other;
3. a dependency bot's title (`Bump x from 1 to 2`, `Update dependency y to v3`).

Only what none of those place is offered to the model, which may move it into
a section — never move an entry the rules placed, and never add one — and
writes a two-to-four sentence narrative for the top. Without a model those
entries go to *Other* and there is no narrative.

Admins (and an admin's read-only API token, so a release pipeline can fetch
them) can get the same from `GET /api/release-notes?owner=&repo=&platform=&from_ref=&to_ref=`
or `&since=YYYY-MM-DD`, which returns the Markdown and the structured sections.

## Providers

Four read-only provider methods back all of this, implemented for GitHub,
GitLab and Forgejo: `list_landed_pull_requests`, `list_commits`,
`compare_commits` and `get_commit_files`. Their defaults raise rather than
return an empty list, because "nothing landed" is a statement a digest makes
in public.
