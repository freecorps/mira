import { Flame } from "lucide-react"
import { useMemo, useState } from "react"

import {
  HotspotLegend,
  HotspotTreemap,
} from "@/components/dashboard/hotspot-treemap"
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
import { useAsync } from "@/lib/hooks"

const WINDOWS = [30, 90, 180, 365]

function PeriodPicker({
  value,
  onChange,
}: {
  value: number
  onChange: (v: number) => void
}) {
  return (
    <div className="inline-flex gap-1" role="group" aria-label="Time window">
      {WINDOWS.map((d) => (
        <button
          key={d}
          type="button"
          onClick={() => onChange(d)}
          aria-pressed={value === d}
          className={`inline-flex h-8 items-center rounded-md border px-3 text-xs font-medium transition-colors focus-visible:ring-2 focus-visible:ring-ring focus-visible:outline-none ${
            value === d
              ? "border-primary bg-primary/10 text-primary"
              : "border-input bg-background text-muted-foreground hover:bg-accent hover:text-accent-foreground"
          }`}
        >
          {d}d
        </button>
      ))}
    </div>
  )
}

/** Change-frequency heatmap (treemap) + ranked hotspot table for one repo. */
export function HotspotsPanel({
  owner,
  repo,
}: {
  owner: string
  repo: string
}) {
  const [days, setDays] = useState(90)
  const [dir, setDir] = useState<string | null>(null)
  const { data, loading, error } = useAsync(
    () => api.getHotspots(owner, repo, days, 150),
    [owner, repo, days]
  )

  const files = useMemo(() => {
    const all = data?.files ?? []
    return dir
      ? all.filter((f) => f.path.startsWith(dir === "." ? "" : `${dir}/`))
      : all
  }, [data, dir])

  const noData = !loading && data?.enabled && (data?.files.length ?? 0) === 0

  return (
    <Card>
      <CardHeader className="flex flex-row flex-wrap items-start justify-between gap-4">
        <div className="space-y-1.5">
          <CardTitle className="flex items-center gap-2">
            <Flame className="h-4 w-4" /> Change hotspots
          </CardTitle>
          <CardDescription>
            Files that change often, are large or symbol-dense, and keep drawing
            findings. Score = churn × complexity × (1 + findings per change).
          </CardDescription>
        </div>
        <PeriodPicker value={days} onChange={setDays} />
      </CardHeader>
      <CardContent className="space-y-6">
        {error ? (
          <p className="text-sm text-destructive">{error}</p>
        ) : loading ? (
          <Skeleton className="h-[320px] w-full" />
        ) : data && !data.enabled ? (
          <p className="text-sm text-muted-foreground">
            Hotspots are turned off (<code>analytics.hotspots.enabled</code>).
          </p>
        ) : noData ? (
          <p className="text-sm text-muted-foreground">
            No change history in the last {days} days yet. Hotspots fill in as
            Mira reviews pull requests and sees them merge (the first merge also
            pulls recent commit history).
          </p>
        ) : (
          data && (
            <>
              {data.directories.length > 1 && (
                <div
                  className="flex flex-wrap gap-1.5"
                  aria-label="Filter by directory"
                >
                  <button
                    type="button"
                    onClick={() => setDir(null)}
                    className={`rounded-full border px-2.5 py-0.5 text-xs ${
                      dir === null
                        ? "border-primary bg-primary/10 text-primary"
                        : "text-muted-foreground"
                    }`}
                  >
                    All
                  </button>
                  {data.directories.slice(0, 12).map((d) => (
                    <button
                      key={d.path}
                      type="button"
                      onClick={() => setDir(dir === d.path ? null : d.path)}
                      className={`rounded-full border px-2.5 py-0.5 font-mono text-xs ${
                        dir === d.path
                          ? "border-primary bg-primary/10 text-primary"
                          : "text-muted-foreground"
                      }`}
                    >
                      {d.path} <span className="opacity-70">({d.files})</span>
                    </button>
                  ))}
                </div>
              )}
              <HotspotTreemap
                files={files.slice(0, 80)}
                maxScore={data.max_score}
              />
              <HotspotLegend />
              <div className="overflow-x-auto">
                <Table>
                  <TableHeader>
                    <TableRow>
                      <TableHead className="w-10">#</TableHead>
                      <TableHead>File</TableHead>
                      <TableHead className="text-right">Score</TableHead>
                      <TableHead className="text-right">Changes</TableHead>
                      <TableHead className="text-right">
                        Lines changed
                      </TableHead>
                      <TableHead className="text-right">Findings</TableHead>
                      <TableHead className="text-right">LOC</TableHead>
                      <TableHead className="text-right">Symbols</TableHead>
                    </TableRow>
                  </TableHeader>
                  <TableBody>
                    {files.slice(0, 25).map((f) => (
                      <TableRow key={f.path}>
                        <TableCell className="text-muted-foreground tabular-nums">
                          {data.files.indexOf(f) + 1}
                        </TableCell>
                        <TableCell className="max-w-[28rem] truncate font-mono text-xs">
                          {f.path}
                        </TableCell>
                        <TableCell className="text-right font-medium tabular-nums">
                          {f.score.toFixed(1)}
                        </TableCell>
                        <TableCell className="text-right tabular-nums">
                          {f.changes}
                        </TableCell>
                        <TableCell className="text-right tabular-nums">
                          {f.lines_changed.toLocaleString()}
                        </TableCell>
                        <TableCell className="text-right tabular-nums">
                          {f.findings}
                        </TableCell>
                        <TableCell className="text-right tabular-nums">
                          {f.loc ? f.loc.toLocaleString() : "—"}
                        </TableCell>
                        <TableCell className="text-right tabular-nums">
                          {f.symbols || "—"}
                        </TableCell>
                      </TableRow>
                    ))}
                  </TableBody>
                </Table>
              </div>
              <p className="text-xs text-muted-foreground">
                {data.total_files} files changed in the last {data.window_days}{" "}
                days. Sources:{" "}
                {[
                  data.sources.history && "commit history",
                  data.sources.reviews && "reviewed PRs",
                  data.sources.findings && "Mira findings",
                  data.sources.index && "code index",
                ]
                  .filter(Boolean)
                  .join(", ") || "none yet"}
                .
              </p>
            </>
          )
        )}
      </CardContent>
    </Card>
  )
}
