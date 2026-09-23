import { deleteJson, fetchJson, postJson } from "./http"

// Read-only API tokens for agents and scripts. The token itself comes back
// from `createApiToken` and nowhere else — the server stores only a digest.
export type ApiToken = {
  id: number
  user_id: number
  username: string
  name: string
  prefix: string
  created_at: number
  expires_at: number
  last_used_at: number
  revoked_at: number
}

export type CreatedApiToken = ApiToken & { token: string }

export const tokensApi = {
  listApiTokens: (allUsers = false) =>
    fetchJson<ApiToken[]>(
      `/api/auth/tokens${allUsers ? "?all_users=true" : ""}`
    ),

  createApiToken: (body: {
    name: string
    expires_in_days: number
    user_id?: number
  }) => postJson<CreatedApiToken>("/api/auth/tokens", body),

  revokeApiToken: (id: number) => deleteJson(`/api/auth/tokens/${id}`),
}
