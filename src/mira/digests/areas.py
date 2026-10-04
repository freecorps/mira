"""Sorting changes into areas by the paths they touched.

An area is either named in ``digests.areas`` (a name and the path patterns it
covers, in the gate's gitignore-shaped syntax) or, for a path no named area
claims, the first ``area_depth`` directories of the path. A change belongs to
every area one of its files falls in, so a pull request that touched the API
and the docs is listed under both.

The number of areas is capped: a monorepo with ninety top-level directories
would otherwise produce a digest nobody reads and a prompt that spends its
budget on headings. The smallest areas past the cap are folded into one.
"""

from __future__ import annotations

from collections.abc import Callable

from mira.digests.models import AreaDigest, Change
from mira.gate.paths import match_any

MAX_AREAS = 20
ROOT_AREA = "(repository root)"
UNKNOWN_AREA = "(files not listed)"
OTHER_AREAS = "Other areas"


def areas_for_path(path: str, areas: dict[str, list[str]], depth: int) -> list[str]:
    named = [name for name, patterns in areas.items() if match_any(path, patterns)]
    if named:
        return named
    parts = [p for p in path.strip("/").split("/") if p]
    if len(parts) <= 1:
        return [ROOT_AREA]
    return ["/".join(parts[: min(depth, len(parts) - 1)]) + "/"]


def group_by_area(
    changes: list[Change],
    areas: dict[str, list[str]] | None = None,
    *,
    depth: int = 1,
    prefix_repo: bool = False,
    max_areas: int = MAX_AREAS,
) -> list[AreaDigest]:
    """Areas, busiest first, each with its changes newest first.

    ``prefix_repo`` names an area ``repo: area`` — the org-wide digest, where
    two repositories can both have a ``src/``.
    """
    areas = areas or {}
    buckets: dict[str, list[Change]] = {}
    for change in changes:
        names: list[str] = []
        for path in change.files:
            for name in areas_for_path(path, areas, depth):
                if name not in names:
                    names.append(name)
        if not names:
            names = [UNKNOWN_AREA]
        for name in names:
            label = f"{change.repo}: {name}" if prefix_repo and change.repo else name
            buckets.setdefault(label, []).append(change)

    ordered = sorted(buckets.items(), key=lambda item: (-len(item[1]), item[0]))
    if len(ordered) > max_areas:
        kept, rest = ordered[: max_areas - 1], ordered[max_areas - 1 :]
        folded: list[Change] = []
        seen: set[str] = set()
        for _, bucket in rest:
            for change in bucket:
                if change.key not in seen:
                    seen.add(change.key)
                    folded.append(change)
        ordered = [*kept, (OTHER_AREAS, folded)]
    return [
        AreaDigest(name=name, changes=sorted(bucket, key=lambda c: c.landed_at, reverse=True))
        for name, bucket in ordered
    ]


def attach_context(areas: list[AreaDigest], lookup: Callable[[str], str] | None) -> None:
    """Fill each area's ``context`` from ``lookup(area_name)``. Best effort."""
    if lookup is None:
        return
    for area in areas:
        try:
            area.context = (lookup(area.name) or "").strip()
        except Exception:  # noqa: BLE001 - context only helps the model
            area.context = ""
