import { ArrowDown, ArrowUp, Info, Rocket } from "lucide-react"
import { type ReactNode, useState } from "react"
import { Bar, BarChart, CartesianGrid, XAxis, YAxis } from "recharts"

import {
  Card,
  CardContent,
  CardDescription,
  CardFooter,
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
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select"
import { Skeleton } from "@/components/ui/skeleton"
import {
  Tooltip,
  TooltipContent,
  TooltipTrigger,
} from "@/components/ui/tooltip"
import { api, type DoraBand } from "@/lib/api"
import { useAsync } from "@/lib/hooks"

const PERIODS = [7, 30, 90, 180]
const ALL = "__all__"

const chartConfig = {
  deployments: { label: "Deployments", color: "var(--chart-2)" },
  failures: { label: "Failure fixes", color: "#dc2626" },
} satisfies ChartConfig

function fmtDuration(secs: number | null | undefined): string {
  if (secs == null) return "—"
  if (secs < 60) return "<1m"
  const mins = Math.floor(secs / 60)
  if (mins < 60) return `${mins}m`
  const hours = Math.floor(mins / 60)
  if (hours < 24) {
    const m = mins % 60
    return m ? `${hours}h ${m}m` : `${hours}h`
  }
  const days = Math.floor(hours / 24)
  const h = hours % 24
  return h ? `${days}d ${h}h` : `${days}d`
}

function fmtRate(perDay: number): string {
  if (perDay >= 1) return `${perDay.toFixed(1)}/day`
  if (perDay * 7 >= 1) return `${(perDay * 7).toFixed(1)}/week`
  if (perDay * 30 >= 0.1) return `${(perDay * 30).toFixed(1)}/month`
  return "none"
}

const BAND_STYLE: Record<DoraBand, string> = {
  elite: "bg-emerald-500/15 text-emerald-700 dark:text-emerald-400",
  high: "bg-sky-500/15 text-sky-700 dark:text-sky-400",
  medium: "bg-amber-500/15 text-amber-700 dark:text-amber-400",
  low: "bg-red-500/15 text-red-700 dark:text-red-400",
}

function BandBadge({ band }: { band: DoraBand | null | undefined }) {
  if (!band) return null
  return (
    <span
      className={`rounded-full px-2 py-0.5 text-[11px] font-medium capitalize ${BAND_STYLE[band]}`}
    >
      {band}
    </span>
  )
}

/** `lowerIsBetter`: a decrease is shown green (durations, failure rate). */
function Trend({
  current,
  previous,
  lowerIsBetter,
  days,
}: {
  current: number | null | undefined
  previous: number | null | undefined
  lowerIsBetter: boolean
  days: number
}) {
  if (current == null || previous == null || previous === 0) {
    return <span className="text-muted-foreground">no prior data</span>
  }
  const delta = current - previous
  if (delta === 0)
    return (
      <span className="text-muted-foreground">no change vs prev {days}d</span>
    )
  const better = lowerIsBetter ? delta < 0 : delta > 0
  const pct = Math.round((Math.abs(delta) / previous) * 100)
  const Icon = delta < 0 ? ArrowDown : ArrowUp
  return (
    <span className="inline-flex items-center gap-1">
      <span
        className={`inline-flex items-center gap-0.5 font-medium ${
          better
            ? "text-emerald-600 dark:text-emerald-500"
            : "text-red-600 dark:text-red-500"
        }`}
      >
        <Icon className="h-3.5 w-3.5" />
        {pct}%
      </span>
      <span className="text-muted-foreground">vs prev {days}d</span>
    </span>
  )
}

function MetricCard({
  label,
  tip,
  value,
  band,
  footer,
  loading,
}: {
  label: string
  tip: string
  value: string
  band?: DoraBand | null
  footer: ReactNode
  loading: boolean
}) {
  return (
    <Card>
      <CardHeader className="pb-2">
        <CardDescription className="flex items-center justify-between gap-2">
          <span className="flex items-center gap-1">
            {label}
            <Tooltip>
              <TooltipTrigger asChild>
                <button
                  type="button"
                  className="text-muted-foreground hover:text-foreground"
                >
                  <Info className="h-3.5 w-3.5" />
                </button>
              </TooltipTrigger>
              <TooltipContent className="max-w-xs">{tip}</TooltipContent>
            </Tooltip>
          </span>
          {!loading && <BandBadge band={band} />}
        </CardDescription>
        <CardTitle className="text-3xl tabular-nums">
          {loading ? <Skeleton className="h-9 w-20" /> : value}
        </CardTitle>
      </CardHeader>
      <CardFooter className="text-sm text-muted-foreground">
        {loading ? <Skeleton className="h-4 w-32" /> : footer}
      </CardFooter>
    </Card>
  )
}

function fmtBucket(start: number, bucketSecs: number): string {
  const d = new Date(start * 1000)
  return bucketSecs >= 7 * 86400
    ? `wk of ${d.toLocaleDateString(undefined, { month: "short", day: "numeric" })}`
    : d.toLocaleDateString(undefined, { month: "short", day: "numeric" })
}

/** DORA metrics + PR cycle-time breakdown, with period and repo selectors. */
export function DoraPanel() {
  const [days, setDays] = useState(30)
  const [repo, setRepo] = useState(ALL)
  const { data, loading, error } = useAsync(
    () => api.getDora(days, repo === ALL ? "" : repo),
    [days, repo]
  )
  const cur = data?.current
  const prev = data?.previous
  const series = (data?.series ?? []).map((b) => ({
    label: fmtBucket(b.start, data?.bucket_secs ?? 86400),
    deployments: b.deployments,
    failures: b.failures,
  }))
  const deploymentWord =
    data?.deployment_source === "releases" ? "releases" : "merges to default"

  return (
    <section className="space-y-4" aria-labelledby="dora-heading">
      <div className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <h2
            id="dora-heading"
            className="flex items-center gap-2 text-lg font-semibold"
          >
            <Rocket className="h-4 w-4" /> Delivery (DORA) &amp; cycle time
          </h2>
          <p className="text-sm text-muted-foreground">
            Proxies from merged PRs Mira tracks; bands follow the State of
            DevOps tiers.
          </p>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          <Select value={repo} onValueChange={setRepo}>
            <SelectTrigger className="w-56" aria-label="Repository">
              <SelectValue placeholder="All repos" />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value={ALL}>All repos</SelectItem>
              {(data?.repos ?? []).map((r) => (
                <SelectItem key={r} value={r}>
                  {r}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
          <div className="inline-flex gap-1" role="group" aria-label="Period">
            {PERIODS.map((p) => (
              <button
                key={p}
                type="button"
                aria-pressed={days === p}
                onClick={() => setDays(p)}
                className={`inline-flex h-8 items-center rounded-md border px-3 text-xs font-medium transition-colors focus-visible:ring-2 focus-visible:ring-ring focus-visible:outline-none ${
                  days === p
                    ? "border-primary bg-primary/10 text-primary"
                    : "border-input bg-background text-muted-foreground hover:bg-accent hover:text-accent-foreground"
                }`}
              >
                {p}d
              </button>
            ))}
          </div>
        </div>
      </div>

      {error && <p className="text-sm text-destructive">{error}</p>}
      {data && !data.enabled && (
        <p className="text-sm text-muted-foreground">
          DORA metrics are turned off (<code>analytics.dora.enabled</code>).
        </p>
      )}

      {(loading || data?.enabled) && (
        <>
          <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
            <MetricCard
              label="Deployment frequency"
              tip={`How often changes ship. Counted as ${deploymentWord} (analytics.dora.deployment_source).`}
              value={cur ? fmtRate(cur.deployments_per_day) : "—"}
              band={cur?.deployment_band}
              footer={
                <span className="flex flex-col">
                  <span>
                    {cur?.deployments ?? 0} in {days}d
                  </span>
                  <Trend
                    current={cur?.deployments}
                    previous={prev?.deployments}
                    lowerIsBetter={false}
                    days={days}
                  />
                </span>
              }
              loading={loading}
            />
            <MetricCard
              label="Lead time for changes"
              tip="Median time from a PR's first commit (or its opening, when unknown) to merge."
              value={fmtDuration(cur?.lead_time_secs)}
              band={cur?.lead_time_band}
              footer={
                <Trend
                  current={cur?.lead_time_secs}
                  previous={prev?.lead_time_secs}
                  lowerIsBetter
                  days={days}
                />
              }
              loading={loading}
            />
            <MetricCard
              label="Change failure rate"
              tip="Share of merges that were reverts or hotfixes (titles starting with Revert/hotfix, or hotfix/incident labels)."
              value={
                cur?.change_failure_rate != null
                  ? `${Math.round(cur.change_failure_rate * 100)}%`
                  : "—"
              }
              band={cur?.change_failure_band}
              footer={
                <span className="flex flex-col">
                  <span>
                    {cur?.failures ?? 0} of {cur?.merged_prs ?? 0} merges
                  </span>
                  <Trend
                    current={cur?.change_failure_rate}
                    previous={prev?.change_failure_rate}
                    lowerIsBetter
                    days={days}
                  />
                </span>
              }
              loading={loading}
            />
            <MetricCard
              label="Time to restore"
              tip="MTTR proxy: for a revert, the reverted PR's merge → the revert's merge; for a hotfix, its open → merge. Median."
              value={fmtDuration(cur?.mttr_secs)}
              band={cur?.mttr_band}
              footer={
                <Trend
                  current={cur?.mttr_secs}
                  previous={prev?.mttr_secs}
                  lowerIsBetter
                  days={days}
                />
              }
              loading={loading}
            />
          </div>

          <div className="grid gap-4 lg:grid-cols-3">
            <Card className="lg:col-span-2">
              <CardHeader className="pb-2">
                <CardTitle className="text-base">
                  Deployments over time
                </CardTitle>
                <CardDescription>
                  {data?.bucket_secs && data.bucket_secs >= 7 * 86400
                    ? "Weekly"
                    : "Daily"}{" "}
                  {deploymentWord}, with failure fixes
                </CardDescription>
              </CardHeader>
              <CardContent>
                {loading ? (
                  <Skeleton className="h-[180px] w-full" />
                ) : series.some((s) => s.deployments || s.failures) ? (
                  <ChartContainer
                    config={chartConfig}
                    className="h-[180px] w-full"
                  >
                    <BarChart
                      data={series}
                      margin={{ left: 0, right: 4, top: 4 }}
                      barGap={2}
                    >
                      <CartesianGrid vertical={false} />
                      <XAxis
                        dataKey="label"
                        tickLine={false}
                        axisLine={false}
                        tickMargin={8}
                        minTickGap={16}
                      />
                      <YAxis
                        allowDecimals={false}
                        tickLine={false}
                        axisLine={false}
                        width={28}
                      />
                      <ChartTooltip content={<ChartTooltipContent />} />
                      <Bar
                        dataKey="deployments"
                        fill="var(--color-deployments)"
                        radius={[4, 4, 0, 0]}
                      />
                      <Bar
                        dataKey="failures"
                        fill="var(--color-failures)"
                        radius={[4, 4, 0, 0]}
                      />
                      <ChartLegend content={<ChartLegendContent />} />
                    </BarChart>
                  </ChartContainer>
                ) : (
                  <div className="flex h-[180px] items-center justify-center text-sm text-muted-foreground">
                    No merges recorded in this period.
                  </div>
                )}
              </CardContent>
            </Card>

            <Card>
              <CardHeader className="pb-2">
                <CardTitle className="text-base">Cycle time</CardTitle>
                <CardDescription>
                  Medians over {cur?.cycle.prs ?? 0} merged PRs, from PR opened
                </CardDescription>
              </CardHeader>
              <CardContent>
                {loading ? (
                  <Skeleton className="h-[160px] w-full" />
                ) : (
                  <dl className="divide-y text-sm">
                    {[
                      [
                        "Coding (first commit → open)",
                        cur?.cycle.coding_time_secs,
                        prev?.cycle.coding_time_secs,
                      ],
                      [
                        "Time to first review",
                        cur?.cycle.time_to_first_review_secs,
                        prev?.cycle.time_to_first_review_secs,
                      ],
                      [
                        "Time to approval",
                        cur?.cycle.time_to_approval_secs,
                        prev?.cycle.time_to_approval_secs,
                      ],
                      [
                        "Time to merge",
                        cur?.cycle.time_to_merge_secs,
                        prev?.cycle.time_to_merge_secs,
                      ],
                      [
                        "Total cycle time",
                        cur?.cycle.total_cycle_secs,
                        prev?.cycle.total_cycle_secs,
                      ],
                    ].map(([label, value, before]) => (
                      <div
                        key={label as string}
                        className="flex items-center justify-between gap-2 py-2"
                      >
                        <dt className="text-muted-foreground">
                          {label as string}
                        </dt>
                        <dd className="text-right">
                          <div className="font-medium tabular-nums">
                            {fmtDuration(value as number | null)}
                          </div>
                          <div className="text-xs">
                            <Trend
                              current={value as number | null}
                              previous={before as number | null}
                              lowerIsBetter
                              days={days}
                            />
                          </div>
                        </dd>
                      </div>
                    ))}
                  </dl>
                )}
              </CardContent>
            </Card>
          </div>

          {!loading && (data?.failures.length ?? 0) > 0 && (
            <Card>
              <CardHeader className="pb-2">
                <CardTitle className="text-base">Failure fixes</CardTitle>
                <CardDescription>
                  Reverts and hotfixes merged in this period
                </CardDescription>
              </CardHeader>
              <CardContent>
                <ul className="divide-y text-sm">
                  {data!.failures.slice(0, 10).map((f) => (
                    <li
                      key={`${f.owner}/${f.repo}#${f.number}`}
                      className="flex items-center justify-between gap-3 py-2"
                    >
                      <span className="min-w-0 truncate">
                        <span className="text-muted-foreground">
                          {f.owner}/{f.repo}#{f.number}
                        </span>{" "}
                        {f.url ? (
                          <a
                            href={f.url}
                            target="_blank"
                            rel="noreferrer"
                            className="hover:underline"
                          >
                            {f.title}
                          </a>
                        ) : (
                          f.title
                        )}
                      </span>
                      <span className="shrink-0 text-muted-foreground tabular-nums">
                        restored in {fmtDuration(f.restore_secs)}
                      </span>
                    </li>
                  ))}
                </ul>
              </CardContent>
            </Card>
          )}
        </>
      )}
    </section>
  )
}
