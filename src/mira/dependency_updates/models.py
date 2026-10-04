"""What a dependency bump is, and what Mira learned about it."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class DependencyBump:
    """One package whose declared version a pull request changes.

    ``old`` and ``new`` are concrete versions (constraint operators removed);
    ``old_spec`` and ``new_spec`` are what the manifest actually says.
    """

    name: str
    kind: str  # Mira's ecosystem key: "pip" | "npm" | "go" | "composer"
    old: str
    new: str
    file_path: str
    old_spec: str = ""
    new_spec: str = ""

    @property
    def key(self) -> tuple[str, str, str, str]:
        return (self.kind, self.name.lower(), self.old, self.new)


@dataclass
class ReleaseNote:
    """One upstream release (or changelog section) between the two versions."""

    version: str
    url: str
    body: str


@dataclass
class NoteItem:
    """A single breaking change, deprecation or notable change, with its source."""

    text: str
    url: str = ""


@dataclass
class DependencyUpdate:
    """Everything the walkthrough and the review prompt show about one bump."""

    bump: DependencyBump
    source_repo: str = ""  # "owner/repo" on GitHub, when found
    notes_url: str = ""  # the page a reader would open first
    releases: list[ReleaseNote] = field(default_factory=list)
    breaking_changes: list[NoteItem] = field(default_factory=list)
    deprecations: list[NoteItem] = field(default_factory=list)
    notable: list[NoteItem] = field(default_factory=list)
    # OSV advisories: affecting the old version but not the new one, and
    # affecting the new version (whether or not the old one had them too).
    vulns_fixed: list[tuple[str, str, str]] = field(default_factory=list)  # (id, severity, url)
    vulns_in_new: list[tuple[str, str, str]] = field(default_factory=list)
    # Why there are no notes, when there are none ("not looked up", "timed out").
    status: str = ""
    # The summary call answered for this package, even if with empty lists.
    summarized: bool = False

    @property
    def has_items(self) -> bool:
        return bool(self.breaking_changes or self.deprecations or self.notable)
