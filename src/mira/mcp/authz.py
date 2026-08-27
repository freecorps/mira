"""Which repositories an MCP session may read, and nothing about how.

The threat model is short. The operator launches the server; the model on the
other end of the pipe calls the tools. So the *grant* is trusted input and
every *argument* is not, and the whole of authorization is keeping those two
apart: a tool call names a repository, and that name is looked up in the grant
rather than parsed into one.

That distinction is what makes the isolation hold. `resolve` never builds a
repository out of what it was handed — it canonicalises the string and matches
it against a grant built at startup. A caller that invents `acme/other`, or
walks out of the index directory with `../..`, or spells a granted repository
in a way that would open a different store, gets the same refusal, because
none of those strings is in the mapping.

An empty grant is a working configuration that refuses everything. It is what
an operator gets by turning the feature on and not saying which repositories,
and it must not read as "all of them".
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass

#: Platforms Mira stores an index for. A repository on anything else cannot be
#: named, because there is nothing to open.
PLATFORMS = ("github", "gitlab", "forgejo")

# One path segment. Deliberately narrow: these become directory names under
# MIRA_INDEX_DIR, so the character set is the one that cannot mean anything
# else on a filesystem. `.` and `..` are excluded by the leading class.
_SEGMENT = r"[A-Za-z0-9_-][A-Za-z0-9._-]*"
_OWNER = rf"{_SEGMENT}(?:/{_SEGMENT})*"
_SPEC = re.compile(rf"^(?:(?P<platform>[a-z]+):)?(?P<owner>{_OWNER})/(?P<repo>{_SEGMENT})$")


class InvalidRepository(ValueError):
    """A repository specification that cannot name any repository at all."""


class NotAuthorized(Exception):
    """A well-formed request for a repository this session was not granted."""

    def __init__(self, requested: str) -> None:
        super().__init__(
            f"This MCP server was not granted {requested!r}. "
            "Call mira_list_repositories for the ones it can read."
        )
        self.requested = requested


@dataclass(frozen=True)
class Repository:
    """A repository the grant names, in the form the index store takes."""

    platform: str
    owner: str
    repo: str

    @property
    def key(self) -> str:
        """The canonical spelling. One repository, one string, forever."""
        return f"{self.platform}:{self.owner}/{self.repo}"

    @property
    def slug(self) -> str:
        return f"{self.owner}/{self.repo}"


def parse_repository(spec: str) -> Repository:
    """Canonicalise ``[platform:]owner/repo``.

    Parsing is not authorization and this function grants nothing. It exists so
    that two spellings of the same repository resolve to one key, and so that a
    string that could never be a repository is rejected where the operator can
    see it rather than at the point it would have opened something.
    """
    text = (spec or "").strip()
    if not text:
        raise InvalidRepository("a repository must be named, as owner/repo")
    match = _SPEC.match(text)
    if match is None:
        raise InvalidRepository(
            f"{spec!r} is not owner/repo (optionally platform:owner/repo). "
            "Segments may hold letters, digits, dot, dash and underscore, and "
            "may not begin with a dot."
        )
    platform = match.group("platform") or "github"
    if platform not in PLATFORMS:
        raise InvalidRepository(
            f"{platform!r} is not a platform Mira indexes ({', '.join(PLATFORMS)})"
        )
    return Repository(platform=platform, owner=match.group("owner"), repo=match.group("repo"))


@dataclass(frozen=True)
class Grant:
    """The repositories this process may read. Fixed at startup."""

    repositories: tuple[Repository, ...] = ()

    @classmethod
    def from_specs(cls, specs: Iterable[str]) -> Grant:
        found: dict[str, Repository] = {}
        for spec in specs:
            repository = parse_repository(spec)
            found.setdefault(repository.key, repository)
        return cls(tuple(found.values()))

    def __bool__(self) -> bool:
        return bool(self.repositories)

    @property
    def keys(self) -> tuple[str, ...]:
        return tuple(r.key for r in self.repositories)

    def resolve(self, requested: str) -> Repository:
        """The granted repository this argument names, or a refusal.

        Note what this does not do: it does not return the parsed repository.
        The value handed back is the one built at startup from the operator's
        configuration, so nothing a client sends can reach the store even in
        the shape of a repository it was allowed to read.
        """
        text = (requested or "").strip()
        if not text:
            raise NotAuthorized("")
        try:
            candidate = parse_repository(text)
        except InvalidRepository:
            # A malformed name and an ungranted one get the same answer. There
            # is no reading of this server for which the difference is the
            # client's business.
            raise NotAuthorized(text) from None
        for repository in self.repositories:
            if repository.key == candidate.key:
                return repository
        raise NotAuthorized(candidate.key)

    def narrow(self, wanted: Iterable[str]) -> Grant:
        """A grant holding only the named repositories.

        Narrowing only. `mira mcp serve --repo` uses this so a launch can ask
        for less than the configuration allows; asking for more is an error
        rather than a silent widening, because a flag that quietly granted
        access would make the configured ceiling decorative.
        """
        chosen: list[Repository] = []
        for spec in wanted:
            candidate = parse_repository(spec)
            match = next((r for r in self.repositories if r.key == candidate.key), None)
            if match is None:
                raise NotAuthorized(candidate.key)
            if match not in chosen:
                chosen.append(match)
        return Grant(tuple(chosen))
