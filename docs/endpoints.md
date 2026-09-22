# Configuring a model provider from the dashboard

Mira reads its LLM endpoint from `mira.yaml` and its key from the
environment. That is right for a deployment you own the file and the process
of, and wrong for a container, a hosted image or a proxy somebody else runs,
where changing either means a rebuild or a restart to answer a question as
small as "which key".

So an endpoint can also live in Mira's own database. **Settings →
Connections → API-key endpoints**: a name, a URL, a key, and which one
reviews use. Nothing on the server has to change, and nothing here is
required — with no endpoint configured, the config file and the environment
decide exactly as before.

---

## Adding one

**Add endpoint** asks for four things:

* **Provider** — a *preset*, or "Custom endpoint". A preset fills in the URL
  and the protocol, and carries the quirks that endpoint needs: the headers
  it wants, the model-id form it routes on, the reasoning levels it spells
  differently, and where it reports usage. Bundled today: OpenRouter,
  OpenCode Go, OpenCode Zen, OpenAI, DeepSeek, Groq, Together, Fireworks and
  Ollama. Anything else is a custom endpoint, which is most of them — a
  LiteLLM proxy, a vLLM server, a gateway of your own.
* **URL** — the base URL, ending in `/v1` for most providers. Held to the
  same rule a URL in `mira.yaml` is: http(s) with a host, and plain http only
  for localhost, private addresses and dotless hostnames, so a key cannot be
  sent unencrypted across the internet.
* **API key** — stored in Mira's database. Leave it blank to use the
  environment instead (below), or for an endpoint that needs no key at all.
* **Protocol** — Chat Completions, which works everywhere, or the Responses
  API for an endpoint that exposes `/responses`.

**Test connection** asks the endpoint for its model list before you save.
That is the cheapest call that proves the URL resolves and speaks the
protocol, and it spends no tokens. It also says whether the *key* was
checked: some endpoints list their models to anybody, so a 200 there proves
nothing about the key, and the test says so rather than implying otherwise.
Where the provider has a usage endpoint (OpenCode Go), that call
authenticates, so its refusal is the test's refusal.

## Which endpoint reviews use

**Use for reviews** makes an endpoint the one a model id without a backend
goes to. The card says which one that is.

A signed-in account still outranks it: if a ChatGPT account is the default
on the same page, bare ids go there and endpoints are reached by naming
them. Both are visible in one place, and under **Settings → Models** a line
under each picker says exactly where that choice sends calls.

Each purpose can name an endpoint of its own, the way it can name an
account. The picker offers every configured endpoint as its own section, and
a chosen option is stored as a route:

| Value | Meaning |
| --- | --- |
| `endpoint:opencode-go:kimi-k2.7-code` | that endpoint, whether or not it is the default |
| `api:openai/gpt-5.1` | whichever endpoint the API-key path uses |
| `oauth:chatgpt:*:gpt-5.6-sol` | a signed-in account (see [oauth.md](oauth.md)) |
| `kimi-k2.7-code` | the default backend |

Routes are plain strings and work in `mira.yaml` too. Indexing every file
through a subscription while reviews run on a stronger model elsewhere is
two lines:

```yaml
llm:
  review_model: "anthropic/claude-sonnet-4-6"
  indexing_model: "endpoint:opencode-go:glm-5.3-flash"
```

## Fallback models

A model can stop answering without the endpoint going down: a rate limit
that outlasts the backoff, a gateway that starts returning empty replies, a
reasoning model that spends its whole output budget thinking and has
nothing left for the tool call. One provider already does what it can for
its own model — transport retries, re-rolls of a malformed tool call with a
corrective prompt, a bigger output budget after a truncated reply, and a
plain-JSON rescue when tool calling will not work. When all of that is
spent, the review used to fail.

Each purpose can now name an ordered list of models to try next. Under
**Settings → Models**, every picker has a *Fallback models* list beneath it:
add entries, put them in order with the arrows, and save. When the purpose's
model fails a call, the same call is made with the first fallback, then the
second, and so on; the review fails only when the whole chain has. A
fallback answers one call, not the rest of the review — the next chunk
starts from the primary again, so a model that has recovered is back in
use without anyone touching the page.

Entries are the same values the pickers take — a bare id, or a route naming
its backend — so a chain can cross endpoints and accounts:

```yaml
llm:
  review_model: "kimi-k2.7-code"
  review_fallback_models:
    - "glm-5.3"                              # same endpoint, another model
    - "api:anthropic/claude-sonnet-4-6"      # the API-key endpoint
    - "oauth:chatgpt:*:gpt-5.6-sol"          # a signed-in account
  indexing_fallback_models: ["endpoint:openrouter:anthropic/claude-haiku-4-5"]
```

