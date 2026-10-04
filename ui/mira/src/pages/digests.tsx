import { ChevronRight, ExternalLink, Newspaper, RefreshCw } from "lucide-react"
import { Fragment, useState } from "react"

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
import { api, type DigestDetail, type DigestListItem } from "@/lib/api"
import { useAuth } from "@/lib/auth"
import { useAsync, useDocumentTitle } from "@/lib/hooks"

const PERIODS = [
  { value: "1", label: "Last day" },
  { value: "7", label: "Last 7 days" },
  { value: "14", label: "Last 14 days" },
  { value: "30", label: "Last 30 days" },
]

function day(epoch: number): string {
  return new Date(epoch * 1000).toISOString().slice(0, 10)
}

function scope(d: DigestListItem): string {
  return d.repo ? `${d.owner}/${d.repo}` : `${d.owner} (all repositories)`
}

// Only links the platform handed back; anything else renders as text.
function safeUrl(url: string): string | null {
  return /^https?:\/\//.test(url) ? url : null
}

function DigestBody({ id }: { id: number }) {
  const { data, loading, error } = useAsync<DigestDetail>(
    () => api.getDigest(id),
    [id]
  )
  if (loading) {
    return (
      <div className="space-y-2 py-2">
        <Skeleton className="h-4 w-2/3" />
        <Skeleton className="h-4 w-1/2" />
      </div>
    )
  }
  if (error || !data) {
    return (
      <p className="text-sm text-destructive">Could not load this digest.</p>
    )
  }
  const d = data.data
  return (
    <div className="space-y-4 py-2">
      {d.overview && <p className="text-sm">{d.overview}</p>}
      {d.areas.length === 0 && (
        <p className="text-sm text-muted-foreground">
          Nothing landed in this period.
        </p>
      )}
      {d.areas.map((area) => (
        <div key={area.name} className="space-y-1.5">
          <div className="flex items-center gap-2">
            <h3 className="font-mono text-sm font-semibold">{area.name}</h3>
            <Badge variant="outline" className="text-[10px]">
              {area.changes.length}
            </Badge>
          </div>
          {area.summary && (
            <p className="text-sm text-muted-foreground">{area.summary}</p>
          )}
          {area.highlights.length > 0 && (
            <ul className="list-disc pl-5 text-sm">
              {area.highlights.map((h, i) => (
                <li key={i}>{h}</li>
              ))}
            </ul>
          )}
          <ul className="space-y-0.5 pl-1 text-xs">
            {area.changes.slice(0, 25).map((c) => {
              const href = safeUrl(c.url)
              const ref = `${c.repo}${c.ref}`
              return (
                <li key={`${c.repo}-${c.ref}`} className="flex gap-2">
                  {href ? (
                    <a
                      href={href}
                      target="_blank"
                      rel="noreferrer"
                      className="inline-flex shrink-0 items-center gap-1 font-mono hover:underline"
                    >
                      {ref}
                      <ExternalLink className="h-3 w-3" />
                    </a>
                  ) : (
                    <span className="shrink-0 font-mono">{ref}</span>
                  )}
                  <span className="truncate">{c.title || "(untitled)"}</span>
                  {c.kind === "commit" && (
                    <span className="shrink-0 text-muted-foreground">
                      direct commit
                    </span>
                  )}
                  {c.author && (
                    <span className="shrink-0 text-muted-foreground">
                      {c.author}
                    </span>
                  )}
                </li>
              )
            })}
            {area.changes.length > 25 && (
              <li className="text-muted-foreground">
                … and {area.changes.length - 25} more
              </li>
            )}
          </ul>
        </div>
      ))}
      {d.notes.length > 0 && (
        <ul className="space-y-0.5 text-xs text-muted-foreground">
          {d.notes.map((n, i) => (
            <li key={i}>{n}</li>
          ))}
        </ul>
      )}
    </div>
  )
}

