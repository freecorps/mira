# Signing in instead of paying per token

Mira normally talks to an LLM with an API key you provide. It can also **sign
in to an account** and review with the models that account already includes —
today that means a ChatGPT plan, through the same backend the Codex CLI uses.

No API key, no per-token bill, and nothing to rotate: the session is stored in
Mira's database and renewed automatically before it expires.

The layer is generic. A provider is a spec class in `mira/oauth/`, so adding
another one later is a file, not a refactor — see [Adding a
provider](#adding-a-provider).

---

## Connecting from the dashboard

**Settings → Connections**, as an admin.

1. **Connect** opens the provider's sign-in page in a new tab.
2. Approve the request.
3. You land on a `http://localhost:1455/...` page that **cannot load**. That is
   expected — see [why](#why-you-have-to-paste-a-url). Copy that URL out of the
   address bar.
4. Paste it into the dialog and press **Finish sign-in**.
5. Press **Use for reviews**.

The card then shows the account, its plan, and when the session renews. Pick
which of that account's models to use under **Settings → Models** — the model
dropdown switches to the connected provider's list automatically.

To go back to the API-key path, press **Stop using** (which keeps the session)
or **Disconnect** (which forgets it). Disconnecting the active provider also
stops routing reviews at it, rather than leaving a pointer at a session that no
longer exists.

## Connecting from the CLI

If Mira runs on your own machine, the CLI can catch the redirect itself:

```bash
mira auth login chatgpt     # opens a browser, listens on localhost:1455
mira auth use chatgpt       # route reviews through it
mira auth status            # who is connected, and until when
mira auth logout chatgpt
```

Away from the machine with the browser, use the same paste flow as the
dashboard:

```bash
mira auth login chatgpt --manual
```

The CLI and the dashboard read and write the same store, so a login done in
either place is the login the other one uses.

## Configuring it in `mira.yaml`

The dashboard is the easy path, but the choice is also a config field — useful
for a CLI-only install or an image that ships pre-configured:

```yaml
llm:
  oauth_provider: "chatgpt"
  model: "gpt-5-codex"
```

You still have to connect the account once; this only says *which* session to
use. When set, `oauth_provider` wins over `provider`, `base_url` and
`api_key_env` — the endpoint and the auth both come from the provider spec.
The dashboard's Connections page writes the same setting, and its choice takes
precedence over the file.

An unknown id is rejected at config load rather than at review time: without
that check, a typo silently resolves to "no session" and quietly puts every
review back on the API key.

If no model is chosen anywhere, the provider's default is used. Mira's built-in
default model id is an OpenRouter-style Claude id that this endpoint cannot
serve, so inheriting it would turn "connect ChatGPT" into a 400 on the next
pull request. A model you *did* choose is always sent as-is.

---

## Why you have to paste a URL

OpenAI registers the Codex client against one redirect URI:
`http://localhost:1455/auth/callback`. That address means "the machine the
browser is running on" — which is your laptop, not the server Mira is deployed
on. A listener on the server would never see it.

So for a hosted dashboard the code comes back the only way it can: through you.
The CLI, which *does* run where the browser is, opens a real listener and skips
the paste entirely.

A provider that accepts an arbitrary redirect URI does not need any of this.
Set `redirect_mode = "dashboard"` on its spec and the browser comes straight
back to `/api/oauth/callback`, which finishes the login by itself. Those
providers need `MIRA_DASHBOARD_URL` set to the absolute address this dashboard
is reached at. That value decides where a provider delivers an authorization
code, so it is read from configuration only — never from the request, and never
from a `Host` header — and it has to match what was registered with the
provider anyway, which a per-request value could not promise.

## What is stored, and where

One row per provider in the dashboard's `settings` table
(`oauth_credentials:<provider>`), holding the access token, the refresh token,
the expiry, and the account id and plan.

* Treat that table like any other secret store: it is as sensitive as the API
  key it replaces. It is not separately encrypted at rest.
* No API route ever returns token material. `/api/oauth/providers` reports who
  is connected and when the session expires — nothing you could sign a request
  with.
* Every OAuth route is admin-only.
* In-flight logins (the PKCE verifier and its redirect URI) get a row each
  under `oauth_pending:<state>`, expire after 15 minutes, and are removed when
  redeemed or on the next login attempt.

Sessions are renewed a few minutes before they expire, and refreshes are
serialised per provider: several review passes run at once, and an issuer that
rotates refresh tokens would invalidate all but one of them — logging you out
mid-review.

## Rate limits are the account's

A ChatGPT plan includes a usage allowance, not an unlimited one. Mira makes
several model calls per review (indexing, review, security), and a busy
repository can exhaust a plan's Codex allowance the same way heavy CLI use
would. When it does, the endpoint answers 429 and Mira retries on the
`Retry-After` it sends back; sustained exhaustion shows up as failed reviews in
the dashboard's Logs.

If a repository's volume outgrows the plan, an API key remains the path that
scales — the two are one setting apart.

---

## Adding a provider

1. Write the spec in `src/mira/oauth/<provider>.py`, subclassing
   `OAuthProviderSpec`: the endpoints, the client id, the scopes, and — if it
   serves models — an `LLMBinding` naming its base URL, protocol, default model
   and model list.
2. Register it in `src/mira/oauth/registry.py`.

That is the whole change. The dashboard page, the API routes, the CLI, the
model dropdown and the config plumbing all read the registry.

Override the hooks only where a provider deviates from a plain
authorization-code + PKCE flow. The three ChatGPT needs are a good map of what
they are for:

| Hook | What it is for | ChatGPT's use |
| --- | --- | --- |
| `identify` | Pull the account out of a token payload | The account id is inside the id_token, under an OpenAI-specific claim |
| `llm_headers` | Headers every request needs | The account id, plus the Codex client's beta headers |
| `adapt_llm_body` | Reshape the request for the endpoint | Force `stream`, drop `temperature`/`max_output_tokens`, hoist the system message into `instructions` |
| `requires_stream` | The endpoint only answers as an event stream | Yes — `mira/llm/oauth.py` collapses it back into one response |
