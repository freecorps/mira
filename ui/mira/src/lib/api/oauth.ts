import { deleteJson, fetchJson, postJson, putJson } from "./http"

// One metered window of a plan's allowance (ChatGPT: 5-hour and weekly).
export type UsageWindow = {
  used_percent: number
  window_minutes: number | null
  resets_at: number | null
  name: string
}

export type UsageSnapshot = {
  primary: UsageWindow | null
  secondary: UsageWindow | null
  credits: {
    has_credits: boolean
    unlimited: boolean
    balance: string | null
  } | null
  plan: string
  limit_reached: boolean
  // "headers" (recorded off the last review call) or "endpoint" (asked for).
  source: string
  fetched_at: number
  // Mira's own note: the backend refused this account until this time.
  exhausted_until: number
  last_used_at: number
}

// One signed-in account. Never carries token material — the backend only
// ever reports who is connected, until when, and how much of the plan is spent.
export type OAuthAccount = {
  key: string
  account_label: string
  account_id_hint: string
  plan: string
  expires_at: number
  connected_at: number
  can_refresh: boolean
  // Bare model ids go to this account specifically.
  is_default: boolean
  usage: UsageSnapshot | null
  available: boolean
}

export type OAuthProtocol = {
  api_style: string
  protocol: string
  transport: string
  endpoint: string
}

export type OAuthProvider = {
  id: string
  label: string
  description: string
  docs_url: string
  serves_models: boolean
  reports_usage: boolean
  connected: boolean
  accounts: OAuthAccount[]
  // "rotate": bare ids go to any of this provider's accounts; "pinned": to
  // one of them (its `is_default` is set); "": this provider is not the default.
  default_mode: "" | "rotate" | "pinned"
  protocol: OAuthProtocol | null
  models: { value: string; label: string; recommended?: boolean }[]
  default_model: string
  manual_exchange: boolean
}

export type OAuthStatus = {
  active_provider: string
  active_account: string
  active_ref: string
  providers: OAuthProvider[]
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

export type OAuthCompleted = OAuthAccount & {
  provider: string
  label: string
  connected: boolean
}

// Sign-in sessions for LLM providers (ChatGPT/Codex today), the accounts
// under each, and the choice of which backend serves bare model ids.
export const oauthApi = {
  getOAuthProviders: () => fetchJson<OAuthStatus>("/api/oauth/providers"),

  // No callback origin from here: where a provider may send an authorization
  // code is deployment configuration (MIRA_DASHBOARD_URL), not something the
  // page gets to name.
  startOAuth: (provider: string) =>
    postJson<OAuthStart>(`/api/oauth/${provider}/start`, {}),

  completeOAuth: (provider: string, redirect_url: string, state: string) =>
    postJson<OAuthCompleted>(`/api/oauth/${provider}/complete`, {
      redirect_url,
      state,
    }),

  refreshOAuthAccount: (provider: string, account: string) =>
    postJson<OAuthAccount>(
      `/api/oauth/${provider}/accounts/${encodeURIComponent(account)}/refresh`,
      {}
    ),

  refreshOAuthUsage: (provider: string, account: string) =>
    postJson<OAuthAccount>(
      `/api/oauth/${provider}/accounts/${encodeURIComponent(account)}/usage`,
      {}
    ),

  disconnectOAuthAccount: (provider: string, account: string) =>
    deleteJson(
      `/api/oauth/${provider}/accounts/${encodeURIComponent(account)}`
    ),

  disconnectOAuth: (provider: string) => deleteJson(`/api/oauth/${provider}`),

  // `account` "" or "*" rotates across every account of the provider;
  // a key pins one. Empty provider goes back to the API key.
  setActiveOAuth: (provider: string, account: string = "") =>
    putJson<{
      ok: boolean
      active_provider: string
      active_account: string
      active_ref: string
    }>("/api/oauth/active", { provider, account }),
}
