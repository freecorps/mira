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
```

The tools reuse the review's settings: `review.repo_snapshot` (one archive
download instead of a file-by-file read) and `review.code_graph.enabled`
(`find_usages` / `find_definition`).
