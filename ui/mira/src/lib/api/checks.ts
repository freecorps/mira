import { fetchJson, putJson } from "./http"
import type {
  CheckCatalogResponse,
  CheckMode,
  CheckOrigin,
  CheckResultPage,
  CheckRunDetail,
  CheckRunPage,
  CheckRunVerdict,
  CheckState,
  CheckSummary,
  ChecksAuditPage,
  ChecksConfigResponse,
} from "./types"

export interface CheckRunFilters {
  owner?: string
  repo?: string
  platform?: string
  verdict?: CheckRunVerdict | ""
  prNumber?: number
  prAuthor?: string
  headSha?: string
  since?: number
  until?: number
  sort?: string
  order?: "asc" | "desc"
  limit?: number
  offset?: number
  withResults?: boolean
}

export interface CheckResultFilters {
  owner?: string
  repo?: string
  platform?: string
  checkId?: string
  origin?: CheckOrigin | ""
  state?: CheckState | ""
  mode?: CheckMode | ""
  prNumber?: number
  headSha?: string
  // The filter to reach for after an incident: everything that was *not* a
  // statement about a pull request.
  incomplete?: boolean
  blocking?: boolean
  since?: number
  until?: number
  sort?: string
  order?: "asc" | "desc"
  limit?: number
  offset?: number
}

// Only non-empty values are sent: the API treats an absent parameter as "no
// filter", and an empty string would be indistinguishable from a real one.
// `false` is dropped for the same reason — `incomplete=false` would read as a
// filter for complete results, which is not what an unticked box means.
function query(params: Record<string, string | number | boolean | undefined>) {
  const search = new URLSearchParams()
  for (const [key, value] of Object.entries(params)) {
    if (value === undefined || value === "" || value === 0 || value === false) {
      continue
    }
    search.set(key, String(value))
  }
  const rendered = search.toString()
  return rendered ? `?${rendered}` : ""
}

// Owners are not path-safe. `IndexStore.open` namespaces non-GitHub owners as
// `_{platform}/{owner}`, and that value reaches the client on run rows — an
// unencoded slash would add a path segment and miss the route entirely.
function seg(value: string | number) {
  return encodeURIComponent(String(value))
}

// Pre-merge check runs, results and policy. Admin-only on the server, reads
// included: a result quotes diff lines, CI output and ticket titles across
// every repository in the install.
export const checksApi = {
  listCheckRuns: (f: CheckRunFilters = {}) =>
    fetchJson<CheckRunPage>(
      `/api/checks/runs${query({
        owner: f.owner,
        repo: f.repo,
        platform: f.platform,
        verdict: f.verdict,
        pr_number: f.prNumber,
        pr_author: f.prAuthor,
        head_sha: f.headSha,
        since: f.since,
        until: f.until,
        sort: f.sort,
        order: f.order,
        limit: f.limit,
        offset: f.offset,
        with_results: f.withResults,
      })}`
    ),

  listCheckResults: (f: CheckResultFilters = {}) =>
    fetchJson<CheckResultPage>(
      `/api/checks/results${query({
        owner: f.owner,
        repo: f.repo,
        platform: f.platform,
        check_id: f.checkId,
        origin: f.origin,
        state: f.state,
        mode: f.mode,
        pr_number: f.prNumber,
        head_sha: f.headSha,
        incomplete: f.incomplete,
        blocking: f.blocking,
        since: f.since,
        until: f.until,
        sort: f.sort,
        order: f.order,
        limit: f.limit,
        offset: f.offset,
      })}`
    ),

  getChecksSummary: (f: CheckRunFilters = {}) =>
    fetchJson<CheckSummary>(
      `/api/checks/summary${query({
        owner: f.owner,
        repo: f.repo,
        platform: f.platform,
        since: f.since,
        until: f.until,
      })}`
    ),

  getCheckRun: (owner: string, repo: string, runId: number) =>
    fetchJson<CheckRunDetail>(
      `/api/checks/runs/${seg(owner)}/${seg(repo)}/${seg(runId)}`
    ),

  getChecksCatalog: (owner?: string, repo?: string) =>
    fetchJson<CheckCatalogResponse>(
      `/api/checks/catalog${query({ owner, repo })}`
    ),

  getChecksConfig: (owner?: string, repo?: string) =>
    fetchJson<ChecksConfigResponse>(
      `/api/checks/config${query({ owner, repo })}`
    ),

  setChecksConfig: (checks: Record<string, unknown>) =>
    putJson<{ ok: boolean; checks: Record<string, unknown> }>(
      "/api/checks/config",
      { checks }
    ),

  getChecksAudit: (limit = 50, offset = 0) =>
    fetchJson<ChecksAuditPage>(
      `/api/checks/config/audit${query({ limit, offset })}`
    ),
}
