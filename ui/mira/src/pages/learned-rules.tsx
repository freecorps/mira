import {
  Brain,
  Check,
  ChevronDown,
  ChevronLeft,
  ChevronRight,
  ChevronsUpDown,
  ChevronUp,
  Clock,
  Download,
  Lock,
  Pencil,
  Plus,
  Power,
  RefreshCw,
  Search,
  Upload,
  X,
} from "lucide-react"
import { useEffect, useMemo, useRef, useState } from "react"
import { useNavigate, useSearchParams } from "react-router"

import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Card, CardContent } from "@/components/ui/card"
import { ConfirmButton } from "@/components/ui/confirm-button"
import { GitHubIcon } from "@/components/ui/github-icon"
import { Input } from "@/components/ui/input"
import { UserAvatar } from "@/components/ui/user-avatar"
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select"
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table"
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs"
import { toast } from "@/components/ui/sonner"
import {
  api,
  type LearningCandidateModel,
  type OrgLearnedRuleModel,
} from "@/lib/api"
import { useAuth } from "@/lib/auth"
import { useAsync, useDocumentTitle } from "@/lib/hooks"
import { cn } from "@/lib/utils"

const ALL_REPOS = "__all__"
const PAGE_SIZE_OPTIONS = [10, 20, 50, 100]
// Blue highlight on a filter control when it's narrowing results (matches Activity).
const ACTIVE_FILTER = "border-blue-500 ring-1 ring-blue-500/30"

const SIGNAL_LABEL: Record<string, string> = {
  reject_pattern: "Rejected pattern",
  accept_pattern: "Accepted pattern",
  human_pattern: "Human reviewer style",
  manual: "Added by admin",
}

type SortKey = "repo" | "learning" | "status" | "updated"
type SortDir = "asc" | "desc"
type LearningRow = Omit<OrgLearnedRuleModel, "status"> & {
  status: OrgLearnedRuleModel["status"] | LearningCandidateModel["status"]
  entity_type: "rule" | "candidate"
  negative_examples?: LearningCandidateModel["negative_examples"]
  confidence?: number
  synthesizer_version?: string
}

function ruleKey(r: LearningRow) {
  return `${r.entity_type}:${r.owner}/${r.repo}#${r.id}`
}

function editHref(r: LearningRow) {
  return `/learnings/edit?owner=${r.owner}&repo=${r.repo}&${
    r.entity_type === "candidate" ? "candidate" : "id"
  }=${r.id}`
}

function candidateRow(candidate: LearningCandidateModel): LearningRow {
  return {
    id: candidate.id,
    owner: candidate.owner,
    repo: candidate.repo,
    rule_text: candidate.rule_text,
    source_signal: "feedback_v2",
    category: candidate.category,
    path_pattern: candidate.scope_type === "path" ? candidate.scope_value : "",
    sample_count: candidate.evidence_count,
    active: false,
    status: candidate.status,
    created_by: "",
    version: 1,
    scope_type: candidate.scope_type,
    scope_value: candidate.scope_value,
    origin_candidate_id: candidate.id,
    rationale: candidate.rationale,
    evidence_count: candidate.evidence_count,
    effective_from: 0,
    disabled_at: null,
    supersedes_rule_id: null,
    updated_at: candidate.updated_at,
    entity_type: "candidate",
    negative_examples: candidate.negative_examples,
    confidence: candidate.confidence,
    synthesizer_version: candidate.synthesizer_version,
  }
}

// Who added a learning: the admin's avatar for manual rules, a Mira mark for
// auto-synthesized ones (no single human author).
function AddedBy({
  rule,
  withName = false,
}: {
  rule: LearningRow
  withName?: boolean
}) {
  if (rule.created_by) {
    return (
      <span
        className="flex items-center gap-1.5"
        title={`Added by ${rule.created_by}`}
      >
        <UserAvatar seed={rule.created_by} className="h-5 w-5" />
        {withName && (
          <span className="truncate text-xs">{rule.created_by}</span>
        )}
      </span>
    )
  }
  return (
    <span className="flex items-center gap-1.5" title="Synthesized by Mira">
      <span className="flex h-5 w-5 shrink-0 items-center justify-center overflow-hidden rounded-full bg-primary/10 ring-1 ring-primary/20">
        <img src="/logo.png" alt="Mira" className="hidden h-3 w-3 dark:block" />
        <img src="/logo-light.png" alt="Mira" className="h-3 w-3 dark:hidden" />
      </span>
      {withName && (
        <span className="truncate text-xs text-muted-foreground">Mira</span>
      )}
    </span>
  )
}

