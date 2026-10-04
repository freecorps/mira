import { ChevronRight, ExternalLink, FlaskConical, Target } from "lucide-react"
import { Fragment, useState } from "react"
import { Link } from "react-router"

import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card"
import { Skeleton } from "@/components/ui/skeleton"
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table"
import { api } from "@/lib/api"
import type { BacktestRun, EscapedBug, QualityScore } from "@/lib/api/quality"
import { useAsync, useDocumentTitle } from "@/lib/hooks"

function pct(value: number | null | undefined): string {
  return value === null || value === undefined
    ? "—"
    : `${Math.round(value * 100)}%`
}

function usd(value: number): string {
  return value < 1 ? `$${value.toFixed(4)}` : `$${value.toFixed(2)}`
}

function when(epoch: number): string {
  return epoch ? new Date(epoch * 1000).toLocaleString() : "—"
}

function Stat({
  label,
  value,
  hint,
}: {
  label: string
  value: string
  hint?: string
}) {
  return (
    <div className="rounded-lg border p-4">
      <p className="text-xs text-muted-foreground">{label}</p>
      <p className="mt-1 text-2xl font-semibold tabular-nums">{value}</p>
      {hint && <p className="mt-1 text-xs text-muted-foreground">{hint}</p>}
    </div>
  )
}

function RecallCard() {
  const { data, loading, error } = useAsync(() => api.getQualitySummary(), [])
  const totals = data?.totals
  return (
    <Card>
      <CardHeader>
        <CardTitle className="flex items-center gap-2 text-base">
          <Target className="h-4 w-4" /> Real-world recall
        </CardTitle>
        <CardDescription>
          Reverts and hotfixes traced back to pull requests Mira reviewed.
          Caught means Mira had flagged the lines that were later reverted or
          fixed.
        </CardDescription>
      </CardHeader>
      <CardContent className="space-y-4">
        {loading ? (
          <Skeleton className="h-20 w-full" />
        ) : error ? (
          <p className="text-sm text-destructive">{error}</p>
        ) : !totals || totals.total === 0 ? (
          <p className="text-sm text-muted-foreground">
            No escaped bugs recorded yet. Enable{" "}
            <code>escaped_bugs.enabled</code> or run{" "}
            <code>mira escaped-bugs scan --repo owner/name</code>.
          </p>
        ) : (
          <>
            <div className="grid gap-3 sm:grid-cols-3">
              <Stat
                label="Recall"
                value={pct(totals.recall)}
                hint="caught / escaped incidents"
              />
              <Stat label="Caught" value={String(totals.caught)} />
              <Stat label="Missed" value={String(totals.missed)} />
            </div>
            {data && data.repos.length > 1 && (
              <Table>
                <TableHeader>
                  <TableRow>
                    <TableHead>Repository</TableHead>
                    <TableHead className="text-right">Caught</TableHead>
                    <TableHead className="text-right">Missed</TableHead>
                    <TableHead className="text-right">Recall</TableHead>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {data.repos.map((r) => (
                    <TableRow key={`${r.platform}/${r.owner}/${r.repo}`}>
                      <TableCell>
                        <Link
                          to={`/repos/${r.owner}/${r.repo}`}
                          className="hover:underline"
                        >
                          {r.owner}/{r.repo}
                        </Link>
                      </TableCell>
                      <TableCell className="text-right tabular-nums">
                        {r.caught}
                      </TableCell>
                      <TableCell className="text-right tabular-nums">
                        {r.missed}
                      </TableCell>
                      <TableCell className="text-right tabular-nums">
                        {pct(r.recall)}
                      </TableCell>
                    </TableRow>
                  ))}
                </TableBody>
              </Table>
            )}
          </>
        )}
      </CardContent>
    </Card>
  )
}

function BugLink({ url, label }: { url: string; label: string }) {
  if (!url) return <span>{label}</span>
  return (
    <a
      href={url}
      target="_blank"
      rel="noreferrer"
      className="inline-flex items-center gap-1 hover:underline"
    >
      {label}
      <ExternalLink className="h-3 w-3" />
    </a>
  )
}

function fixLabel(bug: EscapedBug): string {
  return bug.fix_pr_number ? `#${bug.fix_pr_number}` : bug.fix_sha.slice(0, 10)
}

