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
import type { GateConfigResponse } from "@/lib/api"
import { useAsync } from "@/lib/hooks"

type Draft = {
  mode: string
  kill_switch: boolean
  codeowners: string
  risk_threshold: number
  max_changed_files: number
  max_changed_lines: number
  require_ci_success: boolean
  require_all_files_reviewed: boolean
  require_index_ready: boolean
  request_changes_on_blockers: boolean
  publish_status: boolean
  comment: boolean
  allow_overrides: boolean
  allow_approval_override: boolean
  blocked_labels: string
  required_labels: string
  allowed_base_branches: string
  extra_protected_paths: string
  override_admins: string
}

const EMPTY: Draft = {
  mode: "off",
  kill_switch: false,
  codeowners: "off",
  risk_threshold: 25,
  max_changed_files: 20,
  max_changed_lines: 500,
  require_ci_success: true,
  require_all_files_reviewed: true,
  require_index_ready: true,
  request_changes_on_blockers: false,
  publish_status: true,
  comment: false,
  allow_overrides: true,
  allow_approval_override: false,
  blocked_labels: "",
  required_labels: "",
  allowed_base_branches: "",
  extra_protected_paths: "",
  override_admins: "",
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
    codeowners: pick("codeowners", EMPTY.codeowners),
    risk_threshold: pick("risk_threshold", EMPTY.risk_threshold),
    max_changed_files: pick("max_changed_files", EMPTY.max_changed_files),
    max_changed_lines: pick("max_changed_lines", EMPTY.max_changed_lines),
    require_ci_success: pick("require_ci_success", EMPTY.require_ci_success),
    require_all_files_reviewed: pick(
      "require_all_files_reviewed",
      EMPTY.require_all_files_reviewed
    ),
    require_index_ready: pick("require_index_ready", EMPTY.require_index_ready),
    request_changes_on_blockers: pick(
      "request_changes_on_blockers",
      EMPTY.request_changes_on_blockers
    ),
    publish_status: pick("publish_status", EMPTY.publish_status),
    comment: pick("comment", EMPTY.comment),
    allow_overrides: pick("allow_overrides", EMPTY.allow_overrides),
    allow_approval_override: pick(
      "allow_approval_override",
      EMPTY.allow_approval_override
    ),
    blocked_labels: asList(config.blocked_labels),
    required_labels: asList(config.required_labels),
    allowed_base_branches: asList(config.allowed_base_branches),
    extra_protected_paths: asList(config.extra_protected_paths),
    override_admins: asList(config.override_admins),
  }
}

