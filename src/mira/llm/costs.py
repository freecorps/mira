"""The cost view: what each endpoint and model spent, from the usage ledger.

One row per endpoint and model over the last N days — calls, input, the part
of it served from the prompt cache and the hit rate that makes, output, the
estimated USD and plan credits, and a month projected from the pace so far.
The figures are Mira's estimates from the registry's prices
(:mod:`mira.llm.registry`); a model the registry has no price for is counted
in ``unpriced_calls`` rather than costed at a guess.
"""

from __future__ import annotations

import time
from typing import Any

_DAY = 86400.0
# The shortest span a projection is extrapolated from: one review an hour ago
# is not a month's pace multiplied by seven hundred.
_MIN_SPAN_DAYS = 1.0


def _labels(db: Any, config: Any) -> dict[str, tuple[str, float]]:
    """``{endpoint id: (label, cost scale)}`` for every endpoint the ledger names."""
    from mira.llm import endpoints, key_providers

    out: dict[str, tuple[str, float]] = {}
    for endpoint in endpoints.all_endpoints(db).values():
        out[endpoint.id] = (endpoint.label or endpoint.id, endpoint.cost_scale or 1.0)
    profile = key_providers.config_profile(config)
    scale = getattr(config, "cost_scale", 1.0) or 1.0
    out[endpoints.CONFIG_ID] = (
        str(profile.get("label") or "") or "Endpoint from mira.yaml",
        scale if profile else 1.0,
    )
    return out


def _endpoint_label(endpoint_id: str, labels: dict[str, tuple[str, float]]) -> tuple[str, float]:
    if endpoint_id in labels:
        return labels[endpoint_id]
    if endpoint_id.startswith("oauth:"):
        from mira.oauth import registry as oauth_registry

        spec = oauth_registry.get(endpoint_id[len("oauth:") :])
        return (spec.label if spec is not None else endpoint_id, 1.0)
    if endpoint_id == "bedrock":
        return ("AWS Bedrock", 1.0)
    # An endpoint since removed: its rows still count, under the id they had.
    return (f"{endpoint_id} (removed)", 1.0)


def report(db: Any, *, days: int, config: Any = None, now: float | None = None) -> dict[str, Any]:
    """Per endpoint and model totals over the last ``days`` days, plus the sum."""
    from mira.llm import ledger, registry

    now = now if now is not None else time.time()
    since = now - days * _DAY
    raw = db.llm_usage_summary(since=since) if hasattr(db, "llm_usage_summary") else []
    labels = _labels(db, config)

    grouped: dict[tuple[str, str], dict[str, Any]] = {}
    for row in raw:
        key = (row["endpoint"], row["model"])
        label, scale = _endpoint_label(row["endpoint"], labels)
        entry = grouped.get(key)
        if entry is None:
            info = registry.get(row["model"], row["provider"] or None) or {}
            entry = grouped[key] = {
                "endpoint": row["endpoint"],
                "endpoint_label": label,
                "provider": row["provider"],
                "model": row["model"],
                "model_label": str(info.get("label") or row["model"]),
                "calls": 0,
                "prompt_tokens": 0,
                "cached_tokens": 0,
                "completion_tokens": 0,
                "reasoning_tokens": 0,
                "cost_usd": 0.0,
                "credits": 0.0,
                "unpriced_calls": 0,
                "purposes": {},
                "first_at": row["first_at"],
                "last_at": row["last_at"],
            }
        for field in (
            "calls",
            "prompt_tokens",
            "cached_tokens",
            "completion_tokens",
            "reasoning_tokens",
            "cost_usd",
            "unpriced_calls",
        ):
            entry[field] += row[field]
        entry["credits"] += row["credits"] * scale
        purpose = row["purpose"] or "other"
        entry["purposes"][purpose] = entry["purposes"].get(purpose, 0) + row["calls"]
        entry["first_at"] = min(entry["first_at"], row["first_at"])
        entry["last_at"] = max(entry["last_at"], row["last_at"])

    first_seen = min((e["first_at"] for e in grouped.values()), default=now)
    span_days = max(_MIN_SPAN_DAYS, min(float(days), (now - first_seen) / _DAY))

    def finish(entry: dict[str, Any]) -> dict[str, Any]:
        prompt = entry["prompt_tokens"]
        entry["cache_hit_rate"] = round(entry["cached_tokens"] / prompt, 4) if prompt else 0.0
        entry["cost_usd"] = round(entry["cost_usd"], 6)
        entry["credits"] = round(entry["credits"], 6)
        entry["projected_month_usd"] = round(entry["cost_usd"] / span_days * 30, 4)
        entry["projected_month_credits"] = round(entry["credits"] / span_days * 30, 4)
        return entry

    rows = sorted(
        (finish(e) for e in grouped.values()),
        key=lambda e: (-e["cost_usd"], e["endpoint_label"].lower(), e["model"]),
    )
    total: dict[str, Any] = {
        "calls": 0,
        "prompt_tokens": 0,
        "cached_tokens": 0,
        "completion_tokens": 0,
        "reasoning_tokens": 0,
        "cost_usd": 0.0,
        "credits": 0.0,
        "unpriced_calls": 0,
    }
    for row in rows:
        for field in total:
            total[field] += row[field]
    total = finish({**total, "purposes": {}})
    total.pop("purposes", None)
    return {
        "days": days,
        "since": since,
        "span_days": round(span_days, 2),
        "rows": rows,
        "total": total,
        "ledger": ledger.stats(),
        "note": (
            "Estimated from the registry's per-model prices, cached input at the "
            "cache-read rate. Plan credits are the cost times each model's credit "
            "multiplier, scaled by the endpoint's calibration."
        ),
    }
