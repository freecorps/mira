"""Data shapes for digests and release notes.

Plain dataclasses with ``to_dict``/``from_dict`` so a digest can be stored as
JSON, sent to a webhook and read back by the dashboard without a second model
of the same thing drifting out of step.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import Any


def day(epoch: float) -> str:
    """``2026-09-28`` for an epoch, in UTC."""
    return datetime.fromtimestamp(float(epoch or 0.0), tz=UTC).strftime("%Y-%m-%d")


@dataclass
class Change:
    """One thing that landed: a merged pull request or a direct commit.

    ``title`` and ``body`` are whatever the author wrote and are never trusted.
    ``repo`` is set only in an org-wide digest, where changes from several
    repositories sit side by side.
    """

    kind: str  # "pr" or "commit"
    title: str
    number: int = 0
    sha: str = ""
    body: str = ""
    url: str = ""
    author: str = ""
    landed_at: float = 0.0
    labels: list[str] = field(default_factory=list)
    files: list[str] = field(default_factory=list)
    repo: str = ""

    @property
    def ref(self) -> str:
        """``#123`` for a pull request, a short sha for a commit."""
        if self.kind == "pr":
            return f"#{self.number}"
        return self.sha[:8]

    @property
    def key(self) -> str:
        """Unique within a digest, across repositories."""
        return f"{self.repo}{self.ref}"

    def to_dict(self) -> dict[str, Any]:
        out = asdict(self)
        out["ref"] = self.ref
        return out

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Change:
        known = {k: data[k] for k in cls.__dataclass_fields__ if k in data}
        return cls(**known)


@dataclass
class AreaDigest:
    """The changes that touched one area, and what the model made of them."""

    name: str
    changes: list[Change] = field(default_factory=list)
    summary: str = ""
    highlights: list[str] = field(default_factory=list)
    # Directory summary from the repository index, when there is one. Context
    # for the model, not shown to readers.
    context: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "summary": self.summary,
            "highlights": list(self.highlights),
            "changes": [c.to_dict() for c in self.changes],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> AreaDigest:
        return cls(
            name=str(data.get("name") or ""),
            summary=str(data.get("summary") or ""),
            highlights=[str(h) for h in data.get("highlights") or []],
            changes=[Change.from_dict(c) for c in data.get("changes") or []],
        )


@dataclass
class Digest:
    """What landed in one period, for a repository or for an owner's repositories.

    ``repo`` is empty for an org-wide digest. ``notes`` says what a cap cut or
    what could not be read, so a short digest is never mistaken for a quiet
    week.
    """

    platform: str
    owner: str
    repo: str
    period_start: float
    period_end: float
    branch: str = ""
    areas: list[AreaDigest] = field(default_factory=list)
    overview: str = ""
    pull_requests: int = 0
    direct_commits: int = 0
    notes: list[str] = field(default_factory=list)
    llm_used: bool = False
    generated_at: float = 0.0

    @property
    def scope_name(self) -> str:
        return f"{self.owner}/{self.repo}" if self.repo else self.owner

    @property
    def title(self) -> str:
        return f"Mira digest: {self.scope_name}, {day(self.period_start)} to {day(self.period_end)}"

    @property
    def is_empty(self) -> bool:
        return self.pull_requests == 0 and self.direct_commits == 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "platform": self.platform,
            "owner": self.owner,
            "repo": self.repo,
            "period_start": self.period_start,
            "period_end": self.period_end,
            "branch": self.branch,
            "title": self.title,
            "overview": self.overview,
            "pull_requests": self.pull_requests,
            "direct_commits": self.direct_commits,
            "notes": list(self.notes),
            "llm_used": self.llm_used,
            "generated_at": self.generated_at,
            "areas": [a.to_dict() for a in self.areas],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Digest:
        return cls(
            platform=str(data.get("platform") or "github"),
            owner=str(data.get("owner") or ""),
            repo=str(data.get("repo") or ""),
            period_start=float(data.get("period_start") or 0.0),
            period_end=float(data.get("period_end") or 0.0),
            branch=str(data.get("branch") or ""),
            overview=str(data.get("overview") or ""),
            pull_requests=int(data.get("pull_requests") or 0),
            direct_commits=int(data.get("direct_commits") or 0),
            notes=[str(n) for n in data.get("notes") or []],
            llm_used=bool(data.get("llm_used")),
            generated_at=float(data.get("generated_at") or 0.0),
            areas=[AreaDigest.from_dict(a) for a in data.get("areas") or []],
        )
