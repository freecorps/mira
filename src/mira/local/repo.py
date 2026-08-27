"""What is being reviewed, and which repository it belongs to.

Two jobs, both of them purely local.

**Producing the diff.** ``--working-tree``, ``--staged`` and ``--range`` are
three questions to git, not three code paths: each resolves to a base and a
comparison, and every one of them goes through the same classification and the
same exclusions. The classification comes from ``git diff --raw -z`` rather
than from the unified diff, because the raw form is the only one that reports a
file's *mode* — and mode ``160000`` is the difference between a submodule
pointer and a one-line text file. A submodule bump rendered as a unified diff
is a plausible-looking file called ``sm`` containing the line
``Subproject commit <sha>``; handing that to a reviewing model produces
confident nonsense about a file that does not exist.

**Naming the repository.** Retrieval of learned rules and indexed context is
keyed by ``(platform, owner, repo)``, and so is every policy that can be scoped
per repository. A local review that guessed a different key would silently read
an empty index and run under the global policy instead of the repository's
own — it would still produce output, which is what makes it worth getting
right. The key is derived from the configured remote, exactly as the server
derives it from a pull request URL, and can be stated outright when the remote
does not say (a self-hosted Forgejo behind a vanity host).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

from mira.local.gitcmd import (
    EMPTY_TREE,
    GitError,
    current_branch,
    head_exists,
    merge_base,
    resolve_commit,
    run_git,
)

#: The three things a local review can look at.
MODE_WORKING_TREE = "working_tree"
MODE_STAGED = "staged"
MODE_RANGE = "range"

#: Git's mode for a gitlink — a submodule pointer rather than a file.
_SUBMODULE_MODE = "160000"

#: Untracked files are synthesised into "new file" diffs by hand (see
#: :func:`_untracked_diff`), so a cap belongs here rather than being left to the
#: review's own size budget: a 40 MB untracked artefact should never be read
#: into memory in the first place.
MAX_UNTRACKED_BYTES = 512 * 1024

#: Ceiling on everything synthesised from untracked files in one run. The
#: per-file limit bounds one file; a scratch directory holding four thousand of
#: them is bounded by nothing without this. What the ceiling excluded is named
#: in the report rather than dropped quietly.
MAX_UNTRACKED_TOTAL_BYTES = 4 * 1024 * 1024

#: Characters git escapes when it writes a path into a patch header. A path
#: holding one of these is skipped rather than interpolated: a newline in a
#: filename would end the header line it appears in and let the rest of the
#: name forge the next one.
_UNQUOTABLE = frozenset('"\\') | {chr(code) for code in range(0x20)} | {chr(0x7F)}


def _needs_quoting(path: str) -> bool:
    """Whether git would have to quote this path in a diff header."""
    return any(character in _UNQUOTABLE for character in path)


#: Hosts whose platform is not in doubt. Anything else falls back to the
#: configured provider type, or to whatever the caller stated.
_KNOWN_HOSTS = {
    "github.com": "github",
    "www.github.com": "github",
    "gitlab.com": "gitlab",
    "www.gitlab.com": "gitlab",
    "codeberg.org": "forgejo",
}

_SCP_LIKE = re.compile(r"^(?:(?P<user>[^@/]+)@)?(?P<host>[^:/]+):(?P<path>.+)$")


def _is_windows_drive(host: str, path: str) -> bool:
    """Whether ``host:path`` is a drive letter rather than an scp-like remote.

    ``C:/repos/widgets.git`` and ``git@github.com:acme/widgets.git`` have the
    same shape, and git resolves the ambiguity the same way: one character
    before the colon, followed by a separator, is a drive. Without this a local
    Windows checkout reports its remote's host as ``c`` and its owner as the
    directory the repository happens to sit in.
    """
    return len(host) == 1 and host.isalpha() and path[:1] in ("/", "\\")


@dataclass(frozen=True)
class ChangedEntry:
    """One path git reports as changed, and what the review did with it."""

    path: str
    status: str
    old_path: str = ""
    submodule: bool = False
    binary: bool = False
    reviewed: bool = False
    #: Why it was not reviewed. Empty when it was.
    excluded_reason: str = ""

    def as_dict(self) -> dict[str, object]:
        data: dict[str, object] = {
            "path": self.path,
            "status": self.status,
            "reviewed": self.reviewed,
        }
        if self.old_path:
            data["old_path"] = self.old_path
        if self.submodule:
            data["submodule"] = True
        if self.binary:
            data["binary"] = True
        if self.excluded_reason:
            data["excluded_reason"] = self.excluded_reason
        return data


@dataclass
class LocalDiff:
    """The diff a local review will read, and everything left out of it."""

    mode: str
    diff_text: str
    entries: list[ChangedEntry] = field(default_factory=list)
    #: Human-readable description of what was compared, e.g. "HEAD..working tree".
    comparison: str = ""
    base_label: str = ""
    head_label: str = ""
    base_sha: str = ""
    head_sha: str = ""
    #: Untracked paths present in the work tree. Included in ``diff_text`` only
    #: when the caller asked for them.
    untracked: list[str] = field(default_factory=list)
    untracked_included: bool = False
    #: Notes worth showing the user: submodules skipped, untracked files left
    #: out, an untracked file too large to synthesise.
    notes: list[str] = field(default_factory=list)

    @property
    def is_empty(self) -> bool:
        return not self.diff_text.strip()


@dataclass(frozen=True)
class RepoIdentity:
    """The key a local review shares with the server's view of this repository."""

    root: Path
    platform: str
    owner: str
    repo: str
    remote_name: str = ""
    remote_url: str = ""
    branch: str = ""
    head_sha: str = ""
    #: True when owner/repo were stated by the caller rather than derived.
    stated: bool = False

    @property
    def known(self) -> bool:
        """False when there is no remote to derive a key from.

        A local review still runs: retrieval finds nothing, per-repository
        policy falls back to the global one, and the output says so.
        """
        return bool(self.owner and self.repo)

    @property
    def slug(self) -> str:
        return f"{self.owner}/{self.repo}" if self.known else ""


