"""Multi-run ensemble: review the same chunk N times, keep consensus findings.

Variance-driven false positives flicker across runs while real findings
recur, so a majority vote across independent samples filters both noise
and plausibility-bias FPs. Costs N× review-tier tokens; runs fire in
parallel so wall clock stays roughly flat.
"""

from __future__ import annotations

import copy
from collections.abc import Callable

from mira.core.noise_filter import _is_duplicate
from mira.models import ReviewComment

Cluster = list[tuple[int, ReviewComment]]


def cluster_runs(
    runs: list[list[ReviewComment]],
    same: Callable[[ReviewComment, ReviewComment], bool] = _is_duplicate,
) -> list[Cluster]:
    """Group the comments of several runs into clusters of one finding each.

    Each cluster holds ``(run_index, comment)`` pairs; a comment joins the
    first cluster holding one ``same`` says it matches. Shared by the
    ensemble vote below and the shuffled multi-pass vote (core/multipass.py).
    """
    clusters: list[Cluster] = []
    for run_idx, run in enumerate(runs):
        for comment in run:
            for cluster in clusters:
                if any(same(comment, other) for _, other in cluster):
                    cluster.append((run_idx, comment))
                    break
            else:
                clusters.append([(run_idx, comment)])
    return clusters


def cluster_votes(cluster: Cluster) -> int:
    """How many distinct runs reported this finding."""
    return len({run_idx for run_idx, _ in cluster})


def cluster_representative(cluster: Cluster) -> ReviewComment:
    """The cluster's highest-confidence member, its confidence set to the mean.

    A copy: the drafts stay as each run wrote them, for the audit.
    """
    rep = copy.copy(max(cluster, key=lambda pair: pair[1].confidence)[1])
    rep.confidence = round(sum(c.confidence for _, c in cluster) / len(cluster), 3)
    return rep


def merge_ensemble_runs(
    runs: list[list[ReviewComment]],
    min_votes: int | None = None,
) -> list[ReviewComment]:
    """Cluster comments across runs and keep findings seen in >= min_votes runs.

    ``min_votes`` defaults to a strict majority of the runs. The kept
    representative is the cluster's highest-confidence member, with its
    confidence replaced by the cluster mean — recurrence strength doubles
    as a calibration signal.
    """
    if not runs:
        return []
    if len(runs) == 1:
        return list(runs[0])
    if min_votes is None:
        min_votes = len(runs) // 2 + 1

    merged: list[ReviewComment] = []
    for cluster in cluster_runs(runs):
        if cluster_votes(cluster) < min_votes:
            continue
        merged.append(cluster_representative(cluster))
    return merged