function payloadFrom(draft: Draft): Record<string, unknown> {
  return {
    mode: draft.mode,
    kill_switch: draft.kill_switch,
    codeowners: draft.codeowners,
    risk_threshold: Number(draft.risk_threshold),
    max_changed_files: Number(draft.max_changed_files),
    max_changed_lines: Number(draft.max_changed_lines),
    require_ci_success: draft.require_ci_success,
    require_all_files_reviewed: draft.require_all_files_reviewed,
    require_index_ready: draft.require_index_ready,
    request_changes_on_blockers: draft.request_changes_on_blockers,
    publish_status: draft.publish_status,
    comment: draft.comment,
    allow_overrides: draft.allow_overrides,
    allow_approval_override: draft.allow_approval_override,
    blocked_labels: toList(draft.blocked_labels),
    required_labels: toList(draft.required_labels),
    allowed_base_branches: toList(draft.allowed_base_branches),
    extra_protected_paths: toList(draft.extra_protected_paths),
    override_admins: toList(draft.override_admins),
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

export function GatePolicyPanel() {
  const { data, loading, error } = useAsync<GateConfigResponse>(
    () => api.getGateConfig(),
    []
  )

  if (loading) return <Skeleton className="h-96 w-full" />
  if (error || !data)
    return (
      <p className="text-sm text-muted-foreground">
        {error ?? "The gate policy could not be loaded."}
      </p>
    )
  // Keyed on the loaded policy so the form owns its draft from the first
  // render. Seeding it from an effect would mean one render showing values
  // nobody configured, and a cascading re-render to correct them.
  return <PolicyForm key={String(data.effective.version ?? "")} config={data} />
}

function PolicyForm({ config: data }: { config: GateConfigResponse }) {
  const [draft, setDraft] = useState<Draft>(() => draftFrom(data.config))
  const [saving, setSaving] = useState(false)

  const set = <K extends keyof Draft>(key: K, value: Draft[K]) =>
    setDraft((current) => ({ ...current, [key]: value }))

  const protectedPaths = useMemo(
    () => (data?.effective?.protected_paths as string[] | undefined) ?? [],
    [data]
  )

  const save = async () => {
    setSaving(true)
    try {
      await api.setGateConfig(payloadFrom(draft))
      toast.success("Gate policy saved")
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
                Every repository is inert, whatever its own policy says.
              </CardDescription>
            </div>
          </CardHeader>
        </Card>
      )}

      <Card>
        <CardHeader>
          <CardTitle className="text-base">Mode</CardTitle>
          <CardDescription>
            Start in shadow. It reaches and records the same decision it would
            act on, which is what makes the false-approval rate measurable.
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
                <SelectItem value="off">Off — do not run</SelectItem>
                <SelectItem value="shadow">
                  Shadow — decide, never act
                </SelectItem>
                <SelectItem value="enforce">
                  Enforce — approve when it says so
                </SelectItem>
              </SelectContent>
            </Select>
            {draft.mode === "enforce" && (
              <Badge
                variant="secondary"
                className="gap-1 bg-amber-500/10 text-amber-600"
              >
                <AlertTriangle className="h-3 w-3" />
                Real approvals will be submitted
              </Badge>
            )}
          </div>
          <Toggle
            label="Kill switch"
            hint="Hard-disable the gate everywhere, independent of mode and every per-repository override."
            checked={draft.kill_switch}
            onChange={(value) => set("kill_switch", value)}
          />
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle className="text-base">Eligibility</CardTitle>
          <CardDescription>
            Deployment policy. Nothing in a pull request can change any of it —
            labels and branches are inputs you chose to consult, and consulting
            them can only ever take a PR out of scope or disqualify it.
          </CardDescription>
        </CardHeader>
        <CardContent className="grid gap-4 md:grid-cols-2">
          <div className="space-y-1.5">
            <label className="text-sm font-medium">Risk threshold</label>
            <Input
              type="number"
              min={0}
              max={100}
              value={draft.risk_threshold}
              onChange={(event) =>
                set("risk_threshold", Number(event.target.value))
              }
            />
            <p className="text-xs text-muted-foreground">
              A PR scoring above this is never approved, however clean it looks.
            </p>
          </div>
          <div className="grid grid-cols-2 gap-3">
            <div className="space-y-1.5">
              <label className="text-sm font-medium">Max files</label>
              <Input
                type="number"
                min={1}
                value={draft.max_changed_files}
                onChange={(event) =>
                  set("max_changed_files", Number(event.target.value))
                }
              />
            </div>
            <div className="space-y-1.5">
              <label className="text-sm font-medium">Max lines</label>
              <Input
                type="number"
                min={1}
                value={draft.max_changed_lines}
                onChange={(event) =>
                  set("max_changed_lines", Number(event.target.value))
                }
              />
            </div>
          </div>
          <ListField
            label="Blocking labels"
            hint="Any of these on a PR disqualifies it. One per line."
            value={draft.blocked_labels}
            onChange={(value) => set("blocked_labels", value)}
            placeholder={"do-not-merge\nwip"}
          />
          <ListField
            label="Required labels"
            hint="Without all of these the gate is out of scope. Necessary, never sufficient."
            value={draft.required_labels}
            onChange={(value) => set("required_labels", value)}
          />
          <ListField
            label="Base branches in scope"
            hint="Empty means every branch."
            value={draft.allowed_base_branches}
            onChange={(value) => set("allowed_base_branches", value)}
            placeholder="main"
          />
          <ListField
            label="Extra protected paths"
            hint="Added to the built-in list. A match is an absolute veto — never an auto-approval."
            value={draft.extra_protected_paths}
            onChange={(value) => set("extra_protected_paths", value)}
            placeholder={"infra/**\n*.tf"}
          />
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle className="text-base">Completeness</CardTitle>
          <CardDescription>
            What has to be true before the gate will vouch for a change. Each of
            these resolves an unknown to &ldquo;no approval&rdquo;.
          </CardDescription>
        </CardHeader>
        <CardContent className="grid gap-3 md:grid-cols-2">
          <Toggle
            label="Require green CI"
            hint="Pending, failing and unreadable all count as not green."
            checked={draft.require_ci_success}
            onChange={(value) => set("require_ci_success", value)}
          />
          <Toggle
            label="Require every file reviewed"
            hint="A PR that blew past the diff budget was only half read."
            checked={draft.require_all_files_reviewed}
            onChange={(value) => set("require_all_files_reviewed", value)}
          />
          <Toggle
            label="Require a ready index"
            hint="Without it the review had partial cross-file context."
            checked={draft.require_index_ready}
            onChange={(value) => set("require_index_ready", value)}
          />
          <div className="space-y-1.5">
            <label className="text-sm font-medium">CODEOWNERS</label>
            <Select
              value={draft.codeowners}
              onValueChange={(value) => set("codeowners", value)}
            >
              <SelectTrigger>
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="off">Off — do not read it</SelectItem>
                <SelectItem value="risk">
                  Risk — an owned path adds risk
                </SelectItem>
                <SelectItem value="block">
                  Block — an owned path is never auto-approved
                </SelectItem>
              </SelectContent>
            </Select>
            <p className="text-xs text-muted-foreground">
              In block mode a CODEOWNERS Mira cannot parse is also a veto.
            </p>
          </div>
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle className="text-base">Actions and overrides</CardTitle>
        </CardHeader>
        <CardContent className="grid gap-3 md:grid-cols-2">
          <Toggle
            label="Request changes on open blockers"
            hint="Enforce mode only, and never over an existing human review."
            checked={draft.request_changes_on_blockers}
            onChange={(value) => set("request_changes_on_blockers", value)}
          />
          <Toggle
            label="Publish a status check"
            hint="Carries the explanation. Neutral in shadow mode."
            checked={draft.publish_status}
            onChange={(value) => set("publish_status", value)}
          />
          <Toggle
            label="Comment on the pull request"
            hint="Posts the public explanation and updates it in place."
            checked={draft.comment}
            onChange={(value) => set("comment", value)}
          />
          <Toggle
            label="Allow overrides"
            hint="Revoking a decision by hand. Always recorded with actor and reason."
            checked={draft.allow_overrides}
            onChange={(value) => set("allow_overrides", value)}
          />
          <Toggle
            label="Allow forcing an approval"
            hint="A separate power from revoking one, and still refused past a hard veto."
            checked={draft.allow_approval_override}
            onChange={(value) => set("allow_approval_override", value)}
          />
          <ListField
            label="Override admins"
            hint="Admins permitted to move a decision. Empty means every admin."
            value={draft.override_admins}
            onChange={(value) => set("override_admins", value)}
          />
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle className="text-base">Effective protected paths</CardTitle>
          <CardDescription>
            What a pull request will actually be matched against, after the
            built-in list and your additions are resolved.
          </CardDescription>
        </CardHeader>
        <CardContent>
          <div className="flex flex-wrap gap-1.5">
            {protectedPaths.map((pattern) => (
              <Badge
                key={pattern}
                variant="outline"
                className="font-mono text-xs"
              >
                {pattern}
              </Badge>
            ))}
          </div>
        </CardContent>
      </Card>

      <div className="flex justify-end">
        <Button onClick={save} disabled={saving}>
          <Save className="mr-1.5 h-4 w-4" />
          {saving ? "Saving…" : "Save policy"}
        </Button>
      </div>
    </div>
  )
}