# ── Repository identity ─────────────────────────────────────────────────────


def _strip_git_suffix(path: str) -> str:
    return path[:-4] if path.endswith(".git") else path


def split_remote_url(url: str) -> tuple[str, str, str]:
    """``(host, owner, repo)`` for a remote URL, or empty strings.

    Handles the three shapes a checkout actually carries: an https URL, an
    ``ssh://`` URL, and the scp-like ``git@host:owner/repo.git``. ``owner`` may
    contain slashes, because a GitLab project can live under nested groups and
    the server's own URL parser treats everything before the last segment as the
    namespace.
    """
    raw = (url or "").strip()
    if not raw:
        return "", "", ""

    host = ""
    path = ""
    if "://" in raw:
        parts = urlsplit(raw)
        host = (parts.hostname or "").lower()
        path = parts.path
    else:
        match = _SCP_LIKE.match(raw)
        if match and not _is_windows_drive(match.group("host"), match.group("path")):
            host = match.group("host").lower()
            path = match.group("path")
        else:
            # A local path remote (`/srv/git/repo.git`, `../other`). There is a
            # repository name in it but no platform and no namespace.
            path = raw

    path = _strip_git_suffix(path.strip("/"))
    if not host:
        # A local-path remote (`/srv/git/repo.git`, `../other`, a `file://`
        # URL). There is a directory name in it and no namespace: `/srv/git`
        # is not an owner, and returning it as one would key retrieval and
        # per-repository policy on a path that happens to be on this disk.
        # An unidentified checkout is reported and asks for `--repo`; a
        # confidently wrong one silently reads an empty index.
        return "", "", ""
    if not path:
        return host, "", ""
    segments = [segment for segment in path.split("/") if segment]
    if len(segments) < 2:
        return host, "", segments[-1] if segments else ""
    return host, "/".join(segments[:-1]), segments[-1]


def platform_for_host(host: str, fallback: str) -> str:
    """The platform a host implies, or ``fallback`` when it implies nothing."""
    normalized = (host or "").lower()
    if normalized in _KNOWN_HOSTS:
        return _KNOWN_HOSTS[normalized]
    # A self-hosted instance usually names itself. This is a hint, not a
    # decision: anything unrecognised falls back to the configured provider.
    for marker, platform in (("gitlab", "gitlab"), ("forgejo", "forgejo"), ("gitea", "forgejo")):
        if marker in normalized:
            return platform
    return fallback


def _remote_url(repo_root: Path, remote: str) -> str:
    result = run_git(repo_root, "remote", "get-url", remote)
    return result.stdout.strip() if result.ok else ""


def _first_remote(repo_root: Path) -> str:
    result = run_git(repo_root, "remote")
    if not result.ok:
        return ""
    names = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    if not names:
        return ""
    return "origin" if "origin" in names else names[0]


