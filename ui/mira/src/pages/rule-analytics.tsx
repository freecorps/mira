import {
  AlertTriangle,
  ArrowLeft,
  BarChart3,
  Download,
  ExternalLink,
  Info,
  RefreshCw,
  Search,
  ThumbsDown,
  ThumbsUp,
} from "lucide-react"
import { useMemo, useState } from "react"
import { useSearchParams } from "react-router"

import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Card, CardContent } from "@/components/ui/card"
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
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table"
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs"
import { api } from "@/lib/api"
import type {
  RuleAnalyticsModel,
  RuleEvaluationModel,
  RuleOutcome,
} from "@/lib/api"
import { useAsync, useDocumentTitle } from "@/lib/hooks"
import { cn } from "@/lib/utils"

const ALL = "__all__"
const PAGE_SIZE = 25
const ACTIVE_FILTER = "border-blue-500 ring-1 ring-blue-500/30"

const OUTCOME_LABEL: Record<RuleOutcome, string> = {
  positive: "Agreed",
  negative: "Disagreed",
  neutral: "Discussed",
  unobserved: "No response",
  not_applicable: "No finding",
}

const OUTCOME_STYLE: Record<RuleOutcome, string> = {
  positive: "bg-emerald-500/10 text-emerald-600 dark:text-emerald-400",
  negative: "bg-red-500/10 text-red-600 dark:text-red-400",
  neutral: "bg-blue-500/10 text-blue-600 dark:text-blue-400",
  unobserved: "bg-muted text-muted-foreground",
  not_applicable: "bg-muted text-muted-foreground",
}

const SORT_OPTIONS = [
  { value: "exposures", label: "Most exposures" },
  { value: "negative", label: "Most disagreement" },
  { value: "positive", label: "Most agreement" },
  { value: "findings", label: "Most findings" },
  { value: "last_exposure_at", label: "Most recent" },
]

const SUMMARY_DIMENSIONS = [
  { value: "category", label: "Category" },
  { value: "repo", label: "Repository" },
  { value: "author", label: "PR author" },
  { value: "scope_type", label: "Scope" },
  { value: "origin", label: "Origin" },
]

/** A rate of null means nobody has given a decisive signal.
 *
 * That is rendered as an em dash, never as 0%. Showing zero would turn "we
 * have no evidence" into "this rule scored badly", which is exactly the
 * conversion this whole feature exists to prevent. */
function rate(value: number | null | undefined) {
  if (value === null || value === undefined) return "—"
  return `${Math.round(value * 100)}%`
}

function timestamp(seconds: number | null | undefined) {
  if (!seconds) return "—"
  return new Date(seconds * 1000).toLocaleDateString()
}

function OutcomeBadge({ outcome }: { outcome: RuleOutcome }) {
  return (
    <Badge
      variant="secondary"
      className={cn("font-normal", OUTCOME_STYLE[outcome])}
    >
      {OUTCOME_LABEL[outcome]}
    </Badge>
  )
}

function EmptyState({ title, hint }: { title: string; hint: string }) {
  return (
    <div className="flex flex-col items-center gap-2 py-14 text-center">
      <BarChart3 className="size-8 text-muted-foreground/50" />
      <p className="font-medium">{title}</p>
      <p className="max-w-md text-sm text-muted-foreground">{hint}</p>
    </div>
  )
}

function ErrorState({
  message,
  onRetry,
}: {
  message: string
  onRetry: () => void
}) {
  return (
    <div className="flex flex-col items-center gap-3 py-14 text-center">
      <AlertTriangle className="size-8 text-red-500/70" />
      <p className="font-medium">Couldn't load analytics</p>
      <p className="max-w-md text-sm text-muted-foreground">{message}</p>
      <Button variant="outline" size="sm" onClick={onRetry}>
        <RefreshCw className="size-4" /> Try again
      </Button>
    </div>
  )
}

function TableSkeleton({ rows = 6 }: { rows?: number }) {
  return (
    <div className="space-y-3 p-4">
      {Array.from({ length: rows }).map((_, index) => (
        <div key={index} className="flex items-center gap-4">
          <Skeleton className="h-4 flex-1" />
          <Skeleton className="hidden h-4 w-20 md:block" />
          <Skeleton className="h-4 w-16" />
          <Skeleton className="h-4 w-16" />
        </div>
      ))}
    </div>
  )
}

