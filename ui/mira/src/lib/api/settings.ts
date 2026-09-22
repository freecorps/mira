import { fetchJson, putJson } from "./http"

export type ModelOptionDto = {
  value: string
  label: string
  recommended?: boolean
  group?: string
  detail?: string
  description?: string
  reasoning_levels?: string[]
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

// The optional part of a models save: only the keys present are written.
// A chain or the budget that is absent is left as stored; `null` clears
// the dashboard's override so mira.yaml decides again. `max_tokens: 0` is
// "unlimited" — no cap on the request at all.
export type ModelsSaveExtras = {
  indexing_fallbacks?: string[] | null
  review_fallbacks?: string[] | null
  security_fallbacks?: string[] | null
  max_tokens?: number | null
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
      // What the saved review model takes, per its provider, and who says so.
      review_thinking_levels: string[]
      review_thinking_source: "provider" | "builtin"
      api_style: string
      api_style_options: ModelOptionDto[]
      // Set when a signed-in provider (Settings → Connections) is the default
      // for bare model ids. Options that name a backend explicitly
      // (`oauth:…`, `api:…`) are unaffected by it.
      oauth_provider: string
      oauth_label: string
      // Ordered fallback chains: the routes tried, in order, when the
      // purpose's model fails a call. `*_source` says whether the dashboard
      // or mira.yaml set it; `config_*` is what "inherit" would give.
      indexing_fallbacks: string[]
      review_fallbacks: string[]
      security_fallbacks: string[]
      indexing_fallbacks_source: "dashboard" | "config"
      review_fallbacks_source: "dashboard" | "config"
      security_fallbacks_source: "dashboard" | "config"
      config_indexing_fallbacks: string[]
      config_review_fallbacks: string[]
      config_security_fallbacks: string[]
      fallback_chain_limit: number
      // Output budget per call, resolved DB → config. 0 means unlimited.
      max_tokens: number
      max_tokens_source: "dashboard" | "config"
      config_max_tokens: number
    }>("/api/settings/models"),

  // `extras` carries only what changed (see ModelsSaveExtras): a key that
  // is absent leaves the stored value alone, `null` clears the dashboard's
  // override so mira.yaml decides again, and a value is stored as sent.
  saveModels: (
    indexing_model: string,
    review_model: string,
    security_model: string,
    review_thinking_mode: string = "off",
    api_style: string = "chat",
    extras: ModelsSaveExtras = {}
  ) =>
    putJson<{ ok: boolean }>("/api/settings/models", {
      indexing_model,
      review_model,
      security_model,
      review_thinking_mode,
      api_style,
      ...extras,
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