def identify_repo(
    repo_root: Path,
    *,
    fallback_platform: str,
    remote: str = "",
    stated_slug: str = "",
    stated_platform: str = "",
) -> RepoIdentity:
    """Work out which repository this checkout is, for retrieval and policy.

    ``stated_slug`` (``owner/repo``) and ``stated_platform`` win over anything
    derived, because a self-hosted instance on a vanity host cannot be guessed
    and guessing wrong is worse than being told.
    """
    remote_name = remote or _first_remote(repo_root)
    remote_url = _remote_url(repo_root, remote_name) if remote_name else ""
    if remote and not remote_url:
        raise GitError(f"remote {remote!r} is not configured in this repository")

    host, owner, name = split_remote_url(remote_url)
    platform = stated_platform or platform_for_host(host, fallback_platform)

    stated = False
    if stated_slug:
        if "/" not in stated_slug:
            raise GitError(f"--repo expects owner/repo, got {stated_slug!r}")
        owner, _, name = stated_slug.rpartition("/")
        owner = owner.strip("/")
        stated = True
        if not owner or not name:
            raise GitError(f"--repo expects owner/repo, got {stated_slug!r}")

    head = ""
    if head_exists(repo_root):
        head = resolve_commit(repo_root, "HEAD")

    return RepoIdentity(
        root=repo_root,
        platform=platform,
        owner=owner,
        repo=name,
        remote_name=remote_name,
        remote_url=remote_url,
        branch=current_branch(repo_root),
        head_sha=head,
        stated=stated,
    )


# ── Diff production ─────────────────────────────────────────────────────────


def parse_range(spec: str) -> tuple[str, str, bool]:
    """``(base, head, three_dot)`` for a commit-range string.

    ``A...B`` is the merge-base form and is what a pull request shows, so it is
    supported and named; ``A..B`` compares the two commits directly. Anything
    else — a bare revision, an empty side, a stray option — is a usage error
    here rather than a confusing git message three calls later.
    """
    raw = (spec or "").strip()
    if not raw:
        raise ValueError("--range needs a commit range, e.g. main..HEAD")
    if "..." in raw:
        base, _, head = raw.partition("...")
        three_dot = True
    elif ".." in raw:
        base, _, head = raw.partition("..")
        three_dot = False
    else:
        raise ValueError(
            f"{spec!r} is not a commit range. Use <base>..<head> or <base>...<head> "
            "(for example main..HEAD)."
        )
    base = base.strip()
    head = head.strip() or "HEAD"
    if not base:
        raise ValueError(f"{spec!r} has no base revision. Use <base>..<head>.")
    # A leading or trailing dot on either side is what is left over when the
    # separator was mistyped — `main....feature` splits into `main` and
    # `.feature`. Git forbids both shapes in a ref name, so rejecting them here
    # turns a typo into a usage error instead of "no such revision", which
    # sends the reader looking for a branch they never named.
    for side in (base, head):
        if ".." in side or side.startswith(".") or side.endswith("."):
            raise ValueError(
                f"{spec!r} is not a commit range. Use exactly <base>..<head> or <base>...<head>."
            )
    return base, head, three_dot


def _parse_raw_z(raw: str) -> list[ChangedEntry]:
    """Parse ``git diff --raw -z`` into entries, modes included.

    The record is ``:<srcmode> <dstmode> <srcsha> <dstsha> <status>`` followed by
    one path, or by two when the status is a rename or a copy.
    """
    fields = [field for field in raw.split("\0") if field != ""]
    entries: list[ChangedEntry] = []
    index = 0
    while index < len(fields):
        meta = fields[index]
        index += 1
        if not meta.startswith(":"):
            # Not a record header. Skipping is right: a malformed stream must
            # not shift every following path onto the wrong status.
            continue
        parts = meta[1:].split()
        if len(parts) < 5:
            continue
        src_mode, dst_mode, _src_sha, _dst_sha, status = (
            parts[0],
            parts[1],
            parts[2],
            parts[3],
            parts[4],
        )
        renamed = status[:1] in ("R", "C")
        if index >= len(fields):
            break
        first = fields[index]
        index += 1
        old_path = ""
        path = first
        if renamed:
            if index >= len(fields):
                break
            path = fields[index]
            old_path = first
            index += 1
        entries.append(
            ChangedEntry(
                path=path,
                status=status[:1],
                old_path=old_path,
                submodule=_SUBMODULE_MODE in (src_mode, dst_mode),
            )
        )
    return entries


def _diff_args(mode: str, base: str, head: str, three_dot: bool) -> list[str]:
    """The comparison half of a ``git diff`` invocation, without pathspecs."""
    if mode == MODE_STAGED:
        return ["--cached", base]
    if mode == MODE_RANGE:
        return [f"{base}...{head}"] if three_dot else [base, head]
    return [base]