/** Counts, with the neutral buckets always visible.
 *
 * `No response` sits next to agreement and disagreement on purpose: hiding it
 * is what makes a rule with three thumbs-up and ninety silences look loved. */
function OutcomeBar({ rule }: { rule: RuleAnalyticsModel }) {
  const segments = [
    { key: "positive", value: rule.positive, className: "bg-emerald-500" },
    { key: "negative", value: rule.negative, className: "bg-red-500" },
    { key: "neutral", value: rule.neutral, className: "bg-blue-500" },
    {
      key: "unobserved",
      value: rule.unobserved,
      className: "bg-muted-foreground/30",
    },
  ]
  const total = segments.reduce((sum, s) => sum + s.value, 0)
  if (!total) {
    return (
      <span className="text-xs text-muted-foreground">No findings yet</span>
    )
  }
  return (
    <div className="flex h-2 w-full min-w-24 overflow-hidden rounded-full">
      {segments.map((segment) =>
        segment.value ? (
          <div
            key={segment.key}
            className={segment.className}
            style={{ width: `${(segment.value / total) * 100}%` }}
            title={`${OUTCOME_LABEL[segment.key as RuleOutcome]}: ${segment.value}`}
          />
        ) : null
      )}
    </div>
  )
}

function Stat({
  label,
  value,
  hint,
}: {
  label: string
  value: string | number
  hint?: string
}) {
  return (
    <Card>
      <CardContent className="p-4">
        <p className="text-xs text-muted-foreground">{label}</p>
        <p className="mt-1 text-2xl font-semibold tabular-nums">{value}</p>
        {hint ? (
          <p className="mt-1 text-xs text-muted-foreground">{hint}</p>
        ) : null}
      </CardContent>
    </Card>
  )
}

