import { fetchJson } from "./http"

// Delivery analytics: change-frequency hotspots (per repo) and DORA metrics
// with the cycle-time breakdown (review-health page).

export interface FileHotspot {
  path: string
  changes: number
  prs: number
  lines_changed: number
  loc: number
  symbols: number
  findings: number
  churn: number
  complexity: number
  finding_density: number
  score: number
  last_changed_at: number
}

export interface DirectoryHotspot {
  path: string
  files: number
  changes: number
  lines_changed: number
  findings: number
  score: number
}

export interface HotspotReport {
  enabled: boolean
  window_days: number
  total_files: number
  max_score: number
  files: FileHotspot[]
  directories: DirectoryHotspot[]
  sources: Record<string, boolean>
}

export type DoraBand = "elite" | "high" | "medium" | "low"

export interface CycleTime {
  time_to_first_review_secs: number | null
  time_to_approval_secs: number | null
  time_to_merge_secs: number | null
  coding_time_secs: number | null
  total_cycle_secs: number | null
  prs: number
}

export interface DoraWindow {
  start: number
  end: number
  deployments: number
  deployments_per_day: number
  deployment_band: DoraBand
  lead_time_secs: number | null
  lead_time_band: DoraBand | null
  change_failure_rate: number | null
  change_failure_band: DoraBand | null
  failures: number
  mttr_secs: number | null
  mttr_band: DoraBand | null
  merged_prs: number
  cycle: CycleTime
}

export interface DoraBucket {
  start: number
  deployments: number
  failures: number
  lead_time_secs: number | null
}

export interface DoraFailure {
  owner: string
  repo: string
  number: number
  title: string
  url: string
  merged_at: number
  restore_secs: number | null
}

export interface DoraReport {
  enabled: boolean
  days: number
  deployment_source: "merges" | "releases"
  current: DoraWindow | null
  previous: DoraWindow | null
  series: DoraBucket[]
  bucket_secs: number
  failures: DoraFailure[]
  repos: string[]
}

export const deliveryApi = {
  getHotspots: (owner: string, repo: string, days = 90, limit = 100) =>
    fetchJson<HotspotReport>(
      `/api/repos/${owner}/${repo}/hotspots?days=${days}&limit=${limit}`
    ),

  getDora: (days = 30, repo = "") =>
    fetchJson<DoraReport>(
      `/api/review-insights/dora?days=${days}${repo ? `&repo=${encodeURIComponent(repo)}` : ""}`
    ),
}
