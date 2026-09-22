import { ArrowDown, ArrowUp, Loader2, Plus, X } from "lucide-react"
import { useEffect, useState } from "react"

import { ModelCombobox, type ModelOption } from "@/components/model-combobox"
import { Button } from "@/components/ui/button"
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card"
import { Input } from "@/components/ui/input"
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select"
import { useParams } from "react-router"

import { api } from "@/lib/api"
import type {
  DefaultBackend,
  ModelRoute,
  ModelsSaveExtras,
} from "@/lib/api/settings"
import { useAuth } from "@/lib/auth"
import { useDocumentTitle } from "@/lib/hooks"

function endpointHost(url: string): string {
  try {
    const u = new URL(url)
    return `${u.host}${u.pathname}`.replace(/\/$/, "")
  } catch {
    return url
  }
}

// A settings key is a *path*: `verdict.mode` lives under a `verdict` object,
// because the override blob mirrors the shape of the config tree rather than
// flattening it. Reading and writing both have to walk it.
function readPath(
  source: Record<string, unknown> | undefined,
  path: string
): unknown {
  let node: unknown = source
  for (const part of path.split(".")) {
    if (!node || typeof node !== "object") return undefined
    node = (node as Record<string, unknown>)[part]
  }
  return node
}

// Writing `null` *removes* the key, and removes the parent it leaves empty:
// an override of `{}` is not the same as no override — it is a stored object
// that shadows nothing, forever, and there is no UI to notice it.
function writePath(
  source: Record<string, unknown>,
  path: string,
  value: number | boolean | string | null
): Record<string, unknown> {
  const [head, ...rest] = path.split(".")
  const next = { ...source }
  if (rest.length === 0) {
    if (value === null || value === "") delete next[head]
    else next[head] = value
    return next
  }
  const child = writePath(
    (next[head] as Record<string, unknown> | undefined) ?? {},
    rest.join("."),
    value
  )
  if (Object.keys(child).length === 0) delete next[head]
  else next[head] = child
  return next
}

type Purpose = "indexing" | "review" | "security"
const NO_CHAINS: Record<Purpose, string[]> = {
  indexing: [],
  review: [],
  security: [],
}

