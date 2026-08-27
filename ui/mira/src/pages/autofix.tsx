import {
  AlertTriangle,
  Ban,
  CheckCircle2,
  Clock,
  ExternalLink,
  GitPullRequestArrow,
  Info,
  Loader2,
  RefreshCw,
  ShieldQuestion,
  Wrench,
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
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog"
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
import { Textarea } from "@/components/ui/textarea"
import { api } from "@/lib/api"
import type {
  AutofixCheck,
  AutofixJobDetail,
  AutofixJobModel,
  AutofixJobPage,
  AutofixJobState,
  AutofixSummary,
} from "@/lib/api"
import { useAsync, useDocumentTitle } from "@/lib/hooks"
import { cn } from "@/lib/utils"

import { AutofixPolicyPanel } from "./autofix-policy"

const ALL = "__all__"
const PAGE_SIZE = 25

// `opened` is the only success, and it means a *reviewable* change exists —
// never that anything was merged.
const STATE_LABEL: Record<AutofixJobState, string> = {
  queued: "Queued",
  running: "Generating",
  validating: "Validating",
  publishing: "Publishing",
  opened: "Ready to review",
  failed: "Failed — will retry",
  dead_letter: "Gave up",
  cancelled: "Cancelled",
}

const STATE_STYLE: Record<AutofixJobState, string> = {
  queued: "bg-muted text-muted-foreground",
  running: "bg-sky-500/10 text-sky-600 dark:text-sky-400",
  validating: "bg-sky-500/10 text-sky-600 dark:text-sky-400",
  publishing: "bg-sky-500/10 text-sky-600 dark:text-sky-400",
  opened: "bg-emerald-500/10 text-emerald-600 dark:text-emerald-400",
  failed: "bg-amber-500/10 text-amber-600 dark:text-amber-400",
  dead_letter: "bg-red-500/10 text-red-600 dark:text-red-400",
  cancelled: "bg-muted text-muted-foreground",
}

const STATE_ICON: Record<AutofixJobState, typeof CheckCircle2> = {
  queued: Clock,
  running: Loader2,
  validating: ShieldQuestion,
  publishing: GitPullRequestArrow,
  opened: CheckCircle2,
  failed: AlertTriangle,
  dead_letter: XCircle,
  cancelled: Ban,
}

const MODE_LABEL: Record<string, string> = {
  branch_pr: "Stacked PR",
  pr_branch: "On the PR branch",
  handoff: "Handed off",
}

const CHECK_STYLE: Record<string, string> = {
  passed: "text-emerald-600 dark:text-emerald-400",
  skipped: "text-muted-foreground",
  failed: "text-red-600 dark:text-red-400",
  error: "text-red-600 dark:text-red-400",
  timeout: "text-red-600 dark:text-red-400",
}

const STATES: AutofixJobState[] = [
  "queued",
  "running",
  "validating",
  "publishing",
  "opened",
  "failed",
  "dead_letter",
  "cancelled",
]

// A job that is neither finished nor abandoned can still be stopped.
const CANCELLABLE = new Set<AutofixJobState>([
  "queued",
  "running",
  "validating",
  "publishing",
  "failed",
])

function formatDate(seconds: number): string {
  if (!seconds) return "—"
  return new Date(seconds * 1000).toLocaleString()
}

function StateBadge({ state }: { state: AutofixJobState }) {
  const Icon = STATE_ICON[state] ?? Info
  return (
    <Badge
      variant="secondary"
      className={cn("gap-1 font-medium", STATE_STYLE[state])}
    >
      <Icon className={cn("h-3 w-3", state === "running" && "animate-spin")} />
      {STATE_LABEL[state] ?? state}
    </Badge>
  )
}

function SummaryTiles({
  summary,
  loading,
}: {
  summary: AutofixSummary | null
  loading: boolean
}) {
  const totals = summary?.totals ?? {}
  const tiles = [
    {
      key: "opened",
      label: "Ready to review",
      value: totals.opened ?? 0,
      hint: "A reviewable change exists. Nothing here was merged by Mira.",
    },
    {
      key: "in_flight",
      label: "In flight",
      value:
        (totals.queued ?? 0) +
        (totals.running ?? 0) +
        (totals.validating ?? 0) +
        (totals.publishing ?? 0),
      hint: "Queued or being worked on. Nothing has been written yet.",
    },
    {
      key: "failed",
      label: "Retrying",
      value: totals.failed ?? 0,
      hint: "An attempt failed and the job is waiting for its next try.",
    },
    {
      key: "dead_letter",
      label: "Gave up",
      value: totals.dead_letter ?? 0,
      hint: "Out of attempts, or refused for a reason retrying cannot change.",
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

function CancelDialog({
  job,
  open,
  onOpenChange,
  onDone,
}: {
  job: AutofixJobModel | null
  open: boolean
  onOpenChange: (open: boolean) => void
  onDone: () => void
}) {
  const [reason, setReason] = useState("")
  const [saving, setSaving] = useState(false)

  const submit = async () => {
    if (!job) return
    setSaving(true)
    try {
      await api.cancelAutofixJob(job.owner, job.repo, job.id, {
        reason: reason.trim() || "cancelled from the dashboard",
      })
      toast.success("Job cancelled")
      onOpenChange(false)
      setReason("")
      onDone()
    } catch (err) {
      toast.error(err instanceof Error ? err.message : "Cancellation refused")
    } finally {
      setSaving(false)
    }
  }

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>Cancel this job</DialogTitle>
          <DialogDescription>
            The worker stops at its next heartbeat and the job is never claimed
            again. This does not reach through to the platform — anything
            already pushed or opened stays exactly where it is, because closing
            somebody&apos;s pull request is not what cancelling means.
          </DialogDescription>
        </DialogHeader>
        <div className="space-y-2">
          <label className="text-sm font-medium">Reason</label>
          <Textarea
            value={reason}
            onChange={(event) => setReason(event.target.value)}
            placeholder="Why is this being stopped?"
            rows={3}
          />
        </div>
        <DialogFooter>
          <Button variant="outline" onClick={() => onOpenChange(false)}>
            Keep it
          </Button>
          <Button variant="destructive" onClick={submit} disabled={saving}>
            {saving ? "Cancelling…" : "Cancel job"}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}

function ValidationTable({ checks }: { checks: AutofixCheck[] }) {
  if (checks.length === 0) {
    return (
      <p className="text-sm text-muted-foreground">
        No validation has run for this job yet.
      </p>
    )
  }
  return (
    <Table>
      <TableHeader>
        <TableRow>
          <TableHead className="w-40">Check</TableHead>
          <TableHead className="w-28">Result</TableHead>
          <TableHead>Detail</TableHead>
        </TableRow>
      </TableHeader>
      <TableBody>
        {checks.map((check, index) => (
          <TableRow key={`${check.name}-${index}`}>
            <TableCell className="font-medium">{check.name}</TableCell>
            <TableCell
              className={cn("font-mono text-xs", CHECK_STYLE[check.outcome])}
            >
              {check.outcome}
              {check.exit_code !== null && check.exit_code !== 0
                ? ` (${check.exit_code})`
                : ""}
            </TableCell>
            <TableCell className="max-w-lg text-xs whitespace-pre-wrap text-muted-foreground">
              {check.detail || "—"}
            </TableCell>
          </TableRow>
        ))}
      </TableBody>
    </Table>
  )
}

function DiffBlock({ diff }: { diff: string }) {
  if (!diff) return null
  return (
    <pre className="max-h-96 overflow-auto rounded-md border bg-muted/40 p-3 font-mono text-[11px] leading-relaxed">
      {diff.split("\n").map((line, index) => (
        <div
          key={index}
          className={cn(
            line.startsWith("+") &&
              !line.startsWith("+++") &&
              "text-emerald-600 dark:text-emerald-400",
            line.startsWith("-") &&
              !line.startsWith("---") &&
              "text-red-600 dark:text-red-400",
            line.startsWith("@@") && "text-sky-600 dark:text-sky-400"
          )}
        >
          {line || " "}
        </div>
      ))}
    </pre>
  )
}

function JobDetail({
  owner,
  repo,
  id,
}: {
  owner: string
  repo: string
  id: number
}) {
  const { data, loading, error } = useAsync<AutofixJobDetail>(
    () => api.getAutofixJob(owner, repo, id),
    [owner, repo, id]
  )

  if (loading) return <Skeleton className="h-40 w-full" />
  if (error || !data)
    return (
      <p className="p-4 text-sm text-muted-foreground">
        {error ?? "This job could not be loaded."}
      </p>
    )

  const { job } = data
  return (
    <div className="space-y-4 border-t bg-muted/30 p-4">
      <div className="flex flex-wrap items-center gap-3">
        <StateBadge state={job.state} />
        <span className="text-xs text-muted-foreground">
          attempt {job.attempts}/{job.max_attempts}
          {job.max_ci_attempts > 0 &&
            ` · CI retries ${job.ci_attempts}/${job.max_ci_attempts}`}
        </span>
        <span className="font-mono text-xs text-muted-foreground">
          {job.policy_version}
        </span>
        {job.model && (
          <span className="font-mono text-xs text-muted-foreground">
            model {job.model}
          </span>
        )}
        {job.error && (
          <span className="text-xs text-orange-600 dark:text-orange-400">
            {job.error}
          </span>
        )}
      </div>

      <div className="grid gap-2 text-xs text-muted-foreground sm:grid-cols-2">
        <span>
          Finding:{" "}
          <span className="font-mono">{job.finding_id.slice(0, 12)}</span>
          {job.finding_title ? ` — ${job.finding_title}` : ""}
        </span>
        <span>Requested by @{job.requested_by || "unknown"}</span>
        <span>
          Delivery: {MODE_LABEL[job.mode] ?? job.mode}
          {job.request_kind === "all" ? " (from `fix all`)" : ""}
        </span>
        <span>
          Branch:{" "}
          <span className="font-mono">{job.branch_name || "not created"}</span>
        </span>
        <span>
          Commit:{" "}
          <span className="font-mono">
            {job.commit_sha ? job.commit_sha.slice(0, 12) : "none"}
          </span>
        </span>
        <span>
          Patch: <span className="font-mono">{job.patch_digest || "—"}</span>
        </span>
        {job.handoff_ref && (
          <span>
            Handoff: <span className="font-mono">{job.handoff_ref}</span>
          </span>
        )}
        {job.cancelled_by && <span>Cancelled by @{job.cancelled_by}</span>}
      </div>

      {job.reasons.length > 0 && (
        <div>
          <h4 className="mb-1 text-sm font-semibold">Reasons</h4>
          <ul className="space-y-1 text-sm">
            {job.reasons.map((reason, index) => (
              <li key={`${reason.code}-${index}`} className="flex gap-2">
                <Badge
                  variant="outline"
                  className="shrink-0 font-mono text-[10px]"
                >
                  {reason.kind}
                </Badge>
                <span>
                  {reason.message}{" "}
                  <span className="font-mono text-xs text-muted-foreground">
                    ({reason.code})
                  </span>
                </span>
              </li>
            ))}
          </ul>
        </div>
      )}

      {job.diff && (
        <div>
          <h4 className="mb-1 text-sm font-semibold">The change</h4>
          <DiffBlock diff={job.diff} />
        </div>
      )}

      <div>
        <h4 className="mb-1 text-sm font-semibold">Validation</h4>
        {!job.validation.executed && job.validation.checks.length === 0 ? (
          <p className="text-sm text-muted-foreground">
            Nothing has been validated yet.
          </p>
        ) : (
          <ValidationTable checks={job.validation.checks} />
        )}
      </div>

      {data.attempts.length > 0 && (
        <div>
          <h4 className="mb-1 text-sm font-semibold">
            Attempts ({data.attempts.length})
          </h4>
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead className="w-40">When</TableHead>
                <TableHead className="w-24">Phase</TableHead>
                <TableHead className="w-24">Outcome</TableHead>
                <TableHead>Detail</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {data.attempts.map((attempt) => (
                <TableRow key={attempt.id}>
                  <TableCell className="text-xs whitespace-nowrap">
                    {formatDate(attempt.created_at)}
                  </TableCell>
                  <TableCell className="font-mono text-xs">
                    {attempt.phase}
                  </TableCell>
                  <TableCell
                    className={cn(
                      "font-mono text-xs",
                      attempt.outcome.includes("fail") ||
                        attempt.outcome === "refused"
                        ? "text-red-600 dark:text-red-400"
                        : "text-muted-foreground"
                    )}
                  >
                    {attempt.outcome}
                  </TableCell>
                  <TableCell className="max-w-md text-xs text-muted-foreground">
                    {attempt.detail ||
                      attempt.reasons.map((r) => r.message).join("; ") ||
                      "—"}
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        </div>
      )}
    </div>
  )
}

function JobHistory() {
  const [state, setState] = useState<string>(ALL)
  const [mode, setMode] = useState<string>(ALL)
  const [repo, setRepo] = useState("")
  const [page, setPage] = useState(0)
  const [refreshKey, setRefreshKey] = useState(0)
  const [expanded, setExpanded] = useState<number | null>(null)
  const [cancelling, setCancelling] = useState<AutofixJobModel | null>(null)

  const [owner, repoName] = useMemo(() => {
    const trimmed = repo.trim()
    if (!trimmed.includes("/")) return ["", trimmed]
    const [first, ...rest] = trimmed.split("/")
    return [first, rest.join("/")]
  }, [repo])

  const filters = useMemo(
    () => ({
      owner,
      repo: repoName,
      state: state === ALL ? undefined : (state as AutofixJobState),
      mode: mode === ALL ? undefined : (mode as "branch_pr"),
    }),
    [owner, repoName, state, mode]
  )

  const { data: summary, loading: summaryLoading } = useAsync<AutofixSummary>(
    () => api.getAutofixSummary({ owner, repo: repoName }),
    [owner, repoName, refreshKey]
  )

  const { data, loading, error } = useAsync<AutofixJobPage>(
    () =>
      api.listAutofixJobs({
        ...filters,
        limit: PAGE_SIZE,
        offset: page * PAGE_SIZE,
      }),
    [filters, page, refreshKey]
  )

  const refresh = useCallback(() => setRefreshKey((key) => key + 1), [])
  const jobs = data?.jobs ?? []
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
              value={state}
              onValueChange={(value) => {
                setState(value)
                setPage(0)
              }}
            >
              <SelectTrigger className="h-9 w-48">
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
            <Select
              value={mode}
              onValueChange={(value) => {
                setMode(value)
                setPage(0)
              }}
            >
              <SelectTrigger className="h-9 w-44">
                <SelectValue placeholder="Any delivery" />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value={ALL}>Any delivery</SelectItem>
                <SelectItem value="branch_pr">Stacked PR</SelectItem>
                <SelectItem value="pr_branch">On the PR branch</SelectItem>
                <SelectItem value="handoff">Handed off</SelectItem>
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
          ) : jobs.length === 0 ? (
            <div className="flex flex-col items-center gap-2 px-6 py-12 text-center">
              <Wrench className="h-8 w-8 text-muted-foreground" />
              <p className="text-sm font-medium">No fixes requested yet</p>
              <p className="max-w-md text-sm text-muted-foreground">
                Autofix ships off. Set it to{" "}
                <span className="font-mono">suggest</span> on the Policy tab,
                then reply <span className="font-mono">@mira fix</span> to one
                of Mira&apos;s review comments to see what it would write.
              </p>
            </div>
          ) : (
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>Pull request</TableHead>
                  <TableHead>Finding</TableHead>
                  <TableHead>State</TableHead>
                  <TableHead>Result</TableHead>
                  <TableHead>Requested</TableHead>
                  <TableHead className="w-24" />
                </TableRow>
              </TableHeader>
              <TableBody>
                {jobs.map((job) => (
                  <Fragment key={job.id}>
                    <TableRow
                      className="cursor-pointer"
                      onClick={() =>
                        setExpanded(expanded === job.id ? null : job.id)
                      }
                    >
                      <TableCell>
                        <div className="flex items-center gap-2">
                          <span className="font-medium">
                            {job.owner}/{job.repo}#{job.pr_number}
                          </span>
                          {job.pr_url && (
                            <a
                              href={job.pr_url}
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
                          @{job.requested_by || "unknown"} ·{" "}
                          {MODE_LABEL[job.mode] ?? job.mode}
                        </span>
                      </TableCell>
                      <TableCell className="max-w-xs">
                        <span className="block truncate text-sm">
                          {job.finding_title || "—"}
                        </span>
                        <span className="font-mono text-xs text-muted-foreground">
                          {job.finding_id.slice(0, 12)}
                        </span>
                      </TableCell>
                      <TableCell>
                        <StateBadge state={job.state} />
                      </TableCell>
                      <TableCell>
                        {job.child_pr_url ? (
                          <a
                            href={job.child_pr_url}
                            target="_blank"
                            rel="noreferrer"
                            onClick={(event) => event.stopPropagation()}
                            className="inline-flex items-center gap-1 text-sm hover:underline"
                          >
                            #{job.child_pr_number}
                            <ExternalLink className="h-3 w-3" />
                          </a>
                        ) : job.commit_sha ? (
                          <span className="font-mono text-xs">
                            {job.commit_sha.slice(0, 8)}
                          </span>
                        ) : (
                          <span className="text-xs text-muted-foreground">
                            nothing written
                          </span>
                        )}
                      </TableCell>
                      <TableCell className="text-xs whitespace-nowrap text-muted-foreground">
                        {formatDate(job.created_at)}
                      </TableCell>
                      <TableCell>
                        {CANCELLABLE.has(job.state) && (
                          <Button
                            variant="ghost"
                            size="sm"
                            onClick={(event) => {
                              event.stopPropagation()
                              setCancelling(job)
                            }}
                          >
                            Cancel
                          </Button>
                        )}
                      </TableCell>
                    </TableRow>
                    {expanded === job.id && (
                      <TableRow>
                        <TableCell colSpan={6} className="p-0">
                          <JobDetail
                            owner={job.owner}
                            repo={job.repo}
                            id={job.id}
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

      <CancelDialog
        job={cancelling}
        open={cancelling !== null}
        onOpenChange={(open) => !open && setCancelling(null)}
        onDone={refresh}
      />
    </div>
  )
}

export function AutofixPage() {
  useDocumentTitle("Autofix")
  const [params, setParams] = useSearchParams()
  const tab = params.get("tab") === "policy" ? "policy" : "jobs"

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-2xl font-semibold tracking-tight">Autofix</h1>
        <p className="max-w-3xl text-sm text-muted-foreground">
          A maintainer replies <span className="font-mono">@mira fix</span> to a
          review comment and gets a reviewable change on a branch of its own.
          Mira never writes to the default branch, never force pushes, and never
          merges. It ships off; suggest mode shows you the patch it would have
          written without writing anything.
        </p>
      </div>

      <Tabs
        value={tab}
        onValueChange={(value) =>
          setParams(value === "policy" ? { tab: "policy" } : {})
        }
      >
        <TabsList>
          <TabsTrigger value="jobs">Jobs</TabsTrigger>
          <TabsTrigger value="policy">Policy</TabsTrigger>
        </TabsList>
        <TabsContent value="jobs" className="pt-4">
          <JobHistory />
        </TabsContent>
        <TabsContent value="policy" className="pt-4">
          <AutofixPolicyPanel />
        </TabsContent>
      </Tabs>
    </div>
  )
}
