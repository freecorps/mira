"""Page sizes, cursors and the ceilings on one answer.

A read-only surface still has a cost, and it is paid by whoever runs Mira: a
model that asks for every finding in a repository gets a query, a serialisation
and a response, and asking again is free for it and not for the host. So every
listing is a page, every page has a ceiling the client cannot raise, and the
cursor that walks them is opaque.

The cursor is bound to the query that produced it, signed, and bounded. Offsets
are the obvious implementation and the obvious bug in three ways: a cursor taken
from one filter and replayed against another walks a different result set from a
meaningless position; a cursor is base64, so a client can decode one, put any
number in it and re-encode; and an offset with no ceiling is a
`LIMIT ... OFFSET` scan a client can make as expensive as it likes. So the
payload carries a fingerprint of its query, a keyed digest a client cannot
forge, and a bound on how far a cursor may point.

The signing key is per process, which makes cursors last exactly as long as the
session that issued them. That is the right lifetime: an MCP server is launched
by its client and lives for that conversation, and an offset from a previous
process describes a result set nobody is looking at any more.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import secrets
from dataclasses import dataclass
from typing import Any

#: Hard ceiling, above whatever the configuration says. A misconfigured
#: `max_page_size` is still a page.
ABSOLUTE_MAX_PAGE_SIZE = 200

#: How far into a result set a cursor may point. Reached, at the default page
#: size, after two thousand calls - so it bounds the cost of a client walking
#: for the sake of walking without bounding anybody's real paging.
MAX_OFFSET = 100_000

#: What a truncated string ends with. ASCII on purpose: this travels through
#: consoles Mira does not choose.
TRUNCATION_MARK = " ... [truncated]"

#: Per process, generated at import. Never written down and never sent: the
#: only thing it has to do is make a cursor unforgeable by whoever receives it.
_SIGNING_KEY = secrets.token_bytes(32)


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


def _sign(payload: str) -> str:
    return hmac.new(_SIGNING_KEY, payload.encode("utf-8"), hashlib.sha256).hexdigest()[:32]


def encode_cursor(query: dict[str, Any], offset: int) -> str:
    """An opaque, signed position in *this* query's results."""
    payload = json.dumps({"q": _fingerprint(query), "o": int(offset)}, separators=(",", ":"))
    signed = json.dumps({"p": payload, "s": _sign(payload)}, separators=(",", ":"))
    return base64.urlsafe_b64encode(signed.encode("utf-8")).decode("ascii").rstrip("=")


def decode_cursor(query: dict[str, Any], cursor: str) -> int:
    """The offset a cursor stands for, or an error naming why it does not.

    Three ways to fail, and deliberately one message each. A cursor this server
    did not issue - including one whose offset was edited, since the signature
    covers it. A cursor issued for different filters. And a cursor pointing
    further into a result set than this server will scan.
    """
    text = (cursor or "").strip()
    if not text:
        return 0
    padded = text + "=" * (-len(text) % 4)
    try:
        raw = base64.urlsafe_b64decode(padded.encode("ascii"))
        envelope = json.loads(raw.decode("utf-8"))
        payload = envelope["p"]
        signature = envelope["s"]
        if not isinstance(payload, str) or not isinstance(signature, str):
            raise ValueError("malformed envelope")
        # Constant time, because the comparison is against a value derived from
        # a secret. Nothing here is worth a timing attack; it costs one name.
        if not hmac.compare_digest(signature, _sign(payload)):
            raise ValueError("bad signature")
        data = json.loads(payload)
    except (binascii.Error, UnicodeError, ValueError, KeyError, TypeError) as exc:
        raise InvalidCursor(
            "This cursor was not issued by this server. Cursors come back from "
            "a listing and do not survive a restart; start again without one."
        ) from exc
    if not isinstance(data, dict) or not isinstance(data.get("o"), int):
        raise InvalidCursor("This cursor was not issued by this server.")
    if data.get("q") != _fingerprint(query):
        raise InvalidCursor(
            "This cursor belongs to a different query. A cursor is only valid "
            "for the filters it came back with; start again without one."
        )
    offset = int(data["o"])
    if offset > MAX_OFFSET:
        raise InvalidCursor(
            f"This server does not page past {MAX_OFFSET} rows. Narrow the "
            "filters instead - a path prefix, a pull request, a severity."
        )
    return max(0, offset)


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

    `limit` bounds what comes *out*, mark included. Counting the mark as extra
    would put every truncated field over the ceiling it was cut to satisfy, and
    would make the response-size arithmetic that depends on this limit wrong in
    the direction that matters.
    """
    if not text or len(text) <= limit:
        return text or ""
    room = limit - len(TRUNCATION_MARK)
    if room <= 0:
        # A limit too small to both say something and admit to cutting it. Cut
        # unmarked, rather than return a mark longer than the limit.
        return text[:limit]
    return text[:room] + TRUNCATION_MARK