function formatDate(epochSeconds: number) {
  if (!epochSeconds) return "—"
  return new Date(epochSeconds * 1000).toLocaleString()
}

function relativeTime(epochSeconds: number) {
  if (!epochSeconds) return "—"
  const s = Math.floor(Date.now() / 1000 - epochSeconds)
  if (s < 60) return "just now"
  const m = Math.floor(s / 60)
  if (m < 60) return `${m}m ago`
  const h = Math.floor(m / 60)
  if (h < 24) return `${h}h ago`
  const d = Math.floor(h / 24)
  if (d < 30) return `${d}d ago`
  return new Date(epochSeconds * 1000).toLocaleDateString()
}

export function LearnedRulesPage() {
  useDocumentTitle("Learnings")
  const { user } = useAuth()
  const isAdmin = !!user?.is_admin
  const username = user?.username ?? ""
  // Admins can edit any rule; a creator can edit their own while it's pending.
  const canEdit = (r: LearningRow) =>
    r.entity_type === "candidate"
      ? isAdmin && r.status === "pending"
      : isAdmin ||
        (!!username && r.created_by === username && r.status === "pending")
  const navigate = useNavigate()
  const [params, setParams] = useSearchParams()

  const [refreshKey, setRefreshKey] = useState(0)
  const refresh = () => setRefreshKey((k) => k + 1)
  // Tab lives in the URL (?tab=) so the browser back button moves between tabs.
  const requestedTab = params.get("tab")
  const tab: "approved" | "pending" | "rejected" =
    requestedTab === "pending" || requestedTab === "rejected"
      ? requestedTab
      : "approved"
  const setTab = (t: "approved" | "pending" | "rejected") => {
    setPanelOpen(false) // switching tabs closes any open detail panel
    const next = new URLSearchParams(params)
    next.set("tab", t)
    setParams(next)
  }
  const [query, setQuery] = useState("")
  const [repoFilter, setRepoFilter] = useState(ALL_REPOS)
  const [enabledFilter, setEnabledFilter] = useState<
    "all" | "enabled" | "disabled"
  >("all")
  const [selected, setSelected] = useState<LearningRow | null>(null)
  const importInput = useRef<HTMLInputElement>(null)

  const [panelOpen, setPanelOpen] = useState(false)

  const openDetail = (r: LearningRow) => {
    setSelected(r)
    setPanelOpen(true)
  }
  const closeDetail = () => setPanelOpen(false)

  useEffect(() => {
    if (!panelOpen) return
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && closeDetail()
    window.addEventListener("keydown", onKey)
    return () => window.removeEventListener("keydown", onKey)
  }, [panelOpen])

  const { data, loading, error } = useAsync(async () => {
    const [rules, candidates] = await Promise.all([
      api.listLearnedRules(""),
      isAdmin
        ? api.listLearningCandidates("")
        : Promise.resolve([] as LearningCandidateModel[]),
    ])
    return {
      rules: rules.map((rule) => ({ ...rule, entity_type: "rule" as const })),
      candidates: candidates.map(candidateRow),
    }
  }, [refreshKey, isAdmin])

  const allRows = useMemo(
    () => [...(data?.rules ?? []), ...(data?.candidates ?? [])],
    [data]
  )

  const approved = useMemo(
    () =>
      allRows.filter(
        (r) => r.status === "approved" && r.entity_type === "rule"
      ),
    [allRows]
  )
  const pending = useMemo(
    () =>
      allRows.filter(
        (r) => r.status === "pending" || r.status === "collecting"
      ),
    [allRows]
  )
  const rejected = useMemo(
    () =>
      allRows.filter(
        (r) => r.status === "rejected" || r.status === "superseded"
      ),
    [allRows]
  )

  const repoOptions = useMemo(() => {
    const set = new Set<string>()
    for (const r of allRows) set.add(`${r.owner}/${r.repo}`)
    return [...set].sort()
  }, [allRows])

  const applyFilter = (list: LearningRow[]) => {
    const q = query.trim().toLowerCase()
    return list.filter((r) => {
      const slug = `${r.owner}/${r.repo}`
      if (repoFilter !== ALL_REPOS && slug !== repoFilter) return false
      if (enabledFilter !== "all" && r.status === "approved") {
        if (enabledFilter === "enabled" && !r.active) return false
        if (enabledFilter === "disabled" && r.active) return false
      }
      if (!q) return true
      return `${r.rule_text} ${r.category} ${r.path_pattern} ${slug}`
        .toLowerCase()
        .includes(q)
    })
  }

  useEffect(() => {
    const candidateId = Number(params.get("candidate"))
    const owner = params.get("owner")
    const repo = params.get("repo")
    if (!candidateId || !owner || !repo || !data) return
    const candidate = data.candidates.find(
      (row) =>
        row.id === candidateId && row.owner === owner && row.repo === repo
    )
    if (candidate) {
      setSelected(candidate)
      setPanelOpen(true)
      const next = new URLSearchParams(params)
      next.delete("candidate")
      next.delete("owner")
      next.delete("repo")
      setParams(next, { replace: true })
    }
  }, [data, params, setParams])

  const act = (fn: () => Promise<unknown>, successMsg?: string) =>
    fn()
      .then(() => {
        refresh()
        if (successMsg) toast.success(successMsg)
        return true
      })
      .catch((e) => {
        toast.error("Action failed", {
          description: e instanceof Error ? e.message : String(e),
        })
        return false
      })

  // After approving/rejecting in the queue, advance to the next pending rule
  // so the admin can clear the queue without reopening the panel — close only
  // when none are left. The next rule is computed before the API call (the
  // list is refreshed by then) and applied once the action resolves.
  const computeNextPending = () => {
    if (!selected) return () => {}
    const list = applyFilter(pending)
    const idx = list.findIndex((r) => ruleKey(r) === ruleKey(selected))
    const remaining = list.filter((r) => ruleKey(r) !== ruleKey(selected))
    const next = remaining.length
      ? remaining[Math.min(idx, remaining.length - 1)]
      : null
    return () => {
      if (next) setSelected(next)
      else closeDetail()
    }
  }

  // Panel actions operate on the selected rule.
  const approveSel = () => {
    if (!selected) return
    const advance = computeNextPending()
    const action =
      selected.entity_type === "candidate"
        ? () =>
            api.approveLearningCandidate(
              selected.owner,
              selected.repo,
              selected.id
            )
        : () =>
            api.approveLearnedRule(selected.owner, selected.repo, selected.id)
    void act(action, "Approved").then((succeeded) => {
      if (succeeded) advance()
    })
  }
  const rejectSel = () => {
    if (!selected) return
    const advance = computeNextPending()
    const action =
      selected.entity_type === "candidate"
        ? () =>
            api.rejectLearningCandidate(
              selected.owner,
              selected.repo,
              selected.id
            )
        : () =>
            api.rejectLearnedRule(selected.owner, selected.repo, selected.id)
    void act(action, "Rejected").then((succeeded) => {
      if (succeeded) advance()
    })
  }
  const toggleSel = (active: boolean) => {
    if (!selected) return
    const toggledKey = ruleKey(selected)
    void act(
      () =>
        api.setLearnedRuleActive(
          selected.owner,
          selected.repo,
          selected.id,
          active
        ),
      active ? "Enabled" : "Disabled"
    ).then((succeeded) => {
      if (succeeded) {
        setSelected((current) =>
          current && ruleKey(current) === toggledKey
            ? { ...current, active }
            : current
        )
      }
    })
  }
  const selectedRepo = () => {
    const split = repoFilter.lastIndexOf("/")
    return split > 0
      ? ([repoFilter.slice(0, split), repoFilter.slice(split + 1)] as const)
      : null
  }
  const exportYaml = async () => {
    const repo = selectedRepo()
    if (!repo) return
    try {
      const yaml = await api.exportLearnedRules(repo[0], repo[1])
      const url = URL.createObjectURL(
        new Blob([yaml], { type: "application/yaml" })
      )
      const anchor = document.createElement("a")
      anchor.href = url
      anchor.download = `${repo[0].replaceAll("/", "-")}-${repo[1]}-mira-rules.yaml`
      anchor.click()
      URL.revokeObjectURL(url)
    } catch (e) {
      toast.error("Couldn't export learnings", {
        description: e instanceof Error ? e.message : String(e),
      })
    }
  }
  const importYaml = async (file: File | undefined) => {
    const repo = selectedRepo()
    if (!repo || !file) return
    try {
      const result = await api.importLearnedRules(
        repo[0],
        repo[1],
        await file.text()
      )
      toast.success(
        `Imported ${result.imported} learning${result.imported === 1 ? "" : "s"}`
      )
      refresh()
    } catch (e) {
      toast.error("Couldn't import learnings", {
        description: e instanceof Error ? e.message : String(e),
      })
    } finally {
      if (importInput.current) importInput.current.value = ""
    }
  }
  // Enabled/disabled only applies to approved rules, so only offer it there.
  const showEnabledFilter = tab === "approved"

  const filters = (
    <div className="flex flex-col gap-2 sm:flex-row sm:items-center">
      <div className="relative flex-1">
        <Search className="pointer-events-none absolute top-1/2 left-2.5 h-4 w-4 -translate-y-1/2 text-muted-foreground" />
        <Input
          aria-label="Filter learnings"
          placeholder="Filter learnings…"
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          className={cn("pl-8", query && ACTIVE_FILTER)}
        />
      </div>
      {showEnabledFilter && (
        <Select
          value={enabledFilter}
          onValueChange={(v) =>
            setEnabledFilter(v as "all" | "enabled" | "disabled")
          }
        >
          <SelectTrigger
            className={cn("sm:w-40", enabledFilter !== "all" && ACTIVE_FILTER)}
          >
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            <SelectItem value="all">All statuses</SelectItem>
            <SelectItem value="enabled">Enabled</SelectItem>
            <SelectItem value="disabled">Disabled</SelectItem>
          </SelectContent>
        </Select>
      )}
      <Select value={repoFilter} onValueChange={setRepoFilter}>
        <SelectTrigger
          className={cn("sm:w-64", repoFilter !== ALL_REPOS && ACTIVE_FILTER)}
        >
          <SelectValue placeholder="All repos" />
        </SelectTrigger>
        <SelectContent>
          <SelectItem value={ALL_REPOS}>All repos</SelectItem>
          {repoOptions.map((r) => (
            <SelectItem key={r} value={r}>
              <span className="flex items-center gap-1.5">
                <GitHubIcon className="h-3.5 w-3.5 shrink-0 text-muted-foreground" />
                {r}
              </span>
            </SelectItem>
          ))}
        </SelectContent>
      </Select>
      {isAdmin && (
        <>
          <input
            ref={importInput}
            type="file"
            accept=".yaml,.yml,application/yaml,text/yaml"
            className="hidden"
            onChange={(event) => void importYaml(event.target.files?.[0])}
          />
          <Button
            variant="outline"
            size="sm"
            disabled={repoFilter === ALL_REPOS}
            onClick={() => importInput.current?.click()}
          >
            <Upload className="h-4 w-4" /> Import
          </Button>
          <Button
            variant="outline"
            size="sm"
            disabled={repoFilter === ALL_REPOS}
            onClick={() => void exportYaml()}
          >
            <Download className="h-4 w-4" /> Export
          </Button>
        </>
      )}
      <Button
        variant="outline"
        size="sm"
        onClick={refresh}
        disabled={loading}
        title="Refresh"
        aria-label="Refresh learnings"
      >
        <RefreshCw className={cn("h-4 w-4", loading && "animate-spin")} />
        Refresh
      </Button>
    </div>
  )

  const firstLoad = loading && !data
  const selectedKey = selected ? ruleKey(selected) : null

  return (
    // Full-height: only the table body scrolls, not the whole page.
    <div className="flex h-[calc(100svh-3rem)] flex-col gap-4 overflow-hidden p-6">
      <div className="flex shrink-0 items-start justify-between gap-4">
        <div>
          <h1 className="text-2xl font-semibold tracking-tight">Learnings</h1>
          <p className="text-sm text-muted-foreground">
            What Mira has learned from your team's PR feedback. Approved
            learnings are added to future reviews that match their scope.
          </p>
        </div>
        <Button size="sm" onClick={() => navigate("/learnings/new")}>
          <Plus className="mr-1 h-4 w-4" /> Add learning
        </Button>
      </div>

      {firstLoad ? (
        <div className="text-sm text-muted-foreground">Loading…</div>
      ) : error ? (
        <div className="text-sm text-destructive">
          Couldn't load learnings: {error}
        </div>
      ) : (
        <Tabs
          value={tab}
          onValueChange={(v) =>
            setTab(v as "approved" | "pending" | "rejected")
          }
          className="flex min-h-0 flex-1 flex-col"
        >
          <TabsList className="shrink-0">
            <TabsTrigger value="approved">
              Approved
              <Badge variant="secondary" className="ml-2 tabular-nums">
                {approved.length}
              </Badge>
            </TabsTrigger>
            <TabsTrigger value="pending">
              Pending
              <Badge
                variant={pending.length ? "default" : "secondary"}
                className="ml-2 tabular-nums"
              >
                {pending.length}
              </Badge>
            </TabsTrigger>
            <TabsTrigger value="rejected">
              Rejected
              <Badge variant="secondary" className="ml-2 tabular-nums">
                {rejected.length}
              </Badge>
            </TabsTrigger>
          </TabsList>

          <div className="mt-4 shrink-0">{filters}</div>

          <TabsContent
            value="approved"
            className="mt-3 flex min-h-0 flex-1 flex-col gap-2"
          >
            {isAdmin && pending.length > 0 && (
              <button
                onClick={() => setTab("pending")}
                className="inline-flex items-center gap-1.5 rounded-md border border-amber-500/40 bg-amber-500/10 px-2.5 py-1 text-xs text-amber-700 transition-colors hover:bg-amber-500/15 dark:text-amber-400"
              >
                <Clock className="h-3 w-3 shrink-0" />
                <span>
                  <span className="font-medium">{pending.length}</span> in the
                  learning queue
                </span>
                <ChevronRight className="h-3.5 w-3.5 shrink-0" />
              </button>
            )}
            <LearningsTable
              rows={applyFilter(approved)}
              onSelect={openDetail}
              selectedKey={panelOpen ? selectedKey : null}
              resetKey={`approved|${query}|${repoFilter}|${enabledFilter}`}
            />
          </TabsContent>

          <TabsContent
            value="pending"
            className="mt-3 flex min-h-0 flex-1 flex-col gap-2"
          >
            {!isAdmin && (
              <div className="flex items-center gap-1.5 rounded-md border bg-muted/40 px-2.5 py-1.5 text-xs text-muted-foreground">
                <Lock className="h-3 w-3 shrink-0" />
                Only admins can approve learnings.
              </div>
            )}
            <LearningsTable
              rows={applyFilter(pending)}
              onSelect={openDetail}
              selectedKey={panelOpen ? selectedKey : null}
              resetKey={`pending|${query}|${repoFilter}|${enabledFilter}`}
            />
          </TabsContent>

          <TabsContent
            value="rejected"
            className="mt-3 flex min-h-0 flex-1 flex-col gap-2"
          >
            <LearningsTable
              rows={applyFilter(rejected)}
              onSelect={openDetail}
              selectedKey={panelOpen ? selectedKey : null}
              resetKey={`rejected|${query}|${repoFilter}|${enabledFilter}`}
            />
          </TabsContent>
        </Tabs>
      )}

      {/* Detail side panel */}
      <div
        aria-hidden={!panelOpen}
        className={cn(
          "fixed top-12 right-0 bottom-0 z-30 flex w-full max-w-[560px] flex-col border-l bg-background shadow-2xl transition-transform duration-300 ease-in-out",
          panelOpen ? "translate-x-0" : "pointer-events-none translate-x-full"
        )}
      >
        {selected && (
          <>
            <div className="flex items-center justify-between gap-3 border-b p-6">
              <div className="flex min-w-0 items-center gap-1.5 font-mono text-xs text-muted-foreground">
                <GitHubIcon className="h-3.5 w-3.5 shrink-0" />
                <span className="truncate">
                  {selected.owner}/{selected.repo}
                </span>
              </div>
              <div className="flex shrink-0 items-center gap-2">
                <StatusBadge rule={selected} />
                <Button
                  variant="ghost"
                  size="icon-sm"
                  onClick={closeDetail}
                  aria-label="Close"
                >
                  <X />
                </Button>
              </div>
            </div>

            {(isAdmin || canEdit(selected)) && (
              <div className="flex flex-wrap gap-2 border-b bg-muted/30 p-4">
                {isAdmin &&
                (selected.status === "pending" ||
                  selected.status === "collecting") ? (
                  <>
                    {selected.status === "pending" && (
                      <Button onClick={approveSel}>
                        <Check className="mr-1 h-4 w-4" /> Approve
                      </Button>
                    )}
                    <Button variant="outline" onClick={rejectSel}>
                      <X className="mr-1 h-4 w-4" /> Reject
                    </Button>
                  </>
                ) : isAdmin &&
                  selected.entity_type === "rule" &&
                  selected.status === "approved" ? (
                  selected.active ? (
                    <ConfirmButton
                      variant="destructive"
                      destructive
                      className="ring-1 ring-destructive/30 ring-inset"
                      dialogTitle="Disable learning?"
                      dialogDescription="It will stop influencing reviews until you re-enable it."
                      confirmLabel="Disable"
                      onConfirm={() => toggleSel(false)}
                    >
                      <Power className="mr-1 h-4 w-4" /> Disable
                    </ConfirmButton>
                  ) : (
                    <Button onClick={() => toggleSel(true)}>
                      <Power className="mr-1 h-4 w-4" /> Enable
                    </Button>
                  )
                ) : null}
                {canEdit(selected) && (
                  <Button
                    variant="outline"
                    onClick={() => navigate(editHref(selected))}
                  >
                    <Pencil className="mr-1 h-4 w-4" /> Edit
                  </Button>
                )}
              </div>
            )}

            <div className="flex-1 space-y-6 overflow-y-auto p-6">
              <div>
                <h3 className="mb-2 text-xs font-medium text-muted-foreground uppercase">
                  Learning
                </h3>
                <p className="text-sm whitespace-pre-wrap">
                  {selected.rule_text}
                </p>
              </div>
              {selected.rationale && (
                <div>
                  <h3 className="mb-2 text-xs font-medium text-muted-foreground uppercase">
                    Rationale
                  </h3>
                  <p className="text-sm whitespace-pre-wrap text-muted-foreground">
                    {selected.rationale}
                  </p>
                </div>
              )}
              <dl className="grid grid-cols-2 gap-x-4 gap-y-3">
                <Meta
                  label="Repo"
                  value={`${selected.owner}/${selected.repo}`}
                />
                <Meta label="Category" value={selected.category || "—"} />
                <Meta
                  label="Scope"
                  value={`${selected.scope_type}: ${selected.scope_value || "Any"}`}
                />
                <Meta
                  label="Source"
                  value={
                    SIGNAL_LABEL[selected.source_signal] ??
                    selected.source_signal
                  }
                />
                <Meta
                  label="Evidence"
                  value={String(selected.evidence_count)}
                />
                <Meta label="Version" value={`v${selected.version}`} />
                {selected.confidence !== undefined && (
                  <Meta
                    label="Confidence"
                    value={`${Math.round(selected.confidence * 100)}%`}
                  />
                )}
                <Meta label="Updated" value={formatDate(selected.updated_at)} />
                <div>
                  <dt className="text-xs text-muted-foreground">Added by</dt>
                  <dd className="mt-1">
                    <AddedBy rule={selected} withName />
                  </dd>
                </div>
              </dl>
              {selected.negative_examples &&
                selected.negative_examples.length > 0 && (
                  <div>
                    <h3 className="mb-2 text-xs font-medium text-muted-foreground uppercase">
                      Evidence
                    </h3>
                    <div className="space-y-3">
                      {selected.negative_examples.map((example) => (
                        <div
                          key={
                            example.feedback_id ??
                            `${example.finding_id}:${example.head_sha}:${example.path}:${example.line}`
                          }
                          className="rounded-md border p-3"
                        >
                          <div className="font-mono text-xs text-muted-foreground">
                            {example.path ?? "Unknown path"}
                            {example.line ? `:${example.line}` : ""}
                          </div>
                          {example.human_feedback && (
                            <p className="mt-2 text-sm">
                              “{example.human_feedback}”
                            </p>
                          )}
                          {example.finding && (
                            <p className="mt-2 text-xs text-muted-foreground">
                              Original: {example.finding}
                            </p>
                          )}
                        </div>
                      ))}
                    </div>
                  </div>
                )}
            </div>
          </>
        )}
      </div>
    </div>
  )
}

