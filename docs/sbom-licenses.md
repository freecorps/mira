# SBOM export and license compliance

Mira already keeps an inventory of every package a repository's manifests and
lockfiles declare — it is what the OSV poller scans and what the dashboard's
*Dependencies* tab and *Packages* page show. Two features read it, and neither
calls a model:

* **SBOM export** — the inventory as a CycloneDX 1.5/1.6 or SPDX 2.3 JSON
  document, for one repository or every repository at once.
* **License policy** — when a pull request adds or bumps a dependency whose
  license your policy does not allow, Mira posts a finding on the line that
  added it.

## Where licenses come from

In this order, first answer wins:

1. **The lockfile.** `package-lock.json` records each package's `license`, and
   `composer.lock` its `license` list (alternatives, so `["MIT", "GPL-2.0"]`
   reads as `MIT OR GPL-2.0`). Indexing stores it with the package. `uv.lock`,
   `poetry.lock`, `requirements.txt`, `pyproject.toml`, `go.mod` and
   `package.json`'s dependency lists carry no license per dependency.
2. **The cache** — the `package_licenses` table in the index store, filled by
   earlier registry lookups. A license is trusted for 30 days; "the registry
   had none" for one day.
3. **The registry**, only when `licenses.lookup` is on (the default) and only
   during a pull request review or `--lookup-licenses` on the command line:

   | Ecosystem | Asked                                                                 |
   |-----------|-----------------------------------------------------------------------|
   | npm       | `registry.npmjs.org/<name>/<version>` (`license`, `licenses`)          |
   | PyPI      | `pypi.org/pypi/<name>/<version>/json` (`license_expression`, then `license`, then the `License ::` classifiers) |
   | Cargo     | `crates.io/api/v1/crates/<name>/<version>`                             |
   | Go        | `api.deps.dev/v3/systems/go/packages/<module>/versions/<version>`      |
   | Composer  | `repo.packagist.org/p2/<vendor>/<name>.json`                           |

   A version constraint (`^4.18`, `>=2.31`) names no single release, so the
   latest release is asked instead; so is a pinned version the registry does
   not have. Docker images have no license metadata and are always unknown.

Registry lookups use the same HTTP client as
[dependency release notes](dependency-updates.md), with the same posture:
only the hosts in `licenses.allowed_hosts` (the environment variable
`MIRA_LICENSES_HOSTS` wins, and `MIRA_LICENSES_HOSTS=""` contacts nothing),
HTTPS only, no redirects, hosts that resolve to private, loopback or
link-local addresses refused, the checked address pinned for the connection,
and one deadline (`timeout_seconds`) and byte allowance (`max_bytes`) for every
request of a review. At most `max_lookups` packages are looked up per review;
the rest count as unknown. `mira local review` never runs the check.

### Normalization

Everything is turned into an SPDX license expression: `MIT License` → `MIT`,
`Apache 2.0` → `Apache-2.0`, `MIT/Apache-2.0` → `MIT OR Apache-2.0`,
`License :: OSI Approved :: MIT License` → `MIT`, the deprecated `GPL-3.0` →
`GPL-3.0-only` and `GPL-2.0+` → `GPL-2.0-or-later`. Text Mira does not
recognize — `UNKNOWN`, `SEE LICENSE IN LICENSE.md`, npm's `UNLICENSED`, a whole
license text pasted into the field — is *unknown*, never a guess. One
judgement call: PyPI's `BSD License` classifier names a family, and is read as
`BSD-3-Clause`, which is what nearly every package classified that way ships.

An identifier that parses but is not on the SPDX list (`Proprietary`) is kept:
an SPDX document writes it as `LicenseRef-Proprietary` and records the
original text, and a policy with an `allow` list does not allow it.

## SBOM export

```bash
# One repository, CycloneDX 1.6 (default) to stdout
mira sbom --repo acme/api

# SPDX 2.3 to a file, asking registries for licenses the lockfiles lack
mira sbom --repo acme/api --format spdx --lookup-licenses -o api.spdx.json

# CycloneDX 1.5, every indexed repository in one document
mira sbom --all --spec-version 1.5 -o org.cdx.json

# A repository on another platform
mira sbom --repo group/project --platform gitlab
```

The CLI reads the index the server writes (`DATABASE_URL`, else the SQLite
files under `MIRA_INDEX_DIR`), so a repository must have been indexed first.

From the API (session cookie or a read-only API token):

```
GET /api/repos/{owner}/{repo}/sbom?format=cyclonedx|spdx&spec_version=1.5|1.6
GET /api/sbom?format=cyclonedx|spdx&spec_version=1.5|1.6     # every tracked repository
```

Both answer a file download (`application/vnd.cyclonedx+json` or
`application/spdx+json`). They never contact a registry — a page load should
not be as slow as the slowest registry — so licenses are those the lockfiles
recorded and the cache holds. The dashboard has **CycloneDX** and **SPDX**
download buttons on a repository's *Dependencies* tab and, for every
repository, on the *Packages* page; the dependencies table gains a *License*
column.

