"""Page sizes, cursors and the ceilings on one answer.

A read-only surface still has a cost, and it is paid by whoever runs Mira: a
model that asks for every finding in a repository gets a query, a serialisation
and a response, and asking again is free for it and not for the host. So every
listing is a page, every page has a ceiling the client cannot raise, and the
cursor that walks them is opaque.

The cursor is bound to the query that produced it. Offsets are the obvious
implementation and the obvious bug: a cursor taken from one filter and replayed
against another walks a different result set from a meaningless position, and
the client has no way to notice. Binding turns that into an error.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
from dataclasses import dataclass
from typing import Any

#: Hard ceiling, above whatever the configuration says. A misconfigured
#: `max_page_size` is still a page.
ABSOLUTE_MAX_PAGE_SIZE = 200

#: What a truncated string ends with. ASCII on purpose: this travels through
#: consoles Mira does not choose.
TRUNCATION_MARK = " ... [truncated]"


class InvalidCursor(ValueError):
    """A cursor that did not come from this query, or from Mira at all."""


def page_size(requested: int | None, *, configured: int) -> int:
    """The number of rows to read for one page.

    A client asking for more than the ceiling gets the ceiling rather than an
    error: the limit is Mira's to enforce, and the client has no way to learn
    it before asking.
    """
    ceiling = max(1, min(int(configured), ABSOLUTE_MAX_PAGE_SIZE))
    if requested is None:
        return ceiling
    try:
        wanted = int(requested)
    except (TypeError, ValueError):
        return ceiling
    return max(1, min(wanted, ceiling))


def _fingerprint(query: dict[str, Any]) -> str:
    payload = json.dumps(query, sort_keys=True, default=str, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def encode_cursor(query: dict[str, Any], offset: int) -> str:
    """An opaque position in *this* query's results."""
    payload = json.dumps({"q": _fingerprint(query), "o": int(offset)}, separators=(",", ":"))
    return base64.urlsafe_b64encode(payload.encode("utf-8")).decode("ascii").rstrip("=")


def decode_cursor(query: dict[str, Any], cursor: str) -> int:
    """The offset a cursor stands for, or an error naming why it does not."""
    text = (cursor or "").strip()
    if not text:
        return 0
    padded = text + "=" * (-len(text) % 4)
    try:
        raw = base64.urlsafe_b64decode(padded.encode("ascii"))
        data = json.loads(raw.decode("utf-8"))
    except (binascii.Error, UnicodeError, ValueError) as exc:
        raise InvalidCursor("This cursor did not come from Mira.") from exc
    if not isinstance(data, dict) or not isinstance(data.get("o"), int):
        raise InvalidCursor("This cursor did not come from Mira.")
    if data.get("q") != _fingerprint(query):
        raise InvalidCursor(
            "This cursor belongs to a different query. A cursor is only valid "
            "for the filters it came back with; start again without one."
        )
    return max(0, int(data["o"]))


@dataclass(frozen=True)
class Page:
    """One page of rows, and where the next one starts."""

    rows: list[Any]
    next_cursor: str = ""

    @property
    def has_more(self) -> bool:
        return bool(self.next_cursor)


def take_page(rows: list[Any], *, query: dict[str, Any], offset: int, size: int) -> Page:
    """Split an over-read into the page and the answer to "is there more?".

    Callers read ``size + 1`` rows. The extra row is never returned; its only
    job is to distinguish "the page is full" from "the page is full and that is
    the end", which a count query would answer at the cost of a second scan
    nobody asked for.
    """
    page = rows[:size]
    more = len(rows) > size
    return Page(rows=page, next_cursor=encode_cursor(query, offset + len(page)) if more else "")


def truncate(text: str, *, limit: int) -> str:
    """Cut a free-text field to ``limit`` characters, visibly.

    Silent truncation is the failure mode worth avoiding: a finding body that
    stops mid-sentence reads as a finding that *said* that, and the model on
    the other end has no way to tell a short body from a cut one.

    Always applied to stored text, never to already-truncated text: a response
    that does not fit is re-rendered from the rows, with a smaller allowance,
    rather than cut again.
    """
    if not text or len(text) <= limit:
        return text or ""
    return text[:limit] + TRUNCATION_MARK
