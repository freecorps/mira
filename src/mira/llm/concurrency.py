"""How many requests one endpoint gets at once, process-wide.

A subscription endpoint caps concurrent requests per key — OpenDesign Cloud's
Plus plan at 20 — and a review does not ask how many it is making: shuffled
passes × review parts × concurrent reviews, plus the security pass, plus
indexing, plus a chat reply. Two days of production made 1,566 requests the
endpoint refused with ``tier_limit_exceeded`` ("membership concurrency limit
exceeded: 21/20"), each one a pass, a part or an index batch dying after it had
already spent tokens.

So every request to an endpoint with a known limit holds one of its slots for
as long as it is in flight (:class:`EndpointLimiter`, one per endpoint and
event loop). Background work — reviews, indexing, the security pass — gets the
limit minus a little headroom, so a chat or thread reply never waits behind a
large review. A refusal for concurrency that still happens (another process on
the same key) is not a failure: the request gives its slot back, the limiter
lowers its ceiling for a minute, and the request waits and goes again
(:func:`is_concurrency_refusal`, :func:`refusal_wait`).

The limit comes from, in order: ``llm.max_concurrent_requests``, the stored
endpoint's ``max_concurrency``, the concurrency of the plan the endpoint is on
(``plans.<id>.concurrency`` in ``providers.json``), and the profile's
``max_concurrency``. None of them set means no limit, as before.

Two more context-local signals live here because the HTTP layer is where they
are observed:

* :func:`background_lane` marks the calls made inside it as background work.
* :func:`watch_first_response` lets a caller learn when the first reply of the
  calls it makes starts arriving — the moment its prompt prefix is in the
  endpoint's cache, which is what the shuffled passes of a review part wait
  for before they send the same prefix (see :mod:`mira.core.multipass`).
"""

from __future__ import annotations

import asyncio
import contextlib
import contextvars
import logging
import random
import re
import time
import weakref
from collections import deque
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any

logger = logging.getLogger(__name__)

# How long the ceiling stays lowered after the endpoint refused a request for
# concurrency: long enough for whoever else holds the key's slots to finish a
# call or two, short enough that a one-off does not halve a review's pace.
_THROTTLE_SECONDS = 60.0
# Waits between attempts of a refused request, when the refusal names no time.
_REFUSAL_MIN_WAIT = 2.0
_REFUSAL_MAX_WAIT = 30.0

# Words a refusal for too many requests in flight carries. OpenDesign says
# `tier_limit_exceeded` / "membership concurrency limit exceeded: 21/20";
# the others are how other gateways put the same thing.
_CONCURRENCY_HINTS = (
    "tier_limit_exceeded",
    "concurrency limit",
    "concurrency_limit",
    "concurrent request",
    "too many concurrent",
    "max concurrency",
)
_RESET_AT = re.compile(
    r"resets?\s+(?:at\s+)?(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z?)", re.I
)


def is_concurrency_refusal(status: int, text: str) -> bool:
    """Is this response the endpoint saying "too many requests at once"?

    Not a quota and not a bad request: the same request goes through once a
    slot frees up. OpenDesign answers it with a 400, which is why the status
    alone cannot tell.
    """
    if status not in (400, 403, 409, 429, 503):
        return False
    lowered = (text or "").lower()
    return any(hint in lowered for hint in _CONCURRENCY_HINTS)


def refusal_wait(text: str, attempt: int, *, now: float | None = None) -> float:
    """Seconds to wait before sending a concurrency-refused request again.

    The reset time the refusal names, when it names one that is near, and
    otherwise an exponential step from 2s to 30s, with jitter so the
    requests refused together do not come back together.
    """
    match = _RESET_AT.search(text or "")
    if match:
        stamp = match.group(1)
        try:
            at = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
            if at.tzinfo is None:
                at = at.replace(tzinfo=UTC)
            current = datetime.fromtimestamp(now if now is not None else time.time(), UTC)
            seconds = (at - current).total_seconds()
            if 0 < seconds <= _REFUSAL_MAX_WAIT:
                return seconds + random.uniform(0, 1.0)
        except ValueError:
            pass
    step = min(_REFUSAL_MAX_WAIT, _REFUSAL_MIN_WAIT * (2 ** max(0, attempt - 1)))
    return step + random.uniform(0, min(1.0, step))


