import { CheckCircle2, Loader2, TriangleAlert } from "lucide-react"
import { useState } from "react"

import { Button } from "@/components/ui/button"
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog"
import { Input } from "@/components/ui/input"
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select"
import { api } from "@/lib/api"
import type {
  EndpointPreset,
  EndpointTest,
  ProviderEndpoint,
} from "@/lib/api/providers"

const PROTOCOLS = [
  { value: "chat", label: "Chat Completions" },
  { value: "responses", label: "Responses API" },
]

// Stands in for the empty preset id inside the Select, which rejects one.
const CUSTOM = "__custom__"

function Field({
  label,
  hint,
  children,
}: {
  label: string
  hint?: React.ReactNode
  children: React.ReactNode
}) {
  return (
    <div className="space-y-1.5">
      <label className="text-sm font-medium">{label}</label>
      {children}
      {hint && <p className="text-xs text-muted-foreground">{hint}</p>}
    </div>
  )
}

// Add or edit an endpoint. Used by the Connections page and by setup, which
// is the same question asked at different moments: where do calls go, and
// what opens it.
//
// The form is its own component and lives inside the dialog content, so
// closing the dialog unmounts it and every field — the key above all —
// starts empty next time, with no effect to clear them.
export function EndpointDialog({
  open,
  onOpenChange,
  presets,
  envCandidates,
  editing,
  makeDefault,
  onSaved,
}: {
  open: boolean
  onOpenChange: (open: boolean) => void
  presets: EndpointPreset[]
  envCandidates: string[]
  // The endpoint being edited, or null to add one.
  editing?: ProviderEndpoint | null
  // Make the saved endpoint the one bare model ids go to.
  makeDefault?: boolean
  onSaved: (endpoint: ProviderEndpoint) => void
}) {
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-h-[85vh] overflow-y-auto sm:max-w-lg">
        {open && (
          <EndpointForm
            key={editing?.id ?? "new"}
            presets={presets}
            envCandidates={envCandidates}
            editing={editing ?? null}
            makeDefault={makeDefault}
            onSaved={onSaved}
            onClose={() => onOpenChange(false)}
          />
        )}
      </DialogContent>
    </Dialog>
  )
}

