import { fetchJson, postJson, putJson } from "./http"
import type {
  GateConfigResponse,
  GateDecisionDetail,
  GateDecisionPage,
  GateOverrideResult,
  GateState,
  GateSummary,
} from "./types"

export interface GateDecisionFilters {
  owner?: string
  repo?: string
  platform?: string
  state?: GateState | ""
  mode?: string
  prNumber?: number
  prAuthor?: string
  riskBand?: string
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
// `_{platform}/{owner}`, and that value reaches the client on decision rows —
// an unencoded slash would add a path segment and miss the route entirely.
function seg(value: string | number) {
  return encodeURIComponent(String(value))
}

function filterParams(f: GateDecisionFilters) {
  return {
    owner: f.owner,
    repo: f.repo,
    platform: f.platform,
    state: f.state,
    mode: f.mode,
    pr_number: f.prNumber,
    pr_author: f.prAuthor,
    risk_band: f.riskBand,
    since: f.since,
    until: f.until,
  }
}

// Merge gate decisions and policy (admin-only on the server; the override
// route additionally requires the gate's own override permission).
export const gateApi = {
  listGateDecisions: (f: GateDecisionFilters = {}) =>
    fetchJson<GateDecisionPage>(
      `/api/gate/decisions${query({
        ...filterParams(f),
        sort: f.sort,
        order: f.order,
        limit: f.limit,
        offset: f.offset,
      })}`
    ),

  getGateSummary: (f: GateDecisionFilters = {}) =>
    fetchJson<GateSummary>(
      `/api/gate/summary${query({
        owner: f.owner,
        repo: f.repo,
        platform: f.platform,
        since: f.since,
        until: f.until,
      })}`
    ),

  getGateDecision: (owner: string, repo: string, decisionId: number) =>
    fetchJson<GateDecisionDetail>(
      `/api/gate/decisions/${seg(owner)}/${seg(repo)}/${seg(decisionId)}`
    ),

  overrideGateDecision: (
    owner: string,
    repo: string,
    decisionId: number,
    body: { new_state: GateState; reason: string; nonce?: string }
  ) =>
    postJson<GateOverrideResult>(
      `/api/gate/decisions/${seg(owner)}/${seg(repo)}/${seg(decisionId)}/override`,
      body
    ),

  getGateConfig: (owner?: string, repo?: string) =>
    fetchJson<GateConfigResponse>(`/api/gate/config${query({ owner, repo })}`),

  setGateConfig: (gate: Record<string, unknown>) =>
    putJson<{ ok: boolean; gate: Record<string, unknown> }>(
      "/api/gate/config",
      { gate }
    ),
}
