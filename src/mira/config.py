"""Configuration loading and validation for Mira."""

from __future__ import annotations

import ipaddress
import logging
import os
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import yaml
from pydantic import BaseModel, Field, field_validator

from mira.exceptions import ConfigError

logger = logging.getLogger(__name__)

# `.mira.yaml` is the canonical per-repo override filename. `.mira.yaml`
# is accepted for backward compat with repos that committed it before the
# 0.1.1 standardization on the .yaml extension.
_DEFAULT_CONFIG_FILENAMES = (".mira.yaml", ".mira.yml")


def _is_local_host(host: str) -> bool:
    """Loopback, private/link-local IP literals, and dotless hostnames
    (docker-compose services) — where a plain-http endpoint is legitimate."""
    if host == "localhost" or "." not in host:
        return True
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False
    return ip.is_loopback or ip.is_private or ip.is_link_local


class LLMConfig(BaseModel):
    model: str = "anthropic/claude-sonnet-4-6"
    fallback_model: str | None = None
    # Optional per-purpose overrides. Fall back to `model` if not set.
    indexing_model: str | None = None
    review_model: str | None = None
    # Optional dedicated model for the security review pass. Falls back to
    # `review_model`, then `model` — deliberately never to `indexing_model`:
    # the security sweep is the highest-stakes pass and must not silently
    # downgrade to the indexing tier.
    security_model: str | None = None
    # Extended-thinking effort for reviews ("low"/"medium"/"high"; None/"off" =
    # no reasoning). `review_reasoning_effort` is the mira.yaml-level override;
    # `reasoning_effort` is the resolved value the provider reads (set by
    # `llm_config_for`, the same way `model` is resolved from `review_model`).
    review_reasoning_effort: str | None = None
    reasoning_effort: str | None = None
    temperature: float = 0.2
    max_tokens: int = 4096
    max_context_tokens: int = 120_000
    # Provider selection. "openai" uses any OpenAI-compatible endpoint (default).
    # "bedrock" uses AWS Bedrock Converse API directly (requires boto3).
    provider: str = "openai"
    # Protocol dialect for the OpenAI-compatible endpoint: "chat"
    # (Chat Completions, default) or "responses" (OpenAI Responses API).
    # Only meaningful when `provider` is "openai" — bedrock ignores it
    # (create_llm checks provider first).
    api_style: str = "chat"
    # Endpoint configuration. Defaults to OpenRouter but any OpenAI-compatible
    # chat-completions endpoint works — vLLM, Ollama, LiteLLM proxy, LocalAI,
    # llama.cpp server, Together, Fireworks, Groq, etc. Set api_key_env to ""
    # for local endpoints that don't require auth.
    base_url: str = "https://openrouter.ai/api/v1"
    api_key_env: str = "OPENROUTER_API_KEY"
    # AWS Bedrock settings. Auth uses the standard AWS credential chain
    # (env vars, instance profile, ECS task role, SSO).
    region: str = "us-east-1"
    aws_profile: str | None = None
    # Retry and timeout configuration for LLM calls.
    # Defaults match the previous hardcoded values to preserve existing behavior.
    max_retries: int = Field(default=3, ge=1)
    request_timeout: int = Field(default=120, ge=1)
    retry_min_wait: int = Field(default=2, ge=0)
    retry_max_wait: int = Field(default=30, ge=0)

    @field_validator("base_url")
    @classmethod
    def _validate_base_url(cls, v: str) -> str:
        parsed = urlparse(v)
        if parsed.scheme not in ("http", "https") or not parsed.hostname:
            raise ValueError(f"llm.base_url must be an http(s) URL, got {v!r}")
        if parsed.scheme == "http" and not _is_local_host(parsed.hostname):
            raise ValueError(
                f"llm.base_url {v!r} uses plain http to a public host — use https "
                "(http is allowed only for localhost, private IPs, and dotless "
                "hostnames like docker-compose services)"
            )
        return v


