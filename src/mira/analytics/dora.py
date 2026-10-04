"""DORA metrics and the PR cycle-time breakdown, from stored PR data.

Everything here is a *proxy* built from what the review-insights tables
already hold (``pull_requests`` + ``deployments``); Mira does not see a
deploy pipeline. The proxies, and why:

Deployment frequency
    ``merges``: PRs merged into the repository's default branch (a PR whose
    base or default branch is unknown counts — most installs merge straight
    to the default branch). ``releases``: published releases/tags.

Lead time for changes
    First commit on the PR → merge. When the first commit is unknown the PR's
    creation time stands in, which under-reports by the time spent before the
    PR was opened.

Change failure rate
    Failure-fix PRs / all deployments-by-merge in the window. A failure fix is
    a merged PR whose title starts with a configured prefix (``Revert``,
    ``hotfix`` …) or that carries a configured label.

MTTR (time to restore) proxy
    For a revert whose title names the reverted PR (``Revert "<title>"``, as
    GitHub writes it), the reverted PR's merge → the revert's merge: how long
    the bad change was live. Otherwise the fix PR's own open → merge time:
    how long it took to ship the fix once someone started on it.

Cycle time
    Time to first review (open → first human review), time to approval
    (open → first approval), time to merge (open → merge), and the total from
    first commit. Medians, so one PR left open over a holiday does not set
    the number.
"""

from __future__ import annotations

import re
import statistics
import time
from dataclasses import asdict, dataclass, field
from typing import Any

DAY = 86400.0
HOUR = 3600.0
WEEK = 7 * DAY

_REVERT_TITLE = re.compile(r'^\s*revert\s+"(?P<title>.+)"\s*$', re.IGNORECASE)


def _median(values: list[float]) -> float | None:
    clean = [v for v in values if v >= 0]
    return float(statistics.median(clean)) if clean else None


def is_default_branch_merge(row: dict[str, Any]) -> bool:
    base = row.get("base_branch") or ""
    default = row.get("default_branch") or ""
    return not base or not default or base == default


def is_failure_fix(
    row: dict[str, Any], title_prefixes: list[str], failure_labels: list[str]
) -> bool:
    title = (row.get("title") or "").strip().lower()
    for prefix in title_prefixes:
        p = prefix.strip().lower()
        if p and (title.startswith(p) or title.startswith(f"[{p}]")):
            return True
    wanted = {lbl.strip().lower() for lbl in failure_labels if lbl.strip()}
    return any(lbl.lower() in wanted for lbl in row.get("labels") or [])


# DORA performance bands (from the State of DevOps reports, simplified to the
# boundaries a dashboard can show), checked best band first.
def _band_deploy(per_day: float) -> str:
    if per_day >= 1:
        return "elite"
    if per_day >= 1 / 7:
        return "high"
    if per_day >= 1 / 30:
        return "medium"
    return "low"


def _band_lead(secs: float | None) -> str | None:
    if secs is None:
        return None
    if secs < DAY:
        return "elite"
    if secs < WEEK:
        return "high"
    if secs < 30 * DAY:
        return "medium"
    return "low"


def _band_cfr(rate: float | None) -> str | None:
    if rate is None:
        return None
    if rate <= 0.05:
        return "elite"
    if rate <= 0.15:
        return "high"
    if rate <= 0.30:
        return "medium"
    return "low"


def _band_mttr(secs: float | None) -> str | None:
    if secs is None:
        return None
    if secs < HOUR:
        return "elite"
    if secs < DAY:
        return "high"
    if secs < WEEK:
        return "medium"
    return "low"


@dataclass
class CycleTime:
    time_to_first_review_secs: float | None = None
    time_to_approval_secs: float | None = None
    time_to_merge_secs: float | None = None
    coding_time_secs: float | None = None  # first commit → PR opened
    total_cycle_secs: float | None = None  # first commit (or open) → merge
    prs: int = 0


@dataclass
class DoraWindow:
    start: float
    end: float
    deployments: int = 0
    deployments_per_day: float = 0.0
    deployment_band: str = "low"
    lead_time_secs: float | None = None
    lead_time_band: str | None = None
    change_failure_rate: float | None = None
    change_failure_band: str | None = None
    failures: int = 0
    mttr_secs: float | None = None
    mttr_band: str | None = None
    merged_prs: int = 0
    cycle: CycleTime = field(default_factory=CycleTime)


@dataclass
class DoraBucket:
    start: float
    deployments: int = 0
    failures: int = 0
    lead_time_secs: float | None = None


@dataclass
class DoraReport:
    days: int
    deployment_source: str
    current: DoraWindow
    previous: DoraWindow
    series: list[DoraBucket] = field(default_factory=list)
    bucket_secs: float = DAY
    failures: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _cycle(rows: list[dict[str, Any]]) -> CycleTime:
    ttfr, tta, ttm, coding, total = [], [], [], [], []
    for r in rows:
        created = r["created_at"]
        if created <= 0:
            continue
        if r["first_review_at"] >= created:
            ttfr.append(r["first_review_at"] - created)
        if r["first_approval_at"] >= created:
            tta.append(r["first_approval_at"] - created)
        if r["merged_at"] >= created:
            ttm.append(r["merged_at"] - created)
        fc = r["first_commit_at"]
        if 0 < fc <= created:
            coding.append(created - fc)
        start = fc if 0 < fc <= r["merged_at"] else created
        if r["merged_at"] >= start:
            total.append(r["merged_at"] - start)
    return CycleTime(
        time_to_first_review_secs=_median(ttfr),
        time_to_approval_secs=_median(tta),
        time_to_merge_secs=_median(ttm),
        coding_time_secs=_median(coding),
        total_cycle_secs=_median(total),
        prs=len(rows),
    )


