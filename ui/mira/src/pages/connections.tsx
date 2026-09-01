import {
  CheckCircle2,
  ExternalLink,
  Loader2,
  Plug,
  RefreshCw,
} from "lucide-react"
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
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog"
import { Input } from "@/components/ui/input"
import { toast } from "@/components/ui/sonner"
import { api } from "@/lib/api"
import type { OAuthProvider, OAuthStart } from "@/lib/api/oauth"
import { useAuth } from "@/lib/auth"
import { useAsync, useDocumentTitle } from "@/lib/hooks"

function expiryLabel(expiresAt: number): string {
  if (!expiresAt) return "no expiry"
  const minutes = Math.round((expiresAt * 1000 - Date.now()) / 60000)
  if (minutes <= 0) return "expired — will renew on next review"
  if (minutes < 60) return `renews in ${minutes} min`
  return `renews in ${Math.round(minutes / 60)} h`
}

export function ConnectionsPage() {
  useDocumentTitle("Connections")
  const { user } = useAuth()
  const [refreshKey, setRefreshKey] = useState(0)
  const [busy, setBusy] = useState("")
  // The in-flight sign-in, if any. Held here rather than in the dialog so the
  // `state` from `start` survives until the user comes back with the redirect.
  const [flow, setFlow] = useState<OAuthStart | null>(null)
  const [redirectUrl, setRedirectUrl] = useState("")
  const [finishing, setFinishing] = useState(false)

  const { data, loading } = useAsync(
    () =>
      user?.is_admin
        ? api.getOAuthProviders()
        : Promise.resolve({ active_provider: "", providers: [] }),
    [user, refreshKey]
  )
  const providers: OAuthProvider[] = data?.providers ?? []
  const active = data?.active_provider ?? ""
  const reload = () => setRefreshKey((k) => k + 1)

  if (!user?.is_admin) {
    return (
      <div className="p-6 text-sm text-muted-foreground">
        Admin access required.
      </div>
    )
  }

  const connect = async (provider: string) => {
    setBusy(provider)
    try {
      const started = await api.startOAuth(provider)
      setRedirectUrl("")
      setFlow(started)
      window.open(started.authorization_url, "_blank", "noopener")
    } catch (e) {
      toast.error(e instanceof Error ? e.message : String(e))
    } finally {
      setBusy("")
    }
  }

  const finish = async () => {
    if (!flow) return
    setFinishing(true)
    try {
      await api.completeOAuth(flow.provider, redirectUrl, flow.state)
      setFlow(null)
      reload()
      toast.success("Connected")
    } catch (e) {
      toast.error(e instanceof Error ? e.message : String(e))
    } finally {
      setFinishing(false)
    }
  }

  const act = async (provider: string, fn: () => Promise<unknown>) => {
    setBusy(provider)
    try {
      await fn()
      reload()
    } catch (e) {
      toast.error(e instanceof Error ? e.message : String(e))
    } finally {
      setBusy("")
    }
  }

  return (
    <div className="space-y-6 p-6">
      <div>
        <h1 className="text-2xl font-semibold tracking-tight">Connections</h1>
        <p className="text-sm text-muted-foreground">
          Sign in to a provider once and review with the models that account
          already includes, instead of managing an API key. The session is
          stored on this server and renewed automatically.
        </p>
      </div>

      {loading ? (
        <div className="flex items-center gap-2 text-sm text-muted-foreground">
          <Loader2 className="h-4 w-4 animate-spin" /> Loading…
        </div>
      ) : (
        <div className="space-y-4">
          {providers.map((p) => {
            const isActive = active === p.id
            const working = busy === p.id
            return (
              <Card key={p.id}>
                <CardHeader>
                  <div className="flex items-start justify-between gap-4">
                    <div className="space-y-1">
                      <CardTitle className="flex items-center gap-2">
                        {p.label}
                        {isActive && (
                          <Badge variant="default">Serving reviews</Badge>
                        )}
                        {p.connected && !isActive && (
                          <Badge variant="secondary">Connected</Badge>
                        )}
                      </CardTitle>
                      <CardDescription>{p.description}</CardDescription>
                    </div>
                    {p.docs_url && (
                      <a
                        className="flex shrink-0 items-center gap-1 text-xs text-muted-foreground hover:text-foreground"
                        href={p.docs_url}
                        target="_blank"
                        rel="noreferrer"
                      >
                        Docs <ExternalLink className="size-3" />
                      </a>
                    )}
                  </div>
                </CardHeader>
                <CardContent className="space-y-4">
                  {p.connected ? (
                    <div className="flex items-center gap-2 text-sm">
                      <CheckCircle2 className="size-4 text-muted-foreground" />
                      <span>
                        {p.account_label || "Signed in"}
                        {p.plan && (
                          <span className="text-muted-foreground">
                            {" "}
                            · {p.plan}
                          </span>
                        )}
                      </span>
                      <span className="text-xs text-muted-foreground">
                        ({expiryLabel(p.expires_at)})
                      </span>
                    </div>
                  ) : (
                    <p className="text-sm text-muted-foreground">
                      Not connected.
                    </p>
                  )}

                  <div className="flex flex-wrap items-center gap-2">
                    <Button
                      size="sm"
                      variant={p.connected ? "outline" : "default"}
                      disabled={working}
                      onClick={() => connect(p.id)}
                    >
                      {working ? (
                        <Loader2 className="mr-1 h-3 w-3 animate-spin" />
                      ) : (
                        <Plug className="mr-1 h-3 w-3" />
                      )}
                      {p.connected ? "Reconnect" : "Connect"}
                    </Button>

                    {p.connected && p.serves_models && !isActive && (
                      <Button
                        size="sm"
                        disabled={working}
                        onClick={() =>
                          act(p.id, () => api.setActiveOAuth(p.id))
                        }
                      >
                        Use for reviews
                      </Button>
                    )}
                    {isActive && (
                      <Button
                        size="sm"
                        variant="outline"
                        disabled={working}
                        onClick={() => act(p.id, () => api.setActiveOAuth(""))}
                      >
                        Stop using
                      </Button>
                    )}
                    {p.connected && p.can_refresh && (
                      <Button
                        size="sm"
                        variant="ghost"
                        disabled={working}
                        onClick={() =>
                          act(p.id, () => api.refreshOAuth(p.id))
                        }
                      >
                        <RefreshCw className="mr-1 h-3 w-3" /> Refresh session
                      </Button>
                    )}
                    {p.connected && (
                      <ConfirmButton
                        size="sm"
                        variant="ghost"
                        destructive
                        disabled={working}
                        dialogTitle={`Disconnect ${p.label}?`}
                        dialogDescription={
                          isActive
                            ? "This session is serving reviews. Disconnecting it sends reviews back to the configured API key."
                            : "Mira will forget this session. You can sign in again at any time."
                        }
                        confirmLabel="Disconnect"
                        onConfirm={() =>
                          act(p.id, () => api.disconnectOAuth(p.id))
                        }
                      >
                        Disconnect
                      </ConfirmButton>
                    )}
                  </div>

                  {isActive && (
                    <p className="text-xs text-muted-foreground">
                      Pick which of this account's models to use under{" "}
                      <a className="underline" href="/settings/models">
                        Settings → Models
                      </a>
                      .
                    </p>
                  )}
                </CardContent>
              </Card>
            )
          })}
        </div>
      )}

      <Dialog open={flow !== null} onOpenChange={(o) => !o && setFlow(null)}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Finish signing in to {flow?.label}</DialogTitle>
            <DialogDescription>
              {flow?.manual_exchange
                ? "Approve the request in the tab that just opened. It will send you to a localhost address that cannot load — that is expected. Copy that URL from the address bar and paste it below."
                : "Approve the request in the tab that just opened. This page updates once you are back."}
            </DialogDescription>
          </DialogHeader>

          {flow?.manual_exchange && (
            <div className="space-y-3">
              <a
                className="flex items-center gap-1 text-xs text-muted-foreground hover:text-foreground"
                href={flow.authorization_url}
                target="_blank"
                rel="noreferrer"
              >
                Open the sign-in page again <ExternalLink className="size-3" />
              </a>
              <Input
                autoFocus
                placeholder={`${flow.redirect_uri}?code=…`}
                value={redirectUrl}
                onChange={(e) => setRedirectUrl(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === "Enter" && redirectUrl.trim()) finish()
                }}
              />
            </div>
          )}

          <DialogFooter>
            <Button variant="outline" onClick={() => setFlow(null)}>
              Cancel
            </Button>
            {flow?.manual_exchange ? (
              <Button
                onClick={finish}
                disabled={finishing || !redirectUrl.trim()}
              >
                {finishing && <Loader2 className="mr-2 h-3 w-3 animate-spin" />}
                Finish sign-in
              </Button>
            ) : (
              <Button
                onClick={() => {
                  setFlow(null)
                  reload()
                }}
              >
                Done
              </Button>
            )}
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  )
}
