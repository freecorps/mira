import {
  AlertTriangle,
  ChevronDown,
  ChevronRight,
  Copy,
  Download,
  RefreshCw,
  ScrollText,
  Search,
  Trash2,
  X,
} from "lucide-react"
import { Fragment, useCallback, useEffect, useMemo, useRef, useState } from "react"
import { useSearchParams } from "react-router"

import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card"
import { ConfirmButton } from "@/components/ui/confirm-button"
import { Input } from "@/components/ui/input"
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select"
import { Skeleton } from "@/components/ui/skeleton"
import { toast } from "@/components/ui/sonner"
import { api, type AppLogEntry, type AppLogLogger, type AppLogPage } from "@/lib/api"
import { useDocumentTitle } from "@/lib/hooks"
import { cn } from "@/lib/utils"

const ACTIVE_FILTER = "border-blue-500 ring-1 ring-blue-500/30"

const ALL_LOGGERS = "__all__"
const PAGE_SIZE = 200
const REFRESH_MS = 5000

const LEVELS = [
  { value: "ALL", label: "All levels" },
  { value: "DEBUG", label: "Debug and above" },
  { value: "INFO", label: "Info and above" },
  { value: "WARNING", label: "Warning and above" },
  { value: "ERROR", label: "Error and above" },
  { value: "CRITICAL", label: "Critical only" },
]

const WINDOWS = [
  { value: "1", label: "Last hour" },
  { value: "6", label: "Last 6 hours" },
  { value: "24", label: "Last 24 hours" },
  { value: "72", label: "Last 3 days" },
  { value: "168", label: "Last 7 days" },
  { value: "0", label: "Everything kept" },
]

// Level colours are semantic and deliberately loud only at the top: a page
// where INFO is coloured is a page where nothing stands out, and the reason
// somebody opened this one is almost always an ERROR.
const LEVEL_PILL: Record<string, string> = {
  CRITICAL: "border-red-500/60 bg-red-500/10 text-red-400",
  ERROR: "border-red-500/50 text-red-400",
  WARNING: "border-yellow-500/50 text-yellow-400",
  INFO: "border-zinc-500/40 text-muted-foreground",
  DEBUG: "border-zinc-500/30 text-muted-foreground/70",
}

function formatTime(epoch: number): string {
  const d = new Date(epoch * 1000)
  return d.toLocaleTimeString(undefined, { hour12: false }) + "." +
    String(d.getMilliseconds()).padStart(3, "0")
}

function formatDate(epoch: number): string {
  return new Date(epoch * 1000).toLocaleDateString(undefined, {
    month: "short",
    day: "numeric",
  })
}

/** One entry as the text the copy button produces — the same shape the
 *  server's export uses, so a single line and a whole page paste alike. */
function entryToText(e: AppLogEntry): string {
  const stamp = new Date(e.created_at * 1000).toISOString()
  const where = e.repo && e.pr_number ? `${e.repo}#${e.pr_number}` : e.repo
  const context = [e.trace_id, where].filter(Boolean).join(" ")
  const head = `${stamp} ${e.level.padEnd(8)} ${e.logger}${context ? ` [${context}]` : ""} ${e.message}`
  if (!e.traceback) return head
  const indented = e.traceback.trimEnd().split("\n").map((l) => `    ${l}`).join("\n")
  return `${head}\n${indented}`
}

async function copyText(text: string, label: string) {
  try {
    await navigator.clipboard.writeText(text)
    toast.success(label)
  } catch {
    toast.error("The browser refused clipboard access")
  }
}