def _lead_time(row: dict[str, Any]) -> float | None:
    fc = row["first_commit_at"]
    start = fc if 0 < fc <= row["merged_at"] else row["created_at"]
    if start <= 0 or row["merged_at"] < start:
        return None
    return row["merged_at"] - start


def _restore_time(fix: dict[str, Any], by_title: dict[str, list[dict[str, Any]]]) -> float | None:
    """How long the failure was live: reverted PR merge → revert merge, or the
    fix PR's own open → merge when the culprit cannot be identified."""
    m = _REVERT_TITLE.match(fix.get("title") or "")
    if m:
        candidates = [
            r
            for r in by_title.get(m.group("title").strip().lower(), [])
            if r["owner"] == fix["owner"]
            and r["repo"] == fix["repo"]
            and 0 < r["merged_at"] <= fix["merged_at"]
            and r["number"] != fix["number"]
        ]
        if candidates:
            culprit = max(candidates, key=lambda r: r["merged_at"])
            return fix["merged_at"] - culprit["merged_at"]
    if fix["created_at"] > 0 and fix["merged_at"] >= fix["created_at"]:
        return fix["merged_at"] - fix["created_at"]
    return None


def _window(
    start: float,
    end: float,
    rows: list[dict[str, Any]],
    deployments: list[dict[str, Any]],
    *,
    source: str,
    title_prefixes: list[str],
    failure_labels: list[str],
    by_title: dict[str, list[dict[str, Any]]],
) -> tuple[DoraWindow, list[dict[str, Any]]]:
    merged = [r for r in rows if start <= r["merged_at"] < end and is_default_branch_merge(r)]
    if source == "releases":
        deploys = sum(1 for d in deployments if start <= d["deployed_at"] < end)
    else:
        deploys = len(merged)
    days = max((end - start) / DAY, 1e-9)
    per_day = deploys / days

    leads = [lt for lt in (_lead_time(r) for r in merged) if lt is not None]
    lead = _median(leads)

    fixes = [r for r in merged if is_failure_fix(r, title_prefixes, failure_labels)]
    cfr = (len(fixes) / len(merged)) if merged else None
    restores = [rt for rt in (_restore_time(f, by_title) for f in fixes) if rt is not None]
    mttr = _median(restores)

    window = DoraWindow(
        start=start,
        end=end,
        deployments=deploys,
        deployments_per_day=round(per_day, 4),
        deployment_band=_band_deploy(per_day),
        lead_time_secs=lead,
        lead_time_band=_band_lead(lead),
        change_failure_rate=(round(cfr, 4) if cfr is not None else None),
        change_failure_band=_band_cfr(cfr),
        failures=len(fixes),
        mttr_secs=mttr,
        mttr_band=_band_mttr(mttr),
        merged_prs=len(merged),
        cycle=_cycle(merged),
    )
    fix_rows = [
        {
            "owner": f["owner"],
            "repo": f["repo"],
            "number": f["number"],
            "title": f["title"],
            "url": f.get("url", ""),
            "merged_at": f["merged_at"],
            "restore_secs": _restore_time(f, by_title),
        }
        for f in fixes
    ]
    return window, fix_rows


def compute_dora(
    rows: list[dict[str, Any]],
    deployments: list[dict[str, Any]],
    *,
    days: int,
    deployment_source: str = "merges",
    title_prefixes: list[str] | None = None,
    failure_labels: list[str] | None = None,
    now: float = 0.0,
) -> DoraReport:
    """DORA + cycle time for the last ``days`` and the ``days`` before that.

    ``rows`` are merged PRs as ``AppDatabase.get_delivery_rows`` returns them
    and should reach back at least ``2 × days`` (plus some slack, so a revert
    can find the PR it reverted).
    """
    now = now or time.time()
    window = days * DAY
    prefixes = title_prefixes if title_prefixes is not None else ["revert", "hotfix"]
    labels = failure_labels if failure_labels is not None else ["hotfix", "revert"]
    by_title: dict[str, list[dict[str, Any]]] = {}
    for r in rows:
        by_title.setdefault((r.get("title") or "").strip().lower(), []).append(r)
    kw: dict[str, Any] = {
        "source": deployment_source,
        "title_prefixes": prefixes,
        "failure_labels": labels,
        "by_title": by_title,
    }
    current, fixes = _window(now - window, now, rows, deployments, **kw)
    previous, _ = _window(now - 2 * window, now - window, rows, deployments, **kw)

    bucket = DAY if days <= 31 else WEEK
    series: list[DoraBucket] = []
    start = now - window
    while start < now:
        end = min(start + bucket, now)
        win, _ = _window(start, end, rows, deployments, **kw)
        series.append(
            DoraBucket(
                start=start,
                deployments=win.deployments,
                failures=win.failures,
                lead_time_secs=win.lead_time_secs,
            )
        )
        start = end

    return DoraReport(
        days=days,
        deployment_source=deployment_source,
        current=current,
        previous=previous,
        series=series,
        bucket_secs=bucket,
        failures=sorted(fixes, key=lambda f: -f["merged_at"])[:50],
    )
