"""Turning signals into a ranked list, deterministically.

Everything here is arithmetic over facts already gathered. No provider, no
store, no model — which is what makes a suggestion reproducible: the same
signals under the same policy produce the same ranking, in the same order, on
any machine.

The scoring rule is deliberately simple enough to explain in one sentence:
**a person scores once per changed file they are connected to, weighted by how
that connection was made and how recently.** Owning four of the changed files
beats having edited one of them last week; having edited four of them beats
having edited one. Forty commits to the same file count once, because forty
commits to one file is one person who knows one file, and counting them all
would rank whoever rebases most at the top of every list.

Exclusions are checked in a fixed order and recorded rather than applied
silently. "Dana owns three of these files but opened the pull request" is a
more useful thing to show a reader than an empty suggestion list, and it is
also the only way anybody can debug a ranking that surprised them.
"""

from __future__ import annotations

from mira.triage.history import MAX_EVIDENCE_PER_SIGNAL, Touch, recency
from mira.triage.models import (
    Evidence,
    Exclusion,
    ExclusionReason,
    ReviewerCandidate,
    SignalContribution,
)
from mira.triage.policy import EffectiveTriagePolicy

# How many pieces of evidence one contribution keeps. The rest are counted in
# `raw` and dropped from the record: a stored run is read by a person.
MAX_EVIDENCE = MAX_EVIDENCE_PER_SIGNAL


def normalize(identity: str) -> str:
    """The spelling identities are *compared* on: lower-case, no leading ``@``."""
    return (identity or "").strip().lstrip("@").lower()


def kind_of(identity: str) -> str:
    """Whether this identity is a person, a team, or an email address.

    Teams and email addresses are ranked and shown, and are treated differently
    in two places: a team is never excluded for being the pull request's author
    (Mira does not resolve membership and will not guess at it), and an email
    address is masked when rendered publicly.
    """
    bare = normalize(identity)
    if "/" in bare:
        return "team"
    if "@" in bare:
        return "email"
    return "user"


def _display(identity: str) -> str:
    """The spelling a candidate is *shown* under: as written, without the ``@``."""
    return (identity or "").strip().lstrip("@")


def _ownership_contribution(paths: list[Evidence], *, weight: float) -> SignalContribution:
    owned = len(paths)
    return SignalContribution(
        kind="codeowners",
        raw=float(owned),
        weight=weight,
        # No recency on ownership: CODEOWNERS is a current statement by the
        # repository, not a record of something that happened once.
        score=round(weight * owned, 4),
        detail=f"owns {owned} of the changed file(s)",
        evidence=paths[:MAX_EVIDENCE],
    )


def _history_contribution(
    touches: list[Touch], *, kind: str, weight: float, now: float, window_days: int
) -> SignalContribution:
    total = 0.0
    for touch in touches:
        total += recency(touch.at, now=now, window_days=window_days)
    verb = "changed" if kind == "authored" else "reviewed changes to"
    return SignalContribution(
        kind=kind,
        raw=float(len(touches)),
        weight=weight,
        score=round(weight * total, 4),
        detail=f"{verb} {len(touches)} of the changed file(s)",
        evidence=[touch.evidence for touch in touches[:MAX_EVIDENCE]],
    )


def rank(
    *,
    policy: EffectiveTriagePolicy,
    owners: dict[str, list[Evidence]],
    authored: dict[str, list[Touch]],
    reviewed: dict[str, list[Touch]],
    pr_author: str = "",
    load: dict[str, int] | None = None,
    now: float,
) -> tuple[list[ReviewerCandidate], list[Exclusion]]:
    """Rank every identity the signals produced. Returns ``(candidates, excluded)``.

    Ties are broken by the number of distinct signals and then alphabetically.
    Both halves matter: somebody named by two signals is a better suggestion
    than somebody named by one at the same score, and the alphabetical fallback
    means the same inputs always produce the same order rather than whatever
    order a dictionary happened to iterate in.
    """
    load = {normalize(key): value for key, value in (load or {}).items()}
    author = normalize(pr_author)

    # Merge the three signals on the normalized spelling, keeping the first
    # display spelling seen — signals are visited in a fixed order, so which
    # one that is does not vary between runs.
    display: dict[str, str] = {}
    contributions: dict[str, list[SignalContribution]] = {}

    def _add(identity: str, contribution: SignalContribution) -> None:
        key = normalize(identity)
        if not key:
            return
        display.setdefault(key, _display(identity))
        contributions.setdefault(key, []).append(contribution)

    for identity, evidence in sorted(owners.items()):
        if not evidence:
            continue
        _add(identity, _ownership_contribution(evidence, weight=policy.weights.codeowners))
    for identity, touches in sorted(authored.items()):
        if not touches:
            continue
        _add(
            identity,
            _history_contribution(
                touches,
                kind="authored",
                weight=policy.weights.authored,
                now=now,
                window_days=policy.history_days,
            ),
        )
    for identity, touches in sorted(reviewed.items()):
        if not touches:
            continue
        _add(
            identity,
            _history_contribution(
                touches,
                kind="reviewed",
                weight=policy.weights.reviewed,
                now=now,
                window_days=policy.history_days,
            ),
        )

    candidates: list[ReviewerCandidate] = []
    excluded: list[Exclusion] = []

    for key in sorted(contributions):
        name = display[key]
        kind = kind_of(key)
        items = contributions[key]

        if kind == "user" and author and key == author:
            excluded.append(
                Exclusion(
                    identity=name,
                    reason=ExclusionReason.AUTHOR,
                    detail="opened this pull request",
                )
            )
            continue
        if kind == "user" and policy.is_bot(key):
            excluded.append(
                Exclusion(
                    identity=name,
                    reason=ExclusionReason.BOT,
                    detail="looks like a machine account",
                )
            )
            continue
        if policy.excluded(key):
            excluded.append(
                Exclusion(
                    identity=name,
                    reason=ExclusionReason.OPTED_OUT,
                    detail="is on the opt-out list",
                )
            )
            continue
        if not any(item.evidence for item in items):
            # Defensive rather than expected: every signal above attaches its
            # evidence. A name with none is a bug somewhere upstream, and the
            # right response to it is to drop the name, not to publish an
            # accusation nobody can check.
            excluded.append(
                Exclusion(
                    identity=name,
                    reason=ExclusionReason.NO_EVIDENCE,
                    detail="no signal could say why",
                )
            )
            continue

        base = round(sum(item.score for item in items), 4)
        open_reviews = int(load.get(key, 0)) if kind == "user" else 0
        penalty = round(policy.load_penalty * open_reviews, 4)
        score = round(max(0.0, base - penalty), 4)

        if score < policy.min_score:
            excluded.append(
                Exclusion(
                    identity=name,
                    reason=ExclusionReason.BELOW_THRESHOLD,
                    detail=f"scored {score:g}, below the {policy.min_score:g} floor",
                )
            )
            continue

        candidates.append(
            ReviewerCandidate(
                identity=name,
                kind=kind,
                score=score,
                contributions=sorted(items, key=lambda item: (-item.score, item.kind)),
                load_penalty=penalty,
                open_reviews=open_reviews,
            )
        )

    candidates.sort(key=lambda c: (-c.score, -len(c.contributions), c.identity.lower()))

    kept = candidates[: policy.max_suggestions]
    for candidate in candidates[policy.max_suggestions :]:
        excluded.append(
            Exclusion(
                identity=candidate.identity,
                reason=ExclusionReason.NOT_TOP_RANKED,
                detail=f"scored {candidate.score:g}, outside the top {policy.max_suggestions}",
            )
        )
    return kept, excluded
