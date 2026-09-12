import {
  ReactFlow,
  Background,
  Controls,
  Handle,
  Position,
  addEdge,
  useNodesState,
  useEdgesState,
  type Node,
  type NodeProps,
  type Edge,
  type Connection,
  type ReactFlowInstance,
} from "@xyflow/react"
import "@xyflow/react/dist/style.css"
import {
  Copy,
  GitPullRequest,
  GitBranch,
  Plus,
  Save,
  Tags,
  Trash2,
  Play,
  LayoutGrid,
} from "lucide-react"
import { useEffect, useState } from "react"
import { toast } from "sonner"

import { Button } from "@/components/ui/button"
import {
  Card,
  CardContent,
  CardHeader,
  CardTitle,
  CardDescription,
} from "@/components/ui/card"
import { Input } from "@/components/ui/input"
import { Textarea } from "@/components/ui/textarea"
import { ConfirmDialog } from "@/components/ui/confirm-dialog"
import { api, type RepoListItem } from "@/lib/api"
import {
  labelsApi,
  type LabelWorkflow,
  type LabelNode,
  type LabelPreset,
  type LabelScope,
  type LabelField,
  type LabelCondition,
  type LabelOperator,
  type LabelFacts,
  type LabelPreview,
} from "@/lib/api/labels"
import { useAsync } from "@/lib/hooks"

const fields: Record<LabelField, string> = {
  total_lines: "Total lines (added + deleted)",
  additions: "Lines added",
  deletions: "Lines deleted",
  changed_files: "Number of changed files",
  author: "PR author",
  title: "PR title",
  description: "PR description",
  base_branch: "Target branch",
  head_branch: "Source branch",
  files: "Changed file path",
  draft: "Draft PR",
}
const operators: Record<LabelOperator, string> = {
  eq: "equals",
  ne: "does not equal",
  gt: "is greater than",
  gte: "is at least",
  lt: "is less than",
  lte: "is at most",
  contains: "contains",
  starts_with: "starts with",
  glob: "matches pattern",
}
const numeric = new Set<LabelField>([
  "total_lines",
  "additions",
  "deletions",
  "changed_files",
])
const selectClass =
  "h-9 w-full rounded-md border border-input bg-background px-3 text-sm"
type FlowNode = Node<{ rule: LabelNode }, "rule">

function WorkflowNodeView({ data, selected }: NodeProps<FlowNode>) {
  const rule = data.rule
  const Icon =
    rule.kind === "start"
      ? GitPullRequest
      : rule.kind === "condition"
        ? GitBranch
        : Tags
  return (
    <div
      className={`w-60 rounded-xl border bg-card p-4 text-card-foreground shadow-sm ${selected ? "ring-2 ring-primary" : "border-border"}`}
    >
      {rule.kind !== "start" && (
        <Handle type="target" position={Position.Left} />
      )}
      <div className="mb-2 flex items-center gap-2 text-xs font-medium text-muted-foreground">
        <Icon size={15} />
        {rule.kind === "start"
          ? "PR opened or updated"
          : rule.kind === "condition"
            ? "Condition"
            : "Apply label"}
      </div>
      {rule.kind === "start" && (
        <p className="text-sm">Evaluate this repository’s rules</p>
      )}
      {rule.condition && (
        <p className="text-sm break-words">
          {fields[rule.condition.field]}
          <br />
          <span className="font-semibold">
            {operators[rule.condition.operator]} {String(rule.condition.value)}
          </span>
        </p>
      )}
      {rule.action && (
        <>
          <span className="inline-flex max-w-full items-center gap-2 rounded-md bg-muted px-2 py-1 text-sm font-medium break-all">
            <span
              className="size-2.5 shrink-0 rounded-full"
              style={{ background: `#${rule.action.color}` }}
            />
            {rule.action.name || "Untitled label"}
          </span>
          <p className="mt-2 text-xs text-muted-foreground">
            {rule.action.mode === "sync"
              ? "Remove when no longer matching"
              : "Keep after adding"}
          </p>
        </>
      )}
      {rule.kind === "condition" ? (
        <>
          <div className="mt-3 flex justify-between text-[10px]">
            <span className="text-emerald-600">YES · upper output</span>
            <span className="text-muted-foreground">NO · lower output</span>
          </div>
          <Handle
            type="source"
            id="true"
            position={Position.Right}
            style={{ top: "35%", background: "#16a34a" }}
          />
          <Handle
            type="source"
            id="false"
            position={Position.Right}
            style={{ top: "75%", background: "#a1a1aa" }}
          />
        </>
      ) : rule.kind === "start" ? (
        <Handle type="source" id="next" position={Position.Right} />
      ) : null}
    </div>
  )
}
const nodeTypes = { rule: WorkflowNodeView }
const toNodes = (workflow: LabelWorkflow): FlowNode[] =>
  workflow.nodes.map((rule) => ({
    id: rule.id,
    type: "rule",
    position: rule.position,
    data: { rule },
    deletable: rule.kind !== "start",
  }))
