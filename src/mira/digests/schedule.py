"""When a digest is due, and which period it covers.

A schedule is a sequence of boundaries — every day at ``hour`` UTC, or every
``day`` at ``hour`` UTC. The period a digest covers is the interval that ends
at the most recent boundary, so a digest delivered on Monday at 09:00 covers
the previous Monday 09:00 up to that moment, and two runs that agree on the
clock agree on the period. The scheduler stores the last boundary it ran for;
a boundary at or before that one is never run again.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from mira.config import _WEEKDAYS, DigestsConfig


def interval(cfg: DigestsConfig) -> timedelta:
    return timedelta(days=1) if cfg.schedule == "daily" else timedelta(days=7)


def latest_boundary(now: datetime, cfg: DigestsConfig) -> datetime:
    """The most recent scheduled instant at or before ``now``."""
    now = now.astimezone(UTC) if now.tzinfo else now.replace(tzinfo=UTC)
    today = now.replace(hour=cfg.hour, minute=0, second=0, microsecond=0)
    if cfg.schedule == "daily":
        return today if today <= now else today - timedelta(days=1)
    target = _WEEKDAYS.index(cfg.day)
    candidate = today - timedelta(days=(now.weekday() - target) % 7)
    return candidate if candidate <= now else candidate - timedelta(days=7)


def period_ending(boundary: datetime, cfg: DigestsConfig) -> tuple[float, float]:
    """``(start, end)`` epoch seconds of the period that ends at ``boundary``."""
    return (boundary - interval(cfg)).timestamp(), boundary.timestamp()


def next_boundary(now: datetime, cfg: DigestsConfig) -> datetime:
    return latest_boundary(now, cfg) + interval(cfg)
