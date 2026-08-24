// Shared API response/request types. Re-exported from `lib/api.ts`, so
// `import { RepoListItem } from "@/lib/api"` continues to work.

export interface RepoListItem {
  owner: string
  repo: string
  platform: string
  status: string
  index_mode: string
  file_count: number
  file_count_estimate: number
  installation_id: number
  error: string
  last_indexed: string | null
}

export interface SymbolModel {
  name: string
  kind: string
  signature: string
}

export interface FileModel {
  path: string
  language: string
  summary: string
  symbols: SymbolModel[]
  imports: string[]
  loc?: number
}

export interface RepoDetail {
  owner: string
  repo: string
  file_count: number
  files: FileModel[]
  symbols_count: number
  imports_count: number
  external_refs_count: number
  lines_count: number
  last_indexed: string | null
}

export interface ImportEdge {
  source: string
  target: string
}

export interface DependentEdge {
  path: string
  dependent_path: string
}

export interface DependencyGraph {
  imports: ImportEdge[]
  dependents: DependentEdge[]
}

export interface ExternalRefModel {
  file_path: string
  kind: string
  target: string
  description: string
}

export interface PackageModel {
  name: string
  kind: string
  version: string
  file_path: string
  is_dev: boolean
}

export interface PackageSearchHit {
  owner: string
  repo: string
  name: string
  kind: string
  version: string
  file_path: string
  is_dev: boolean
}

export interface VulnerabilityModel {
  package_name: string
  ecosystem: string
  package_version: string
  cve_id: string
  summary: string
  severity: "critical" | "high" | "moderate" | "low" | "unknown"
  advisory_url: string
  fixed_in: string
  last_seen_at: number
}

export interface OrgVulnerabilityModel extends VulnerabilityModel {
  owner: string
  repo: string
}

export interface VulnerabilitySummary {
  total: number
  critical: number
  high: number
  moderate: number
  low: number
  unknown: number
}

export interface LearnedRuleModel {
  id: number
  rule_text: string
  source_signal: string
  category: string
  path_pattern: string
  sample_count: number
  active: boolean
  status: "pending" | "approved" | "rejected" | "superseded"
  created_by: string
  version: number
  scope_type: string
  scope_value: string
  origin_candidate_id: number | null
  rationale: string
  evidence_count: number
  effective_from: number
  disabled_at: number | null
  supersedes_rule_id: number | null
  updated_at: number
}

export interface OrgLearnedRuleModel extends LearnedRuleModel {
  owner: string
  repo: string
}

export interface LearningEvidence {
  feedback_id?: number
  finding_id?: string
  path?: string
  line?: number
  head_sha?: string
  finding?: string
  human_feedback?: string
}

export interface LearningCandidateModel {
  id: number
  owner: string
  repo: string
  rule_text: string
  rationale: string
  scope_type: string
  scope_value: string
  category: string
  language: string
  confidence: number
  status: "collecting" | "pending" | "approved" | "rejected" | "superseded"
  synthesizer_version: string
  evidence_ids: Array<number | string>
  positive_examples: LearningEvidence[]
  negative_examples: LearningEvidence[]
  evidence_count: number
  source_finding_id: string | null
  source_feedback_id: number | null
  created_at: number
  updated_at: number
}

export interface RepoEdgeModel {
  source_repo: string
  target_repo: string
  kind: string
  ref_count: number
}

export interface RepoGroupModel {
  name: string
  repos: string[]
  confidence: number
  evidence: string[]
}

export interface RelationshipsResponse {
  edges: RepoEdgeModel[]
  groups: RepoGroupModel[]
}

export interface RelatedRepoModel {
  repo: string
  relationship_type: string
  edge_count: number
}

export interface ReviewEventModel {
  id: number
  pr_number: number
  pr_title: string
  pr_url: string
  comments_posted: number
  blockers: number
  warnings: number
  suggestions: number
  files_reviewed: number
  lines_changed: number
  tokens_used: number
  duration_ms: number
  categories: string
  created_at: number
}

