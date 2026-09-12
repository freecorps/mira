import { fetchJson, postJson, putJson } from "./http"

export type LabelField =
  | "total_lines"
  | "additions"
  | "deletions"
  | "changed_files"
  | "author"
  | "title"
  | "description"
  | "base_branch"
  | "head_branch"
  | "files"
  | "draft"
export type LabelOperator =
  | "eq"
  | "ne"
  | "gt"
  | "gte"
  | "lt"
  | "lte"
  | "contains"
  | "starts_with"
  | "glob"
export interface LabelCondition {
  field: LabelField
  operator: LabelOperator
  value: string | number | boolean
}
export interface LabelAction {
  name: string
  color: string
  description: string
  mode: "sync" | "add"
}
export interface LabelNode {
  id: string
  kind: "start" | "condition" | "label"
  position: { x: number; y: number }
  condition?: LabelCondition | null
  action?: LabelAction | null
}
export interface LabelEdge {
  id: string
  source: string
  target: string
  branch: "next" | "true" | "false"
}
export interface LabelWorkflow {
  version: 1
  enabled: boolean
  nodes: LabelNode[]
  edges: LabelEdge[]
}
export interface LabelWorkflowSnapshot {
  workflow: LabelWorkflow
  revision: string
}
export interface LabelPreset {
  id: string
  name: string
  description: string
  workflow: LabelWorkflow
}
export interface LabelScope {
  owner: string
  repo: string
  platform: string
}
export interface LabelPreview {
  evaluation: {
    matched_nodes: string[]
    labels: LabelAction[]
    managed_labels: string[]
    total_lines: number
  }
  add: string[]
  remove: string[]
}
export interface LabelFacts {
  additions: number
  deletions: number
  changed_files: number
  author: string
  title: string
  description: string
  base_branch: string
  head_branch: string
  files: string[]
  draft: boolean
}
const query = (scope: LabelScope) =>
  new URLSearchParams({ ...scope }).toString()
export const labelsApi = {
  get: (scope: LabelScope) =>
    fetchJson<LabelWorkflowSnapshot>(`/api/labels/workflow?${query(scope)}`),
  save: (scope: LabelScope, workflow: LabelWorkflow, revision: string) =>
    putJson<LabelWorkflowSnapshot>(`/api/labels/workflow?${query(scope)}`, {
      workflow,
      revision,
    }),
  presets: () => fetchJson<LabelPreset[]>("/api/labels/presets"),
  copy: (source: LabelScope) =>
    postJson<LabelWorkflow>("/api/labels/copy", source),
  preview: (
    workflow: LabelWorkflow,
    facts: LabelFacts,
    current_labels: string[]
  ) =>
    postJson<LabelPreview>("/api/labels/preview", {
      workflow,
      facts,
      current_labels,
    }),
}
