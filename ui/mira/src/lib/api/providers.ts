import { fetchJson, postJson } from "./http"
import type { OAuthProtocol, UsageSnapshot } from "./oauth"

// One API-key endpoint Mira has a profile for (OpenRouter, OpenCode Go).
// Never carries the key: the backend reports whether one is set and, for a
// metered subscription, how much of it is spent.
export type KeyProvider = {
  id: string
  label: string
  description: string
  docs_url: string
  endpoint: string
  // The environment variable the key is read from.
  api_key_env: string
  key_configured: boolean
  // True for the endpoint `llm.base_url` points at — where bare model ids go
  // when no signed-in account is the default.
  is_endpoint: boolean
  reports_usage: boolean
  protocol: OAuthProtocol
  usage: UsageSnapshot | null
  available: boolean
}

export type KeyProviders = {
  providers: KeyProvider[]
}

export const providersApi = {
  getKeyProviders: () => fetchJson<KeyProviders>("/api/providers/keys"),

  refreshKeyProviderUsage: (provider: string) =>
    postJson<KeyProvider>(
      `/api/providers/keys/${encodeURIComponent(provider)}/usage`,
      {}
    ),
}