#: Ceiling on one `git diff` invocation's output. Generous - the review has its
#: own, much smaller, budget and does the priority selection - but present,
#: because the alternative to a ceiling is reading an arbitrary repository into
#: memory. Reaching it is reported rather than absorbed: a diff cut mid-hunk
#: would otherwise arrive at the parser as a malformed one and fail as a Mira
#: bug rather than as the oversized comparison it is.
MAX_DIFF_BYTES = 20_000_000


def _run_diff(repo_root: Path, comparison: list[str], excluded: list[str], raw: bool) -> str:
    argv = ["diff", "--no-color", "--no-ext-diff", "--find-renames", *comparison]
    if raw:
        argv += ["--raw", "-z", "--abbrev=40"]
    argv.append("--")
    # An exclude-only pathspec matches nothing in git, so the positive "." has
    # to be there for the exclusions to subtract from anything. `literal` on the
    # exclusions, because a repository may legitimately contain a path with a
    # `*` or a `[` in it and a plain pathspec would read those as a pattern,
    # excluding files nobody asked to exclude and saying nothing about it.
    argv.append(".")
    argv += [f":(exclude,literal){path}" for path in excluded]
    result = run_git(repo_root, *argv, max_output_bytes=MAX_DIFF_BYTES)
    if not result.ok:
        message = (result.stderr or result.stdout).strip().splitlines()
        raise GitError(f"git diff failed: {message[0] if message else result.exit_code}")
    if len(result.stdout) >= MAX_DIFF_BYTES:
        raise GitError(
            f"this comparison produces more than {MAX_DIFF_BYTES // 1_000_000} MB of diff, "
            "which is too much to read in one go. Review a narrower commit range."
        )
    return result.stdout


def _untracked_paths(repo_root: Path) -> list[str]:
    """Untracked, non-ignored paths in the work tree.

    ``--others --exclude-standard`` rather than ``git status``: it answers the
    one question asked, and it never writes an index refresh.
    """
    result = run_git(repo_root, "ls-files", "--others", "--exclude-standard", "-z")
    if not result.ok:
        return []
    return sorted(path for path in result.stdout.split("\0") if path)


def _untracked_diff(repo_root: Path, paths: list[str], notes: list[str]) -> tuple[str, list[str]]:
    """Synthesise "new file" diffs for untracked paths, without touching git.

    The obvious implementations are ``git add --intent-to-add`` and
    ``git diff --no-index``. The first writes to the developer's index, which
    this surface promises never to do. The second produces headers naming a
    temporary file rather than the repository path, which then has to be
    rewritten anyway. Building the patch is a dozen lines and is the only
    version that is both read-only and correct.

    Two things it refuses rather than attempts.

    A path git would have to *quote* in a header — one holding a newline, a
    tab, a quote or a backslash — is skipped and named. Interpolating one
    verbatim would let a filename split the header it appears in and forge
    another, and the parser downstream would then attribute a file's contents
    to a path nobody wrote. Git's own answer is C-style quoting, but a quoted
    header only helps a reader that unquotes it, and the point of synthesising
    this patch at all is that it is read by the same parser as git's output.

    An aggregate byte ceiling stops the loop, because a per-file limit bounds
    nothing when a directory holds four thousand files. What was dropped is
    named: a silent cap reads as "there was nothing else".
    """
    sections: list[str] = []
    included: list[str] = []
    budget = MAX_UNTRACKED_TOTAL_BYTES
    for index, rel in enumerate(paths):
        if _needs_quoting(rel):
            notes.append(
                f"An untracked file was not reviewed: its name contains a character "
                f"git would have to quote in a diff header ({rel!r})."
            )
            continue
        target = repo_root / rel
        try:
            if not target.is_file() or target.is_symlink():
                continue
            size = target.stat().st_size
            if size > MAX_UNTRACKED_BYTES:
                notes.append(
                    f"Untracked file {rel} was not reviewed: it is larger than "
                    f"{MAX_UNTRACKED_BYTES // 1024} KiB."
                )
                continue
            if size > budget:
                remaining = len(paths) - index
                notes.append(
                    f"{remaining} untracked file(s) were not reviewed: the total "
                    f"synthesised from them would have passed "
                    f"{MAX_UNTRACKED_TOTAL_BYTES // 1024} KiB."
                )
                break
            budget -= size
            data = target.read_bytes()
        except OSError as exc:
            notes.append(f"Untracked file {rel} could not be read: {exc}")
            continue
        if b"\0" in data:
            notes.append(f"Untracked file {rel} was not reviewed: it looks binary.")
            continue
        if not data:
            notes.append(f"Untracked file {rel} was not reviewed: it is empty.")
            continue
        text = data.decode("utf-8", errors="replace")
        # `split`, not `splitlines`: the latter also breaks on a form feed and
        # on the Unicode separators, which would put a line count in the hunk
        # header that no other tool agrees with. A CR is left on the line, which
        # is what git does with a CRLF file.
        lines = text.split("\n")
        ends_with_newline = lines[-1] == ""
        if ends_with_newline:
            lines.pop()
        body = [f"+{line}" for line in lines]
        if not ends_with_newline:
            body.append("\\ No newline at end of file")
        sections.append(
            "\n".join(
                [
                    f"diff --git a/{rel} b/{rel}",
                    "new file mode 100644",
                    "--- /dev/null",
                    f"+++ b/{rel}",
                    f"@@ -0,0 +1,{len(lines)} @@",
                    *body,
                    "",
                ]
            )
        )
        included.append(rel)
    return "".join(sections), included


