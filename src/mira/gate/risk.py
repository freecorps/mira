"""Deterministic, explainable risk scoring.

Two constraints shape everything here. It has to be *deterministic* — the same
inputs give the same integer on every replica, forever, so a decision can be
recomputed from its stored inputs and checked. And it has to be *explainable* —
the score is never a number on its own, it is a list of named factors that add
up to it, because "risk 62" is not a reason and cannot be argued with.

This is not the review's quality score and does not read from it. Quality asks
whether the code is good; risk asks how much is riding on being wrong about
that. A flawless 4,000-line change to the deployment pipeline is high risk and
high quality at the same time, and the gate has to be able to say so.

No LLM is involved. On the Orange Pi profile the gate has to be free next to a
review, so scoring is arithmetic over facts already gathered.
"""

from __future__ import annotations

from mira.config import RiskWeights
from mira.gate.models import GateInputs, RiskFactor

# Manifest files whose change means the dependency surface moved — the one part
# of a diff whose real blast radius is not in the diff.
_MANIFEST_NAMES = frozenset(
    {
        "package.json",
        "requirements.txt",
        "pyproject.toml",
        "setup.py",
        "go.mod",
        "cargo.toml",
        "gemfile",
        "composer.json",
        "build.gradle",
        "build.gradle.kts",
        "pom.xml",
        "pubspec.yaml",
        "mix.exs",
    }
)

# Above this share of generated files the diff is mostly machine output, which
# tells us less than its size suggests — in both directions.
_GENERATED_HEAVY_RATIO = 0.5


def _capped(count: int, per_unit: int, cap: int) -> int:
    return min(count * per_unit, cap) if per_unit else 0


def _touches_manifest(paths: list[str]) -> list[str]:
    hits = []
    for path in paths:
        name = path.rsplit("/", 1)[-1].lower()
        if name in _MANIFEST_NAMES:
            hits.append(path)
    return hits


def score(inputs: GateInputs, weights: RiskWeights) -> tuple[int, list[RiskFactor]]:
    """Score one PR. Returns ``(0..100, factors)``.

    Everything it reads is on ``inputs``, which is also what gets persisted, so
    a stored decision can be re-scored and checked. Factors are emitted in a
    fixed order and only when they contribute, so two runs over the same inputs
    produce identical lists — an audit compares them directly.
    """
    warnings = inputs.open_warnings
    suggestions = inputs.open_suggestions
    security_findings = inputs.open_security
    factors: list[RiskFactor] = []

    def add(code: str, label: str, points: int, detail: str = "") -> None:
        if points > 0:
            factors.append(RiskFactor(code=code, label=label, points=points, detail=detail))

    # ── What the review found and nobody has resolved ────────────────────
    if inputs.open_blockers:
        add(
            "open_blocker",
            "Open blocker findings",
            weights.open_blocker,
            f"{inputs.open_blockers} blocker finding(s) still open",
        )
    if warnings:
        add(
            "warning_findings",
            "Open warnings",
            _capped(warnings, weights.warning_finding, weights.warning_cap),
            f"{warnings} warning finding(s)",
        )
    if suggestions:
        add(
            "suggestion_findings",
            "Open suggestions",
            _capped(suggestions, weights.suggestion_finding, weights.suggestion_cap),
            f"{suggestions} suggestion/nitpick finding(s)",
        )
    if security_findings:
        add(
            "security_findings",
            "Security-category findings",
            weights.security_finding,
            f"{security_findings} finding(s) in the security category",
        )

    # ── Size, discounting machine output ─────────────────────────────────
    counted_files = inputs.changed_files
    generated = len(inputs.generated_paths)
    if generated and inputs.changed_files:
        counted_files = max(0, inputs.changed_files - generated)
    over_files = max(0, counted_files - weights.size_free_files)
    add(
        "size_files",
        "Files changed",
        _capped(over_files, weights.size_per_file, weights.size_file_cap),
        f"{counted_files} reviewable file(s) changed",
    )
    total_lines = inputs.added_lines + inputs.deleted_lines
    over_lines = max(0, total_lines - weights.size_free_lines)
    add(
        "size_lines",
        "Lines changed",
        _capped(over_lines // 100, weights.size_per_100_lines, weights.size_line_cap),
        f"{total_lines} line(s) added or removed",
    )
    if inputs.changed_files and generated / max(inputs.changed_files, 1) >= _GENERATED_HEAVY_RATIO:
        add(
            "generated_heavy",
            "Mostly generated output",
            weights.generated_heavy,
            f"{generated} of {inputs.changed_files} changed files are generated",
        )
    manifests = _touches_manifest(inputs.changed_paths)
    if manifests:
        add(
            "dependency_manifest",
            "Dependency manifest changed",
            weights.dependency_manifest,
            ", ".join(sorted(manifests)[:5]),
        )

    # ── What Mira could not see ──────────────────────────────────────────
    if inputs.review_skipped_paths:
        add(
            "unreviewed_paths",
            "Files left unreviewed",
            weights.unreviewed_paths,
            f"{len(inputs.review_skipped_paths)} file(s) were not reviewed",
        )
    if not inputs.index_ready:
        add(
            "index_not_ready",
            "Repository index incomplete",
            weights.index_not_ready,
            "cross-file context was unavailable or partial",
        )

    # ── Who is asking, and what the platform says ────────────────────────
    association = (inputs.author_association or "unknown").upper()
    if association in {"", "UNKNOWN"}:
        add(
            "unknown_association",
            "Author association unknown",
            weights.unknown_association,
            "the platform did not report the author's relationship to the repository",
        )
    elif association in {"FIRST_TIME_CONTRIBUTOR", "FIRST_TIMER", "NONE", "NONE_MEMBER"}:
        add(
            "first_time_contributor",
            "First-time contributor",
            weights.first_time_contributor,
            f"author association is {association}",
        )
    if inputs.ci.state != "success":
        add(
            "ci_not_success",
            "CI not green",
            weights.ci_not_success,
            f"CI reported {inputs.ci.state}",
        )

    # ── Places Mira has no standing to approve ───────────────────────────
    if inputs.protected_matches:
        add(
            "protected_path",
            "Protected paths touched",
            weights.protected_path,
            ", ".join(sorted(inputs.protected_matches)[:5]),
        )
    if inputs.codeowner_matches:
        add(
            "codeowner_path",
            "CODEOWNERS-owned paths touched",
            weights.codeowner_path,
            ", ".join(sorted(inputs.codeowner_matches)[:5]),
        )
    blocking_humans = sorted(
        login
        for login, state in (inputs.human_states or {}).items()
        if state == "CHANGES_REQUESTED"
    )
    if blocking_humans:
        add(
            "human_changes_requested",
            "A human requested changes",
            weights.human_changes_requested,
            ", ".join(blocking_humans[:5]),
        )

    total = min(100, sum(factor.points for factor in factors))
    return total, factors
