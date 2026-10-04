"""Evaluating a package's license expression against ``licenses.allow`` / ``deny``.

The rules, in order:

* An expression the policy names verbatim (``allow: ["MIT OR GPL-3.0-only"]``)
  is decided by that entry, deny first.
* Otherwise the expression is evaluated: an identifier is allowed when no
  ``deny`` entry matches it and, if ``allow`` is not empty, an ``allow`` entry
  does. ``A OR B`` is allowed when either side is — the consumer may choose;
  ``A AND B`` only when both are.
* A bare GNU family in the policy (``GPL-3.0``) matches its ``-only`` and
  ``-or-later`` forms, so ``deny: [GPL-3.0]`` means what it reads as.
* No license at all is ``unknown``: a violation only when ``fail_on_unknown``.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from mira.licenses.expressions import LicenseParseError, Node, parse_expression

ALLOWED = "allowed"
DENIED = "denied"
UNKNOWN = "unknown"


@dataclass
class Verdict:
    status: str  # ALLOWED | DENIED | UNKNOWN
    expression: str = ""
    # Identifiers that made it fail, and why, e.g. "GPL-3.0-only is denied".
    reasons: list[str] = field(default_factory=list)

    def violates(self, fail_on_unknown: bool) -> bool:
        return self.status == DENIED or (self.status == UNKNOWN and fail_on_unknown)


@dataclass(frozen=True)
class Policy:
    allow: tuple[str, ...] = ()
    deny: tuple[str, ...] = ()
    fail_on_unknown: bool = False

    @classmethod
    def from_config(cls, config: object) -> Policy:
        return cls(
            allow=tuple(getattr(config, "allow", ()) or ()),
            deny=tuple(getattr(config, "deny", ()) or ()),
            fail_on_unknown=bool(getattr(config, "fail_on_unknown", False)),
        )

    @property
    def active(self) -> bool:
        return bool(self.allow or self.deny or self.fail_on_unknown)


def _entries(entries: tuple[str, ...]) -> tuple[list[Node], set[str]]:
    """(single-identifier entries, verbatim compound expressions)."""
    singles: list[Node] = []
    compound: set[str] = set()
    for entry in entries:
        try:
            node = parse_expression(entry, map_deprecated=False)
        except LicenseParseError:
            continue
        if node.op == "id":
            singles.append(node)
        else:
            compound.add(parse_expression(entry).render().lower())
    return singles, compound


def _matches(entry: Node, leaf: Node) -> bool:
    e, lic = entry.value.lower(), leaf.value.lower()
    if entry.exception and entry.exception.lower() != leaf.exception.lower():
        return False
    if e == lic:
        return True
    if e.endswith("+"):
        return lic == e[:-1] + "-or-later" or lic == e
    # A bare family ("gpl-3.0") covers "gpl-3.0-only" and "gpl-3.0-or-later".
    return lic in (f"{e}-only", f"{e}-or-later")


def evaluate(expression: str, policy: Policy) -> Verdict:
    """Decide one normalized expression (``""`` is unknown)."""
    if not expression:
        return Verdict(UNKNOWN, "", ["no license could be determined"])
    try:
        tree = parse_expression(expression)
    except LicenseParseError:
        return Verdict(UNKNOWN, expression, [f"{expression!r} is not a license expression"])

    allow_ids, allow_compound = _entries(policy.allow)
    deny_ids, deny_compound = _entries(policy.deny)
    rendered = tree.render().lower()
    if rendered in deny_compound:
        return Verdict(DENIED, expression, [f"{expression} is denied"])
    if rendered in allow_compound:
        return Verdict(ALLOWED, expression)

    reasons: list[str] = []

    def ok(node: Node) -> bool:
        if node.op == "id":
            label = node.render()
            if any(_matches(d, node) for d in deny_ids):
                reasons.append(f"{label} is denied")
                return False
            if allow_ids or allow_compound:
                if any(_matches(a, node) for a in allow_ids):
                    return True
                reasons.append(f"{label} is not in the allow list")
                return False
            return True
        results = [ok(c) for c in node.children]
        return any(results) if node.op == "or" else all(results)

    if ok(tree):
        return Verdict(ALLOWED, expression)
    return Verdict(DENIED, expression, list(dict.fromkeys(reasons)))
