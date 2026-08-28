import { History, Save, ShieldOff, UserMinus } from "lucide-react"
import { useState } from "react"

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
import type { TriageAuditPage, TriageConfigResponse } from "@/lib/api"
import { useAsync } from "@/lib/hooks"

type Weights = { codeowners: number; authored: number; reviewed: number }

type Draft = {
  enabled: boolean
  kill_switch: boolean
  comment: boolean
  codeowners: boolean
  history: boolean
  max_suggestions: number
  min_score: number
  history_days: number
  load_penalty: number
  exclude_bots: boolean
  exclude: string[]
  weights: Weights
}

function draftFrom(config: Record<string, unknown>): Draft {
  const pick = <T,>(key: string, fallback: T): T =>
    (config[key] as T | undefined) ?? fallback
  const weights = pick<Weights>("weights", {
    codeowners: 3,
    authored: 1,
    reviewed: 1.5,
  })
  return {
    enabled: pick("enabled", false),
    kill_switch: pick("kill_switch", false),
    comment: pick("comment", true),
    codeowners: pick("codeowners", true),
    history: pick("history", true),
    max_suggestions: pick("max_suggestions", 3),
    min_score: pick("min_score", 0.75),
    history_days: pick("history_days", 180),
    load_penalty: pick("load_penalty", 0.25),
    exclude_bots: pick("exclude_bots", true),
    exclude: [...pick<string[]>("exclude", [])],
    weights: { ...weights },
  }
}

// The override blob is a *layer*, not a copy of the resolved policy. Writing
// every field the form shows would freeze whatever `mira.yaml` currently says
// into the database, and a later edit to that file would then stop taking
// effect. So a field is written only when it differs from what was resolved
// without it.
//
// `exclude` is the exception and is written whole: it is the opt-out list, an
// admin edits it as one thing, and a per-entry diff would make "I removed that
// person" indistinguishable from "I did not touch it" — in the direction that
// keeps somebody opted out, which is the safe way to be wrong here.
function payloadFrom(draft: Draft, resolved: Draft): Record<string, unknown> {
  const payload: Record<string, unknown> = {}
  const put = <K extends keyof Draft>(key: K) => {
    if (draft[key] !== resolved[key]) payload[key] = draft[key]
  }
  put("enabled")
  put("kill_switch")
  put("comment")
  put("codeowners")
  put("history")
  put("exclude_bots")
  for (const key of [
    "max_suggestions",
    "min_score",
    "history_days",
    "load_penalty",
  ] as const) {
    if (Number(draft[key]) !== Number(resolved[key]))
      payload[key] = Number(draft[key])
  }
  if (
    draft.exclude.length ||
    resolved.exclude.length ||
    payload.exclude !== undefined
  ) {
    payload.exclude = draft.exclude
  }
  const weightsChanged = (["codeowners", "authored", "reviewed"] as const).some(
    (key) => Number(draft.weights[key]) !== Number(resolved.weights[key])
  )
  if (weightsChanged) payload.weights = draft.weights
  return payload
}

function Toggle({
  label,
  hint,
  checked,
  onChange,
}: {
  label: string
  hint: string
  checked: boolean
  onChange: (value: boolean) => void
}) {
  return (
    <label className="flex cursor-pointer items-start gap-3 rounded-md border p-3">
      <Checkbox
        checked={checked}
        onCheckedChange={(value) => onChange(value === true)}
        className="mt-0.5"
      />
      <span>
        <span className="block text-sm font-medium">{label}</span>
        <span className="block text-xs text-muted-foreground">{hint}</span>
      </span>
    </label>
  )
}

function NumberField({
  label,
  hint,
  value,
  step,
  onChange,
}: {
  label: string
  hint: string
  value: number
  step?: string
  onChange: (value: number) => void
}) {
  return (
    <div className="space-y-1.5">
      <label className="text-sm font-medium">{label}</label>
      <Input
        type="number"
        step={step ?? "1"}
        value={value}
        onChange={(event) => onChange(Number(event.target.value))}
        className="h-9"
      />
      <p className="text-xs text-muted-foreground">{hint}</p>
    </div>
  )
}

function formatDate(seconds: number): string {
  if (!seconds) return "—"
  return new Date(seconds * 1000).toLocaleString()
}