class FilterConfig(BaseModel):
    confidence_threshold: float = Field(default=0.7, ge=0.0, le=1.0)
    # Per-category floors layered over confidence_threshold (the higher wins).
    # Lets noisy categories (e.g. "security" from the cheap-model pass) be
    # held to a stricter bar without raising the global floor.
    category_confidence_thresholds: dict[str, float] = Field(default_factory=dict)
    max_comments: int = Field(default=5, ge=1)
    min_severity: str = "nitpick"
    exclude_patterns: list[str] = Field(
        default_factory=lambda: [
            "*.lock",
            "*.lockb",
            "package-lock.json",
            "yarn.lock",
            "pnpm-lock.yaml",
            "Pipfile.lock",
            "poetry.lock",
            "go.sum",
            "*.min.js",
            "*.min.css",
            "*.map",
            "*.svg",
            "*.png",
            "*.jpg",
            "*.jpeg",
            "*.gif",
            "*.ico",
            "*.woff",
            "*.woff2",
            "*.ttf",
            "*.eot",
            "*.pdf",
            "*.zip",
            "*.tar.gz",
        ]
    )
    exclude_deleted: bool = True
    max_files: int = 50
    # Only auto-review PRs whose author (payload `sender.login` /
    # `user.username`) is in this list. Empty list = review all (default).
    # Bot-self events are always excluded regardless of this list.
    allowed_authors: list[str] = Field(default_factory=list)
    # Never auto-review PRs from these authors. Takes precedence over
    # allowed_authors. A trailing `[bot]` suffix on the payload login is
    # stripped by the dispatcher check so that `dependabot` here matches
    # `dependabot[bot]` in a webhook payload.
    blocked_authors: list[str] = Field(default_factory=list)


class OverlapConfig(BaseModel):
    """Cross-PR overlap detection ("stepping on each other's toes").

    While reviewing a PR, Mira compares it against other open PRs in the repo
    and flags ones that touch the same code (merge-conflict risk) or pursue the
    same goal (duplicate effort). A cheap deterministic pre-filter runs first;
    only the survivors cost an LLM call.
    """

    enabled: bool = True
    # Cap on how many recently-updated open PRs to compare against, to bound
    # GitHub API calls and LLM cost on busy repos.
    max_candidates: int = Field(default=20, ge=1, le=100)
    # Verdicts below this confidence are dropped (LLM-scored, 0..1).
    confidence_floor: float = Field(default=0.6, ge=0.0, le=1.0)
    # Pre-filter keeps a candidate with no shared files/symbols only if its
    # title is at least this Jaccard-similar — the duplicate-effort lane.
    title_similarity_threshold: float = Field(default=0.4, ge=0.0, le=1.0)


class VerdictConfig(BaseModel):
    """Whether Mira submits a real review event, not just comments.

    Off by default: an APPROVE from a GitHub App counts toward branch-protection
    approvals, and a REQUEST_CHANGES blocks the merge until it's superseded.
    Both are opt-in decisions for the deployment, not defaults to inherit.

      "off"             — comment only (historical behaviour)
      "approve"         — approve clean PRs; stay silent when findings exceed
                          the ceiling (the inline comments already say it)
      "request_changes" — also submit REQUEST_CHANGES on findings above it
    """

    mode: str = "off"
    # Highest severity tolerated in an approved PR. "suggestion" approves a PR
    # whose only findings are suggestions and nitpicks; "nitpick" demands a
    # completely clean pass.
    approve_max_severity: str = "suggestion"
    # Never approve when files were skipped because the diff blew past
    # max_diff_size — approving a partially-read PR is the worst failure mode.
    require_all_files_reviewed: bool = True

    @field_validator("mode")
    @classmethod
    def _valid_mode(cls, v: str) -> str:
        allowed = {"off", "approve", "request_changes"}
        if v not in allowed:
            raise ValueError(f"review.verdict.mode must be one of {sorted(allowed)}, got {v!r}")
        return v

    @field_validator("approve_max_severity")
    @classmethod
    def _valid_severity(cls, v: str) -> str:
        allowed = {"blocker", "warning", "suggestion", "nitpick"}
        if v not in allowed:
            raise ValueError(
                f"review.verdict.approve_max_severity must be one of {sorted(allowed)}, got {v!r}"
            )
        return v