function RuleDetail({
  owner,
  repo,
  ruleId,
  onBack,
}: {
  owner: string
  repo: string
  ruleId: number
  onBack: () => void
}) {
  const [reload, setReload] = useState(0)
  const [outcome, setOutcome] = useState<string>(ALL)
  const [page, setPage] = useState(0)

  const {
    data: detail,
    loading,
    error,
  } = useAsync(
    () => api.getRuleAnalytics(owner, repo, ruleId),
    [owner, repo, ruleId, reload]
  )
  const {
    data: evaluations,
    loading: loadingEvaluations,
    error: evaluationsError,
  } = useAsync(
    () =>
      api.listRuleEvaluations(owner, repo, ruleId, {
        outcome: outcome === ALL ? "" : outcome,
        limit: PAGE_SIZE,
        offset: page * PAGE_SIZE,
      }),
    [owner, repo, ruleId, outcome, page, reload]
  )

  async function acknowledge(action: "accepted" | "dismissed" | "deferred") {
    try {
      await api.acknowledgeRegression(owner, repo, ruleId, { action })
      toast.success(`Suggestion ${action}`, {
        description:
          "Recorded in the audit log. The rule itself is unchanged — change it from the Learnings page.",
      })
      setReload((n) => n + 1)
    } catch (err) {
      toast.error("Couldn't record the decision", {
        description: err instanceof Error ? err.message : String(err),
      })
    }
  }

  if (loading) return <TableSkeleton rows={8} />
  if (error)
    return (
      <ErrorState message={error} onRetry={() => setReload((n) => n + 1)} />
    )
  if (!detail)
    return (
      <EmptyState title="Rule not found" hint="It may have been deleted." />
    )

  const rule = detail.rule
  const comparison = detail.period_comparison
  const total = evaluations?.total ?? 0

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center gap-2">
        <Button variant="ghost" size="sm" onClick={onBack}>
          <ArrowLeft className="size-4" /> All rules
        </Button>
        <Badge variant={rule.origin === "manual" ? "default" : "secondary"}>
          {rule.origin === "manual" ? "Manual rule" : "Learned rule"}
        </Badge>
        <Badge variant="outline">v{rule.version}</Badge>
        <Badge variant="outline">
          {rule.scope_type}
          {rule.scope_value ? `=${rule.scope_value}` : ""}
        </Badge>
        {!rule.active ? <Badge variant="destructive">Inactive</Badge> : null}
        <div className="ml-auto flex gap-2">
          <Button variant="outline" size="sm" asChild>
            <a href={api.ruleEvaluationsExportUrl(owner, repo, ruleId, "csv")}>
              <Download className="size-4" /> CSV
            </a>
          </Button>
          <Button variant="outline" size="sm" asChild>
            <a href={api.ruleEvaluationsExportUrl(owner, repo, ruleId, "json")}>
              <Download className="size-4" /> JSON
            </a>
          </Button>
        </div>
      </div>

      <Card>
        <CardContent className="p-4">
          <p className="text-sm">
            {rule.rule_text || <em>Rule text unavailable</em>}
          </p>
          <p className="mt-2 text-xs text-muted-foreground">
            {owner}/{repo} · rule #{rule.rule_id} · activated{" "}
            {timestamp(rule.effective_from)}
          </p>
        </CardContent>
      </Card>

      <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
        <Stat
          label="Exposures"
          value={rule.exposures}
          hint={`${rule.findings} findings · ${rule.review_exposures} reviews with no finding`}
        />
        <Stat
          label="Agreement"
          value={rate(rule.acceptance_rate)}
          hint={
            rule.positive + rule.negative === 0
              ? "No decisive feedback yet"
              : `${rule.positive} agreed · ${rule.negative} disagreed`
          }
        />
        <Stat
          label="Addressed"
          value={rate(rule.addressed_rate)}
          hint={`${rule.addressed} of ${rule.findings} findings resolved`}
        />
        <Stat
          label="No response"
          value={rule.unobserved}
          hint="Counted as neutral — never as approval"
        />
      </div>

      {detail.regression ? (
        <Card className="border-amber-500/40">
          <CardContent className="flex flex-wrap items-start gap-3 p-4">
            <AlertTriangle className="mt-0.5 size-5 shrink-0 text-amber-500" />
            <div className="min-w-56 flex-1">
              <p className="font-medium">
                Suggested:{" "}
                {detail.regression.action === "disable"
                  ? "disable"
                  : "downgrade"}{" "}
                this rule
              </p>
              <p className="text-sm text-muted-foreground">
                {detail.regression.reason}
              </p>
              <p className="mt-1 text-xs text-muted-foreground">
                Mira never disables a rule on its own. Recording a decision here
                writes an audit entry; change the rule itself from the Learnings
                page.
              </p>
            </div>
            <div className="flex gap-2">
              <Button
                size="sm"
                variant="outline"
                onClick={() => acknowledge("accepted")}
              >
                Accept
              </Button>
              <Button
                size="sm"
                variant="outline"
                onClick={() => acknowledge("deferred")}
              >
                Defer
              </Button>
              <Button
                size="sm"
                variant="ghost"
                onClick={() => acknowledge("dismissed")}
              >
                Dismiss
              </Button>
            </div>
          </CardContent>
        </Card>
      ) : null}

      <Card>
        <CardContent className="p-4">
          <div className="flex items-center gap-2">
            <h3 className="font-medium">
              {comparison.window_days} days before and after activation
            </h3>
            {!comparison.comparable ? (
              <Badge variant="outline" className="gap-1 font-normal">
                <Info className="size-3" /> Not yet comparable
              </Badge>
            ) : null}
          </div>
          {comparison.reason ? (
            <p className="mt-1 text-xs text-muted-foreground">
              {comparison.reason}
            </p>
          ) : null}
          {comparison.before && comparison.after ? (
            <Table className="mt-3">
              <TableHeader>
                <TableRow>
                  <TableHead>Window</TableHead>
                  <TableHead className="text-right">Findings</TableHead>
                  <TableHead className="text-right">Agreed</TableHead>
                  <TableHead className="text-right">Disagreed</TableHead>
                  <TableHead className="text-right">No response</TableHead>
                  <TableHead className="text-right">Addressed</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {[
                  { label: "Before", stats: comparison.before },
                  { label: "After", stats: comparison.after },
                ].map(({ label, stats }) => (
                  <TableRow key={label}>
                    <TableCell className="font-medium">{label}</TableCell>
                    <TableCell className="text-right tabular-nums">
                      {stats.findings}
                    </TableCell>
                    <TableCell className="text-right tabular-nums">
                      {stats.positive}
                    </TableCell>
                    <TableCell className="text-right tabular-nums">
                      {stats.negative}
                    </TableCell>
                    <TableCell className="text-right tabular-nums">
                      {stats.unobserved}
                    </TableCell>
                    <TableCell className="text-right tabular-nums">
                      {rate(stats.addressed_rate)}
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          ) : (
            <p className="mt-3 text-sm text-muted-foreground">
              No comparison available for this rule.
            </p>
          )}
        </CardContent>
      </Card>

      <Card>
        <CardContent className="p-0">
          <div className="flex flex-wrap items-center gap-2 border-b p-4">
            <h3 className="font-medium">Evidence</h3>
            <span className="text-sm text-muted-foreground">
              {total} evaluation{total === 1 ? "" : "s"}
            </span>
            <Select
              value={outcome}
              onValueChange={(value) => {
                setOutcome(value)
                setPage(0)
              }}
            >
              <SelectTrigger
                size="sm"
                className={cn("ml-auto w-44", outcome !== ALL && ACTIVE_FILTER)}
                aria-label="Filter by outcome"
              >
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value={ALL}>All outcomes</SelectItem>
                {(Object.keys(OUTCOME_LABEL) as RuleOutcome[]).map((value) => (
                  <SelectItem key={value} value={value}>
                    {OUTCOME_LABEL[value]}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </div>

          {loadingEvaluations ? (
            <TableSkeleton />
          ) : evaluationsError ? (
            // An unreachable audit trail must never render as an empty one --
            // that would present a failed request as proof the count is zero.
            <ErrorState
              message={evaluationsError}
              onRetry={() => setReload((n) => n + 1)}
            />
          ) : !evaluations || evaluations.evaluations.length === 0 ? (
            <EmptyState
              title="No evaluations match"
              hint="Every aggregate above is built from these rows, so an empty list here means the number above is zero too."
            />
          ) : (
            <>
              <Table>
                <TableHeader>
                  <TableRow>
                    <TableHead>Finding</TableHead>
                    <TableHead className="hidden md:table-cell">PR</TableHead>
                    <TableHead className="hidden lg:table-cell">
                      Author
                    </TableHead>
                    <TableHead>Outcome</TableHead>
                    <TableHead className="text-right">Reactions</TableHead>
                    <TableHead className="hidden text-right sm:table-cell">
                      When
                    </TableHead>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {evaluations.evaluations.map((row: RuleEvaluationModel) => (
                    <TableRow key={row.id}>
                      <TableCell className="max-w-80">
                        {row.finding_id ? (
                          <>
                            <p className="truncate font-medium">
                              {row.finding_title}
                            </p>
                            <p className="truncate text-xs text-muted-foreground">
                              {row.finding_path}
                              {row.finding_line
                                ? `:${row.finding_line}`
                                : ""} · {row.finding_state}
                            </p>
                          </>
                        ) : (
                          <p className="text-sm text-muted-foreground">
                            Exposed to the review; produced no finding
                          </p>
                        )}
                      </TableCell>
                      <TableCell className="hidden md:table-cell">
                        {row.pr_url ? (
                          <a
                            className="inline-flex items-center gap-1 text-sm hover:underline"
                            href={row.pr_url}
                            target="_blank"
                            rel="noreferrer"
                          >
                            #{row.pr_number} <ExternalLink className="size-3" />
                          </a>
                        ) : (
                          <span className="text-sm text-muted-foreground">
                            #{row.pr_number}
                          </span>
                        )}
                      </TableCell>
                      <TableCell className="hidden text-sm lg:table-cell">
                        {row.pr_author || "—"}
                      </TableCell>
                      <TableCell>
                        <OutcomeBadge outcome={row.outcome} />
                      </TableCell>
                      <TableCell className="text-right text-sm tabular-nums">
                        <span className="inline-flex items-center gap-2">
                          <span className="inline-flex items-center gap-1">
                            <ThumbsUp className="size-3" />
                            {row.thumbs_up}
                          </span>
                          <span className="inline-flex items-center gap-1">
                            <ThumbsDown className="size-3" />
                            {row.thumbs_down}
                          </span>
                        </span>
                      </TableCell>
                      <TableCell className="hidden text-right text-sm text-muted-foreground sm:table-cell">
                        {timestamp(row.created_at)}
                      </TableCell>
                    </TableRow>
                  ))}
                </TableBody>
              </Table>
              <div className="flex items-center justify-between border-t p-3">
                <span className="text-sm text-muted-foreground">
                  {page * PAGE_SIZE + 1}–
                  {Math.min((page + 1) * PAGE_SIZE, total)} of {total}
                </span>
                <div className="flex gap-2">
                  <Button
                    variant="outline"
                    size="sm"
                    disabled={page === 0}
                    onClick={() => setPage((p) => Math.max(0, p - 1))}
                  >
                    Previous
                  </Button>
                  <Button
                    variant="outline"
                    size="sm"
                    disabled={(page + 1) * PAGE_SIZE >= total}
                    onClick={() => setPage((p) => p + 1)}
                  >
                    Next
                  </Button>
                </div>
              </div>
            </>
          )}
        </CardContent>
      </Card>
    </div>
  )
}

export function RuleAnalyticsPage() {
  useDocumentTitle("Rule analytics")
  const [searchParams, setSearchParams] = useSearchParams()
  const [reload, setReload] = useState(0)
  const [search, setSearch] = useState("")
  const [origin, setOrigin] = useState(ALL)
  const [sort, setSort] = useState("exposures")
  const [dimension, setDimension] = useState("category")
  const [page, setPage] = useState(0)

  const selected = useMemo(() => {
    const owner = searchParams.get("owner")
    const repo = searchParams.get("repo")
    const ruleId = searchParams.get("rule")
    if (!owner || !repo || !ruleId) return null
    return { owner, repo, ruleId: Number(ruleId) }
  }, [searchParams])

  const filters = useMemo(
    () => ({
      origin: origin === ALL ? "" : origin,
      sort,
      limit: PAGE_SIZE,
      offset: page * PAGE_SIZE,
    }),
    [origin, sort, page]
  )

  const { data, loading, error } = useAsync(
    () => api.listRuleAnalytics(filters),
    [filters, reload]
  )
  const { data: summary } = useAsync(
    () =>
      api.analyticsSummary(dimension, { origin: origin === ALL ? "" : origin }),
    [dimension, origin, reload]
  )
  const { data: regressions } = useAsync(() => api.listRegressions(), [reload])

  // Text search narrows the page in the browser. The server-side filters
  // (origin, sort, paging) do the heavy lifting; this is a refinement on what
  // is already on screen, not a substitute for them.
  const visible = useMemo(() => {
    const rules = data?.rules ?? []
    const needle = search.trim().toLowerCase()
    if (!needle) return rules
    return rules.filter(
      (rule) =>
        rule.rule_text.toLowerCase().includes(needle) ||
        `${rule.owner}/${rule.repo}`.toLowerCase().includes(needle) ||
        rule.category.toLowerCase().includes(needle)
    )
  }, [data, search])

  if (selected) {
    return (
      <div className="space-y-4 p-4 md:p-6">
        <RuleDetail
          owner={selected.owner}
          repo={selected.repo}
          ruleId={selected.ruleId}
          onBack={() => setSearchParams({})}
        />
      </div>
    )
  }

  const total = data?.total ?? 0

  return (
    <div className="space-y-4 p-4 md:p-6">
      <div>
        <h1 className="text-2xl font-semibold">Rule analytics</h1>
        <p className="text-sm text-muted-foreground">
          Where each rule was applied, what people did about it, and whether
          reviews got better. Findings nobody responded to stay neutral — they
          never count as approval.
        </p>
      </div>

      {regressions && regressions.suggestions.length > 0 ? (
        <Card className="border-amber-500/40">
          <CardContent className="flex items-start gap-3 p-4">
            <AlertTriangle className="mt-0.5 size-5 shrink-0 text-amber-500" />
            <div>
              <p className="font-medium">
                {regressions.suggestions.length} rule
                {regressions.suggestions.length === 1 ? "" : "s"} may have
                regressed
              </p>
              <p className="text-sm text-muted-foreground">
                Flagged after at least {regressions.min_exposures} exposures
                with a majority of negative feedback. Nothing has been disabled
                — open a rule to review the evidence.
              </p>
            </div>
          </CardContent>
        </Card>
      ) : null}

      <div className="flex flex-wrap items-center gap-2">
        <div className="relative min-w-56 flex-1">
          <Search className="absolute top-1/2 left-2.5 size-4 -translate-y-1/2 text-muted-foreground" />
          <Input
            className={cn("pl-8", search && ACTIVE_FILTER)}
            placeholder="Filter by rule, repository or category"
            value={search}
            onChange={(event) => setSearch(event.target.value)}
            aria-label="Filter rules"
          />
        </div>
        <Select
          value={origin}
          onValueChange={(value) => {
            setOrigin(value)
            setPage(0)
          }}
        >
          <SelectTrigger
            className={cn("w-40", origin !== ALL && ACTIVE_FILTER)}
            aria-label="Filter by origin"
          >
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            <SelectItem value={ALL}>All origins</SelectItem>
            <SelectItem value="learned">Learned</SelectItem>
            <SelectItem value="manual">Manual</SelectItem>
          </SelectContent>
        </Select>
        <Select value={sort} onValueChange={setSort}>
          <SelectTrigger className="w-48" aria-label="Sort rules">
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            {SORT_OPTIONS.map((option) => (
              <SelectItem key={option.value} value={option.value}>
                {option.label}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
        <Button variant="outline" size="sm" asChild>
          <a
            href={api.ruleAnalyticsExportUrl("csv", {
              origin: origin === ALL ? "" : origin,
            })}
          >
            <Download className="size-4" /> CSV
          </a>
        </Button>
        <Button variant="outline" size="sm" asChild>
          <a
            href={api.ruleAnalyticsExportUrl("json", {
              origin: origin === ALL ? "" : origin,
            })}
          >
            <Download className="size-4" /> JSON
          </a>
        </Button>
      </div>

      <Tabs defaultValue="rules">
        <TabsList>
          <TabsTrigger value="rules">Rules</TabsTrigger>
          <TabsTrigger value="breakdown">Breakdown</TabsTrigger>
        </TabsList>

        <TabsContent value="rules">
          <Card>
            <CardContent className="p-0">
              {loading ? (
                <TableSkeleton />
              ) : error ? (
                <ErrorState
                  message={error}
                  onRetry={() => setReload((n) => n + 1)}
                />
              ) : visible.length === 0 ? (
                <EmptyState
                  title={
                    search
                      ? "No rules match that filter"
                      : "No rule exposures recorded yet"
                  }
                  hint={
                    search
                      ? "Try a shorter search, or clear the origin filter."
                      : "Rules start appearing here after they run in a review. If analytics is turned off in your config, nothing is recorded."
                  }
                />
              ) : (
                <>
                  <Table>
                    <TableHeader>
                      <TableRow>
                        <TableHead>Rule</TableHead>
                        <TableHead className="hidden lg:table-cell">
                          Repository
                        </TableHead>
                        <TableHead className="text-right">Exposures</TableHead>
                        <TableHead className="min-w-28">Outcomes</TableHead>
                        <TableHead className="text-right">Agreement</TableHead>
                        <TableHead className="hidden text-right md:table-cell">
                          Addressed
                        </TableHead>
                      </TableRow>
                    </TableHeader>
                    <TableBody>
                      {visible.map((rule) => (
                        <TableRow
                          key={`${rule.owner}/${rule.repo}#${rule.rule_id}`}
                          className="cursor-pointer"
                          onClick={() =>
                            setSearchParams({
                              owner: rule.owner,
                              repo: rule.repo,
                              rule: String(rule.rule_id),
                            })
                          }
                        >
                          <TableCell className="max-w-96">
                            <p className="truncate font-medium">
                              {rule.rule_text || `Rule #${rule.rule_id}`}
                            </p>
                            <p className="mt-0.5 flex items-center gap-1.5 text-xs text-muted-foreground">
                              <Badge
                                variant={
                                  rule.origin === "manual"
                                    ? "default"
                                    : "secondary"
                                }
                                className="font-normal"
                              >
                                {rule.origin}
                              </Badge>
                              <span>v{rule.version}</span>
                              {rule.category ? (
                                <span>· {rule.category}</span>
                              ) : null}
                            </p>
                          </TableCell>
                          <TableCell className="hidden text-sm text-muted-foreground lg:table-cell">
                            {rule.owner}/{rule.repo}
                          </TableCell>
                          <TableCell className="text-right tabular-nums">
                            {rule.exposures}
                          </TableCell>
                          <TableCell>
                            <OutcomeBar rule={rule} />
                          </TableCell>
                          <TableCell className="text-right tabular-nums">
                            {rate(rule.acceptance_rate)}
                          </TableCell>
                          <TableCell className="hidden text-right tabular-nums md:table-cell">
                            {rate(rule.addressed_rate)}
                          </TableCell>
                        </TableRow>
                      ))}
                    </TableBody>
                  </Table>
                  <div className="flex items-center justify-between border-t p-3">
                    <span className="text-sm text-muted-foreground">
                      {total} rule{total === 1 ? "" : "s"}
                    </span>
                    <div className="flex gap-2">
                      <Button
                        variant="outline"
                        size="sm"
                        disabled={page === 0}
                        onClick={() => setPage((p) => Math.max(0, p - 1))}
                      >
                        Previous
                      </Button>
                      <Button
                        variant="outline"
                        size="sm"
                        disabled={(page + 1) * PAGE_SIZE >= total}
                        onClick={() => setPage((p) => p + 1)}
                      >
                        Next
                      </Button>
                    </div>
                  </div>
                </>
              )}
            </CardContent>
          </Card>
        </TabsContent>

        <TabsContent value="breakdown">
          <Card>
            <CardContent className="p-0">
              <div className="flex items-center gap-2 border-b p-4">
                <h3 className="font-medium">Grouped by</h3>
                <Select value={dimension} onValueChange={setDimension}>
                  <SelectTrigger
                    size="sm"
                    className="w-40"
                    aria-label="Group by"
                  >
                    <SelectValue />
                  </SelectTrigger>
                  <SelectContent>
                    {SUMMARY_DIMENSIONS.map((option) => (
                      <SelectItem key={option.value} value={option.value}>
                        {option.label}
                      </SelectItem>
                    ))}
                  </SelectContent>
                </Select>
              </div>
              {!summary ? (
                <TableSkeleton rows={4} />
              ) : summary.buckets.length === 0 ? (
                <EmptyState
                  title="Nothing to group yet"
                  hint="Breakdowns appear once rules have been exposed to reviews."
                />
              ) : (
                <Table>
                  <TableHeader>
                    <TableRow>
                      <TableHead>Group</TableHead>
                      <TableHead className="text-right">Exposures</TableHead>
                      <TableHead className="text-right">Agreed</TableHead>
                      <TableHead className="text-right">Disagreed</TableHead>
                      <TableHead className="text-right">No response</TableHead>
                      <TableHead className="text-right">Addressed</TableHead>
                    </TableRow>
                  </TableHeader>
                  <TableBody>
                    {summary.buckets.map((bucket) => (
                      <TableRow key={bucket.bucket}>
                        <TableCell className="font-medium">
                          {bucket.bucket || "—"}
                        </TableCell>
                        <TableCell className="text-right tabular-nums">
                          {bucket.exposures}
                        </TableCell>
                        <TableCell className="text-right tabular-nums">
                          {bucket.positive}
                        </TableCell>
                        <TableCell className="text-right tabular-nums">
                          {bucket.negative}
                        </TableCell>
                        <TableCell className="text-right tabular-nums">
                          {bucket.unobserved}
                        </TableCell>
                        <TableCell className="text-right tabular-nums">
                          {rate(bucket.addressed_rate)}
                        </TableCell>
                      </TableRow>
                    ))}
                  </TableBody>
                </Table>
              )}
            </CardContent>
          </Card>
        </TabsContent>
      </Tabs>
    </div>
  )
}
