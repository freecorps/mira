import { fetchJson, postJson } from "./http"

// Digests: what landed on each repository's default branch, per area.

export interface DigestListItem {
  id: number
  kind: string
  platform: string
  owner: string
  repo: string // "" for an org-wide digest
  period_start: number
  period_end: number
  title: string
  created_at: number
}

export interface DigestChange {
  kind: "pr" | "commit"
  ref: string
  number: number
  sha: string
  title: string
  url: string
  author: string
  landed_at: number
  labels: string[]
  repo: string
}

export interface DigestArea {
  name: string
  summary: string
  highlights: string[]
  changes: DigestChange[]
}

export interface DigestData {
  title: string
  branch: string
  overview: string
  pull_requests: number
  direct_commits: number
  notes: string[]
  llm_used: boolean
  areas: DigestArea[]
}

export interface DigestDetail extends DigestListItem {
  markdown: string
  data: DigestData
}

export const digestsApi = {
  listDigests: (params: { limit?: number; offset?: number } = {}) => {
    const query = new URLSearchParams({
      limit: String(params.limit ?? 50),
      offset: String(params.offset ?? 0),
    })
    return fetchJson<{
      digests: DigestListItem[]
      total: number
      limit: number
      offset: number
    }>(`/api/digests?${query}`)
  },

  getDigest: (id: number) => fetchJson<DigestDetail>(`/api/digests/${id}`),

  generateDigest: (body: {
    platform: string
    owner: string
    repo: string
    days: number
    deliver: boolean
  }) => postJson<DigestDetail>("/api/digests/generate", body),
}
