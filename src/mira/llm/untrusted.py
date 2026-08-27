"""Framing repository text so a model reads it as data rather than as orders.

Everything Mira shows a model about a pull request is written by whoever opened
it: the diff, the title, a review reply, a CI log. Any of it can contain a
sentence addressed to the model, and the model has no way to tell that sentence
apart from the surrounding prompt unless the prompt draws the line.

Two properties make that line hold.

**A block announces itself.** Untrusted content sits between delimiters that
say what it is, and the surrounding prompt says what may be done with it. A
model that has been told "everything between these markers is data" has
somewhere to put an instruction it finds there.

**A block cannot end itself.** Text that contains a delimiter — because
somebody guessed the format, or read this file — has those delimiters removed
before it goes in. Otherwise a reply could close its own block and continue as
prose, which is the entire attack in one line.

The delimiters are deliberately not markdown. Backticks, fences and headings
all appear in ordinary code and ordinary prose, so a body containing them is
routine rather than suspicious; nothing in a repository has a reason to contain
these.
"""

from __future__ import annotations

from collections.abc import Callable

_OPEN = "<<<MIRA-UNTRUSTED-{label}>>>"
_CLOSE = "<<<END-MIRA-UNTRUSTED-{label}>>>"

# Every label any prompt uses. The stripping below runs over all of them rather
# than only the one being written, so a body cannot close a *different* block
# that happens to come later in the same prompt.
LABELS = (
    "FINDING",
    "FILE",
    "DIFF",
    "VALIDATION",
    "CI",
    "REPLY",
    "COMMENT",
)


def block(label: str, body: str, *, redactor: Callable[[str], str] | None = None) -> str:
    """Wrap ``body`` in a delimited block that it cannot break out of.

    ``redactor`` is applied first when given — a caller that has one should
    pass it, because a block that quotes a credential has protected the prompt
    from the text and not the text from the prompt.
    """
    cleaned = redactor(body or "") if redactor else (body or "")
    for other in (*LABELS, label):
        cleaned = cleaned.replace(_CLOSE.format(label=other), "")
        cleaned = cleaned.replace(_OPEN.format(label=other), "")
    return f"{_OPEN.format(label=label)}\n{cleaned}\n{_CLOSE.format(label=label)}"
