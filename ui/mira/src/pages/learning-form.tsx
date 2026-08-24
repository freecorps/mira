import { ChevronLeft, Loader2, Trash2 } from "lucide-react"
import { useEffect, useReducer } from "react"
import { useNavigate, useSearchParams } from "react-router"

import { Button } from "@/components/ui/button"
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card"
import { ConfirmButton } from "@/components/ui/confirm-button"
import { Input } from "@/components/ui/input"
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select"
import { Textarea } from "@/components/ui/textarea"
import { toast } from "@/components/ui/sonner"
import { useDocumentTitle } from "@/lib/hooks"
import { api } from "@/lib/api"
import { useAuth } from "@/lib/auth"

function parseDetail(e: unknown): string {
  const raw = e instanceof Error ? e.message : String(e)
  try {
    const parsed = JSON.parse(raw.replace(/^API error \d+: /, ""))
    if (parsed?.detail)
      return typeof parsed.detail === "string"
        ? parsed.detail
        : JSON.stringify(parsed.detail)
  } catch {
    /* ignore */
  }
  return raw
}

function splitRepoKey(repoKey: string): [string, string] {
  const separator = repoKey.lastIndexOf("/")
  return separator > 0
    ? [repoKey.slice(0, separator), repoKey.slice(separator + 1)]
    : [repoKey, ""]
}

interface LearningFormState {
  loading: boolean
  saving: boolean
  error: string | null
  canEdit: boolean
  repos: string[]
  repoKey: string
  ruleText: string
  category: string
  pathPattern: string
  scopeType: string
  rationale: string
  language: string
}

const INITIAL_FORM: LearningFormState = {
  loading: true,
  saving: false,
  error: null,
  canEdit: true,
  repos: [],
  repoKey: "",
  ruleText: "",
  category: "other",
  pathPattern: "",
  scopeType: "repo",
  rationale: "",
  language: "",
}

function mergeForm(
  state: LearningFormState,
  patch: Partial<LearningFormState>
): LearningFormState {
  return { ...state, ...patch }
}

