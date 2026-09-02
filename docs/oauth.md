# Signing in instead of paying per token

Mira normally talks to an LLM with an API key you provide. It can also **sign
in to an account** and review with the models that account already includes —
today that means a ChatGPT plan, through the same backend the Codex CLI uses.

No API key, no per-token bill, and nothing to rotate: the session is stored in
Mira's database and renewed automatically before it expires.

You can connect **several accounts of the same provider**. Each shows its own
allowance on the Connections page, the model picker lists each one as its own
section, and reviews can rotate across them — the next call goes to the
account with the most allowance left, and one the backend refuses is set
aside until its window resets.

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

The account appears on the provider's card with its plan, when the session
renews, and — for ChatGPT — how much of the **5-hour** and **weekly** windows
is spent and when each resets. The first account you connect becomes the
default backend for reviews on its own; nothing else to press.

**Add another account** runs the same flow again. Signing in with an account
that is already connected replaces its session (a reconnect); any other
account is added alongside. Each account has its own **Refresh usage**,
**Refresh session** and **Disconnect**.

### What "default" means, and how to choose

A model can be picked in two ways under **Settings → Models**:

* **A bare id** — `gpt-5-codex`, the way models were always picked — goes to
  the *default backend*: the provider marked as such on the Connections page,
  or the API-key endpoint when none is. On the card, **Rotate across
  accounts** makes bare ids go to any of the provider's accounts (by remaining
  allowance), **Use this account** on one account pins them to it, and
  **Stop using** sends them back to the API key.
* **A route** names its backend and does not depend on the default. The
  picker builds these for you: every signed-in account is a section of its
  own, so is "any account (rotate)" when a provider has more than one — it
  lists only the models every one of those accounts can serve, since
  rotation picks the account by allowance rather than by model — and so is
  the API-key endpoint. A route to a provider or account that is not
  connected fails with a clear error rather than falling back to the key. Under each picker a line says exactly where that
  choice sends calls — backend, account, protocol, endpoint and the model id
  on the wire.

Routes are plain strings, accepted anywhere a model id is:

| Value | Meaning |
| --- | --- |
| `oauth:chatgpt:<key>:gpt-5-codex` | that one account (its key is shown on the card and by `mira auth status`) |
| `oauth:chatgpt:*:gpt-5-codex` | any ChatGPT account, rotating by remaining allowance |
| `api:openai/gpt-5.1` | the configured API-key endpoint, whatever the default is |
| `gpt-5-codex` | the default backend |

The last two are how one purpose stays on a key while another uses a
signed-in account: indexing every file through a cheap key-based model, say,
and reviewing through the plan.

The model list under each ChatGPT account is asked of the backend itself
(`GET /codex/models`), so a model that appears on the plan appears in the
picker without a Mira release. The curated list built into the provider spec
stands in when that call fails.

## Connecting from the CLI

If Mira runs on your own machine, the CLI can catch the redirect itself:

```bash
mira auth login chatgpt            # opens a browser, listens on localhost:1455
mira auth login chatgpt            # again, with another account: it is added
mira auth status                   # every account, its allowance, and the default
mira auth status --refresh         # …after asking the provider for fresh numbers
mira auth use chatgpt              # bare ids rotate across every ChatGPT account
mira auth use chatgpt:<key>        # …or go to one account
mira auth use                      # …or back to the API key
mira auth logout chatgpt:<key>     # forget one account; `chatgpt` alone forgets all
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
  oauth_provider: "chatgpt"     # bare ids go to ChatGPT, rotating across accounts
  oauth_account: "<key>"        # optional: one account only
  model: "gpt-5-codex"
  indexing_model: "api:openai/gpt-5-nano"          # a route works here too
  security_model: "oauth:chatgpt:*:gpt-5-codex"
```

You still have to connect the account once; this only says *which* session to
use. When set, `oauth_provider` wins over `provider`, `base_url` and
`api_key_env` for bare ids — the endpoint and the auth both come from the
provider spec. The dashboard's Connections page writes the same setting, and
its choice takes precedence over the file.

An unknown id is rejected at config load rather than at review time: without
that check, a typo silently resolves to "no session" and quietly puts every
review back on the API key.

