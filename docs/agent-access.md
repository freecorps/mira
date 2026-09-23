# Agent access: API tokens, the REST API and MCP over HTTP

Point an agent — Claude Code, Cursor, a script, `curl` — at a running Mira and
let it read what Mira knows: findings, rules, review passes, indexed context,
and, for an admin, Mira's own log trail. Read-only, by construction.

```bash
mira token create --user admin --name claude-code
claude mcp add --transport http mira https://mira.example.com/mcp \
  --header "Authorization: Bearer mira_pat_..."
```

The agent can now answer "why did the review on #412 fail?" by following the
trace ID from the failure notice to every line that review logged, without
anybody opening a terminal on the host.

---

## Two ways in, one credential

| | Reaches | Best for |
|---|---|---|
| **MCP** at `POST /mcp` | Ten typed tools with schemas, paging and framing built for a model | Any MCP client: Claude Code, Claude Desktop, Cursor, … |
| **REST** at `/api/*` | Every `GET` the dashboard itself uses; schema at `/openapi.json` | Shell agents, scripts, `curl`, anything that is not an MCP client |

Both take the same token in an `Authorization: Bearer` header. There is no CLI
client: an agent with a shell already has `curl`, and the OpenAPI document tells
it what to call.

The stdio server (`mira mcp serve`) is unchanged and still the right choice
when the agent runs where Mira's storage is — see [mcp.md](mcp.md).

---

## Tokens

A token belongs to one dashboard user and reaches what that user can read in
the dashboard, with two cuts that hold whatever the user is allowed to do:

- **It only reads.** A request carrying a token is refused before any route
  runs unless it is a `GET` or `HEAD`. The one exception is `/mcp`, a `POST` by
  protocol and read-only by inventory.
- **It cannot manage credentials.** Every `/api/auth` route except `me` refuses
  a token, and so does the OAuth callback. A leaked token is a leak of what it
  can read; it is not a way to mint the next one.

An **admin's** token additionally reaches the log trail (`/api/logs` and the
two log tools), exactly as the dashboard's Logs page is admin-only. For an
agent that only needs findings and reviews, create a dedicated non-admin user
and mint the token for it.

Only a SHA-256 digest of the token is stored. The token is shown once, when it
is created; lose it and mint another. The `mira_pat_` prefix is recognised by
Mira's redaction filter, so a token pasted into something that ends up in a log
line or an MCP response comes back out as `[REDACTED:mira-token]`.

### Creating one

**Dashboard → Settings → API tokens**: name, expiry (30 days to never) and, for
an admin, which user it acts as. The page shows the token once, with the
`claude mcp add` and `curl` lines already filled in.

**CLI**, on the host that holds the database — useful before anybody has signed
in, or from a script. The token alone goes to stdout:

```bash
mira token create --user admin --name claude-code --expires-days 90
mira token list
mira token revoke 3
```

On Railway, Fly or Render, run it inside the service (`railway run`,
`fly ssh console`, …) so it sees the same `DATABASE_URL`.

Revoking takes effect on the next request. A revoked or expired token stays in
the list, marked, so "which token was that?" still has an answer; deleting a
user deletes their tokens.

---

## MCP over HTTP

The same server as `mira mcp serve`, on the same port as the dashboard. Same
tools, same redaction and framing, same audit trail. What differs is where the
grant comes from:

- **Repositories: every one Mira has registered.** The dashboard shows all of
  them to every user and a token is a read-only dashboard login, so narrowing
  here would be decorative. `mcp.repositories` is the stdio server's ceiling,
  not this one's.
- **Logs: an admin's token only.** A non-admin session is not offered
  `mira_search_logs` or `mira_get_trace`, and is refused if it calls one.

The transport is the stateless subset of Streamable HTTP: one JSON-RPC message
per `POST`, answered with JSON; `GET` and `DELETE` answer `405`. The endpoint
refuses a session cookie — only a token opens it — and a request whose `Origin`
is not the dashboard's own.

### Wiring it to a client

```bash
# Claude Code
claude mcp add --transport http mira https://mira.example.com/mcp \
  --header "Authorization: Bearer $MIRA_TOKEN"
```

```json
{
  "mcpServers": {
    "mira": {
      "type": "http",
      "url": "https://mira.example.com/mcp",
      "headers": { "Authorization": "Bearer ${MIRA_TOKEN}" }
    }
  }
}
```

### The tools

| Tool | Answers | Who |
|---|---|---|
| `mira_list_repositories` | Which repositories this session can read | everyone |
| `mira_list_findings` / `mira_get_finding` | Findings, filterable; one in full with its feedback | everyone |
| `mira_list_rules` / `mira_list_evaluations` | Approved rules, and how each performed | everyone |
| `mira_list_indexed_files` / `mira_get_indexed_file` | The index's summaries, symbols and dependents | everyone |
| `mira_list_reviews` | Review passes on a repository: files, lines, what was posted, tokens and time | everyone |
| `mira_search_logs` | The log trail, newest first, by level floor, logger, text, trace ID, repository, window | admin |
| `mira_get_trace` | Every line one review logged, oldest first | admin |

`mira_search_logs` and `mira_get_trace` page through a trail that is still being
written. The first page pins the moment it was read, and the cursor carries it,
so a line written between two calls neither repeats a row nor pushes one off the
page. Every answer about logs carries the capture state, so "nothing matched"
and "capture is off" do not look the same.

### The audit trail

Every call over HTTP lands in the same trail as stdio, grouped by credential —
`session_id` is `token-<id>` and `client` names the token and its user:

```bash
mira mcp audit --limit 20
```

---

## REST

```bash
curl -H "Authorization: Bearer $MIRA_TOKEN" https://mira.example.com/api/auth/me
curl -H "Authorization: Bearer $MIRA_TOKEN" "https://mira.example.com/api/activity?repo=acme/widgets"
curl -H "Authorization: Bearer $MIRA_TOKEN" "https://mira.example.com/api/logs?trace_id=9f2c41ab7d3e5106&hours=0"
```

The schema is at `/openapi.json` (public, like `/docs`). Anything that is not a
`GET` answers `403` with a token, whatever the route.

Unlike MCP, REST responses are the dashboard's own JSON: not framed as
untrusted data and not truncated. An agent reading them should treat finding
bodies, PR titles and log messages as data, not instructions.

---

## Settings

| Key | Default | What it does |
|---|---|---|
| `mcp.http_enabled` | `true` | The `/mcp` endpoint. Off answers `404`; the REST API is unaffected |
| `mcp.max_page_size`, `mcp.max_text_chars`, `mcp.max_response_bytes`, `mcp.audit` | see [mcp.md](mcp.md) | Shared with stdio |

`mcp.enabled` and `mcp.repositories` govern the stdio server only.

`/mcp` defaults on where the stdio server defaults off because it widens no
one's access: it needs a token, and it reaches nothing that token cannot
already read from the REST API.

---

## What this is not

- **Not a write surface.** No token can approve, dismiss, trigger a review,
  change a setting or mint a token. Those need a signed-in person.
- **Not narrower than the user.** A token is scoped by its user, not by
  repository. For a narrower grant, run the stdio server with `--repo`.
- **Not a replacement for the session cookie.** The dashboard keeps using its
  login; tokens are for programs.
