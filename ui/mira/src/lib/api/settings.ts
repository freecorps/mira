import { fetchJson, putJson } from "./http"

export type ModelOptionDto = {
  value: string
  label: string
  recommended?: boolean
  group?: string
  detail?: string
  description?: string
}

// Where one purpose's calls actually go: the backend, the account, the
// protocol, the endpoint and the model id on the wire.
export type ModelRoute = {
  value: string
  backend: "oauth" | "api" | "bedrock"
  provider: string
  provider_label: string
  account: string
  account_label: string
  model: string
  api_style: string
  protocol: string
  transport: string
  endpoint: string
  connected: boolean
}

export type DefaultBackend = {
  provider?: string
  provider_label?: string
  account?: string
  account_label?: string
  mode?: "rotate" | "pinned" | ""
  accounts?: number
}

// Model selection, cost estimate, and admin review-config overrides.
export const settingsApi = {
  getModels: () =>
    fetchJson<{
      indexing_model: string
      review_model: string
      security_model: string
      backend: string
      indexing_route: ModelRoute
      review_route: ModelRoute
      security_route: ModelRoute
      default_backend: DefaultBackend
      indexing_source: "dashboard" | "config"
      review_source: "dashboard" | "config"
      security_source: "dashboard" | "config"
      config_indexing_model: string
      config_review_model: string
      config_security_model: string
      indexing_options: ModelOptionDto[]
      review_options: ModelOptionDto[]
      security_options: ModelOptionDto[]
      review_thinking_mode: string
      thinking_options: ModelOptionDto[]
      api_style: string
      api_style_options: ModelOptionDto[]
      // Set when a signed-in provider (Settings → Connections) is the default
      // for bare model ids. Options that name a backend explicitly
      // (`oauth:…`, `api:…`) are unaffected by it.
      oauth_provider: string
      oauth_label: string
    }>("/api/settings/models"),

  saveModels: (
    indexing_model: string,
    review_model: string,
    security_model: string,
    review_thinking_mode: string = "off",
    api_style: string = "chat"
  ) =>
    putJson<{ ok: boolean }>("/api/settings/models", {
      indexing_model,
      review_model,
      security_model,
      review_thinking_mode,
      api_style,
    }),

  getCostEstimate: () =>
    fetchJson<{
      estimated_usd: number
      input_tokens: number
      output_tokens: number
      model: string
      file_count: number
    }>("/api/indexing/estimate"),

  // The override blob mirrors the config tree rather than flattening it, so a
  // section's values are `unknown`: `review.verdict` is an object, not a scalar.
  getGlobalSettings: () =>
    fetchJson<{
      overrides: {
        filter?: Record<string, unknown>
        review?: Record<string, unknown>
      }
      effective: Record<string, unknown>
    }>("/api/admin/settings"),

  // Sections are written, not replaced-around: the endpoint writes the ones it
  // is sent and leaves the rest — `gate`, `checks`, `autofix`, `triage`, each
  // owned by its own panel — untouched. So an omitted section means "not
  // mine", and an empty one means "remove it".
  saveGlobalSettings: (overrides: Record<string, Record<string, unknown>>) =>
    putJson<{ ok: boolean }>("/api/admin/settings", { overrides }),
}
