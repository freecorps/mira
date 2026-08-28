import {
  AlertTriangle,
  CircleSlash,
  ExternalLink,
  Info,
  RefreshCw,
  UserSearch,
  Users,
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
  TriageCandidate,
  TriageRunDetail,
  TriageRunModel,
  TriageRunPage,
  TriageSignalReport,
  TriageStatus,
  TriageSuggestionSummary,
} from "@/lib/api"
import { useAsync, useDocumentTitle } from "@/lib/hooks"
import { cn } from "@/lib/utils"

import { TriagePolicyPanel } from "./triage-policy"

const ALL = "__all__"
const PAGE_SIZE = 25

// The distinction the page exists to make visible. `no_candidates` is an
// answer about the repository; `unavailable` is Mira reporting about itself,
// and the two never share a colour.
const STATUS_LABEL: Record<TriageStatus, string> = {
  ok: "Suggested",
  no_candidates: "Nobody to suggest",
  unavailable: "Mira could not tell",
  not_run: "Did not run",
}

const STATUS_STYLE: Record<TriageStatus, string> = {
  ok: "bg-emerald-500/10 text-emerald-600 dark:text-emerald-400",
  no_candidates: "bg-muted text-muted-foreground",
  unavailable: "bg-orange-500/10 text-orange-600 dark:text-orange-400",
  not_run: "bg-muted text-muted-foreground",
}

const STATUSES: TriageStatus[] = [
  "ok",
  "no_candidates",
  "unavailable",
  "not_run",
]

const SIGNAL_LABEL: Record<string, string> = {
  codeowners: "CODEOWNERS",
  authored: "file history",
  reviewed: "review history",
}

const SIGNAL_REASON: Record<string, string> = {
  codeowners: "listed in CODEOWNERS",
  authored: "has changed these files",
  reviewed: "has reviewed these files",
}

const EXCLUSION_LABEL: Record<string, string> = {
  author: "opened this pull request",
  bot: "is a machine account",
  opted_out: "has opted out",
  no_evidence: "had no evidence behind the name",
  below_threshold: "scored below the floor",
  not_top_ranked: "ranked below the cut",
}

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

function StatusBadge({ status }: { status: TriageStatus }) {
  return (
    <Badge
      variant="secondary"
      className={cn("font-medium", STATUS_STYLE[status])}
    >
      {STATUS_LABEL[status] ?? status}
    </Badge>
  )
}

function SignalRow({ signal }: { signal: TriageSignalReport }) {
  const Icon = signal.answered ? Info : AlertTriangle
  return (
    <div className="flex items-start gap-2 text-xs">
      <Icon
        className={cn(
          "mt-0.5 h-3.5 w-3.5 shrink-0",
          signal.answered
            ? "text-muted-foreground"
            : "text-orange-600 dark:text-orange-400"
        )}
      />
      <span>
        <span className="font-medium">
          {SIGNAL_LABEL[signal.kind] ?? signal.kind}
        </span>{" "}
        <span className="font-mono text-muted-foreground">{signal.status}</span>
        {" — "}
        <span className="text-muted-foreground">{signal.detail}</span>
      </span>
    </div>
  )
}

