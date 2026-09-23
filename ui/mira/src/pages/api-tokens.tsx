import { Check, Copy, KeyRound, Plus, Trash2 } from "lucide-react"
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
import { ConfirmButton } from "@/components/ui/confirm-button"
import { Input } from "@/components/ui/input"
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select"
import { toast } from "@/components/ui/sonner"
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table"
import { api, type ApiToken, type CreatedApiToken } from "@/lib/api"
import { useAuth } from "@/lib/auth"
import { useAsync, useDocumentTitle } from "@/lib/hooks"

const EXPIRY_OPTIONS = [
  { value: "30", label: "30 days" },
  { value: "90", label: "90 days" },
  { value: "365", label: "1 year" },
  { value: "0", label: "Never" },
]

// Epoch seconds → a relative time for the past, a date for the future.
function when(ts: number, never: string): string {
  if (!ts) return never
  const diff = Date.now() / 1000 - ts
  if (diff < 0) return new Date(ts * 1000).toLocaleDateString()
  if (diff < 60) return "Just now"
  const mins = Math.floor(diff / 60)
  if (mins < 60) return `${mins}m ago`
  const hrs = Math.floor(mins / 60)
  if (hrs < 24) return `${hrs}h ago`
  const days = Math.floor(hrs / 24)
  if (days < 30) return `${days}d ago`
  return new Date(ts * 1000).toLocaleDateString()
}

function status(token: ApiToken): "active" | "revoked" | "expired" {
  if (token.revoked_at) return "revoked"
  if (token.expires_at && token.expires_at < Date.now() / 1000) return "expired"
  return "active"
}

function CopyLine({ label, value }: { label: string; value: string }) {
  const [copied, setCopied] = useState(false)
  const copy = async () => {
    try {
      await navigator.clipboard.writeText(value)
      setCopied(true)
      setTimeout(() => setCopied(false), 1500)
    } catch {
      toast.error("The browser refused clipboard access")
    }
  }
  return (
    <div className="space-y-1">
      <p className="text-xs font-medium text-muted-foreground">{label}</p>
      <div className="flex items-start gap-2">
        <pre className="min-w-0 flex-1 overflow-x-auto rounded-md bg-muted px-3 py-2 font-mono text-xs break-all whitespace-pre-wrap">
          {value}
        </pre>
        <Button variant="outline" size="icon-sm" onClick={copy}>
          {copied ? (
            <Check className="h-3.5 w-3.5" />
          ) : (
            <Copy className="h-3.5 w-3.5" />
          )}
        </Button>
      </div>
    </div>
  )
}

// Shown once, right after a token is created. The server keeps only a digest,
// so closing this card is the last chance to copy it.
function CreatedTokenCard({
  created,
  onDismiss,
}: {
  created: CreatedApiToken
  onDismiss: () => void
}) {
  const origin = window.location.origin
  return (
    <Card className="border-emerald-500/40">
      <CardHeader>
        <CardTitle className="text-base">
          Token “{created.name}” created
        </CardTitle>
        <CardDescription>
          Copy it now — it is not stored and will not be shown again.
        </CardDescription>
      </CardHeader>
      <CardContent className="space-y-4">
        <CopyLine label="Token" value={created.token} />
        <CopyLine
          label="Claude Code (MCP over HTTP)"
          value={`claude mcp add --transport http mira ${origin}/mcp --header "Authorization: Bearer ${created.token}"`}
        />
        <CopyLine
          label="REST API"
          value={`curl -H "Authorization: Bearer ${created.token}" ${origin}/api/auth/me`}
        />
        <div className="flex justify-end">
          <Button variant="outline" size="sm" onClick={onDismiss}>
            I&apos;ve copied it
          </Button>
        </div>
      </CardContent>
    </Card>
  )
}

