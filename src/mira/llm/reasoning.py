"""Which reasoning level one call is made at.

A client has one configured level (``LLMConfig.reasoning_effort``: the
dashboard's thinking mode for reviews, nothing for the other purposes), and
two things lower it for a single call:

* **A lookup hop** of an agentic loop runs under
  :func:`mira.llm.base.reasoning_override` (``review.lookup_reasoning_effort``):
  deciding which file to read next needs less thinking than writing the
  findings.
* **A mechanical call** — a file or directory summary, the walkthrough, the
  pull request's summary and title, the overlap check, a release note —
  produces text from what it is shown and decides nothing a review's
  correctness rests on. The stage a call is tagged with
  (:func:`mira.llm.accounting.stage`) says which calls those are, in one
  table, :data:`MECHANICAL_STAGES`; ``llm.mechanical_reasoning`` (``none``
  by default, ``inherit`` to keep the configured level) is the level they
  run at.

An override only ever lowers the level. The one exception is ``none`` on a
client with no level configured: some models think unless told not to
(DeepSeek V4 on OpenDesign reasons at length when the request names no
level), so "no level" is not "no thinking" there, and a mechanical call asks
for ``none`` where the endpoint is known to take it.

**Honoured levels.** An endpoint may accept every level and act on few. On
OpenDesign, DeepSeek V4 ignores ``low``, ``minimal`` and ``high`` alike — it
reasons until it is done or the output budget runs out — and only ``none``
does anything: thinking off, an answer in a fraction of the time. A preset
says so in ``providers.json`` (``reasoning_levels``, by model glob), and a
requested level is mapped onto those:

* a level an override lowered goes **down** to the nearest honoured one —
  ``low`` for a lookup hop is ``none`` there, which is what a lookup asked
  for;
* the configured level goes **up** to the nearest honoured one other than
  ``none`` — a judgement call configured ``low`` is sent ``high``, which is
  what the endpoint does with ``low`` anyway, and is never silently turned
  into no thinking at all.

Without a preset entry, models.dev's levels for the model (the ones the
dashboard's picker offers) say whether ``none`` exists; an endpoint nobody
describes gets ``low`` where an override asked for ``none``, the lowest level
nearly every API takes.
"""

from __future__ import annotations

from fnmatch import fnmatchcase
from typing import Any

# Reasoning levels from least to most thinking.
EFFORT_ORDER: tuple[str, ...] = ("none", "minimal", "low", "medium", "high", "xhigh", "max")

# Values ``llm.mechanical_reasoning`` takes: "none" turns thinking off for
# mechanical calls, "inherit" leaves them at the client's configured level.
# Other levels are not offered: an override only ever lowers, so a mechanical
# level above "none" could never turn thinking on for a client configured
# without it, and would read as a setting that does nothing.
MECHANICAL_VALUES: tuple[str, ...] = ("inherit", "none")

# The stages whose calls are mechanical: they summarise or rephrase what they
# are shown and decide nothing a review's correctness rests on. The review
# judgement (``pass``, ``system``, ``security``, ``dependency``), the
# self-critique, the learning judge, thread replies and fix verdicts are not
# here and keep the configured level. Each name is also what the call is
# recorded under in the usage ledger.
MECHANICAL_STAGES: frozenset[str] = frozenset(
    {
        "file_summaries",  # indexing: what each file does
        "directory_summaries",  # indexing: what each directory holds
        "walkthrough",  # the walkthrough comment
        "summary",  # the review's summary, rewritten from the posted findings
        "pr_title",  # a title for a pull request titled "@mira"
        "pr_description",  # the summary section of the description
        "overlaps",  # which open pull requests touch the same code
        "review_pattern",  # a "look for…" pattern from a peer bot's finding
        "release_notes",  # digests: release notes
        "digest",  # digests: the period's summary
        "release_summary",  # dependency updates: what a release changed
        "ci_summary",  # what a failed CI job's log shows
    }
)

# What a client with no level configured is said to have.
_OFF = ("", "off")


def rank(level: str | None) -> int | None:
    """Where ``level`` sits on :data:`EFFORT_ORDER`, or None when it is not on it."""
    try:
        return EFFORT_ORDER.index(str(level))
    except ValueError:
        return None


