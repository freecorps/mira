"""Stable identifiers and hidden comment markers for review findings."""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from typing import Any

_FINDING_MARKER_RE = re.compile(r"<!--\s*mira:finding:([0-9a-fA-F-]{32,36})\s*-->", re.IGNORECASE)
_LEGACY_NAMESPACE = uuid.UUID("2d19711a-4448-4b31-a17e-94f34b1edec8")


def new_finding_id() -> str:
    """Return an opaque identifier that remains stable for the finding row."""
    return str(uuid.uuid4())


def legacy_finding_id(*parts: object) -> str:
    """Create a repeatable ID while backfilling records from the legacy schema."""
    return str(uuid.uuid5(_LEGACY_NAMESPACE, ":".join(str(part) for part in parts)))


def finding_marker(finding_id: str) -> str:
    return f"<!-- mira:finding:{finding_id} -->"


def parse_finding_id(body: str) -> str | None:
    match = _FINDING_MARKER_RE.search(body or "")
    return match.group(1).lower() if match else None


def _normalise(value: Any) -> str:
    return " ".join(str(value or "").split()).strip().lower()


def finding_fingerprint(
    *,
    owner: str,
    repo: str,
    pr_number: int,
    base_sha: str,
    head_sha: str,
    path: str,
    symbol: str,
    category: str,
    detector: str,
    problem: str,
) -> str:
    """Hash the immutable provenance used to identify equivalent findings."""
    payload = {
        "repo": f"{_normalise(owner)}/{_normalise(repo)}",
        "pr": pr_number,
        "base_sha": _normalise(base_sha),
        "head_sha": _normalise(head_sha),
        "path": _normalise(path),
        "symbol": _normalise(symbol),
        "category": _normalise(category),
        "detector": _normalise(detector),
        "problem": _normalise(problem),
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()