function GenerateForm({ onDone }: { onDone: () => void }) {
  const { data: repos, error: reposError } = useAsync(
    () => api.listRepos(),
    []
  )
  const [repoKey, setRepoKey] = useState("")
  const [days, setDays] = useState("7")
  const [busy, setBusy] = useState(false)

  const generate = async () => {
    const target = (repos ?? []).find(
      (r) => `${r.platform}:${r.owner}/${r.repo}` === repoKey
    )
    if (!target) return
    setBusy(true)
    try {
      await api.generateDigest({
        platform: target.platform,
        owner: target.owner,
        repo: target.repo,
        days: Number(days),
        deliver: false,
      })
      toast.success("Digest generated")
      onDone()
    } catch (err) {
      toast.error(
        err instanceof Error ? err.message : "Could not generate the digest"
      )
    } finally {
      setBusy(false)
    }
  }

  if (reposError) {
    return (
      <p className="text-sm text-destructive">
        Couldn't load repositories: {reposError}
      </p>
    )
  }

  return (
    <div className="flex flex-col gap-2 sm:flex-row sm:items-center">
      <Select value={repoKey} onValueChange={setRepoKey}>
        <SelectTrigger className="sm:w-64">
          <SelectValue placeholder="Repository" />
        </SelectTrigger>
        <SelectContent>
          {(repos ?? []).map((r) => {
            const key = `${r.platform}:${r.owner}/${r.repo}`
            return (
              <SelectItem key={key} value={key}>
                {r.owner}/{r.repo}
                {r.platform !== "github" ? ` (${r.platform})` : ""}
              </SelectItem>
            )
          })}
        </SelectContent>
      </Select>
      <Select value={days} onValueChange={setDays}>
        <SelectTrigger className="sm:w-40">
          <SelectValue />
        </SelectTrigger>
        <SelectContent>
          {PERIODS.map((p) => (
            <SelectItem key={p.value} value={p.value}>
              {p.label}
            </SelectItem>
          ))}
        </SelectContent>
      </Select>
      <Button size="sm" onClick={generate} disabled={!repoKey || busy}>
        <RefreshCw className={busy ? "animate-spin" : ""} />
        {busy ? "Generating…" : "Generate now"}
      </Button>
    </div>
  )
}

export function DigestsPage() {
  useDocumentTitle("Digests")
  const { user } = useAuth()
  const [refreshKey, setRefreshKey] = useState(0)
  const [open, setOpen] = useState<number | null>(null)
  const { data, loading, error } = useAsync(
    () => api.listDigests({ limit: 100 }),
    [refreshKey]
  )
  const digests = data?.digests ?? []

  return (
    <div className="space-y-6 p-6">
      <div>
        <h1 className="text-2xl font-semibold tracking-tight">Digests</h1>
        <p className="text-sm text-muted-foreground">
          What landed on each repository&apos;s default branch, summarised per
          area. Scheduled digests are configured under <code>digests</code> in
          the Mira configuration and sent to webhooks subscribed to{" "}
          <code>digest.ready</code>.
        </p>
      </div>

      {user?.is_admin && (
        <Card>
          <CardHeader>
            <CardTitle className="text-base">Generate a digest</CardTitle>
            <CardDescription>
              Builds and stores a digest now, without delivering it.
            </CardDescription>
          </CardHeader>
          <CardContent>
            <GenerateForm onDone={() => setRefreshKey((k) => k + 1)} />
          </CardContent>
        </Card>
      )}

      <Card>
        <CardHeader>
          <CardTitle className="text-base">
            {loading ? (
              <Skeleton className="h-5 w-32" />
            ) : (
              `${data?.total ?? 0} ${data?.total === 1 ? "digest" : "digests"}`
            )}
          </CardTitle>
        </CardHeader>
        <CardContent className="px-0 pb-0">
          {loading ? (
            <div className="space-y-3 px-6 py-4">
              {Array.from({ length: 4 }).map((_, i) => (
                <Skeleton key={i} className="h-4 w-full max-w-md" />
              ))}
            </div>
          ) : error ? (
            <p className="px-6 py-12 text-center text-sm text-destructive">
              Couldn't load digests: {error}
            </p>
          ) : digests.length === 0 ? (
            <div className="flex flex-col items-center gap-2 px-6 py-12 text-center">
              <Newspaper className="h-8 w-8 text-muted-foreground" />
              <p className="text-sm font-medium">No digests yet</p>
              <p className="text-sm text-muted-foreground">
                Enable <code>digests.enabled</code> for a scheduled digest, or
                generate one above.
              </p>
            </div>
          ) : (
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead className="w-[40px] pl-6"></TableHead>
                  <TableHead>Scope</TableHead>
                  <TableHead>Period</TableHead>
                  <TableHead className="hidden pr-6 md:table-cell">
                    Generated
                  </TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {digests.map((d) => {
                  const isOpen = open === d.id
                  return (
                    <Fragment key={d.id}>
                      <TableRow
                        className="cursor-pointer"
                        onClick={() => setOpen(isOpen ? null : d.id)}
                      >
                        <TableCell className="pl-6">
                          <ChevronRight
                            className={`h-4 w-4 text-muted-foreground transition-transform ${isOpen ? "rotate-90" : ""}`}
                          />
                        </TableCell>
                        <TableCell className="text-sm">{scope(d)}</TableCell>
                        <TableCell className="font-mono text-xs">
                          {day(d.period_start)} → {day(d.period_end)}
                        </TableCell>
                        <TableCell className="hidden pr-6 text-xs text-muted-foreground md:table-cell">
                          {new Date(d.created_at * 1000).toLocaleString()}
                        </TableCell>
                      </TableRow>
                      {isOpen && (
                        <TableRow className="border-t-0 bg-muted/30 hover:bg-muted/30">
                          <TableCell></TableCell>
                          <TableCell
                            colSpan={3}
                            className="pr-6 whitespace-normal"
                          >
                            <DigestBody id={d.id} />
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
    </div>
  )
}
