import {
  AlertTriangle,
  CheckCircle2,
  CircleSlash,
  ClipboardCheck,
  ExternalLink,
  Info,
  RefreshCw,
  Timer,
  XCircle,
} from "lucide-react"
import { Fragment, useCallback, useMemo, useState } from "react"
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
import { Checkbox } from "@/components/ui/checkbox"
import { Input } from "@/components/ui/input"
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select"
import { Skeleton } from "@/components/ui/skeleton"
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
  CheckResultModel,
  CheckResultPage,
  CheckRunDetail,
  CheckRunModel,
  CheckRunPage,
  CheckRunVerdict,
  CheckState,
  CheckSummary,
} from "@/lib/api"
import { useAsync, useDocumentTitle } from "@/lib/hooks"
import { cn } from "@/lib/utils"

import { ChecksPolicyPanel } from "./checks-policy"

const ALL = "__all__"
const PAGE_SIZE = 25

// The distinction the whole page exists to make visible. Only `violation` is
// styled as a finding; the other three non-pass states are Mira reporting
// about itself, and they never share a colour with one.
const STATE_LABEL: Record<CheckState, string> = {
  pass: "Passed",
  violation: "Found a problem",
  infrastructure_error: "Mira could not answer",
  skipped: "Not applicable",
  timeout: "Ran out of time",
}

const STATE_STYLE: Record<CheckState, string> = {
  pass: "bg-emerald-500/10 text-emerald-600 dark:text-emerald-400",
  violation: "bg-red-500/10 text-red-600 dark:text-red-400",
  infrastructure_error: "bg-orange-500/10 text-orange-600 dark:text-orange-400",
  skipped: "bg-muted text-muted-foreground",
  timeout: "bg-orange-500/10 text-orange-600 dark:text-orange-400",
}

const STATE_ICON: Record<CheckState, typeof CheckCircle2> = {
  pass: CheckCircle2,
  violation: XCircle,
  infrastructure_error: AlertTriangle,
  skipped: CircleSlash,
  timeout: Timer,
}

const STATES: CheckState[] = [
  "pass",
  "violation",
  "infrastructure_error",
  "skipped",
  "timeout",
]

const VERDICT_LABEL: Record<CheckRunVerdict, string> = {
  pass: "Passed",
  violation: "Found a problem",
  incomplete: "Could not answer",
  not_run: "Did not run",
}

const VERDICT_STYLE: Record<CheckRunVerdict, string> = {
  pass: "bg-emerald-500/10 text-emerald-600 dark:text-emerald-400",
  violation: "bg-red-500/10 text-red-600 dark:text-red-400",
  incomplete: "bg-orange-500/10 text-orange-600 dark:text-orange-400",
  not_run: "bg-muted text-muted-foreground",
}

const VERDICTS: CheckRunVerdict[] = [
  "pass",
  "violation",
  "incomplete",
  "not_run",
]

function formatDate(seconds: number): string {
  if (!seconds) return "—"
  return new Date(seconds * 1000).toLocaleString()
}

function formatDuration(seconds: number): string {
  if (!seconds) return "—"
  return seconds < 1
    ? `${Math.round(seconds * 1000)}ms`
    : `${seconds.toFixed(1)}s`
}

function StateBadge({ state }: { state: CheckState }) {
  const Icon = STATE_ICON[state] ?? Info
  return (
    <Badge
      variant="secondary"
      className={cn("gap-1 font-medium", STATE_STYLE[state])}
    >
      <Icon className="h-3 w-3" />
      {STATE_LABEL[state] ?? state}
    </Badge>
  )
}

function VerdictBadge({ verdict }: { verdict: CheckRunVerdict }) {
  return (
    <Badge
      variant="secondary"
      className={cn("font-medium", VERDICT_STYLE[verdict])}
    >
      {VERDICT_LABEL[verdict] ?? verdict}
    </Badge>
  )
}

