# Logs

Mira's own log output, kept in your database and readable from the dashboard.

Everything Mira does that can fail already says so through Python's `logging`.
The problem was never that the information does not exist — it is that it
exists on a container's stdout, which is the one record reliably gone by the
time anybody goes looking. A restart takes it, and the person who saw
**Review failed** on a pull request is usually not the person who can reach the
host.

## The loop this closes

A review that fails posts a notice on the pull request. That notice is
deliberately thin — it goes somewhere public, so it names no model and carries
no internal error:

```
❌ Review failed — click for details

The code review failed to complete due to an unexpected error.

Stage: Code review
Error type: LLMError
Message: LLM tool-call failed
Trace ID: 9f2c41ab7d3e5106 — search this on the Logs page of your Mira
          dashboard for the full diagnostics.
```

The trace ID is the handle. Every log line that review emitted carries it —
including the lines from the LLM client and the platform providers, which know
nothing about pull requests. Paste it into the **Trace ID** box on
**Dashboard → Logs** and you get the whole story: which model was tried, what
each re-roll came back with, whether the fallback model ran, whether JSON mode
recovered it, and the stack that finally ended the review.

Without it, "a review failed at some point this afternoon" is a timestamp hunt
through the interleaved output of every review that was running at the time.

## The page

**Dashboard → Logs**, admin only. Filters:

| Filter | What it does |
|---|---|
| Search | substring over both the message and the traceback |
| Trace ID | one review's lines, exactly |
| Level | a floor, not an exact match — `Error and above` includes critical |
| Module | the logger name (`mira.llm.base`, `httpx`, `uvicorn.error`) |
| Time | trailing window; `Everything kept` ignores it |

Every filter lives in the URL, so a filtered view is a link you can send to a
colleague. Rows with a traceback expand. **Copy these logs** puts the current
filtered set on the clipboard as plain text; **Download** serves the same bytes
as a file. Both apply the filters that are on screen — an export that quietly
widened them is how a private repository's name ends up in a public bug report.

**Follow** polls every five seconds while something is still running.

## What is stored, and what is not

Log lines are the least structured thing Mira keeps: they carry URLs, request
bodies, model output, and whatever an exception put in its message. Two things
follow.

**Every line is redacted before it is written**, using the same rules
[autofix](autofix.md) applies to anything it sends to a model — private keys,
vendor tokens, `password = "…"` assignments, credentials in URLs, email
addresses. A secret in a database somebody can read from a browser is worse
than one on a terminal.

**Every route is admin-only, reads included.** There is no per-repository slice
of this that would be safe to hand to somebody who can only see one repository:
a review of a private repository logs its name.

## What it costs

Nothing on a review's path waits for the database. The handler formats the
record and drops it on a bounded in-memory queue; a background thread batches
and inserts. When the queue fills — a burst faster than the disk — records are
**dropped and counted** rather than held onto or waited on, and the page says
so in a banner rather than showing a quietly incomplete trail.

Retention is two limits, because either alone has a failure mode: an age limit
lets one loud afternoon fill the disk, and a row limit lets a quiet install
keep lines from a year ago that nobody will read.

On SQLite the writer holds **its own connection**. `sqlite3` makes individual
statements safe across threads but leaves the transaction shared, so a writer
committing on a timer would commit whatever the dashboard had half-written at
that moment — and the rollback that should have discarded it would find nothing
left to discard. With a separate connection the two never share a transaction;
if they contend for the write lock, the writer waits and then drops the batch,
which the gaps banner reports.

## Configuration

All optional; the defaults are what a self-hosted install wants.

| Variable | Default | Notes |
|---|---|---|
| `MIRA_LOG_CAPTURE` | `1` | `0` switches the whole thing off. The page then says so rather than showing an empty table. |
| `MIRA_LOG_CAPTURE_LEVEL` | `INFO` | `DEBUG` captures far more, and costs it. Turning this down does **not** make stdout noisier — the handlers that were already attached keep the level they had. |
| `MIRA_LOG_RETENTION_DAYS` | `7` | |
| `MIRA_LOG_MAX_ROWS` | `200000` | |

Two loggers are never captured, whatever the level: `mira.logs` (capturing it
would let a failed write report itself into the queue that is failing to
drain), and `uvicorn.access`, which is one line per HTTP request — including
every poll the Logs page itself makes.
