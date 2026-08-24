import {
  AlertTriangle,
  CheckCircle2,
  CircleSlash,
  ExternalLink,
  Gavel,
  Info,
  RefreshCw,
  ShieldCheck,
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
  GateDecisionDetail,
  GateDecisionModel,
  GateDecisionPage,
  GateState,
  GateSummary,
} from "@/lib/api"
import { useAsync, useDocumentTitle } from "@/lib/hooks"
import { cn } from "@/lib/utils"

import { GatePolicyPanel } from "./merge-gate-policy"

const ALL = "__all__"
const PAGE_SIZE = 25

// `would_approve` is never styled as a success. It is the dry run saying it
// reached the conclusion it would have acted on — and deliberately did not.
const STATE_LABEL: Record<GateState, string> = {
  approved: "Approved",
  would_approve: "Would approve",
  not_approved: "Not approved",
  skipped: "Not applicable",
  error: "Could not decide",
}

const STATE_STYLE: Record<GateState, string> = {
  approved: "bg-emerald-500/10 text-emerald-600 dark:text-emerald-400",
  would_approve: "bg-amber-500/10 text-amber-600 dark:text-amber-400",
  not_approved: "bg-red-500/10 text-red-600 dark:text-red-400",
  skipped: "bg-muted text-muted-foreground",
  error: "bg-orange-500/10 text-orange-600 dark:text-orange-400",
}

const STATE_ICON: Record<GateState, typeof CheckCircle2> = {
  approved: CheckCircle2,
  would_approve: ShieldCheck,
  not_approved: XCircle,
  skipped: CircleSlash,
  error: AlertTriangle,
}

const BAND_STYLE: Record<string, string> = {
  low: "text-emerald-600 dark:text-emerald-400",
  medium: "text-amber-600 dark:text-amber-400",
  high: "text-red-600 dark:text-red-400",
}

const STATES: GateState[] = [
  "approved",
  "would_approve",
  "not_approved",
  "skipped",
  "error",
]

function formatDate(seconds: number): string {
  if (!seconds) return "—"
  return new Date(seconds * 1000).toLocaleString()
}

