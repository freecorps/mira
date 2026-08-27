import { AlertTriangle, History, Save, ShieldOff } from "lucide-react"
import { useState } from "react"

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
import type {
  CheckCatalogResponse,
  CheckMode,
  ChecksAuditPage,
  ChecksConfigResponse,
} from "@/lib/api"
import { useAsync } from "@/lib/hooks"

const MODES: CheckMode[] = ["off", "warning", "error"]

const MODE_HINT: Record<CheckMode, string> = {
  off: "Does not run. Recorded as skipped, not as a pass.",
  warning: "Runs and reports. Never blocks a merge.",
  error: "Blocks on a violation — and on an inability to answer.",
}

type Draft = {
  enabled: boolean
  kill_switch: boolean
  default_mode: CheckMode
  modes: Record<string, CheckMode>
  max_concurrency: number
  check_timeout_seconds: number
  total_timeout_seconds: number
  publish_status: boolean
  comment: boolean
}

function draftFrom(config: Record<string, unknown>): Draft {
  const pick = <T,>(key: string, fallback: T): T =>
    (config[key] as T | undefined) ?? fallback
  return {
    enabled: pick("enabled", false),
    kill_switch: pick("kill_switch", false),
    default_mode: pick<CheckMode>("default_mode", "warning"),
    modes: { ...(pick("modes", {}) as Record<string, CheckMode>) },
    max_concurrency: pick("max_concurrency", 2),
    check_timeout_seconds: pick("check_timeout_seconds", 60),
    total_timeout_seconds: pick("total_timeout_seconds", 300),
    publish_status: pick("publish_status", true),
    comment: pick("comment", false),
  }
}

