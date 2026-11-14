import { deleteJson, fetchJson, postJson, putJson } from "./http"
import type { OAuthProtocol, UsageSnapshot } from "./oauth"

// One endpoint reached with an API key. Never carries the key: the backend
// reports where it comes from and its last four characters, which is what
// tells two keys apart.
export type ProviderEndpoint = {
  id: string
  label: string
  // The preset it was built from ("opencode-go"), or "" for a plain URL.
  preset: string
  description: string
  docs_url: string
  endpoint: string
  // "stored" (in Mira's database), "env:VAR", or "" for no key at all.
  key_source: string
  key_hint: string
  // The variable this endpoint reads a key from, whether or not the server
  // currently exports it. The form edits this; `key_source` only says where
  // the key is coming from right now.
  key_variable: string
  key_configured: boolean
  // Bare model ids go here, unless a signed-in account outranks it.
  is_default: boolean
  // A row in the database. False for the endpoint mira.yaml names, which is
  // shown but not editable here.
  editable: boolean
  reports_usage: boolean
  // No usage API: the meters are Mira's own estimate, from the usage ledger.
  usage_estimated: boolean
  // The plans the preset meters ([] for none), the one this key is on, the
  // day it started ("" = rolling windows), and the calibration factor.
  plans: PlanOption[]
  plan: string
  plan_started: string
  cost_scale: number
  protocol: OAuthProtocol
  usage: UsageSnapshot | null
  available: boolean
  source?: string
}

export type PlanOption = { id: string; label: string }

// A starting point for a new endpoint: the URL to prefill and the variable
// the key is conventionally read from.
export type EndpointPreset = {
  id: string
  label: string
  description: string
  docs_url: string
  base_url: string
  api_key_env: string
  api_style: string
  reports_usage: boolean
  usage_estimated: boolean
  plans: PlanOption[]
}

export type ProvidersResponse = {
  endpoints: ProviderEndpoint[]
  presets: EndpointPreset[]
  active: string
  // LLM key variables that are set in the server's environment, offered so a
  // key can stay there instead of being copied into the database.
  env_candidates: string[]
  // Whether anything here can serve a review right now.
  configured: boolean
}

// `api_key` omitted leaves a stored key alone; "" clears it.
export type EndpointInput = {
  label?: string
  base_url?: string
  preset?: string
  api_style?: string
  api_key?: string
  api_key_env?: string
  model_prefix?: string
  default_model?: string
  plan?: string
  plan_started?: string
  cost_scale?: number
  make_default?: boolean
}

// One endpoint and model in the cost view.
export type CostRow = {
  endpoint: string
  endpoint_label: string
  provider: string
  model: string
  model_label: string
  calls: number
  prompt_tokens: number
  cached_tokens: number
  completion_tokens: number
  reasoning_tokens: number
  cost_usd: number
  credits: number
  unpriced_calls: number
  cache_hit_rate: number
  projected_month_usd: number
  projected_month_credits: number
  purposes: Record<string, number>
}

export type CostReport = {
  days: number
  since: number
  span_days: number
  rows: CostRow[]
  total: Omit<
    CostRow,
    | "endpoint"
    | "endpoint_label"
    | "provider"
    | "model"
    | "model_label"
    | "purposes"
  >
  ledger: { dropped: number; write_errors: number; queued: number }
  note: string
}

export type EndpointTest = {
  ok: boolean
  detail: string
  models?: number
}

export const providersApi = {
  getProviders: () => fetchJson<ProvidersResponse>("/api/providers"),

  createProvider: (body: EndpointInput) =>
    postJson<ProviderEndpoint>("/api/providers", body),

  updateProvider: (id: string, body: EndpointInput) =>
    putJson<ProviderEndpoint>(`/api/providers/${encodeURIComponent(id)}`, body),

  deleteProvider: (id: string) =>
    deleteJson(`/api/providers/${encodeURIComponent(id)}`),

  // "" hands bare model ids back to the endpoint mira.yaml names.
  setActiveProvider: (endpoint: string) =>
    putJson<{ ok: boolean; active: string; oauth_provider: string }>(
      "/api/providers/active",
      { endpoint }
    ),

  refreshProviderUsage: (id: string) =>
    postJson<ProviderEndpoint>(
      `/api/providers/${encodeURIComponent(id)}/usage`,
      {}
    ),

  // Values that may not be saved yet; `api_key` omitted uses whatever the
  // endpoint already has.
  testProvider: (body: {
    base_url?: string
    preset?: string
    api_key?: string
    api_key_env?: string
    endpoint?: string
  }) => postJson<EndpointTest>("/api/providers/test", body),

  getUsageCosts: (days: number) =>
    fetchJson<CostReport>(`/api/usage/costs?days=${days}`),
}
