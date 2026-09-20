import { CheckCircle2, Loader2, Plus } from "lucide-react"
import { useEffect, useState } from "react"
import { useNavigate } from "react-router"

import { EndpointDialog } from "@/components/endpoint-dialog"
import { ModelCombobox, type ModelOption } from "@/components/model-combobox"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card"
import { api } from "@/lib/api"
import type { EndpointPreset, ProviderEndpoint } from "@/lib/api/providers"
import { useDocumentTitle } from "@/lib/hooks"

export function SetupPage() {
  useDocumentTitle("Setup")
  const navigate = useNavigate()
  const [loading, setLoading] = useState(true)
  const [saving, setSaving] = useState(false)
  // Two steps: where calls go, then which models. The first is skipped when
  // something can already serve a review — a key in the environment, or an
  // account signed in — which is every upgrade of an install that worked.
  const [step, setStep] = useState<"provider" | "models">("models")
  const [presets, setPresets] = useState<EndpointPreset[]>([])
  const [envCandidates, setEnvCandidates] = useState<string[]>([])
  const [endpoints, setEndpoints] = useState<ProviderEndpoint[]>([])
  const [dialogOpen, setDialogOpen] = useState(false)
  // "" = inherit from deployment config — an untouched save must not
  // convert mira.yaml-sourced models into dashboard overrides.
  const [indexingModel, setIndexingModel] = useState("")
  const [reviewModel, setReviewModel] = useState("")
  const [configIndexingModel, setConfigIndexingModel] = useState("")
  const [configReviewModel, setConfigReviewModel] = useState("")
  const [indexingOptions, setIndexingOptions] = useState<ModelOption[]>([])
  const [reviewOptions, setReviewOptions] = useState<ModelOption[]>([])

  const loadModels = () =>
    api.getModels().then((data) => {
      setIndexingModel(
        data.indexing_source === "config" ? "" : data.indexing_model
      )
      setReviewModel(data.review_source === "config" ? "" : data.review_model)
      setConfigIndexingModel(data.config_indexing_model)
      setConfigReviewModel(data.config_review_model)
      setIndexingOptions(data.indexing_options)
      setReviewOptions(data.review_options)
    })

  useEffect(() => {
    let cancelled = false
    const load = async () => {
      const providers = await api.getProviders().catch(() => null)
      if (cancelled) return
      if (providers) {
        setPresets(providers.presets)
        setEnvCandidates(providers.env_candidates)
        setEndpoints(providers.endpoints.filter((e) => e.key_configured))
        if (!providers.configured) setStep("provider")
      }
      await loadModels().catch(() => undefined)
      if (!cancelled) setLoading(false)
    }
    void load()
    return () => {
      cancelled = true
    }
  }, [])

  const handleSave = async () => {
    setSaving(true)
    await api.saveModels(indexingModel, reviewModel, "")
    navigate("/")
  }

  if (loading) {
    return (
      <div className="flex min-h-screen items-center justify-center">
        <Loader2 className="h-5 w-5 animate-spin text-muted-foreground" />
      </div>
    )
  }

  const header = (subtitle: string) => (
    <div className="text-center">
      <img
        src="/logo.png"
        alt="Mira"
        className="mx-auto mb-4 hidden h-12 w-12 dark:block"
      />
      <img
        src="/logo-light.png"
        alt="Mira"
        className="mx-auto mb-4 h-12 w-12 dark:hidden"
      />
      <h1 className="text-2xl font-semibold tracking-tight">Welcome to Mira</h1>
      <p className="mt-1 text-sm text-muted-foreground">{subtitle}</p>
    </div>
  )

  if (step === "provider") {
    return (
      <div className="mx-auto max-w-lg space-y-6 px-4 py-16">
        {header("First, tell Mira which model provider to review with")}

        <Card>
          <CardHeader className="pb-3">
            <CardTitle className="text-base">Model provider</CardTitle>
            <CardDescription>
              Reviews need somewhere to send their calls. Add an endpoint with
              its URL and API key — start from a preset for a provider Mira
              knows, or point it at any OpenAI-compatible URL. It is stored
              here, so nothing on the server has to change.
            </CardDescription>
          </CardHeader>
          <CardContent className="space-y-3">
            {endpoints.map((e) => (
              <div
                key={e.id}
                className="flex items-center gap-2 rounded-md border p-3 text-sm"
              >
                <CheckCircle2 className="size-4 text-muted-foreground" />
                <span className="font-medium">{e.label}</span>
                <Badge variant="outline" className="font-mono text-xs">
                  {e.endpoint}
                </Badge>
              </div>
            ))}
            <Button
              variant={endpoints.length ? "outline" : "default"}
              className="w-full"
              onClick={() => setDialogOpen(true)}
            >
              <Plus className="mr-1 h-4 w-4" />
              {endpoints.length ? "Add another endpoint" : "Add an endpoint"}
            </Button>
          </CardContent>
        </Card>

        <div className="flex items-center justify-between">
          <button
            className="text-xs text-muted-foreground underline"
            onClick={() => setStep("models")}
          >
            Skip — I set the key in mira.yaml or the environment
          </button>
          <Button
            disabled={endpoints.length === 0}
            onClick={() => {
              void loadModels()
              setStep("models")
            }}
          >
            Continue
          </Button>
        </div>

        <p className="text-center text-xs text-muted-foreground">
          You can add more, or sign in to a ChatGPT account instead, under
          Settings → Connections.
        </p>

        <EndpointDialog
          open={dialogOpen}
          onOpenChange={setDialogOpen}
          presets={presets}
          envCandidates={envCandidates}
          // The first one added is what reviews will use.
          makeDefault={endpoints.length === 0}
          onSaved={(saved) => {
            setEndpoints((prev) => [...prev.filter((e) => e.id !== saved.id), saved])
            void loadModels()
          }}
        />
      </div>
    )
  }

  return (
    <div className="mx-auto max-w-lg space-y-6 px-4 py-16">
      {header("Choose which models to use for indexing and reviews")}

      <Card>
        <CardHeader className="pb-3">
          <CardTitle className="text-base">Indexing Model</CardTitle>
          <CardDescription>
            Used to summarize files when building the code index. We recommend
            a cheaper model here since it runs over every file.
          </CardDescription>
        </CardHeader>
        <CardContent>
          <ModelCombobox
            value={indexingModel}
            onChange={setIndexingModel}
            options={indexingOptions}
            configModel={configIndexingModel}
          />
        </CardContent>
      </Card>

      <Card>
        <CardHeader className="pb-3">
          <CardTitle className="text-base">Review Model</CardTitle>
          <CardDescription>
            Used to analyze PRs and post comments. A more powerful model here
            gives better review quality.
          </CardDescription>
        </CardHeader>
        <CardContent>
          <ModelCombobox
            value={reviewModel}
            onChange={setReviewModel}
            options={reviewOptions}
            configModel={configReviewModel}
          />
        </CardContent>
      </Card>

      <p className="text-center text-xs text-muted-foreground">
        You can change these later in Settings
      </p>

      <Button
        className="w-full"
        size="lg"
        onClick={handleSave}
        disabled={saving}
      >
        {saving && <Loader2 className="mr-2 h-4 w-4 animate-spin" />}
        Save and Continue
      </Button>
    </div>
  )
}