export function ApiTokensPage() {
  useDocumentTitle("API tokens")
  const { user } = useAuth()
  const isAdmin = !!user?.is_admin
  const [refreshKey, setRefreshKey] = useState(0)
  const [name, setName] = useState("")
  const [expiry, setExpiry] = useState("90")
  const [ownerId, setOwnerId] = useState<string>("")
  const [creating, setCreating] = useState(false)
  const [created, setCreated] = useState<CreatedApiToken | null>(null)
  const [error, setError] = useState<string | null>(null)

  const {
    data: tokens,
    loading,
    error: loadError,
  } = useAsync(() => api.listApiTokens(isAdmin), [refreshKey, isAdmin])
  const { data: users } = useAsync(
    () => (isAdmin ? api.listUsers() : Promise.resolve([])),
    [isAdmin]
  )

  const create = async () => {
    setError(null)
    setCreating(true)
    try {
      const token = await api.createApiToken({
        name: name.trim(),
        expires_in_days: Number(expiry),
        ...(ownerId ? { user_id: Number(ownerId) } : {}),
      })
      setCreated(token)
      setName("")
      setRefreshKey((k) => k + 1)
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setCreating(false)
    }
  }

  const revoke = async (id: number) => {
    try {
      await api.revokeApiToken(id)
      toast.success("Token revoked")
      setRefreshKey((k) => k + 1)
    } catch (e) {
      toast.error(e instanceof Error ? e.message : String(e))
    }
  }

  return (
    <div className="space-y-6 p-6">
      <div>
        <h1 className="text-2xl font-semibold tracking-tight">API tokens</h1>
        <p className="max-w-3xl text-sm text-muted-foreground">
          Read-only credentials for agents and scripts. A token reads what its
          user can read in this dashboard — through the REST API or the MCP
          endpoint at <code className="font-mono">/mcp</code> — and cannot
          change anything. An admin&apos;s token also reaches the log trail;
          give an agent a dedicated non-admin user unless it needs that.
        </p>
      </div>

      {created && (
        <CreatedTokenCard
          created={created}
          onDismiss={() => setCreated(null)}
        />
      )}

      <Card>
        <CardHeader>
          <CardTitle className="text-base">New token</CardTitle>
        </CardHeader>
        <CardContent>
          <form
            className="flex flex-col gap-3 md:flex-row md:items-end"
            onSubmit={(e) => {
              e.preventDefault()
              if (name.trim()) void create()
            }}
          >
            <div className="flex-1 space-y-1">
              <label htmlFor="token-name" className="text-xs font-medium">
                Name
              </label>
              <Input
                id="token-name"
                placeholder="What it is for, e.g. claude-code"
                value={name}
                maxLength={80}
                onChange={(e) => setName(e.target.value)}
              />
            </div>
            <div className="space-y-1">
              <span className="text-xs font-medium">Expires</span>
              <Select value={expiry} onValueChange={setExpiry}>
                <SelectTrigger className="md:w-36">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  {EXPIRY_OPTIONS.map((o) => (
                    <SelectItem key={o.value} value={o.value}>
                      {o.label}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </div>
            {isAdmin && users && users.length > 1 && (
              <div className="space-y-1">
                <span className="text-xs font-medium">Acts as</span>
                <Select
                  value={ownerId || String(user?.id ?? "")}
                  onValueChange={(v) =>
                    setOwnerId(v === String(user?.id) ? "" : v)
                  }
                >
                  <SelectTrigger className="md:w-44">
                    <SelectValue />
                  </SelectTrigger>
                  <SelectContent>
                    {users.map((u) => (
                      <SelectItem key={u.id} value={String(u.id)}>
                        {u.username}
                        {u.is_admin ? " (admin)" : ""}
                      </SelectItem>
                    ))}
                  </SelectContent>
                </Select>
              </div>
            )}
            <Button type="submit" disabled={creating || !name.trim()}>
              <Plus className="mr-1 h-4 w-4" /> Create token
            </Button>
          </form>
          {error && (
            <p className="mt-3 text-sm break-words text-destructive">{error}</p>
          )}
        </CardContent>
      </Card>

      {loading ? (
        <div className="text-sm text-muted-foreground">Loading…</div>
      ) : loadError ? (
        // Not the empty state: a list that failed to load must not read as a
        // list with nothing in it, or a live token looks like it is gone.
        <p className="text-sm break-words text-destructive">
          Could not load tokens: {loadError}
        </p>
      ) : !tokens || tokens.length === 0 ? (
        <Card>
          <CardContent className="flex flex-col items-center justify-center gap-3 py-12 text-center">
            <div className="flex size-12 items-center justify-center rounded-full bg-muted">
              <KeyRound className="size-6 text-muted-foreground" />
            </div>
            <p className="text-sm text-muted-foreground">No tokens yet.</p>
          </CardContent>
        </Card>
      ) : (
        <Card className="overflow-hidden py-0">
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>Name</TableHead>
                {isAdmin && <TableHead>User</TableHead>}
                <TableHead>Token</TableHead>
                <TableHead>Last used</TableHead>
                <TableHead>Expires</TableHead>
                <TableHead>Status</TableHead>
                <TableHead className="text-right">Actions</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {tokens.map((t) => {
                const s = status(t)
                return (
                  <TableRow
                    key={t.id}
                    className={s === "active" ? "" : "opacity-60"}
                  >
                    <TableCell className="font-medium">{t.name}</TableCell>
                    {isAdmin && (
                      <TableCell className="text-sm">{t.username}</TableCell>
                    )}
                    <TableCell className="font-mono text-xs text-muted-foreground">
                      {t.prefix}…
                    </TableCell>
                    <TableCell className="text-sm text-muted-foreground">
                      {when(t.last_used_at, "Never")}
                    </TableCell>
                    <TableCell className="text-sm text-muted-foreground">
                      {when(t.expires_at, "Never")}
                    </TableCell>
                    <TableCell>
                      <Badge
                        variant="secondary"
                        className={
                          s === "active"
                            ? "ring-1 ring-border"
                            : "bg-transparent ring-1 ring-border"
                        }
                      >
                        {s === "active"
                          ? "Active"
                          : s === "revoked"
                            ? "Revoked"
                            : "Expired"}
                      </Badge>
                    </TableCell>
                    <TableCell className="text-right">
                      {s === "active" && (
                        <ConfirmButton
                          variant="ghost"
                          size="icon-sm"
                          tooltip="Revoke"
                          dialogTitle="Revoke token?"
                          dialogDescription={`Anything using "${t.name}" stops working on its next request. This can't be undone.`}
                          confirmLabel="Revoke"
                          destructive
                          onConfirm={() => revoke(t.id)}
                        >
                          <Trash2 className="h-3.5 w-3.5 text-destructive" />
                        </ConfirmButton>
                      )}
                    </TableCell>
                  </TableRow>
                )
              })}
            </TableBody>
          </Table>
        </Card>
      )}
    </div>
  )
}
