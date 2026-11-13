import { ExternalLink, RefreshCw, Users } from "lucide-react"
import { useState } from "react"
import { CartesianGrid, Line, LineChart, XAxis, YAxis } from "recharts"

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
  type ChartConfig,
  ChartContainer,
  ChartLegend,
  ChartLegendContent,
  ChartTooltip,
  ChartTooltipContent,
} from "@/components/ui/chart"
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
import { api } from "@/lib/api"
import type { PeerCounts, PeerRecallSummary } from "@/lib/api/quality"
import { useAsync } from "@/lib/hooks"

// The chart palette tokens, in order; peer bots past the fifth reuse them.
const SERIES = [
  "var(--chart-1)",
  "var(--chart-2)",
  "var(--chart-3)",
  "var(--chart-4)",
  "var(--chart-5)",
]

const LEARNING_LABEL: Record<string, string> = {
  pending: "queued",
  skipped: "no pattern",
  collecting: "collecting",
  active: "rule active",
  error: "failed",
}

function pct(value: number | null | undefined): string {
  return value === null || value === undefined
    ? "—"
    : `${Math.round(value * 100)}%`
}

function when(epoch: number): string {
  return epoch ? new Date(epoch * 1000).toLocaleDateString() : "—"
}

// Recharts keys must be identifiers; logins carry brackets and dashes.
function seriesKey(bot: string): string {
  return `b_${bot.replace(/[^A-Za-z0-9]/g, "_")}`
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

function CountCells({ row }: { row: PeerCounts }) {
  return (
    <>
      <TableCell className="text-right tabular-nums">{row.threads}</TableCell>
      <TableCell className="text-right tabular-nums">{row.addressed}</TableCell>
      <TableCell className="text-right tabular-nums">
        {row.addressed_flagged}
      </TableCell>
      <TableCell className="text-right tabular-nums">
        {row.missed_addressed}
      </TableCell>
      <TableCell className="pr-6 text-right font-medium tabular-nums">
        {pct(row.recall)}
      </TableCell>
    </>
  )
}

function CountHeads({ first }: { first: string }) {
  return (
    <TableRow>
      <TableHead className="pl-6">{first}</TableHead>
      <TableHead className="text-right">Threads</TableHead>
      <TableHead className="text-right">Addressed</TableHead>
      <TableHead className="text-right">Caught</TableHead>
      <TableHead className="text-right">Missed</TableHead>
      <TableHead className="pr-6 text-right">Recall</TableHead>
    </TableRow>
  )
}

function RecallChart({ data }: { data: PeerRecallSummary }) {
  const bots = data.bots.map((b) => b.bot)
  if (data.weeks.length < 2 || bots.length === 0) return null
  const config: ChartConfig = Object.fromEntries(
    bots.map((bot, i) => [
      seriesKey(bot),
      { label: bot, color: SERIES[i % SERIES.length] },
    ])
  )
  const byWeek = new Map<string, Record<string, number | string | null>>()
  for (const point of data.weeks) {
    const row = byWeek.get(point.week) ?? { week: point.week }
    row[seriesKey(point.bot)] =
      point.recall === null ? null : Math.round(point.recall * 100)
    byWeek.set(point.week, row)
  }
  const points = [...byWeek.values()].sort((a, b) =>
    String(a.week).localeCompare(String(b.week))
  )
  return (
    <div className="px-6">
      <p className="mb-2 text-xs text-muted-foreground">
        Weekly recall on addressed threads, by the week the pull request closed.
      </p>
      <ChartContainer config={config} className="h-[220px] w-full">
        <LineChart data={points}>
          <CartesianGrid vertical={false} />
          <XAxis
            dataKey="week"
            tickLine={false}
            axisLine={false}
            tickMargin={8}
          />
          <YAxis
            domain={[0, 100]}
            tickLine={false}
            axisLine={false}
            width={36}
            tickFormatter={(v: number) => `${v}%`}
          />
          <ChartTooltip content={<ChartTooltipContent />} />
          <ChartLegend content={<ChartLegendContent />} />
          {bots.map((bot) => (
            <Line
              key={bot}
              type="monotone"
              dataKey={seriesKey(bot)}
              stroke={`var(--color-${seriesKey(bot)})`}
              strokeWidth={2}
              dot={false}
              connectNulls
            />
          ))}
        </LineChart>
      </ChartContainer>
    </div>
  )
}

function MissedList({ bot, category }: { bot: string; category: string }) {
  const { data, loading, error } = useAsync(
    () => api.listMissedPeerThreads(bot, category),
    [bot, category]
  )
  const threads = data ?? []
  if (loading)
    return (
      <div className="px-6 pb-6">
        <Skeleton className="h-24 w-full" />
      </div>
    )
  if (error)
    return <p className="px-6 pb-6 text-sm text-destructive">{error}</p>
  if (threads.length === 0)
    return (
      <p className="px-6 pb-6 text-sm text-muted-foreground">
        Nothing missed and addressed.
      </p>
    )
  return (
    <Table>
      <TableHeader>
        <TableRow>
          <TableHead className="pl-6">Closed</TableHead>
          <TableHead>Pull request</TableHead>
          <TableHead>Bot</TableHead>
          <TableHead>Finding</TableHead>
          <TableHead>Lines</TableHead>
          <TableHead className="pr-6">Learning</TableHead>
        </TableRow>
      </TableHeader>
      <TableBody>
        {threads.map((t) => (
          <TableRow key={t.id}>
            <TableCell className="pl-6 text-xs text-muted-foreground">
              {when(t.pr_closed_at)}
            </TableCell>
            <TableCell className="text-sm">
              {t.pr_url ? (
                <a
                  href={t.pr_url}
                  target="_blank"
                  rel="noreferrer"
                  className="hover:underline"
                >
                  {t.owner}/{t.repo}#{t.pr_number}
                </a>
              ) : (
                `${t.owner}/${t.repo}#${t.pr_number}`
              )}
            </TableCell>
            <TableCell className="text-xs">{t.bot}</TableCell>
            <TableCell className="max-w-[360px] text-sm">
              {t.url ? (
                <a
                  href={t.url}
                  target="_blank"
                  rel="noreferrer"
                  className="inline-flex items-start gap-1 hover:underline"
                >
                  <span className="line-clamp-2">
                    {t.title || "(untitled)"}
                  </span>
                  <ExternalLink className="mt-1 h-3 w-3 shrink-0" />
                </a>
              ) : (
                <span className="line-clamp-2">{t.title}</span>
              )}
              <p className="text-xs text-muted-foreground">
                {t.category} · addressed by {t.addressed_reason}
              </p>
            </TableCell>
            <TableCell className="font-mono text-xs">
              {t.path}
              {t.line ? `:${t.line}` : ""}
            </TableCell>
            <TableCell className="pr-6">
              <Badge variant="outline">
                {LEARNING_LABEL[t.learning_state || "pending"] ??
                  t.learning_state}
              </Badge>
            </TableCell>
          </TableRow>
        ))}
      </TableBody>
    </Table>
  )
}

export function PeerRecallCard() {
  const [refresh, setRefresh] = useState(0)
  const [bot, setBot] = useState("")
  const [category, setCategory] = useState("")
  const [starting, setStarting] = useState(false)
  const { data, loading, error } = useAsync(
    () => api.getPeerRecall(),
    [refresh]
  )

  async function runNow() {
    setStarting(true)
    try {
      await api.runPeerRecall()
      toast.success("Peer recall started; refresh in a few minutes")
      setRefresh((n) => n + 1)
    } catch (err) {
      toast.error(err instanceof Error ? err.message : "Could not start")
    } finally {
      setStarting(false)
    }
  }

  const totals = data?.totals
  const repoCount = new Set(data?.repos.map((r) => `${r.owner}/${r.repo}`)).size
  const last = data?.last_run
  return (
    <Card>
      <CardHeader>
        <div className="flex flex-wrap items-start justify-between gap-2">
          <div>
            <CardTitle className="flex items-center gap-2 text-base">
              <Users className="h-4 w-4" /> Recall vs peer review bots
            </CardTitle>
            <CardDescription>
              Review threads other bots opened on pull requests Mira also
              reviewed. Recall is the share of threads the author addressed
              where Mira had a finding within a few lines. Missed ones teach
              Mira what to also look for
              {data && !data.positive_learning ? " (learning is off)" : ""}.
            </CardDescription>
          </div>
          <Button
            size="sm"
            variant="outline"
            disabled={starting || data?.running}
            onClick={runNow}
          >
            <RefreshCw
              className={`mr-1 h-3.5 w-3.5 ${data?.running ? "animate-spin" : ""}`}
            />
            {data?.running ? "Running…" : "Run now"}
          </Button>
        </div>
        {last && (
          <p className="text-xs text-muted-foreground">
            Last run {new Date(last.finished_at * 1000).toLocaleString()} (
            {last.trigger}):{" "}
            {last.repos.reduce((n, r) => n + r.prs_examined, 0)} PR(s) read,{" "}
            {last.repos.reduce((n, r) => n + r.rules_activated.length, 0)}{" "}
            rule(s) activated
            {last.repos.some((r) => r.error)
              ? `, ${last.repos.filter((r) => r.error).length} failed`
              : ""}
            .
          </p>
        )}
      </CardHeader>
      <CardContent className="space-y-6 px-0 pb-0">
        {loading ? (
          <div className="px-6 pb-6">
            <Skeleton className="h-24 w-full" />
          </div>
        ) : error ? (
          <p className="px-6 pb-6 text-sm text-destructive">{error}</p>
        ) : !data || !totals || totals.threads === 0 ? (
          <p className="px-6 pb-6 text-sm text-muted-foreground">
            No peer threads recorded yet. They are collected daily by{" "}
            <code>mira serve</code>, or run{" "}
            <code>mira peer-recall scan --repo owner/name</code>.
          </p>
        ) : (
          <>
            <div className="grid gap-3 px-6 sm:grid-cols-3">
              <Stat
                label="Recall"
                value={pct(totals.recall)}
                hint={`${totals.addressed_flagged} of ${totals.addressed} addressed threads`}
              />
              <Stat
                label="Missed and addressed"
                value={String(totals.missed_addressed)}
                hint="what Mira should have said"
              />
              <Stat
                label="Overlap, all threads"
                value={pct(totals.overlap)}
                hint={`${totals.flagged} of ${totals.threads}, noise included`}
              />
            </div>
            <RecallChart data={data} />
            <Table>
              <TableHeader>
                <CountHeads first="Peer bot" />
              </TableHeader>
              <TableBody>
                {data.bots.map((row) => (
                  <TableRow
                    key={row.bot}
                    className="cursor-pointer"
                    data-state={bot === row.bot ? "selected" : undefined}
                    onClick={() => setBot(bot === row.bot ? "" : row.bot)}
                  >
                    <TableCell className="pl-6 text-sm">{row.bot}</TableCell>
                    <CountCells row={row} />
                  </TableRow>
                ))}
              </TableBody>
            </Table>
            <Table>
              <TableHeader>
                <CountHeads first="Category" />
              </TableHeader>
              <TableBody>
                {data.categories.map((row) => (
                  <TableRow
                    key={row.category}
                    className="cursor-pointer"
                    data-state={
                      category === row.category ? "selected" : undefined
                    }
                    onClick={() =>
                      setCategory(category === row.category ? "" : row.category)
                    }
                  >
                    <TableCell className="pl-6 text-sm">
                      {row.category}
                    </TableCell>
                    <CountCells row={row} />
                  </TableRow>
                ))}
              </TableBody>
            </Table>
            {repoCount > 1 && (
              <Table>
                <TableHeader>
                  <CountHeads first="Repository / bot" />
                </TableHeader>
                <TableBody>
                  {data.repos.map((row) => (
                    <TableRow
                      key={`${row.platform}/${row.owner}/${row.repo}/${row.bot}`}
                    >
                      <TableCell className="pl-6 text-sm">
                        {row.owner}/{row.repo}{" "}
                        <span className="text-xs text-muted-foreground">
                          {row.bot}
                        </span>
                      </TableCell>
                      <CountCells row={row} />
                    </TableRow>
                  ))}
                </TableBody>
              </Table>
            )}
            <div>
              <div className="flex flex-wrap items-center gap-2 px-6 pb-2">
                <p className="text-sm font-medium">Missed and addressed</p>
                {bot && (
                  <button type="button" onClick={() => setBot("")}>
                    <Badge variant="secondary">{bot} ×</Badge>
                  </button>
                )}
                {category && (
                  <button type="button" onClick={() => setCategory("")}>
                    <Badge variant="secondary">{category} ×</Badge>
                  </button>
                )}
                <p className="text-xs text-muted-foreground">
                  Click a bot or category above to filter.
                </p>
              </div>
              <MissedList bot={bot} category={category} />
            </div>
          </>
        )}
      </CardContent>
    </Card>
  )
}