def resolve_diff(
    repo_root: Path,
    *,
    mode: str,
    range_spec: str = "",
    include_untracked: bool = False,
) -> LocalDiff:
    """Produce the diff for one of the three local modes.

    Raises :class:`GitError` for anything git could not answer and ``ValueError``
    for a range the user typed wrong — the caller turns the first into an exit
    code about the repository and the second into one about the command line.
    """
    notes: list[str] = []
    three_dot = False
    if mode == MODE_RANGE:
        base_rev, head_rev, three_dot = parse_range(range_spec)
        base_sha = resolve_commit(repo_root, base_rev)
        head_sha = resolve_commit(repo_root, head_rev)
        if three_dot:
            # Fail here rather than letting git report an empty diff for two
            # histories that never met: "no common ancestor" and "no changes"
            # are opposite answers and must not share an exit code.
            base_sha = merge_base(repo_root, base_sha, head_sha)
        base_label, head_label = base_rev, head_rev
        comparison_label = f"{base_rev}{'...' if three_dot else '..'}{head_rev}"
        compare_from, compare_to = base_rev, head_rev
    else:
        has_head = head_exists(repo_root)
        base_rev = "HEAD" if has_head else EMPTY_TREE
        if not has_head:
            notes.append(
                "This repository has no commits yet, so the change is compared "
                "against the empty tree."
            )
        base_sha = resolve_commit(repo_root, base_rev) if has_head else EMPTY_TREE
        head_sha = ""
        base_label = "HEAD" if has_head else "empty tree"
        head_label = "index" if mode == MODE_STAGED else "working tree"
        comparison_label = f"{base_label} -> {head_label}"
        compare_from, compare_to = base_rev, ""

    comparison = _diff_args(mode, compare_from, compare_to, three_dot)

    raw = _run_diff(repo_root, comparison, [], raw=True)
    entries = _parse_raw_z(raw)
    submodules = [entry.path for entry in entries if entry.submodule]
    if submodules:
        notes.append(
            "Submodule pointer change"
            + ("s" if len(submodules) > 1 else "")
            + " not reviewed: "
            + ", ".join(sorted(submodules))
            + ". A gitlink records a commit id, not code; review it in the submodule."
        )

    diff_text = _run_diff(repo_root, comparison, submodules, raw=False)

    # Untracked files belong to the working tree and to nothing else. They are
    # not in the index, so they are not part of what a commit would contain, and
    # they are in no commit, so they are not part of a range.
    untracked = _untracked_paths(repo_root) if mode == MODE_WORKING_TREE else []
    untracked_included: list[str] = []
    if untracked and include_untracked:
        extra, untracked_included = _untracked_diff(repo_root, untracked, notes)
        if extra:
            if diff_text and not diff_text.endswith("\n"):
                diff_text += "\n"
            diff_text += extra
        for rel in untracked_included:
            entries.append(ChangedEntry(path=rel, status="A"))
    elif untracked:
        notes.append(
            f"{len(untracked)} untracked file(s) were not reviewed. "
            "Pass --include-untracked to review them."
        )

    return LocalDiff(
        mode=mode,
        diff_text=diff_text,
        entries=entries,
        comparison=comparison_label,
        base_label=base_label,
        head_label=head_label,
        base_sha=base_sha,
        head_sha=head_sha,
        untracked=untracked,
        untracked_included=bool(untracked_included),
        notes=notes,
    )