The dashboard's list outranks the file's, the way the model settings do;
*Use deployment config* hands the choice back to the file. The security
pass falls back to the review chain when it has none of its own, the way
`security_model` falls back to `review_model`. At most five per purpose:
each entry is a full round of retries before the next is tried, so a long
chain is a slow failure rather than a resilient one. An entry naming an
endpoint or account this install does not have is skipped with a warning
at start-up rather than failing the primary.

**Reasoning levels.** *Review Thinking Mode* offers the levels the review
model's provider reports for it — read from [models.dev](https://models.dev),
the catalogue the OpenCode client reads, for API-key endpoints, and from
the backend itself for ChatGPT — and falls back to the built-in list when
the provider has said nothing. A line under the picker says which. On the
wire, a level a model lacks is snapped to the nearest it has, so one
setting serves a chain of models with different scales; each preset in
`providers.json` names the field the level travels in (`reasoning_param`)
and its models.dev id (`models_dev`). Set `MIRA_MODELS_DEV_URL=""` to keep
an install off that catalogue.

**Output budget.** The same page sets *Max output tokens*, the cap on every
call — thinking included, which is why a reasoning model on the 4096
default can answer with nothing. Pick a count, or *Unlimited* to send no
cap at all and let the model's own maximum apply; `max_tokens: 0` in
`mira.yaml` means the same. It applies to every purpose and to every model
in a chain.

The log tells the story: `Model chain: kimi-k2.7-code @ … → glm-5.3 @ …`
when the client is built, a warning naming the failure and the next model
at each step, and the trace id on every line so the Logs page shows the
whole walk (see [logs.md](logs.md)).

## Where the key lives

A stored key is a row in the dashboard's `settings` table, in its own row
next to the endpoint rather than inside it — no API route can return one by
forgetting to leave it out. What the page shows is where the key comes from
and its last four characters, which is what tells two keys apart.

* Treat that table like any other secret store: a stored key is exactly as
  sensitive as the environment variable it replaces. It is not separately
  encrypted at rest.
* Every route under `/api/providers` is admin-only, reads included.
* **No route hands a key back.** Mira reads it to sign a request and to
  derive the four characters on the card, and nothing else returns it —
  there is no endpoint that answers with a key, so the browser never holds
  one it did not just type. Editing an endpoint and leaving the key field
  blank keeps the key that is set; **Remove stored key** clears it.

**Keeping the key in the environment.** The form's *environment variable*
field points an endpoint at a variable instead of storing a secret: Mira
reads it at call time. The variables already set on the server are listed
under the key field, by name. A preset also knows its conventional variable
(`OPENCODE_API_KEY`, `OPENROUTER_API_KEY`, …) and falls back to it when
nothing else is set, so an install that already had its key in the
environment keeps working when you add the matching endpoint.

The order is: the stored key, then the endpoint's own variable, then the
preset's.

## What happens to mira.yaml

Nothing. The endpoint the file names still appears on the page, marked
**from mira.yaml** and not editable there — the file is the authority for
it, and a form that appeared to edit something it cannot write would be a
lie. It is the default until you make a stored endpoint the default, and
removing every stored endpoint hands it back.

`llm.provider` can still name a preset directly, which is the file-only way
to say the same thing:

```yaml
llm:
  provider: "opencode-go"   # fills in the URL and OPENCODE_API_KEY
  model: "kimi-k2.7-code"
```

An unknown name is rejected at config load rather than at review time, where
it would quietly resolve to OpenRouter and whatever key that path found.

Two names are not presets and never will be: **`openai`** means any
OpenAI-compatible endpoint — whatever `base_url` says, which is what it has
always meant — and **`bedrock`** means the AWS Converse API. OpenAI's own
API is therefore the **`openai-api`** preset. A profile that tries to take
either name is ignored with a warning, so neither meaning can shift under a
config that already works.

## First run

A fresh install with no key anywhere starts on **Add an endpoint** rather
than on the model picker: with nothing to call, a model list is a list of
things that will fail. Adding one there makes it the default and moves on to
the models. An install that already had a working key skips the step
entirely.

## From the command line

```bash
mira auth status     # signed-in accounts, then every endpoint and its allowance
mira auth status --refresh   # …after asking the providers for fresh numbers
```

Endpoints are added and edited from the dashboard; the CLI reads them. A
CLI-only install can still use `llm.provider`/`llm.base_url` in `mira.yaml`
as it always could.

## Metered subscriptions

An endpoint whose preset reports usage shows its windows on the card, read
from the provider's own endpoint and refreshed on demand. OpenCode Go is the
one today — 5-hour, weekly and monthly. See
[opencode-go.md](opencode-go.md).

## Adding a preset

A preset is an entry in `src/mira/llm/providers.json`: a label, a
description, a URL, the key variable, and whatever quirks the endpoint has.
Operators can add their own without forking by pointing
`MIRA_PROVIDERS_JSON_PATH` at a file of the same shape — its entries are
overlaid by name, and any entry with a `label` becomes a preset in the form.