class ReviewConfig(BaseModel):
    context_lines: int = Field(default=3, ge=0)
    # Total diff size cap. Above this, the diff is *not* truncated arbitrarily —
    # files are ranked by priority and the lowest-priority files are skipped
    # until the diff fits. Skipped files are listed in the walkthrough so the
    # user can invoke `@miracodeai review-rest` to review them.
    max_diff_size: int = 250_000
    # Per-file size cap. A single huge file (lockfile, generated SDK, etc.)
    # gets skipped before chunking even starts.
    max_file_size: int = 50_000
    # Hard ceiling on chunks per single review pass. If the diff would split
    # into more chunks, only the top-priority N are reviewed; the rest are
    # listed as skipped.
    max_chunks_per_review: int = Field(default=5, ge=1, le=20)
    include_summary: bool = True
    focus_only_on_problems: bool = False
    walkthrough: bool = True
    walkthrough_sequence_diagram: bool = True
    code_context: bool = True
    context_token_budget: int = 8_000
    max_concurrent_chunks: int = Field(default=5, ge=1, le=20)
    # Review each chunk N times and keep only majority-vote findings.
    # 1 = off (single pass, exact current behavior). 3 is the sweet spot:
    # variance FPs flicker across runs, real findings recur. Runs fire in
    # parallel so wall clock stays ~flat, but token cost multiplies by N —
    # this is the opt-in "thorough" tier, not the default.
    ensemble_runs: int = Field(default=1, ge=1, le=5)
    # Sampling temperature for the extra ensemble runs (the first run keeps
    # the configured llm.temperature). Mild diversity makes the vote useful.
    ensemble_temperature: float = Field(default=0.3, ge=0.0, le=1.0)

    # Run a second-pass LLM critique on each draft comment before posting.
    # The critic asks "is this analysis actually correct? Cite specific
    # lines that prove it." Comments that fail the critique are dropped.
    # Disable for faster reviews where the extra wall-clock time matters
    # more than catching confident-but-wrong findings.
    self_critique: bool = True

    # Run a dedicated security review pass in parallel with the main review.
    # Uses the security tier (`llm.security_model`, falling back to the
    # review model). The main pass on the review tier still catches deeper
    # chained-inference security bugs — this pass is the focused pattern
    # sweep (XSS, injection, auth bypass, CSRF, SSRF, origin validation,
    # deserialization, crypto) on top. Findings merge into the main review's
    # comments and go through the same noise filter.
    security_pass: bool = True

    # Deterministic CVE check on changed dependency manifests: packages added
    # or version-bumped by the PR are queried against OSV.dev at review time
    # (the background poller only re-scans the repo hourly, post-merge). No
    # LLM involved — one batch HTTP request per PR with manifest changes.
    osv_scan: bool = True

    # Give the reviewer LLM tools (`read_file`, `grep_repo`) to fetch
    # cross-file context on demand. On unindexed repos this closes the
    # Java/Go gaps JIT pre-fetch can't reach; on indexed repos it lets the
    # reviewer trace callers and dispatch points beyond the pre-fetched
    # index context. Disable to force single-shot reviews (cheaper, less
    # thorough).
    agentic_tools: bool = True

    # Whether the JIT cross-file resolver should attempt Java + Go imports.
    # Resolution for those languages is heuristic (we can't see the build
    # system), and a wrong-file pick pollutes the prompt with off-topic
    # symbols. Toggle off when measuring whether Java/Go JIT is helping vs
    # hurting on a given codebase. The agentic loop still covers cross-file
    # needs for Java/Go on the unindexed path when this is False.
    jit_java_go: bool = True

    # Render the cross-repo "Blast Radius" section in the walkthrough comment.
    # Lists dependent repos that import code touched by this PR. Disable to
    # skip the relationship-store lookup and trim the walkthrough.
    blast_radius: bool = True

    # Warn when a PR adds a dependency that duplicates the functionality of one
    # already in the repo (e.g. a second table or HTTP-client library). Runs a
    # dedicated indexing-tier pass, but only when the PR changes a manifest file
    # (package.json, pyproject.toml, go.mod, …) — no LLM call otherwise.
    dependency_overlap: bool = True

    # Cross-PR overlap detection — flag other open PRs that step on this one
    # (same files = merge-conflict risk, or same goal = duplicate effort).
    overlap: OverlapConfig = Field(default_factory=OverlapConfig)

    # Submit an approve / request-changes review event alongside the comments.
    verdict: VerdictConfig = Field(default_factory=VerdictConfig)

    # Automatically resolve bot review threads that the LLM verifies as fixed
    # on each review pass. Disable to leave all bot comments open until a human
    # resolves them (user-initiated reject/resolve replies still work).
    auto_resolve_conversations: bool = True

    # Auto-review on every push (`synchronize` event). When False, Mira only
    # reviews when the PR is opened or reopened. Subsequent commits are
    # ignored unless you comment `@bot_name review` to trigger a manual pass.
    # Disabling this saves tokens and reduces noise when you batch commits
    # locally before pushing — only the final diff gets reviewed.
    review_on_synchronize: bool = True