### What is in the document

One component per package *version*:

* A lockfile's resolved version wins over a manifest's constraint. A package
  only a manifest names keeps its constraint (CycloneDX property
  `mira:version_constraint`, SPDX `comment`) and has no version.
* **Direct or transitive.** A package some manifest declares is direct; one
  only a lockfile names is transitive, and so is a `go.mod` `// indirect`
  requirement. CycloneDX: the root component's `dependencies` entry lists the
  direct ones, and every component has a `mira:dependency` property. SPDX:
  relationships from the repository, with the scope in each package's
  `comment`.
* **Dev dependencies** (only when every manifest naming the package marks it
  dev): CycloneDX `scope: excluded` plus `mira:dev`, SPDX
  `DEV_DEPENDENCY_OF`. Everything else is `scope: required` / `DEPENDS_ON`.
* **Package URLs**: `pkg:npm/%40babel/core@7.24.0`, `pkg:pypi/requests@2.32.3`
  (PEP 503 name), `pkg:golang/github.com/pkg/errors@v0.9.1`,
  `pkg:cargo/serde@1.0.0`, `pkg:composer/monolog/monolog@3.5.0`,
  `pkg:docker/python@3.12-slim`.
* **Licenses**: CycloneDX `license.id` for a listed SPDX id, `license.name`
  otherwise, `expression` for a compound; SPDX `licenseDeclared` (with
  `licenseConcluded: NOASSERTION` — Mira did not inspect the code). Unknown is
  omitted in CycloneDX and `NOASSERTION` in SPDX. The CycloneDX property
  `mira:license_source` says where the answer came from.

An org-wide document lists each repository as a component of its own
(CycloneDX `application`, SPDX package described by the document); a library
used by several repositories appears once, with a `mira:repository` property
per repository in CycloneDX and one relationship per repository in SPDX.

## License policy

```yaml
licenses:
  enabled: true
  allow: [MIT, Apache-2.0, BSD-2-Clause, BSD-3-Clause, ISC, "MIT OR GPL-3.0-only"]
  deny: [GPL-3.0, AGPL-3.0]
  fail_on_unknown: false
  severity: warning      # or blocker
  ignore_dev: false
  ignore_packages: [our-internal-sdk]
```

How a package's expression is decided:

* An expression the policy names verbatim (`"MIT OR GPL-3.0-only"` above) is
  decided by that entry, `deny` first.
* Otherwise each identifier is allowed when no `deny` entry matches it and —
  if `allow` is not empty — some `allow` entry does. `A OR B` passes when
  either side does (you may choose the license you comply with); `A AND B`
  only when both do. A `WITH` exception is judged by its license.
* A bare GNU family in the policy (`GPL-3.0`, `LGPL-2.1`) matches both its
  `-only` and `-or-later` forms; `GPL-2.0+` matches `GPL-2.0-or-later`.
* No license at all is *unknown*: a violation only with `fail_on_unknown`.

Entries are validated when the configuration loads; one that is not a license
expression is a configuration error, not a silently ignored line.

### During review

With `licenses.enabled` and a policy (`allow`, `deny` or `fail_on_unknown`
set), a pull request that changes a manifest or lockfile is checked like the
OSV scan: each changed file is read at the head, and a package named on an
added line is one the pull request adds or bumps. A package added to
`package.json` and resolved in `package-lock.json` is one finding, placed on
the `package.json` line, judged with the lockfile's version and license.

A violation is an inline finding (category *License compliance*, confidence
1.0) naming the license and the rule it breaks. These findings skip the noise
filter and the self-critique: a policy fact is not a claim for a model to
grade, and grading it would cost a model call. They are still deduplicated
against Mira's open threads, so a later push does not repeat them. At most
`max_comments` are posted; the last one says how many more there are.

`severity: blocker` makes each violation a blocker, which is what everything
downstream already reads: with `review.status.fail_on: blocker` (the default)
the `mira/review` status goes red, and the verdict and the merge gate treat it
as they treat any other blocker. With `severity: warning` it is reported and
nothing is blocked.

The check never costs the review: an unreadable manifest, a registry that does
not answer or a broken cache is a log line and fewer findings.

### From the command line

```bash
mira licenses --repo acme/api                  # table; exit 1 on any violation
mira licenses --repo acme/api --output json --lookup-licenses
```

`mira licenses` evaluates every package in the inventory — not just a pull
request's additions — against the `licenses` section of the configuration
(`--config`), whether or not `enabled` is set, so it can gate a CI job or an
audit.

## Storage

* `package_manifests.license` — the license a lockfile recorded. Added to
  existing SQLite and Postgres databases on open; rows written before read as
  "not recorded" until the next index.
* `package_licenses (kind, name, version, license, source, fetched_at)` — the
  registry cache. In Postgres it is shared by every repository: the license
  of `lodash 4.17.21` is the same answer everywhere.
