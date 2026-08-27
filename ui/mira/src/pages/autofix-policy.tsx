import { AlertTriangle, Save, ShieldOff } from "lucide-react"
import { useMemo, useState } from "react"

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
import { Textarea } from "@/components/ui/textarea"
import { api } from "@/lib/api"
import type { AutofixConfigResponse } from "@/lib/api"
import { useAsync } from "@/lib/hooks"

type Draft = {
  mode: string
  kill_switch: boolean
  require_write_permission: boolean
  allow_unknown_permission: boolean
  allow_commit_to_pr_branch: boolean
  restrict_to_changed_files: boolean
  allow_new_files: boolean
  branch_prefix: string
  max_files: number
  max_lines: number
  max_fixes_per_request: number
  max_concurrent_jobs: number
  max_attempts: number
  max_ci_retries: number
  min_severity_for_fix_all: string
  inline_worker: boolean
  allowed_requesters: string
  blocked_requesters: string
  extra_protected_paths: string
  cancel_admins: string
}

const EMPTY: Draft = {
  mode: "off",
  kill_switch: false,
  require_write_permission: true,
  allow_unknown_permission: false,
  allow_commit_to_pr_branch: false,
  restrict_to_changed_files: true,
  allow_new_files: false,
  branch_prefix: "mira/fix",
  max_files: 3,
  max_lines: 120,
  max_fixes_per_request: 3,
  max_concurrent_jobs: 2,
  max_attempts: 2,
  max_ci_retries: 1,
  min_severity_for_fix_all: "warning",
  inline_worker: true,
  allowed_requesters: "",
  blocked_requesters: "",
  extra_protected_paths: "",
  cancel_admins: "",
}

function asList(value: unknown): string {
  return Array.isArray(value) ? value.join("\n") : ""
}

function toList(value: string): string[] {
  return value
    .split(/[\n,]/)
    .map((item) => item.trim())
    .filter(Boolean)
}

function draftFrom(config: Record<string, unknown>): Draft {
  const pick = <T,>(key: string, fallback: T): T =>
    (config[key] as T | undefined) ?? fallback
  return {
    mode: pick("mode", EMPTY.mode),
    kill_switch: pick("kill_switch", EMPTY.kill_switch),
    require_write_permission: pick(
      "require_write_permission",
      EMPTY.require_write_permission
    ),
    allow_unknown_permission: pick(
      "allow_unknown_permission",
      EMPTY.allow_unknown_permission
    ),
    allow_commit_to_pr_branch: pick(
      "allow_commit_to_pr_branch",
      EMPTY.allow_commit_to_pr_branch
    ),
    restrict_to_changed_files: pick(
      "restrict_to_changed_files",
      EMPTY.restrict_to_changed_files
    ),
    allow_new_files: pick("allow_new_files", EMPTY.allow_new_files),
    branch_prefix: pick("branch_prefix", EMPTY.branch_prefix),
    max_files: pick("max_files", EMPTY.max_files),
    max_lines: pick("max_lines", EMPTY.max_lines),
    max_fixes_per_request: pick(
      "max_fixes_per_request",
      EMPTY.max_fixes_per_request
    ),
    max_concurrent_jobs: pick("max_concurrent_jobs", EMPTY.max_concurrent_jobs),
    max_attempts: pick("max_attempts", EMPTY.max_attempts),
    max_ci_retries: pick("max_ci_retries", EMPTY.max_ci_retries),
    min_severity_for_fix_all: pick(
      "min_severity_for_fix_all",
      EMPTY.min_severity_for_fix_all
    ),
    inline_worker: pick("inline_worker", EMPTY.inline_worker),
    allowed_requesters: asList(config.allowed_requesters),
    blocked_requesters: asList(config.blocked_requesters),
    extra_protected_paths: asList(config.extra_protected_paths),
    cancel_admins: asList(config.cancel_admins),
  }
}