function EscapedBugsCard() {
  const [filter, setFilter] = useState<"" | "yes" | "no">("")
  const { data, loading, error } = useAsync(
    () => api.listEscapedBugs(filter),
    [filter]
  )
  const bugs = data ?? []
  return (
    <Card>
      <CardHeader>
        <div className="flex flex-wrap items-center justify-between gap-2">
          <div>
            <CardTitle className="text-base">Escaped bugs</CardTitle>
            <CardDescription>
              Missed ones are added to Learnings as pending candidates when
              enabled.
            </CardDescription>
          </div>
          <div className="flex gap-1">
            {(
              [
                ["", "All"],
                ["no", "Missed"],
                ["yes", "Caught"],
              ] as const
            ).map(([value, label]) => (
              <Button
                key={label}
                size="sm"
                variant={filter === value ? "secondary" : "ghost"}
                onClick={() => setFilter(value)}
              >
                {label}
              </Button>
            ))}
          </div>
        </div>
      </CardHeader>
      <CardContent className="px-0 pb-0">
        {loading ? (
          <div className="px-6 pb-6">
            <Skeleton className="h-24 w-full" />
          </div>
        ) : error ? (
          <p className="px-6 pb-6 text-sm text-destructive">{error}</p>
        ) : bugs.length === 0 ? (
          <p className="px-6 pb-6 text-sm text-muted-foreground">
            Nothing recorded.
          </p>
        ) : (
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead className="pl-6">Detected</TableHead>
                <TableHead>Repository</TableHead>
                <TableHead>Kind</TableHead>
                <TableHead>Fix</TableHead>
                <TableHead>Original PR</TableHead>
                <TableHead>Lines</TableHead>
                <TableHead>Mira</TableHead>
                <TableHead className="pr-6">Learning</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {bugs.map((bug) => (
                <TableRow key={bug.id}>
                  <TableCell className="pl-6 text-xs text-muted-foreground">
                    {when(bug.detected_at)}
                  </TableCell>
                  <TableCell className="text-sm">
                    {bug.owner}/{bug.repo}
                  </TableCell>
                  <TableCell>
                    <Badge variant="outline">{bug.kind}</Badge>
                  </TableCell>
                  <TableCell className="max-w-[240px] text-sm">
                    <BugLink url={bug.fix_url} label={fixLabel(bug)} />
                    <p className="line-clamp-1 text-xs text-muted-foreground">
                      {bug.fix_title}
                    </p>
                  </TableCell>
                  <TableCell className="text-sm">
                    <BugLink
                      url={bug.original_pr_url}
                      label={`#${bug.original_pr_number}`}
                    />
                    <p className="text-xs text-muted-foreground">
                      via {bug.link_method}
                    </p>
                  </TableCell>
                  <TableCell className="font-mono text-xs">
                    {bug.path}:{bug.line_start}
                    {bug.line_end > bug.line_start ? `-${bug.line_end}` : ""}
                  </TableCell>
                  <TableCell>
                    {bug.flagged ? (
                      <Badge variant="secondary">flagged</Badge>
                    ) : (
                      <Badge variant="destructive">
                        missed{bug.flagged_file ? " (same file)" : ""}
                      </Badge>
                    )}
                  </TableCell>
                  <TableCell className="pr-6 text-sm">
                    {bug.learning_candidate_id ? (
                      <Link
                        className="hover:underline"
                        to={`/learnings?tab=pending&candidate=${bug.learning_candidate_id}&owner=${encodeURIComponent(bug.owner)}&repo=${encodeURIComponent(bug.repo)}`}
                      >
                        candidate #{bug.learning_candidate_id}
                      </Link>
                    ) : (
                      "—"
                    )}
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        )}
      </CardContent>
    </Card>
  )
}

function ScoreCells({ score }: { score: QualityScore }) {
  return (
    <>
      <TableCell className="text-right tabular-nums">
        {score.findings}
      </TableCell>
      <TableCell className="text-right tabular-nums">
        {pct(score.precision)}
      </TableCell>
      <TableCell className="text-right tabular-nums">
        {pct(score.labelled_precision)}
      </TableCell>
      <TableCell className="text-right tabular-nums">
        {pct(score.recall)}
      </TableCell>
    </>
  )
}

function RunDetail({ run }: { run: BacktestRun }) {
  const { data, loading, error } = useAsync(
    () => api.getBacktest(run.owner, run.repo, run.id, run.platform),
    [run.id]
  )
  if (loading) return <Skeleton className="h-16 w-full" />
  if (error) return <p className="text-sm text-destructive">{error}</p>
  const results = data?.results ?? []
  return (
    <div className="space-y-2">
      <Table>
        <TableHeader>
          <TableRow>
            <TableHead>PR</TableHead>
            <TableHead>Variant</TableHead>
            <TableHead className="text-right">Findings</TableHead>
            <TableHead className="text-right">TP / FP</TableHead>
            <TableHead className="text-right">Caught / signals</TableHead>
            <TableHead className="text-right">Cost</TableHead>
            <TableHead className="text-right">Latency</TableHead>
            <TableHead>Status</TableHead>
          </TableRow>
        </TableHeader>
        <TableBody>
          {results.map((r) => (
            <TableRow key={`${r.variant}-${r.pr_number}`}>
              <TableCell className="max-w-[260px] text-sm">
                <BugLink url={r.pr_url} label={`#${r.pr_number}`} />{" "}
                <span className="text-xs text-muted-foreground">
                  {r.pr_title}
                </span>
              </TableCell>
              <TableCell>{r.variant}</TableCell>
              <TableCell className="text-right tabular-nums">
                {r.score.findings}
              </TableCell>
              <TableCell className="text-right tabular-nums">
                {r.score.tp} / {r.score.fp}
              </TableCell>
              <TableCell className="text-right tabular-nums">
                {r.score.positives_caught} / {r.score.positives}
              </TableCell>
              <TableCell className="text-right tabular-nums">
                {usd(r.cost_usd)}
              </TableCell>
              <TableCell className="text-right tabular-nums">
                {(r.latency_ms / 1000).toFixed(1)}s
              </TableCell>
              <TableCell className="text-xs text-muted-foreground">
                {r.error || r.skipped_reason || "ok"}
              </TableCell>
            </TableRow>
          ))}
        </TableBody>
      </Table>
      {run.notes.length > 0 && (
        <ul className="list-disc pl-6 text-xs text-muted-foreground">
          {run.notes.map((note) => (
            <li key={note}>{note}</li>
          ))}
        </ul>
      )}
    </div>
  )
}

function BacktestsCard() {
  const { data, loading, error } = useAsync(() => api.listBacktests(), [])
  const [open, setOpen] = useState<string | null>(null)
  const runs = data ?? []
  return (
    <Card>
      <CardHeader>
        <CardTitle className="flex items-center gap-2 text-base">
          <FlaskConical className="h-4 w-4" /> Backtests
        </CardTitle>
        <CardDescription>
          Dry-run replays of merged pull requests, started with{" "}
          <code>mira backtest --repo owner/name --confirm</code>. Strict
          precision counts findings with no evidence as misses; labelled
          precision ignores them.
        </CardDescription>
      </CardHeader>
      <CardContent className="px-0 pb-0">
        {loading ? (
          <div className="px-6 pb-6">
            <Skeleton className="h-24 w-full" />
          </div>
        ) : error ? (
          <p className="px-6 pb-6 text-sm text-destructive">{error}</p>
        ) : runs.length === 0 ? (
          <p className="px-6 pb-6 text-sm text-muted-foreground">
            No backtests stored yet.
          </p>
        ) : (
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead className="w-[32px] pl-6"></TableHead>
                <TableHead>Run</TableHead>
                <TableHead>Variant</TableHead>
                <TableHead className="text-right">PRs</TableHead>
                <TableHead className="text-right">Findings</TableHead>
                <TableHead className="text-right">Precision</TableHead>
                <TableHead className="text-right">Labelled</TableHead>
                <TableHead className="text-right">Recall</TableHead>
                <TableHead className="text-right">Cost</TableHead>
                <TableHead className="pr-6 text-right">Latency</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {runs.map((run) => {
                const isOpen = open === run.id
                const summaries = run.summaries.length ? run.summaries : []
                return (
                  <Fragment key={run.id}>
                    {summaries.map((s, i) => (
                      <TableRow
                        key={`${run.id}-${s.variant}`}
                        className="cursor-pointer"
                        onClick={() => setOpen(isOpen ? null : run.id)}
                      >
                        <TableCell className="pl-6">
                          {i === 0 && (
                            <ChevronRight
                              className={`h-4 w-4 text-muted-foreground transition-transform ${isOpen ? "rotate-90" : ""}`}
                            />
                          )}
                        </TableCell>
                        <TableCell className="text-sm">
                          {i === 0 && (
                            <>
                              {run.owner}/{run.repo}
                              <p className="text-xs text-muted-foreground">
                                {when(run.created_at)} · {run.status}
                              </p>
                            </>
                          )}
                        </TableCell>
                        <TableCell className="text-sm">
                          {s.variant}{" "}
                          <span className="font-mono text-xs text-muted-foreground">
                            {s.config_label}
                          </span>
                        </TableCell>
                        <TableCell className="text-right tabular-nums">
                          {s.prs}
                        </TableCell>
                        <ScoreCells score={s.score} />
                        <TableCell className="text-right tabular-nums">
                          {usd(s.cost_usd)}
                        </TableCell>
                        <TableCell className="pr-6 text-right tabular-nums">
                          {(s.mean_latency_ms / 1000).toFixed(1)}s
                        </TableCell>
                      </TableRow>
                    ))}
                    {isOpen && (
                      <TableRow className="bg-muted/30 hover:bg-muted/30">
                        <TableCell colSpan={10} className="px-6 py-4">
                          <RunDetail run={run} />
                        </TableCell>
                      </TableRow>
                    )}
                  </Fragment>
                )
              })}
            </TableBody>
          </Table>
        )}
      </CardContent>
    </Card>
  )
}

export function ReviewQualityPage() {
  useDocumentTitle("Review quality")
  return (
    <div className="space-y-6 p-6">
      <div>
        <h1 className="text-2xl font-semibold tracking-tight">
          Review quality
        </h1>
        <p className="text-sm text-muted-foreground">
          How often Mira catches what later turns out to be a bug, measured on
          real history.
        </p>
      </div>
      <RecallCard />
      <EscapedBugsCard />
      <BacktestsCard />
    </div>
  )
}
