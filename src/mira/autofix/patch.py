"""Turning a model's answer into a change, or refusing to.

This module is where a proposal stops being text and becomes something that
could be committed, so it is where every structural guard lives. It talks to no
provider, no store and no model: it takes file contents, takes edits, and
returns either an applied patch or a reason it refused.

Three properties are load-bearing.

**A path is checked before it is used, and checked against the repository, not
against the filesystem.** ``../../etc/passwd``, ``/etc/passwd``, ``C:\\x``, a
path with a NUL, a path with a ``.git`` component and a path the policy
protects are all rejected by the same function, and that function is the only
way a path enters the pipeline.

**An edit either matches byte for byte or it does not apply.** There is no
fuzzy matching, no whitespace normalisation and no line-number fallback. A
model that quoted the file inaccurately gets a refusal, not a guess.

**Limits are enforced on the applied result.** Counting the edits the model
sent would let it hand back ten small edits that expand into a rewritten file;
counting the diff Mira produced cannot.
"""

from __future__ import annotations

import difflib
import posixpath
import re
from dataclasses import dataclass

from mira.autofix.models import FileEdit, FixPatch, Reason, ReasonCode
from mira.autofix.policy import EffectivePolicy
from mira.gate import paths as gate_paths

# Path components that are never a file a fix may edit. `.git` is the obvious
# one — writing into it is writing history rather than code — and the Windows
# device names are here because a repository can legitimately contain a path
# that is harmless on Linux and a device handle on a self-hosted runner.
_FORBIDDEN_COMPONENTS = frozenset({".git", ".hg", ".svn"})
_WINDOWS_DEVICES = re.compile(
    r"(?i)^(?:con|prn|aux|nul|com[1-9]|lpt[1-9])(?:\.|$)",
)
_DRIVE_LETTER = re.compile(r"(?i)^[a-z]:")


class PatchRefused(Exception):
    """A patch that must not be applied, with the reason it must not be.

    Carries a :class:`Reason` rather than a message so a caller can persist a
    code and render a sentence without re-deriving either.
    """

    def __init__(self, reason: Reason) -> None:
        super().__init__(reason.message)
        self.reason = reason


def _refuse(code: str, message: str) -> PatchRefused:
    return PatchRefused(Reason(code, message))


@dataclass(frozen=True)
class PathVerdict:
    """The result of asking whether one path may be edited."""

    path: str
    allowed: bool
    reason: Reason | None = None


def safe_repo_path(candidate: str) -> str:
    """Normalise a path or raise. The only door into the pipeline.

    Returns a repository-relative POSIX path. Raises :class:`PatchRefused` for
    anything absolute, anything that escapes the root, anything with a NUL, and
    anything naming a directory the fix has no business inside.
    """
    raw = (candidate or "").strip()
    if not raw:
        raise _refuse(ReasonCode.PATCH_INVALID, "An edit named no file")
    if "\x00" in raw:
        raise _refuse(ReasonCode.PATH_TRAVERSAL, "A file path contained a NUL byte")
    cleaned = raw.replace("\\", "/")
    if cleaned.startswith("/") or _DRIVE_LETTER.match(cleaned) or cleaned.startswith("//"):
        raise _refuse(
            ReasonCode.PATH_OUTSIDE_REPO,
            f"{raw!r} is an absolute path; fixes only touch files inside the repository",
        )
    # `normpath` collapses `a/../b` to `b`. A leading `..` survives it, which is
    # exactly the case we reject — and we reject on the *original* text too, so
    # a path that merely round-trips through a parent directory is refused
    # rather than quietly rewritten into something that looks innocent.
    normalised = posixpath.normpath(cleaned).lstrip("./")
    parts = [part for part in cleaned.split("/") if part not in ("", ".")]
    if ".." in parts or normalised.startswith("../") or normalised in ("..", "."):
        raise _refuse(
            ReasonCode.PATH_TRAVERSAL,
            f"{raw!r} walks outside the repository",
        )
    for part in parts:
        if part.lower() in _FORBIDDEN_COMPONENTS:
            raise _refuse(
                ReasonCode.PATH_OUTSIDE_REPO,
                f"{raw!r} points inside {part}, which is not source code",
            )
        if _WINDOWS_DEVICES.match(part):
            raise _refuse(
                ReasonCode.PATH_TRAVERSAL,
                f"{raw!r} names a reserved device path",
            )
    resolved = gate_paths.normalize("/".join(parts))
    if not resolved:
        raise _refuse(ReasonCode.PATCH_INVALID, f"{raw!r} does not name a file")
    return resolved