class IndexConfig(BaseModel):
    # Skip indexing any file larger than this (bytes). Generated SDKs, vendored
    # bundles and large test fixtures burn indexing tokens for little value.
    # Defaults to the previous hard-coded tarball cap (1 MB) so it's a no-op
    # until lowered; 0 disables the limit. In bytes, matching review.max_file_size.
    max_file_size: int = Field(default=1024 * 1024, ge=0)


class ProviderConfig(BaseModel):
    type: str = "github"


class DatabaseConfig(BaseModel):
    url: str = ""  # empty = SQLite fallback. "postgresql://user:pass@host:5432/mira"
    admin_password: str = (
        ""  # initial admin password; empty = generated on first start, written to a 0600 file
    )


class LearningConfig(BaseModel):
    """Feature gates and conservative limits for feedback-driven learning."""

    feedback_v2: bool = True
    learning_synthesis: bool = True
    # Opt-in only. When disabled, every synthesized candidate requires an
    # explicit approval before it can affect a review.
    learning_auto_apply: bool = False
    max_rules_per_review: int = Field(default=10, ge=1, le=50)
    min_evidence_path: int = Field(default=1, ge=1)
    min_evidence_language: int = Field(default=3, ge=1)
    min_evidence_repo: int = Field(default=5, ge=1)
    min_evidence_org: int = Field(default=10, ge=1)

    # Phase 3 — continuous evaluation. The kill switch stops Mira recording
    # rule exposures; the review itself runs exactly the same either way, since
    # recording happens after the comments have already been posted.
    evaluation_analytics: bool = True
    # A rule needs this many exposures before Mira will suggest a downgrade.
    # Below it there simply isn't enough evidence to call a regression.
    min_exposures_for_regression: int = Field(default=20, ge=1)
    # And this many *decisive* signals. Exposures alone are not evidence: a
    # rule can reach the exposure floor on review-scoped rows and then hit a
    # 100% negative rate from a single thumbs-down.
    min_decisive_for_regression: int = Field(default=5, ge=1)
    # Share of *decisive* signals (positive + negative) that must be negative
    # before a rule is flagged. Unobserved findings never enter this ratio.
    regression_negative_rate: float = Field(default=0.5, ge=0.0, le=1.0)
    # Above this the suggestion escalates from "downgrade" to "disable".
    # Mira still only suggests; nothing is disabled without an admin acting.
    regression_disable_rate: float = Field(default=0.8, ge=0.0, le=1.0)
    # Default window, in days, for the before/after activation comparison.
    evaluation_window_days: int = Field(default=30, ge=1, le=365)