function AuditTrail() {
  const { data, loading, error } = useAsync<TriageAuditPage>(
    () => api.getTriageAudit(25, 0),
    []
  )

  return (
    <Card>
      <CardHeader>
        <CardTitle className="flex items-center gap-2 text-base">
          <History className="h-4 w-4" />
          Policy changes
        </CardTitle>
        <CardDescription>
          Append-only, and separate from the policy itself: the settings row
          only ever holds the current value, so somebody taken off the opt-out
          list for an afternoon and put back leaves no trace in it.
        </CardDescription>
      </CardHeader>
      <CardContent className="px-0 pb-0">
        {loading ? (
          <div className="space-y-2 px-6 pb-4">
            {Array.from({ length: 3 }).map((_, index) => (
              <Skeleton key={index} className="h-8 w-full" />
            ))}
          </div>
        ) : error ? (
          <p className="px-6 pb-6 text-sm text-muted-foreground">{error}</p>
        ) : !data?.entries.length ? (
          <p className="px-6 pb-6 text-sm text-muted-foreground">
            No policy change has been recorded yet.
          </p>
        ) : (
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead className="w-48">When</TableHead>
                <TableHead className="w-40">Who</TableHead>
                <TableHead>From</TableHead>
                <TableHead>To</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {data.entries.map((entry) => (
                <TableRow key={entry.id}>
                  <TableCell className="text-xs whitespace-nowrap">
                    {formatDate(entry.created_at)}
                  </TableCell>
                  <TableCell className="font-medium">
                    {entry.actor || "an admin"}
                  </TableCell>
                  <TableCell className="max-w-xs truncate font-mono text-xs text-muted-foreground">
                    {JSON.stringify(entry.previous)}
                  </TableCell>
                  <TableCell className="max-w-xs truncate font-mono text-xs">
                    {JSON.stringify(entry.new)}
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

export function TriagePolicyPanel() {
  const { data, loading, error } = useAsync<TriageConfigResponse>(
    () => api.getTriageConfig(),
    []
  )

  if (loading) return <Skeleton className="h-96 w-full" />
  if (error || !data)
    return (
      <p className="text-sm text-muted-foreground">
        {error ?? "The triage policy could not be loaded."}
      </p>
    )
  // Keyed on the loaded policy so the form owns its draft from the first
  // render rather than seeding it from an effect.
  return <PolicyForm key={String(data.effective.version ?? "")} config={data} />
}

function PolicyForm({ config: data }: { config: TriageConfigResponse }) {
  const [draft, setDraft] = useState<Draft>(() => draftFrom(data.config))
  // What the server resolved *before* this save. Anything left equal to it is
  // not written, so an inherited `mira.yaml` value stays inherited.
  const [resolved] = useState<Draft>(() => draftFrom(data.config))
  const [saving, setSaving] = useState(false)
  const [newExclusion, setNewExclusion] = useState("")

  const set = <K extends keyof Draft>(key: K, value: Draft[K]) =>
    setDraft((current) => ({ ...current, [key]: value }))

  const setWeight = (key: keyof Weights, value: number) =>
    setDraft((current) => ({
      ...current,
      weights: { ...current.weights, [key]: value },
    }))

  const addExclusion = () => {
    const identity = newExclusion.trim().replace(/^@/, "").toLowerCase()
    if (!identity) return
    setDraft((current) =>
      current.exclude.includes(identity)
        ? current
        : { ...current, exclude: [...current.exclude, identity] }
    )
    setNewExclusion("")
  }

  const save = async () => {
    setSaving(true)
    try {
      // The endpoint replaces the whole `triage` section, so keys this form
      // does not render — the bot list, the history fetch caps, the budget,
      // per-organisation and per-repository entries — are carried over from
      // what was loaded, or saving would silently delete them.
      await api.setTriageConfig({
        ...data.overrides,
        ...payloadFrom(draft, resolved),
      })
      toast.success("Triage policy saved")
    } catch (err) {
      toast.error(err instanceof Error ? err.message : "The policy was refused")
    } finally {
      setSaving(false)
    }
  }

  return (
    <div className="space-y-6">
      {draft.kill_switch && (
        <Card className="border-orange-500/40">
          <CardHeader className="flex-row items-center gap-3 space-y-0">
            <ShieldOff className="h-5 w-5 text-orange-500" />
            <div>
              <CardTitle className="text-base">The kill switch is on</CardTitle>
              <CardDescription>
                No suggestion is produced anywhere in the install, and no path
                history is recorded, whatever a repository policy says.
              </CardDescription>
            </div>
          </CardHeader>
        </Card>
      )}

      <Card>
        <CardHeader>
          <CardTitle className="text-base">Running at all</CardTitle>
          <CardDescription>
            Triage ships off. Turning it on starts two things: a suggestion
            comment on reviewed pull requests, and a record of who authored and
            reviewed which files — kept only for repositories where this is on.
          </CardDescription>
        </CardHeader>
        <CardContent className="space-y-4">
          <div className="grid gap-3 sm:grid-cols-2">
            <Toggle
              label="Suggest reviewers"
              hint="Off by default. Per-organisation and per-repository entries in mira.yaml can still narrow this."
              checked={draft.enabled}
              onChange={(value) => set("enabled", value)}
            />
            <Toggle
              label="Kill switch"
              hint="Stops every suggestion everywhere in one edit, independent of every override."
              checked={draft.kill_switch}
              onChange={(value) => set("kill_switch", value)}
            />
            <Toggle
              label="Post the suggestion as a comment"
              hint="Updated in place, never stacked, and never on a draft. Nobody is @-mentioned: the reader picks a name and requests the review themselves."
              checked={draft.comment}
              onChange={(value) => set("comment", value)}
            />
            <Toggle
              label="Ignore machine accounts"
              hint="Skips identities ending in [bot] and any login on the bots list. Bots do not review code."
              checked={draft.exclude_bots}
              onChange={(value) => set("exclude_bots", value)}
            />
          </div>
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle className="text-base">Signals</CardTitle>
          <CardDescription>
            CODEOWNERS is the repository <em>stating</em> who reviews a file and
            is read at the pull request&apos;s base commit, so a branch cannot
            add itself an owner. History is Mira <em>inferring</em> it from
            commits and from reviews it watched. When the two disagree, the
            statement should win — which is what the weights below say.
          </CardDescription>
        </CardHeader>
        <CardContent className="space-y-4">
          <div className="grid gap-3 sm:grid-cols-2">
            <Toggle
              label="Use CODEOWNERS"
              hint="Read at the base commit, never at the head."
              checked={draft.codeowners}
              onChange={(value) => set("codeowners", value)}
            />
            <Toggle
              label="Use file history"
              hint="Who has changed and reviewed these files before, within the window below."
              checked={draft.history}
              onChange={(value) => set("history", value)}
            />
          </div>
          <div className="grid gap-4 sm:grid-cols-3">
            <NumberField
              label="CODEOWNERS weight"
              hint="Points per changed file somebody owns."
              step="0.5"
              value={draft.weights.codeowners}
              onChange={(value) => setWeight("codeowners", value)}
            />
            <NumberField
              label="Authored weight"
              hint="Points per changed file they have edited, before recency."
              step="0.5"
              value={draft.weights.authored}
              onChange={(value) => setWeight("authored", value)}
            />
            <NumberField
              label="Reviewed weight"
              hint="Points per changed file they have reviewed, before recency."
              step="0.5"
              value={draft.weights.reviewed}
              onChange={(value) => setWeight("reviewed", value)}
            />
          </div>
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle className="text-base">Shape of the suggestion</CardTitle>
        </CardHeader>
        <CardContent className="grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
          <NumberField
            label="Names suggested"
            hint="Anyone ranked below the cut is recorded as such rather than dropped."
            value={draft.max_suggestions}
            onChange={(value) => set("max_suggestions", value)}
          />
          <NumberField
            label="Score floor"
            hint="Just under one recent authorship by default: one file you changed last week counts, one from six months ago does not."
            step="0.25"
            value={draft.min_score}
            onChange={(value) => set("min_score", value)}
          />
          <NumberField
            label="History window (days)"
            hint="Older work still counts for less the older it is, down to a fifth at the edge."
            value={draft.history_days}
            onChange={(value) => set("history_days", value)}
          />
          <NumberField
            label="Load penalty"
            hint="Points subtracted per pull request already waiting on them. A dampener, not a cap."
            step="0.25"
            value={draft.load_penalty}
            onChange={(value) => set("load_penalty", value)}
          />
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle className="flex items-center gap-2 text-base">
            <UserMinus className="h-4 w-4" />
            Opt-out list
          </CardTitle>
          <CardDescription>
            Never suggested, whatever the signals say. This is the answer to
            &quot;please stop suggesting me&quot;, and it is matched
            case-insensitively with or without the <span className="font-mono">@</span>.
          </CardDescription>
        </CardHeader>
        <CardContent className="space-y-3">
          <div className="flex flex-wrap gap-2">
            {draft.exclude.length === 0 && (
              <p className="text-sm text-muted-foreground">
                Nobody has opted out.
              </p>
            )}
            {draft.exclude.map((identity) => (
              <Button
                key={identity}
                variant="outline"
                size="sm"
                onClick={() =>
                  set(
                    "exclude",
                    draft.exclude.filter((item) => item !== identity)
                  )
                }
              >
                {identity}
                <span className="ml-2 text-muted-foreground">remove</span>
              </Button>
            ))}
          </div>
          <div className="flex gap-2">
            <Input
              value={newExclusion}
              onChange={(event) => setNewExclusion(event.target.value)}
              onKeyDown={(event) => {
                if (event.key === "Enter") {
                  event.preventDefault()
                  addExclusion()
                }
              }}
              placeholder="login or org/team"
              className="h-9 w-64"
            />
            <Button variant="outline" size="sm" onClick={addExclusion}>
              Add
            </Button>
          </div>
        </CardContent>
      </Card>

      <div className="flex justify-end">
        <Button onClick={save} disabled={saving}>
          <Save className="mr-1 h-4 w-4" />
          {saving ? "Saving…" : "Save policy"}
        </Button>
      </div>

      <AuditTrail />
    </div>
  )
}
