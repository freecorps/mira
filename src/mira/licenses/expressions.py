"""SPDX license identifiers and expressions: normalizing what registries write.

Registries and lockfiles describe licenses however their authors typed them:
``MIT``, ``MIT License``, ``Apache 2.0``, ``(MIT OR Apache-2.0)``,
``MIT/Apache-2.0``, ``License :: OSI Approved :: BSD License``. A policy and an
SBOM both need one spelling, so :func:`normalize` turns each into an SPDX
license expression — or into ``""`` when the text names no license Mira can
recognise, which is "unknown" and never a guess.

The expression grammar is the useful subset of SPDX's: identifiers (with an
optional ``+``), ``WITH`` exceptions, ``AND``, ``OR`` and parentheses, with
``WITH`` binding tighter than ``AND`` and ``AND`` tighter than ``OR``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# Common SPDX license identifiers (https://spdx.org/licenses/). Not the whole
# list: the ones package registries actually carry. An identifier outside it
# still parses — it just is not a recognised SPDX id, and an SPDX document
# writes it as a LicenseRef.
SPDX_IDS: frozenset[str] = frozenset(
    {
        "0BSD",
        "AFL-2.1",
        "AFL-3.0",
        "AGPL-1.0-only",
        "AGPL-1.0-or-later",
        "AGPL-3.0-only",
        "AGPL-3.0-or-later",
        "Apache-1.0",
        "Apache-1.1",
        "Apache-2.0",
        "APSL-2.0",
        "Artistic-1.0",
        "Artistic-2.0",
        "BlueOak-1.0.0",
        "BSD-1-Clause",
        "BSD-2-Clause",
        "BSD-2-Clause-Patent",
        "BSD-3-Clause",
        "BSD-3-Clause-Clear",
        "BSD-4-Clause",
        "BSL-1.0",
        "BUSL-1.1",
        "CAL-1.0",
        "CC-BY-3.0",
        "CC-BY-4.0",
        "CC-BY-NC-4.0",
        "CC-BY-NC-SA-4.0",
        "CC-BY-SA-3.0",
        "CC-BY-SA-4.0",
        "CC0-1.0",
        "CDDL-1.0",
        "CDDL-1.1",
        "CECILL-2.1",
        "CPAL-1.0",
        "CPL-1.0",
        "ECL-2.0",
        "EFL-2.0",
        "Elastic-2.0",
        "EPL-1.0",
        "EPL-2.0",
        "EUPL-1.1",
        "EUPL-1.2",
        "GFDL-1.3-only",
        "GFDL-1.3-or-later",
        "GPL-1.0-only",
        "GPL-1.0-or-later",
        "GPL-2.0-only",
        "GPL-2.0-or-later",
        "GPL-3.0-only",
        "GPL-3.0-or-later",
        "HPND",
        "ICU",
        "IJG",
        "ISC",
        "LGPL-2.0-only",
        "LGPL-2.0-or-later",
        "LGPL-2.1-only",
        "LGPL-2.1-or-later",
        "LGPL-3.0-only",
        "LGPL-3.0-or-later",
        "Libpng",
        "MIT",
        "MIT-0",
        "MIT-CMU",
        "MPL-1.1",
        "MPL-2.0",
        "MPL-2.0-no-copyleft-exception",
        "MS-PL",
        "MS-RL",
        "NCSA",
        "ODbL-1.0",
        "OFL-1.1",
        "OpenSSL",
        "OSL-3.0",
        "PHP-3.0",
        "PHP-3.01",
        "PostgreSQL",
        "PSF-2.0",
        "Python-2.0",
        "Ruby",
        "SSPL-1.0",
        "Unicode-3.0",
        "Unicode-DFS-2016",
        "Unlicense",
        "UPL-1.0",
        "Vim",
        "W3C",
        "WTFPL",
        "X11",
        "Zlib",
        "ZPL-2.1",
    }
)

SPDX_EXCEPTIONS: frozenset[str] = frozenset(
    {
        "Autoconf-exception-3.0",
        "Bison-exception-2.2",
        "Classpath-exception-2.0",
        "GCC-exception-3.1",
        "LLVM-exception",
        "OpenJDK-assembly-exception-1.0",
    }
)

_CANONICAL = {i.lower(): i for i in SPDX_IDS | SPDX_EXCEPTIONS}

# SPDX ids deprecated in favour of an explicit -only / -or-later.
_DEPRECATED_GNU = {
    "gpl-1.0": "GPL-1.0",
    "gpl-2.0": "GPL-2.0",
    "gpl-3.0": "GPL-3.0",
    "lgpl-2.0": "LGPL-2.0",
    "lgpl-2.1": "LGPL-2.1",
    "lgpl-3.0": "LGPL-3.0",
    "agpl-1.0": "AGPL-1.0",
    "agpl-3.0": "AGPL-3.0",
    "gfdl-1.3": "GFDL-1.3",
}

# Free-text names (lowercased, whitespace collapsed) → SPDX expression. Covers
# the PyPI trove classifiers and the spellings registries most often carry.
_ALIASES: dict[str, str] = {
    "mit license": "MIT",
    "the mit license": "MIT",
    "the mit license (mit)": "MIT",
    "mit licence": "MIT",
    "expat": "MIT",
    "expat license": "MIT",
    "mit/x11": "MIT",
    "apache": "Apache-2.0",
    "apache 2": "Apache-2.0",
    "apache 2.0": "Apache-2.0",
    "apache2": "Apache-2.0",
    "apache-2": "Apache-2.0",
    "apache license": "Apache-2.0",
    "apache license 2.0": "Apache-2.0",
    "apache license, version 2.0": "Apache-2.0",
    "apache license version 2.0": "Apache-2.0",
    "apache software license": "Apache-2.0",
    "apache software license 2.0": "Apache-2.0",
    "apache software license (apache-2.0)": "Apache-2.0",
    "apache software license (apache 2.0)": "Apache-2.0",
    "asl 2.0": "Apache-2.0",
    "asl2": "Apache-2.0",
    # "BSD" alone names a family; the 3-clause text is what nearly every
    # package classified that way actually ships. Documented in sbom-licenses.md.
    "bsd": "BSD-3-Clause",
    "bsd license": "BSD-3-Clause",
    "new bsd": "BSD-3-Clause",
    "new bsd license": "BSD-3-Clause",
    "modified bsd license": "BSD-3-Clause",
    "bsd-3": "BSD-3-Clause",
    "bsd3": "BSD-3-Clause",
    "3-clause bsd": "BSD-3-Clause",
    "bsd 3-clause": "BSD-3-Clause",
    "bsd 3-clause license": "BSD-3-Clause",
    "simplified bsd": "BSD-2-Clause",
    "bsd-2": "BSD-2-Clause",
    "bsd 2-clause": "BSD-2-Clause",
    "2-clause bsd": "BSD-2-Clause",
    "bsd 2-clause license": "BSD-2-Clause",
    "isc license": "ISC",
    "isc license (iscl)": "ISC",
    "iscl": "ISC",
    "mozilla public license 2.0": "MPL-2.0",
    "mozilla public license 2.0 (mpl 2.0)": "MPL-2.0",
    "mpl 2.0": "MPL-2.0",
    "mpl2": "MPL-2.0",
    "mozilla public license 1.1 (mpl 1.1)": "MPL-1.1",
    "gplv2": "GPL-2.0-only",
    "gpl v2": "GPL-2.0-only",
    "gpl-2": "GPL-2.0-only",
    "gplv2+": "GPL-2.0-or-later",
    "gplv3": "GPL-3.0-only",
    "gpl v3": "GPL-3.0-only",
    "gpl-3": "GPL-3.0-only",
    "gplv3+": "GPL-3.0-or-later",
    "gnu gpl v3": "GPL-3.0-only",
    "gnu general public license v2 (gplv2)": "GPL-2.0-only",
    "gnu general public license v2 or later (gplv2+)": "GPL-2.0-or-later",
    "gnu general public license v3 (gplv3)": "GPL-3.0-only",
    "gnu general public license v3 or later (gplv3+)": "GPL-3.0-or-later",
    "gnu general public license (gpl)": "GPL-2.0-or-later",
    "lgplv2": "LGPL-2.0-only",
    "lgplv2+": "LGPL-2.0-or-later",
    "lgplv3": "LGPL-3.0-only",
    "lgplv3+": "LGPL-3.0-or-later",
    "gnu lesser general public license v2 (lgplv2)": "LGPL-2.0-only",
    "gnu lesser general public license v2 or later (lgplv2+)": "LGPL-2.0-or-later",
    "gnu lesser general public license v3 (lgplv3)": "LGPL-3.0-only",
    "gnu lesser general public license v3 or later (lgplv3+)": "LGPL-3.0-or-later",
    "gnu library or lesser general public license (lgpl)": "LGPL-2.0-or-later",
    "agplv3": "AGPL-3.0-only",
    "agplv3+": "AGPL-3.0-or-later",
    "gnu affero general public license v3": "AGPL-3.0-only",
    "gnu affero general public license v3 or later (agplv3+)": "AGPL-3.0-or-later",
    "python software foundation license": "PSF-2.0",
    "psf": "PSF-2.0",
    "psfl": "PSF-2.0",
    "the unlicense": "Unlicense",
    "the unlicense (unlicense)": "Unlicense",
    "zlib/libpng license": "Zlib",
    "zlib license": "Zlib",
    "eclipse public license 1.0": "EPL-1.0",
    "eclipse public license 2.0": "EPL-2.0",
    "eclipse public license 2.0 (epl-2.0)": "EPL-2.0",
    "boost software license 1.0 (bsl-1.0)": "BSL-1.0",
    "boost software license 1.0": "BSL-1.0",
    "cc0 1.0 universal (cc0 1.0) public domain dedication": "CC0-1.0",
    "cc0": "CC0-1.0",
    "historical permission notice and disclaimer (hpnd)": "HPND",
    "universal permissive license (upl)": "UPL-1.0",
    "european union public licence 1.2 (eupl 1.2)": "EUPL-1.2",
    "server side public license": "SSPL-1.0",
    "wtfpl": "WTFPL",
}

# Text that says, in so many words, that there is no answer.
_NO_ANSWER = {"", "unknown", "noassertion", "none", "n/a", "na", "other", "license", "see license"}

_CLASSIFIER = re.compile(r"^license\s*::\s*(?:osi approved\s*::\s*)?(.+)$", re.IGNORECASE)
_TOKEN = re.compile(r"\s*(\(|\)|[^\s()]+)")
_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.\-+:]*$")


class LicenseParseError(ValueError):
    """Text that is not a license expression."""


@dataclass(frozen=True)
class Node:
    """One node of a parsed expression.

    ``op`` is ``"id"`` (``value`` is the identifier, ``exception`` the
    ``WITH`` exception if any), ``"and"`` or ``"or"`` (``children``).
    """

    op: str
    value: str = ""
    exception: str = ""
    children: tuple[Node, ...] = ()

    def ids(self) -> list[str]:
        if self.op == "id":
            return [self.value]
        return [i for c in self.children for i in c.ids()]

    def render(self) -> str:
        if self.op == "id":
            return f"{self.value} WITH {self.exception}" if self.exception else self.value
        sep = " AND " if self.op == "and" else " OR "
        parts = []
        for c in self.children:
            text = c.render()
            # OR inside AND needs its parentheses back.
            if c.op == "or" and self.op == "and":
                text = f"({text})"
            parts.append(text)
        return sep.join(parts)


def canonical_id(token: str, *, map_deprecated: bool = True) -> str:
    """The SPDX spelling of one identifier; unknown ones come back as written.

    ``GPL-3.0`` (deprecated) becomes ``GPL-3.0-only`` and ``GPL-3.0+``
    ``GPL-3.0-or-later`` when ``map_deprecated``; a policy entry keeps the bare
    form, which then matches the whole family (see :mod:`mira.licenses.policy`).
    """
    raw = token.strip()
    lowered = raw.lower()
    if lowered in _CANONICAL:
        return _CANONICAL[lowered]
    plus = lowered.endswith("+")
    base = lowered[:-1] if plus else lowered
    if base in _DEPRECATED_GNU:
        family = _DEPRECATED_GNU[base]
        if not map_deprecated:
            return family + ("+" if plus else "")
        return f"{family}-or-later" if plus else f"{family}-only"
    if lowered in _ALIASES and " " not in _ALIASES[lowered]:
        return _ALIASES[lowered]
    if plus and base in _CANONICAL:
        return _CANONICAL[base] + "+"
    return raw


def parse_expression(text: str, *, map_deprecated: bool = True) -> Node:
    """Parse an SPDX license expression. Raises :class:`LicenseParseError`."""
    if not isinstance(text, str) or not text.strip():
        raise LicenseParseError("empty expression")
    if len(text) > 300:
        raise LicenseParseError("too long to be an expression")
    tokens = [m.group(1) for m in _TOKEN.finditer(text)]
    pos = 0

    def peek() -> str:
        return tokens[pos] if pos < len(tokens) else ""

    def take() -> str:
        nonlocal pos
        if pos >= len(tokens):
            raise LicenseParseError("unexpected end of expression")
        tok = tokens[pos]
        pos += 1
        return tok

    def parse_or() -> Node:
        nodes = [parse_and()]
        while peek().upper() == "OR":
            take()
            nodes.append(parse_and())
        return nodes[0] if len(nodes) == 1 else Node("or", children=_flatten("or", nodes))

    def parse_and() -> Node:
        nodes = [parse_atom()]
        while peek().upper() == "AND":
            take()
            nodes.append(parse_atom())
        return nodes[0] if len(nodes) == 1 else Node("and", children=_flatten("and", nodes))

    def parse_atom() -> Node:
        tok = take()
        if tok == "(":
            node = parse_or()
            if take() != ")":
                raise LicenseParseError("unbalanced parentheses")
            return node
        if tok == ")" or tok.upper() in ("AND", "OR", "WITH") or not _ID.match(tok):
            raise LicenseParseError(f"unexpected {tok!r}")
        node = Node("id", value=canonical_id(tok, map_deprecated=map_deprecated))
        if peek().upper() == "WITH":
            take()
            exc = take()
            if not _ID.match(exc):
                raise LicenseParseError(f"unexpected {exc!r} after WITH")
            node = Node("id", value=node.value, exception=_CANONICAL.get(exc.lower(), exc))
        return node

    node = parse_or()
    if pos != len(tokens):
        raise LicenseParseError(f"unexpected {tokens[pos]!r}")
    return node


def _flatten(op: str, nodes: list[Node]) -> tuple[Node, ...]:
    out: list[Node] = []
    for n in nodes:
        out.extend(n.children if n.op == op else (n,))
    return tuple(out)


def _alias(text: str) -> str:
    key = " ".join(text.lower().split())
    if key in _ALIASES:
        return _ALIASES[key]
    m = _CLASSIFIER.match(key)
    if m:
        return _ALIASES.get(m.group(1).strip(), "") or (
            _CANONICAL.get(m.group(1).strip(), "") if " " not in m.group(1).strip() else ""
        )
    return ""


def normalize(raw: str | None) -> str:
    """An SPDX expression for ``raw``, or ``""`` when it names no license.

    Free text Mira does not recognise is unknown, not passed through: a whole
    license text pasted into a ``license`` field must not become an identifier.
    """
    if not isinstance(raw, str):
        return ""
    text = raw.strip()
    if text.lower().strip("() ") in _NO_ANSWER or text.upper().startswith("SEE LICENSE"):
        return ""
    if text.upper() == "UNLICENSED":
        # npm's word for "proprietary, no license granted" — not The Unlicense.
        return ""
    aliased = _alias(text)
    if aliased:
        return aliased
    if len(text) > 200 or "\n" in text:
        return ""
    # Old npm and Cargo wrote alternatives as "MIT/Apache-2.0".
    candidate = re.sub(r"\s*/\s*", " OR ", text) if "/" in text else text
    try:
        node = parse_expression(candidate)
    except LicenseParseError:
        return ""
    return node.render()


def is_spdx_expression(expression: str) -> bool:
    """Whether every identifier in ``expression`` is a recognised SPDX id or a LicenseRef."""
    try:
        node = parse_expression(expression)
    except LicenseParseError:
        return False
    return all(i in SPDX_IDS or i.startswith("LicenseRef-") for i in node.ids())


def license_ref(text: str) -> str:
    """``LicenseRef-…`` for an identifier SPDX does not list."""
    return "LicenseRef-" + (re.sub(r"[^A-Za-z0-9.\-]+", "-", text).strip("-") or "unknown")
