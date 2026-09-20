# Reviewing with OpenCode Go

[OpenCode Go](https://opencode.ai/docs/go/) is OpenCode's $10/month
subscription to a curated set of open coding models — Kimi, GLM, DeepSeek,
Qwen, MiniMax, MiMo and others — served from one OpenAI-compatible endpoint
and metered in three windows (5-hour, weekly, monthly) rather than billed per
token. Mira can review with it the same way it reviews through any other
API-key endpoint, and shows where the subscription's allowance stands.

It is an API key, not a sign-in: there is no session to renew and nothing on
the Connections page to connect. What that page adds for Go is the meters.

---

## Configuring it

Put the key in the server's environment and name the provider in
`mira.yaml`:

```bash
# .env
OPENCODE_API_KEY=oc_sk_...
```

```yaml
# mira.yaml
llm:
  provider: "opencode-go"
  model: "kimi-k2.7-code"
  indexing_model: "glm-5.3-flash"
```

`provider: opencode-go` is a shortcut: it fills in `base_url`
(`https://opencode.ai/zen/go/v1`) and `api_key_env` (`OPENCODE_API_KEY`) from
the profile in `providers.json`, and the config then reads as the plain
OpenAI-compatible backend it is. Setting `base_url` to that address yourself
does the same thing; naming `api_key_env` overrides the variable.

The key is created in the [OpenCode Zen console](https://opencode.ai/auth)
after subscribing to Go. Only one member of a workspace can hold the
subscription, and the key is that member's.

Model ids are the bare ones the endpoint lists (`kimi-k2.7-code`, not
`moonshot/kimi-k2.7-code`). The `opencode-go/<id>` form OpenCode's own config
uses is accepted too; the prefix is dropped on the wire.

## Models

**Settings → Models** lists what the endpoint serves. The list is the
endpoint's own (`GET /zen/go/v1/models`, asked once an hour), with labels,
per-purpose recommendations and prices for the models Mira's registry knows.
Models the registry does not know still appear, under their id.

Every Go model answers Chat Completions, which is what Mira speaks by
default. Some of them are documented against the Responses or Anthropic
Messages endpoints too; leave **API Protocol** on Chat Completions, since the
Responses form is refused for the Qwen and MiniMax families.

The registry recommends **Kimi K2.7 Code** for reviews and **GLM-5.3 Flash**
for indexing. Both sit in the $60/month allowance tier; the models with a $15
tier (Kimi K3, GLM-5.3, DeepSeek V4 Pro, Qwen3.8 Max, GPT-5.6 Luna, Grok 4.6)
run out sooner on a busy repository. Prices in the registry are the base
per-million rates the provider lists; they feed the indexing cost estimate
and nothing else, since a subscription is not billed by them.

Extended thinking (**Review Thinking Mode**) is sent as the unified
`reasoning.effort` and dropped for a model that rejects it, as on any other
endpoint.

## What a call carries

Every request goes to `https://opencode.ai/zen/go/v1/chat/completions` with
the key as a bearer token, a `User-Agent` naming Mira, and an
`x-opencode-session` header carrying an id that is stable for one client
instance — one review pass, since a client is created per purpose per review.
Go refuses a request without that header: it routes and caches prompts by
conversation, and asks coding agents to say which conversation a call belongs
to. The id is random and says nothing about the repository.

## Where the allowance stands

**Settings → Connections** has an *API-key endpoints* section. The OpenCode Go
card shows whether the key is set, whether Go is the configured endpoint, and
three meters — **5-hour**, **weekly** and **monthly** — with how much of each
is spent and when it resets, read from `GET /zen/go/v1/usage` (the same
document the Zen console's Go page draws from). The snapshot is asked for when
the page opens and the stored one is more than five minutes old; **Refresh
usage** asks right away and reports the provider's own words when it refuses
("Invalid API key.", "OpenCode Go subscription required.").

The limits are the plan's: 20% of the model's monthly allowance in any
five-hour window, 50% in a week, 100% in the month, tracked per model tier by
the provider. When a window is spent the endpoint answers 429 with a
`Retry-After`, and Mira retries on it the way it does for any other endpoint.
A meter that reads *rate-limited* is the provider saying so; it clears when
the window resets.

From the command line:

```bash
mira auth status              # every signed-in account, then the API-key endpoints
mira auth status --refresh    # …after asking the providers for fresh numbers
```

Nothing about the key is stored: the snapshot is kept in the settings table
under a digest of the key, so a rotated key starts from an empty meter.

## Things to know

* **Data policy.** Some Go models require consent to training on the
  workspace's Go page before the endpoint serves them; the endpoint refuses
  with a message naming the page. The console's Go page also has the region
  toggle a few models (the DeepSeek family) need.
* **Zen is not Go.** The pay-per-token catalogue at `https://opencode.ai/zen/v1`
  (Claude, GPT, Gemini) is a separate endpoint with the same key. Point
  `llm.base_url` there to use it; it has no usage meters, and its ids are
  its own.
* **One workspace, one subscriber.** The Go subscription belongs to one
  member of an OpenCode workspace, and so does the allowance the meters show.
* **Switching back.** The key path and a signed-in account are one setting
  apart, as always: a ChatGPT account connected on the Connections page can
  be made the default for bare model ids, and an `api:<model>` route keeps a
  purpose on Go regardless.