class EndpointLimiter:
    """The slots of one endpoint, first come first served.

    ``limit`` slots in all; background work may hold ``limit - headroom`` of
    them (at least one), so the rest stay free for interactive calls. A
    waiting interactive call may take a slot a background call ahead of it
    could not.
    """

    def __init__(self, limit: int, headroom: int = 0) -> None:
        self.limit = max(1, int(limit))
        self.headroom = max(0, int(headroom))
        self.in_use = 0
        self._waiters: deque[tuple[asyncio.Future[None], bool]] = deque()
        self._lowered: int | None = None
        self._lowered_until = 0.0

    def configure(self, limit: int, headroom: int) -> None:
        self.limit = max(1, int(limit))
        self.headroom = max(0, int(headroom))
        self._wake()

    def ceiling(self, background: bool) -> int:
        cap = self.limit
        if self._lowered is not None:
            if time.monotonic() < self._lowered_until:
                cap = min(cap, self._lowered)
            else:
                self._lowered = None
        if background:
            cap -= self.headroom
        return max(1, cap)

    @property
    def waiting(self) -> int:
        return sum(1 for fut, _ in self._waiters if not fut.done())

    async def acquire(self, background: bool) -> None:
        # Hand out what the waiters ahead can take first; whoever is still
        # waiting then cannot fit, so a call that fits now (an interactive one
        # past background waiters held at their ceiling) jumps no fair queue.
        self._wake()
        if self.in_use < self.ceiling(background):
            self.in_use += 1
            return
        fut: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        self._waiters.append((fut, background))
        try:
            await fut
        except asyncio.CancelledError:
            if fut.done() and not fut.cancelled():
                # The slot was handed over as the wait was cancelled: give it
                # back, or it is gone for the life of the process.
                self.release()
            else:
                with contextlib.suppress(ValueError):
                    self._waiters.remove((fut, background))
            raise

    def release(self) -> None:
        self.in_use = max(0, self.in_use - 1)
        self._wake()

    def throttle(self) -> None:
        """The endpoint refused us for concurrency: hold fewer for a while.

        Someone else is using the key's slots — another Mira process, a person
        in an editor — so the ceiling drops to what is in flight now, less
        the refused request.
        """
        lowered = max(1, self.in_use - 1)
        self._lowered = lowered if self._lowered is None else min(self._lowered, lowered)
        self._lowered_until = time.monotonic() + _THROTTLE_SECONDS

    def _wake(self) -> None:
        kept: deque[tuple[asyncio.Future[None], bool]] = deque()
        while self._waiters:
            fut, background = self._waiters.popleft()
            if fut.done():
                continue
            if self.in_use < self.ceiling(background):
                self.in_use += 1
                fut.set_result(None)
            else:
                kept.append((fut, background))
        self._waiters = kept


# One table per event loop: a future belongs to the loop that made it, and
# tests (and the CLI) run several loops in one process.
_limiters: weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, dict[str, EndpointLimiter]] = (
    weakref.WeakKeyDictionary()
)


def limiter(key: str, limit: int, headroom: int) -> EndpointLimiter:
    """The running loop's limiter for ``key``, sized to ``limit`` / ``headroom``."""
    loop = asyncio.get_running_loop()
    table = _limiters.setdefault(loop, {})
    found = table.get(key)
    if found is None:
        found = table[key] = EndpointLimiter(limit, headroom)
    elif found.limit != limit or found.headroom != headroom:
        found.configure(limit, headroom)
    return found


def reset() -> None:
    """Forget every limiter. For tests."""
    _limiters.clear()


def _positive_int(value: Any) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return 0
    return number if number > 0 else 0


def limit_for(config: Any, profile: dict) -> tuple[int, int] | None:
    """``(limit, headroom)`` for a client's endpoint, or None for no limit.

    See the module docstring for where the limit comes from. The headroom is
    the profile's ``concurrency_headroom`` (2 by default), never more than a
    quarter of the limit, so a small plan is not left with one slot.
    """
    limit = _positive_int(getattr(config, "max_concurrent_requests", 0))
    if not limit:
        limit = _positive_int(profile.get("max_concurrency"))
        plan_id = str(profile.get("plan") or getattr(config, "plan", "") or "").strip().lower()
        plans = profile.get("plans")
        plan = plans.get(plan_id) if isinstance(plans, dict) and plan_id else None
        if isinstance(plan, dict) and _positive_int(plan.get("concurrency")):
            limit = _positive_int(plan.get("concurrency"))
        if _positive_int(profile.get("endpoint_max_concurrency")):
            limit = _positive_int(profile.get("endpoint_max_concurrency"))
    if not limit:
        return None
    headroom = profile.get("concurrency_headroom")
    headroom = 2 if headroom is None else max(0, _positive_int(headroom))
    return limit, min(headroom, limit // 4)


# ── Context-local signals ───────────────────────────────────────────

_background: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "mira_llm_background", default=False
)
_first_response: contextvars.ContextVar[asyncio.Event | None] = contextvars.ContextVar(
    "mira_llm_first_response", default=None
)

# Purposes that are background work wherever they run.
_BACKGROUND_PURPOSES = frozenset({"indexing", "security"})


@contextlib.contextmanager
def background_lane() -> Iterator[None]:
    """Calls made inside this block (and the tasks it starts) are background work."""
    token = _background.set(True)
    try:
        yield
    finally:
        _background.reset(token)


def is_background(purpose: str = "") -> bool:
    return _background.get() or purpose in _BACKGROUND_PURPOSES


@contextlib.contextmanager
def watch_first_response(event: asyncio.Event) -> Iterator[None]:
    """Set ``event`` as soon as a reply to a call made in this block starts arriving."""
    token = _first_response.set(event)
    try:
        yield
    finally:
        _first_response.reset(token)


def first_response() -> None:
    """A reply has started arriving: tell whoever is watching for it."""
    event = _first_response.get()
    if event is not None and not event.is_set():
        event.set()
