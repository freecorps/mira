"""One tolerant timestamp parser, shared by the three providers.

All three report commit times as ISO-8601, and all three spell it slightly
differently — a trailing ``Z``, a numeric offset, occasionally no offset at
all. Parsing it in three places would eventually mean three behaviours for the
same string, and the thing that consumes these values is a recency weighting:
a timestamp read as zero does not fail loudly, it quietly makes a contribution
look as old as it is possible to be.

An unparseable value returns ``0.0``, which every caller in this codebase
treats as "no date known" rather than as 1970.
"""

from __future__ import annotations

from datetime import UTC, datetime


def iso_to_epoch(value: str) -> float:
    """``2026-08-01T12:00:00Z`` → epoch seconds, or ``0.0`` if unreadable."""
    text = (value or "").strip()
    if not text:
        return 0.0
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return 0.0
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.timestamp()