export function LogsPage() {
  useDocumentTitle("Logs")

  // Filters live in the URL. A trace id read off a failed pull request is the
  // main way anybody arrives here, and putting the state in the address bar is
  // what makes "here is the link to the logs for that failure" a thing you can
  // send to a colleague rather than a list of boxes to re-tick.
  const [params, setParams] = useSearchParams()
  const level = params.get("level") ?? "INFO"
  const loggerName = params.get("logger") ?? ""
  const traceId = params.get("trace") ?? ""
  const repo = params.get("repo") ?? ""
  const hours = params.get("hours") ?? "24"
  const q = params.get("q") ?? ""

  const setParam = useCallback(
    (key: string, value: string) => {
      setParams(
        (prev) => {
          const next = new URLSearchParams(prev)
          if (value) next.set(key, value)
          else next.delete(key)
          // Any filter change invalidates the page you were on: offset 40 of
          // the old result set names nothing in the new one.
          next.delete("offset")
          return next
        },
        { replace: true },
      )
    },
    [setParams],
  )

  const offset = Math.max(0, Number(params.get("offset") ?? 0) || 0)

  // The search box is debounced against the URL rather than typed straight
  // into it: one request per keystroke over a table this size is a query per
  // character against a LIKE on two text columns.
  const [searchDraft, setSearchDraft] = useState(q)
  useEffect(() => setSearchDraft(q), [q])
  useEffect(() => {
    if (searchDraft === q) return
    const t = setTimeout(() => setParam("q", searchDraft), 300)
    return () => clearTimeout(t)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [searchDraft])

  const filters = useMemo(
    () => ({
      level,
      loggerName,
      q,
      traceId,
      repo,
      hours: Number(hours),
    }),
    [level, loggerName, q, traceId, repo, hours],
  )

  const [page, setPage] = useState<AppLogPage | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [live, setLive] = useState(false)
  const [expanded, setExpanded] = useState<Set<number>>(new Set())
  const [loggers, setLoggers] = useState<AppLogLogger[]>([])

  // Guards against a slow response for an old filter landing on top of a fast
  // one for the current filter — the classic way a log view ends up showing
  // rows that do not match the boxes above it.
  const requestSeq = useRef(0)

  const load = useCallback(
    async (opts: { quiet?: boolean } = {}) => {
      const seq = ++requestSeq.current
      if (!opts.quiet) setLoading(true)
      try {
        const result = await api.listLogs({ ...filters, limit: PAGE_SIZE, offset })
        if (seq !== requestSeq.current) return
        setPage(result)
        setError(null)
      } catch (err) {
        if (seq !== requestSeq.current) return
        setError(err instanceof Error ? err.message : "Could not load the logs")
      } finally {
        if (seq === requestSeq.current) setLoading(false)
      }
    },
    [filters, offset],
  )

  useEffect(() => {
    load()
  }, [load])

  useEffect(() => {
    api
      .listLogLoggers()
      .then(setLoggers)
      .catch(() => setLoggers([]))
  }, [])

  // Live tail polls rather than streaming. The event bus this dashboard
  // already has would mean emitting an event from inside a log handler, and a
  // handler that logs about the log it is shipping is a loop with a queue in
  // the middle of it. Polling a page that is open is the boring option and it
  // cannot feed itself.
  useEffect(() => {
    if (!live) return
    const t = setInterval(() => load({ quiet: true }), REFRESH_MS)
    return () => clearInterval(t)
  }, [live, load])

  const entries = page?.entries ?? []
  const total = page?.total ?? 0
  const hasFilters = Boolean(q || loggerName || traceId || repo) || level !== "INFO" || hours !== "24"

  const toggleRow = (id: number) =>
    setExpanded((prev) => {
      const next = new Set(prev)
      if (next.has(id)) next.delete(id)
      else next.add(id)
      return next
    })

  const copyVisible = async () => {
    try {
      const text = await api.exportLogs({ ...filters, limit: 5000 })
      if (!text.trim()) {
        toast.error("Nothing to copy — no lines match these filters")
        return
      }
      await copyText(text, `Copied ${text.split("\n").length} lines`)
    } catch (err) {
      toast.error(err instanceof Error ? err.message : "Could not build the export")
    }
  }

  const clearFilters = () => setParams(new URLSearchParams(), { replace: true })

  return (
    <div className="space-y-6 p-6">
      <div>
        <h1 className="text-2xl font-semibold tracking-tight">Logs</h1>
        <p className="text-sm text-muted-foreground">
          What Mira logged about itself, kept in your database rather than on a
          container's stdout. When a review fails it prints a trace ID — paste
          it below to pull up every line that review emitted.
        </p>
      </div>

      {page && !page.capture_enabled && (
        <Card className="border-yellow-500/40">
          <CardHeader className="flex-row items-start gap-3 space-y-0">
            <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0 text-yellow-400" />
            <div>
              <CardTitle className="text-base">Log capture is switched off</CardTitle>
              <CardDescription>
                This page will stay empty until it is switched back on. Unset{" "}
                <code className="font-mono">MIRA_LOG_CAPTURE=0</code> and restart
                Mira.
              </CardDescription>
            </div>
          </CardHeader>
        </Card>
      )}

      {page && page.capture_enabled && (page.dropped > 0 || page.write_errors > 0) && (
        <Card className="border-yellow-500/40">
          <CardHeader className="flex-row items-start gap-3 space-y-0">
            <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0 text-yellow-400" />
            <div>
              <CardTitle className="text-base">This trail has gaps</CardTitle>
              <CardDescription>
                {page.dropped > 0 && (
                  <>
                    {page.dropped.toLocaleString()} line
                    {page.dropped === 1 ? "" : "s"} were dropped because they
                    arrived faster than they could be written.{" "}
                  </>
                )}
                {page.write_errors > 0 && (
                  <>
                    {page.write_errors.toLocaleString()} batch
                    {page.write_errors === 1 ? "" : "es"} could not be stored at
                    all.{" "}
                  </>
                )}
                Reviews were never held up for this — the writer drops rather
                than blocks — but what you see below is incomplete.
              </CardDescription>
            </div>
          </CardHeader>
        </Card>
      )}

      {/* Filters */}
      <div className="flex flex-col gap-2 xl:flex-row xl:items-center">
        <div className="relative flex-1">
          <Search className="pointer-events-none absolute left-2.5 top-1/2 h-4 w-4 -translate-y-1/2 text-muted-foreground" />
          <Input
            aria-label="Search log messages"
            placeholder="Search messages and tracebacks…"
            value={searchDraft}
            onChange={(e) => setSearchDraft(e.target.value)}
            className={cn("pl-8", q && ACTIVE_FILTER)}
          />
        </div>
        <Input
          aria-label="Filter by trace ID"
          placeholder="Trace ID"
          value={traceId}
          onChange={(e) => setParam("trace", e.target.value.trim())}
          className={cn("font-mono xl:w-40", traceId && ACTIVE_FILTER)}
        />
        <Select value={level} onValueChange={(v) => setParam("level", v)}>
          <SelectTrigger className={cn("xl:w-44", level !== "INFO" && ACTIVE_FILTER)}>
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            {LEVELS.map((l) => (
              <SelectItem key={l.value} value={l.value}>
                {l.label}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
        <Select
          value={loggerName || ALL_LOGGERS}
          onValueChange={(v) => setParam("logger", v === ALL_LOGGERS ? "" : v)}
        >
          <SelectTrigger className={cn("xl:w-56", loggerName && ACTIVE_FILTER)}>
            <SelectValue placeholder="All modules" />
          </SelectTrigger>
          <SelectContent>
            <SelectItem value={ALL_LOGGERS}>All modules</SelectItem>
            {loggers.map((l) => (
              <SelectItem key={l.logger} value={l.logger}>
                {l.logger}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
        <Select value={hours} onValueChange={(v) => setParam("hours", v)}>
          <SelectTrigger className={cn("xl:w-40", hours !== "24" && ACTIVE_FILTER)}>
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            {WINDOWS.map((w) => (
              <SelectItem key={w.value} value={w.value}>
                {w.label}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
        {hasFilters && (
          <Button variant="ghost" size="sm" onClick={clearFilters} title="Clear filters">
            <X className="h-4 w-4" />
            Clear
          </Button>
        )}
      </div>

      {/* Actions */}
      <div className="flex flex-wrap items-center gap-2">
        <Button
          variant="outline"
          size="sm"
          onClick={() => load()}
          disabled={loading}
          aria-label="Refresh logs"
        >
          <RefreshCw className={cn("h-4 w-4", loading && "animate-spin")} />
          Refresh
        </Button>
        <Button
          variant={live ? "default" : "outline"}
          size="sm"
          onClick={() => setLive((v) => !v)}
          aria-pressed={live}
          title={live ? "Live — refreshing every 5s" : "Follow new lines as they arrive"}
        >
          <span
            className={cn(
              "h-2 w-2 rounded-full",
              live ? "animate-pulse bg-green-500" : "bg-muted-foreground",
            )}
          />
          {live ? "Live" : "Follow"}
        </Button>
        <Button variant="outline" size="sm" onClick={copyVisible}>
          <Copy className="h-4 w-4" />
          Copy these logs
        </Button>
        <Button variant="outline" size="sm" asChild>
          <a href={api.exportLogsUrl({ ...filters, limit: 5000 })} download>
            <Download className="h-4 w-4" />
            Download
          </a>
        </Button>
        <div className="ml-auto flex items-center gap-2">
          <span className="text-sm text-muted-foreground">
            {loading && !page
              ? "Loading…"
              : `${total.toLocaleString()} line${total === 1 ? "" : "s"} match`}
          </span>
          <ConfirmButton
            variant="outline"
            size="sm"
            destructive
            dialogTitle="Delete the whole log trail?"
            dialogDescription="Every captured line is removed, not just the ones matching the current filters. Reviews already finished are unaffected; their logs are simply gone."
            confirmLabel="Delete everything"
            onConfirm={async () => {
              await api.clearLogs()
              toast.success("Log trail cleared")
              setExpanded(new Set())
              await load()
            }}
          >
            <Trash2 className="h-4 w-4" />
            Clear
          </ConfirmButton>
        </div>
      </div>

      <Card>
        <CardContent className="p-0">
          {error ? (
            <div className="px-6 py-12 text-center text-sm text-muted-foreground">
              {error}
            </div>
          ) : loading && !page ? (
            <div className="space-y-3 px-6 py-4">
              {Array.from({ length: 8 }).map((_, i) => (
                <div key={i} className="flex items-center gap-4">
                  <Skeleton className="h-4 w-20" />
                  <Skeleton className="h-5 w-16" />
                  <Skeleton className="h-4 w-40" />
                  <Skeleton className="h-4 flex-1" />
                </div>
              ))}
            </div>
          ) : entries.length === 0 ? (
            <div className="flex flex-col items-center gap-2 px-6 py-12 text-center">
              <ScrollText className="h-8 w-8 text-muted-foreground" />
              <p className="text-sm font-medium">No log lines match</p>
              <p className="max-w-md text-sm text-muted-foreground">
                {hasFilters
                  ? "Try widening the time window or lowering the level — the default only shows the last 24 hours at info and above."
                  : "Nothing has been logged in the last 24 hours."}
              </p>
            </div>
          ) : (
            <div className="divide-y divide-border">
              {entries.map((e) => {
                const open = expanded.has(e.id)
                const hasDetail = Boolean(e.traceback)
                return (
                  <Fragment key={e.id}>
                    <div
                      className={cn(
                        "flex cursor-pointer items-start gap-3 px-4 py-2 font-mono text-xs hover:bg-muted/50",
                        e.level_no >= 40 && "bg-red-500/5",
                      )}
                      onClick={() => toggleRow(e.id)}
                    >
                      <span className="mt-0.5 w-3 shrink-0 text-muted-foreground">
                        {hasDetail ? (
                          open ? (
                            <ChevronDown className="h-3 w-3" />
                          ) : (
                            <ChevronRight className="h-3 w-3" />
                          )
                        ) : null}
                      </span>
                      <span
                        className="shrink-0 tabular-nums text-muted-foreground"
                        title={new Date(e.created_at * 1000).toString()}
                      >
                        <span className="hidden md:inline">{formatDate(e.created_at)} </span>
                        {formatTime(e.created_at)}
                      </span>
                      <Badge
                        variant="outline"
                        className={cn(
                          "h-5 shrink-0 justify-center px-1.5 text-[10px] font-semibold",
                          LEVEL_PILL[e.level] ?? LEVEL_PILL.INFO,
                        )}
                      >
                        {e.level}
                      </Badge>
                      <span
                        className="hidden w-56 shrink-0 truncate text-muted-foreground lg:inline"
                        title={`${e.logger} · ${e.func_name}:${e.lineno}`}
                      >
                        {e.logger}
                      </span>
                      <span className="min-w-0 flex-1 whitespace-pre-wrap break-words">
                        {e.message}
                      </span>
                      {e.trace_id && (
                        <button
                          type="button"
                          onClick={(ev) => {
                            ev.stopPropagation()
                            setParam("trace", e.trace_id)
                          }}
                          title={
                            e.repo
                              ? `Show every line from this review (${e.repo}#${e.pr_number})`
                              : "Show every line from this run"
                          }
                          className="shrink-0 rounded border border-border px-1.5 py-0.5 text-[10px] text-muted-foreground hover:border-blue-500 hover:text-foreground"
                        >
                          {e.trace_id}
                        </button>
                      )}
                    </div>
                    {open && (
                      <div className="space-y-2 bg-muted/30 px-4 py-3 pl-10">
                        <div className="flex flex-wrap items-center gap-x-4 gap-y-1 font-mono text-[11px] text-muted-foreground">
                          <span>
                            {e.module}.{e.func_name}:{e.lineno}
                          </span>
                          <span>thread {e.thread}</span>
                          {e.repo && (
                            <span>
                              {e.repo}
                              {e.pr_number ? `#${e.pr_number}` : ""}
                            </span>
                          )}
                          <Button
                            variant="ghost"
                            size="sm"
                            className="h-6 px-2 text-[11px]"
                            onClick={() => copyText(entryToText(e), "Entry copied")}
                          >
                            <Copy className="h-3 w-3" />
                            Copy entry
                          </Button>
                        </div>
                        {e.traceback && (
                          <pre className="overflow-x-auto whitespace-pre rounded bg-background p-3 font-mono text-[11px] leading-relaxed text-red-300/90">
                            {e.traceback}
                          </pre>
                        )}
                      </div>
                    )}
                  </Fragment>
                )
              })}
            </div>
          )}
        </CardContent>
      </Card>

      {total > PAGE_SIZE && (
        <div className="flex items-center justify-between">
          <span className="text-sm text-muted-foreground">
            Showing {offset + 1}–{Math.min(offset + entries.length, total)} of{" "}
            {total.toLocaleString()}
          </span>
          <div className="flex gap-2">
            <Button
              variant="outline"
              size="sm"
              disabled={offset === 0}
              onClick={() => setParams(
                (prev) => {
                  const next = new URLSearchParams(prev)
                  const back = Math.max(0, offset - PAGE_SIZE)
                  if (back) next.set("offset", String(back))
                  else next.delete("offset")
                  return next
                },
                { replace: true },
              )}
            >
              Newer
            </Button>
            <Button
              variant="outline"
              size="sm"
              disabled={offset + entries.length >= total}
              onClick={() => setParams(
                (prev) => {
                  const next = new URLSearchParams(prev)
                  next.set("offset", String(offset + PAGE_SIZE))
                  return next
                },
                { replace: true },
              )}
            >
              Older
            </Button>
          </div>
        </div>
      )}
    </div>
  )
}
