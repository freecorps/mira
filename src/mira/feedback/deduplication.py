"""Deterministic semantic deduplication for learning candidates.

The first release intentionally avoids a vector database. Normalized token
similarity is predictable, cheap on an Orange Pi, and can later be replaced by
embeddings without changing candidate identity or lifecycle APIs.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from collections.abc import Iterable

from mira.feedback.models import LearningCandidate

_WORD_RE = re.compile(r"[a-z0-9_]+")
_STOP_WORDS = {
    "a",
    "an",
    "and",
    "as",
    "at",
    "be",
    "do",
    "for",
    "in",
    "is",
    "it",
    "of",
    "on",
    "or",
    "the",
    "this",
    "to",
    "when",
}


def normalized_rule(rule_text: str) -> str:
    text = unicodedata.normalize("NFKD", rule_text).encode("ascii", "ignore").decode()
    return " ".join(token for token in _WORD_RE.findall(text.lower()) if token not in _STOP_WORDS)


def semantic_fingerprint(rule_text: str, category: str) -> str:
    canonical = f"{category.strip().lower()}\0{normalized_rule(rule_text)}"
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def rule_similarity(left: str, right: str) -> float:
    left_tokens = set(normalized_rule(left).split())
    right_tokens = set(normalized_rule(right).split())
    if not left_tokens or not right_tokens:
        return 0.0
    return len(left_tokens & right_tokens) / len(left_tokens | right_tokens)


def find_equivalent_candidate(
    candidates: Iterable[LearningCandidate],
    *,
    rule_text: str,
    category: str,
    threshold: float = 0.72,
) -> LearningCandidate | None:
    """Return the closest non-rejected candidate in the same category."""
    best: LearningCandidate | None = None
    best_score = threshold
    for candidate in candidates:
        if (
            candidate.status in {"rejected", "superseded"}
            or candidate.category.casefold() != category.casefold()
        ):
            continue
        score = rule_similarity(candidate.rule_text, rule_text)
        if score >= best_score:
            best = candidate
            best_score = score
    return best
