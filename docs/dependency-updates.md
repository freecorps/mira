# Dependency updates and upstream release notes

A pull request that moves `requests` from 2.31 to 3.0 changes one line of
`requirements.txt`, and the code it can break is nowhere in the diff. The
reviewer is asked whether a function the repository still calls was removed,
and the only thing that knows is the package's release notes.

So when a pull request bumps a dependency, Mira reads those notes:

1. **The bump**, from the manifests themselves. Each changed manifest is
   parsed at the base and at the head of the pull request with the same
   parsers the index uses (`package.json`, `requirements*.txt`,
   `pyproject.toml`, `go.mod`, `composer.json`), and a package whose concrete
   version goes *up* is a bump. Constraints like `^4.18.0` or `>=2.31` are read
   as the version they name; `*`, git URLs, `workspace:` ranges and Go
   pseudo-versions are not versions anyone publishes notes for and are left
   out. Lockfiles and Dockerfiles are not read.
2. **The source repository**, from the registry: PyPI's JSON API
   (`project_urls`), the npm registry (`repository`), the Go module proxy
   (`Origin`, or the module path itself for `github.com/…`) and Packagist
   (`source`). Only a GitHub `owner/repo` is taken from that metadata.
3. **The notes between the two versions**: the repository's GitHub releases
   whose tag is after the old version and at most the new one — pre-releases
   only when the bump lands on one, and, in a monorepo that tags each package
   (`@scope/pkg@1.2.0`, `pkg-v1.2.0`), only this package's tags. A project
   that publishes no releases is read from `CHANGELOG.md`, `CHANGES.md`,
   `HISTORY.md`, `CHANGES.rst` or `NEWS.md` at the new version's tag, cut to
   the sections in range.
4. **A summary**: one structured call on the indexing model, for every bump
   with notes at once, returning breaking changes, deprecations and a few
   notable changes per package, each linked to its release. No notes, no call.
5. **Advisories**: when `review.osv_scan` is on, OSV.dev is asked about both
   versions, so each bump says which known vulnerabilities it fixes and which
   affect the version it moves to.

## Where it shows up

**In the review.** The summary is placed ahead of the diffs, and the review
prompt tells the model what to do with it: a changed line, the codebase context
or a file read with the tools that still uses a removed or changed API is a
finding — on the line that uses it, or on the manifest line of the bump when
the use is outside the diff. A bump is never reported merely for having
breaking changes.

**In the walkthrough**, as a *Dependency updates* section:

> **Dependency updates** — 2 bumps, 1 with breaking changes
>
> - **`requests`** `2.31.0` → `3.0.0` · PyPI · `requirements.txt` · release notes
>   - **Breaking:** `Session.mount` was removed. (notes)
>   - **Fixes:** GHSA-xxxx (high)
> - **`flask`** `2.3.0` → `3.0.0` · PyPI · `requirements.txt` · release notes
>   - No breaking changes or deprecations in the release notes

A bump whose notes could not be had is listed anyway, with the reason (no
GitHub repository in the metadata, no notes in range, did not arrive in time,
over the per-review limit).

## Release notes are untrusted

Release notes are written by the package's maintainers, and a compromised or
hostile package can write anything there. They are treated the way Mira treats
repository content:

- they reach the summariser inside `<<<MIRA-UNTRUSTED-RELEASE-NOTES>>>`
  blocks whose delimiters are stripped from the text first, under a system
  prompt that says the block is data and not instructions;
- secrets are redacted and every release, package and call is truncated;
- the summary that comes back is checked: every item is cut to one plain
  sentence (no links, HTML or `@mentions`), and a link is kept only when it is
  one of the release URLs Mira handed over;
- the review model reads the summary inside the same kind of block.

## Never at the review's expense

Everything is best effort. The lookup starts as soon as the review knows which
files changed and runs alongside the rest of preparation. The review prompt
waits for it at most `context_wait_seconds` past its own preparation, and a
lookup still running then goes on for the walkthrough, which grants it the same
grace once more when the review is done — so the review is never more than that
late twice over, and usually not at all. The whole lookup,
summary included, has a wall-clock budget (`timeout_seconds`) and a byte budget
(`max_bytes`) shared across its release-note and registry requests (the OSV
query is separate, small and bounded by its own timeout), each request has its own timeout, and
at most `max_packages` bumps are looked up. A failure anywhere — a registry
down, a rate limit, a model error — is logged and costs that bump its notes.

Responses are cached in process for six hours, and a finished answer for a bump
(`requests 2.31.0 → 3.0.0`) is reused by every review that makes the same bump.

## Network posture

The hosts contacted are exactly `allowed_hosts`:

```
pypi.org  registry.npmjs.org  proxy.golang.org  repo.packagist.org
api.github.com  raw.githubusercontent.com  api.osv.dev
```

with `api.osv.dev` asked only when `review.osv_scan` is on and the list names
it. Requests are HTTPS only, never follow redirects, and are refused for a host
that resolves to a private, loopback, link-local or reserved address — the rule
the outbound webhooks use. The connection goes to the address that was checked
rather than to a second DNS lookup, so a rebinding answer cannot slip in between,
and TLS still verifies the certificate against the host name. URLs read from registry metadata
are never requested as given.

`MIRA_DEPENDENCY_UPDATES_HOSTS` overrides the list from the environment
(comma-separated). Set it to an empty string to keep an offline install offline
without touching any repository's configuration — the same switch
`MIRA_MODELS_DEV_URL=""` is for the models.dev catalogue.

GitHub's unauthenticated API allows 60 requests an hour per IP; Mira stops
asking for the rest of a review once it is rate-limited. On a busy install, set
`github_token_env` to the name of an environment variable holding a token (a
fine-grained token with no permissions reads public releases).

## Local review

`mira local review` promises to contact the model and nothing else, so it does
**not** look up release notes unless `review.dependency_updates.local: true` is
set — in the `.mira.yaml` committed at the review's base as well as in the
effective configuration. As with the model endpoint, a change under review does
not get to decide who else hears about it. When it runs, the manifests are read
with `git show` (and from disk for the working tree), and the report notes that
the registries were contacted.

## Configuration

```yaml
review:
  dependency_updates:
    enabled: true            # server reviews
    local: false             # mira local review; must also be committed at the base
    max_packages: 8          # bumps looked up per review; the rest are listed
    timeout_seconds: 15      # the whole lookup, summary included
    request_timeout_seconds: 5
    max_bytes: 2000000       # downloaded across the whole lookup
    context_wait_seconds: 3  # how long the review prompt waits after preparation
    allowed_hosts: [pypi.org, registry.npmjs.org, proxy.golang.org,
                    repo.packagist.org, api.github.com, raw.githubusercontent.com,
                    api.osv.dev]
    github_token_env: ""     # e.g. MIRA_RELEASE_NOTES_TOKEN
```
