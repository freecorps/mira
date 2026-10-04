"""Dependency bumps reviewed against their upstream release notes.

When a pull request moves a package from one version to another, the review is
usually about code the pull request does not show: a removed function the
repository still calls, a default that changed under it. This package reads
the bump from the manifests at both ends of the review, finds the package's
source repository through its registry, fetches the GitHub releases (or the
changelog) between the two versions, and has the indexing model pull out the
breaking changes and deprecations. The review model reads that as context; the
walkthrough lists each bump.

See docs/dependency-updates.md.
"""

from mira.dependency_updates.models import DependencyBump, DependencyUpdate, NoteItem, ReleaseNote
from mira.dependency_updates.render import review_context, walkthrough_section
from mira.dependency_updates.service import collect_dependency_updates, provider_reader

__all__ = [
    "DependencyBump",
    "DependencyUpdate",
    "NoteItem",
    "ReleaseNote",
    "collect_dependency_updates",
    "provider_reader",
    "review_context",
    "walkthrough_section",
]
