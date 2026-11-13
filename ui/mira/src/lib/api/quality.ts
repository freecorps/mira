import { fetchJson, postJson } from "./http"

// Review-quality measurement: escaped bugs (real-world recall) and stored
// backtest runs. Admin-only on the server; everything here is read-only —
// backtests are started from the CLI and escaped bugs come from webhooks.

export interface RecallRow {
  platform: string
  owner: string
  repo: string
  caught: number
  missed: number
  total: number
  recall: number | null
}

export interface QualitySummary {
  repos: RecallRow[]
  totals: {
    caught: number
    missed: number
    total: number
    recall: number | null
  }
}

export interface EscapedBug {
  id: string
  platform: string
  owner: string
  repo: string
  kind: "revert" | "hotfix" | string
  fix_ref: string
  fix_pr_number: number
  fix_sha: string
  fix_title: string
  fix_url: string
  original_pr_number: number
  original_pr_url: string
  original_merged_at: number
  path: string
  line_start: number
  line_end: number
  flagged: boolean
  flagged_file: boolean
  finding_ids: string[]
  link_method: string
  detected_at: number
  learning_candidate_id: number
}

export interface QualityScore {
  findings: number
  tp: number
  fp: number
  unlabelled: number
  positives: number
  positives_caught: number
  has_ground_truth: boolean
  precision: number | null
  labelled_precision: number | null
  recall: number | null
}

export interface VariantSummary {
  variant: string
  config_label: string
  prs: number
  prs_with_ground_truth: number
  errors: number
  score: QualityScore
  prompt_tokens: number
  completion_tokens: number
  cost_usd: number
  mean_latency_ms: number
}

export interface BacktestResult {
  variant: string
  pr_number: number
  pr_title: string
  pr_url: string
  score: QualityScore
  prompt_tokens: number
  completion_tokens: number
  cost_usd: number
  latency_ms: number
  error: string
  skipped_reason: string
  blocked_writes: number
}

export interface BacktestRun {
  id: string
  platform: string
  owner: string
  repo: string
  status: string
  variants: Record<string, string>
  notes: string[]
  estimated_cost_usd: number
  total_cost_usd: number
  summaries: VariantSummary[]
  created_at: number
  finished_at: number
  results?: BacktestResult[]
}

// Peer recall: review threads other bots opened on pull requests Mira also
// reviewed. `recall` is over threads the author addressed; `overlap` over all.
export interface PeerCounts {
  threads: number
  flagged: number
  addressed: number
  addressed_flagged: number
  missed_addressed: number
  overlap: number | null
  recall: number | null
}

export interface PeerRepoRun {
  platform: string
  owner: string
  repo: string
  prs_examined: number
  threads: number
  missed_and_addressed: number
  learned: number
  rules_activated: number[]
  error: string
  skipped: string
}

export interface PeerRecallSummary {
  totals: PeerCounts
  bots: (PeerCounts & { bot: string })[]
  repos: (PeerCounts & {
    platform: string
    owner: string
    repo: string
    bot: string
  })[]
  categories: (PeerCounts & { category: string })[]
  weeks: (PeerCounts & { week: string; bot: string })[]
  learning: Record<string, number>
  last_run: {
    started_at: number
    finished_at: number
    trigger: string
    repos: PeerRepoRun[]
  } | null
  running: boolean
  enabled: boolean
  positive_learning: boolean
}

export interface PeerThread {
  id: string
  platform: string
  owner: string
  repo: string
  pr_number: number
  pr_url: string
  pr_closed_at: number
  bot: string
  url: string
  path: string
  line: number
  title: string
  category: string
  addressed_reason: string
  learning_state: string
  learning_candidate_id: number
}

function seg(value: string) {
  return encodeURIComponent(value)
}

export const qualityApi = {
  getQualitySummary: () => fetchJson<QualitySummary>("/api/quality/summary"),
  listEscapedBugs: (flagged: "" | "yes" | "no" = "", limit = 100) =>
    fetchJson<{ bugs: EscapedBug[] }>(
      `/api/quality/escaped-bugs?limit=${limit}${flagged ? `&flagged=${flagged}` : ""}`
    ).then((page) => page.bugs),
  getPeerRecall: (bot = "") =>
    fetchJson<PeerRecallSummary>(
      `/api/quality/peer-recall/summary${bot ? `?bot=${encodeURIComponent(bot)}` : ""}`
    ),
  listMissedPeerThreads: (bot = "", category = "", limit = 100) => {
    const params = new URLSearchParams({ limit: String(limit) })
    if (bot) params.set("bot", bot)
    if (category) params.set("category", category)
    return fetchJson<{ threads: PeerThread[] }>(
      `/api/quality/peer-recall/missed?${params.toString()}`
    ).then((page) => page.threads)
  },
  runPeerRecall: (owner = "", repo = "") =>
    postJson<{ started: boolean }>("/api/quality/peer-recall/run", {
      owner,
      repo,
    }),
  listBacktests: (limit = 50) =>
    fetchJson<{ runs: BacktestRun[] }>(
      `/api/quality/backtests?limit=${limit}`
    ).then((page) => page.runs),
  getBacktest: (
    owner: string,
    repo: string,
    runId: string,
    platform = "github"
  ) =>
    fetchJson<{ run: BacktestRun }>(
      `/api/quality/backtests/${seg(owner)}/${seg(repo)}/${seg(runId)}?platform=${seg(platform)}`
    ).then((page) => page.run),
}
