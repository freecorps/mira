"""What kind of change this is, from the diff and nothing else.

Classification is deterministic and observational. It reads paths and line
counts; it does not read the title, the description, the branch name or a
label. That is not a simplification — it is the point. A pull request that says
"docs only" and touches ``src/auth`` is exactly the pull request a reviewer
most needs classified correctly, and a classifier that believed the title would
be wrong precisely there.

The kinds are the ones that change *who* should look at a change and *how long
it will take*: whether it carries tests, whether it touches a migration,
whether it is mostly generated files. They are deliberately coarse. A finer
taxonomy would be more impressive and less useful, because the next question a
human asks after "what is this" is "who reviews it", and that is answered by
the paths themselves.
"""

from __future__ import annotations

from mira.checks.native import paths as pathkind
from mira.models import FileChangeStat
from mira.triage.models import Classification, size_bucket

# How many directory areas a classification names. Four, because a change
# spanning more than four areas is better described by its size than by its
# areas, and a list of twelve directories is not a summary.
MAX_AREAS = 4

# Depth of the directory prefix an area is grouped at. Two levels keeps
# `src/mira` apart from `ui/mira` without splitting a package into its modules.
AREA_DEPTH = 2

# Dependency manifests and lockfiles. A change here is a supply-chain change
# whatever else it is, and often wants a different reviewer from the code.
_DEPENDENCY_NAMES = frozenset(
    {
        "package.json",
        "package-lock.json",
        "yarn.lock",
        "pnpm-lock.yaml",
        "bun.lockb",
        "requirements.txt",
        "requirements-dev.txt",
        "pyproject.toml",
        "poetry.lock",
        "uv.lock",
        "pipfile",
        "pipfile.lock",
        "go.mod",
        "go.sum",
        "cargo.toml",
        "cargo.lock",
        "gemfile",
        "gemfile.lock",
        "composer.json",
        "composer.lock",
        "build.gradle",
        "pom.xml",
    }
)

# Continuous-integration definitions, by directory prefix or exact name.
_CI_PREFIXES = (
    ".github/workflows/",
    ".gitlab/",
    ".forgejo/workflows/",
    ".gitea/workflows/",
    ".circleci/",
    ".woodpecker/",
)
_CI_NAMES = frozenset(
    {".gitlab-ci.yml", ".travis.yml", "azure-pipelines.yml", "jenkinsfile", ".woodpecker.yml"}
)


def _basename(path: str) -> str:
    return path.rsplit("/", 1)[-1].lower()


def is_dependency(path: str) -> bool:
    return _basename(path) in _DEPENDENCY_NAMES


def is_ci(path: str) -> bool:
    lowered = (path or "").lower()
    return lowered.startswith(_CI_PREFIXES) or _basename(path) in _CI_NAMES


def kinds_for(path: str) -> list[str]:
    """Every kind one path belongs to, most specific first.

    A path can be several things at once — a generated migration, a test for a
    CI script — and collapsing that to one label would throw away the part the
    reviewer cares about. The order is fixed so two runs over the same file
    list produce the same string.
    """
    kinds: list[str] = []
    if pathkind.is_migration(path):
        kinds.append("migration")
    if is_dependency(path):
        kinds.append("dependencies")
    if is_ci(path):
        kinds.append("ci")
    if pathkind.is_test(path):
        kinds.append("tests")
    if pathkind.is_doc(path):
        kinds.append("docs")
    if pathkind.is_generated(path):
        kinds.append("generated")
    if not kinds and pathkind.is_source(path):
        kinds.append("code")
    return kinds or ["other"]


def area_for(path: str) -> str:
    """The directory area a path belongs to.

    A file at the repository root has no area — reporting one would invent a
    grouping that does not exist — so it is reported as ``(root)``, which is
    both honest and sortable.
    """
    parts = [part for part in (path or "").split("/") if part]
    directories = parts[:-1]
    if not directories:
        return "(root)"
    return "/".join(directories[:AREA_DEPTH])


def classify(changes: list[FileChangeStat]) -> Classification:
    """Describe a change by its files, deterministically.

    Generated files are counted in ``changed_files`` — they *are* part of the
    change — and excluded from the line count that picks the size bucket. A
    regenerated lockfile makes a pull request long, not large, and calling it
    ``xl`` would train everyone to ignore the size.
    """
    if not changes:
        return Classification()

    kinds: dict[str, int] = {}
    areas: dict[str, int] = {}
    total_lines = 0
    weighted_lines = 0

    for change in changes:
        lines = int(change.added_lines) + int(change.deleted_lines)
        total_lines += lines
        path_kinds = kinds_for(change.path)
        for kind in path_kinds:
            kinds[kind] = kinds.get(kind, 0) + 1
        if "generated" not in path_kinds:
            weighted_lines += lines
        areas[area_for(change.path)] = areas.get(area_for(change.path), 0) + lines

    ordered_areas = sorted(areas.items(), key=lambda item: (-item[1], item[0]))
    # Kinds are ordered by how many files carry them, then alphabetically, so
    # the same file list always renders the same sentence.
    ordered_kinds = sorted(kinds.items(), key=lambda item: (-item[1], item[0]))

    return Classification(
        size=size_bucket(weighted_lines),
        changed_files=len(changes),
        changed_lines=total_lines,
        areas=[area for area, _ in ordered_areas[:MAX_AREAS]],
        kinds=[kind for kind, _ in ordered_kinds],
    )
