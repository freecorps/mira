"""Optional CODEOWNERS integration, read conservatively.

A CODEOWNERS entry is a repository saying "a specific human signs off on this
file". Mira is not that human. So when the integration is enabled, a changed
path with a declared owner is a reason *not* to auto-approve — never a reason
to approve on the owner's behalf, and never a substitute for their review.

The parser is strict on purpose. A CODEOWNERS file it cannot fully understand
produces ``unreadable``, which the policy treats as a veto: guessing at an
ownership rule is exactly how a protected file gets approved by accident. The
cost of strictness is a loud failure on a weird file; the cost of leniency is a
silent one.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from mira.gate.paths import PatternError, matches, normalize

# Where each platform looks for the file, in the order it resolves them.
CODEOWNERS_LOCATIONS: dict[str, tuple[str, ...]] = {
    "github": (".github/CODEOWNERS", "CODEOWNERS", "docs/CODEOWNERS"),
    "gitlab": ("CODEOWNERS", ".gitlab/CODEOWNERS", "docs/CODEOWNERS"),
    "forgejo": ("CODEOWNERS", ".forgejo/CODEOWNERS", ".gitea/CODEOWNERS", "docs/CODEOWNERS"),
}

# `@user`, `@org/team`, or a bare email address.
_OWNER = re.compile(
    r"^(?:@[A-Za-z0-9][A-Za-z0-9._\-]*(?:/[A-Za-z0-9._\-]+)?|[^@\s]+@[^@\s]+\.[^@\s]+)$"
)

# GitLab section headers: `[Backend]`, `^[Optional]`, `[Backend][2]`,
# `[Backend] @default-owner`. Skipped rather than rejected — they organise
# rules, they are not rules themselves.
_SECTION = re.compile(r"^\^?\[[^\]]+\](?:\[\d+\])?")


@dataclass(frozen=True)
class Rule:
    pattern: str
    owners: tuple[str, ...]


@dataclass
class CodeownersFile:
    """A parsed CODEOWNERS, or the reason it could not be parsed."""

    # "ok" | "absent" | "unreadable" | "not_checked"
    status: str = "not_checked"
    path: str = ""
    rules: list[Rule] = field(default_factory=list)
    error: str = ""

    @property
    def usable(self) -> bool:
        return self.status in {"ok", "absent"}

    def owners_for(self, path: str) -> tuple[str, ...]:
        """Owners of one path, last matching rule winning, as git does.

        A trailing rule with no owners genuinely un-owns a path — that is what
        writing it means — so it is honoured rather than treated as ownership.
        Everything *else* about this module resolves doubt toward "owned"; this
        one case is an explicit statement by the repository, not a doubt.
        """
        candidate = normalize(path)
        owners: tuple[str, ...] = ()
        for rule in self.rules:
            if matches(candidate, rule.pattern):
                owners = rule.owners
        return owners

    def owned_paths(self, paths: list[str]) -> list[str]:
        """Every path in ``paths`` that has at least one declared owner."""
        return [path for path in paths if self.owners_for(path)]


def parse(text: str, *, source_path: str = "") -> CodeownersFile:
    """Parse CODEOWNERS text. Any line it cannot read makes the whole file
    ``unreadable`` — a half-understood ownership map is worse than none."""
    rules: list[Rule] = []
    for lineno, raw in enumerate(text.splitlines(), start=1):
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        if _SECTION.match(line):
            # Strip the section header; anything after it on the line is a
            # default-owner list for the section, not a path rule.
            continue
        parts = line.split()
        pattern, owners = parts[0], tuple(parts[1:])
        for owner in owners:
            if not _OWNER.match(owner):
                return CodeownersFile(
                    status="unreadable",
                    path=source_path,
                    error=f"line {lineno}: {owner!r} is not a recognizable owner",
                )
        try:
            matches("probe.txt", pattern)
        except PatternError as exc:
            return CodeownersFile(
                status="unreadable",
                path=source_path,
                error=f"line {lineno}: {exc}",
            )
        rules.append(Rule(pattern=pattern, owners=owners))
    return CodeownersFile(status="ok", path=source_path, rules=rules)


def locations_for(platform: str) -> tuple[str, ...]:
    return CODEOWNERS_LOCATIONS.get(platform, CODEOWNERS_LOCATIONS["github"])