// The override blob is a *layer*, not a copy of the resolved policy. Writing
// every scalar the form shows would freeze whatever `mira.yaml` currently says
// into the database — and a later edit to that file would then stop taking
// effect on any field this panel happens to render. So a field is written only
// when it actually differs from the value that was resolved without it.
//
// `modes` is the exception and is written whole: it is a mapping an admin
// edits as one thing, and a per-key diff would make "I removed that entry"
// indistinguishable from "I did not touch it".
function payloadFrom(draft: Draft, resolved: Draft): Record<string, unknown> {
  const payload: Record<string, unknown> = {}
  const put = <K extends keyof Draft>(key: K) => {
    if (draft[key] !== resolved[key]) payload[key] = draft[key]
  }
  put("enabled")
  put("kill_switch")
  put("default_mode")
  put("publish_status")
  put("comment")
  for (const key of [
    "max_concurrency",
    "check_timeout_seconds",
    "total_timeout_seconds",
  ] as const) {
    if (Number(draft[key]) !== Number(resolved[key]))
      payload[key] = Number(draft[key])
  }
  // Only the checks an admin moved away from the default. Writing every id
  // would freeze today's default into the policy, so a later change to
  // `default_mode` would quietly apply to nothing.
  if (Object.keys(draft.modes).length || Object.keys(resolved.modes).length) {
    payload.modes = draft.modes
  }
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

function formatDate(seconds: number): string {
  if (!seconds) return "—"
  return new Date(seconds * 1000).toLocaleString()
}

function AuditTrail() {
  const { data, loading, error } = useAsync<ChecksAuditPage>(
    () => api.getChecksAudit(25, 0),
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
          Append-only, and deliberately separate from the policy itself: the
          settings row only ever holds the current value, so a policy that was
          loosened for an afternoon and tightened again leaves no trace in it —
          which is exactly the change somebody comes looking for.
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

export function ChecksPolicyPanel() {
  const { data, loading, error } = useAsync<ChecksConfigResponse>(
    () => api.getChecksConfig(),
    []
  )

  if (loading) return <Skeleton className="h-96 w-full" />
  if (error || !data)
    return (
      <p className="text-sm text-muted-foreground">
        {error ?? "The check policy could not be loaded."}
      </p>
    )
  // Keyed on the loaded policy so the form owns its draft from the first
  // render. Seeding it from an effect would mean one render showing values
  // nobody configured, and a cascading re-render to correct them.
  return <PolicyForm key={String(data.effective.version ?? "")} config={data} />
}

function PolicyForm({ config: data }: { config: ChecksConfigResponse }) {
  // `data.config` is the fully resolved policy (defaults + mira.yaml + DB) and
  // is what the form shows; `data.overrides` is only what an admin typed, and
  // is what gets written back.
  const [draft, setDraft] = useState<Draft>(() => draftFrom(data.config))
  // What the server resolved *before* this save. Anything the admin leaves
  // equal to it is not written, so an inherited `mira.yaml` value stays
  // inherited instead of being copied into the database.
  const [resolved] = useState<Draft>(() => draftFrom(data.config))
  const [saving, setSaving] = useState(false)

  const catalog = useAsync<CheckCatalogResponse>(
    () => api.getChecksCatalog(),
    []
  )

  const set = <K extends keyof Draft>(key: K, value: Draft[K]) =>
    setDraft((current) => ({ ...current, [key]: value }))

  const setMode = (checkId: string, mode: CheckMode | "__default__") => {
    setDraft((current) => {
      const modes = { ...current.modes }
      if (mode === "__default__") {
        delete modes[checkId]
      } else {
        modes[checkId] = mode
      }
      return { ...current, modes }
    })
  }

  const save = async () => {
    setSaving(true)
    try {
      // The endpoint replaces the whole `checks` section — wholesale, so that
      // an empty list is expressible. Keys this form does not render (the
      // analyser list, natural-language rules, ticket and CI settings,
      // per-organisation and per-repository entries) are carried over from
      // what was loaded, or saving would silently delete them. Fields the
      // admin did not change are omitted by `payloadFrom`, so an inherited
      // value stays inherited rather than being frozen into the override.
      await api.setChecksConfig({
        ...data.overrides,
        ...payloadFrom(draft, resolved),
      })
      toast.success("Check policy saved")
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
                Every check in the install is inert, whatever its repository or
                organisation policy says.
              </CardDescription>
            </div>
          </CardHeader>
        </Card>
      )}

      <Card>
        <CardHeader>
          <CardTitle className="text-base">Running at all</CardTitle>
          <CardDescription>
            Checks ship off. Turning them on with the default mode at{" "}
            <span className="font-mono">warning</span> reports everything and
            blocks nothing, which is the recommended first step.
          </CardDescription>
        </CardHeader>
        <CardContent className="space-y-4">
          <div className="grid gap-3 sm:grid-cols-2">
            <Toggle
              label="Run pre-merge checks"
              hint="Off by default. Per-organisation and per-repository entries in mira.yaml can still narrow this."
              checked={draft.enabled}
              onChange={(value) => set("enabled", value)}
            />
            <Toggle
              label="Kill switch"
              hint="Stops every check everywhere in one edit, independent of every override. For an incident, not for a policy change."
              checked={draft.kill_switch}
              onChange={(value) => set("kill_switch", value)}
            />
          </div>

          <div className="space-y-1.5">
            <label className="text-sm font-medium">Default mode</label>
            <Select
              value={draft.default_mode}
              onValueChange={(value) => set("default_mode", value as CheckMode)}
            >
              <SelectTrigger className="w-72">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                {MODES.map((mode) => (
                  <SelectItem key={mode} value={mode}>
                    {mode} — {MODE_HINT[mode]}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
            <p className="text-xs text-muted-foreground">
              Applies to every check without an entry below.
            </p>
          </div>
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle className="text-base">Per-check mode</CardTitle>
          <CardDescription>
            <span className="font-mono">error</span> blocks a merge on a
            violation <em>and</em> on an inability to answer — a missing linter,
            a model that would not respond, a CI run still in flight. That is
            deliberate: a check you declared blocking must not be satisfied by
            failing to run.
          </CardDescription>
        </CardHeader>
        <CardContent className="px-0 pb-0">
          {catalog.loading ? (
            <div className="space-y-2 px-6 pb-4">
              {Array.from({ length: 5 }).map((_, index) => (
                <Skeleton key={index} className="h-8 w-full" />
              ))}
            </div>
          ) : catalog.error || !catalog.data ? (
            <p className="px-6 pb-6 text-sm text-muted-foreground">
              {catalog.error ?? "The check catalog could not be loaded."}
            </p>
          ) : (
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>Check</TableHead>
                  <TableHead className="w-32">Origin</TableHead>
                  <TableHead className="w-24">Version</TableHead>
                  <TableHead className="w-52">Mode</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {catalog.data.checks.map((entry) => (
                  <TableRow key={entry.check_id}>
                    <TableCell>
                      <span className="block font-mono text-xs">
                        {entry.check_id}
                      </span>
                      <span className="block text-xs text-muted-foreground">
                        {entry.description}
                      </span>
                    </TableCell>
                    <TableCell>
                      <Badge variant="outline" className="font-normal">
                        {entry.origin.replace("_", " ")}
                      </Badge>
                    </TableCell>
                    <TableCell className="font-mono text-xs text-muted-foreground">
                      {entry.version}
                    </TableCell>
                    <TableCell>
                      <Select
                        value={draft.modes[entry.check_id] ?? "__default__"}
                        onValueChange={(value) =>
                          setMode(
                            entry.check_id,
                            value as CheckMode | "__default__"
                          )
                        }
                      >
                        <SelectTrigger className="h-8 w-44">
                          <SelectValue />
                        </SelectTrigger>
                        <SelectContent>
                          <SelectItem value="__default__">
                            Default ({draft.default_mode})
                          </SelectItem>
                          {MODES.map((mode) => (
                            <SelectItem key={mode} value={mode}>
                              {mode}
                            </SelectItem>
                          ))}
                        </SelectContent>
                      </Select>
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          )}
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle className="text-base">Budget and announcing</CardTitle>
          <CardDescription>
            The defaults assume a four-core board also serving webhooks. A check
            that overruns is recorded as a timeout; one that never started
            because the run budget was spent says so — and neither is a pass.
          </CardDescription>
        </CardHeader>
        <CardContent className="space-y-4">
          <div className="grid gap-4 sm:grid-cols-3">
            <div className="space-y-1.5">
              <label className="text-sm font-medium">Concurrency</label>
              <Input
                type="number"
                min={1}
                max={16}
                value={draft.max_concurrency}
                onChange={(event) =>
                  set("max_concurrency", Number(event.target.value))
                }
              />
            </div>
            <div className="space-y-1.5">
              <label className="text-sm font-medium">Per check (s)</label>
              <Input
                type="number"
                min={1}
                value={draft.check_timeout_seconds}
                onChange={(event) =>
                  set("check_timeout_seconds", Number(event.target.value))
                }
              />
            </div>
            <div className="space-y-1.5">
              <label className="text-sm font-medium">Whole run (s)</label>
              <Input
                type="number"
                min={1}
                value={draft.total_timeout_seconds}
                onChange={(event) =>
                  set("total_timeout_seconds", Number(event.target.value))
                }
              />
            </div>
          </div>
          <div className="grid gap-3 sm:grid-cols-2">
            <Toggle
              label="Publish a status check"
              hint="Where the provider supports one. Never red for a check Mira could not run — that publishes neutral."
              checked={draft.publish_status}
              onChange={(value) => set("publish_status", value)}
            />
            <Toggle
              label="Comment on the pull request"
              hint="A single comment, updated in place. The only way to surface the summary on GitLab, which gets no status."
              checked={draft.comment}
              onChange={(value) => set("comment", value)}
            />
          </div>
        </CardContent>
      </Card>

      <Card>
        <CardHeader className="flex-row items-start gap-3 space-y-0">
          <AlertTriangle className="mt-0.5 h-4 w-4 text-muted-foreground" />
          <div>
            <CardTitle className="text-base">
              Edited here, or in mira.yaml
            </CardTitle>
            <CardDescription>
              Analysers, natural-language rules, ticket and CI settings, and the
              per-organisation and per-repository entries live in{" "}
              <span className="font-mono">mira.yaml</span>. Saving here leaves
              them untouched. Nothing in a pull request can reach any of it.
            </CardDescription>
          </div>
        </CardHeader>
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