function EndpointForm({
  presets,
  envCandidates,
  editing,
  makeDefault,
  onSaved,
  onClose,
}: {
  presets: EndpointPreset[]
  envCandidates: string[]
  editing: ProviderEndpoint | null
  makeDefault?: boolean
  onSaved: (endpoint: ProviderEndpoint) => void
  onClose: () => void
}) {
  const [preset, setPreset] = useState(editing?.preset ?? "")
  const [label, setLabel] = useState(editing?.label ?? "")
  const [baseUrl, setBaseUrl] = useState(editing?.endpoint ?? "")
  const [apiStyle, setApiStyle] = useState(
    editing?.protocol.api_style ?? "chat"
  )
  // Always empty: the server never sends a key back, so an untouched field
  // means "keep whatever is stored".
  const [apiKey, setApiKey] = useState("")
  // The variable the endpoint names, not the one it is currently reading: a
  // server that does not export it yet still has it configured, and seeding
  // this from "where the key comes from" would blank the field and save the
  // pointer away.
  const [apiKeyEnv, setApiKeyEnv] = useState(editing?.key_variable ?? "")
  // Sends api_key: "" — the only way to take a stored key back out.
  const [clearKey, setClearKey] = useState(false)
  const [saving, setSaving] = useState(false)
  const [testing, setTesting] = useState(false)
  const [tested, setTested] = useState<EndpointTest | null>(null)
  const [error, setError] = useState("")

  const chosen = presets.find((p) => p.id === preset)

  const pickPreset = (id: string) => {
    setPreset(id)
    setTested(null)
    const found = presets.find((p) => p.id === id)
    if (!found) return
    // Prefill what the preset knows, leaving anything already typed alone:
    // picking a preset is a starting point, not a reset.
    if (
      found.base_url &&
      (!baseUrl || presets.some((p) => p.base_url === baseUrl))
    )
      setBaseUrl(found.base_url)
    if (found.id && (!label || presets.some((p) => p.label === label)))
      setLabel(found.label)
    if (found.api_style) setApiStyle(found.api_style)
  }

  const runTest = async () => {
    setTesting(true)
    setError("")
    try {
      setTested(
        await api.testProvider({
          base_url: baseUrl,
          preset,
          // Omitted rather than empty, so the test uses whatever the
          // endpoint already has when the field was not retyped.
          ...(apiKey ? { api_key: apiKey } : {}),
          api_key_env: apiKeyEnv,
          endpoint: editing?.id ?? "",
        })
      )
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setTesting(false)
    }
  }

  const save = async () => {
    setSaving(true)
    setError("")
    try {
      const body = {
        label,
        base_url: baseUrl,
        preset,
        api_style: apiStyle,
        api_key_env: apiKeyEnv,
        // A typed key replaces the stored one and "" removes it. Absent —
        // neither typed nor cleared — leaves it alone, which is what an
        // edit that only touches the name has to do.
        ...(apiKey ? { api_key: apiKey } : clearKey ? { api_key: "" } : {}),
        ...(makeDefault ? { make_default: true } : {}),
      }
      const saved = editing
        ? await api.updateProvider(editing.id, body)
        : await api.createProvider(body)
      onSaved(saved)
      onClose()
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setSaving(false)
    }
  }

  const keyPlaceholder = clearKey
    ? "The stored key will be removed on save"
    : editing?.key_source === "stored"
      ? "Leave blank to keep the key that is stored"
      : chosen?.api_key_env
        ? `sk-…  (or leave blank and read ${chosen.api_key_env} from the environment)`
        : "sk-…"

  return (
    <>
      <DialogHeader>
        <DialogTitle>
          {editing ? `Edit ${editing.label}` : "Add an endpoint"}
        </DialogTitle>
        <DialogDescription>
          Any OpenAI-compatible endpoint. Start from a preset to fill in the URL
          and the quirks it needs, or type a URL of your own. The key is stored
          in Mira&apos;s database and never shown again.
        </DialogDescription>
      </DialogHeader>

      <div className="space-y-4">
        <Field
          label="Provider"
          hint={
            chosen?.description ||
            "A gateway, a proxy, or a model server you run."
          }
        >
          {/* "Custom" is the empty preset id; Select needs a non-empty
                value for every item, so it travels as a sentinel. */}
          <Select
            value={preset || CUSTOM}
            onValueChange={(v) => pickPreset(v === CUSTOM ? "" : v)}
          >
            <SelectTrigger>
              <SelectValue placeholder="Custom endpoint" />
            </SelectTrigger>
            <SelectContent>
              {presets.map((p) => (
                <SelectItem key={p.id || CUSTOM} value={p.id || CUSTOM}>
                  {p.label}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
        </Field>

        <Field label="Name">
          <Input
            value={label}
            placeholder="OpenCode Go"
            onChange={(e) => setLabel(e.target.value)}
          />
        </Field>

        <Field
          label="URL"
          hint="The base URL, ending in /v1 for most providers. Plain http is allowed only for localhost and private addresses."
        >
          <Input
            value={baseUrl}
            placeholder="https://opencode.ai/zen/go/v1"
            onChange={(e) => {
              setBaseUrl(e.target.value)
              setTested(null)
            }}
          />
        </Field>

        <Field
          label="API key"
          hint={
            envCandidates.length > 0
              ? `Leave blank to read it from the environment instead. Set here: ${envCandidates.join(", ")}.`
              : "Stored in Mira's database, as sensitive as the environment variable it replaces."
          }
        >
          <Input
            type="password"
            autoComplete="off"
            value={apiKey}
            placeholder={keyPlaceholder}
            disabled={clearKey}
            onChange={(e) => {
              setApiKey(e.target.value)
              setTested(null)
            }}
          />
          {editing?.key_source === "stored" && (
            <label className="flex items-center gap-2 text-xs text-muted-foreground">
              <input
                type="checkbox"
                className="size-3.5 accent-current"
                checked={clearKey}
                onChange={(e) => {
                  setClearKey(e.target.checked)
                  if (e.target.checked) setApiKey("")
                  setTested(null)
                }}
              />
              Remove the stored key {editing.key_hint} on save
            </label>
          )}
        </Field>

        <Field
          label="…or the environment variable holding it"
          hint="Optional. Use this to keep the key out of the database — Mira reads the variable at call time."
        >
          <Input
            value={apiKeyEnv}
            placeholder={chosen?.api_key_env || "MY_PROVIDER_API_KEY"}
            onChange={(e) => {
              setApiKeyEnv(e.target.value)
              setTested(null)
            }}
          />
        </Field>

        <Field
          label="Protocol"
          hint="Chat Completions works everywhere. Pick Responses only for an endpoint that exposes /responses."
        >
          <Select value={apiStyle} onValueChange={setApiStyle}>
            <SelectTrigger>
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              {PROTOCOLS.map((p) => (
                <SelectItem key={p.value} value={p.value}>
                  {p.label}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
        </Field>

        {tested && (
          <p
            className={`flex items-start gap-2 text-xs ${tested.ok ? "text-muted-foreground" : "text-destructive"}`}
          >
            {tested.ok ? (
              <CheckCircle2 className="mt-0.5 size-3.5 shrink-0" />
            ) : (
              <TriangleAlert className="mt-0.5 size-3.5 shrink-0" />
            )}
            {tested.detail}
          </p>
        )}
        {error && (
          <p className="flex items-start gap-2 text-xs text-destructive">
            <TriangleAlert className="mt-0.5 size-3.5 shrink-0" />
            {error}
          </p>
        )}
      </div>

      <DialogFooter className="gap-2 sm:justify-between">
        <Button
          variant="outline"
          disabled={testing || !baseUrl}
          onClick={runTest}
          title="Ask this endpoint for its model list — no tokens are spent"
        >
          {testing && <Loader2 className="mr-2 h-3 w-3 animate-spin" />}
          Test connection
        </Button>
        <div className="flex gap-2">
          <Button variant="ghost" onClick={onClose}>
            Cancel
          </Button>
          <Button onClick={save} disabled={saving || !baseUrl || !label}>
            {saving && <Loader2 className="mr-2 h-3 w-3 animate-spin" />}
            {editing ? "Save" : "Add endpoint"}
          </Button>
        </div>
      </DialogFooter>
    </>
  )
}