function SummaryTiles({
  summary,
  loading,
}: {
  summary: CheckSummary | null
  loading: boolean
}) {
  const totals = summary?.totals ?? {}
  const tiles = [
    {
      key: "violation",
      label: "Found a problem",
      value: totals.violation ?? 0,
      hint: "The only number here that is a statement about a pull request.",
    },
    {
      key: "pass",
      label: "Passed",
      value: totals.pass ?? 0,
      hint: "Ran, looked, found nothing.",
    },
    {
      key: "inconclusive",
      label: "Mira could not answer",
      value: totals.inconclusive ?? 0,
      hint: "Errors and timeouts. This climbing is an infrastructure problem, and no violation count would show it.",
    },
    {
      key: "skipped",
      label: "Not applicable",
      value: totals.skipped ?? 0,
      hint: "Off, out of scope, or nothing here to look at.",
    },
  ]
  return (
    <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
      {tiles.map((tile) => (
        <Card key={tile.key}>
          <CardHeader className="pb-2">
            <CardDescription className="text-xs">{tile.label}</CardDescription>
            <CardTitle className="text-2xl tabular-nums">
              {loading ? <Skeleton className="h-7 w-12" /> : tile.value}
            </CardTitle>
          </CardHeader>
          <CardContent className="pt-0">
            <p className="text-xs text-muted-foreground">{tile.hint}</p>
          </CardContent>
        </Card>
      ))}
    </div>
  )
}

function EvidenceList({ result }: { result: CheckResultModel }) {
  if (!result.findings.length && !result.evidence.length) return null
  return (
    <div className="space-y-3">
      {result.findings.map((finding) => (
        <div key={finding.fingerprint} className="space-y-1">
          <div className="flex flex-wrap items-center gap-2">
            <span className="text-sm font-medium">{finding.title}</span>
            {finding.sources.length > 1 && (
              // The whole point of running two producers: it is one problem,
              // and both of them are named on it.
              <Badge variant="outline" className="text-[10px] font-normal">
                also found by {finding.sources.slice(1).join(", ")}
              </Badge>
            )}
          </div>
          {finding.detail && (
            <p className="text-xs whitespace-pre-line text-muted-foreground">
              {finding.detail}
            </p>
          )}
          {finding.evidence.map((item, index) => (
            <div key={index} className="rounded border bg-muted/40 p-2">
              <div className="flex items-center gap-2 text-xs">
                <span className="font-mono">
                  {item.path
                    ? `${item.path}${item.start_line ? `:${item.start_line}` : ""}`
                    : item.detail || "evidence"}
                </span>
                {item.url && (
                  <a
                    href={item.url}
                    target="_blank"
                    rel="noreferrer"
                    className="text-muted-foreground hover:text-foreground"
                  >
                    <ExternalLink className="h-3 w-3" />
                  </a>
                )}
                <span className="text-muted-foreground">{item.source}</span>
              </div>
              {item.snippet && (
                <pre className="mt-1 overflow-x-auto text-[11px] whitespace-pre-wrap">
                  {item.snippet}
                </pre>
              )}
            </div>
          ))}
        </div>
      ))}
      {!result.findings.length && result.evidence.length > 0 && (
        <div className="flex flex-wrap gap-2 text-xs text-muted-foreground">
          {result.evidence.slice(0, 6).map((item, index) => (
            <span key={index} className="font-mono">
              {item.path || item.detail}
            </span>
          ))}
        </div>
      )}
    </div>
  )
}

