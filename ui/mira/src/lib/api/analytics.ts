import { API_BASE, fetchJson, postJson } from "./http"
import type {
  AnalyticsSummary,
  LearningAuditEvent,
  RegressionResponse,
  RuleAnalyticsDetail,
  RuleAnalyticsPage,
  RuleEvaluationPage,
} from "./types"

export interface RuleAnalyticsFilters {
  owner?: string
  repo?: string
  category?: string
  origin?: string
  scopeType?: string
  prAuthor?: string
  since?: number
  until?: number
  sort?: string
  order?: "asc" | "desc"
  limit?: number
  offset?: number
}

// Only non-empty values are sent: the API treats an absent parameter as "no
// filter", and an empty string would be indistinguishable from a real one.
function query(params: Record<string, string | number | undefined>) {
  const search = new URLSearchParams()
  for (const [key, value] of Object.entries(params)) {
    if (value === undefined || value === "" || value === 0) continue
    search.set(key, String(value))
  }
  const rendered = search.toString()
  return rendered ? `?${rendered}` : ""
}

function filterParams(f: RuleAnalyticsFilters) {
  return {
    owner: f.owner,
    repo: f.repo,
    category: f.category,
    origin: f.origin,
    scope_type: f.scopeType,
    pr_author: f.prAuthor,
    since: f.since,
    until: f.until,
  }
}

// Rule evaluation analytics (admin-only on the server).
export const analyticsApi = {
  listRuleAnalytics: (f: RuleAnalyticsFilters = {}) =>
    fetchJson<RuleAnalyticsPage>(
      `/api/analytics/rules${query({
        ...filterParams(f),
        sort: f.sort,
        order: f.order,
        limit: f.limit,
        offset: f.offset,
      })}`
    ),

  getRuleAnalytics: (owner: string, repo: string, ruleId: number) =>
    fetchJson<RuleAnalyticsDetail>(
      `/api/analytics/rules/${owner}/${repo}/${ruleId}`
    ),

  listRuleEvaluations: (
    owner: string,
    repo: string,
    ruleId: number,
    opts: { outcome?: string; limit?: number; offset?: number } = {}
  ) =>
    fetchJson<RuleEvaluationPage>(
      `/api/analytics/rules/${owner}/${repo}/${ruleId}/evaluations${query({
        outcome: opts.outcome,
        limit: opts.limit,
        offset: opts.offset,
      })}`
    ),

  analyticsSummary: (dimension: string, f: RuleAnalyticsFilters = {}) =>
    fetchJson<AnalyticsSummary>(
      `/api/analytics/summary${query({ dimension, ...filterParams(f) })}`
    ),

  listRegressions: (owner = "", repo = "") =>
    fetchJson<RegressionResponse>(
      `/api/analytics/regressions${query({ owner, repo })}`
    ),

  acknowledgeRegression: (
    owner: string,
    repo: string,
    ruleId: number,
    body: { action: "accepted" | "dismissed" | "deferred"; note?: string }
  ) =>
    postJson<{ ok: boolean; event_id: number }>(
      `/api/analytics/regressions/${owner}/${repo}/${ruleId}/ack`,
      { action: body.action, note: body.note ?? "" }
    ),

  listAuditEvents: (
    opts: { owner?: string; repo?: string; ruleId?: number } = {}
  ) =>
    fetchJson<{ events: LearningAuditEvent[] }>(
      `/api/analytics/audit${query({
        owner: opts.owner,
        repo: opts.repo,
        rule_id: opts.ruleId,
      })}`
    ),

  // Exports are plain links so the browser handles the download and the
  // session cookie rides along; no blob juggling in JS.
  ruleAnalyticsExportUrl: (fmt: "csv" | "json", f: RuleAnalyticsFilters = {}) =>
    `${API_BASE}/api/analytics/export${query({ fmt, ...filterParams(f) })}`,

  ruleEvaluationsExportUrl: (
    owner: string,
    repo: string,
    ruleId: number,
    fmt: "csv" | "json",
    outcome = ""
  ) =>
    `${API_BASE}/api/analytics/rules/${owner}/${repo}/${ruleId}/export${query({
      fmt,
      outcome,
    })}`,
}
