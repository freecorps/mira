import { deleteJson, fetchJson, postJson, putJson } from "./http"

// One OAuth provider's connection state. Never carries token material — the
// backend only ever reports who is connected and until when.
export type OAuthProvider = {
  id: string
  label: string
  description: string
  docs_url: string
  serves_models: boolean
  connected: boolean
  account_label: string
  plan: string
  expires_at: number
  connected_at: number
  can_refresh: boolean
  models: { value: string; label: string; recommended?: boolean }[]
  default_model: string
}

export type OAuthStart = {
  provider: string
  label: string
  authorization_url: string
  state: string
  redirect_uri: string
  redirect_mode: string
  // True when the browser can't hand the code back to us and the user has to
  // paste the URL they landed on.
  manual_exchange: boolean
  expires_in: number
}

// Sign-in sessions for LLM providers (ChatGPT/Codex today) and the choice of
// which one serves reviews.
export const oauthApi = {
  getOAuthProviders: () =>
    fetchJson<{ active_provider: string; providers: OAuthProvider[] }>(
      "/api/oauth/providers"
    ),

  startOAuth: (provider: string) =>
    postJson<OAuthStart>(`/api/oauth/${provider}/start`, {
      dashboard_origin: window.location.origin,
    }),

  completeOAuth: (provider: string, redirect_url: string, state: string) =>
    postJson<OAuthProvider>(`/api/oauth/${provider}/complete`, {
      redirect_url,
      state,
    }),

  refreshOAuth: (provider: string) =>
    postJson<OAuthProvider>(`/api/oauth/${provider}/refresh`, {}),

  disconnectOAuth: (provider: string) => deleteJson(`/api/oauth/${provider}`),

  setActiveOAuth: (provider: string) =>
    putJson<{ ok: boolean; active_provider: string }>("/api/oauth/active", {
      provider,
    }),
}