function ResultRow({ result }: { result: CheckResultModel }) {
  const [open, setOpen] = useState(false)
  const hasDetail = Boolean(
    result.findings.length || result.evidence.length || result.error
  )
  return (
    <>
      <TableRow
        className={hasDetail ? "cursor-pointer" : undefined}
        onClick={() => hasDetail && setOpen((value) => !value)}
      >
        <TableCell>
          <span className="block font-mono text-xs">{result.check_id}</span>
          <span className="block text-xs text-muted-foreground">
            {result.summary}
          </span>
        </TableCell>
        <TableCell>
          <StateBadge state={result.state} />
        </TableCell>
        <TableCell className="text-xs">
          <Badge variant="outline" className="font-normal">
            {result.origin.replace("_", " ")}
          </Badge>
        </TableCell>
        <TableCell className="font-mono text-xs">
          {result.mode}
          {result.blocking && (
            <span className="ml-1 text-red-600 dark:text-red-400">blocks</span>
          )}
        </TableCell>
        <TableCell className="text-right text-xs tabular-nums">
          {formatDuration(result.duration_seconds)}
        </TableCell>
      </TableRow>
      {open && (
        <TableRow>
          <TableCell colSpan={5} className="bg-muted/30">
            <div className="space-y-3 py-2">
              {result.error && (
                <p className="text-xs text-orange-600 dark:text-orange-400">
                  {result.error}
                </p>
              )}
              {result.skip_reason && (
                <p className="text-xs text-muted-foreground">
                  Skip reason:{" "}
                  <span className="font-mono">{result.skip_reason}</span>
                  {result.incomplete &&
                    " — this counts as unanswered, so a blocking check is not satisfied by it."}
                </p>
              )}
              <EvidenceList result={result} />
              <p className="text-[11px] text-muted-foreground">
                v{result.check_version} · config {result.config_digest}
              </p>
            </div>
          </TableCell>
        </TableRow>
      )}
    </>
  )
}

function RunDetail({
  owner,
  repo,
  id,
}: {
  owner: string
  repo: string
  id: number
}) {
  const { data, loading, error } = useAsync<CheckRunDetail>(
    () => api.getCheckRun(owner, repo, id),
    [owner, repo, id]
  )

  if (loading) return <Skeleton className="m-4 h-40 w-[calc(100%-2rem)]" />
  if (error || !data)
    return (
      <p className="p-4 text-sm text-muted-foreground">
        {error ?? "This run could not be loaded."}
      </p>
    )

  const run = data.run
  const violations = run.results.filter((r) => r.state === "violation")
  const unanswered = run.results.filter(
    (r) => r.incomplete && r.state !== "violation"
  )
  const quiet = run.results.filter(
    (r) => !r.incomplete && r.state !== "violation"
  )

  return (
    <div className="space-y-4 p-4">
      <div className="flex flex-wrap items-center gap-3 text-xs text-muted-foreground">
        <span>Policy {run.policy_version}</span>
        <span>{formatDuration(run.duration_seconds)}</span>
        <span>{run.head_sha.slice(0, 12)}</span>
      </div>

      {violations.length > 0 && (
        <div>
          <h4 className="mb-1 text-sm font-semibold">What the checks found</h4>
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>Check</TableHead>
                <TableHead className="w-44">State</TableHead>
                <TableHead className="w-32">Origin</TableHead>
                <TableHead className="w-28">Mode</TableHead>
                <TableHead className="w-20 text-right">Took</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {violations.map((result) => (
                <ResultRow key={result.result_key} result={result} />
              ))}
            </TableBody>
          </Table>
        </div>
      )}

      {unanswered.length > 0 && (
        <div>
          <h4 className="mb-1 text-sm font-semibold">
            What Mira could not answer
          </h4>
          <p className="mb-2 text-xs text-muted-foreground">
            These are not findings against the pull request. Each one is a check
            that reached no conclusion, and the reason is Mira&apos;s side of
            the line.
          </p>
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>Check</TableHead>
                <TableHead className="w-44">State</TableHead>
                <TableHead className="w-32">Origin</TableHead>
                <TableHead className="w-28">Mode</TableHead>
                <TableHead className="w-20 text-right">Took</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {unanswered.map((result) => (
                <ResultRow key={result.result_key} result={result} />
              ))}
            </TableBody>
          </Table>
        </div>
      )}

      {quiet.length > 0 && (
        <details>
          <summary className="cursor-pointer text-sm font-semibold">
            {quiet.length} check(s) passed or did not apply
          </summary>
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>Check</TableHead>
                <TableHead className="w-44">State</TableHead>
                <TableHead className="w-32">Origin</TableHead>
                <TableHead className="w-28">Mode</TableHead>
                <TableHead className="w-20 text-right">Took</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {quiet.map((result) => (
                <ResultRow key={result.result_key} result={result} />
              ))}
            </TableBody>
          </Table>
        </details>
      )}
    </div>
  )
}