const toEdges = (workflow: LabelWorkflow): Edge[] =>
  workflow.edges.map((edge) => ({
    id: edge.id,
    source: edge.source,
    target: edge.target,
    sourceHandle: edge.branch,
    label:
      edge.branch === "next"
        ? undefined
        : edge.branch === "true"
          ? "Yes"
          : "No",
  }))
const empty: LabelWorkflow = {
  version: 1,
  enabled: false,
  nodes: [{ id: "start", kind: "start", position: { x: 40, y: 140 } }],
  edges: [],
}

function errorText(error: unknown): string {
  const message =
    error instanceof Error
      ? error.message
      : typeof error === "string"
        ? error
        : "Something went wrong"
  const start = message.indexOf("{")
  if (start !== -1) {
    try {
      const detail = JSON.parse(message.slice(start)).detail
      if (Array.isArray(detail))
        return detail
          .map((item: { msg: string }) => item.msg.replace("Value error, ", ""))
          .join(". ")
      if (typeof detail === "string") return detail
    } catch {
      /* Fall back to the transport error. */
    }
  }
  return message
}

export function LabelWorkflowPanel({
  owner,
  repo,
}: {
  owner: string
  repo: string
}) {
  const { data: repos, loading, error } = useAsync(() => api.listRepos(), [])
  const [platform, setPlatform] = useState("")
  const available = (repos ?? []).filter(
    (item) => item.owner === owner && item.repo === repo
  )
  const selected =
    available.find((item) => item.platform === platform) ??
    available.find((item) => item.platform === "github") ??
    available[0]
  if (loading)
    return (
      <p className="p-6 text-sm text-muted-foreground">
        Loading label workflows…
      </p>
    )
  if (error || !selected)
    return (
      <p role="alert" className="p-6 text-sm text-destructive">
        {error ?? "Repository not found"}
      </p>
    )
  return (
    <div className="space-y-4 pt-4">
      {available.length > 1 && (
        <label className="block max-w-xs space-y-1 text-sm">
          Hosting platform
          <select
            className={selectClass}
            value={selected.platform}
            onChange={(event) => setPlatform(event.target.value)}
          >
            {available.map((item) => (
              <option key={item.platform}>{item.platform}</option>
            ))}
          </select>
        </label>
      )}
      <WorkflowLoader
        key={`${selected.platform}/${owner}/${repo}`}
        scope={{ owner, repo, platform: selected.platform }}
        repos={repos ?? []}
      />
    </div>
  )
}

function WorkflowLoader({
  scope,
  repos,
}: {
  scope: LabelScope
  repos: RepoListItem[]
}) {
  const { data, loading, error } = useAsync(
    () => Promise.all([labelsApi.get(scope), labelsApi.presets()]),
    [scope.platform, scope.owner, scope.repo]
  )
  if (loading)
    return <p className="p-6 text-sm text-muted-foreground">Loading rules…</p>
  if (error || !data)
    return (
      <p role="alert" className="p-6 text-sm text-destructive">
        {errorText(error)}
      </p>
    )
  return (
    <WorkflowEditor
      scope={scope}
      initial={data[0]}
      presets={data[1]}
      repos={repos}
    />
  )
}