function LearningDetailsCard({
  form,
  isEdit,
  update,
}: {
  form: LearningFormState
  isEdit: boolean
  update: (patch: Partial<LearningFormState>) => void
}) {
  return (
    <Card>
      <CardHeader>
        <CardTitle>Details</CardTitle>
      </CardHeader>
      <CardContent className="space-y-4">
        <div className="space-y-2">
          <label className="text-sm font-medium">Repo</label>
          {isEdit ? (
            <div className="font-mono text-sm">{form.repoKey}</div>
          ) : (
            <Select
              value={form.repoKey}
              onValueChange={(repoKey) => update({ repoKey })}
            >
              <SelectTrigger>
                <SelectValue placeholder="Select a repo" />
              </SelectTrigger>
              <SelectContent>
                {form.repos.map((repo) => (
                  <SelectItem key={repo} value={repo}>
                    {repo}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          )}
        </div>

        <div className="space-y-2">
          <label className="text-sm font-medium" htmlFor="lr-text">
            Rule
          </label>
          <Textarea
            id="lr-text"
            rows={4}
            placeholder="e.g. Don't flag missing docstrings on internal helpers."
            value={form.ruleText}
            onChange={(event) => update({ ruleText: event.target.value })}
          />
        </div>

        <div className="space-y-2">
          <label className="text-sm font-medium" htmlFor="lr-rationale">
            Rationale
          </label>
          <Textarea
            id="lr-rationale"
            rows={3}
            placeholder="Why this rule exists and what evidence supports it."
            value={form.rationale}
            onChange={(event) => update({ rationale: event.target.value })}
          />
        </div>

        <div className="grid grid-cols-2 gap-4">
          <div className="space-y-2">
            <label className="text-sm font-medium" htmlFor="lr-category">
              Category
            </label>
            <Input
              id="lr-category"
              value={form.category}
              onChange={(event) => update({ category: event.target.value })}
            />
          </div>
          <div className="space-y-2">
            <label className="text-sm font-medium" htmlFor="lr-scope-type">
              Scope
            </label>
            <Select
              value={form.scopeType}
              onValueChange={(scopeType) => update({ scopeType })}
            >
              <SelectTrigger id="lr-scope-type">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="symbol">Symbol</SelectItem>
                <SelectItem value="path">Path</SelectItem>
                <SelectItem value="language">Language</SelectItem>
                <SelectItem value="repo">Repository</SelectItem>
                <SelectItem value="org">Organization</SelectItem>
              </SelectContent>
            </Select>
          </div>
        </div>
        <div className="space-y-2">
          <label className="text-sm font-medium" htmlFor="lr-scope-value">
            Scope value
          </label>
          <Input
            id="lr-scope-value"
            placeholder={
              form.scopeType === "path"
                ? "e.g. tests/**"
                : form.scopeType === "language"
                  ? "e.g. python"
                  : "Exact scope value"
            }
            value={form.pathPattern}
            onChange={(event) => update({ pathPattern: event.target.value })}
          />
        </div>
        {form.scopeType === "language" && (
          <div className="space-y-2">
            <label className="text-sm font-medium" htmlFor="lr-language">
              Language metadata
            </label>
            <Input
              id="lr-language"
              value={form.language}
              onChange={(event) => update({ language: event.target.value })}
            />
          </div>
        )}
      </CardContent>
    </Card>
  )
}

export function LearningFormPage() {
  const { user } = useAuth()
  const isAdmin = !!user?.is_admin
  const username = user?.username ?? ""
  const navigate = useNavigate()
  const [params] = useSearchParams()

  const editOwner = params.get("owner") ?? ""
  const editRepo = params.get("repo") ?? ""
  const editId = params.get("id")
  const editCandidateId = params.get("candidate")
  const isCandidate = Boolean(editCandidateId)
  const isEdit = Boolean(editId || editCandidateId)
  useDocumentTitle(isEdit ? "Edit learning" : "Add learning")

  const [form, updateForm] = useReducer(mergeForm, INITIAL_FORM)
  const {
    loading,
    saving,
    error,
    canEdit,
    repoKey,
    ruleText,
    category,
    pathPattern,
    scopeType,
    rationale,
    language,
  } = form

  useEffect(() => {
    const reposP = api.listRepos().catch(() => [])
    const ruleP =
      isCandidate && editOwner && editRepo && editCandidateId
        ? api
            .getLearningCandidate(editOwner, editRepo, Number(editCandidateId))
            .then((item) => ({ kind: "candidate" as const, item }))
        : isEdit && editOwner && editRepo && editId
          ? api
              .getLearnedRule(editOwner, editRepo, Number(editId))
              .then((item) => ({ kind: "rule" as const, item }))
          : Promise.resolve(null)
    Promise.all([reposP, ruleP])
      .then(([list, rule]) => {
        const slugs = list.map((r) => `${r.owner}/${r.repo}`)
        updateForm({ repos: slugs })
        if (rule) {
          const item = rule.item
          updateForm({
            repoKey: `${item.owner}/${item.repo}`,
            ruleText: item.rule_text,
            category: item.category || "other",
            scopeType: item.scope_type || "repo",
            rationale: item.rationale || "",
            language: "language" in item ? item.language : "",
            pathPattern:
              item.scope_value ||
              ("path_pattern" in item ? item.path_pattern : "") ||
              "",
            canEdit:
              rule.kind === "candidate"
                ? isAdmin &&
                  ["collecting", "pending"].includes(rule.item.status)
                : isAdmin ||
                  (rule.item.created_by === username &&
                    rule.item.status === "pending"),
            loading: false,
          })
        } else {
          updateForm({ repoKey: slugs[0] ?? "", loading: false })
        }
      })
      .catch((e) => {
        updateForm({ error: parseDetail(e), loading: false })
      })
  }, [
    isAdmin,
    username,
    isCandidate,
    isEdit,
    editOwner,
    editRepo,
    editId,
    editCandidateId,
  ])

  if (isEdit && !loading && !canEdit) {
    return (
      <div className="p-6 text-sm text-muted-foreground">
        You can only edit your own learnings while they're pending approval.
      </div>
    )
  }

  const save = async () => {
    if (!repoKey || !ruleText.trim()) {
      updateForm({ error: "Pick a repo and enter the rule text." })
      return
    }
    const [owner, repo] = splitRepoKey(repoKey)
    const scopeValue =
      pathPattern.trim() ||
      (scopeType === "repo" ? repoKey : scopeType === "org" ? owner : "")
    if (!scopeValue) {
      updateForm({ error: "Enter the exact scope value for this learning." })
      return
    }
    const body = {
      rule_text: ruleText.trim(),
      category: category.trim() || "other",
      path_pattern: scopeType === "path" ? pathPattern.trim() : "",
      scope_type: scopeType,
      scope_value: scopeValue,
      rationale: rationale.trim(),
      language: language.trim(),
    }
    updateForm({ saving: true, error: null })
    try {
      if (isCandidate && editCandidateId) {
        await api.updateLearningCandidate(
          owner,
          repo,
          Number(editCandidateId),
          body
        )
      } else if (isEdit && editId) {
        await api.updateLearnedRule(owner, repo, Number(editId), body)
      } else {
        await api.createLearnedRule(owner, repo, body)
      }
      toast.success(
        isEdit
          ? "Learning saved"
          : isAdmin
            ? "Learning added"
            : "Submitted for approval"
      )
      navigate(isEdit || isAdmin ? "/learnings" : "/learnings?tab=pending")
    } catch (e) {
      updateForm({ error: parseDetail(e) })
      toast.error("Couldn't save learning", { description: parseDetail(e) })
    } finally {
      updateForm({ saving: false })
    }
  }

  const remove = async () => {
    if (!editId) return
    updateForm({ error: null })
    try {
      await api.deleteLearnedRule(editOwner, editRepo, Number(editId))
      toast.success("Learning deleted")
      navigate("/learnings")
    } catch (e) {
      updateForm({ error: parseDetail(e) })
      toast.error("Couldn't delete learning", { description: parseDetail(e) })
    }
  }

  return (
    <div className="mx-auto max-w-2xl space-y-6 p-6">
      <button
        onClick={() => navigate("/learnings")}
        className="flex items-center gap-1 text-sm text-muted-foreground transition-colors hover:text-foreground"
      >
        <ChevronLeft className="h-4 w-4" /> Learnings
      </button>

      <div>
        <h1 className="text-2xl font-semibold tracking-tight">
          {isEdit ? "Edit learning" : "Add learning"}
        </h1>
        <p className="text-sm text-muted-foreground">
          {isEdit
            ? "Update this learned rule."
            : isAdmin
              ? "Author a rule directly — it's approved immediately and feeds future reviews."
              : "Suggest a rule — it'll be sent to the approval queue for an admin to review before it affects reviews."}
        </p>
      </div>

      {loading ? (
        <div className="flex items-center gap-2 text-sm text-muted-foreground">
          <Loader2 className="h-4 w-4 animate-spin" /> Loading…
        </div>
      ) : (
        <>
          <LearningDetailsCard
            form={form}
            isEdit={isEdit}
            update={updateForm}
          />

          {error && (
            <p className="text-sm break-words text-destructive">{error}</p>
          )}

          <div className="flex items-center justify-between">
            <div className="flex gap-2">
              <Button
                onClick={save}
                disabled={
                  saving ||
                  !ruleText.trim() ||
                  !repoKey ||
                  (!pathPattern.trim() &&
                    scopeType !== "repo" &&
                    scopeType !== "org")
                }
              >
                {saving && <Loader2 className="mr-2 h-4 w-4 animate-spin" />}
                {isEdit ? "Save changes" : "Add learning"}
              </Button>
              <Button variant="ghost" onClick={() => navigate("/learnings")}>
                Cancel
              </Button>
            </div>
            {isEdit && !isCandidate && isAdmin && (
              <ConfirmButton
                variant="ghost"
                className="text-destructive"
                destructive
                dialogTitle="Delete learning?"
                dialogDescription="This permanently removes the rule. This cannot be undone."
                confirmLabel="Delete"
                onConfirm={remove}
              >
                <Trash2 className="mr-2 h-4 w-4" /> Delete
              </ConfirmButton>
            )}
          </div>
        </>
      )}
    </div>
  )
}