def check_path(
    path: str,
    *,
    policy: EffectivePolicy,
    changed_paths: set[str],
    known: bool,
) -> str:
    """Normalise ``path`` and confirm the policy permits editing it.

    ``known`` says whether the file exists at the head commit. It is passed in
    rather than inferred from ``changed_paths``, because a pull request that
    *deletes* a file has it in the changed set and not in the tree.
    """
    resolved = safe_repo_path(path)
    protected = gate_paths.match_any(resolved, list(policy.protected_paths))
    if protected:
        raise _refuse(
            ReasonCode.PATH_PROTECTED,
            f"{resolved} is protected by the pattern {protected!r} and is never edited by a fix",
        )
    if not known and not policy.allow_new_files:
        raise _refuse(
            ReasonCode.NEW_FILE_REFUSED,
            f"{resolved} does not exist at this commit, and creating files is disabled",
        )
    if policy.restrict_to_changed_files and resolved not in changed_paths:
        raise _refuse(
            ReasonCode.PATH_NOT_IN_DIFF,
            f"{resolved} is not touched by this pull request, so a fix will not touch it either",
        )
    return resolved


def _apply_edit(content: str, edit: FileEdit, *, exists: bool) -> str:
    """Replace exactly one occurrence of ``edit.find``, or refuse.

    "Exactly one" is the contract for an edit to a file that is already there.
    Zero means the model quoted code that is not there; more than one means it
    quoted something ambiguous, and picking the first would be a coin flip on
    which call site gets changed.

    A file that does *not* exist has nothing to quote, so an empty ``find`` is
    how a new file is written — and only then. An empty ``find`` on an existing
    file is still a refusal: it would mean "replace nothing", which is not an
    edit anybody meant to make.
    """
    if not edit.find:
        if exists:
            raise _refuse(
                ReasonCode.PATCH_INVALID,
                f"An edit to {edit.path} quoted no existing code to replace",
            )
        if not edit.replace:
            raise _refuse(
                ReasonCode.PATCH_EMPTY,
                f"An edit would create {edit.path} with no content",
            )
        return edit.replace
    occurrences = content.count(edit.find)
    if occurrences == 0:
        raise _refuse(
            ReasonCode.PATCH_NOT_APPLICABLE,
            f"The code an edit quoted was not found verbatim in {edit.path}",
        )
    if occurrences > 1:
        raise _refuse(
            ReasonCode.PATCH_NOT_APPLICABLE,
            f"The code an edit quoted appears {occurrences} times in {edit.path}; "
            "a fix will not guess which one was meant",
        )
    if edit.find == edit.replace:
        raise _refuse(
            ReasonCode.PATCH_EMPTY,
            f"An edit to {edit.path} replaces code with itself",
        )
    return content.replace(edit.find, edit.replace, 1)