function payloadFrom(draft: Draft): Record<string, unknown> {
  return {
    mode: draft.mode,
    kill_switch: draft.kill_switch,
    require_write_permission: draft.require_write_permission,
    allow_unknown_permission: draft.allow_unknown_permission,
    allow_commit_to_pr_branch: draft.allow_commit_to_pr_branch,
    restrict_to_changed_files: draft.restrict_to_changed_files,
    allow_new_files: draft.allow_new_files,
    branch_prefix: draft.branch_prefix,
    max_files: Number(draft.max_files),
    max_lines: Number(draft.max_lines),
    max_fixes_per_request: Number(draft.max_fixes_per_request),
    max_concurrent_jobs: Number(draft.max_concurrent_jobs),
    max_attempts: Number(draft.max_attempts),
    max_ci_retries: Number(draft.max_ci_retries),
    min_severity_for_fix_all: draft.min_severity_for_fix_all,
    inline_worker: draft.inline_worker,
    allowed_requesters: toList(draft.allowed_requesters),
    blocked_requesters: toList(draft.blocked_requesters),
    extra_protected_paths: toList(draft.extra_protected_paths),
    cancel_admins: toList(draft.cancel_admins),
  }
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
  onChange,
}: {
  label: string
  hint: string
  value: number
  onChange: (value: number) => void
}) {
  return (
    <div className="space-y-1.5">
      <label className="text-sm font-medium">{label}</label>
      <Input
        type="number"
        value={value}
        onChange={(event) => onChange(Number(event.target.value))}
        className="h-9"
      />
      <p className="text-xs text-muted-foreground">{hint}</p>
    </div>
  )
}

function ListField({
  label,
  hint,
  value,
  onChange,
  placeholder,
}: {
  label: string
  hint: string
  value: string
  onChange: (value: string) => void
  placeholder?: string
}) {
  return (
    <div className="space-y-1.5">
      <label className="text-sm font-medium">{label}</label>
      <Textarea
        value={value}
        onChange={(event) => onChange(event.target.value)}
        placeholder={placeholder}
        rows={3}
        className="font-mono text-xs"
      />
      <p className="text-xs text-muted-foreground">{hint}</p>
    </div>
  )
}

export function AutofixPolicyPanel() {
  const { data, loading, error } = useAsync<AutofixConfigResponse>(
    () => api.getAutofixConfig(),
    []
  )

  if (loading) return <Skeleton className="h-96 w-full" />
  if (error || !data)
    return (
      <p className="text-sm text-muted-foreground">
        {error ?? "The autofix policy could not be loaded."}
      </p>
    )
  // Keyed on the loaded policy so the form owns its draft from the first
  // render. Seeding it from an effect would mean one render showing values
  // nobody configured, and a cascading re-render to correct them.
  return <PolicyForm key={String(data.effective.version ?? "")} config={data} />
}

