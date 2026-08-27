import { fetchJson, postJson, putJson } from "./http"
import type {
  AutofixCancelResult,
  AutofixConfigResponse,
  AutofixJobDetail,
  AutofixJobPage,
  AutofixJobState,
  AutofixMode,
  AutofixSummary,
} from "./types"

export interface AutofixJobFilters {
  owner?: string
  repo?: string
  platform?: string
  state?: AutofixJobState | ""
  mode?: AutofixMode | ""
  prNumber?: number
  requestedBy?: string
  findingId?: string
  requestId?: string
  since?: number
  until?: number
  sort?: string
  order?: "asc" | "desc"
  limit?: number
  offset?: number
}

// Only non-empty values are sent: the API treats an absent parameter as "no
// filter", and an empty string would be indistinguishable from a real one.
function query(params: Record<string, string | number | undefined>) {
  const search = new URLSearchParams()
  for (const [key, value] of Object.entries(params)) {
    if (value === undefined || value === "" || value === 0) continue
    search.set(key, String(value))
  }
  const rendered = search.toString()
  return rendered ? `?${rendered}` : ""
}

// Owners are not path-safe. `IndexStore.open` namespaces non-GitHub owners as
// `_{platform}/{owner}`, and that value reaches the client on job rows — an
// unencoded slash would add a path segment and miss the route entirely.
function seg(value: string | number) {
  return encodeURIComponent(String(value))
}

function filterParams(f: AutofixJobFilters) {
  return {
    owner: f.owner,
    repo: f.repo,
    platform: f.platform,
    state: f.state,
    mode: f.mode,
    pr_number: f.prNumber,
    requested_by: f.requestedBy,
    finding_id: f.findingId,
    request_id: f.requestId,
    since: f.since,
    until: f.until,
  }
}

// Autofix jobs and policy (admin-only on the server; the cancel route
// additionally requires the cancel permission).
//
// There is deliberately no `requestFix` here: a fix is requested from the pull
// request by an account whose write permission the platform confirmed. Adding
// one would make "can log into Mira" and "can commit to this repository" the
// same permission.
export const autofixApi = {
  listAutofixJobs: (f: AutofixJobFilters = {}) =>
    fetchJson<AutofixJobPage>(
      `/api/autofix/jobs${query({
        ...filterParams(f),
        sort: f.sort,
        order: f.order,
        limit: f.limit,
        offset: f.offset,
      })}`
    ),

  getAutofixSummary: (f: AutofixJobFilters = {}) =>
    fetchJson<AutofixSummary>(
      `/api/autofix/summary${query({
        owner: f.owner,
        repo: f.repo,
        platform: f.platform,
        since: f.since,
        until: f.until,
      })}`
    ),

  getAutofixJob: (owner: string, repo: string, jobId: number) =>
    fetchJson<AutofixJobDetail>(
      `/api/autofix/jobs/${seg(owner)}/${seg(repo)}/${seg(jobId)}`
    ),

  cancelAutofixJob: (
    owner: string,
    repo: string,
    jobId: number,
    body: { reason: string }
  ) =>
    postJson<AutofixCancelResult>(
      `/api/autofix/jobs/${seg(owner)}/${seg(repo)}/${seg(jobId)}/cancel`,
      body
    ),

  getAutofixConfig: (owner?: string, repo?: string) =>
    fetchJson<AutofixConfigResponse>(
      `/api/autofix/config${query({ owner, repo })}`
    ),

  setAutofixConfig: (autofix: Record<string, unknown>) =>
    putJson<{ ok: boolean; autofix: Record<string, unknown> }>(
      "/api/autofix/config",
      { autofix }
    ),
}