export interface ActivityEventModel extends ReviewEventModel {
  owner: string
  repo: string
  author_username: string
  author_avatar_url: string
}

export interface ActivityResponse {
  events: ActivityEventModel[]
  repos: string[]
}

export interface ReviewCommentModel {
  id: number
  review_id: number
  path: string
  line: number
  severity: string
  category: string
  title: string
  body: string
  github_comment_id: number
  created_at: number
}

export interface PRReplyModel {
  id: number
  author: string
  author_avatar_url: string
  body: string
  comment_path: string
  comment_line: number
  in_reply_to_id: number
  created_at: number
}

export interface ActivityReviewModel extends ReviewEventModel {
  reviewed_paths: string[]
  comments: ReviewCommentModel[]
}

export interface ActivityDetailModel {
  owner: string
  repo: string
  pr_number: number
  pr_title: string
  pr_url: string
  author_username: string
  author_avatar_url: string
  reviews: ActivityReviewModel[]
  replies: PRReplyModel[]
}

export interface ReviewStatsModel {
  total_reviews: number
  total_comments: number
  total_blockers: number
  total_warnings: number
  total_suggestions: number
  total_files_reviewed: number
  total_lines_changed: number
  total_tokens: number
  avg_duration_ms: number
  categories: Record<string, number>
  avg_comments_per_pr: number
}

export interface OrgStatsModel {
  total_repos: number
  total_files: number
  total_edges: number
  total_groups: number
  review_stats: ReviewStatsModel
}

export interface ReviewContextModel {
  id: number
  title: string
  content: string
  created_at: number
  updated_at: number
}

export interface OverrideModel {
  source_repo: string
  target_repo: string
  status: string
  created_at: number
}

export interface CustomEdgeModel {
  id: number
  source_repo: string
  target_repo: string
  reason: string
  created_at: number
}

export interface RuleModel {
  id: number
  title: string
  content: string
  enabled: boolean
  created_at: number
  updated_at: number
}

// ── Contributors ──

export interface ContributorListItem {
  id: number
  provider: string
  login: string
  display_name: string
  avatar_url: string
  is_bot: boolean
  prs_opened: number
  prs_merged: number
  commits: number
  reviews: number
  additions: number
  deletions: number
  last_active: number | null
  repos_touched: number
}

export interface HeatmapDay {
  day: string
  total: number
  commits: number
  prs_opened: number
  prs_merged: number
  reviews: number
}

export interface ContributorRepoBreakdown {
  owner: string
  repo: string
  commits: number
  prs_opened: number
  prs_merged: number
  reviews: number
}

export interface ReviewQuality {
  reviews: number
  blockers: number
  warnings: number
  suggestions: number
  feedback_accepted: number
  feedback_rejected: number
  accept_rate: number
}

export interface ContributorDetail {
  contributor: ContributorListItem
  heatmap: HeatmapDay[]
  repos: ContributorRepoBreakdown[]
  quality: ReviewQuality
}

export interface ContributionWindow {
  commits: number
  prs_opened: number
  prs_merged: number
  reviews: number
  additions: number
  contributors: number
}

export interface ContributorSummary {
  days: number
  current: ContributionWindow
  previous: ContributionWindow
}

export type ContributorSort =
  "commits" | "prs" | "reviews" | "recent" | "additions"
export type StatsPeriod = "day" | "week" | "month"

// ── Review insights ──

export interface ThroughputWindow {
  time_to_first_review_secs: number | null
  time_to_first_review_count: number
  time_to_merge_secs: number | null
  time_to_merge_count: number
}

export interface HealthComponent {
  key: string
  label: string
  score: number // 0–1
  detail: string
}

export interface ReviewSummary {
  days: number
  open_prs: number
  stale_prs: number
  awaiting_review: number
  merged: number
  approved_merged: number
  approvals: number
  rubber_stamps: number
  health_score: number | null
  health: HealthComponent[]
  current: ThroughputWindow
  previous: ThroughputWindow
}

