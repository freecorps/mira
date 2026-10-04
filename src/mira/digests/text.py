"""Making untrusted and generated text safe to publish.

A digest is posted to chat channels and email and a release note is pasted into
a release page, so everything that reaches one — a contributor's title, or
prose a model wrote after reading contributors' titles — goes through here:
one line where one line is expected, no raw HTML, no links the reader did not
get from the platform (raw HTML is escaped by each renderer, see
:func:`escape_html`), and no mentions (``@channel`` in a pull request title
must not page a whole Slack workspace).
"""

from __future__ import annotations

import re

_ZWSP = "​"
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def neutralize_mentions(text: str) -> str:
    """``@name`` → ``@​name``: still readable, no longer a mention."""
    return re.sub(r"@(?=[\w-])", "@" + _ZWSP, text)


def _base(text: str) -> str:
    return neutralize_mentions(_CONTROL.sub("", text or ""))


def escape_html(text: str) -> str:
    """For a destination that renders HTML (Markdown) or Slack's mrkdwn.

    Applied when rendering, not when storing: the dashboard renders text nodes
    and would show ``&lt;`` literally.
    """
    return (text or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def one_line(text: str, limit: int = 200) -> str:
    """A title: first line, whitespace collapsed, bounded."""
    first = next((ln for ln in (text or "").splitlines() if ln.strip()), "")
    line = " ".join(_base(first).split())
    if len(line) > limit:
        line = line[: limit - 1].rstrip() + "…"
    return line


def prose(text: str, limit: int = 600) -> str:
    """Model-written prose: no links, no HTML, no mentions, bounded."""
    text = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", text or "")
    text = re.sub(r"https?://\S+", "", text)
    text = " ".join(_base(text).split())
    if len(text) > limit:
        text = text[: limit - 1].rstrip() + "…"
    return text


def excerpt(text: str, limit: int) -> str:
    """Untrusted text quoted to a model: trimmed, never framed here."""
    text = (text or "").strip()
    return text if len(text) <= limit else text[:limit] + " […]"