function WorkflowEditor({
  scope,
  initial,
  presets,
  repos,
}: {
  scope: LabelScope
  initial: LabelWorkflow
  presets: LabelPreset[]
  repos: RepoListItem[]
}) {
  const [nodes, setNodes, onNodesChange] = useNodesState<FlowNode>(
    toNodes(initial.nodes.length ? initial : empty)
  )
  const [edges, setEdges, onEdgesChange] = useEdgesState(toEdges(initial))
  const [enabled, setEnabled] = useState(initial.enabled)
  const [selectedId, setSelectedId] = useState<string | null>(null)
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState("")
  const [source, setSource] = useState("")
  const [copying, setCopying] = useState(false)
  const [confirmCopy, setConfirmCopy] = useState(false)
  const [instance, setInstance] = useState<ReactFlowInstance<FlowNode> | null>(
    null
  )
  const workflow: LabelWorkflow = {
    version: 1,
    enabled,
    nodes: nodes.map((node) => ({
      ...node.data.rule,
      position: node.position,
    })),
    edges: edges.map((edge) => ({
      id: edge.id,
      source: edge.source,
      target: edge.target,
      branch: (edge.sourceHandle ?? "next") as "next" | "true" | "false",
    })),
  }
  const serialized = JSON.stringify(workflow)
  const [saved, setSaved] = useState(serialized)
  const dirty = serialized !== saved
  const selected = nodes.find((node) => node.id === selectedId)?.data.rule
  const otherRepos = repos.filter(
    (item) =>
      item.owner !== scope.owner ||
      item.repo !== scope.repo ||
      item.platform !== scope.platform
  )

  useEffect(() => {
    if (!dirty) return
    const warn = (event: BeforeUnloadEvent) => {
      event.preventDefault()
    }
    window.addEventListener("beforeunload", warn)
    return () => window.removeEventListener("beforeunload", warn)
  }, [dirty])

  function update(rule: LabelNode) {
    setNodes((previous) =>
      previous.map((node) =>
        node.id === rule.id ? { ...node, data: { rule } } : node
      )
    )
  }
  function addNode(kind: "condition" | "label") {
    const id = crypto.randomUUID()
    const position = instance?.screenToFlowPosition({
      x: window.innerWidth / 2,
      y: window.innerHeight / 2,
    }) ?? { x: 350, y: 150 }
    const rule: LabelNode = {
      id,
      kind,
      position,
      ...(kind === "condition"
        ? {
            condition: {
              field: "author",
              operator: "eq",
              value: "octocat",
            } as LabelCondition,
          }
        : {
            action: {
              name: "custom-label",
              color: "1d76db",
              description: "",
              mode: "sync" as const,
            },
          }),
    }
    setNodes((previous) => [
      ...previous,
      ...toNodes({ ...empty, nodes: [rule] }),
    ])
    setSelectedId(id)
  }
  function addPreset(preset: LabelPreset) {
    const start = nodes.find((node) => node.data.rule.kind === "start")!
    const ids = new Map(
      preset.workflow.nodes.map((node) => [
        node.id,
        node.kind === "start" ? start.id : crypto.randomUUID(),
      ])
    )
    const offset =
      nodes.length > 1
        ? Math.max(...nodes.map((node) => node.position.y)) + 240
        : 0
    const added: LabelWorkflow = {
      ...preset.workflow,
      nodes: preset.workflow.nodes
        .filter((node) => node.kind !== "start")
        .map((node) => ({
          ...node,
          id: ids.get(node.id)!,
          position: { x: node.position.x, y: node.position.y + offset },
        })),
      edges: preset.workflow.edges.map((edge) => ({
        ...edge,
        id: crypto.randomUUID(),
        source: ids.get(edge.source)!,
        target: ids.get(edge.target)!,
      })),
    }
    setNodes((previous) => [...previous, ...toNodes(added)])
    setEdges((previous) => [...previous, ...toEdges(added)])
    setError("")
    setTimeout(() => {
      void instance?.fitView({ padding: 0.15, duration: 300 })
    }, 50)
  }
  function validConnection(connection: Connection | Edge): boolean {
    const from = nodes.find((node) => node.id === connection.source)?.data.rule
    const to = nodes.find((node) => node.id === connection.target)?.data.rule
    if (
      !from ||
      !to ||
      from.kind === "label" ||
      to.kind === "start" ||
      from.id === to.id
    )
      return false
    const pending = [to.id]
    const seen = new Set<string>()
    while (pending.length) {
      const id = pending.pop()!
      if (id === from.id) return false
      if (seen.has(id)) continue
      seen.add(id)
      pending.push(
        ...edges.filter((edge) => edge.source === id).map((edge) => edge.target)
      )
    }
    return true
  }
  async function save() {
    setSaving(true)
    setError("")
    try {
      await labelsApi.save(scope, workflow)
      setSaved(serialized)
      toast.success("Label workflow saved")
    } catch (error) {
      setError(errorText(error))
    } finally {
      setSaving(false)
    }
  }
  async function copy() {
    const chosen = otherRepos.find(
      (item) => `${item.platform}/${item.owner}/${item.repo}` === source
    )
    if (!chosen) return
    setCopying(true)
    setError("")
    try {
      const draft = await labelsApi.copy({
        platform: chosen.platform,
        owner: chosen.owner,
        repo: chosen.repo,
      })
      setNodes(toNodes(draft.nodes.length ? draft : empty))
      setEdges(toEdges(draft))
      setEnabled(false)
      setSelectedId(null)
      setConfirmCopy(false)
      toast.success(
        "Rules copied as a draft. Review, enable and save for this repository."
      )
      setTimeout(() => {
        void instance?.fitView({ padding: 0.15, duration: 300 })
      }, 50)
    } catch (error) {
      setError(errorText(error))
      setConfirmCopy(false)
    } finally {
      setCopying(false)
    }
  }
  return (
    <div className="space-y-5">
      <div className="flex flex-wrap items-start justify-between gap-4">
        <div>
          <h2 className="text-xl font-semibold">Automatic labels</h2>
          <p className="mt-1 max-w-2xl text-sm text-muted-foreground">
            Build rules for {scope.owner}/{scope.repo}. Labels follow the PR as
            it changes, including when it grows or shrinks.
          </p>
        </div>
        <div className="flex items-center gap-4">
          <label className="flex items-center gap-2 text-sm">
            <input
              type="checkbox"
              checked={enabled}
              onChange={(event) => setEnabled(event.target.checked)}
            />
            Enabled
          </label>
          <Button onClick={save} disabled={saving || !dirty}>
            <Save />
            {saving ? "Saving…" : "Save rules"}
          </Button>
        </div>
      </div>
      <div className="flex items-center gap-2 text-xs text-muted-foreground">
        <span
          className={`size-2 rounded-full ${dirty ? "bg-amber-500" : enabled ? "bg-emerald-500" : "bg-muted-foreground"}`}
        />
        {dirty
          ? "Unsaved changes · save to apply on the next PR event"
          : enabled
            ? "Active · runs on PR opens, edits and new commits"
            : "Disabled · build and simulate before enabling"}
      </div>
      {error && (
        <p
          role="alert"
          className="rounded-lg border border-destructive/30 bg-destructive/5 p-3 text-sm text-destructive"
        >
          {error}
        </p>
      )}
      <div className="grid gap-3 md:grid-cols-3">
        {presets.map((preset) => (
          <Card key={preset.id} className="gap-2 py-4">
            <CardHeader className="px-4">
              <CardTitle className="text-sm">{preset.name}</CardTitle>
              <CardDescription className="text-xs">
                {preset.description}
              </CardDescription>
            </CardHeader>
            <CardContent className="px-4">
              <Button
                variant="outline"
                size="sm"
                onClick={() => addPreset(preset)}
              >
                <Plus />
                Add preset
              </Button>
            </CardContent>
          </Card>
        ))}
      </div>
      <div className="flex flex-wrap items-center gap-2">
        <Copy size={16} className="text-muted-foreground" />
        <select
          aria-label="Copy rules from repository"
          className={`${selectClass} max-w-sm`}
          value={source}
          onChange={(event) => setSource(event.target.value)}
        >
          <option value="">Copy rules from another repository…</option>
          {otherRepos.map((item) => {
            const key = `${item.platform}/${item.owner}/${item.repo}`
            return (
              <option key={key} value={key}>
                {item.owner}/{item.repo} ({item.platform})
              </option>
            )
          })}
        </select>
        <Button
          variant="outline"
          size="sm"
          disabled={!source || copying}
          onClick={() => setConfirmCopy(true)}
        >
          Copy into editor
        </Button>
        <span className="text-xs text-muted-foreground">
          Copies are independent.
        </span>
      </div>
      <ConfirmDialog
        open={confirmCopy}
        onOpenChange={setConfirmCopy}
        title="Replace the editor with copied rules?"
        description="This replaces the current draft. Your saved rules stay active until you save the copied workflow."
        confirmLabel="Copy rules"
        loading={copying}
        onConfirm={copy}
      />
      <div className="overflow-hidden rounded-xl border">
        <div className="flex flex-wrap items-center gap-2 border-b bg-muted/30 p-3">
          <Button
            variant="outline"
            size="sm"
            onClick={() => addNode("condition")}
          >
            <GitBranch />
            Add condition
          </Button>
          <Button variant="outline" size="sm" onClick={() => addNode("label")}>
            <Tags />
            Add label
          </Button>
          <Button
            variant="ghost"
            size="sm"
            onClick={() => {
              void instance?.fitView({ padding: 0.15, duration: 300 })
            }}
          >
            <LayoutGrid />
            Fit view
          </Button>
          <p className="ml-auto text-xs text-muted-foreground">
            Connect outputs to inputs · Select a node to edit · Select an edge
            and press Delete to disconnect
          </p>
        </div>
        <div className="grid lg:grid-cols-[1fr_290px]">
          <div className="h-[540px] min-w-0 bg-muted/10">
            <ReactFlow<FlowNode>
              nodes={nodes}
              edges={edges}
              nodeTypes={nodeTypes}
              onNodesChange={onNodesChange}
              onEdgesChange={onEdgesChange}
              onInit={setInstance}
              onNodeClick={(_, node) => setSelectedId(node.id)}
              onPaneClick={() => setSelectedId(null)}
              onConnect={(connection) =>
                setEdges((previous) =>
                  addEdge(
                    {
                      ...connection,
                      label:
                        connection.sourceHandle === "next"
                          ? undefined
                          : connection.sourceHandle === "true"
                            ? "Yes"
                            : "No",
                    },
                    previous
                  )
                )
              }
              isValidConnection={validConnection}
              fitView
              minZoom={0.2}
              maxZoom={1.5}
              deleteKeyCode={["Backspace", "Delete"]}
              colorMode="system"
            >
              <Background />
              <Controls />
            </ReactFlow>
          </div>
          <aside className="space-y-4 border-t bg-card p-4 lg:border-t-0 lg:border-l">
            {selected ? (
              <>
                <h3 className="text-sm font-semibold">
                  {selected.kind === "start"
                    ? "PR event"
                    : selected.kind === "condition"
                      ? "Edit condition"
                      : "Edit label"}
                </h3>
                {selected.condition && (
                  <ConditionEditor
                    condition={selected.condition}
                    onChange={(condition) => update({ ...selected, condition })}
                  />
                )}
                {selected.action && (
                  <>
                    <label className="block space-y-1 text-xs">
                      Label name
                      <Input
                        maxLength={50}
                        value={selected.action.name}
                        onChange={(event) =>
                          update({
                            ...selected,
                            action: {
                              ...selected.action!,
                              name: event.target.value,
                            },
                          })
                        }
                      />
                    </label>
                    <label className="block space-y-1 text-xs">
                      Color
                      <input
                        aria-label="Label color"
                        type="color"
                        className="block h-9 w-full cursor-pointer"
                        value={`#${selected.action.color}`}
                        onChange={(event) =>
                          update({
                            ...selected,
                            action: {
                              ...selected.action!,
                              color: event.target.value.slice(1),
                            },
                          })
                        }
                      />
                    </label>
                    <label className="block space-y-1 text-xs">
                      Description
                      <Input
                        maxLength={100}
                        value={selected.action.description}
                        onChange={(event) =>
                          update({
                            ...selected,
                            action: {
                              ...selected.action!,
                              description: event.target.value,
                            },
                          })
                        }
                      />
                    </label>
                    <label className="block space-y-1 text-xs">
                      When the rule stops matching
                      <select
                        className={selectClass}
                        value={selected.action.mode}
                        onChange={(event) =>
                          update({
                            ...selected,
                            action: {
                              ...selected.action!,
                              mode: event.target.value as "sync" | "add",
                            },
                          })
                        }
                      >
                        <option value="sync">Remove this label</option>
                        <option value="add">Keep this label</option>
                      </select>
                    </label>
                    <p className="text-xs text-muted-foreground">
                      Missing labels are created automatically. Existing
                      repository colors and descriptions are preserved.
                      Synchronized labels are controlled by this workflow, even
                      if added manually.
                    </p>
                  </>
                )}
                {selected.kind === "start" ? (
                  <p className="text-sm text-muted-foreground">
                    Connect this event to any number of rules. This runs
                    independently of AI reviews.
                  </p>
                ) : (
                  <Button
                    variant="outline"
                    size="sm"
                    onClick={() => {
                      setNodes((previous) =>
                        previous.filter((node) => node.id !== selected.id)
                      )
                      setEdges((previous) =>
                        previous.filter(
                          (edge) =>
                            edge.source !== selected.id &&
                            edge.target !== selected.id
                        )
                      )
                      setSelectedId(null)
                    }}
                  >
                    <Trash2 />
                    Delete node
                  </Button>
                )}
              </>
            ) : (
              <>
                <Tags className="text-muted-foreground" size={24} />
                <h3 className="text-sm font-semibold">Your rules, connected</h3>
                <p className="text-sm text-muted-foreground">
                  Choose a preset or add a condition, then connect it to a
                  label.
                </p>
                <p className="text-xs text-muted-foreground">
                  Chain conditions for AND. Connect alternative paths to the
                  same label for OR. Yes and No outputs let you create exclusive
                  size ranges.
                </p>
                <p className="text-xs text-muted-foreground">
                  Only labels configured here are managed. Other PR labels stay
                  untouched.
                </p>
              </>
            )}
          </aside>
        </div>
      </div>
      <WorkflowPreview workflow={workflow} />
    </div>
  )
}

