# Asking Mira about a pull request

Mention Mira with a question on a pull request or merge request, in any
language, and it answers in the same conversation:

```text
@mira where else is parse_config called?
@mira o caller já valida isso?
```

Before answering, Mira can read the repository at the pull request's head with
the same read-only tools a review uses — `read_file`, `grep_repo`,
`find_usages`, `find_definition` and, on an indexed repository, `grep_index` —
so a question the diff cannot answer ("does the caller already validate this?")
is checked rather than guessed at. The answer cites the `path:line` it looked
at, and says so plainly when it could not verify something.

The same applies to a question asked on one of Mira's review threads, or a
mention of Mira in somebody else's review thread: the code the thread sits on
is shown, and the rest of the repository is a lookup away.

## What Mira reads

- **The question**, and the language it is written in: a question in
  Portuguese gets an answer in Portuguese.
- **The conversation so far** — earlier comments on the pull request, or the
  earlier messages of the review thread — including Mira's own replies, oldest
  first. "And the other file?" refers to what was just discussed. Bounded by
  `chat.history_messages` and `chat.history_chars`, newest kept; each message
  is cut at 1,500 characters.
- **The pull request's diff**, up to `chat.max_diff_chars`; files beyond that
  are still readable with `read_file`.
- **The repository at the head**, through the tools.

Everything taken from the pull request — title, description, diff, comments,
file contents a tool returns — is framed as untrusted data and redacted before
the model sees it, as in every other Mira prompt. Text in a comment cannot
change what Mira is allowed to do.

## Bounds and fallback

The tool loop runs on the review model and stops at whichever comes first:

| Bound | Key | Default |
|---|---|---|
| Model turns, lookups included (the last is told to answer) | `chat.max_hops` | 6 |
| Wall clock for the whole loop (past 60% the next turn is the last) | `chat.deadline_seconds` | 120 |
| Length of the posted answer, cut at a paragraph break | `chat.max_answer_chars` | 6000 |

When the loop fails, times out or runs out of turns without answering, Mira
answers in one call over the diff and the conversation instead, carrying
whatever its lookups had already found. If even that fails, it says it could
not answer rather than staying silent.

## Replies on Mira's review threads

A reply on one of Mira's review comments is classified first — the finding is
wrong, it is fixed, a question, agreement — and what happens next depends on
which. Two of those answers are decided by the code rather than by the reply:

- **"Fixed"** ("done", "corrigido", "it's in `store.ts` now"): Mira rechecks
  the code, and only a recheck that finds the problem gone resolves the
  thread, as fixed.
- **Pushback** ("the caller already validates it"): a judge reads the code
  before anything is learned from the reply.

Both used to see the commented file only — a window around the finding and
what changed *in that file* since. A fix in a helper, a caller, another
package or a re-export was invisible, and Mira asked the author to "show" or
"push" code that was already there. Now each check sees:

- **The commented file at the head**, numbered, around the finding and around
  every line the reply cites.
- **What changed since the finding, in every file**: the commented file
  first, then files the reply, the finding or the thread name (by path, by
  filename, or by a symbol on their changed lines), then the rest — up to
  16,000 characters of hunks, with a list of every changed file and its line
  counts, each marked shown, shown in part or not shown.
- **Lookups at the head.** Before the verdict, a short read-only tool loop —
  the same tools as above — reads what that still does not show: the file the
  reply names, the definition of the function, its callers, a search for the
  literal. The verdict is then given in one call, through the same submit tool
  as before, with the lookups in view.

The lookups are bounded by turns (`chat.thread_max_hops`, default 5) and wall
clock (`chat.thread_deadline_seconds`, default 90, snapshot included; past 60%
the next turn is the last), and run at `chat.thread_lookup_effort` (default
`low`) when the review model reasons at a higher effort — the verdict keeps
the review model's. When they fail or time out, the check goes ahead with
whatever they found, or with the diff alone, exactly as it did before; a
reply is never left unanswered because a lookup failed. They run only for
those two checks: a question goes to the chat above, and agreement needs no
code.

Mira is told never to ask the author to show, push or point at code. If the
fix is genuinely not at the head, the reply says which file or symbol it
looked for and where.

When a review runs on a new push, Mira also checks its open threads for
fixes (`review.auto_resolve_conversations`). That check now sees the rest of
the pull request's diff, prioritised the same way, alongside each thread's
file, so a thread whose fix landed in another file is resolved too.

## Asking Mira to change code

A chat message that asks Mira to change code — "fix this", "can you fix it?",
"pode corrigir?", "corrige tudo" — is not answered with an explanation. It goes
to [autofix](autofix.md), exactly as the commands do:

- On one of Mira's review threads, it is `@mira fix` for that finding.
- On the pull request, "fix all" / "corrige tudo" is `@mira fix all`; a request
  that names no finding gets told how to name one.

Autofix's own rules still decide: who may ask, what may be written, and the
limits. When autofix is off for the repository, Mira says so and how to turn
it on (`autofix.mode: suggest` or `on`). Only the requester's own words count
— quoted text and code blocks are ignored — and talk *about* a fix ("Fix
applied in abc123", "how would you fix this?") is answered as a question.
Set `chat.autofix_handoff: false` to answer every message instead.

## Configuration

```yaml
chat:
  agentic: true            # false: one call over the diff and the conversation
  max_hops: 6              # 2-12
  deadline_seconds: 120    # 15-600
  max_answer_chars: 6000   # 500-60000
  max_diff_chars: 40000    # 2000-200000
  history_messages: 20     # 0 turns memory off
  history_chars: 12000
  autofix_handoff: true
  thread_lookups: true          # look up the code before a recheck or a judge
  thread_max_hops: 5            # 2-8
  thread_deadline_seconds: 90   # 15-300
  thread_lookup_effort: low     # "" keeps the review model's effort
```

The tools reuse the review's settings: `review.repo_snapshot` (one archive
download instead of a file-by-file read) and `review.code_graph.enabled`
(`find_usages` / `find_definition`).