function useRepoFilter(value: string): [string, string] {
  return useMemo(() => {
    const trimmed = value.trim()
    if (!trimmed.includes("/")) return ["", trimmed]
    const [first, ...rest] = trimmed.split("/")
    return [first, rest.join("/")]
  }, [value])
}

function RunHistory() {
  const [verdict, setVerdict] = useState<string>(ALL)
  const [repo, setRepo] = useState("")
  const [page, setPage] = useState(0)
  const [refreshKey, setRefreshKey] = useState(0)
  const [expanded, setExpanded] = useState<number | null>(null)

  const [owner, repoName] = useRepoFilter(repo)

  const { data: summary, loading: summaryLoading } = useAsync<CheckSummary>(
    () => api.getChecksSummary({ owner, repo: repoName }),
    [owner, repoName, refreshKey]
  )

  const { data, loading, error } = useAsync<CheckRunPage>(
    () =>
      api.listCheckRuns({
        owner,
        repo: repoName,
        verdict: verdict === ALL ? undefined : (verdict as CheckRunVerdict),
        limit: PAGE_SIZE,
        offset: page * PAGE_SIZE,
      }),
    [owner, repoName, verdict, page, refreshKey]
  )

  const refresh = useCallback(() => setRefreshKey((key) => key + 1), [])
  const runs: CheckRunModel[] = data?.runs ?? []
  const total = data?.total ?? 0

  return (
    <div className="space-y-6">
      <SummaryTiles summary={summary} loading={summaryLoading} />

      <Card>
        <CardHeader className="gap-3">
          <div className="flex flex-wrap items-center gap-2">
            <Input
              value={repo}
              onChange={(event) => {
                setRepo(event.target.value)
                setPage(0)
              }}
              placeholder="owner/repo"
              className="h-9 w-56"
            />
            <Select
              value={verdict}
              onValueChange={(value) => {
                setVerdict(value)
                setPage(0)
              }}
            >
              <SelectTrigger className="h-9 w-52">
                <SelectValue placeholder="Any verdict" />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value={ALL}>Any verdict</SelectItem>
                {VERDICTS.map((item) => (
                  <SelectItem key={item} value={item}>
                    {VERDICT_LABEL[item]}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
            <Button variant="outline" size="sm" onClick={refresh}>
              <RefreshCw className="mr-1 h-3.5 w-3.5" />
              Refresh
            </Button>
          </div>
        </CardHeader>
        <CardContent className="px-0 pb-0">
          {loading ? (
            <div className="space-y-3 px-6 py-4">
              {Array.from({ length: 5 }).map((_, index) => (
                <Skeleton key={index} className="h-8 w-full" />
              ))}
            </div>
          ) : error ? (
            <p className="px-6 py-8 text-sm text-muted-foreground">{error}</p>
          ) : runs.length === 0 ? (
            <div className="flex flex-col items-center gap-2 px-6 py-12 text-center">
              <ClipboardCheck className="h-8 w-8 text-muted-foreground" />
              <p className="text-sm font-medium">No checks have run yet</p>
              <p className="max-w-md text-sm text-muted-foreground">
                Checks ship off. Turn them on from the Policy tab with every
                mode left at <span className="font-mono">warning</span> — that
                reports everything and blocks nothing.
              </p>
            </div>
          ) : (
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>Pull request</TableHead>
                  <TableHead className="w-44">Verdict</TableHead>
                  <TableHead className="w-56">Checks</TableHead>
                  <TableHead className="w-24 text-right">Took</TableHead>
                  <TableHead className="w-48">Ran</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {runs.map((run) => (
                  <Fragment key={run.id}>
                    <TableRow
                      className="cursor-pointer"
                      onClick={() =>
                        setExpanded(expanded === run.id ? null : run.id)
                      }
                    >
                      <TableCell>
                        <div className="flex items-center gap-2">
                          <span className="font-medium">
                            {run.owner}/{run.repo}#{run.pr_number}
                          </span>
                          {run.pr_url && (
                            <a
                              href={run.pr_url}
                              target="_blank"
                              rel="noreferrer"
                              onClick={(event) => event.stopPropagation()}
                              className="text-muted-foreground hover:text-foreground"
                            >
                              <ExternalLink className="h-3.5 w-3.5" />
                            </a>
                          )}
                        </div>
                        <span className="text-xs text-muted-foreground">
                          {run.pr_author || "unknown author"} ·{" "}
                          {run.head_sha.slice(0, 8)}
                        </span>
                      </TableCell>
                      <TableCell>
                        <VerdictBadge verdict={run.verdict} />
                      </TableCell>
                      <TableCell className="text-xs text-muted-foreground">
                        {run.counts.violation ?? 0} found ·{" "}
                        {(run.counts.infrastructure_error ?? 0) +
                          (run.counts.timeout ?? 0)}{" "}
                        unanswered · {run.counts.pass ?? 0} passed
                      </TableCell>
                      <TableCell className="text-right text-xs tabular-nums">
                        {formatDuration(run.duration_seconds)}
                      </TableCell>
                      <TableCell className="text-xs whitespace-nowrap text-muted-foreground">
                        {formatDate(run.created_at)}
                      </TableCell>
                    </TableRow>
                    {expanded === run.id && (
                      <TableRow>
                        <TableCell colSpan={5} className="p-0">
                          <RunDetail
                            owner={run.owner}
                            repo={run.repo}
                            id={run.id}
                          />
                        </TableCell>
                      </TableRow>
                    )}
                  </Fragment>
                ))}
              </TableBody>
            </Table>
          )}
        </CardContent>
      </Card>

      {total > PAGE_SIZE && (
        <div className="flex items-center justify-between text-sm">
          <span className="text-muted-foreground">
            {page * PAGE_SIZE + 1}–{Math.min((page + 1) * PAGE_SIZE, total)} of{" "}
            {total}
          </span>
          <div className="flex gap-2">
            <Button
              variant="outline"
              size="sm"
              disabled={page === 0}
              onClick={() => setPage((value) => Math.max(0, value - 1))}
            >
              Previous
            </Button>
            <Button
              variant="outline"
              size="sm"
              disabled={(page + 1) * PAGE_SIZE >= total}
              onClick={() => setPage((value) => value + 1)}
            >
              Next
            </Button>
          </div>
        </div>
      )}
    </div>
  )
}

function CheckHistory() {
  const [checkId, setCheckId] = useState("")
  const [state, setState] = useState<string>(ALL)
  const [incomplete, setIncomplete] = useState(false)
  const [repo, setRepo] = useState("")
  const [page, setPage] = useState(0)

  const [owner, repoName] = useRepoFilter(repo)

  const { data, loading, error } = useAsync<CheckResultPage>(
    () =>
      api.listCheckResults({
        owner,
        repo: repoName,
        checkId: checkId.trim() || undefined,
        state: state === ALL ? undefined : (state as CheckState),
        incomplete,
        limit: PAGE_SIZE,
        offset: page * PAGE_SIZE,
      }),
    [owner, repoName, checkId, state, incomplete, page]
  )

  const results = data?.results ?? []
  const total = data?.total ?? 0

  return (
    <div className="space-y-6">
      <Card>
        <CardHeader className="gap-3">
          <CardDescription>
            One check&apos;s history. Ticking{" "}
            <span className="font-mono">only unanswered</span> selects exactly
            the results that were <em>not</em> statements about a pull request —
            the set to exclude when hunting a noisy check, and the set to start
            from when hunting an infrastructure problem.
          </CardDescription>
          <div className="flex flex-wrap items-center gap-2">
            <Input
              value={repo}
              onChange={(event) => {
                setRepo(event.target.value)
                setPage(0)
              }}
              placeholder="owner/repo"
              className="h-9 w-52"
            />
            <Input
              value={checkId}
              onChange={(event) => {
                setCheckId(event.target.value)
                setPage(0)
              }}
              placeholder="check id, e.g. native.tests"
              className="h-9 w-60 font-mono text-xs"
            />
            <Select
              value={state}
              onValueChange={(value) => {
                setState(value)
                setPage(0)
              }}
            >
              <SelectTrigger className="h-9 w-52">
                <SelectValue placeholder="Any state" />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value={ALL}>Any state</SelectItem>
                {STATES.map((item) => (
                  <SelectItem key={item} value={item}>
                    {STATE_LABEL[item]}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
            <label className="flex cursor-pointer items-center gap-2 text-sm">
              <Checkbox
                checked={incomplete}
                onCheckedChange={(value) => {
                  setIncomplete(value === true)
                  setPage(0)
                }}
              />
              only unanswered
            </label>
          </div>
        </CardHeader>
        <CardContent className="px-0 pb-0">
          {loading ? (
            <div className="space-y-3 px-6 py-4">
              {Array.from({ length: 5 }).map((_, index) => (
                <Skeleton key={index} className="h-8 w-full" />
              ))}
            </div>
          ) : error ? (
            <p className="px-6 py-8 text-sm text-muted-foreground">{error}</p>
          ) : results.length === 0 ? (
            <p className="px-6 py-12 text-center text-sm text-muted-foreground">
              No result matches this filter.
            </p>
          ) : (
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>Check</TableHead>
                  <TableHead className="w-44">State</TableHead>
                  <TableHead className="w-32">Origin</TableHead>
                  <TableHead className="w-28">Mode</TableHead>
                  <TableHead className="w-20 text-right">Took</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {results.map((result) => (
                  <ResultRow key={result.result_key} result={result} />
                ))}
              </TableBody>
            </Table>
          )}
        </CardContent>
      </Card>

      {total > PAGE_SIZE && (
        <div className="flex items-center justify-between text-sm">
          <span className="text-muted-foreground">
            {page * PAGE_SIZE + 1}–{Math.min((page + 1) * PAGE_SIZE, total)} of{" "}
            {total}
          </span>
          <div className="flex gap-2">
            <Button
              variant="outline"
              size="sm"
              disabled={page === 0}
              onClick={() => setPage((value) => Math.max(0, value - 1))}
            >
              Previous
            </Button>
            <Button
              variant="outline"
              size="sm"
              disabled={(page + 1) * PAGE_SIZE >= total}
              onClick={() => setPage((value) => value + 1)}
            >
              Next
            </Button>
          </div>
        </div>
      )}
    </div>
  )
}

export function ChecksPage() {
  useDocumentTitle("Pre-merge checks")
  const [params, setParams] = useSearchParams()
  const requested = params.get("tab")
  const tab =
    requested === "policy" || requested === "checks" ? requested : "runs"

  return (
    <div className="space-y-6 p-6">
      <div>
        <h1 className="text-2xl font-semibold tracking-tight">
          Pre-merge checks
        </h1>
        <p className="max-w-3xl text-sm text-muted-foreground">
          The questions a diff alone does not answer, each asked independently
          and each showing its evidence. A <strong>violation</strong> is a
          statement about the pull request; a missing linter, a model that would
          not answer and a tracker that could not be reached are statements
          about Mira, and they are never shown as the same thing.
        </p>
      </div>

      <Tabs
        value={tab}
        onValueChange={(value) =>
          setParams(value === "runs" ? {} : { tab: value })
        }
      >
        <TabsList>
          <TabsTrigger value="runs">Runs</TabsTrigger>
          <TabsTrigger value="checks">Per check</TabsTrigger>
          <TabsTrigger value="policy">Policy</TabsTrigger>
        </TabsList>
        <TabsContent value="runs" className="pt-4">
          <RunHistory />
        </TabsContent>
        <TabsContent value="checks" className="pt-4">
          <CheckHistory />
        </TabsContent>
        <TabsContent value="policy" className="pt-4">
          <ChecksPolicyPanel />
        </TabsContent>
      </Tabs>
    </div>
  )
}
