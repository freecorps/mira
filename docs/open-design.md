# Reviewing with OpenDesign Cloud

[OpenDesign Cloud](https://open-design.ai/pt-br/help/cloud-api/) is a
coding-plan subscription to DeepSeek, GLM, Kimi, Qwen, MiniMax, MiMo, Claude,
GPT, Gemini and more, served from one OpenAI-compatible endpoint. It is billed
in plan credits rather than per token: each call consumes the model's USD
usage value times a per-model *Coding Plan multiplier* (0.27× for DeepSeek
V4.1 Flash Eco, 0.5× for DeepSeek V4.1 Flash, 1× for most models, 4× for a
few), within a 7-day window — and, on the Go plan, a 5-hour one too.

OpenDesign has **no usage API**: what a key has spent is visible only in its
web console. So the meters Mira shows for it are an **estimate**, worked out
from the tokens Mira itself recorded spending on that endpoint. They are
labelled as such, and the console is the authority.

---

## Configuring it

1. **Create a key** in the OpenDesign console (Cloud API → API keys). It
   starts with `sk-amr-`.
2. **Give Mira the key.** Either paste it into the endpoint form (stored in
   Mira's database), or export it:

   ```bash
   # .env
   OPENDESIGN_API_KEY=sk-amr-...
   ```

3. **Add the endpoint.** Settings → Connections → **Add endpoint**, pick
   **OpenDesign Cloud**. The URL (`https://amr-link.open-design.ai/v1`), the
   protocol (Chat Completions) and the quirks come from the preset. **Test
   connection** asks for the model list, which needs a valid key, so a pass
   means the key works; no tokens are spent.
4. **Pick the plan** the key is on (Go, Plus, Pro or Max), and the day the
   subscription started if you know it — the 7-day window starts on that day,
   not on a Monday. Without a date the 7-day window rolls.
5. **Choose models.** Settings → Models lists what the endpoint serves.
   `deepseek-v4.1-flash-eco` is the recommended pick for both reviews and
   indexing: the same model as Flash at 0.27× instead of 0.5×, so the same
   plan goes almost twice as far. OpenDesign notes Eco "can be slower at
   times" and recommends it for batch work, which a review is; if latency
   matters more than allowance, use `deepseek-v4.1-flash`.

From the config file instead:

```yaml
# mira.yaml
llm:
  provider: "open-design"            # fills in the URL and OPENDESIGN_API_KEY
  model: "deepseek-v4.1-flash-eco"
  indexing_model: "deepseek-v4.1-flash-eco"
  plan: "go"                         # go | plus | pro | max
  plan_started: "2026-11-10"         # optional; start of the 7-day window
  cost_scale: 1.0                    # calibration, see below
```

Model ids are the bare ones the endpoint lists. The `/v1/models` list carries
each model's usable input and output budgets (`context_budget`,
`output_budget`), and Mira uses them for any model its registry has no entry
for on this endpoint.

## The estimated meter

The preset's plans are data in `src/mira/llm/providers.json`, in plan credits:

| Plan | 5-hour | 7-day |
|------|--------|-------|
| Go   | 5      | 15    |
| Plus | —      | 20    |
| Pro  | —      | 45    |
| Max  | —      | 75    |

These are derived from the per-model limits OpenDesign's billing page lists.
Go is "$55 of Eco or $30 of Flash per 7 days" and "$18 / $10 per 5 hours";
at 0.27× and 0.5× both work out to 15 and 5 credits. So the page is
describing one pool of credits restated per model, not separate pools. Plus
is $74 / $40, which is 20 credits. Pro and Max list $90 and $150 of Flash.

Every call Mira makes is written to the usage ledger with its estimated cost
and credits (see below). The card sums the credits recorded against the
endpoint inside each window and shows the percentage of the allowance. It
also restates what is left in the tightest window per reference model, for
example "≈ $14.81 of DeepSeek V4.1 Flash Eco or $8.00 of DeepSeek V4.1 Flash".
**Recalculate** works it out again.

What the estimate cannot see:

- calls made with the same key from anywhere else (an IDE, another tool);
- the provider's own rounding, promotions (Flash has run at 0.25× off-peak),
  or a price change Mira's registry has not caught up with;
- the per-token price itself. OpenDesign does not publish one, so the base
  prices are DeepSeek's own list prices (V4.1 Flash: $0.30 input, $1.20
  output, $0.006 cache read per 1M tokens).

### Calibration

Compare the card with the OpenDesign console after a few reviews. If the
console reads 25% more than Mira, set **Calibration** to `1.25` on the
endpoint (or `llm.cost_scale` in `mira.yaml`). It scales the recorded credits
when the meter and the cost view read them, so it applies to past calls too,
and changing it again is harmless.

## Cost and cache hits, for every endpoint

The ledger is not OpenDesign-specific. Every LLM call, on every endpoint,
records the endpoint, model, purpose (review, indexing, security), prompt
tokens, the part of them served from the provider's prompt cache, completion
tokens, the estimated USD and the estimated plan credits. The cost view at the
bottom of Settings → Connections (and `GET /api/usage/costs?days=7|30`) sums
it per endpoint and model over 7 or 30 days. It shows calls, input, cached
input and the cache-hit rate, output, estimated USD and credits, and a month
projected from the pace so far. Each review's record carries its own prompt,
cached, completion and cost totals.

Cached tokens are billed at the model's `cache_read_cost_per_1m` in
`models.json`. On DeepSeek that is about a fiftieth of the input rate, which
is why a review's cost depends so much on its cache-hit rate. Rows are kept
for 90 days (`MIRA_USAGE_RETENTION_DAYS`); `MIRA_USAGE_LEDGER=0` turns the
ledger off.

## Known quirks

- **One turn split across choices.** A non-streamed reply sometimes puts
  the text in `choices[0]` and the tool call in `choices[1]`. Mira merges
  the choices of a reply into one turn (it never asks for `n`), so the call
  is not lost.
- **`200` with no choices.** When reasoning spends the whole output budget,
  the reply is `"choices": []` with a usage block. Mira retries it like any
  malformed reply, and the log line says the completion tokens went to
  reasoning.
- **Reasoning comes back as `reasoning_details`** on non-streamed replies,
  and as `delta.reasoning` text when streamed (the preset streams). Sent back
  in a tool loop, OpenDesign accepts it in every form, so the preset echoes
  it. An endpoint that is not sent its reasoning back has
  `reasoning_details` stripped along with the text fields.
- **A spent plan.** The shape of OpenDesign's "window spent" refusal is not
  documented. A 402, 403 or 429 whose body talks about a quota, balance,
  credits or a usage limit is treated as the plan's limit: it is logged as
  such and not retried, so the next model in the fallback chain takes the
  call at once. A plain "rate limit exceeded" 429 is still retried.
- **Forced `tool_choice` works, but slowly** (around 25 seconds).
  `response_format: json_object` and a custom `temperature` are accepted.
- No rate-limit headers were seen, and 14 concurrent requests all
  succeeded. The documented concurrency per workspace is 10 on Go, 20 on
  Plus, 30 on Pro and 50 on Max.
