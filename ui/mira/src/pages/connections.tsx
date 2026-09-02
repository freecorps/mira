import {
  CheckCircle2,
  ExternalLink,
  Gauge,
  Loader2,
  Plug,
  RefreshCw,
  Repeat,
  TriangleAlert,
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
import type {
  OAuthAccount,
  OAuthProvider,
  OAuthStart,
  UsageWindow,
} from "@/lib/api/oauth"
import { useAuth } from "@/lib/auth"
import { useAsync, useDocumentTitle } from "@/lib/hooks"

function expiryLabel(expiresAt: number): string {
  if (!expiresAt) return "no expiry"
  const minutes = Math.round((expiresAt * 1000 - Date.now()) / 60000)
  if (minutes <= 0) return "expired — renews on next call"
  if (minutes < 60) return `session renews in ${minutes} min`
  return `session renews in ${Math.round(minutes / 60)} h`
}

function untilLabel(at: number | null | undefined): string {
  if (!at) return ""
  const seconds = Math.round(at - Date.now() / 1000)
  if (seconds <= 0) return "now"
  if (seconds < 3600) return `${Math.max(1, Math.round(seconds / 60))} min`
  if (seconds < 86400) {
    const h = Math.floor(seconds / 3600)
    const m = Math.round((seconds % 3600) / 60)
    return m ? `${h} h ${m} min` : `${h} h`
  }
  const d = Math.floor(seconds / 86400)
  const h = Math.round((seconds % 86400) / 3600)
  return h ? `${d} d ${h} h` : `${d} d`
}

function agoLabel(at: number): string {
  if (!at) return ""
  const seconds = Math.round(Date.now() / 1000 - at)
  if (seconds < 90) return "just now"
  if (seconds < 3600) return `${Math.round(seconds / 60)} min ago`
  if (seconds < 86400) return `${Math.round(seconds / 3600)} h ago`
  return `${Math.round(seconds / 86400)} d ago`
}

function endpointHost(url: string): string {
  try {
    const u = new URL(url)
    return `${u.host}${u.pathname}`.replace(/\/$/, "")
  } catch {
    return url
  }
}

// One metered window as a labelled bar: "5-hour · 42% used · resets in 2 h".
function UsageMeter({ window }: { window: UsageWindow }) {
  const used = Math.max(0, Math.min(100, window.used_percent))
  const tone =
    used >= 95 ? "bg-destructive" : used >= 75 ? "bg-amber-500" : "bg-primary"
  const resets = untilLabel(window.resets_at)
  return (
    <div className="space-y-1">
      <div className="flex items-center justify-between text-xs">
        <span className="font-medium capitalize">{window.name}</span>
        <span className="text-muted-foreground">
          {used.toFixed(0)}% used
          {resets && <> · resets in {resets}</>}
        </span>
      </div>
      <div
        className="h-1.5 w-full overflow-hidden rounded-full bg-muted"
        role="progressbar"
        aria-valuenow={used}
        aria-valuemin={0}
        aria-valuemax={100}
        aria-label={`${window.name} window`}
      >
        <div className={`h-full ${tone}`} style={{ width: `${used}%` }} />
      </div>
    </div>
  )
}

function AccountRow({
  provider,
  account,
  working,
  onAct,
}: {
  provider: OAuthProvider
  account: OAuthAccount
  working: boolean
  onAct: (fn: () => Promise<unknown>, done?: string) => void
}) {
  const usage = account.usage
  // `available` is the server's judgement, so this render stays pure.
  const limited = !!usage && !account.available && usage.exhausted_until > 0
  const windows = [usage?.primary, usage?.secondary].filter(
    (w): w is UsageWindow => !!w
  )
  return (
    <div className="space-y-3 rounded-md border p-3">
      <div className="flex flex-wrap items-start justify-between gap-2">
        <div className="space-y-1">
          <div className="flex flex-wrap items-center gap-2 text-sm">
            <CheckCircle2 className="size-4 text-muted-foreground" />
            <span className="font-medium">
              {account.account_label || "Signed in"}
            </span>
            {account.plan && (
              <Badge variant="outline" className="capitalize">
                {account.plan}
              </Badge>
            )}
            {account.is_default && (
              <Badge variant="default">Default for bare model ids</Badge>
            )}
            {limited && (
              <Badge variant="destructive">
                <TriangleAlert /> rate-limited ·{" "}
                {untilLabel(usage!.exhausted_until)}
              </Badge>
            )}
          </div>
          <div className="text-xs text-muted-foreground">
            {expiryLabel(account.expires_at)} · account key{" "}
            <code className="font-mono">{account.key}</code>
          </div>
        </div>
        {provider.reports_usage && (
          <Button
            size="sm"
            variant="ghost"
            disabled={working}
            title="Ask the provider where this account's allowance stands"
            onClick={() =>
              onAct(
                () => api.refreshOAuthUsage(provider.id, account.key),
                "Usage updated"
              )
            }
          >
            <Gauge className="mr-1 h-3 w-3" /> Refresh usage
          </Button>
        )}
      </div>

      {provider.reports_usage && (
        <div className="space-y-2">
          {windows.length > 0 ? (
            <div className="grid gap-3 sm:grid-cols-2">
              {windows.map((w) => (
                <UsageMeter key={w.name} window={w} />
              ))}
            </div>
          ) : (
            <p className="text-xs text-muted-foreground">
              No usage recorded yet — it is read off every review call, or press{" "}
              <em>Refresh usage</em> to ask now.
            </p>
          )}
          {usage?.credits && (
            <p className="text-xs text-muted-foreground">
              Credits:{" "}
              {usage.credits.unlimited
                ? "unlimited"
                : usage.credits.has_credits
                  ? (usage.credits.balance ?? "available")
                  : "none"}
            </p>
          )}
          {usage && usage.fetched_at > 0 && (
            <p className="text-[0.7rem] text-muted-foreground">
              As of {agoLabel(usage.fetched_at)} · from{" "}
              {usage.source === "endpoint"
                ? "the usage endpoint"
                : "the last response's headers"}
            </p>
          )}
        </div>
      )}

      <div className="flex flex-wrap items-center gap-2">
        {provider.serves_models && !account.is_default && (
          <Button
            size="sm"
            variant="outline"
            disabled={working}
            title="Send bare model ids (ones that do not name a backend) to this account only"
            onClick={() =>
              onAct(
                () => api.setActiveOAuth(provider.id, account.key),
                `Bare model ids now go to ${account.account_label || account.key}`
              )
            }
          >
            Use this account
          </Button>
        )}
        {account.can_refresh && (
          <Button
            size="sm"
            variant="ghost"
            disabled={working}
            onClick={() =>
              onAct(
                () => api.refreshOAuthAccount(provider.id, account.key),
                "Session renewed"
              )
            }
          >
            <RefreshCw className="mr-1 h-3 w-3" /> Refresh session
          </Button>
        )}
        <ConfirmButton
          size="sm"
          variant="ghost"
          destructive
          disabled={working}
          dialogTitle={`Disconnect ${account.account_label || account.key}?`}
          dialogDescription={
            account.is_default
              ? "Bare model ids go to this account. Disconnecting it sends them back to the configured API key (or to the provider's other accounts, if you pick one)."
              : "Mira will forget this session. You can sign in again at any time."
          }
          confirmLabel="Disconnect"
          onConfirm={() =>
            onAct(
              () => api.disconnectOAuthAccount(provider.id, account.key),
              "Disconnected"
            )
          }
        >
          Disconnect
        </ConfirmButton>
      </div>
    </div>
  )
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
        : Promise.resolve({
            active_provider: "",
            active_account: "",
            active_ref: "",
            providers: [],
          }),
    [user, refreshKey]
  )
  const providers: OAuthProvider[] = data?.providers ?? []
  const activeProvider = data?.active_provider ?? ""
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
      const done = await api.completeOAuth(
        flow.provider,
        redirectUrl,
        flow.state
      )
      setFlow(null)
      reload()
      toast.success(
        `Connected ${done.account_label || "account"}${done.plan ? ` (${done.plan})` : ""}`
      )
    } catch (e) {
      toast.error(e instanceof Error ? e.message : String(e))
    } finally {
      setFinishing(false)
    }
  }

  const act = async (
    provider: string,
    fn: () => Promise<unknown>,
    done?: string
  ) => {
    setBusy(provider)
    try {
      await fn()
      reload()
      if (done) toast.success(done)
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
          Sign in to a provider and review with the models those accounts
          already include, instead of managing an API key. Connect as many
          accounts of a provider as you like: each shows its own allowance, and
          reviews can rotate across them. Sessions are stored on this server and
          renewed automatically.
        </p>
      </div>

      {loading ? (
        <div className="flex items-center gap-2 text-sm text-muted-foreground">
          <Loader2 className="h-4 w-4 animate-spin" /> Loading…
        </div>
      ) : (
        <div className="space-y-4">
          {providers.map((p) => {
            const isDefault = activeProvider === p.id
            const working = busy === p.id
            const many = p.accounts.length > 1
            return (
              <Card key={p.id}>
                <CardHeader>
                  <div className="flex items-start justify-between gap-4">
                    <div className="space-y-1">
                      <CardTitle className="flex flex-wrap items-center gap-2">
                        {p.label}
                        {isDefault && p.default_mode === "rotate" && (
                          <Badge variant="default">
                            <Repeat /> Default · rotating across{" "}
                            {p.accounts.length}
                          </Badge>
                        )}
                        {isDefault && p.default_mode === "pinned" && (
                          <Badge variant="default">Default · one account</Badge>
                        )}
                        {p.connected && !isDefault && (
                          <Badge variant="secondary">
                            {p.accounts.length} connected
                          </Badge>
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
                  {p.protocol && (
                    <div className="flex flex-wrap items-center gap-1.5 pt-1 text-xs text-muted-foreground">
                      <span>Calls use</span>
                      <Badge variant="outline">{p.protocol.protocol}</Badge>
                      <Badge variant="outline">{p.protocol.transport}</Badge>
                      <Badge variant="outline" className="font-mono">
                        {endpointHost(p.protocol.endpoint)}
                      </Badge>
                    </div>
                  )}
                </CardHeader>
                <CardContent className="space-y-4">
                  {p.connected ? (
                    <div className="space-y-3">
                      {p.accounts.map((a) => (
                        <AccountRow
                          key={a.key}
                          provider={p}
                          account={a}
                          working={working}
                          onAct={(fn, done) => act(p.id, fn, done)}
                        />
                      ))}
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
                      {p.connected ? "Add another account" : "Connect"}
                    </Button>

                    {p.connected &&
                      p.serves_models &&
                      (!isDefault || p.default_mode === "pinned") && (
                        <Button
                          size="sm"
                          disabled={working}
                          title={
                            many
                              ? "Each call goes to the account with the most allowance left; one that is rate-limited is skipped until its window resets"
                              : "Send bare model ids to this provider"
                          }
                          onClick={() =>
                            act(
                              p.id,
                              () => api.setActiveOAuth(p.id, "*"),
                              many
                                ? `Rotating across ${p.accounts.length} ${p.label} accounts`
                                : `Bare model ids now go to ${p.label}`
                            )
                          }
                        >
                          {many ? (
                            <>
                              <Repeat className="mr-1 h-3 w-3" /> Rotate across
                              accounts
                            </>
                          ) : (
                            "Use for reviews"
                          )}
                        </Button>
                      )}
                    {isDefault && (
                      <Button
                        size="sm"
                        variant="outline"
                        disabled={working}
                        onClick={() =>
                          act(
                            p.id,
                            () => api.setActiveOAuth("", ""),
                            "Bare model ids now use the API key"
                          )
                        }
                      >
                        Stop using
                      </Button>
                    )}
                  </div>

                  {p.connected && p.serves_models && (
                    <p className="text-xs text-muted-foreground">
                      {isDefault
                        ? "A model picked without a backend goes here. "
                        : "Pick this provider per purpose, or make it the default here. "}
                      Under{" "}
                      <a className="underline" href="/settings/models">
                        Settings → Models
                      </a>{" "}
                      every account is listed as its own section, so the
                      indexing, review and security models can each name a
                      specific account, the whole provider (rotating), or the
                      API key.
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
                ? "Approve the request in the tab that just opened. It will send you to a localhost address that cannot load — that is expected. Copy that URL from the address bar and paste it below. Signing in with an account that is already connected replaces its session; any other account is added alongside."
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