function Meta({ label, value }: { label: string; value: string }) {
  return (
    <div>
      <dt className="text-xs text-muted-foreground">{label}</dt>
      <dd className="text-sm font-medium">{value}</dd>
    </div>
  )
}

function sortValue(r: LearningRow, key: SortKey): string | number {
  switch (key) {
    case "repo":
      return `${r.owner}/${r.repo}`.toLowerCase()
    case "learning":
      return r.rule_text.toLowerCase()
    case "status":
      return r.status === "approved"
        ? r.active
          ? "enabled"
          : "disabled"
        : r.status
    case "updated":
      return r.updated_at
  }
}

function LearningsTable({
  rows,
  onSelect,
  selectedKey,
  resetKey,
}: {
  rows: LearningRow[]
  onSelect: (r: LearningRow) => void
  selectedKey: string | null
  resetKey: string
}) {
  const [sort, setSort] = useState<{ key: SortKey; dir: SortDir }>({
    key: "updated",
    dir: "desc",
  })
  const [page, setPage] = useState(0)
  const [pageSize, setPageSize] = useState(20)

  useEffect(() => {
    setPage(0)
  }, [resetKey, sort.key, sort.dir, pageSize])

  const toggleSort = (key: SortKey) =>
    setSort((s) =>
      s.key === key
        ? { key, dir: s.dir === "asc" ? "desc" : "asc" }
        : { key, dir: "asc" }
    )

  const sorted = useMemo(() => {
    const dir = sort.dir === "asc" ? 1 : -1
    return [...rows].sort((a, b) => {
      const av = sortValue(a, sort.key)
      const bv = sortValue(b, sort.key)
      if (av < bv) return -1 * dir
      if (av > bv) return 1 * dir
      return a.id - b.id // stable tiebreaker
    })
  }, [rows, sort])

  const totalPages = Math.max(1, Math.ceil(sorted.length / pageSize))
  const safePage = Math.min(page, totalPages - 1)
  const paged = sorted.slice(
    safePage * pageSize,
    safePage * pageSize + pageSize
  )
  const rangeStart = sorted.length === 0 ? 0 : safePage * pageSize + 1
  const rangeEnd = Math.min(sorted.length, safePage * pageSize + pageSize)

  if (rows.length === 0) {
    return (
      <Card className="flex min-h-0 flex-1 flex-col">
        <CardContent className="flex flex-1 flex-col items-center justify-center gap-2 py-12 text-center">
          <Brain className="h-8 w-8 text-muted-foreground" />
          <p className="text-sm text-muted-foreground">No learnings here.</p>
        </CardContent>
      </Card>
    )
  }

  return (
    <Card className="flex min-h-0 flex-1 flex-col overflow-hidden py-0">
      <div className="themed-scrollbar min-h-0 flex-1 overflow-auto">
        <Table
          className="table-fixed"
          containerClassName="overflow-x-visible overflow-y-visible"
        >
          <TableHeader className="sticky top-0 z-10 bg-background shadow-[0_1px_0_0_var(--border)]">
            <TableRow>
              <SortHead
                label="Repo"
                sortKey="repo"
                sort={sort}
                onSort={toggleSort}
                className="w-56"
              />
              <SortHead
                label="Learning"
                sortKey="learning"
                sort={sort}
                onSort={toggleSort}
              />
              <SortHead
                label="Status"
                sortKey="status"
                sort={sort}
                onSort={toggleSort}
                className="w-28"
              />
              <TableHead className="w-40">Created by</TableHead>
              <SortHead
                label="Updated"
                sortKey="updated"
                sort={sort}
                onSort={toggleSort}
                className="w-28"
              />
            </TableRow>
          </TableHeader>
          <TableBody>
            {paged.map((r) => {
              const disabled = r.status === "approved" && !r.active
              return (
                <TableRow
                  key={ruleKey(r)}
                  data-active={selectedKey === ruleKey(r)}
                  className="cursor-pointer data-[active=true]:bg-muted/60"
                  onClick={() => onSelect(r)}
                >
                  <TableCell className="align-top font-mono text-xs whitespace-nowrap text-muted-foreground">
                    <span className="flex items-center gap-1.5">
                      <GitHubIcon className="h-3.5 w-3.5 shrink-0" />
                      {r.owner}/{r.repo}
                    </span>
                  </TableCell>
                  <TableCell className="align-top">
                    {/* table-fixed gives this column the leftover width; the text
                      truncates to it (with an ellipsis) and adapts on resize. */}
                    <div
                      className={cn(
                        "line-clamp-2 text-sm break-words",
                        disabled && "opacity-50"
                      )}
                    >
                      {r.rule_text}
                    </div>
                    {(r.category || r.path_pattern) && (
                      <div className="mt-0.5 truncate text-xs text-muted-foreground">
                        {r.category}
                        {r.path_pattern ? ` · ${r.path_pattern}` : ""}
                      </div>
                    )}
                  </TableCell>
                  <TableCell className="align-top">
                    <StatusBadge rule={r} />
                  </TableCell>
                  <TableCell className="align-top">
                    <AddedBy rule={r} withName />
                  </TableCell>
                  <TableCell
                    className="align-top text-xs whitespace-nowrap text-muted-foreground"
                    title={formatDate(r.updated_at)}
                  >
                    {relativeTime(r.updated_at)}
                  </TableCell>
                </TableRow>
              )
            })}
          </TableBody>
        </Table>
      </div>

      <div className="flex shrink-0 items-center justify-between gap-2 border-t px-4 py-2 text-xs text-muted-foreground">
        <div className="flex items-center gap-3">
          <span>
            {rangeStart}–{rangeEnd} of {sorted.length}
          </span>
          <div className="flex items-center gap-1.5">
            <span>Rows:</span>
            <Select
              value={String(pageSize)}
              onValueChange={(v) => setPageSize(Number(v))}
            >
              <SelectTrigger className="h-7 w-[4.25rem] text-xs">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                {PAGE_SIZE_OPTIONS.map((n) => (
                  <SelectItem key={n} value={String(n)}>
                    {n}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </div>
        </div>
        <div className="flex items-center gap-2">
          <span className="tabular-nums">
            Page {safePage + 1} / {totalPages}
          </span>
          <Button
            variant="outline"
            size="icon-sm"
            onClick={() => setPage((p) => Math.max(0, p - 1))}
            disabled={safePage === 0}
            aria-label="Previous page"
          >
            <ChevronLeft />
          </Button>
          <Button
            variant="outline"
            size="icon-sm"
            onClick={() => setPage((p) => Math.min(totalPages - 1, p + 1))}
            disabled={safePage >= totalPages - 1}
            aria-label="Next page"
          >
            <ChevronRight />
          </Button>
        </div>
      </div>
    </Card>
  )
}

function StatusBadge({ rule }: { rule: LearningRow }) {
  if (rule.status === "collecting") {
    return (
      <Badge
        variant="outline"
        className="border-blue-500/40 bg-blue-500/10 text-blue-700 dark:text-blue-400"
      >
        Collecting
      </Badge>
    )
  }
  if (rule.status === "pending") {
    return (
      <Badge
        variant="outline"
        className="border-amber-500/40 bg-amber-500/10 text-amber-700 dark:text-amber-400"
      >
        Pending
      </Badge>
    )
  }
  if (rule.status === "rejected") {
    return (
      <Badge variant="outline" className="text-muted-foreground">
        Rejected
      </Badge>
    )
  }
  if (rule.status === "superseded") {
    return (
      <Badge variant="outline" className="text-muted-foreground">
        Superseded
      </Badge>
    )
  }
  return rule.active ? (
    <Badge
      variant="outline"
      className="border-emerald-500/40 bg-emerald-500/10 text-emerald-700 dark:text-emerald-400"
    >
      Enabled
    </Badge>
  ) : (
    <Badge variant="outline" className="text-muted-foreground">
      Disabled
    </Badge>
  )
}

function SortHead({
  label,
  sortKey,
  sort,
  onSort,
  className,
}: {
  label: string
  sortKey: SortKey
  sort: { key: SortKey; dir: SortDir }
  onSort: (key: SortKey) => void
  className?: string
}) {
  const active = sort.key === sortKey
  const Icon = active
    ? sort.dir === "asc"
      ? ChevronUp
      : ChevronDown
    : ChevronsUpDown
  return (
    <TableHead className={className}>
      <button
        type="button"
        onClick={() => onSort(sortKey)}
        aria-label={`Sort by ${label}`}
        className={cn(
          "inline-flex items-center gap-1 transition-colors hover:text-foreground",
          active ? "text-foreground" : "text-muted-foreground"
        )}
      >
        {label}
        <Icon
          className={cn(
            "h-3.5 w-3.5",
            active ? "text-foreground" : "text-muted-foreground/50"
          )}
        />
      </button>
    </TableHead>
  )
}