class RiskWeights(BaseModel):
    """Points each observable fact adds to a PR's risk score.

    Integers only, so the same inputs produce a byte-identical score on every
    replica. Weights at or above 100 are effectively vetoes on their own, which
    is deliberate for the facts that must never be outweighed by a clean diff.
    """

    # Findings the review left open.
    warning_finding: int = Field(default=8, ge=0, le=100)
    warning_cap: int = Field(default=32, ge=0, le=100)
    suggestion_finding: int = Field(default=1, ge=0, le=100)
    suggestion_cap: int = Field(default=6, ge=0, le=100)
    security_finding: int = Field(default=15, ge=0, le=100)

    # Size. Small diffs cost nothing; the free allowances keep an ordinary
    # change at zero so the score stays readable.
    size_free_files: int = Field(default=5, ge=0)
    size_per_file: int = Field(default=1, ge=0, le=100)
    size_file_cap: int = Field(default=20, ge=0, le=100)
    size_free_lines: int = Field(default=100, ge=0)
    size_per_100_lines: int = Field(default=2, ge=0, le=100)
    size_line_cap: int = Field(default=20, ge=0, le=100)

    # What Mira could not see.
    unreviewed_paths: int = Field(default=15, ge=0, le=100)
    index_not_ready: int = Field(default=10, ge=0, le=100)
    generated_heavy: int = Field(default=5, ge=0, le=100)
    dependency_manifest: int = Field(default=8, ge=0, le=100)

    # Who is asking, and whether the platform agrees the change is sound.
    unknown_association: int = Field(default=25, ge=0, le=100)
    first_time_contributor: int = Field(default=15, ge=0, le=100)
    ci_not_success: int = Field(default=30, ge=0, le=100)

    # Facts that must never be outscored by an otherwise clean PR.
    protected_path: int = Field(default=100, ge=0, le=100)
    codeowner_path: int = Field(default=40, ge=0, le=100)
    open_blocker: int = Field(default=100, ge=0, le=100)
    human_changes_requested: int = Field(default=100, ge=0, le=100)


class GateRepoPolicy(BaseModel):
    """Per-repository overrides, keyed ``owner/repo`` under ``gate.repositories``.

    Set only what differs. ``None`` means "inherit"; a list set to ``[]`` means
    "explicitly empty", which is how a repository opts out of an inherited
    requirement rather than accidentally inheriting it forever.
    """

    enabled: bool | None = None
    mode: str | None = None
    protected_paths: list[str] | None = None
    extra_protected_paths: list[str] = Field(default_factory=list)
    allowed_base_branches: list[str] | None = None
    blocked_base_branches: list[str] | None = None
    required_labels: list[str] | None = None
    blocked_labels: list[str] | None = None
    allowed_author_associations: list[str] | None = None
    max_changed_files: int | None = Field(default=None, ge=1)
    max_changed_lines: int | None = Field(default=None, ge=1)
    risk_threshold: int | None = Field(default=None, ge=0, le=100)
    codeowners: str | None = None
    request_changes_on_blockers: bool | None = None

    @field_validator("mode")
    @classmethod
    def _valid_mode(cls, v: str | None) -> str | None:
        if v is not None and v not in {"off", "shadow", "enforce"}:
            raise ValueError("gate.repositories[].mode must be off, shadow or enforce")
        return v

    @field_validator("codeowners")
    @classmethod
    def _valid_codeowners(cls, v: str | None) -> str | None:
        if v is not None and v not in {"off", "risk", "block"}:
            raise ValueError("gate.repositories[].codeowners must be off, risk or block")
        return v

    @field_validator("protected_paths", "extra_protected_paths")
    @classmethod
    def _valid_patterns(cls, v: list[str] | None) -> list[str] | None:
        if v:
            _validate_path_patterns(v, "gate.repositories[].protected_paths")
        return v


