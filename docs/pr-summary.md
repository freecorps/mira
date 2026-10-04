# Generated PR description and title

Mira can write a summary into a pull request's description and a title in place
of a bare `@mira`. Both are off-by-default or opt-in, and both follow one rule:
**Mira only replaces text it owns.** Everything a human wrote stays exactly as
it was written.

```yaml
pr_summary:
  description:
    enabled: false        # off by default
    mode: "section"       # "section" | "empty_only"
  title:
    mode: "on_mention"    # "off" | "on_mention" | "always"
```

The section lives in `mira.yaml` or in a repository's own `.mira.yaml`, like
every other review setting.

## The description

With `description.enabled: true`, Mira writes its summary between two HTML
comments, which render as nothing:

```markdown
Why this change: the retry loop never backed off.      ← the author's text

<!-- mira:summary:start -->
### Summary
Adds exponential backoff to webhook delivery …

<details><summary>Changes (3 files)</summary> … </details>
<!-- mira:summary:end -->
```

| `mode` | What Mira writes |
|---|---|
| `section` *(default)* | Appends the section to the body, or refreshes it in place if it is already there. Refreshed on every push that brings new commits. |
| `empty_only` | Writes the section only while the body has no human-written text. Once the author writes next to it, Mira stops refreshing. |

**Placement.** Put `@mira summary` anywhere in the description and the
section goes there instead of at the end, in either mode. A section that
already exists elsewhere moves to the placeholder rather than appearing twice.

**What is never touched.** Anything outside the markers. A body whose markers
do not form exactly one ordered pair — a start whose end was deleted, two
sections pasted together — is left alone entirely, because the next refresh
would otherwise swallow whatever the author wrote after the orphaned marker.
A body that would not change is not written, so a refresh never produces an
"edited" mark for nothing.

**What the section can never say.** Generated text drops the `@` from Mira's
own handles, so a summary that quotes `@mira ignore` from the diff cannot turn
the next review off, and the markers themselves are stripped from anything the
model wrote.

**What the review reads.** The review is given the description *without*
Mira's section. The description is the author's statement of intent; reading
last push's summary back as that statement would be the model agreeing with
itself.

### Cost

On the first review, the summary is the walkthrough the review already
generated — no extra model call. A follow-up push is reviewed incrementally,
so its walkthrough describes only the new commits; refreshing the section then
costs one walkthrough call over the full diff. A push with nothing new to
review leaves an existing section as it is.

## The title

Mira writes a title in exactly two cases:

| `title.mode` | When |
|---|---|
| `off` | Never — not even for `@mira`. |
| `on_mention` *(default)* | The title is the bot mention alone: `@mira`, any case, surrounding whitespace ignored. The bot's real platform handle (the GitHub App slug, the GitLab or Forgejo bot user) counts too. |
| `always` | The above, plus the first review of every pull request. A title a human sets after that is never overwritten. |

"First review" means Mira has no record of having reviewed the pull request.
If that record cannot be read, the answer is "not the first", so a database
hiccup cannot cost a human their title.

The generated title is one line, at most 72 characters, plain text, and never
mentions anybody. It comes from one small structured call that sees the list of
changed files, the walkthrough summary and the author's description — all
quoted as untrusted data, so text in the pull request asking for a particular
title is described rather than obeyed.

## `@mira describe`

Comment `@mira describe` (alias `summary`) on a pull request to regenerate now.
It always writes or refreshes the section, even in `empty_only` mode — still
without touching anything outside it — and regenerates the title only where
the rules above allow. Mira replies with what it changed. With
`description.enabled: false` it says so rather than writing the description.

## Opt-outs

`@mira ignore` in the description and the `mira-paused` label stop this
exactly as they stop reviews: no section, no title, and `@mira describe`
answers that the pull request is opted out or paused.

## Failure

Everything here runs after the review has been posted and is best-effort. A
token without permission to edit the pull request, a model that did not
answer, a provider that cannot edit: each is a warning in the log, never a
failed or delayed review.

Editing a pull request needs `pull_requests: write` on the GitHub App,
Developer access on GitLab, and write access on Forgejo — the same access
posting a review already needs.