function ConditionEditor({
  condition,
  onChange,
}: {
  condition: LabelCondition
  onChange: (value: LabelCondition) => void
}) {
  const available: LabelOperator[] = numeric.has(condition.field)
    ? ["eq", "ne", "gt", "gte", "lt", "lte"]
    : condition.field === "draft"
      ? ["eq", "ne"]
      : ["eq", "ne", "contains", "starts_with", "glob"]
  function changeField(field: LabelField) {
    onChange({
      field,
      operator: numeric.has(field) ? "lte" : field === "files" ? "glob" : "eq",
      value: numeric.has(field)
        ? 500
        : field === "draft"
          ? true
          : field === "files"
            ? "src/**"
            : "",
    })
  }
  return (
    <>
      <label className="block space-y-1 text-xs">
        PR attribute
        <select
          className={selectClass}
          value={condition.field}
          onChange={(event) => changeField(event.target.value as LabelField)}
        >
          {Object.entries(fields).map(([key, label]) => (
            <option key={key} value={key}>
              {label}
            </option>
          ))}
        </select>
      </label>
      <label className="block space-y-1 text-xs">
        Comparison
        <select
          className={selectClass}
          value={condition.operator}
          onChange={(event) =>
            onChange({
              ...condition,
              operator: event.target.value as LabelOperator,
            })
          }
        >
          {available.map((op) => (
            <option key={op} value={op}>
              {operators[op]}
            </option>
          ))}
        </select>
      </label>
      <label className="block space-y-1 text-xs">
        Value
        {condition.field === "draft" ? (
          <select
            className={selectClass}
            value={String(condition.value)}
            onChange={(event) =>
              onChange({ ...condition, value: event.target.value === "true" })
            }
          >
            <option value="true">Yes</option>
            <option value="false">No</option>
          </select>
        ) : (
          <Input
            type={numeric.has(condition.field) ? "number" : "text"}
            min={0}
            step={1}
            value={String(condition.value)}
            onChange={(event) =>
              onChange({
                ...condition,
                value: numeric.has(condition.field)
                  ? Number(event.target.value)
                  : event.target.value,
              })
            }
          />
        )}
      </label>
      <p className="text-xs text-muted-foreground">
        {condition.field === "total_lines"
          ? "Counts additions + deletions across the complete PR, including every new push."
          : condition.field === "author"
            ? "Uses the PR author, not the person who pushed. Logins ignore @ and letter case."
            : condition.field === "files"
              ? "Matches any changed path. Patterns are case-sensitive: docs/**, *.tsx. “Does not equal” requires every path to differ."
              : "Text matching is case-sensitive. Connect Yes or No to the next step."}
      </p>
    </>
  )
}