def lower_effort(configured: str | None, override: str | None) -> str | None:
    """The level a call is made at: ``override`` when it is lower than ``configured``.

    An override never turns thinking on for a client that has it off, and
    never raises the level; an unknown level on either side leaves the
    configured one.
    """
    if not configured or configured == "off" or not override or override == "inherit":
        return configured
    want, have = rank(override), rank(configured)
    if want is None or have is None:
        return configured
    return override if want < have else configured


def lowest(*levels: str | None) -> str | None:
    """The lowest of the levels given (unknown ones ignored), or None."""
    known = [level for level in levels if level and rank(level) is not None]
    return min(known, key=lambda level: rank(level) or 0) if known else None


def stage_level(stage: str, mechanical: str | None) -> str | None:
    """The override a call in ``stage`` runs under, or None.

    ``mechanical`` is ``llm.mechanical_reasoning``. A stage outside
    :data:`MECHANICAL_STAGES` — the judgement calls — has none.
    """
    if stage not in MECHANICAL_STAGES or not mechanical or mechanical == "inherit":
        return None
    return mechanical if rank(mechanical) is not None else None


def honoured_levels(profile: dict, model: str) -> tuple[str, ...] | None:
    """The levels the endpoint acts on for ``model``, from its preset; None when unsaid.

    ``reasoning_levels`` is a list for every model, or an object from model
    globs (``"deepseek-*"``) to lists, matched against the bare id and the
    one with a vendor prefix alike. The first matching glob wins.
    """
    raw = profile.get("reasoning_levels")
    if isinstance(raw, list):
        return _levels(raw)
    if not isinstance(raw, dict) or not model:
        return None
    bare = model.split("/", 1)[1] if "/" in model else model
    for pattern, levels in raw.items():
        if not isinstance(pattern, str) or pattern.startswith("_"):
            continue  # a "_comment"
        if fnmatchcase(bare, pattern) or fnmatchcase(model, pattern):
            return _levels(levels) if isinstance(levels, list) else None
    return None


def _levels(raw: list[Any]) -> tuple[str, ...] | None:
    out = tuple(level for level in raw if isinstance(level, str) and rank(level) is not None)
    return tuple(sorted(set(out), key=lambda level: rank(level) or 0)) or None


def _down(honoured: tuple[str, ...], level: str) -> str:
    """The highest honoured level at or below ``level``; else the lowest honoured."""
    have = rank(level) or 0
    below = [candidate for candidate in honoured if (rank(candidate) or 0) <= have]
    return below[-1] if below else honoured[0]


def _up(honoured: tuple[str, ...], level: str) -> str:
    """The lowest honoured level at or above ``level`` that still thinks.

    A configured level is never mapped onto ``none``: that would turn a
    judgement call's thinking off without anyone asking. With no thinking
    level honoured at all, the level is sent as configured.
    """
    if level == "none":
        return level
    thinking = [candidate for candidate in honoured if candidate != "none"]
    if not thinking:
        return level
    have = rank(level) or 0
    above = [candidate for candidate in thinking if (rank(candidate) or 0) >= have]
    return above[0] if above else thinking[-1]


def resolve(
    configured: str | None,
    override: str | None = None,
    *,
    honoured: tuple[str, ...] | None = None,
    known: tuple[str, ...] | None = None,
) -> str | None:
    """The level to ask for: ``configured`` lowered by ``override``, mapped onto ``honoured``.

    ``honoured`` is the preset's :func:`honoured_levels`; ``known`` is what
    models.dev says the model takes (its snapping to those happens after,
    in the provider). None (or "off") means no level goes on the wire.
    """
    takes_none = "none" in (honoured or ()) or "none" in (known or ())
    if not configured or configured in _OFF:
        # Thinking off as far as the config goes — but a model may think
        # unless told not to, and "none" tells it, where it is known to work.
        return "none" if override == "none" and takes_none else configured
    level = lower_effort(configured, override) or configured
    lowered = level != configured
    if honoured and rank(level) is not None:
        level = _down(honoured, level) if lowered else _up(honoured, level)
    elif lowered and level == "none" and not takes_none and not known:
        # Nobody has said this endpoint takes "none"; "low" nearly every API does.
        level = lower_effort(configured, "low") or configured
    return level
