import { fetchJson, putJson } from "./http"

// Model selection, cost estimate, and admin review-config overrides.
export const settingsApi = {
  getModels: () =>
    fetchJson<{
      indexing_model: string
      review_model: string
      security_model: string
      backend: string
      indexing_source: "dashboard" | "config"
      review_source: "dashboard" | "config"
      security_source: "dashboard" | "config"
      config_indexing_model: string
      config_review_model: string
      config_security_model: string
      indexing_options: {
        value: string
        label: string
        recommended?: boolean
      }[]
      review_options: { value: string; label: string; recommended?: boolean }[]
      security_options: {
        value: string
        label: string
        recommended?: boolean
      }[]
      review_thinking_mode: string
      thinking_options: {
        value: string
        label: string
        recommended?: boolean
      }[]
      api_style: string
      api_style_options: {
        value: string
        label: string
        recommended?: boolean
      }[]
      // Set when a signed-in account (Settings → Connections) is serving
      // reviews: the options above then come from that provider, and the
      // endpoint/protocol are fixed by it.
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
