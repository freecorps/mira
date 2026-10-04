"""Implementation plans for new issues.

When an issue is opened — or someone comments ``@mira plan`` on one — Mira
reads it, finds the files a change would most likely touch, and posts a plan:
what is being asked, which files, in what order, what could go wrong, and what
to test. One comment per issue, found again by a hidden marker, so a re-run
edits the plan rather than stacking a second one under it.

**Where the files come from.** The repository index first: every indexed
file's path and summary is scored against the issue's words, and the best few
are re-scored with their symbols. A repository that was never indexed falls
back to its file tree, scored on paths alone. The model then picks from that
list; an existing path it names that is not in the repository is dropped, so
the plan can be wrong about *which* file but never invents one.

**What the model is shown.** The issue is written by anyone who can open one,
and index summaries are text a model wrote about repository content. Both go
in as untrusted blocks, redacted and bounded; the output is a structured
object with no field that does anything but describe.

**What the comment can say.** Generated text loses the ``@`` in front of any
handle, so a plan never pings a person and never addresses the bot — a plan
that quoted ``@mira plan`` would otherwise be a command. The marker itself is
removed from anything the model wrote.

Every entry point is best-effort and never raises: a plan that could not be
written is a log line, never a failed webhook.
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from jinja2 import Environment, FileSystemLoader
from pydantic import BaseModel, Field

from mira.autofix.redact import redact
from mira.config import IssuePlannerConfig, MiraConfig
from mira.llm import untrusted
from mira.models import IssueInfo, PRInfo
from mira.platforms.chat_commands import normalize_command
from mira.platforms.mentions import strip_mentions

if TYPE_CHECKING:
    from mira.llm.base import LLMProviderProtocol

logger = logging.getLogger(__name__)

PLAN_MARKER = "<!-- mira:issue-plan -->"

PLAN_KEYWORDS = {"plan", "plan this", "plan this issue", "implementation plan", "coding plan"}

# How much of the issue the model reads. An issue body is unbounded input from
# anybody; a plan needs the ask, not a pasted log of 200 kB.
MAX_TITLE_CHARS = 300
MAX_BODY_CHARS = 8_000

# How much of the repository the ranking reads. Bounded so a monorepo costs a
# fixed amount of work per issue rather than an amount proportional to its size.
_MAX_SCANNED_FILES = 20_000
_PAGE = 1_000
_MAX_KEYWORDS = 40
_SUMMARY_CHARS = 300
_SYMBOLS_PER_FILE = 8

# The rendered comment: per-item and per-section caps.
_MAX_ITEM_CHARS = 500
_MAX_ITEMS = 12

_TEMPLATE_ENV = Environment(
    loader=FileSystemLoader(str(Path(__file__).resolve().parents[1] / "llm/prompts/templates")),
    trim_blocks=True,
    lstrip_blocks=True,
)

# Words that say nothing about *where* a change goes. Matching them against
# paths would rank every file that has `test` or `get` in it.
_STOPWORDS = frozenset(
    [
        "a",
        "about",
        "above",
        "after",
        "again",
        "against",
        "all",
        "also",
        "am",
        "an",
        "and",
        "any",
        "are",
        "as",
        "at",
        "be",
        "because",
        "been",
        "before",
        "being",
        "below",
        "between",
        "both",
        "but",
        "by",
        "can",
        "cannot",
        "could",
        "did",
        "do",
        "does",
        "doing",
        "down",
        "during",
        "each",
        "few",
        "for",
        "from",
        "further",
        "had",
        "has",
        "have",
        "having",
        "he",
        "her",
        "here",
        "hers",
        "him",
        "his",
        "how",
        "i",
        "if",
        "in",
        "into",
        "is",
        "it",
        "its",
        "itself",
        "just",
        "let",
        "me",
        "more",
        "most",
        "my",
        "no",
        "nor",
        "not",
        "now",
        "of",
        "off",
        "on",
        "once",
        "only",
        "or",
        "other",
        "our",
        "ours",
        "out",
        "over",
        "own",
        "same",
        "she",
        "should",
        "so",
        "some",
        "such",
        "than",
        "that",
        "the",
        "their",
        "theirs",
        "them",
        "then",
        "there",
        "these",
        "they",
        "this",
        "those",
        "through",
        "to",
        "too",
        "under",
        "until",
        "up",
        "very",
        "was",
        "we",
        "were",
        "what",
        "when",
        "where",
        "which",
        "while",
        "who",
        "whom",
        "why",
        "will",
        "with",
        "would",
        "you",
        "your",
        "yours",
        "issue",
        "issues",
        "bug",
        "bugs",
        "problem",
        "please",
        "thanks",
        "thank",
        "hi",
        "hello",
        "want",
        "wants",
        "need",
        "needs",
        "like",
        "make",
        "makes",
        "made",
        "use",
        "used",
        "using",
        "get",
        "gets",
        "got",
        "set",
        "new",
        "add",
        "adds",
        "added",
        "fix",
        "fixes",
        "fixed",
        "currently",
        "current",
        "way",
        "ways",
        "thing",
        "things",
        "something",
        "someone",
        "anything",
        "able",
        "expected",
        "actual",
        "behavior",
        "behaviour",
        "describe",
        "description",
        "steps",
        "reproduce",
        "feature",
        "request",
        "support",
        "work",
        "works",
        "working",
        "doesn",
        "don",
        "isn",
        "wasn",
        "won",
        "didn",
        "true",
        "false",
        "null",
        "none",
        "yes",
        "also",
        "e",
        "g",
        "eg",
        "ie",
        "etc",
        "via",
        "one",
        "two",
        "see",
        "seems",
        "seem",
        "maybe",
    ]
)

# Directories a plan never needs to point into.
_SKIP_DIRS = ("node_modules/", "vendor/", "dist/", "build/", ".git/", "__pycache__/")
_SKIP_SUFFIXES = (
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".svg",
    ".ico",
    ".lock",
    ".min.js",
    ".map",
    ".pdf",
    ".woff",
    ".woff2",
    ".ttf",
)


# ── The model's answer ──────────────────────────────────────────────────────


class PlannedFile(BaseModel):
    path: str = Field(description="Repository-relative path, copied from the candidate list.")
    reason: str = Field(default="", description="One line: why this file changes.")
    new: bool = Field(default=False, description="True only for a file the change creates.")


class IssuePlan(BaseModel):
    summary: str = Field(description="Two or three sentences restating the ask.")
    files: list[PlannedFile] = Field(default_factory=list)
    steps: list[str] = Field(default_factory=list)
    risks: list[str] = Field(default_factory=list)
    open_questions: list[str] = Field(default_factory=list)
    tests: list[str] = Field(default_factory=list)


# ── Candidate files ─────────────────────────────────────────────────────────


@dataclass
class CandidateFile:
    path: str
    score: float
    summary: str = ""
    symbols: list[str] = field(default_factory=list)


@dataclass
class Candidates:
    files: list[CandidateFile]
    # Every path the ranking saw, so a path the model names can be checked.
    known_paths: set[str]
    # "index", "tree", or "" when nothing could be read.
    source: str


def _split_identifier(word: str) -> list[str]:
    """``parseHTTPResponse`` → parse, http, response; ``snake_case`` → snake, case."""
    parts: list[str] = []
    for chunk in re.split(r"[_\-]+", word):
        parts.extend(re.findall(r"[A-Z]+(?=[A-Z][a-z])|[A-Z]?[a-z]+|[A-Z]+|\d+", chunk))
    return [p.lower() for p in parts if p]


def _path_tokens(path: str) -> set[str]:
    tokens: set[str] = set()
    for piece in re.split(r"[/.\s]+", path):
        if not piece:
            continue
        tokens.add(piece.lower())
        tokens.update(_split_identifier(piece))
    return tokens


_PATH_MENTION = re.compile(r"(?<![\w/.-])((?:[\w.-]+/)*[\w-]+\.[A-Za-z][\w]{0,7})(?![\w/])")


def mentioned_paths(text: str) -> list[str]:
    """File-looking tokens the issue names: ``src/app/auth.py``, ``config.yaml``."""
    seen: list[str] = []
    for match in _PATH_MENTION.finditer(text or ""):
        candidate = match.group(1).removeprefix("./")
        if not candidate or candidate.lower().startswith(("http", "www.")):
            continue
        if candidate not in seen:
            seen.append(candidate)
    return seen[:20]


def extract_keywords(title: str, body: str) -> dict[str, float]:
    """The issue's distinctive words, weighted: the title counts double.

    Identifiers are kept whole and split (``RetryPolicy`` gives ``retrypolicy``,
    ``retry`` and ``policy``), stopwords are dropped, and only the strongest
    ``_MAX_KEYWORDS`` survive so a long body cannot dilute the title.
    """
    weights: dict[str, float] = {}
    for text, weight in ((title or "", 2.0), (body or "", 1.0)):
        for word in re.findall(r"[A-Za-z_][A-Za-z0-9_]{2,}", text):
            pieces = {word.lower(), *_split_identifier(word)}
            for piece in pieces:
                if len(piece) < 3 or piece in _STOPWORDS or piece.isdigit():
                    continue
                weights[piece] = weights.get(piece, 0.0) + weight
    # Diminishing returns for repetition: a word said ten times is not ten
    # times more about the change than one said twice.
    scored = {k: min(v, 4.0) for k, v in weights.items()}
    top = sorted(scored.items(), key=lambda kv: (-kv[1], kv[0]))[:_MAX_KEYWORDS]
    return dict(top)


def _skippable(path: str) -> bool:
    lower = path.lower()
    return any(f"/{d}" in f"/{lower}" for d in _SKIP_DIRS) or lower.endswith(_SKIP_SUFFIXES)


_TEST_PATH = re.compile(r"(^|/)(tests?|spec|__tests__)/|(^|/)test_|_test\.|\.(test|spec)\.")


def score_path(path: str, keywords: dict[str, float], mentions: list[str]) -> float:
    """How strongly a path alone matches the issue."""
    score = 0.0
    lower = path.lower()
    base = lower.rsplit("/", 1)[-1]
    for mention in mentions:
        m = mention.lower()
        if lower == m or lower.endswith("/" + m):
            score += 30.0
        elif base == m.rsplit("/", 1)[-1]:
            score += 12.0
    tokens = _path_tokens(path)
    stem_tokens = _path_tokens(base.rsplit(".", 1)[0])
    for word, weight in keywords.items():
        if word in stem_tokens:
            score += 4.0 * weight
        elif word in tokens:
            score += 2.0 * weight
        elif len(word) >= 5 and word in lower:
            score += 1.0 * weight
    if score and _TEST_PATH.search(lower):
        # Tests follow the code they test; on a tie the code ranks first.
        score *= 0.9
    return score


def _text_score(text: str, keywords: dict[str, float], cap: float) -> float:
    words = {w.lower() for w in re.findall(r"[A-Za-z_][A-Za-z0-9_]{2,}", text or "")}
    expanded = set(words)
    for w in words:
        expanded.update(_split_identifier(w))
    return min(cap, sum(weight for word, weight in keywords.items() if word in expanded))


def rank_paths(
    paths: Iterable[str], keywords: dict[str, float], mentions: list[str], limit: int
) -> list[CandidateFile]:
    """Paths alone, best first: the fallback for an unindexed repository."""
    scored = [
        CandidateFile(path=p, score=s)
        for p in paths
        if not _skippable(p) and (s := score_path(p, keywords, mentions)) > 0
    ]
    scored.sort(key=lambda c: (-c.score, len(c.path), c.path))
    return scored[:limit]


def rank_indexed(
    store: Any, keywords: dict[str, float], mentions: list[str], limit: int
) -> tuple[list[CandidateFile], set[str]]:
    """Index-backed ranking: path and summary over every file, then symbols
    for the best few. Synchronous — the store is; run it in a thread."""
    pool: list[CandidateFile] = []
    known: set[str] = set()
    offset = 0
    while offset < _MAX_SCANNED_FILES:
        page = store.list_indexed_files(limit=_PAGE, offset=offset)
        if not page:
            break
        for fs in page:
            known.add(fs.path)
            if _skippable(fs.path):
                continue
            score = score_path(fs.path, keywords, mentions) + _text_score(
                fs.summary, keywords, cap=10.0
            )
            if score > 0:
                pool.append(CandidateFile(path=fs.path, score=score, summary=fs.summary or ""))
        if len(page) < _PAGE:
            break
        offset += _PAGE
    pool.sort(key=lambda c: (-c.score, c.path))
    shortlist = pool[: max(limit * 3, limit)]
    for cand in shortlist:
        try:
            full = store.get_summary(cand.path)
        except Exception as exc:  # noqa: BLE001 - symbols are a refinement
            logger.debug("Could not read symbols for %s: %s", cand.path, exc)
            continue
        if full is None:
            continue
        names = [s.name for s in (full.symbols or []) if getattr(s, "name", "")]
        cand.symbols = names[:_SYMBOLS_PER_FILE]
        cand.score += _text_score(" ".join(names), keywords, cap=12.0)
    shortlist.sort(key=lambda c: (-c.score, c.path))
    return shortlist[:limit], known


def _index_candidates(
    owner: str,
    repo: str,
    platform: str,
    keywords: dict[str, float],
    mentions: list[str],
    limit: int,
) -> tuple[list[CandidateFile], set[str]] | None:
    """Rank from the repository index, or None when there is no index to read.

    Opened the way the MCP reads open it: never creating a store, never
    answering from a backend other than the configured one.
    """
    from mira.mcp.authz import Repository
    from mira.mcp.reads import NotIndexed, is_indexed, open_index

    repository = Repository(platform=platform, owner=owner, repo=repo)
    if not is_indexed(repository):
        return None
    try:
        with open_index(repository) as store:
            ranked, known = rank_indexed(store, keywords, mentions, limit)
    except NotIndexed:
        return None
    return (ranked, known) if known else None


async def find_candidates(
    provider: Any,
    issue_ref: PRInfo,
    keywords: dict[str, float],
    mentions: list[str],
    limit: int,
    *,
    fetcher: Any = None,
) -> Candidates:
    """The files most likely to change, from the index or, failing that, the tree."""
    owner, repo, platform = issue_ref.owner, issue_ref.repo, issue_ref.platform
    try:
        indexed = await asyncio.to_thread(
            _index_candidates, owner, repo, platform, keywords, mentions, limit
        )
    except Exception as exc:  # noqa: BLE001 - fall through to the tree
        logger.warning("Index unavailable for issue plan on %s/%s: %s", owner, repo, exc)
        indexed = None
    if indexed is not None:
        return Candidates(files=indexed[0], known_paths=indexed[1], source="index")

    paths: list[str] = []
    try:
        if fetcher is not None:
            branch = await fetcher.default_branch(owner, repo)
            paths = list(await fetcher.repo_tree(owner, repo, branch) or [])
        else:
            paths = list(await provider.get_repo_tree(issue_ref, "HEAD") or [])
    except Exception as exc:  # noqa: BLE001 - a plan without files is still a plan
        logger.warning("Could not list files for issue plan on %s/%s: %s", owner, repo, exc)
    paths = paths[: _MAX_SCANNED_FILES * 2]
    if not paths:
        return Candidates(files=[], known_paths=set(), source="")
    return Candidates(
        files=rank_paths(paths, keywords, mentions, limit),
        known_paths=set(paths),
        source="tree",
    )


# ── Prompt ──────────────────────────────────────────────────────────────────


def _bounded(text: str, limit: int) -> str:
    text = text or ""
    if len(text) <= limit:
        return text
    return text[:limit].rsplit("\n", 1)[0] + "\n… (truncated)"


def build_plan_prompt(
    issue: IssueInfo,
    candidates: Candidates,
    *,
    repository: str,
    max_files: int,
) -> list[dict[str, str]]:
    issue_text = (
        f"Title: {_bounded(issue.title, MAX_TITLE_CHARS)}\n\n"
        f"{_bounded(issue.body, MAX_BODY_CHARS) or '(no description)'}"
    )
    lines: list[str] = []
    for cand in candidates.files:
        line = f"- {cand.path}"
        if cand.summary:
            line += f" — {' '.join(cand.summary.split())[:_SUMMARY_CHARS]}"
        if cand.symbols:
            line += f" [symbols: {', '.join(cand.symbols)}]"
        lines.append(line)
    template = _TEMPLATE_ENV.get_template("issue_plan.jinja2")
    prompt = template.render(
        repository=repository,
        max_files=max_files,
        issue_block=untrusted.block("ISSUE", issue_text, redactor=redact),
        candidates_block=(
            untrusted.block("FILE", "\n".join(lines), redactor=redact) if lines else ""
        ),
        candidates_source=(
            "from the repository index"
            if candidates.source == "index"
            else "from file paths only; the repository is not indexed"
        ),
    )
    return [
        {"role": "system", "content": prompt},
        {"role": "user", "content": "Write the implementation plan for this issue."},
    ]


# ── Rendering ───────────────────────────────────────────────────────────────


def _clean(text: str, limit: int = _MAX_ITEM_CHARS) -> str:
    """One line of generated text that can address nobody and hide nothing."""
    text = (text or "").replace(PLAN_MARKER, "")
    text = text.replace("<!--", "&lt;!--")
    # No handle survives: a plan neither pings people nor commands the bot.
    text = re.sub(r"@(?=[\w-])", "", text)
    text = " ".join(text.split())
    if len(text) > limit:
        text = text[: limit - 1].rstrip() + "…"
    return text


def _cell(text: str) -> str:
    return _clean(text).replace("|", "\\|")


def _safe_new_path(path: str) -> bool:
    return (
        bool(path)
        and len(path) <= 200
        and not path.startswith(("/", "~"))
        and ".." not in path.split("/")
        and re.fullmatch(r"[\w./ +-]+", path) is not None
    )


def vet_files(plan: IssuePlan, candidates: Candidates, max_files: int) -> list[PlannedFile]:
    """The model's files, held to the repository.

    An existing path must be one the ranking saw — the model may only pick, not
    invent. A new path must be relative and stay inside the repository. When
    the model named none that survive, the ranking's own top files stand in,
    so the section is never empty while there were candidates.
    """
    out: list[PlannedFile] = []
    seen: set[str] = set()
    for entry in plan.files:
        path = (entry.path or "").strip().strip("`").removeprefix("./")
        if not path or path in seen:
            continue
        if path in candidates.known_paths:
            out.append(PlannedFile(path=path, reason=entry.reason, new=False))
        elif entry.new and _safe_new_path(path):
            out.append(PlannedFile(path=path, reason=entry.reason, new=True))
        else:
            logger.debug("Dropping planned file not in the repository: %s", path)
            continue
        seen.add(path)
        if len(out) >= max_files:
            break
    if not out:
        out = [
            PlannedFile(path=c.path, reason="Matches the issue's wording.")
            for c in candidates.files[:max_files]
        ]
    return out


def _bullets(items: list[str], *, numbered: bool = False) -> list[str]:
    cleaned = [c for c in (_clean(i) for i in items[:_MAX_ITEMS]) if c]
    return [f"{n}. {c}" if numbered else f"- {c}" for n, c in enumerate(cleaned, 1)]


def render_plan(
    plan: IssuePlan,
    files: list[PlannedFile],
    *,
    source: str,
    bot_name: str,
    requested_by: str = "",
) -> str:
    parts = [PLAN_MARKER, "## Implementation plan", ""]
    summary = _clean(plan.summary, limit=1_500)
    if summary:
        parts += [f"**Summary.** {summary}", ""]
    if files:
        parts += ["### Likely files", "", "| # | File | Why |", "|---|---|---|"]
        for n, entry in enumerate(files, 1):
            path = entry.path.replace("`", "").replace("|", "\\|")
            label = f"`{path}`" + (" *(new)*" if entry.new else "")
            parts.append(f"| {n} | {label} | {_cell(entry.reason)} |")
        parts.append("")
    sections = (
        ("Steps", plan.steps, True),
        ("Risks", plan.risks, False),
        ("Open questions", plan.open_questions, False),
        ("Suggested tests", plan.tests, False),
    )
    for heading, items, numbered in sections:
        bullets = _bullets(items, numbered=numbered)
        if bullets:
            parts += [f"### {heading}", "", *bullets, ""]
    origin = {
        "index": "the issue and the repository index",
        "tree": "the issue and the repository's file paths (the repository is not indexed)",
    }.get(source, "the issue alone (no repository files could be read)")
    footer = (
        f"<sub>Written by Mira from {origin}. It is a starting point, not a spec. "
        f"Comment `@{bot_name} plan` to regenerate it; this comment is replaced each time."
    )
    if requested_by:
        footer += f" Last regenerated at the request of {_clean(requested_by, limit=60)}."
    parts.append(footer + "</sub>")
    return "\n".join(parts)


# ── Decisions ───────────────────────────────────────────────────────────────


def planner_config(config: Any) -> IssuePlannerConfig | None:
    cfg = getattr(config, "issue_planner", None)
    return cfg if isinstance(cfg, IssuePlannerConfig) else None


def is_plan_command(text: str, names: list[str]) -> bool:
    """Whether a comment is ``@mira plan`` (the whole command, mentions stripped)."""
    # Longest handle first, so `@mira-bot` is not read as `@mira` plus "-bot".
    ordered = sorted((n for n in names if n), key=len, reverse=True)
    return normalize_command(strip_mentions(text or "", ordered)) in PLAN_KEYWORDS


def skip_reason(
    issue: IssueInfo, cfg: IssuePlannerConfig, names: list[str], *, explicit: bool
) -> str:
    """Why this issue gets no plan, or ``""`` when it should get one.

    An explicit ``@mira plan`` is a person asking, so it is not held to the
    ``labels`` allowlist or to the issue being open; ``ignore_labels`` and an
    ``@mira ignore`` in the issue body stop it all the same.
    """
    labels = {label.lower() for label in issue.labels}
    ignored = [lbl for lbl in cfg.ignore_labels if lbl.lower() in labels]
    if ignored:
        return f"it carries the `{ignored[0]}` label"
    body = issue.body or ""
    if any(re.search(rf"@{re.escape(n)}[ \t]+ignore\b", body, re.IGNORECASE) for n in names if n):
        return "its description opts out with an ignore command"
    if explicit:
        return ""
    if issue.state and issue.state.lower() not in {"open", "opened", "reopened"}:
        return "it is closed"
    if cfg.labels and not any(lbl.lower() in labels for lbl in cfg.labels):
        return "it carries none of the labels in `issue_planner.labels`"
    return ""


# ── Orchestration ───────────────────────────────────────────────────────────


def issue_ref(owner: str, repo: str, number: int, platform: str) -> PRInfo:
    """A repository locator in the shape providers take, numbered as the issue."""
    return PRInfo(
        title="",
        description="",
        base_branch="",
        head_branch="",
        url="",
        number=int(number),
        owner=owner,
        repo=repo,
        platform=platform,
    )


async def upsert_plan_comment(provider: Any, ref: PRInfo, body: str) -> str:
    """Edit the existing plan comment, or post one. Returns "updated" or "posted"."""
    existing: int | None = None
    try:
        existing = await provider.find_issue_comment(ref, PLAN_MARKER)
    except Exception as exc:  # noqa: BLE001 - not finding one means posting one
        logger.debug("Could not look for an existing plan on #%s: %s", ref.number, exc)
    if existing is not None:
        try:
            await provider.update_issue_comment(ref, existing, body)
            return "updated"
        except Exception as exc:  # noqa: BLE001
            logger.warning("Could not edit plan comment %s; posting anew: %s", existing, exc)
    await provider.post_issue_comment(ref, body)
    return "posted"


async def _reply(provider: Any, ref: PRInfo, actor: str, text: str) -> None:
    try:
        prefix = f"> @{actor}: " if actor else ""
        await provider.post_issue_comment(ref, f"{prefix}{text}")
    except Exception as exc:  # noqa: BLE001
        logger.warning("Could not reply on issue #%s: %s", ref.number, exc)


async def run_issue_plan(
    provider: Any,
    owner: str,
    repo: str,
    number: int,
    *,
    platform: str,
    bot_name: str,
    bot_identity: str | None = None,
    explicit: bool = False,
    actor: str = "",
    config: MiraConfig | None = None,
    llm: LLMProviderProtocol | None = None,
    fetcher: Any = None,
) -> str:
    """Plan issue ``number`` and post or refresh the plan comment. Never raises.

    ``explicit`` is an ``@mira plan`` comment by ``actor``: it bypasses
    ``auto_on_open`` and the label allowlist, and a refusal is answered rather
    than logged. Returns what happened, for logs and tests: ``posted``,
    ``updated``, ``disabled``, ``skipped``, ``missing`` or ``failed``.
    """
    ref = issue_ref(owner, repo, number, platform)
    try:
        if config is None:
            from mira.config import load_config

            config = load_config()
        cfg = planner_config(config)
        if cfg is None or not cfg.enabled:
            if explicit:
                await _reply(
                    provider,
                    ref,
                    actor,
                    "the issue planner is off for this repository (`issue_planner.enabled`).",
                )
            return "disabled"
        if not explicit and not cfg.auto_on_open:
            return "skipped"

        from mira.platforms.mentions import mention_names

        names = mention_names(bot_name, bot_identity)
        issue = await provider.get_issue(ref, number)
        if issue is None:
            logger.info("Issue %s/%s#%s not found; no plan", owner, repo, number)
            return "missing"
        reason = skip_reason(issue, cfg, names, explicit=explicit)
        if reason:
            logger.info("No plan for %s/%s#%s: %s", owner, repo, number, reason)
            if explicit:
                await _reply(provider, ref, actor, f"I did not plan this issue: {reason}.")
            return "skipped"

        title = issue.title[:MAX_TITLE_CHARS]
        body = (issue.body or "")[:MAX_BODY_CHARS]
        keywords = extract_keywords(title, body)
        mentions = mentioned_paths(f"{title}\n{body}")
        candidates = await find_candidates(
            provider,
            ref,
            keywords,
            mentions,
            min(cfg.max_files * 3, 40),
            fetcher=fetcher,
        )

        if llm is None:
            from mira.dashboard.models_config import llm_config_for
            from mira.llm import create_llm

            llm = create_llm(llm_config_for("review", config.llm))
        messages = build_plan_prompt(
            issue, candidates, repository=f"{owner}/{repo}", max_files=cfg.max_files
        )
        plan = await llm.generate_object(
            messages,
            IssuePlan,
            name="submit_issue_plan",
            description="Submit the implementation plan for the issue.",
            temperature=0.2,
        )
        files = vet_files(plan, candidates, cfg.max_files)
        rendered = render_plan(
            plan,
            files,
            source=candidates.source,
            bot_name=bot_name,
            requested_by=actor if explicit else "",
        )
        outcome = await upsert_plan_comment(provider, ref, rendered)
        logger.info(
            "Issue plan %s on %s/%s#%s (%d file(s), from %s)",
            outcome,
            owner,
            repo,
            number,
            len(files),
            candidates.source or "nothing",
        )
        return outcome
    except Exception as exc:  # noqa: BLE001
        logger.warning("Could not plan issue %s/%s#%s: %s", owner, repo, number, exc)
        if explicit:
            await _reply(provider, ref, actor, "I could not write a plan just now.")
        return "failed"