export interface ReviewerStat {
  reviewer: string
  avatar_url: string
  pending: number
  reviews: number
  median_response_secs: number | null
  approvals: number
  rubber_stamps: number
  rubber_stamp_rate: number
}

export interface OpenPrReviewer {
  reviewer: string
  state: string
  requested: boolean
  responded: boolean
}

export interface OpenPr {
  owner: string
  repo: string
  number: number
  author: string
  title: string
  url: string
  draft: boolean
  created_at: number
  updated_at: number
  age_secs: number
  idle_secs: number
  reviewed: boolean
  stale: boolean
  status: string
  waiting_on: string[]
  reviewers: OpenPrReviewer[]
}

// ── Phase 3: rule evaluation analytics ──
// `unobserved` means we recorded that nobody responded. It is never folded
// into `positive`, and `not_applicable` marks a review-scoped exposure that
// produced no finding to have an outcome about.
export type RuleOutcome =
  "positive" | "negative" | "neutral" | "unobserved" | "not_applicable"

export interface RuleOutcomeCounts {
  exposures: number
  review_exposures: number
  findings: number
  observed: number
  positive: number
  negative: number
  neutral: number
  unobserved: number
  addressed: number
  thumbs_up: number
  thumbs_down: number
  reply_agree: number
  reply_disagree: number
  repeated_false_positives: number
  // Null when nobody has given a decisive signal yet — rendered as "no data",
  // never as zero, so silence is not shown as a bad score.
  acceptance_rate: number | null
  addressed_rate: number | null
  negative_rate: number | null
}

export interface RuleAnalyticsModel extends RuleOutcomeCounts {
  rule_id: number
  owner: string
  repo: string
  platform: string
  rule_text: string
  category: string
  scope_type: string
  scope_value: string
  origin: "manual" | "learned"
  version: number
  status: string
  active: boolean
  effective_from: number
  disabled_at: number | null
  first_exposure_at: number
  last_exposure_at: number
}

export interface RuleAnalyticsPage {
  rules: RuleAnalyticsModel[]
  total: number
  limit: number
  offset: number
}

export interface RuleEvaluationModel {
  id: number
  evaluation_key: string
  review_id: number
  rule_id: number
  rule_version: number
  rule_origin: string
  scope_type: string
  scope_value: string
  category: string
  decision: string
  finding_id: string | null
  platform: string
  owner: string
  repo: string
  pr_number: number
  pr_author: string
  head_sha: string
  created_at: number
  finding_title: string
  finding_path: string
  finding_line: number
  finding_severity: string
  finding_state: string
  pr_url: string
  outcome: RuleOutcome
  addressed: boolean
  thumbs_up: number
  thumbs_down: number
  reply_agree: number
  reply_disagree: number
}

export interface RuleEvaluationPage {
  evaluations: RuleEvaluationModel[]
  total: number
  limit: number
  offset: number
}

// Repeat detection needs per-rule grouping a bucket does not have, so the
// server drops the key from summary rows. Omit it here too, rather than let a
// read type-check and come back undefined.
export interface AnalyticsBucket extends Omit<
  RuleOutcomeCounts,
  "repeated_false_positives"
> {
  bucket: string
}

export interface AnalyticsSummary {
  dimension: string
  buckets: AnalyticsBucket[]
}

export interface PeriodStats extends RuleOutcomeCounts {
  start: number
  end: number
}

export interface PeriodComparison {
  rule_id: number
  owner: string
  repo: string
  window_days: number
  activated_at: number | null
  // False when the rule has no activation timestamp, the rule row is gone, or
  // the "after" window has not finished filling. The UI must say so rather
  // than present a partial window as a verdict.
  comparable: boolean
  reason: string
  before: PeriodStats | null
  after: PeriodStats | null
  delta?: {
    findings: number
    negative: number
    positive: number
    acceptance_rate: number | null
    addressed_rate: number | null
    negative_rate: number | null
  }
}

export interface RegressionSuggestion {
  rule_id: number
  owner: string
  repo: string
  action: "downgrade" | "disable"
  reason: string
  exposures: number
  negative_rate: number
  addressed_rate: number | null
  min_exposures: number
}