class GateConfig(BaseModel):
    """The risk-oriented merge gate (Phase 4).

    Off by default, and never anything else by default. An approval from Mira
    can satisfy a branch-protection rule, so turning it on is a decision a
    deployment makes deliberately — and the recommended first step is
    ``shadow``, which records exactly what it *would* have done without doing
    any of it.

      "off"      — the gate does not run.
      "shadow"   — evaluate, explain and record; never approve. Dry run.
      "enforce"  — the same decision, plus a real approval when it says so.

    Everything in here is deployment configuration. Nothing in a pull request —
    its title, body, diff, labels or CI logs — can change any of it. Labels and
    branches are *inputs the operator chose to consult*, and consulting them can
    only ever make the gate more conservative or take a PR out of scope; no
    label grants an approval on its own.
    """

    mode: str = "off"
    # Hard global disable, independent of `mode` and of every per-repo
    # override. Exists so an operator can stop the gate everywhere in one edit
    # during an incident without reconstructing the policy afterwards.
    kill_switch: bool = False
    # Recorded with every decision. Bump it when changing policy semantics so
    # old decisions stay attributable to the policy that produced them.
    policy_version: str = "gate-v1"

    # ── Eligibility ──────────────────────────────────────────────────────
    # Empty allowlists mean "any"; blocklists always win over allowlists.
    allowed_base_branches: list[str] = Field(default_factory=list)
    blocked_base_branches: list[str] = Field(default_factory=list)
    required_labels: list[str] = Field(default_factory=list)
    blocked_labels: list[str] = Field(
        default_factory=lambda: ["do-not-merge", "wip", "hold", "mira-paused"]
    )
    allowed_authors: list[str] = Field(default_factory=list)
    blocked_authors: list[str] = Field(default_factory=list)
    # Platform association of the PR author. An association the platform cannot
    # report is never treated as sufficient.
    allowed_author_associations: list[str] = Field(
        default_factory=lambda: ["OWNER", "MEMBER", "COLLABORATOR"]
    )
    skip_draft_prs: bool = True
    max_changed_files: int = Field(default=20, ge=1)
    max_changed_lines: int = Field(default=500, ge=1)
    # Generated output is excluded from the size budget (a lockfile bump is not
    # a change a human has to read) and a diff made only of it is out of scope.
    generated_paths: list[str] = Field(default_factory=list)
    size_excludes_generated: bool = True

    # ── Protected paths and CODEOWNERS ───────────────────────────────────
    # Replaces the built-in list when set; `extra_protected_paths` adds to
    # whichever list is in effect.
    protected_paths: list[str] | None = None
    extra_protected_paths: list[str] = Field(default_factory=list)
    # "off"   — do not read CODEOWNERS at all (default: it is an integration).
    # "risk"  — an owned path adds risk but is not on its own disqualifying.
    # "block" — an owned path is never auto-approved. The conservative reading,
    #           and what "on" should mean for most deployments.
    codeowners: str = "off"

    # ── Completeness requirements ────────────────────────────────────────
    require_ci_success: bool = True
    require_all_files_reviewed: bool = True
    require_index_ready: bool = True
    approve_max_severity: str = "suggestion"

    # ── Risk ─────────────────────────────────────────────────────────────
    risk_threshold: int = Field(default=25, ge=0, le=100)
    risk_medium_at: int = Field(default=20, ge=0, le=100)
    risk_high_at: int = Field(default=50, ge=0, le=100)
    weights: RiskWeights = Field(default_factory=RiskWeights)

    # ── Actions ──────────────────────────────────────────────────────────
    # Submit REQUEST_CHANGES when a blocker is open. Off by default: it holds
    # the merge box until superseded, which is a deployment's decision, not a
    # default to inherit. Never submitted over an existing human review.
    request_changes_on_blockers: bool = False
    # Publish the decision as a check run / commit status when the provider
    # supports one. Neutral in shadow mode — an explanation, not a verdict.
    publish_status: bool = True
    # Also post the public explanation as a PR comment, updated in place.
    comment: bool = False

    # ── Overrides ────────────────────────────────────────────────────────
    allow_overrides: bool = True
    # Forcing an approval by hand is a separate, opt-in capability from
    # revoking one. Revocation is always available to an authorized admin.
    allow_approval_override: bool = False
    # Admin usernames permitted to override a decision. Empty = every admin.
    # Separates "can administer Mira" from "can move a merge decision".
    override_admins: list[str] = Field(default_factory=list)

    # ── Budget ───────────────────────────────────────────────────────────
    # Wall-clock ceiling for gathering inputs from the platform. Exceeding it
    # is an `error` decision, which never approves.
    timeout_seconds: float = Field(default=20.0, gt=0, le=300)

    # ── Per-repository policy ────────────────────────────────────────────
    repositories: dict[str, GateRepoPolicy] = Field(default_factory=dict)

    @field_validator("mode")
    @classmethod
    def _valid_mode(cls, v: str) -> str:
        allowed = {"off", "shadow", "enforce"}
        if v not in allowed:
            raise ValueError(f"gate.mode must be one of {sorted(allowed)}, got {v!r}")
        return v

    @field_validator("codeowners")
    @classmethod
    def _valid_codeowners(cls, v: str) -> str:
        allowed = {"off", "risk", "block"}
        if v not in allowed:
            raise ValueError(f"gate.codeowners must be one of {sorted(allowed)}, got {v!r}")
        return v

    @field_validator("approve_max_severity")
    @classmethod
    def _valid_severity(cls, v: str) -> str:
        allowed = {"blocker", "warning", "suggestion", "nitpick"}
        if v not in allowed:
            raise ValueError(
                f"gate.approve_max_severity must be one of {sorted(allowed)}, got {v!r}"
            )
        return v

    @field_validator("protected_paths", "extra_protected_paths", "generated_paths")
    @classmethod
    def _valid_patterns(cls, v: list[str] | None) -> list[str] | None:
        if v:
            _validate_path_patterns(v, "gate path pattern")
        return v