If no model is chosen anywhere, the provider's default is used. Mira's built-in
default model id is an OpenRouter-style Claude id that this endpoint cannot
serve, so inheriting it would turn "connect ChatGPT" into a 400 on the next
pull request. A model you *did* choose is always sent as-is.

---

## What a call actually does

For ChatGPT, every call is a **Responses API** request to
`https://chatgpt.com/backend-api/codex/responses`, answered as a
**server-sent event stream** that Mira collapses back into one response
object; the request carries the account id and the Codex client's headers,
and a few fields the public API accepts are dropped because this backend
rejects them (`temperature`, `max_output_tokens`, `store`). The Connections
card and the line under each model picker say this in the same words, so
there is no guessing which protocol a given choice uses — an API-key endpoint
you configured may well speak Chat Completions to a model of the same name.

## Rate limits are the account's

A ChatGPT plan includes a usage allowance, not an unlimited one, metered in
two windows: a short one (five hours) and a long one (a week). The backend
reports where each window stands on **every response**, as headers, and Mira
records that against the account that made the call — so the meters on the
Connections page are as fresh as the last review, and **Refresh usage** asks
the backend directly (`GET /wham/usage`, the same call the Codex CLI makes for
its status screen) when you want to look before a review runs.

Mira makes several model calls per review (indexing, review, security), and a
busy repository can exhaust a plan's allowance the same way heavy CLI use
would. When it does, the endpoint answers 429. With one account, Mira retries
on the `Retry-After` it sends back; with several and rotation on, the refused
account is set aside until its window resets and the same request goes to the
next one. When every account is set aside, the review fails saying so, and
sustained exhaustion shows up in the dashboard's Logs.

Rotation ranks accounts by the headroom in their tightest window, breaking
ties by whichever was used longest ago, so equally fresh accounts take turns.
One review pass stays on the account it started with unless that account is
refused: a tool loop carries encrypted reasoning between turns, which is best
not bounced between accounts mid-conversation.

If a repository's volume outgrows the plans, an API key remains the path that
scales — the two are one setting apart.

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

One row per account in the dashboard's `settings` table
(`oauth_credentials:<provider>:<account key>`), holding the access token, the
refresh token, the expiry, and the account id and plan. Next to it, one row of
usage (`oauth_usage:<provider>:<account key>`): the windows the backend last
reported and Mira's own note of a refusal. The account key is derived from the
provider's account id, which is why signing in to the same account again
lands in the same slot. A row written by an earlier build, which held one
account per provider, is moved into a slot the first time it is read.

* Treat that table like any other secret store: it is as sensitive as the API
  key it replaces. It is not separately encrypted at rest.
* No API route ever returns token material. `/api/oauth/providers` reports who
  is connected, when each session expires and how much of the plan is spent —
  nothing you could sign a request with.
* Every OAuth route is admin-only.
* In-flight logins (the PKCE verifier and its redirect URI) get a row each
  under `oauth_pending:<state>`, expire after 15 minutes, and are removed when
  redeemed or on the next login attempt.

Sessions are renewed a few minutes before they expire, and refreshes are
serialised per account: several review passes run at once, and an issuer that
rotates refresh tokens would invalidate all but one of them — logging you out
mid-review.

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
authorization-code + PKCE flow. What ChatGPT needs is a good map of what they
are for:

| Hook | What it is for | ChatGPT's use |
| --- | --- | --- |
| `identify` | Pull the account out of a token payload | The account id is inside the id_token, under an OpenAI-specific claim |
| `llm_headers` | Headers every request needs | The account id, plus the Codex client's beta headers |
| `adapt_llm_body` | Reshape the request for the endpoint | Force `stream`, drop `temperature`/`max_output_tokens`, hoist the system message into `instructions` |
| `requires_stream` | The endpoint only answers as an event stream | Yes — `mira/llm/oauth.py` collapses it back into one response |
| `usage_from_headers` | Read the allowance off a response | The `x-codex-primary-*` / `x-codex-secondary-*` headers |
| `fetch_usage` | Ask where the allowance stands | `GET /wham/usage` |
| `fetch_models` | Ask which models the account may use | `GET /codex/models` |

A provider that reports no usage leaves the last three alone: the dashboard
shows no meters for it, and rotation across its accounts falls back to
round-robin plus "skip one that answered 429".
