"""Small SQL text helpers shared by both backends.

Neutral ground. The escaping below is needed by the index stores and by the
persistence mixins that sit on top of them, and both SQLite and PostgreSQL
spell it the same way, so it lives where either can import it without reaching
into the other's module.
"""

from __future__ import annotations


def like_prefix(prefix: str) -> str:
    """Turn a literal path prefix into a LIKE pattern that means only itself.

    `%` and `_` are wildcards and a path may contain both - `src/_internal/` is
    an ordinary directory name, and without escaping it would also match
    `src/xinternal/`. The backslash is escaped first, or escaping the wildcards
    would go on to escape the escape.

    Pair it with ``ESCAPE '\'`` in the statement: the default escape character
    is unspecified in SQLite and is the backslash only by convention elsewhere.
    """
    escaped = prefix.replace("\\", "\\\\").replace("%", r"\%").replace("_", r"\_")
    return f"{escaped}%"
