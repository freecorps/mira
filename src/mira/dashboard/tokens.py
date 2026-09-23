"""API tokens: a read-only dashboard login that a program can carry.

The dashboard authenticates people with a session cookie, which is the right
thing for a browser and the wrong thing for everything else. An agent, a
script or `curl` has no login form to fill in and no cookie jar to keep, and
the alternatives — pasting a session cookie out of the browser's devtools, or
handing a program the admin password — are worse than having nothing.

A token is the narrower thing. It belongs to one user and reaches what that
user can read in the dashboard, with two cuts that hold whatever the user can
do:

**It only reads.** A request carrying a token is refused unless it is a `GET`
or a `HEAD`, before any route runs. The one exception is the MCP endpoint,
which is a `POST` by protocol and read-only by inventory: every tool it offers
is a query.

**It cannot manage credentials.** Not its own, not anybody else's: the
`/api/auth` routes other than `me` refuse a token outright. A leaked token is a
leak of what it can read; it must not also be a way to mint the next one.

What is stored is a SHA-256 digest of the token, never the token. A digest
rather than the PBKDF2 the passwords get, because a token is 256 bits of
randomness and not something a person chose: there is no dictionary to slow
down, and a slow hash on every API request would be a cost with nothing on the
other side of it. The digest is looked up directly, so validation is one
indexed read.
"""

from __future__ import annotations

import hashlib
import secrets

#: Every token starts with this. Recognisable on purpose: it is what lets the
#: redaction filter find one in a log line or a pasted config file, and what
#: lets a secret scanner flag one committed by accident.
TOKEN_PREFIX = "mira_pat_"

#: Characters of the token kept for display, prefix included. Enough to tell
#: two tokens apart in a list, far too few to be worth anything to anybody.
DISPLAY_CHARS = len(TOKEN_PREFIX) + 6

#: The longest name a token may be given. It is a label for a list, not a note.
MAX_NAME_CHARS = 80


def generate() -> str:
    """A new token. Shown once, to whoever asked for it, and never again."""
    return TOKEN_PREFIX + secrets.token_urlsafe(32)


def digest(token: str) -> str:
    """What is stored and looked up in place of the token."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def display_prefix(token: str) -> str:
    return token[:DISPLAY_CHARS]


def looks_like_token(value: str) -> bool:
    """Whether a string is shaped like a Mira token.

    A shape check, not validation. It exists so a bearer credential meant for
    some other service is refused without a database read.
    """
    return value.startswith(TOKEN_PREFIX) and len(value) > len(TOKEN_PREFIX) + 16
