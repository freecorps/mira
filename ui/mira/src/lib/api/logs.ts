import { API_BASE, deleteJson, fetchJson, fetchText } from "./http"
import type { AppLogLogger, AppLogPage } from "./types"

export interface LogFilters {
  // "ALL" | "DEBUG" | "INFO" | "WARNING" | "ERROR" | "CRITICAL"
  level?: string
  loggerName?: string
  q?: string
  traceId?: string
  repo?: string
  // Trailing window in hours. 0 means "no lower bound".
  hours?: number
  limit?: number
  offset?: number
}

// Built once and shared by the table, the copy button and the download link,
// so the three can never disagree about what is being looked at. An export
// that quietly widens the filter is how a private repo's name reaches a public
// bug report.
function logQuery(f: LogFilters): URLSearchParams {
  const qs = new URLSearchParams()
  if (f.level) qs.set("level", f.level)
  if (f.loggerName) qs.set("logger_name", f.loggerName)
  if (f.q) qs.set("q", f.q)
  if (f.traceId) qs.set("trace_id", f.traceId)
  if (f.repo) qs.set("repo", f.repo)
  if (f.hours != null) qs.set("hours", String(f.hours))
  if (f.limit != null) qs.set("limit", String(f.limit))
  if (f.offset != null) qs.set("offset", String(f.offset))
  return qs
}

// Mira's own log output, captured into the app database so a failed review can
// be traced from the dashboard rather than from a container's stdout.
export const logsApi = {
  listLogs: (filters: LogFilters = {}) =>
    fetchJson<AppLogPage>(`/api/logs?${logQuery(filters).toString()}`),

  listLogLoggers: () => fetchJson<AppLogLogger[]>("/api/logs/loggers"),

  // Plain text, oldest first — the same bytes the download link serves.
  exportLogs: (filters: LogFilters = {}) => {
    const qs = logQuery(filters)
    qs.delete("offset")
    return fetchText(`/api/logs/export?${qs.toString()}`)
  },

  exportLogsUrl: (filters: LogFilters = {}) => {
    const qs = logQuery(filters)
    qs.delete("offset")
    return `${API_BASE}/api/logs/export?${qs.toString()}`
  },

  clearLogs: () => deleteJson("/api/logs"),
}