def _validate_path_patterns(patterns: list[str], label: str) -> None:
    """Compile gate path patterns at config load, never at decision time.

    A pattern the matcher cannot read has no safe runtime interpretation:
    ignoring it silently un-protects a path, and vetoing everything takes the
    install down on a typo. Failing the config load is the only reading that
    stays honest, so it happens here.
    """
    from mira.gate.paths import PatternError, compile_pattern

    for pattern in patterns:
        try:
            compile_pattern(pattern)
        except PatternError as exc:
            raise ValueError(f"{label}: {exc}") from exc


class MiraConfig(BaseModel):
    llm: LLMConfig = Field(default_factory=LLMConfig)
    filter: FilterConfig = Field(default_factory=FilterConfig)
    review: ReviewConfig = Field(default_factory=ReviewConfig)
    index: IndexConfig = Field(default_factory=IndexConfig)
    provider: ProviderConfig = Field(default_factory=ProviderConfig)
    database: DatabaseConfig = Field(default_factory=DatabaseConfig)
    learning: LearningConfig = Field(default_factory=LearningConfig)
    gate: GateConfig = Field(default_factory=GateConfig)


def find_config_file(start_dir: Path | None = None) -> Path | None:
    """Walk up from start_dir looking for `.mira.yaml` (or legacy `.mira.yaml`)."""
    current = start_dir or Path.cwd()
    for directory in [current, *current.parents]:
        for name in _DEFAULT_CONFIG_FILENAMES:
            candidate = directory / name
            if candidate.is_file():
                return candidate
    return None