function PolicyForm({ config: data }: { config: AutofixConfigResponse }) {
  // `data.config` is the fully resolved policy (defaults + mira.yaml + DB) and
  // is what the form shows; `data.overrides` is only what an admin typed, and
  // is what gets written back.
  const [draft, setDraft] = useState<Draft>(() => draftFrom(data.config))
  const [saving, setSaving] = useState(false)

  const set = <K extends keyof Draft>(key: K, value: Draft[K]) =>
    setDraft((current) => ({ ...current, [key]: value }))

  const protectedPaths = useMemo(
    () => (data?.effective?.protected_paths as string[] | undefined) ?? [],
    [data]
  )
  const commands = useMemo(() => {
    const validation = data?.effective?.validation as
      | { commands?: { name?: string; command?: string[] }[] }
      | undefined
    return validation?.commands ?? []
  }, [data])

  const save = async () => {
    setSaving(true)
    try {
      // The endpoint replaces the whole `autofix` section — wholesale, so that
      // an empty list is expressible. Keys this form does not render (per-repo
      // policy, the validation command allowlist, handoff options) are carried
      // over from what was loaded, or saving would silently delete them.
      await api.setAutofixConfig({ ...data.overrides, ...payloadFrom(draft) })
      toast.success("Autofix policy saved")
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
                No fix is accepted and no queued job runs, whatever any
                repository&apos;s own policy says.
              </CardDescription>
            </div>
          </CardHeader>
        </Card>
      )}

      <Card>
        <CardHeader>
          <CardTitle className="text-base">Mode</CardTitle>
          <CardDescription>
            Start in suggest. It generates and validates the patch, shows you
            the diff, and writes nothing — which is how you find out what
            turning it on would have done.
          </CardDescription>
        </CardHeader>
        <CardContent className="space-y-4">
          <div className="flex flex-wrap items-center gap-3">
            <Select
              value={draft.mode}
              onValueChange={(value) => set("mode", value)}
            >
              <SelectTrigger className="w-56">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="off">Off — no fix is generated</SelectItem>
                <SelectItem value="suggest">
                  Suggest — generate and validate, write nothing
                </SelectItem>
                <SelectItem value="on">
                  On — branch, commit and pull request
                </SelectItem>
              </SelectContent>
            </Select>
            <Badge variant="outline" className="font-mono text-[10px]">
              {String(data.effective.version ?? "")}
            </Badge>
          </div>
          <Toggle
            label="Kill switch"
            hint="Stops every repository at once, and stops jobs that are already queued. For an incident, not for a rollout."
            checked={draft.kill_switch}
            onChange={(value) => set("kill_switch", value)}
          />
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle className="text-base">Who may ask</CardTitle>
          <CardDescription>
            A fix is written on the requester&apos;s behalf, so the permission
            checked is the one it exercises: write access on the repository,
            read from the platform.
          </CardDescription>
        </CardHeader>
        <CardContent className="space-y-4">
          <div className="grid gap-3 sm:grid-cols-2">
            <Toggle
              label="Require write permission"
              hint="Off means the requester allowlist below is your entire permission model."
              checked={draft.require_write_permission}
              onChange={(value) => set("require_write_permission", value)}
            />
            <Toggle
              label="Accept an unreadable permission"
              hint="Leave off. An unreadable permission is not a permission."
              checked={draft.allow_unknown_permission}
              onChange={(value) => set("allow_unknown_permission", value)}
            />
          </div>
          <div className="grid gap-4 sm:grid-cols-2">
            <ListField
              label="Requester allowlist"
              hint="Empty means anyone who can already write here. One login per line."
              value={draft.allowed_requesters}
              onChange={(value) => set("allowed_requesters", value)}
              placeholder="alice&#10;bob"
            />
            <ListField
              label="Requester blocklist"
              hint="Checked before the platform is asked, so a blocked account stays blocked even when the API is down."
              value={draft.blocked_requesters}
              onChange={(value) => set("blocked_requesters", value)}
            />
          </div>
          <ListField
            label="Cancel admins"
            hint="Admins permitted to stop a job. Empty means every admin. Separates administering Mira from stopping somebody's fix."
            value={draft.cancel_admins}
            onChange={(value) => set("cancel_admins", value)}
          />
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle className="text-base">What may be written</CardTitle>
          <CardDescription>
            The default branch is never written to, and nothing is ever
            force-pushed or merged. These settings narrow what is left.
          </CardDescription>
        </CardHeader>
        <CardContent className="space-y-4">
          <div className="grid gap-3 sm:grid-cols-2">
            <Toggle
              label="Allow committing to the pull request's branch"
              hint="Opt-in. Still refused when the head branch is the default branch or lives in a fork."
              checked={draft.allow_commit_to_pr_branch}
              onChange={(value) => set("allow_commit_to_pr_branch", value)}
            />
            <Toggle
              label="Only files the pull request touches"
              hint="A fix that wanders into an untouched file is a change nobody asked for."
              checked={draft.restrict_to_changed_files}
              onChange={(value) => set("restrict_to_changed_files", value)}
            />
            <Toggle
              label="Allow creating new files"
              hint="Off by default, for the same reason."
              checked={draft.allow_new_files}
              onChange={(value) => set("allow_new_files", value)}
            />
            <Toggle
              label="Run the worker in this process"
              hint="On for a single container. Off if you run `mira autofix-worker` separately."
              checked={draft.inline_worker}
              onChange={(value) => set("inline_worker", value)}
            />
          </div>
          <div className="space-y-1.5">
            <label className="text-sm font-medium">Branch prefix</label>
            <Input
              value={draft.branch_prefix}
              onChange={(event) => set("branch_prefix", event.target.value)}
              className="h-9 font-mono"
            />
            <p className="text-xs text-muted-foreground">
              Branch names are{" "}
              <span className="font-mono">
                {draft.branch_prefix}/pr-&lt;number&gt;/&lt;finding&gt;
              </span>
              . Deterministic, so a retry lands on the branch the last attempt
              made instead of beside it.
            </p>
          </div>
          <ListField
            label="Extra protected paths"
            hint="Never edited by a fix, on top of the built-in list. Glob patterns, one per line."
            value={draft.extra_protected_paths}
            onChange={(value) => set("extra_protected_paths", value)}
            placeholder="infra/**&#10;*.tf"
          />
          {protectedPaths.length > 0 && (
            <div className="rounded-md border bg-muted/30 p-3">
              <p className="mb-1 text-xs font-medium">
                In effect ({protectedPaths.length} pattern(s))
              </p>
              <p className="font-mono text-[11px] break-all text-muted-foreground">
                {protectedPaths.join("  ·  ")}
              </p>
            </div>
          )}
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle className="text-base">Limits</CardTitle>
          <CardDescription>
            A fix is meant to be a change a human reads in one sitting.
            Everything past these numbers is refused with the reason.
          </CardDescription>
        </CardHeader>
        <CardContent className="space-y-4">
          <div className="grid gap-4 sm:grid-cols-3">
            <NumberField
              label="Max files"
              hint="Per patch, measured on the applied result."
              value={draft.max_files}
              onChange={(value) => set("max_files", value)}
            />
            <NumberField
              label="Max changed lines"
              hint="Added plus deleted."
              value={draft.max_lines}
              onChange={(value) => set("max_lines", value)}
            />
            <NumberField
              label="Max fixes per request"
              hint="`fix all` never means all. Whatever this excludes is named in the reply."
              value={draft.max_fixes_per_request}
              onChange={(value) => set("max_fixes_per_request", value)}
            />
            <NumberField
              label="Max jobs in flight"
              hint="Per repository, so one `fix all` cannot occupy every worker."
              value={draft.max_concurrent_jobs}
              onChange={(value) => set("max_concurrent_jobs", value)}
            />
            <NumberField
              label="Attempts per job"
              hint="After this the job is parked in the dead-letter state with its last error."
              value={draft.max_attempts}
              onChange={(value) => set("max_attempts", value)}
            />
            <NumberField
              label="CI retries"
              hint="Regenerations driven by a red CI run on the fix's own pull request. Conservative on purpose."
              value={draft.max_ci_retries}
              onChange={(value) => set("max_ci_retries", value)}
            />
          </div>
          <div className="space-y-1.5">
            <label className="text-sm font-medium">
              Severity floor for <span className="font-mono">fix all</span>
            </label>
            <Select
              value={draft.min_severity_for_fix_all}
              onValueChange={(value) => set("min_severity_for_fix_all", value)}
            >
              <SelectTrigger className="w-56">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="blocker">Blockers only</SelectItem>
                <SelectItem value="warning">Warnings and above</SelectItem>
                <SelectItem value="suggestion">Suggestions and above</SelectItem>
                <SelectItem value="nitpick">Everything</SelectItem>
              </SelectContent>
            </Select>
            <p className="text-xs text-muted-foreground">
              A single <span className="font-mono">fix</span> on a named finding
              is not filtered by this — the maintainer already picked that one.
            </p>
          </div>
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle className="text-base">Validation</CardTitle>
          <CardDescription>
            Every patch is parsed and swept for credentials in-process. Command
            checks are an allowlist, and they are edited in{" "}
            <span className="font-mono">mira.yaml</span> — not here, and never
            from anything a pull request contains.
          </CardDescription>
        </CardHeader>
        <CardContent>
          {commands.length === 0 ? (
            <p className="text-sm text-muted-foreground">
              No commands are configured, so validation is static only: the
              edited files are parsed, and a patch that would commit something
              credential-shaped is refused.
            </p>
          ) : (
            <ul className="space-y-1 text-sm">
              {commands.map((entry, index) => (
                <li key={index} className="flex items-baseline gap-2">
                  <Badge variant="outline" className="font-mono text-[10px]">
                    {entry.name ?? "check"}
                  </Badge>
                  <code className="text-xs break-all text-muted-foreground">
                    {(entry.command ?? []).join(" ")}
                  </code>
                </li>
              ))}
            </ul>
          )}
        </CardContent>
      </Card>

      {draft.mode === "on" && (
        <Card className="border-amber-500/40">
          <CardHeader className="flex-row items-start gap-3 space-y-0">
            <AlertTriangle className="mt-0.5 h-5 w-5 text-amber-500" />
            <div>
              <CardTitle className="text-base">
                Mira will write to your repositories
              </CardTitle>
              <CardDescription>
                On a branch of its own, in a pull request a human has to read
                and merge. It never writes to the default branch, never force
                pushes, and never merges anything.
              </CardDescription>
            </div>
          </CardHeader>
        </Card>
      )}

      <div className="flex justify-end">
        <Button onClick={save} disabled={saving}>
          <Save className="mr-1 h-4 w-4" />
          {saving ? "Saving…" : "Save policy"}
        </Button>
      </div>
    </div>
  )
}