export function SettingsPage() {
  useDocumentTitle("Settings")
  const { user: currentUser } = useAuth()
  const { section = "models" } = useParams()

  // "" = inherit from deployment config; anything else is a model id.
  const [indexingModel, setIndexingModel] = useState("")
  const [reviewModel, setReviewModel] = useState("")
  const [securityModel, setSecurityModel] = useState("")
  const [configIndexingModel, setConfigIndexingModel] = useState("")
  const [configReviewModel, setConfigReviewModel] = useState("")
  const [configSecurityModel, setConfigSecurityModel] = useState("")
  const [backend, setBackend] = useState("")
  const [indexingOptions, setIndexingOptions] = useState<ModelOption[]>([])
  const [reviewOptions, setReviewOptions] = useState<ModelOption[]>([])
  const [securityOptions, setSecurityOptions] = useState<ModelOption[]>([])
  const [thinkingMode, setThinkingMode] = useState("off")
  const [thinkingOptions, setThinkingOptions] = useState<ModelOption[]>([])
  const [apiStyle, setApiStyle] = useState("chat")
  const [apiStyleOptions, setApiStyleOptions] = useState<ModelOption[]>([])
  // Where a bare model id goes: a signed-in provider chosen under
  // Connections, or the API key when none is. Options that name a backend
  // themselves (`oauth:…`, `api:…`) do not depend on it.
  const [defaultBackend, setDefaultBackend] = useState<DefaultBackend>({})
  // What each purpose's *saved* value does, as the server resolves it.
  const [routes, setRoutes] = useState<{
    indexing?: ModelRoute
    review?: ModelRoute
    security?: ModelRoute
  }>({})
  // The values as loaded, to know when a draft still matches the saved one.
  const [saved, setSaved] = useState({ indexing: "", review: "", security: "" })
  // Fallback chains as the server resolves them (dashboard → mira.yaml),
  // where each came from, and what "inherit" would give.
  const [fallbacks, setFallbacks] =
    useState<Record<Purpose, string[]>>(NO_CHAINS)
  const [fallbackSources, setFallbackSources] = useState<
    Record<Purpose, "dashboard" | "config">
  >({ indexing: "config", review: "config", security: "config" })
  const [configFallbacks, setConfigFallbacks] =
    useState<Record<Purpose, string[]>>(NO_CHAINS)
  // Chains edited since load: a list to store, or null to hand the chain
  // back to mira.yaml. Only these are sent on save, so an untouched chain
  // that came from the file is not silently converted into an override.
  const [fallbackEdits, setFallbackEdits] = useState<
    Partial<Record<Purpose, string[] | null>>
  >({})
  const [fallbackLimit, setFallbackLimit] = useState(5)
  // Output budget per call. "inherit" = mira.yaml's `max_tokens`;
  // "unlimited" sends no cap at all; "custom" is a count. `maxTokensDirty`
  // says whether to send it on save, so an untouched inherited value stays
  // inherited rather than becoming an override.
  const [maxTokensMode, setMaxTokensMode] = useState<
    "inherit" | "unlimited" | "custom"
  >("inherit")
  const [maxTokensValue, setMaxTokensValue] = useState("")
  const [maxTokensDirty, setMaxTokensDirty] = useState(false)
  const [configMaxTokens, setConfigMaxTokens] = useState(4096)
  const [savingModels, setSavingModels] = useState(false)
  const [modelsSaved, setModelsSaved] = useState(false)

  const [effective, setEffective] = useState<{
    filter?: Record<string, number | boolean | string>
    review?: Record<string, number | boolean | string>
  } | null>(null)
  // `unknown`, not a scalar union: a nested setting like `verdict.mode` is
  // stored the way the config tree stores it, as an object under `verdict`.
  const [overrides, setOverrides] = useState<{
    filter: Record<string, unknown>
    review: Record<string, unknown>
  }>({ filter: {}, review: {} })
  const [savingOverrides, setSavingOverrides] = useState(false)
  const [overridesSaved, setOverridesSaved] = useState(false)
  // While the user is typing in a number field we hold their literal string
  // here. Without this, controlled inputs round-trip through `String(Number())`
  // on every keystroke and partial states like `"0."` get normalized to `"0"`,
  // making backspace/decimal entry feel broken.
  const [drafts, setDrafts] = useState<Record<string, string>>({})
  // Field-keyed errors (e.g. "filter.confidence_threshold" → "must be ≤ 1")
  // render inline under the offending input. `_global` is the catch-all
  // bucket for non-field errors.
  const [fieldErrors, setFieldErrors] = useState<Record<string, string>>({})

  const loadMaxTokens = (
    value: number | undefined,
    source: string | undefined,
    configValue: number | undefined
  ) => {
    setConfigMaxTokens(configValue ?? 4096)
    if (source !== "dashboard") setMaxTokensMode("inherit")
    else if (value === 0) setMaxTokensMode("unlimited")
    else setMaxTokensMode("custom")
    setMaxTokensValue(value && value > 0 ? String(value) : "")
    setMaxTokensDirty(false)
  }

  useEffect(() => {
    if (!currentUser?.is_admin) return
    api.getModels().then((m) => {
      const indexing = m.indexing_source === "config" ? "" : m.indexing_model
      const review = m.review_source === "config" ? "" : m.review_model
      const security = m.security_source === "config" ? "" : m.security_model
      setIndexingModel(indexing)
      setReviewModel(review)
      setSecurityModel(security)
      setSaved({ indexing, review, security })
      setRoutes({
        indexing: m.indexing_route,
        review: m.review_route,
        security: m.security_route,
      })
      setDefaultBackend(m.default_backend ?? {})
      setConfigIndexingModel(m.config_indexing_model)
      setConfigReviewModel(m.config_review_model)
      setConfigSecurityModel(m.config_security_model)
      setBackend(m.backend)
      setIndexingOptions(m.indexing_options)
      setReviewOptions(m.review_options)
      setSecurityOptions(m.security_options)
      setThinkingMode(m.review_thinking_mode)
      setThinkingOptions(m.thinking_options)
      setApiStyle(m.api_style ?? "chat")
      setApiStyleOptions(m.api_style_options ?? [])
      setFallbacks({
        indexing: m.indexing_fallbacks ?? [],
        review: m.review_fallbacks ?? [],
        security: m.security_fallbacks ?? [],
      })
      setFallbackSources({
        indexing: m.indexing_fallbacks_source ?? "config",
        review: m.review_fallbacks_source ?? "config",
        security: m.security_fallbacks_source ?? "config",
      })
      setConfigFallbacks({
        indexing: m.config_indexing_fallbacks ?? [],
        review: m.config_review_fallbacks ?? [],
        security: m.config_security_fallbacks ?? [],
      })
      setFallbackLimit(m.fallback_chain_limit ?? 5)
      setFallbackEdits({})
      loadMaxTokens(m.max_tokens, m.max_tokens_source, m.config_max_tokens)
    })
    api.getGlobalSettings().then((s) => {
      setEffective(
        (s.effective as {
          filter?: Record<string, number | boolean | string>
          review?: Record<string, number | boolean | string>
        }) ?? null
      )
      setOverrides({
        filter: (s.overrides.filter ?? {}) as Record<string, unknown>,
        review: (s.overrides.review ?? {}) as Record<string, unknown>,
      })
    })
  }, [currentUser])

  if (!currentUser?.is_admin) {
    return (
      <div className="p-6 text-sm text-muted-foreground">
        Admin access required.
      </div>
    )
  }

  const saveModels = async () => {
    setSavingModels(true)
    const extras: ModelsSaveExtras = {}
    // A row added but never given a model is not a fallback: it is left out
    // here, in the open, rather than dropped by the server behind the page.
    const filled = (chain: string[] | null | undefined) =>
      chain == null ? chain : chain.filter((v) => v.trim() !== "")
    if (fallbackEdits.indexing !== undefined)
      extras.indexing_fallbacks = filled(fallbackEdits.indexing)
    if (fallbackEdits.review !== undefined)
      extras.review_fallbacks = filled(fallbackEdits.review)
    if (fallbackEdits.security !== undefined)
      extras.security_fallbacks = filled(fallbackEdits.security)
    if (maxTokensDirty) {
      if (maxTokensMode === "inherit") extras.max_tokens = null
      else if (maxTokensMode === "unlimited") extras.max_tokens = 0
      else {
        const n = Number.parseInt(maxTokensValue, 10)
        // A custom mode with nothing typed is the inherited value, not a
        // request for zero output.
        extras.max_tokens = Number.isFinite(n) && n > 0 ? n : null
      }
    }
    await api.saveModels(
      indexingModel,
      reviewModel,
      securityModel,
      thinkingMode,
      apiStyle,
      extras
    )
    setSavingModels(false)
    setModelsSaved(true)
    setTimeout(() => setModelsSaved(false), 2000)
    // The server's resolution is for the saved values; reload it so the
    // "will call" lines describe what was just written.
    api.getModels().then((m) => {
      setSaved({
        indexing: m.indexing_source === "config" ? "" : m.indexing_model,
        review: m.review_source === "config" ? "" : m.review_model,
        security: m.security_source === "config" ? "" : m.security_model,
      })
      setRoutes({
        indexing: m.indexing_route,
        review: m.review_route,
        security: m.security_route,
      })
      setFallbacks({
        indexing: m.indexing_fallbacks ?? [],
        review: m.review_fallbacks ?? [],
        security: m.security_fallbacks ?? [],
      })
      setFallbackSources({
        indexing: m.indexing_fallbacks_source ?? "config",
        review: m.review_fallbacks_source ?? "config",
        security: m.security_fallbacks_source ?? "config",
      })
      setFallbackEdits({})
      loadMaxTokens(m.max_tokens, m.max_tokens_source, m.config_max_tokens)
    })
  }

  // The chain a purpose shows: the edit in progress, else the saved one. A
  // pending reset (null) shows what mira.yaml would give.
  const chainFor = (purpose: Purpose): string[] => {
    const edit = fallbackEdits[purpose]
    if (edit === undefined) return fallbacks[purpose]
    return edit ?? configFallbacks[purpose]
  }
  const chainIsOverride = (purpose: Purpose): boolean => {
    const edit = fallbackEdits[purpose]
    if (edit !== undefined) return edit !== null
    return fallbackSources[purpose] === "dashboard"
  }
  const editChain = (purpose: Purpose, next: string[] | null) =>
    setFallbackEdits((prev) => ({ ...prev, [purpose]: next }))

  const defaultLabel = defaultBackend.provider_label
    ? `${defaultBackend.provider_label} · ${defaultBackend.account_label ?? ""}`
    : "the API-key endpoint"

  // One line under each picker saying where that choice sends calls. For a
  // draft that still matches the saved value the server's resolution is
  // authoritative; for an edited one, the option's own group says it.
  const routeLine = (
    draft: string,
    savedValue: string,
    configValue: string,
    options: ModelOption[],
    route?: ModelRoute
  ): string => {
    if (draft === savedValue && route) {
      const who = route.account_label ? ` · ${route.account_label}` : ""
      return `${route.model} via ${route.provider_label}${who} · ${route.protocol} · ${endpointHost(route.endpoint)}`
    }
    const effective = draft === "" ? configValue : draft
    const option = options.find((o) => o.value === effective)
    if (option?.group) {
      return `${option.label} via ${option.group}${option.detail ? ` · ${option.detail}` : ""}`
    }
    if (effective.startsWith("oauth:")) {
      const [provider, account, ...rest] = effective.slice(6).split(":")
      return `${rest.join(":")} via ${provider} account ${account === "*" ? "(any, rotating)" : account} — not currently connected`
    }
    if (effective.startsWith("api:")) {
      return `${effective.slice(4)} via the API-key endpoint`
    }
    return `${effective} via ${defaultLabel} (bare id: goes to the default backend)`
  }

  // The thinking levels to offer: the review model's own, when its provider
  // reported them (models.dev for API-key endpoints, the backend itself for
  // ChatGPT), else the built-in list. A saved level the list lacks is kept
  // as a row of its own rather than silently shown as something else.
  const reviewOption = reviewOptions.find(
    (o) => o.value === (reviewModel === "" ? configReviewModel : reviewModel)
  )
  const providerLevels = reviewOption?.reasoning_levels ?? []
  const thinkingChoices: ModelOption[] =
    providerLevels.length > 0
      ? [
          { value: "off", label: "Off" },
          ...providerLevels.map((level) => ({
            value: level,
            label: level.charAt(0).toUpperCase() + level.slice(1),
          })),
        ]
      : thinkingOptions
  const thinkingRows = thinkingChoices.some((o) => o.value === thinkingMode)
    ? thinkingChoices
    : [
        ...thinkingChoices,
        { value: thinkingMode, label: `${thinkingMode} (saved)` },
      ]

  // An ordered list of fallback models under a purpose's picker. Each row
  // is the same picker, without the inherit row: a fallback is always a
  // model somebody chose. Rows move up and down, since the order is the
  // order they are tried in.
  const fallbackList = (purpose: Purpose, options: ModelOption[]) => {
    const chain = chainFor(purpose)
    const override = chainIsOverride(purpose)
    const setAt = (i: number, value: string) => {
      const next = [...chain]
      next[i] = value
      editChain(purpose, next)
    }
    const move = (i: number, by: -1 | 1) => {
      const j = i + by
      if (j < 0 || j >= chain.length) return
      const next = [...chain]
      const swapped = next[i]
      next[i] = next[j]
      next[j] = swapped
      editChain(purpose, next)
    }
    return (
      <div className="space-y-2 rounded-md border bg-muted/20 p-3">
        <div className="flex items-center justify-between">
          <span className="text-xs font-medium">
            Fallback models
            {chain.length > 0 ? ` · ${chain.length} of ${fallbackLimit}` : ""}
          </span>
          <span className="text-[0.7rem] text-muted-foreground">
            {override ? "set here" : "from deployment config"}
          </span>
        </div>
        {chain.length === 0 && (
          <p className="text-xs text-muted-foreground">
            None. When this model fails a call after its own retries, the review
            fails.
          </p>
        )}
        {chain.map((value, i) => (
          <div key={`${purpose}-${i}`} className="space-y-1">
            <div className="flex items-center gap-1">
              <span className="w-5 text-right font-mono text-xs text-muted-foreground">
                {i + 1}.
              </span>
              <div className="min-w-0 flex-1">
                <ModelCombobox
                  value={value}
                  onChange={(v) => setAt(i, v)}
                  options={options}
                />
              </div>
              <Button
                variant="ghost"
                size="icon-sm"
                aria-label="Move up"
                disabled={i === 0}
                onClick={() => move(i, -1)}
              >
                <ArrowUp />
              </Button>
              <Button
                variant="ghost"
                size="icon-sm"
                aria-label="Move down"
                disabled={i === chain.length - 1}
                onClick={() => move(i, 1)}
              >
                <ArrowDown />
              </Button>
              <Button
                variant="ghost"
                size="icon-sm"
                aria-label="Remove"
                onClick={() =>
                  editChain(
                    purpose,
                    chain.filter((_, j) => j !== i)
                  )
                }
              >
                <X />
              </Button>
            </div>
            {value ? (
              <p className="pl-6 font-mono text-[0.7rem] text-muted-foreground">
                → {routeLine(value, "", "", options)}
              </p>
            ) : (
              <p className="pl-6 text-[0.7rem] text-amber-600 dark:text-amber-500">
                Pick a model — a row left empty is not saved.
              </p>
            )}
          </div>
        ))}
        <div className="flex items-center gap-2">
          <Button
            variant="outline"
            size="sm"
            disabled={chain.length >= fallbackLimit}
            onClick={() => editChain(purpose, [...chain, ""])}
          >
            <Plus /> Add fallback
          </Button>
          {override && (
            <Button
              variant="ghost"
              size="sm"
              onClick={() => editChain(purpose, null)}
            >
              Use deployment config
              {configFallbacks[purpose].length > 0
                ? ` (${configFallbacks[purpose].length})`
                : " (none)"}
            </Button>
          )}
        </div>
        <p className="text-xs text-muted-foreground">
          Tried in this order when the model above fails a call — after its own
          retries, re-rolls and JSON-mode rescue. Each entry can be on another
          endpoint or account.
        </p>
      </div>
    )
  }

  const setOverride = (
    section: "filter" | "review",
    key: string,
    value: number | boolean | string | null
  ) => {
    setOverrides((prev) => ({
      ...prev,
      [section]: writePath(prev[section], key, value),
    }))
  }

  const saveOverrides = async () => {
    setSavingOverrides(true)
    setFieldErrors({})
    try {
      // Both sections, always — including empty ones. The endpoint writes
      // what it is sent and leaves the sections it is not sent alone (the
      // gate, checks, autofix and triage panels own those), so an omitted
      // section is "not mine to touch" and an empty one is "remove it".
      // Sending only the non-empty ones would make the last override in a
      // section impossible to clear.
      await api.saveGlobalSettings({
        filter: overrides.filter,
        review: overrides.review,
      })
      setOverridesSaved(true)
      setTimeout(() => setOverridesSaved(false), 2000)
      const fresh = await api.getGlobalSettings()
      setEffective(
        (fresh.effective as {
          filter?: Record<string, number | boolean | string>
          review?: Record<string, number | boolean | string>
        }) ?? null
      )
    } catch (err) {
      // The API returns `{detail: {field?, message}}` for validation failures.
      // `fetchJson`/`putJson` wrap the response body in `API error NNN: <body>`,
      // so strip that prefix and JSON.parse the rest to recover the structured
      // detail. Regex-extracting the detail object choked on nested braces /
      // escaped quotes — full JSON.parse is the right tool.
      const raw = err instanceof Error ? err.message : String(err)
      // Try to parse the full error message as JSON to extract structured detail.
      let parsedError: { detail?: { field?: string; message: string } } | null =
        null
      try {
        parsedError = JSON.parse(raw.replace(/^API error \d+: /, ""))
      } catch {
        /* ignore */
      }
      const detail = parsedError?.detail
      if (detail && typeof detail === "object" && "message" in detail) {
        setFieldErrors({ [detail.field ?? "_global"]: detail.message })
      } else {
        setFieldErrors({ _global: raw })
      }
    } finally {
      setSavingOverrides(false)
    }
  }

  const numField = (
    section: "filter" | "review",
    key: string,
    label: string,
    description: string,
    step: string = "1",
    min?: number,
    max?: number
  ) => {
    const fieldKey = `${section}.${key}`
    const eff = readPath(
      effective?.[section] as Record<string, unknown> | undefined,
      key
    )
    const override = readPath(overrides[section], key)
    const overridden = override !== undefined
    const committed = typeof override === "number" ? override : eff
    const draft = drafts[fieldKey]
    const display =
      draft !== undefined
        ? draft
        : committed !== undefined && committed !== null
          ? String(committed)
          : ""
    const error = fieldErrors[fieldKey]

    const commit = () => {
      const v = drafts[fieldKey]
      if (v === undefined) return
      // Drop the draft so the next render reads from `committed` again.
      setDrafts((d) => {
        const next = { ...d }
        delete next[fieldKey]
        return next
      })
      if (v === "") {
        setOverride(section, key, null)
        return
      }
      let n = Number(v)
      if (Number.isNaN(n)) return
      // Clamp to declared bounds so the user can't enter out-of-range
      // values that the server would just reject anyway.
      if (typeof min === "number" && n < min) n = min
      if (typeof max === "number" && n > max) n = max
      setOverride(section, key, n === eff ? null : n)
    }

    return (
      <div className="space-y-1">
        <div className="flex items-baseline gap-3">
          <label className="text-sm font-medium" htmlFor={fieldKey}>
            {label}
          </label>
          {overridden && (
            <span className="text-[11px] font-semibold text-primary">
              Overrides <code className="font-mono">mira.yaml</code>
            </span>
          )}
        </div>
        <Input
          id={fieldKey}
          type="number"
          step={step}
          min={min}
          max={max}
          aria-invalid={error ? true : undefined}
          className={error ? "border-destructive" : undefined}
          value={display}
          onChange={(e) =>
            setDrafts((d) => ({ ...d, [fieldKey]: e.target.value }))
          }
          onBlur={commit}
          onKeyDown={(e) => {
            if (e.key === "Enter") {
              e.currentTarget.blur()
            }
          }}
        />
        {error ? (
          <p className="text-xs text-destructive">
            {label} {error}
          </p>
        ) : (
          <p className="text-xs text-muted-foreground">{description}</p>
        )}
      </div>
    )
  }

  const boolField = (
    section: "filter" | "review",
    key: string,
    label: string,
    description: string
  ) => {
    const eff = readPath(
      effective?.[section] as Record<string, unknown> | undefined,
      key
    )
    const override = readPath(overrides[section], key)
    const overridden = override !== undefined
    const checked = typeof override === "boolean" ? override : Boolean(eff)
    const error = fieldErrors[`${section}.${key}`]
    return (
      <div className="space-y-1">
        <div className="flex items-center gap-3">
          <label
            className="flex items-center gap-2 text-sm font-medium"
            htmlFor={`${section}.${key}`}
          >
            <input
              id={`${section}.${key}`}
              type="checkbox"
              checked={checked}
              onChange={(e) =>
                setOverride(
                  section,
                  key,
                  e.target.checked === Boolean(eff) ? null : e.target.checked
                )
              }
              className="size-4 rounded border-input accent-primary"
            />
            {label}
          </label>
          {overridden && (
            <span className="text-[11px] font-semibold text-primary">
              Overrides <code className="font-mono">mira.yaml</code>
            </span>
          )}
        </div>
        {error ? (
          <p className="pl-6 text-xs text-destructive">
            {label} {error}
          </p>
        ) : (
          <p className="pl-6 text-xs text-muted-foreground">{description}</p>
        )}
      </div>
    )
  }

  const choiceField = (
    section: "filter" | "review",
    key: string,
    label: string,
    description: string,
    options: { value: string; label: string }[]
  ) => {
    const fieldKey = `${section}.${key}`
    const eff = readPath(
      effective?.[section] as Record<string, unknown> | undefined,
      key
    )
    const override = readPath(overrides[section], key)
    const overridden = override !== undefined
    const value = typeof override === "string" ? override : String(eff ?? "")
    const error = fieldErrors[fieldKey]
    return (
      <div className="space-y-1">
        <div className="flex items-baseline gap-3">
          <label className="text-sm font-medium" htmlFor={fieldKey}>
            {label}
          </label>
          {overridden && (
            <span className="text-[11px] font-semibold text-primary">
              Overrides <code className="font-mono">mira.yaml</code>
            </span>
          )}
        </div>
        <Select
          value={value}
          onValueChange={(next) =>
            // Choosing what the config already says clears the override rather
            // than storing a copy of it — otherwise editing mira.yaml later
            // would silently stop taking effect for this field.
            setOverride(section, key, next === String(eff ?? "") ? null : next)
          }
        >
          <SelectTrigger id={fieldKey} className="w-full">
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            {options.map((option) => (
              <SelectItem key={option.value} value={option.value}>
                {option.label}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
        {error ? (
          <p className="text-xs text-destructive">
            {label} {error}
          </p>
        ) : (
          <p className="text-xs text-muted-foreground">{description}</p>
        )}
      </div>
    )
  }

  return (
    <div className="space-y-6 p-6">
      <div>
        <h1 className="text-2xl font-semibold tracking-tight">Settings</h1>
        <p className="text-sm text-muted-foreground">
          Configure Mira models and behavior
        </p>
      </div>

      {section === "models" && (
        <Card>
          <CardHeader>
            <CardTitle>Models</CardTitle>
            <CardDescription>
              Choose models for indexing and PR reviews. Each backend is its own
              section in the picker — the API-key endpoint and every signed-in
              account — so the same model name never stands for two places.
            </CardDescription>
          </CardHeader>
          <CardContent className="space-y-4">
            <p className="rounded-md border bg-muted/40 p-3 text-xs text-muted-foreground">
              {defaultBackend.provider_label ? (
                <>
                  A model picked without a backend (a bare id) goes to{" "}
                  <strong>{defaultLabel}</strong>
                  {defaultBackend.mode === "rotate" &&
                  (defaultBackend.accounts ?? 0) > 1
                    ? ", rotating by remaining allowance"
                    : ""}
                  . Sections marked <em>API key</em> keep a purpose on the
                  configured endpoint instead, and each account&apos;s section
                  pins it there.
                </>
              ) : (
                <>
                  A model picked without a backend (a bare id) goes to the
                  configured API-key endpoint. Signed-in accounts appear as
                  their own sections once connected.
                </>
              )}{" "}
              Manage accounts and the default under{" "}
              <a className="underline" href="/settings/connections">
                Connections
              </a>
              .
            </p>
            <div className="space-y-2">
              <label className="text-sm font-medium">Indexing Model</label>
              <ModelCombobox
                value={indexingModel}
                onChange={setIndexingModel}
                options={indexingOptions}
                configModel={configIndexingModel}
              />
              <p className="font-mono text-[0.7rem] text-muted-foreground">
                →{" "}
                {routeLine(
                  indexingModel,
                  saved.indexing,
                  configIndexingModel,
                  indexingOptions,
                  routes.indexing
                )}
              </p>
              <p className="text-xs text-muted-foreground">
                Used to summarize files when building the code index. A cheaper
                model is recommended since it runs over every file.
              </p>
              {fallbackList("indexing", indexingOptions)}
            </div>
            <div className="space-y-2">
              <label className="text-sm font-medium">Review Model</label>
              <ModelCombobox
                value={reviewModel}
                onChange={setReviewModel}
                options={reviewOptions}
                configModel={configReviewModel}
              />
              <p className="font-mono text-[0.7rem] text-muted-foreground">
                →{" "}
                {routeLine(
                  reviewModel,
                  saved.review,
                  configReviewModel,
                  reviewOptions,
                  routes.review
                )}
              </p>
              <p className="text-xs text-muted-foreground">
                Used to analyze PRs and post review comments. A more powerful
                model gives better review quality.
              </p>
              {fallbackList("review", reviewOptions)}
            </div>
            <div className="space-y-2">
              <label className="text-sm font-medium">Security Model</label>
              <ModelCombobox
                value={securityModel}
                onChange={setSecurityModel}
                options={securityOptions}
                configModel={configSecurityModel}
              />
              <p className="font-mono text-[0.7rem] text-muted-foreground">
                →{" "}
                {routeLine(
                  securityModel,
                  saved.security,
                  configSecurityModel,
                  securityOptions,
                  routes.security
                )}
              </p>
              <p className="text-xs text-muted-foreground">
                Used for the dedicated security pass. Defaults to the review
                model — set a cheaper one only if you accept lower security
                recall. Its fallbacks default to the review model&apos;s.
              </p>
              {fallbackList("security", securityOptions)}
            </div>
            <div className="space-y-2">
              <label className="text-sm font-medium">
                Review Thinking Mode
              </label>
              <Select value={thinkingMode} onValueChange={setThinkingMode}>
                <SelectTrigger>
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  {thinkingRows.map((opt) => (
                    <SelectItem key={opt.value} value={opt.value}>
                      {opt.label}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
              <p className="font-mono text-[0.7rem] text-muted-foreground">
                →{" "}
                {providerLevels.length > 0
                  ? `levels reported for ${reviewOption?.label ?? "this model"} by ${reviewOption?.group || "its provider"}: ${providerLevels.join(", ")}`
                  : "built-in levels (the provider has not reported this model's own)"}
              </p>
              <p className="text-xs text-muted-foreground">
                Extended reasoning budget for reviews — improves depth on
                capable models at the cost of latency and tokens. The levels are
                the model&apos;s own where its provider reports them; a level a
                fallback model lacks is snapped to the nearest it has, and an
                endpoint that rejects reasoning is retried without it.
              </p>
            </div>
            <div className="space-y-2">
              <label className="text-sm font-medium">Max output tokens</label>
              <div className="flex items-center gap-2">
                <Select
                  value={maxTokensMode}
                  onValueChange={(v) => {
                    setMaxTokensMode(v as "inherit" | "unlimited" | "custom")
                    setMaxTokensDirty(true)
                  }}
                >
                  <SelectTrigger className="w-72">
                    <SelectValue />
                  </SelectTrigger>
                  <SelectContent>
                    <SelectItem value="inherit">
                      Inherit from deployment config (
                      {configMaxTokens === 0 ? "unlimited" : configMaxTokens})
                    </SelectItem>
                    <SelectItem value="unlimited">
                      Unlimited — let the model decide
                    </SelectItem>
                    <SelectItem value="custom">Custom limit</SelectItem>
                  </SelectContent>
                </Select>
                {maxTokensMode === "custom" && (
                  <Input
                    type="number"
                    inputMode="numeric"
                    min={1}
                    step={1024}
                    className="w-32"
                    placeholder={String(configMaxTokens || 4096)}
                    value={maxTokensValue}
                    onChange={(e) => {
                      setMaxTokensValue(e.target.value)
                      setMaxTokensDirty(true)
                    }}
                  />
                )}
              </div>
              <p className="text-xs text-muted-foreground">
                The output budget on every call, thinking included. A reasoning
                model can spend a small budget entirely on thinking and answer
                with nothing; <em>Unlimited</em> sends no cap, so the model
                stops where it stops — its own maximum applies. Requests are not
                streamed, so a model that thinks past the request timeout (120s
                by default) fails instead; a large custom limit such as 32768 is
                the safer choice for reasoning models. Applies to every purpose
                and to the fallback models.
              </p>
            </div>
            {backend !== "bedrock" && (
              <div className="space-y-2">
                <label className="text-sm font-medium">
                  API Protocol (API-key endpoint)
                </label>
                <Select value={apiStyle} onValueChange={setApiStyle}>
                  <SelectTrigger>
                    <SelectValue />
                  </SelectTrigger>
                  <SelectContent>
                    {apiStyleOptions.map((opt) => (
                      <SelectItem key={opt.value} value={opt.value}>
                        {opt.label}
                      </SelectItem>
                    ))}
                  </SelectContent>
                </Select>
                <p className="text-xs text-muted-foreground">
                  Protocol used to talk to the configured API-key endpoint.
                  Responses API requires a server exposing /responses (OpenAI
                  and compatible proxies); Chat Completions works everywhere.
                  Signed-in accounts bring their own protocol and ignore this.
                </p>
              </div>
            )}
            <div className="flex items-center gap-3">
              <Button size="sm" onClick={saveModels} disabled={savingModels}>
                {savingModels && (
                  <Loader2 className="mr-2 h-3 w-3 animate-spin" />
                )}
                Save
              </Button>
              {modelsSaved && (
                <span className="text-xs text-muted-foreground">Saved</span>
              )}
            </div>
          </CardContent>
        </Card>
      )}

      {section === "review" && (
        <Card>
          <CardHeader>
            <CardTitle>Review behaviour overrides</CardTitle>
            <CardDescription>
              Tune the noise filter and review knobs without restarting the
              server. These overrides deep-merge over{" "}
              <code className="text-xs">mira.yaml</code> and apply to every repo
              this Mira instance reviews. Per-repo{" "}
              <code className="text-xs">.mira.yml</code> files still take
              precedence for individual repos.
            </CardDescription>
          </CardHeader>
          <CardContent className="space-y-6">
            <div>
              <h3 className="mb-3 text-sm font-semibold">Filter</h3>
              <div className="space-y-4">
                {numField(
                  "filter",
                  "confidence_threshold",
                  "Confidence threshold",
                  "Drop comments the LLM rated below this confidence (0.0–1.0). Lower = more comments survive.",
                  "0.1",
                  0,
                  1
                )}
                {numField(
                  "filter",
                  "max_comments",
                  "Max comments per PR",
                  "Hard cap on inline comments per PR. Most severe + most confident N are kept.",
                  "1",
                  1
                )}
                {numField(
                  "filter",
                  "max_files",
                  "Max files",
                  "Cap on files reviewed in a single PR. PRs above this are partially reviewed.",
                  "1",
                  1
                )}
              </div>
            </div>

            <div>
              <h3 className="mb-3 text-sm font-semibold">Review</h3>
              <div className="space-y-4">
                {boolField(
                  "review",
                  "walkthrough",
                  "Post walkthrough comment",
                  "Top-level summary comment with file coverage and per-severity stats."
                )}
                {boolField(
                  "review",
                  "self_critique",
                  "Self-critique pass",
                  "Second-pass LLM critique on each draft comment. Drops confident-but-wrong findings at the cost of latency."
                )}
                {boolField(
                  "review",
                  "security_pass",
                  "Security review pass",
                  "Dedicated security pass (XSS, injection, auth, CSRF, SSRF, deserialization, crypto) merged with the main review."
                )}
                {boolField(
                  "review",
                  "blast_radius",
                  "Blast radius",
                  "Lists dependent repositories that import code touched by this PR in the walkthrough comment."
                )}
                {boolField(
                  "review",
                  "dependency_overlap",
                  "Duplicate dependency check",
                  "Warns when a PR adds a dependency that duplicates an existing one (e.g. a second table or HTTP-client library)."
                )}
                {boolField(
                  "review",
                  "auto_resolve_conversations",
                  "Auto-resolve conversations",
                  "Automatically resolve bot review threads the LLM verifies as fixed on each review. Turn off to leave comments open until a human resolves them."
                )}
                {boolField(
                  "review",
                  "review_on_synchronize",
                  "Review on every push",
                  "When enabled, Mira reviews every new commit pushed to a PR. Disable to only review on PR open or manual @bot_name review."
                )}
                {numField(
                  "review",
                  "max_concurrent_chunks",
                  "Max concurrent chunks",
                  "Parallelism for chunk reviews (1–20). Raise if your LLM provider can handle it.",
                  "1",
                  1,
                  20
                )}
              </div>
            </div>

            <div>
              <h3 className="mb-3 text-sm font-semibold">
                Approvals and the review check
              </h3>
              <div className="space-y-4">
                {choiceField(
                  "review",
                  "verdict.mode",
                  "Review verdict",
                  "Approving adds a signal a human can dismiss; requesting changes takes the merge button away until somebody does. On GitHub an approval from Mira counts toward a branch-protection approval requirement.",
                  [
                    { value: "off", label: "Comment only" },
                    { value: "approve", label: "Approve clean pull requests" },
                    {
                      value: "request_changes",
                      label: "Approve, and request changes on findings",
                    },
                  ]
                )}
                {choiceField(
                  "review",
                  "verdict.approve_max_severity",
                  "Highest severity still approved",
                  "A pull request whose worst finding is above this is never approved.",
                  [
                    {
                      value: "nitpick",
                      label: "Nitpick — a completely clean pass",
                    },
                    { value: "suggestion", label: "Suggestion" },
                    { value: "warning", label: "Warning" },
                    { value: "blocker", label: "Blocker — approve anything" },
                  ]
                )}
                {numField(
                  "review",
                  "verdict.approve_min_confidence",
                  "Confidence floor for an approval",
                  "The walkthrough's own merge-readiness score, 1–5. It asks what the severity ceiling cannot: not whether Mira found a problem, but whether it understood the change well enough for finding nothing to mean anything. 0 turns the floor off.",
                  "1",
                  0,
                  5
                )}
                {boolField(
                  "review",
                  "status.enabled",
                  "Publish the review check",
                  "A `mira/review` check on the head commit: pending while the review runs, then what it found. Without it a slow review looks like a bot that never arrived."
                )}
                {choiceField(
                  "review",
                  "status.fail_on",
                  "Turn the check red when",
                  "Findings only. Mira's own failures — a timeout, a rate limit — are always neutral and say so: a check that goes red when the API has a bad afternoon is a check people learn to ignore.",
                  [
                    {
                      value: "never",
                      label: "Never — green once the review finished",
                    },
                    { value: "blocker", label: "A blocker was posted" },
                    {
                      value: "above_ceiling",
                      label: "Anything above the approval ceiling was posted",
                    },
                  ]
                )}
              </div>
            </div>

            <div className="space-y-2">
              <div className="flex items-center gap-3">
                <Button
                  size="sm"
                  onClick={saveOverrides}
                  disabled={savingOverrides}
                >
                  {savingOverrides && (
                    <Loader2 className="mr-2 h-3 w-3 animate-spin" />
                  )}
                  Save overrides
                </Button>
                {overridesSaved && (
                  <span className="text-xs text-muted-foreground">Saved</span>
                )}
              </div>
              {fieldErrors._global && (
                <p className="text-xs break-words text-destructive">
                  {fieldErrors._global}
                </p>
              )}
            </div>
          </CardContent>
        </Card>
      )}
    </div>
  )
}
