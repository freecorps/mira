import { fetchJson, putJson } from "./http"
import type {
  TriageAuditPage,
  TriageConfigResponse,
  TriageRunDetail,
  TriageRunPage,
  TriageStatus,
  TriageSuggestionSummary,
} from "./types"

export interface TriageRunFilters {
  owner?: string
  repo?: string
  platform?: string
  status?: TriageStatus | ""
  prNumber?: number
  prAuthor?: string
  headSha?: string
  // Everything a given person was suggested for. Matched exactly on the
  // server, through the candidate table — a substring match would tell
  // `dana` she was suggested when the run named `dana-ops`.
  identity?: string
  // The filter to reach for after an incident: the runs where a signal did
  // not answer, which say something about Mira rather than the repository.
  degraded?: boolean
  since?: number
  until?: number
  sort?: string
  order?: "asc" | "desc"
  limit?: number
  offset?: number
}

// Only non-empty values are sent: the API reads an absent parameter as "no
// filter", and an empty string would be indistinguishable from a real one.
// `false` is dropped for the same reason — `degraded=false` would read as a
// filter for healthy runs, which is not what an unticked box means.
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

// Owners are not path-safe: `IndexStore.open` namespaces non-GitHub owners as
// `_{platform}/{owner}`, and that value reaches the client on run rows.
function seg(value: string | number) {
  return encodeURIComponent(String(value))
}

// Triage runs, the suggestion summary and the policy. Admin-only on the
// server, reads included: a run is a record of people — who owns what, who was
// suggested, and who was passed over.
export const triageApi = {
  listTriageRuns: (f: TriageRunFilters = {}) =>
    fetchJson<TriageRunPage>(
      `/api/triage/runs${query({
        owner: f.owner,
        repo: f.repo,
        platform: f.platform,
        status: f.status,
        pr_number: f.prNumber,
        pr_author: f.prAuthor,
        head_sha: f.headSha,
        identity: f.identity,
        degraded: f.degraded,
        since: f.since,
        until: f.until,
        sort: f.sort,
        order: f.order,
        limit: f.limit,
        offset: f.offset,
      })}`
    ),

  getTriageRun: (owner: string, repo: string, runId: number) =>
    fetchJson<TriageRunDetail>(
      `/api/triage/runs/${seg(owner)}/${seg(repo)}/${seg(runId)}`
    ),

  getTriageSuggestions: (f: TriageRunFilters = {}) =>
    fetchJson<TriageSuggestionSummary>(
      `/api/triage/suggestions${query({
        owner: f.owner,
        repo: f.repo,
        platform: f.platform,
        since: f.since,
        until: f.until,
      })}`
    ),

  getTriageConfig: (owner?: string, repo?: string) =>
    fetchJson<TriageConfigResponse>(
      `/api/triage/config${query({ owner, repo })}`
    ),

  setTriageConfig: (triage: Record<string, unknown>) =>
    putJson<{ ok: boolean; triage: Record<string, unknown> }>(
      "/api/triage/config",
      { triage }
    ),

  getTriageAudit: (limit = 50, offset = 0) =>
    fetchJson<TriageAuditPage>(
      `/api/triage/config/audit${query({ limit, offset })}`
    ),
}