function CandidateCard({
  candidate,
  rank,
}: {
  candidate: TriageCandidate
  rank: number
}) {
  return (
    <div className="rounded border p-3">
      <div className="flex flex-wrap items-center gap-2">
        <span className="text-sm font-semibold tabular-nums text-muted-foreground">
          {rank}.
        </span>
        {/* Rendered as text, never as a mention: this page is a record of a
            suggestion, not a way to notify somebody about it. */}
        <span className="font-mono text-sm">{candidate.identity}</span>
        {candidate.kind !== "user" && (
          <Badge variant="outline" className="text-[10px] font-normal">
            {candidate.kind}
          </Badge>
        )}
        <span className="ml-auto text-sm tabular-nums">
          {candidate.score.toFixed(2)}
        </span>
      </div>
      <div className="mt-2 space-y-1">
        {candidate.contributions.map((contribution) => (
          <div key={contribution.kind} className="text-xs">
            <span className="font-medium">
              {SIGNAL_REASON[contribution.kind] ?? contribution.kind}
            </span>
            <span className="text-muted-foreground">
              {" "}
              — {contribution.detail} ({contribution.raw}×{contribution.weight}={" "}
              {contribution.score.toFixed(2)})
            </span>
            <div className="mt-1 flex flex-wrap gap-2">
              {contribution.evidence.map((item, index) => (
                <span
                  key={index}
                  className="rounded bg-muted/50 px-1.5 py-0.5 font-mono text-[11px]"
                >
                  {item.path}
                  {item.line ? `:${item.line}` : ""}
                  {item.url && (
                    <a
                      href={item.url}
                      target="_blank"
                      rel="noreferrer"
                      className="ml-1 inline-flex align-middle text-muted-foreground hover:text-foreground"
                    >
                      <ExternalLink className="h-3 w-3" />
                    </a>
                  )}
                </span>
              ))}
            </div>
          </div>
        ))}
      </div>
      {candidate.load_penalty > 0 && (
        <p className="mt-2 text-[11px] text-muted-foreground">
          −{candidate.load_penalty.toFixed(2)} for {candidate.open_reviews}{" "}
          review(s) already waiting on them.
        </p>
      )}
    </div>
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
  const { data, loading, error } = useAsync<TriageRunDetail>(
    () => api.getTriageRun(owner, repo, id),
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
  const failed = run.signals.filter((signal) => !signal.answered)

  return (
    <div className="space-y-4 p-4">
      <div className="flex flex-wrap items-center gap-3 text-xs text-muted-foreground">
        <span>Policy {run.policy_version}</span>
        <span>{formatDuration(run.duration_seconds)}</span>
        <span>
          {run.classification.size} · {run.classification.changed_files} file(s)
          · {run.classification.kinds.join(", ") || "unclassified"}
        </span>
        {run.classification.areas.length > 0 && (
          <span className="font-mono">
            {run.classification.areas.join(", ")}
          </span>
        )}
        <span className="font-mono">
          ownership read at{" "}
          {String(run.inputs.ownership_ref || "—").slice(0, 12)}
        </span>
      </div>

      {run.candidates.length > 0 ? (
        <div className="space-y-2">
          <h4 className="text-sm font-semibold">Suggested</h4>
          {run.candidates.map((candidate, index) => (
            <CandidateCard
              key={candidate.identity}
              candidate={candidate}
              rank={index + 1}
            />
          ))}
        </div>
      ) : (
        <p className="text-sm text-muted-foreground">
          {run.status === "unavailable"
            ? "Nobody was suggested because Mira could not read what it ranks on. This says nothing about who is available."
            : "Every signal answered and nobody qualified."}
        </p>
      )}

      {failed.length > 0 && (
        <div className="space-y-1">
          <h4 className="text-sm font-semibold">
            What Mira could not read
          </h4>
          <p className="text-xs text-muted-foreground">
            These are not statements about the repository or the people in it.
          </p>
          {failed.map((signal) => (
            <SignalRow key={signal.kind} signal={signal} />
          ))}
        </div>
      )}

      <details>
        <summary className="cursor-pointer text-sm font-semibold">
          Signals and everyone not suggested
        </summary>
        <div className="mt-2 space-y-2">
          {run.signals.map((signal) => (
            <SignalRow key={signal.kind} signal={signal} />
          ))}
          {run.excluded.length === 0 ? (
            <p className="text-xs text-muted-foreground">Nobody was dropped.</p>
          ) : (
            run.excluded.map((exclusion) => (
              <p
                key={`${exclusion.identity}-${exclusion.reason}`}
                className="text-xs"
              >
                <span className="font-mono">{exclusion.identity}</span>
                <span className="text-muted-foreground">
                  {" — "}
                  {EXCLUSION_LABEL[exclusion.reason] ?? exclusion.reason}
                  {exclusion.detail ? ` (${exclusion.detail})` : ""}
                </span>
              </p>
            ))
          )}
          {run.notes.map((note, index) => (
            <p key={index} className="text-xs text-muted-foreground italic">
              {note}
            </p>
          ))}
        </div>
      </details>
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
  const [status, setStatus] = useState<string>(ALL)
  const [repo, setRepo] = useState("")
  const [identity, setIdentity] = useState("")
  const [degraded, setDegraded] = useState(false)
  const [page, setPage] = useState(0)
  const [refreshKey, setRefreshKey] = useState(0)
  const [expanded, setExpanded] = useState<number | null>(null)

  const [owner, repoName] = useRepoFilter(repo)

  const { data, loading, error } = useAsync<TriageRunPage>(
    () =>
      api.listTriageRuns({
        owner,
        repo: repoName,
        status: status === ALL ? undefined : (status as TriageStatus),
        identity: identity.trim() || undefined,
        degraded,
        limit: PAGE_SIZE,
        offset: page * PAGE_SIZE,
      }),
    [owner, repoName, status, identity, degraded, page, refreshKey]
  )

  const refresh = useCallback(() => setRefreshKey((key) => key + 1), [])
  const runs: TriageRunModel[] = data?.runs ?? []
  const total = data?.total ?? 0

  return (
    <div className="space-y-6">
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
            <Input
              value={identity}
              onChange={(event) => {
                setIdentity(event.target.value)
                setPage(0)
              }}
              placeholder="suggested person"
              className="h-9 w-52"
            />
            <Select
              value={status}
              onValueChange={(value) => {
                setStatus(value)
                setPage(0)
              }}
            >
              <SelectTrigger className="h-9 w-52">
                <SelectValue placeholder="Any status" />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value={ALL}>Any status</SelectItem>
                {STATUSES.map((item) => (
                  <SelectItem key={item} value={item}>
                    {STATUS_LABEL[item]}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
            <label className="flex cursor-pointer items-center gap-2 text-sm">
              <Checkbox
                checked={degraded}
                onCheckedChange={(value) => {
                  setDegraded(value === true)
                  setPage(0)
                }}
              />
              only degraded
            </label>
            <Button variant="outline" size="sm" onClick={refresh}>
              <RefreshCw className="mr-1 h-3.5 w-3.5" />
              Refresh
            </Button>
          </div>
          <CardDescription>
            <span className="font-mono">only degraded</span> selects the runs
            where a signal did not answer — the set that says something about
            Mira rather than about the repository.
          </CardDescription>
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
              <UserSearch className="h-8 w-8 text-muted-foreground" />
              <p className="text-sm font-medium">No triage has run yet</p>
              <p className="max-w-md text-sm text-muted-foreground">
                Triage ships off. Turn it on from the Policy tab; a fresh
                install starts with CODEOWNERS alone and gets better at the
                history half as it watches pull requests merge.
              </p>
            </div>
          ) : (
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>Pull request</TableHead>
                  <TableHead className="w-44">Status</TableHead>
                  <TableHead className="w-64">Suggested</TableHead>
                  <TableHead className="w-40">Change</TableHead>
                  <TableHead className="w-48">Ran</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {runs.map((run) => {
                  const inputs = run.inputs as Record<string, string | number>
                  const key = `${run.run_key}`
                  const owner = String(inputs.owner ?? "")
                  const repoName = String(inputs.repo ?? "")
                  return (
                    <Fragment key={key}>
                      <TableRow
                        className="cursor-pointer"
                        onClick={() =>
                          setExpanded(
                            expanded === run.run_id ? null : run.run_id
                          )
                        }
                      >
                        <TableCell>
                          <div className="flex items-center gap-2">
                            <span className="font-medium">
                              {owner}/{repoName}#{inputs.pr_number}
                            </span>
                            {inputs.pr_url && (
                              <a
                                href={String(inputs.pr_url)}
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
                            {String(inputs.pr_author || "unknown author")} ·{" "}
                            {String(inputs.head_sha ?? "").slice(0, 8)}
                          </span>
                        </TableCell>
                        <TableCell>
                          <div className="flex flex-wrap items-center gap-1">
                            <StatusBadge status={run.status} />
                            {run.degraded && run.status === "ok" && (
                              <Badge
                                variant="outline"
                                className="gap-1 text-[10px] font-normal text-orange-600 dark:text-orange-400"
                              >
                                <CircleSlash className="h-3 w-3" />
                                partial
                              </Badge>
                            )}
                          </div>
                        </TableCell>
                        <TableCell className="font-mono text-xs">
                          {run.candidates.length
                            ? run.candidates
                                .map((candidate) => candidate.identity)
                                .join(", ")
                            : "—"}
                        </TableCell>
                        <TableCell className="text-xs text-muted-foreground">
                          {run.classification.size} ·{" "}
                          {run.classification.changed_files} file(s)
                        </TableCell>
                        <TableCell className="text-xs whitespace-nowrap text-muted-foreground">
                          {formatDate(run.created_at)}
                        </TableCell>
                      </TableRow>
                      {expanded === run.run_id && (
                        <TableRow>
                          <TableCell colSpan={5} className="p-0">
                            <RunDetail
                              owner={owner}
                              repo={repoName}
                              id={run.run_id}
                            />
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

function SuggestionSpread() {
  const [repo, setRepo] = useState("")
  const [owner, repoName] = useRepoFilter(repo)

  const { data, loading, error } = useAsync<TriageSuggestionSummary>(
    () => api.getTriageSuggestions({ owner, repo: repoName }),
    [owner, repoName]
  )

  const rows = data?.identities ?? []

  return (
    <Card>
      <CardHeader className="gap-3">
        <CardTitle className="flex items-center gap-2 text-base">
          <Users className="h-4 w-4" />
          Who is being suggested
        </CardTitle>
        <CardDescription>
          The question to ask before turning suggestions on anywhere else: is
          this naming the same two people over and over, or spreading across the
          team? A concentrated list is not a bug in the ranking — it usually
          means the load penalty is too low, or that CODEOWNERS says the same
          thing about every file.
        </CardDescription>
        <Input
          value={repo}
          onChange={(event) => setRepo(event.target.value)}
          placeholder="owner/repo"
          className="h-9 w-56"
        />
      </CardHeader>
      <CardContent className="px-0 pb-0">
        {loading ? (
          <div className="space-y-2 px-6 pb-4">
            {Array.from({ length: 4 }).map((_, index) => (
              <Skeleton key={index} className="h-8 w-full" />
            ))}
          </div>
        ) : error ? (
          <p className="px-6 pb-6 text-sm text-muted-foreground">{error}</p>
        ) : rows.length === 0 ? (
          <p className="px-6 pb-6 text-sm text-muted-foreground">
            Nobody has been suggested yet.
          </p>
        ) : (
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>Identity</TableHead>
                <TableHead className="w-28">Kind</TableHead>
                <TableHead className="w-28 text-right">Times</TableHead>
                <TableHead className="w-32 text-right">Average rank</TableHead>
                <TableHead className="w-32 text-right">Average score</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {rows.map((row) => (
                <TableRow key={`${row.identity}-${row.kind}`}>
                  <TableCell className="font-mono text-sm">
                    {row.identity}
                  </TableCell>
                  <TableCell className="text-xs text-muted-foreground">
                    {row.kind}
                  </TableCell>
                  <TableCell className="text-right tabular-nums">
                    {row.count}
                  </TableCell>
                  <TableCell className="text-right tabular-nums">
                    {row.average_rank.toFixed(2)}
                  </TableCell>
                  <TableCell className="text-right tabular-nums">
                    {row.average_score.toFixed(2)}
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

export function TriagePage() {
  useDocumentTitle("Reviewer triage")
  const [params, setParams] = useSearchParams()
  const requested = params.get("tab")
  const tab =
    requested === "policy" || requested === "people" ? requested : "runs"

  return (
    <div className="space-y-6 p-6">
      <div>
        <h1 className="text-2xl font-semibold tracking-tight">
          Reviewer triage
        </h1>
        <p className="max-w-3xl text-sm text-muted-foreground">
          What a change is, and who is closest to it. Every name carries the
          evidence that produced it — a CODEOWNERS line, a commit, a review —
          and nobody is assigned, requested or notified: Mira suggests, a human
          decides. A run that could not read what it ranks on says so in
          Mira&apos;s own name and is never shown as &quot;nobody
          available&quot;.
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
          <TabsTrigger value="people">Who gets suggested</TabsTrigger>
          <TabsTrigger value="policy">Policy</TabsTrigger>
        </TabsList>
        <TabsContent value="runs" className="pt-4">
          <RunHistory />
        </TabsContent>
        <TabsContent value="people" className="pt-4">
          <SuggestionSpread />
        </TabsContent>
        <TabsContent value="policy" className="pt-4">
          <TriagePolicyPanel />
        </TabsContent>
      </Tabs>
    </div>
  )
}