def _load_yaml(path: Path) -> dict[str, Any]:
    """Read and parse a YAML config file, returning the top-level dict."""
    try:
        raw = path.read_text(encoding="utf-8")
        parsed = yaml.safe_load(raw)
        if parsed and isinstance(parsed, dict):
            return dict(parsed)
        return {}
    except yaml.YAMLError as e:
        raise ConfigError(f"Invalid YAML in {path}: {e}") from e


_global_defaults: dict[str, Any] = {}


def set_global_defaults(config_path: Path | str) -> MiraConfig:
    """Load a deployment-wide config file once at server startup.

    Subsequent `load_config()` calls deep-merge per-repo `.mira.yaml` (and
    env-var fallbacks) over these defaults.
    """
    global _global_defaults
    path = Path(config_path)
    if not path.is_file():
        raise ConfigError(f"Config file not found: {path}")
    _global_defaults = _load_yaml(path)
    # Validate eagerly so a malformed file fails server boot, not first review.
    return load_config()


def _deep_merge(base: dict[str, Any], overlay: dict[str, Any]) -> dict[str, Any]:
    """Right-biased deep merge — overlay wins; nested dicts recurse."""
    out: dict[str, Any] = dict(base)
    for key, value in overlay.items():
        if key in out and isinstance(out[key], dict) and isinstance(value, dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def load_config(
    config_path: Path | str | None = None,
    overrides: dict[str, Any] | None = None,
) -> MiraConfig:
    """Load config, layering global defaults → per-repo `.mira.yaml` → overrides.

    Sources, lowest priority first:
      1. Built-in pydantic defaults (`MiraConfig()`).
      2. Deployment-wide defaults loaded via `set_global_defaults(...)`.
      3. Admin-editable runtime overrides stored in the dashboard DB
         (Settings page). Optional — falls through cleanly if no DB is
         available (CLI usage, tests, etc.).
      4. Per-repo `.mira.yaml` (auto-discovered by walking up from cwd, OR
         the explicit `config_path` if passed).
      5. Caller-supplied `overrides` dict.
      6. `DATABASE_URL` / `MIRA_MODEL` env-var fallbacks.
    """
    data: dict[str, Any] = _deep_merge({}, _global_defaults)

    # Lazy import + broad except: this function runs in CLI / test contexts
    # that have no DB attached. A DB error must never block a review.
    try:
        from mira.dashboard.api import _app_db

        if _app_db is not None:
            db_overrides = _app_db.get_global_review_overrides()
            if db_overrides:
                data = _deep_merge(data, db_overrides)
    except Exception as _db_exc:  # noqa: BLE001
        logger.debug("load_config: skipping DB overrides (%s)", _db_exc)

    if config_path is not None:
        path = Path(config_path)
        if not path.is_file():
            raise ConfigError(f"Config file not found: {path}")
        data = _deep_merge(data, _load_yaml(path))
    else:
        found = find_config_file()
        if found:
            data = _deep_merge(data, _load_yaml(found))

    if overrides:
        for key, value in overrides.items():
            _set_nested(data, key.split("."), value)

    # Respect DATABASE_URL env var
    env_db_url = os.environ.get("DATABASE_URL")
    if env_db_url and "database" not in data:
        data["database"] = {"url": env_db_url}
    elif env_db_url and "url" not in data.get("database", {}):
        data.setdefault("database", {})["url"] = env_db_url

    # Respect MIRA_MODEL env var as a fallback when not set via file or overrides
    env_model = os.environ.get("MIRA_MODEL")
    if env_model and "llm" not in data:
        data["llm"] = {"model": env_model}
    elif env_model and "model" not in data.get("llm", {}):
        data.setdefault("llm", {})["model"] = env_model

    try:
        return MiraConfig.model_validate(data)
    except Exception as e:
        raise ConfigError(f"Invalid configuration: {e}") from e


def _set_nested(d: dict[str, Any], keys: list[str], value: Any) -> None:
    """Set a value in a nested dict using a list of keys."""
    for key in keys[:-1]:
        d = d.setdefault(key, {})
    d[keys[-1]] = value