function StateBadge({ state }: { state: GateState }) {
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

function SummaryTiles({
  summary,
  loading,
}: {
  summary: GateSummary | null
  loading: boolean
}) {
  const totals = summary?.totals ?? {}
  const tiles = [
    {
      key: "candidate_approvals",
      label: "Would have approved",
      value: totals.candidate_approvals ?? 0,
      hint: "Approved plus would-approve — the number a dry run exists to produce.",
    },
    {
      key: "approved",
      label: "Actually approved",
      value: totals.approved ?? 0,
      hint: "Real approvals delivered to a platform.",
    },
    {
      key: "not_approved",
      label: "Not approved",
      value: totals.not_approved ?? 0,
      hint: "In scope, and the gate said no.",
    },
    {
      key: "error",
      label: "Could not decide",
      value: totals.error ?? 0,
      hint: "An input was unreadable or the budget ran out. Never an approval.",
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

function OverrideDialog({
  decision,
  open,
  onOpenChange,
  onDone,
}: {
  decision: GateDecisionModel | null
  open: boolean
  onOpenChange: (open: boolean) => void
  onDone: () => void
}) {
  const [newState, setNewState] = useState<GateState>("not_approved")
  const [reason, setReason] = useState("")
  const [saving, setSaving] = useState(false)

  const submit = async () => {
    if (!decision) return
    if (!reason.trim()) {
      toast.error("An override has to record why.")
      return
    }
    setSaving(true)
    try {
      await api.overrideGateDecision(
        decision.owner,
        decision.repo,
        decision.id,
        { new_state: newState, reason: reason.trim() }
      )
      toast.success("Override recorded")
      onOpenChange(false)
      setReason("")
      onDone()
    } catch (err) {
      toast.error(err instanceof Error ? err.message : "Override refused")
    } finally {
      setSaving(false)
    }
  }

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>Override this decision</DialogTitle>
          <DialogDescription>
            This records an administrative decision against{" "}
            <span className="font-mono text-xs">
              {decision?.owner}/{decision?.repo}#{decision?.pr_number}
            </span>
            . It does not submit or retract anything on the platform — Mira does
            not approve a pull request on an admin&apos;s behalf.
          </DialogDescription>
        </DialogHeader>
        <div className="space-y-4">
          <div className="space-y-2">
            <label className="text-sm font-medium">New state</label>
            <Select
              value={newState}
              onValueChange={(value) => setNewState(value as GateState)}
            >
              <SelectTrigger>
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="not_approved">
                  Not approved (revoke)
                </SelectItem>
                <SelectItem value="approved">Approved (force)</SelectItem>
              </SelectContent>
            </Select>
            <p className="text-xs text-muted-foreground">
              Forcing an approval is a separate opt-in (
              <span className="font-mono">gate.allow_approval_override</span>),
              and it is refused outright for a protected path, an open blocker,
              or a human who asked for changes.
            </p>
          </div>
          <div className="space-y-2">
            <label className="text-sm font-medium">Reason</label>
            <Textarea
              value={reason}
              onChange={(event) => setReason(event.target.value)}
              placeholder="Why is this decision being moved by hand?"
              rows={3}
            />
          </div>
        </div>
        <DialogFooter>
          <Button variant="outline" onClick={() => onOpenChange(false)}>
            Cancel
          </Button>
          <Button onClick={submit} disabled={saving}>
            {saving ? "Recording…" : "Record override"}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}

function DecisionDetail({
  owner,
  repo,
  id,
}: {
  owner: string
  repo: string
  id: number
}) {
  const { data, loading, error } = useAsync<GateDecisionDetail>(
    () => api.getGateDecision(owner, repo, id),
    [owner, repo, id]
  )

  if (loading) return <Skeleton className="h-40 w-full" />
  if (error || !data)
    return (
      <p className="p-4 text-sm text-muted-foreground">
        {error ?? "This decision could not be loaded."}
      </p>
    )

  const { decision } = data
  return (
    <div className="space-y-4 border-t bg-muted/30 p-4">
      <div className="flex flex-wrap items-center gap-3">
        <StateBadge state={decision.state} />
        <span
          className={cn("text-sm font-medium", BAND_STYLE[decision.risk_band])}
        >
          Risk {decision.risk_score}/100 ({decision.risk_band})
        </span>
        <span className="font-mono text-xs text-muted-foreground">
          {decision.policy_version}
        </span>
        <span className="text-xs text-muted-foreground">
          delivery: {decision.delivery_state}
          {decision.delivery_attempts
            ? ` after ${decision.delivery_attempts} attempt(s)`
            : ""}
        </span>
        {/* A `partial` delivery reached the pull request through one channel
            and not the other, and stays retryable — so the error behind it is
            the operator's only clue about which one is broken. */}
        {decision.error && (
          <span className="text-xs text-orange-600 dark:text-orange-400">
            {decision.error}
          </span>
        )}
      </div>

      {decision.reasons.length > 0 && (
        <div>
          <h4 className="mb-1 text-sm font-semibold">Reasons</h4>
          <ul className="space-y-1 text-sm">
            {decision.reasons.map((reason, index) => (
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

      {decision.factors.length > 0 && (
        <div>
          <h4 className="mb-1 text-sm font-semibold">Risk factors</h4>
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>Factor</TableHead>
                <TableHead className="w-20 text-right">Points</TableHead>
                <TableHead>Detail</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {decision.factors.map((factor) => (
                <TableRow key={factor.code}>
                  <TableCell className="font-medium">{factor.label}</TableCell>
                  <TableCell className="text-right tabular-nums">
                    {factor.points}
                  </TableCell>
                  <TableCell className="text-muted-foreground">
                    {factor.detail}
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        </div>
      )}

      <div className="grid gap-2 text-xs text-muted-foreground sm:grid-cols-2">
        <span>
          Files: {decision.inputs.changed_files} (+{decision.inputs.added_lines}
          /-
          {decision.inputs.deleted_lines}),{" "}
          {decision.inputs.generated_paths.length} generated
        </span>
        <span>
          CI: {decision.inputs.ci.state} over {decision.inputs.ci.total}{" "}
          check(s)
        </span>
        <span>
          Findings: {decision.inputs.open_blockers} blocker(s),{" "}
          {decision.inputs.open_findings} open
        </span>
        <span>
          Protected: {decision.inputs.protected_matches.join(", ") || "—"}
        </span>
        <span>
          CODEOWNERS: {decision.inputs.codeowners_status}
          {decision.inputs.codeowner_matches.length
            ? ` — ${decision.inputs.codeowner_matches.join(", ")}`
            : ""}
        </span>
        <span>Author association: {decision.inputs.author_association}</span>
      </div>

      {data.overrides.length > 0 && (
        <div>
          <h4 className="mb-1 text-sm font-semibold">Override history</h4>
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>When</TableHead>
                <TableHead>Actor</TableHead>
                <TableHead>From → to</TableHead>
                <TableHead>Reason</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {data.overrides.map((item) => (
                <TableRow key={item.id}>
                  <TableCell className="text-xs whitespace-nowrap">
                    {formatDate(item.created_at)}
                  </TableCell>
                  <TableCell className="font-medium">{item.actor}</TableCell>
                  <TableCell className="font-mono text-xs">
                    {item.previous_state} → {item.new_state}
                  </TableCell>
                  <TableCell className="text-muted-foreground">
                    {item.reason}
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

function DecisionHistory() {
  const [state, setState] = useState<string>(ALL)
  const [mode, setMode] = useState<string>(ALL)
  const [repo, setRepo] = useState("")
  const [page, setPage] = useState(0)
  const [refreshKey, setRefreshKey] = useState(0)
  const [expanded, setExpanded] = useState<number | null>(null)
  const [overriding, setOverriding] = useState<GateDecisionModel | null>(null)

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
      state: state === ALL ? undefined : (state as GateState),
      mode: mode === ALL ? undefined : mode,
    }),
    [owner, repoName, state, mode]
  )

  const { data: summary, loading: summaryLoading } = useAsync<GateSummary>(
    () => api.getGateSummary({ owner, repo: repoName }),
    [owner, repoName, refreshKey]
  )

  const { data, loading, error } = useAsync<GateDecisionPage>(
    () =>
      api.listGateDecisions({
        ...filters,
        limit: PAGE_SIZE,
        offset: page * PAGE_SIZE,
      }),
    [filters, page, refreshKey]
  )

  const refresh = useCallback(() => setRefreshKey((key) => key + 1), [])
  const decisions = data?.decisions ?? []
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
              <SelectTrigger className="h-9 w-44">
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
              <SelectTrigger className="h-9 w-40">
                <SelectValue placeholder="Any mode" />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value={ALL}>Any mode</SelectItem>
                <SelectItem value="off">Off</SelectItem>
                <SelectItem value="shadow">Shadow</SelectItem>
                <SelectItem value="enforce">Enforce</SelectItem>
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
          ) : decisions.length === 0 ? (
            <div className="flex flex-col items-center gap-2 px-6 py-12 text-center">
              <Gavel className="h-8 w-8 text-muted-foreground" />
              <p className="text-sm font-medium">No decisions recorded yet</p>
              <p className="max-w-md text-sm text-muted-foreground">
                The gate ships off. Set it to{" "}
                <span className="font-mono">shadow</span> on the Policy tab to
                start recording what it would decide, without it doing anything.
              </p>
            </div>
          ) : (
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>Pull request</TableHead>
                  <TableHead>State</TableHead>
                  <TableHead className="w-20 text-right">Risk</TableHead>
                  <TableHead>Mode</TableHead>
                  <TableHead>Decided</TableHead>
                  <TableHead className="w-24" />
                </TableRow>
              </TableHeader>
              <TableBody>
                {decisions.map((decision) => (
                  <Fragment key={decision.id}>
                    <TableRow
                      className="cursor-pointer"
                      onClick={() =>
                        setExpanded(
                          expanded === decision.id ? null : decision.id
                        )
                      }
                    >
                      <TableCell>
                        <div className="flex items-center gap-2">
                          <span className="font-medium">
                            {decision.owner}/{decision.repo}#
                            {decision.pr_number}
                          </span>
                          {decision.pr_url && (
                            <a
                              href={decision.pr_url}
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
                          {decision.pr_author || "unknown author"} ·{" "}
                          {decision.head_sha.slice(0, 8)}
                        </span>
                      </TableCell>
                      <TableCell>
                        <StateBadge state={decision.state} />
                      </TableCell>
                      <TableCell
                        className={cn(
                          "text-right tabular-nums",
                          BAND_STYLE[decision.risk_band]
                        )}
                      >
                        {decision.risk_score}
                      </TableCell>
                      <TableCell className="font-mono text-xs">
                        {decision.mode}
                      </TableCell>
                      <TableCell className="text-xs whitespace-nowrap text-muted-foreground">
                        {formatDate(decision.created_at)}
                      </TableCell>
                      <TableCell>
                        <Button
                          variant="ghost"
                          size="sm"
                          onClick={(event) => {
                            event.stopPropagation()
                            setOverriding(decision)
                          }}
                        >
                          Override
                        </Button>
                      </TableCell>
                    </TableRow>
                    {expanded === decision.id && (
                      <TableRow>
                        <TableCell colSpan={6} className="p-0">
                          <DecisionDetail
                            owner={decision.owner}
                            repo={decision.repo}
                            id={decision.id}
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

      <OverrideDialog
        decision={overriding}
        open={overriding !== null}
        onOpenChange={(open) => !open && setOverriding(null)}
        onDone={refresh}
      />
    </div>
  )
}

export function MergeGatePage() {
  useDocumentTitle("Merge gate")
  const [params, setParams] = useSearchParams()
  const tab = params.get("tab") === "policy" ? "policy" : "history"

  return (
    <div className="space-y-6 p-6">
      <div>
        <h1 className="text-2xl font-semibold tracking-tight">Merge gate</h1>
        <p className="max-w-3xl text-sm text-muted-foreground">
          A conservative, explainable approval decision, kept separate from the
          review&apos;s quality score. It ships off; shadow mode records exactly
          what it would have decided so you can measure false approvals before
          letting it act.
        </p>
      </div>

      <Tabs
        value={tab}
        onValueChange={(value) =>
          setParams(value === "policy" ? { tab: "policy" } : {})
        }
      >
        <TabsList>
          <TabsTrigger value="history">History</TabsTrigger>
          <TabsTrigger value="policy">Policy</TabsTrigger>
        </TabsList>
        <TabsContent value="history" className="pt-4">
          <DecisionHistory />
        </TabsContent>
        <TabsContent value="policy" className="pt-4">
          <GatePolicyPanel />
        </TabsContent>
      </Tabs>
    </div>
  )
}