def render_diff(before: dict[str, str], after: dict[str, str]) -> tuple[str, int, int]:
    """Unified diff of the applied result, plus added and deleted line counts.

    Produced from the two contents rather than taken from the model, so what a
    reviewer is shown is what a commit would contain.
    """
    chunks: list[str] = []
    added = deleted = 0
    for path in sorted(after):
        old = before.get(path, "")
        new = after[path]
        if old == new:
            continue
        old_lines = old.splitlines(keepends=True)
        new_lines = new.splitlines(keepends=True)
        lines = list(
            difflib.unified_diff(
                old_lines,
                new_lines,
                fromfile=f"a/{path}" if old else "/dev/null",
                tofile=f"b/{path}",
                n=3,
            )
        )
        for line in lines:
            if line.startswith("+") and not line.startswith("+++"):
                added += 1
            elif line.startswith("-") and not line.startswith("---"):
                deleted += 1
        chunks.append(f"diff --git a/{path} b/{path}\n")
        chunks.append("".join(line if line.endswith("\n") else line + "\n" for line in lines))
    return "".join(chunks), added, deleted


def enforce_limits(patch: FixPatch, policy: EffectivePolicy) -> None:
    """Refuse a patch that is bigger than the policy allows.

    Checked on the applied result, after the diff exists. A fix is meant to be
    a change a human reads in one sitting; anything past these numbers is a
    refactor wearing a fix's clothes, and the honest answer is to say so rather
    than to open a pull request nobody will review.
    """
    if patch.changed_files > policy.max_files:
        raise _refuse(
            ReasonCode.TOO_MANY_FILES,
            f"The fix touches {patch.changed_files} files; the limit is {policy.max_files}",
        )
    total_lines = patch.added_lines + patch.deleted_lines
    if total_lines > policy.max_lines:
        raise _refuse(
            ReasonCode.TOO_MANY_LINES,
            f"The fix changes {total_lines} lines; the limit is {policy.max_lines}",
        )
    size = len(patch.diff.encode("utf-8"))
    if size > policy.max_patch_bytes:
        raise _refuse(
            ReasonCode.PATCH_TOO_LARGE,
            f"The patch is {size} bytes; the limit is {policy.max_patch_bytes}",
        )


def apply_patch(
    edits: list[FileEdit],
    *,
    sources: dict[str, str],
    policy: EffectivePolicy,
    changed_paths: set[str],
    summary: str = "",
    rationale: str = "",
    model: str = "",
    prompt_digest: str = "",
) -> FixPatch:
    """Apply every edit to ``sources`` and return the resulting patch.

    ``sources`` is ``path -> content at the head commit``, fetched by the
    caller. A path missing from it is a file that does not exist yet, which is
    only allowed when the policy allows new files.

    All or nothing: the first refusal aborts, and no partial result is
    returned. There is no state to unwind because nothing has been written —
    which is the reason this step happens before anything reaches a platform.
    """
    if not edits:
        raise _refuse(ReasonCode.NO_PATCH, "The model proposed no change")

    before: dict[str, str] = {}
    after: dict[str, str] = {}
    checked: list[FileEdit] = []
    for edit in edits:
        resolved = check_path(
            edit.path,
            policy=policy,
            changed_paths=changed_paths,
            known=edit.path in sources or gate_paths.normalize(edit.path) in sources,
        )
        known = resolved in sources or edit.path in sources
        original = sources.get(resolved, sources.get(edit.path, ""))
        if resolved not in before:
            before[resolved] = original
            after[resolved] = original
        normalised = FileEdit(
            path=resolved,
            find=edit.find,
            replace=edit.replace,
            rationale=edit.rationale,
        )
        # A second edit to a path this patch just created is editing a file
        # that exists as far as the patch is concerned, so it goes back to
        # needing an exact quote.
        after[resolved] = _apply_edit(
            after[resolved], normalised, exists=known or bool(after[resolved])
        )
        checked.append(normalised)

    changed = {path: content for path, content in after.items() if content != before.get(path, "")}
    if not changed:
        raise _refuse(ReasonCode.PATCH_EMPTY, "Applying the fix produced no change")

    diff, added, deleted = render_diff(before, changed)
    patch = FixPatch(
        edits=checked,
        summary=summary,
        rationale=rationale,
        model=model,
        prompt_digest=prompt_digest,
        diff=diff,
        files=changed,
        changed_files=len(changed),
        added_lines=added,
        deleted_lines=deleted,
    )
    enforce_limits(patch, policy)
    return patch
