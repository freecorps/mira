import {
  deleteJson,
  fetchJson,
  fetchText,
  patchJson,
  postJson,
  postText,
  putJson,
} from "./http"
import type {
  LearnedRuleModel,
  LearningCandidateModel,
  OrgLearnedRuleModel,
  RuleModel,
} from "./types"

// Custom rules (global + per-repo) and learned rules.
export const rulesApi = {
  // Learned rules. status: "approved" | "pending" | "rejected" | "" (all)
  listLearnedRules: (status = "") =>
    fetchJson<OrgLearnedRuleModel[]>(
      status
        ? `/api/learned-rules?status=${encodeURIComponent(status)}`
        : `/api/learned-rules`
    ),

  listRepoLearnedRules: (owner: string, repo: string) =>
    fetchJson<LearnedRuleModel[]>(`/api/repos/${owner}/${repo}/learned-rules`),

  listLearningCandidates: (status = "") =>
    fetchJson<LearningCandidateModel[]>(
      status
        ? `/api/learning-candidates?status=${encodeURIComponent(status)}`
        : `/api/learning-candidates`
    ),

  getLearningCandidate: (owner: string, repo: string, id: number) =>
    fetchJson<LearningCandidateModel>(
      `/api/learning-candidates/${owner}/${repo}/${id}`
    ),

  approveLearningCandidate: (owner: string, repo: string, id: number) =>
    postJson<{ ok: boolean; rule_id: number | null }>(
      `/api/learning-candidates/${owner}/${repo}/${id}/approve`,
      {}
    ),

  rejectLearningCandidate: (owner: string, repo: string, id: number) =>
    postJson<{ ok: boolean }>(
      `/api/learning-candidates/${owner}/${repo}/${id}/reject`,
      {}
    ),

  updateLearningCandidate: (
    owner: string,
    repo: string,
    id: number,
    body: {
      rule_text: string
      rationale: string
      category: string
      scope_type: string
      scope_value: string
      language?: string
    }
  ) =>
    putJson<LearningCandidateModel>(
      `/api/learning-candidates/${owner}/${repo}/${id}`,
      body
    ),

  getLearnedRule: (owner: string, repo: string, id: number) =>
    fetchJson<OrgLearnedRuleModel>(`/api/learned-rules/${owner}/${repo}/${id}`),

  approveLearnedRule: (owner: string, repo: string, id: number) =>
    postJson<{ ok: boolean }>(
      `/api/learned-rules/${owner}/${repo}/${id}/approve`,
      {}
    ),

  rejectLearnedRule: (owner: string, repo: string, id: number) =>
    postJson<{ ok: boolean }>(
      `/api/learned-rules/${owner}/${repo}/${id}/reject`,
      {}
    ),

  setLearnedRuleActive: (
    owner: string,
    repo: string,
    id: number,
    active: boolean
  ) =>
    patchJson<{ ok: boolean }>(
      `/api/learned-rules/${owner}/${repo}/${id}/active`,
      { active }
    ),

  createLearnedRule: (
    owner: string,
    repo: string,
    body: { rule_text: string; category: string; path_pattern?: string }
  ) => postJson<LearnedRuleModel>(`/api/learned-rules/${owner}/${repo}`, body),

  updateLearnedRule: (
    owner: string,
    repo: string,
    id: number,
    body: {
      rule_text: string
      category: string
      path_pattern?: string
      scope_type?: string
      scope_value?: string
      rationale?: string
    }
  ) =>
    putJson<{ ok: boolean }>(`/api/learned-rules/${owner}/${repo}/${id}`, body),

  deleteLearnedRule: (owner: string, repo: string, id: number) =>
    deleteJson(`/api/learned-rules/${owner}/${repo}/${id}`),

  exportLearnedRules: (owner: string, repo: string) =>
    fetchText(`/api/learned-rules/${owner}/${repo}/export.yaml`),

  importLearnedRules: (owner: string, repo: string, yaml: string) =>
    postText<{ ok: boolean; imported: number }>(
      `/api/learned-rules/${owner}/${repo}/import.yaml`,
      yaml
    ),

  // Global rules
  listGlobalRules: () => fetchJson<RuleModel[]>("/api/rules/global"),

  createGlobalRule: (title: string, content: string) =>
    postJson<RuleModel>("/api/rules/global", { title, content }),

  updateGlobalRule: (id: number, title: string, content: string) =>
    putJson<RuleModel>(`/api/rules/global/${id}`, { title, content }),

  deleteGlobalRule: (id: number) => deleteJson(`/api/rules/global/${id}`),

  toggleGlobalRule: (id: number) =>
    patchJson<RuleModel>(`/api/rules/global/${id}/toggle`),

  // Per-repo rules
  listRepoRules: (owner: string, repo: string) =>
    fetchJson<RuleModel[]>(`/api/repos/${owner}/${repo}/rules`),

  createRepoRule: (
    owner: string,
    repo: string,
    title: string,
    content: string
  ) =>
    postJson<RuleModel>(`/api/repos/${owner}/${repo}/rules`, {
      title,
      content,
    }),

  updateRepoRule: (
    owner: string,
    repo: string,
    id: number,
    title: string,
    content: string
  ) =>
    putJson<RuleModel>(`/api/repos/${owner}/${repo}/rules/${id}`, {
      title,
      content,
    }),

  deleteRepoRule: (owner: string, repo: string, id: number) =>
    deleteJson(`/api/repos/${owner}/${repo}/rules/${id}`),
}
