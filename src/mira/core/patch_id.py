"""A fingerprint of what a diff changes, independent of where it applies.

Git's ``patch-id`` answers "is this the same patch?" for a commit that was
rebased: line numbers, hunk offsets and blob ids are left out, and what is left
is the set of lines each file adds and removes. The review queue
asks the same question of a whole pull request — is its diff against the base
the one Mira already reviewed? — so a restack that only moved the base does not
pay for the same review again.

Unlike git, whitespace inside a line is kept: in Python an indent is meaning,
and inside a string literal so is a space. Only trailing whitespace, which no
rebase changes and no language reads, is dropped.

Context lines are left out. A rebase that brings in upstream edits *near*
a hunk changes its context without changing what the pull request does, and
counting those would send most restacked pull requests back to the model.
"""

from __future__ import annotations

import hashlib


def diff_patch_id(diff_text: str) -> str:
    """The patch id of a unified diff, or ``""`` for an empty one.

    Files are hashed in path order, so a diff that lists the same files in a
    different order has the same id.
    """
    files: dict[str, list[str]] = {}
    current: list[str] | None = None
    for line in (diff_text or "").splitlines():
        if line.startswith("diff --git "):
            current = files.setdefault(line[len("diff --git ") :].strip(), [])
            continue
        if current is None:
            continue
        if line.startswith(("--- ", "+++ ")):
            current.append(line[:4] + line[4:].strip())
            continue
        if line.startswith(("rename from ", "rename to ", "new file mode", "deleted file mode")):
            current.append(line.strip())
            continue
        if line.startswith(("+", "-")):
            current.append(line.rstrip())
    if not any(files.values()):
        return ""
    digest = hashlib.sha256()
    for path in sorted(files):
        digest.update(path.encode("utf-8", "replace") + b"\0")
        for entry in files[path]:
            digest.update(entry.encode("utf-8", "replace") + b"\n")
        digest.update(b"\0")
    return digest.hexdigest()