export interface RegressionResponse {
  suggestions: RegressionSuggestion[]
  min_exposures: number
  negative_rate_threshold: number
  disable_rate_threshold: number
}

export interface RuleAnalyticsDetail {
  rule: RuleAnalyticsModel
  period_comparison: PeriodComparison
  regression: RegressionSuggestion | null
  min_exposures_for_regression: number
}

export interface LearningAuditEvent {
  id: number
  event_type: string
  rule_id: number
  actor: string
  summary: string
  detail_json: string
  created_at: number
  owner: string
  repo: string
}

// ── Phase 4: the risk-oriented merge gate ────────────────────────────────
//
// `would_approve` is never rendered as an approval. It means the gate reached
// the same conclusion it would have acted on, and deliberately did not act:
// shadow mode, or a platform that cannot record one.
export type GateState =
  | "approved"
  | "would_approve"
  | "not_approved"
  | "skipped"
  | "error"

export type GateMode = "off" | "shadow" | "enforce"

export interface GateReason {
  code: string
  message: string
  // "skip" — out of scope; "block" — disqualifying; "info" — context only.
  kind: "skip" | "block" | "info"
}

export interface GateRiskFactor {
  code: string
  label: string
  points: number
  detail: string
}

export interface GateCIState {
  state: string
  total: number
  failing: string[]
  pending: string[]
}

export interface GateInputs {
  platform: string
  owner: string
  repo: string
  pr_number: number
  pr_url: string
  pr_author: string
  base_branch: string
  head_branch: string
  head_sha: string
  draft: boolean
  labels: string[]
  author_association: string
  changed_paths: string[]
  changed_files: number
  added_lines: number
  deleted_lines: number
  generated_paths: string[]
  protected_matches: string[]
  codeowner_matches: string[]
  codeowners_status: string
  ci: GateCIState
  open_blockers: number
  open_findings: number
  worst_severity: string
  review_complete: boolean
  review_skipped_paths: string[]
  review_failed: string
  index_ready: boolean
  human_states: Record<string, string>
  review_id: number
}

export interface GateDecisionModel {
  id: number
  decision_key: string
  state: GateState
  mode: GateMode
  risk_score: number
  risk_band: string
  policy_version: string
  reasons: GateReason[]
  factors: GateRiskFactor[]
  inputs: GateInputs
  capabilities: Record<string, unknown>
  request_changes: boolean
  delivery_state: string
  delivery_ref: string
  delivery_attempts: number
  error: string
  created_at: number
  updated_at: number
  platform: string
  owner: string
  repo: string
  pr_number: number
  pr_url: string
  pr_author: string
  head_sha: string
  would_have_approved?: boolean
}

export interface GateDecisionPage {
  decisions: GateDecisionModel[]
  total: number
  limit: number
  offset: number
}

export interface GateSummaryBucket {
  state: GateState
  mode: GateMode
  count: number
  approved: number
  average_risk: number
}

export interface GateSummary {
  buckets: GateSummaryBucket[]
  totals: Record<string, number>
}

export interface GateOverride {
  id: number
  override_key: string
  decision_id: number
  decision_key: string
  platform: string
  owner: string
  repo: string
  pr_number: number
  head_sha: string
  actor: string
  reason: string
  previous_state: string
  new_state: string
  previous_risk: number
  detail: Record<string, unknown>
  created_at: number
}

export interface GateDecisionDetail {
  decision: GateDecisionModel
  public_explanation: string
  admin_explanation: string
  overrides: GateOverride[]
  policy: Record<string, unknown>
}

export interface GateOverrideResult {
  ok: boolean
  created: boolean
  decision: GateDecisionModel
  override: GateOverride
}

export interface GateConfigResponse {
  // The full gate config as the server resolved it (defaults + mira.yaml + DB).
  config: Record<string, unknown>
  // Just the admin-editable override blob, which is what the panel writes back.
  overrides: Record<string, unknown>
  // What a pull request will actually meet, per repository.
  effective: Record<string, unknown>
}