const initialFacts: LabelFacts = {
  additions: 500,
  deletions: 0,
  changed_files: 1,
  author: "octocat",
  title: "Example PR",
  description: "",
  base_branch: "main",
  head_branch: "feature/example",
  files: ["src/app.ts"],
  draft: false,
}

function WorkflowPreview({ workflow }: { workflow: LabelWorkflow }) {
  const [facts, setFacts] = useState(initialFacts)
  const [currentLabels, setCurrentLabels] = useState("size/M")
  const [result, setResult] = useState<{
    input: string
    value: LabelPreview
  } | null>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState("")
  const input = JSON.stringify({ workflow, facts, currentLabels })
  const current = result?.input === input ? result.value : null
  async function preview() {
    setBusy(true)
    setError("")
    try {
      setResult({
        input,
        value: await labelsApi.preview(
          workflow,
          facts,
          currentLabels
            .split(",")
            .map((name) => name.trim())
            .filter(Boolean)
        ),
      })
    } catch (error) {
      setError(errorText(error))
    } finally {
      setBusy(false)
    }
  }
  return (
    <Card>
      <CardHeader>
        <CardTitle className="text-base">Simulate a PR</CardTitle>
        <CardDescription>
          Test your draft without changing a real PR. Try 500 → 501 lines to
          check the size boundary.
        </CardDescription>
      </CardHeader>
      <CardContent className="space-y-4">
        <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
          {(
            [
              "additions",
              "deletions",
              "changed_files",
              "author",
              "title",
              "base_branch",
              "head_branch",
            ] as const
          ).map((field) => (
            <label key={field} className="block space-y-1 text-xs">
              {fields[field]}
              <Input
                type={numeric.has(field) ? "number" : "text"}
                min={0}
                step={1}
                value={facts[field]}
                onChange={(event) =>
                  setFacts((previous) => ({
                    ...previous,
                    [field]: numeric.has(field)
                      ? Number(event.target.value)
                      : event.target.value,
                  }))
                }
              />
            </label>
          ))}
          <label className="block space-y-1 text-xs">
            Current labels (comma-separated)
            <Input
              value={currentLabels}
              onChange={(event) => setCurrentLabels(event.target.value)}
            />
          </label>
        </div>
        <details>
          <summary className="cursor-pointer text-sm text-muted-foreground">
            Description, file paths and draft state
          </summary>
          <div className="mt-3 grid gap-3 sm:grid-cols-2">
            <label className="space-y-1 text-xs">
              Description
              <Textarea
                value={facts.description}
                onChange={(event) =>
                  setFacts((previous) => ({
                    ...previous,
                    description: event.target.value,
                  }))
                }
              />
            </label>
            <label className="space-y-1 text-xs">
              Changed paths (one per line)
              <Textarea
                value={facts.files.join("\n")}
                onChange={(event) =>
                  setFacts((previous) => ({
                    ...previous,
                    files: event.target.value.split("\n").filter(Boolean),
                  }))
                }
              />
            </label>
            <label className="flex items-center gap-2 text-sm">
              <input
                type="checkbox"
                checked={facts.draft}
                onChange={(event) =>
                  setFacts((previous) => ({
                    ...previous,
                    draft: event.target.checked,
                  }))
                }
              />
              Draft PR
            </label>
          </div>
        </details>
        <div className="flex flex-wrap items-center gap-3">
          <Button variant="outline" onClick={preview} disabled={busy}>
            <Play />
            {busy ? "Simulating…" : "Simulate rules"}
          </Button>
          <span className="text-sm text-muted-foreground">
            {facts.additions + facts.deletions} total lines
          </span>
          {result && !current && (
            <span className="text-xs text-amber-600">
              Inputs changed. Simulate again.
            </span>
          )}
        </div>
        {error && (
          <p role="alert" className="text-sm text-destructive">
            {error}
          </p>
        )}
        {current && (
          <div
            aria-live="polite"
            className="grid gap-3 rounded-lg border bg-muted/20 p-4 text-sm sm:grid-cols-3"
          >
            <div>
              <p className="mb-1 text-xs text-muted-foreground">Add</p>
              {current.add.join(", ") || "No additions"}
            </div>
            <div>
              <p className="mb-1 text-xs text-muted-foreground">Remove</p>
              {current.remove.join(", ") || "No removals"}
            </div>
            <div>
              <p className="mb-1 text-xs text-muted-foreground">
                Matching labels
              </p>
              {current.evaluation.labels
                .map((label) => label.name)
                .join(", ") || "None"}
            </div>
          </div>
        )}
      </CardContent>
    </Card>
  )
}
