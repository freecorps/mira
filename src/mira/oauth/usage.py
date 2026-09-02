"""Rate-limit snapshots for signed-in accounts, and the choice between them.

A plan's allowance is metered in windows — ChatGPT has a short one (five
hours) and a long one (a week) — and the backend reports where each window
stands on every response, as headers, and on demand from a usage endpoint.
This module is the shape of that information and the two decisions it feeds:

* the dashboard shows each account's windows, so an operator can see which
  one is about to run dry before a review does;
* the LLM client, when it is allowed to rotate across accounts, sends the
  next call to the account with the most headroom and skips one the backend
  has already refused.

Nothing here is provider-specific. A provider spec parses its own headers
and endpoint into :class:`UsageSnapshot`; everything downstream reads only
that.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

# A window whose reported length falls within these bounds is shown under the
# familiar name rather than as a raw duration.
_WINDOW_NAMES: tuple[tuple[int, int, str], ...] = (
    (240, 360, "5-hour"),
    (1380, 1500, "daily"),
    (9000, 11000, "weekly"),
    (40000, 46000, "monthly"),
)


def window_name(minutes: int | None) -> str:
    """A human name for a window length ("5-hour", "weekly"), or a duration."""
    if not minutes or minutes <= 0:
        return "window"
    for low, high, name in _WINDOW_NAMES:
        if low <= minutes <= high:
            return name
    if minutes % 1440 == 0:
        return f"{minutes // 1440}-day"
    if minutes % 60 == 0:
        return f"{minutes // 60}-hour"
    return f"{minutes}-minute"


@dataclass
class UsageWindow:
    """One metered window: how much of it is spent, and when it comes back."""

    used_percent: float = 0.0
    window_minutes: int | None = None
    resets_at: float | None = None

    @property
    def name(self) -> str:
        return window_name(self.window_minutes)

    def exhausted(self, now: float | None = None) -> bool:
        """True while the window is fully spent and has not reset yet."""
        if self.used_percent < 100.0:
            return False
        if self.resets_at is None:
            return True
        return (now if now is not None else time.time()) < self.resets_at

    def to_dict(self) -> dict[str, Any]:
        return {
            "used_percent": round(float(self.used_percent), 2),
            "window_minutes": self.window_minutes,
            "resets_at": self.resets_at,
            "name": self.name,
        }

    @classmethod
    def from_dict(cls, data: Any) -> UsageWindow | None:
        if not isinstance(data, dict):
            return None
        try:
            used = float(data.get("used_percent", 0.0) or 0.0)
        except (TypeError, ValueError):
            used = 0.0
        return cls(
            used_percent=used,
            window_minutes=_as_int(data.get("window_minutes")),
            resets_at=_as_float(data.get("resets_at")),
        )


@dataclass
class UsageSnapshot:
    """What is known about one account's allowance, and how current it is.

    ``primary`` and ``secondary`` are the short and long windows. ``credits``
    is whatever pay-as-you-go balance the plan carries, as the provider
    reports it. ``exhausted_until`` is Mira's own note: the account answered
    a 429, and this is when it is worth trying again — the windows alone
    cannot say that, because a refusal can arrive before a window reads 100%.
    """

    primary: UsageWindow | None = None
    secondary: UsageWindow | None = None
    credits: dict[str, Any] | None = None
    plan: str = ""
    limit_reached: bool = False
    source: str = ""  # "headers" | "endpoint"
    fetched_at: float = field(default_factory=time.time)
    exhausted_until: float = 0.0
    last_used_at: float = 0.0

    def has_data(self) -> bool:
        return self.primary is not None or self.secondary is not None or self.credits is not None

    def available(self, now: float | None = None) -> bool:
        """Can this account take a call right now, as far as we know?"""
        now = now if now is not None else time.time()
        if self.exhausted_until > now:
            return False
        return not any(
            window is not None and window.exhausted(now)
            for window in (self.primary, self.secondary)
        )

    def headroom(self) -> float:
        """Percent of allowance left in the tightest window (100 = untouched)."""
        used = [w.used_percent for w in (self.primary, self.secondary) if w is not None]
        if not used:
            return 100.0
        return max(0.0, 100.0 - max(used))

    def merge_bookkeeping(self, previous: UsageSnapshot | None) -> UsageSnapshot:
        """Carry Mira's own notes over from the snapshot this one replaces.

        The backend's report says nothing about a refusal we handled a minute
        ago or which account we last used; those live only here.
        """
        if previous is not None:
            self.exhausted_until = max(self.exhausted_until, previous.exhausted_until)
            self.last_used_at = max(self.last_used_at, previous.last_used_at)
            if not self.plan:
                self.plan = previous.plan
        return self

    def to_dict(self) -> dict[str, Any]:
        return {
            "primary": self.primary.to_dict() if self.primary else None,
            "secondary": self.secondary.to_dict() if self.secondary else None,
            "credits": self.credits,
            "plan": self.plan,
            "limit_reached": self.limit_reached,
            "source": self.source,
            "fetched_at": self.fetched_at,
            "exhausted_until": self.exhausted_until,
            "last_used_at": self.last_used_at,
        }

    @classmethod
    def from_dict(cls, data: Any) -> UsageSnapshot | None:
        if not isinstance(data, dict):
            return None
        credits = data.get("credits")
        return cls(
            primary=UsageWindow.from_dict(data.get("primary")),
            secondary=UsageWindow.from_dict(data.get("secondary")),
            credits=credits if isinstance(credits, dict) else None,
            plan=str(data.get("plan", "") or ""),
            limit_reached=bool(data.get("limit_reached", False)),
            source=str(data.get("source", "") or ""),
            fetched_at=_as_float(data.get("fetched_at")) or 0.0,
            exhausted_until=_as_float(data.get("exhausted_until")) or 0.0,
            last_used_at=_as_float(data.get("last_used_at")) or 0.0,
        )


def choose_account(
    candidates: list[tuple[str, UsageSnapshot | None]],
    *,
    exclude: set[str] | None = None,
    now: float | None = None,
) -> str | None:
    """Pick the account the next call should go to.

    Accounts the backend has refused, or whose window is spent, are skipped.
    Of the rest, the one with the most headroom wins; ties go to the one
    used least recently, so equally fresh accounts take turns rather than one
    of them absorbing every call. An account with no snapshot at all counts
    as untouched — it has to be tried once to learn anything about it.

    Returns None when nothing is available, which the caller reports rather
    than hammering an account that has already said no.
    """
    now = now if now is not None else time.time()
    excluded = exclude or set()
    ranked: list[tuple[float, float, str]] = []
    for key, snapshot in candidates:
        if key in excluded:
            continue
        if snapshot is not None and not snapshot.available(now):
            continue
        headroom = snapshot.headroom() if snapshot is not None else 100.0
        last_used = snapshot.last_used_at if snapshot is not None else 0.0
        ranked.append((-headroom, last_used, key))
    if not ranked:
        return None
    ranked.sort()
    return ranked[0][2]


def _as_int(value: Any) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _as_float(value: Any) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None
