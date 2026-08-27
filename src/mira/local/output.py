"""Rendering a local review, for a person and for a program.

The findings themselves are rendered by :mod:`mira.report`, the same code the
server-facing CLI uses. What is added here is the local frame: which repository
and which comparison, where the code was sent, what was left out and why, and
the check run — rendered with :func:`mira.checks.explain.public_explanation`,
which is the same text a pull request would have received.

**On JSON stability.** ``schema_version`` is the contract. Keys are added
without bumping it; a key is never renamed or removed without bumping it. The
document is also *deterministic*: given the same diff and the same model
output, byte-identical JSON comes out, so a CI job can diff two runs. That is
why durations, row ids and timestamps are absent from the check run here even
though the dashboard's API carries them — a field that changes on every run
turns "did anything change?" into a question nobody can answer cheaply.
"""

from __future__ import annotations

import json
from typing import Any

from mira.checks.explain import public_explanation
from mira.local.exit_codes import EXIT_CODE_HELP, ExitCode
from mira.local.run import LocalReview
from mira.report import review_result_dict, review_result_text

#: Bumped only for an incompatible change: a renamed key, a removed key, or a
#: value whose meaning changed. New keys do not bump it.
SCHEMA_VERSION = 1

#: Fields dropped from the check run before it is emitted, because they differ
#: between two runs over identical inputs. See the module docstring.
_VOLATILE_CHECK_FIELDS = ("duration_seconds", "created_at", "updated_at", "id")


def _stable_check_run(run: Any) -> dict[str, Any]:
    data = run.as_dict()
    for name in _VOLATILE_CHECK_FIELDS:
        data.pop(name, None)
    results = []
    for result in data.get("results", []):
        for name in _VOLATILE_CHECK_FIELDS:
            result.pop(name, None)
        results.append(result)
    data["results"] = results
    return data


def to_json(review: LocalReview) -> str:
    """The whole local review as one JSON document."""
    payload: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "mode": review.diff.mode,
        "comparison": review.diff.comparison,
        "repository": {
            "root": str(review.identity.root),
            "platform": review.identity.platform,
            "owner": review.identity.owner,
            "repo": review.identity.repo,
            "branch": review.identity.branch,
            "remote": review.identity.remote_name,
            "identified": review.identity.known,
        },
        "base": {"label": review.diff.base_label, "sha": review.diff.base_sha},
        "head": {"label": review.diff.head_label, "sha": review.diff.head_sha},
        "destinations": [destination.as_dict() for destination in review.destinations],
        "review": (
            review_result_dict(review.result)
            if review.result is not None
            else {
                "summary": "",
                "walkthrough": None,
                "comments": [],
                "reviewed_files": 0,
                "token_usage": {},
            }
        ),
        "changed_files": [entry.as_dict() for entry in review.diff.entries],
        "untracked": {
            "paths": list(review.diff.untracked),
            "included": review.diff.untracked_included,
        },
        "checks": _stable_check_run(review.checks) if review.checks is not None else None,
        "counts": review.counts(),
        "fail_on": review.fail_on,
        "notes": list(review.notes),
        "exit_code": int(review.exit_code()),
    }
    # `ensure_ascii` is left on: the document has to survive a console whose
    # encoding is not UTF-8, where a backslash-u escape is both faithful and
    # safe and transliteration would not be.
    return json.dumps(payload, indent=2)


def _header(review: LocalReview) -> list[str]:
    identity = review.identity
    where = identity.slug or f"{identity.root.name} (no remote)"
    lines = [
        "Mira - local review",
        f"  repository   {where} [{identity.platform}]",
        f"  comparison   {review.diff.comparison}",
    ]
    for destination in review.destinations:
        lines.append(f"  {destination.purpose:<12} {destination.describe()}")
    reviewed = sum(1 for entry in review.diff.entries if entry.reviewed)
    lines.append(f"  files        {reviewed} reviewed of {len(review.diff.entries)} changed")
    return lines


def _skipped_lines(review: LocalReview) -> list[str]:
    skipped = [entry for entry in review.diff.entries if not entry.reviewed]
    if not skipped:
        return []
    lines = ["Not reviewed:"]
    for entry in sorted(skipped, key=lambda item: item.path):
        reason = entry.excluded_reason or "not reviewed"
        lines.append(f"  {entry.path} - {reason}")
    return lines


def to_text(review: LocalReview) -> str:
    """The whole local review, for a terminal."""
    blocks: list[str] = ["\n".join(_header(review))]

    if review.result is not None:
        blocks.append(review_result_text(review.result))
    else:
        blocks.append("Nothing was reviewed.")

    skipped = _skipped_lines(review)
    if skipped:
        blocks.append("\n".join(skipped))

    if review.checks is not None:
        blocks.append("Pre-merge checks\n\n" + public_explanation(review.checks))

    if review.notes:
        blocks.append("\n".join(["Notes:", *(f"  - {note}" for note in review.notes)]))

    code = review.exit_code()
    blocks.append(f"Exit code {int(code)}: {EXIT_CODE_HELP[code]}")
    return "\n\n".join(block for block in blocks if block.strip())


def exit_code_table() -> str:
    """The documented exit codes, printable from the command itself."""
    width = max(len(str(int(code))) for code in ExitCode)
    return "\n".join(
        f"{int(code):>{width}}  {ExitCode(code).name.lower():<12} {EXIT_CODE_HELP[code]}"
        for code in ExitCode
    )
