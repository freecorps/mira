"""Configuration for triage and reviewer suggestion (Phase 7C).

In its own module for the same reason the check framework's is, and with the
same rule around it:

**Nothing in a pull request can reach any value defined here.** Not a weight,
not a threshold, not an entry in the opt-out list. A pull request supplies the
files it changed and the commit it changed them at; it supplies nothing to the
policy that decides how those files are turned into a suggestion.

That rule has one concrete consequence worth naming here rather than burying in
the ownership module: CODEOWNERS is read at the pull request's **base**, never
at its head. CODEOWNERS is repository configuration, and a branch that could
add a line to it and be ranked under the result would be choosing its own
reviewer — which is exactly the thing this file exists to prevent.

There is no setting that assigns anybody. Suggestion and assignment are
different acts, and this phase performs only the first one.
"""

from __future__ import annotations

import re

from pydantic import BaseModel, Field, field_validator

# A platform login or `@org/team` handle, in the shapes GitHub, GitLab and
# Forgejo all accept. Validated at config load so an opt-out entry that can
# never match is a startup error rather than a person who quietly keeps being
# suggested.
_IDENTITY = re.compile(r"^@?[A-Za-z0-9][A-Za-z0-9._\-]{0,62}(?:/[A-Za-z0-9._\-]{1,63})?$")


def _normalize_identity(value: str) -> str:
    """Lower-cased, without the leading ``@``.

    Identities are compared, never rendered, in this normalized form. Platforms
    treat logins case-insensitively and people write the ``@`` about half the
    time, so an opt-out list that did not normalize would work for whoever
    wrote it and silently fail for the next person.
    """
    return (value or "").strip().lstrip("@").lower()


def _validate_identities(values: list[str] | None, label: str) -> list[str] | None:
    if values is None:
        return None
    out: list[str] = []
    for raw in values:
        text = (raw or "").strip()
        if not _IDENTITY.match(text):
            raise ValueError(f"{label}: {raw!r} is not a recognizable login or @org/team handle")
        out.append(_normalize_identity(text))
    return out


class TriageWeights(BaseModel):
    """How much each signal is worth, before recency and load.

    Defaults put declared ownership well above observed history on purpose. A
    CODEOWNERS entry is the repository *stating* who reviews a file; history is
    Mira *inferring* it. When the two disagree the statement should win, and
    when only the inference exists it should still be enough to suggest
    somebody.
    """

    codeowners: float = Field(default=3.0, ge=0.0, le=100.0)
    authored: float = Field(default=1.0, ge=0.0, le=100.0)
    reviewed: float = Field(default=1.5, ge=0.0, le=100.0)

    def for_kind(self, kind: str) -> float:
        return float(getattr(self, kind, 0.0))


class TriageScopePolicy(BaseModel):
    """What one organisation or one repository does differently.

    Every field is ``None``-by-default and ``None`` inherits, so a scope that
    only wants a different suggestion count does not restate the weights. An
    explicit ``[]`` on a list still means "empty here", the same sentinel the
    gate, autofix and check policies use.
    """

    enabled: bool | None = None
    comment: bool | None = None
    max_suggestions: int | None = Field(default=None, ge=1, le=10)
    min_score: float | None = Field(default=None, ge=0.0)
    codeowners: bool | None = None
    history: bool | None = None
    history_days: int | None = Field(default=None, ge=1, le=3650)
    weights: TriageWeights | None = None
    load_penalty: float | None = Field(default=None, ge=0.0, le=10.0)
    exclude: list[str] | None = None

    @field_validator("exclude")
    @classmethod
    def _valid_exclude(cls, v: list[str] | None) -> list[str] | None:
        return _validate_identities(v, "triage.<scope>.exclude[]")


class TriageConfig(BaseModel):
    """The whole triage policy: global defaults plus per-scope overrides."""

    # Off until an operator turns it on. A suggestion names people in a public
    # comment, which is not something to start doing by upgrade.
    enabled: bool = False

    # One switch that stops every suggestion everywhere, recorded on the run
    # rather than inferred from silence.
    kill_switch: bool = False

    # Bumped by hand when the *meaning* of the policy changes. Stored with
    # every run next to the content hash, so history stays attributable.
    policy_version: str = "triage-v1"

    # Publish the suggestion as a pull-request comment, updated in place.
    # There is no status-publishing option: a suggestion is not a check, must
    # never appear in a branch protection rule, and must never be something a
    # merge waits on.
    comment: bool = True

    max_suggestions: int = Field(default=3, ge=1, le=10)

    # A candidate scoring below this is recorded as excluded rather than
    # suggested. The floor exists because a single six-month-old commit is a
    # fact, not a recommendation.
    #
    # The default is set just under the weight of one authorship, so that one
    # file you changed *recently* is enough to be suggested and the same file
    # changed most of a window ago is not — recency has decayed it to a fifth
    # by then. A floor at exactly 1.0 would have made the first case depend on
    # whether the commit was today, which is a cliff nobody could predict from
    # reading the configuration.
    min_score: float = Field(default=0.75, ge=0.0)

    # The two signals, each switchable. Turning both off leaves a run that
    # still classifies the change and says plainly that it was not asked to
    # suggest anybody.
    codeowners: bool = True
    history: bool = True

    # How far back history counts, and how hard it is allowed to look. The
    # caps are the Orange Pi's, not the platform's: a pull request touching
    # 300 files must not turn into 300 API calls.
    history_days: int = Field(default=180, ge=1, le=3650)
    history_max_paths: int = Field(default=12, ge=1, le=100)
    history_max_per_path: int = Field(default=20, ge=1, le=100)
    # How long a fetched path history stays usable before it is fetched again.
    history_refresh_hours: float = Field(default=168.0, ge=0.0, le=8760.0)

    # People who are never suggested, whatever the signals say. An opt-out
    # list, and the only correct answer to "please stop pinging me".
    exclude: list[str] = Field(default_factory=list)

    # Skip identities that look like machines. Bots do not review code, and a
    # bot at the top of a suggestion list discredits the whole list.
    exclude_bots: bool = True
    # Extra logins to treat as machines, for accounts whose names do not say so.
    bots: list[str] = Field(default_factory=list)

    # Points subtracted per pull request already waiting on someone. A
    # dampener, not a cap: the most qualified reviewer stays the most
    # qualified reviewer when they are busy, they just stop being the only
    # name suggested.
    load_penalty: float = Field(default=0.25, ge=0.0, le=10.0)

    # Whole-run budget. A suggestion that arrives after the human has already
    # picked somebody is worth nothing, so it is bounded rather than thorough.
    budget_seconds: float = Field(default=30.0, ge=1.0, le=600.0)

    weights: TriageWeights = Field(default_factory=TriageWeights)

    organizations: dict[str, TriageScopePolicy] = Field(default_factory=dict)
    repositories: dict[str, TriageScopePolicy] = Field(default_factory=dict)

    @field_validator("exclude")
    @classmethod
    def _valid_exclude(cls, v: list[str]) -> list[str]:
        return _validate_identities(v, "triage.exclude[]") or []

    @field_validator("bots")
    @classmethod
    def _valid_bots(cls, v: list[str]) -> list[str]:
        return _validate_identities(v, "triage.bots[]") or []

    @field_validator("repositories")
    @classmethod
    def _valid_repo_keys(cls, v: dict[str, TriageScopePolicy]) -> dict[str, TriageScopePolicy]:
        for key in v:
            if key.count("/") != 1 or not all(part.strip() for part in key.split("/")):
                raise ValueError(f"triage.repositories key {key!r} must be 'owner/repo'")
        return v
