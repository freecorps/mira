"""Writing the pull request's own description and title.

Everything else Mira writes lands *next to* the author's text: a comment, a
review, a status. This module writes *into* it, so it is built around one
promise — Mira only ever replaces text it owns.

**The description.** Mira's text lives between two HTML comments that render
as nothing::

    <!-- mira:summary:start -->
    ...
    <!-- mira:summary:end -->

A refresh replaces what is between them and nothing else. Without a section,
``section`` mode appends one, or puts it where the author wrote a
``@mira summary`` placeholder; ``empty_only`` mode writes one only into a body
with no human text. A body whose markers do not form exactly one well-ordered
pair is left alone entirely: a start marker whose end was deleted would
otherwise let the next refresh swallow everything the author wrote after it.

**The title.** Written only while it is the bot mention alone (``@mira``) —
the way an author asks for one — or, with ``title.mode: always``, on the first
review of the pull request. A title a human set after that is theirs.

**The cost.** The description reuses the walkthrough the review already
generated whenever that walkthrough covered the whole pull request. A
follow-up push is reviewed incrementally, so its walkthrough covers only the
new commits; refreshing the section then costs one walkthrough call over the
full diff. The title is one small structured call.

Every entry point is best-effort and never raises: a description that could
not be written is a log line, never a failed review.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import TYPE_CHECKING, Any

from jinja2 import Environment, FileSystemLoader
from pydantic import BaseModel, Field

from mira.autofix.redact import redact
from mira.config import MiraConfig, PRSummaryConfig
from mira.core.diff_parser import parse_diff
from mira.core.file_filter import filter_files
from mira.llm import untrusted
from mira.llm.prompts.review import build_walkthrough_prompt
from mira.llm.response_parser import convert_to_walkthrough_result, parse_walkthrough_response
from mira.models import FileDiff, PRInfo, WalkthroughResult

if TYPE_CHECKING:
    from mira.llm.base import LLMProviderProtocol

logger = logging.getLogger(__name__)

SUMMARY_START = "<!-- mira:summary:start -->"
SUMMARY_END = "<!-- mira:summary:end -->"

# GitHub truncates nothing below 256, but a title is read in lists and tab
# strips; past ~72 characters it stops being a title.
MAX_TITLE_LENGTH = 72

# The section's file table. A pull request touching 400 files does not need a
# 400-row table in its description; the walkthrough comment has the rest.
_MAX_FILE_ROWS = 40

# The description quoted to the title prompt. Enough to carry intent.
_MAX_DESCRIPTION_CHARS = 4_000

_TEMPLATE_ENV = Environment(
    loader=FileSystemLoader(str(Path(__file__).resolve().parents[1] / "llm/prompts/templates")),
    trim_blocks=True,
    lstrip_blocks=True,
)


class _GeneratedTitle(BaseModel):
    title: str = Field(description="The pull request title: one line, plain text.")


# ── Pure helpers ────────────────────────────────────────────────────────────


# What `_section_span` returns for markers that do not form one ordered pair.
_MALFORMED = (-1, -1)


def _section_span(body: str) -> tuple[int, int] | None:
    """Where Mira's section sits: ``(start, end)``, ``None`` for absent, or
    ``_MALFORMED`` when the markers are unpaired and the body must not be
    touched."""
    starts = body.count(SUMMARY_START)
    ends = body.count(SUMMARY_END)
    if starts == 0 and ends == 0:
        return None
    if starts != 1 or ends != 1:
        return _MALFORMED
    start = body.index(SUMMARY_START)
    end = body.index(SUMMARY_END)
    if end < start:
        return _MALFORMED
    return start, end + len(SUMMARY_END)


def strip_summary_section(body: str) -> str:
    """The body without Mira's section: what the author wrote.

    The review reads the description as the author's intent. Feeding it Mira's
    own summary from the previous push would be the model reading its own
    words back as a statement from the author.
    """
    body = body or ""
    span = _section_span(body)
    if span is None or span == _MALFORMED:
        return body
    return (body[: span[0]] + body[span[1] :]).strip()


def _placeholder(body: str, names: list[str]) -> re.Match[str] | None:
    for name in names:
        if not name:
            continue
        match = re.search(rf"@{re.escape(name)}[ \t]+summary\b", body, re.IGNORECASE)
        if match:
            return match
    return None


def is_mention_title(title: str, names: list[str]) -> bool:
    """Whether the title is the bot mention and nothing else (``@mira``).

    Case-insensitive, surrounding whitespace ignored, and either handle the
    bot answers to — the configured name or its platform identity.
    """
    normalized = (title or "").strip().lower()
    return bool(normalized) and any(normalized == f"@{n.lower()}" for n in names if n)


def should_generate_title(mode: str, title: str, names: list[str], *, first_review: bool) -> bool:
    """The only two cases Mira writes a title in.

    A bare mention is the author asking. ``always`` adds the first review, and
    only the first: from then on the title belongs to whoever edits it.
    """
    if mode == "off":
        return False
    if is_mention_title(title, names):
        return True
    return mode == "always" and first_review


def _neutralize_mentions(text: str, names: list[str]) -> str:
    """Generated text never addresses the bot.

    The walkthrough is written from the pull request, and the pull request can
    contain ``@mira ignore``. Copied into the description, that sentence would
    turn the next push's review off — so Mira's text drops the ``@`` from its
    own handles, and its section can never hold a command.
    """
    for name in names:
        if name:
            text = re.sub(rf"@({re.escape(name)})\b", r"\1", text, flags=re.IGNORECASE)
    return text


def _clean_generated(text: str, names: list[str]) -> str:
    text = (text or "").replace(SUMMARY_START, "").replace(SUMMARY_END, "")
    return _neutralize_mentions(text, names).strip()


def _cell(text: str) -> str:
    return " ".join((text or "").split()).replace("|", "\\|")


def render_section_content(walkthrough: WalkthroughResult, names: list[str]) -> str:
    """The markdown between the markers. Empty when there is nothing to say."""
    summary = _clean_generated(walkthrough.summary, names)
    if not summary:
        return ""
    parts = ["### Summary", "", summary]
    files = walkthrough.file_changes
    if files:
        parts += [
            "",
            "<details>",
            f"<summary>Changes ({len(files)} file{'s' if len(files) != 1 else ''})</summary>",
            "",
            "| File | Change |",
            "|---|---|",
        ]
        for entry in files[:_MAX_FILE_ROWS]:
            path = _cell(entry.path).replace("`", "")
            parts.append(f"| `{path}` | {_cell(_clean_generated(entry.description, names))} |")
        if len(files) > _MAX_FILE_ROWS:
            parts.append(f"| … | {len(files) - _MAX_FILE_ROWS} more in the walkthrough comment |")
        parts += ["", "</details>"]
    parts += [
        "",
        "<sub>Written by Mira from the diff and refreshed on new pushes. "
        "Text inside this section is replaced; write outside it.</sub>",
    ]
    return "\n".join(parts)


def apply_description(
    body: str,
    content: str,
    names: list[str],
    *,
    mode: str,
    force: bool = False,
) -> str | None:
    """The new body, or ``None`` when the body should be left as it is.

    ``force`` is an explicit ``@mira describe``: it writes a section even in
    ``empty_only`` mode, still without touching anything outside it.
    """
    original = body or ""
    if not content.strip():
        return None
    section = f"{SUMMARY_START}\n{content.strip()}\n{SUMMARY_END}"
    span = _section_span(original)
    if span == _MALFORMED:
        logger.info("PR body has unpaired Mira summary markers; leaving it untouched")
        return None
    without = original if span is None else original[: span[0]] + original[span[1] :]
    placeholder = _placeholder(without, names)

    if placeholder is not None:
        # The author said where the summary goes. An older section elsewhere
        # moves there rather than appearing twice.
        new = without[: placeholder.start()] + section + without[placeholder.end() :]
    elif span is not None:
        if mode == "empty_only" and without.strip() and not force:
            # The author has taken the description over.
            return None
        new = original[: span[0]] + section + original[span[1] :]
    elif not original.strip():
        new = section
    elif mode == "section" or force:
        new = f"{original.rstrip()}\n\n{section}"
    else:
        return None
    return None if new == original else new


def sanitize_title(raw: str, names: list[str]) -> str:
    """One line, bounded, plain, and never mentioning anyone."""
    line = next((ln.strip() for ln in (raw or "").splitlines() if ln.strip()), "")
    # Markdown heading markers only: `#123 fix` and `#hashtag` keep their `#`.
    line = re.sub(r"^(?:#+\s+)+", "", line).strip()
    for _ in range(2):
        if len(line) >= 2 and line[0] == line[-1] and line[0] in "\"'`":
            line = line[1:-1].strip()
    for name in names:
        if name:
            line = re.sub(rf"@{re.escape(name)}\b", "", line, flags=re.IGNORECASE)
    # Any other handle loses its `@`: a title is no place to ping somebody.
    line = re.sub(r"@(?=\w)", "", line)
    line = " ".join(line.split()).rstrip(".").strip()
    if len(line) > MAX_TITLE_LENGTH:
        cut = line[:MAX_TITLE_LENGTH]
        if " " in cut[MAX_TITLE_LENGTH // 2 :]:
            cut = cut[: cut.rfind(" ")]
        line = cut.rstrip(" ,;:-")
    if is_mention_title(line, names):
        return ""
    return line


# ── Generation ──────────────────────────────────────────────────────────────


def _files(diff_text: str, config: MiraConfig) -> list[FileDiff]:
    return filter_files(parse_diff(diff_text or "").files, config.filter, cap_files=False)


async def generate_walkthrough(
    llm: LLMProviderProtocol,
    config: MiraConfig,
    diff_text: str,
    pr_info: PRInfo,
    names: list[str],
) -> WalkthroughResult | None:
    """A walkthrough over the whole pull request, for when the review's did not
    cover it (an incremental round, ``review-rest``, or ``@mira describe``)."""
    files = _files(diff_text, config)
    if not files:
        return None
    title = "" if is_mention_title(pr_info.title, names) else pr_info.title
    messages = build_walkthrough_prompt(
        files=files,
        config=config,
        pr_title=title,
        pr_description=strip_summary_section(pr_info.description),
    )
    raw = await llm.walkthrough(messages)
    return convert_to_walkthrough_result(parse_walkthrough_response(raw))


def build_title_prompt(
    files: list[FileDiff],
    *,
    summary: str,
    description: str,
) -> list[dict[str, str]]:
    listing = "\n".join(
        f"{f.change_type.value} {f.path} (+{f.added_lines}/-{f.deleted_lines})" for f in files[:60]
    )
    if len(files) > 60:
        listing += f"\n… and {len(files) - 60} more"
    template = _TEMPLATE_ENV.get_template("pr_title.jinja2")
    prompt = template.render(
        max_length=MAX_TITLE_LENGTH,
        files_block=untrusted.block("FILE", listing or "(no files)", redactor=redact),
        summary_block=untrusted.block("PR", summary, redactor=redact) if summary else "",
        description_block=(
            untrusted.block("PR", description[:_MAX_DESCRIPTION_CHARS], redactor=redact)
            if description.strip()
            else ""
        ),
    )
    return [
        {"role": "system", "content": prompt},
        {"role": "user", "content": "Write the title for this pull request."},
    ]


async def generate_title(
    llm: LLMProviderProtocol,
    config: MiraConfig,
    diff_text: str,
    pr_info: PRInfo,
    names: list[str],
    *,
    summary: str = "",
) -> str:
    """A sanitized title, or ``""`` when none could be produced."""
    files = _files(diff_text, config)
    if not files:
        return ""
    messages = build_title_prompt(
        files,
        summary=_clean_generated(summary, names),
        description=strip_summary_section(pr_info.description),
    )
    result = await llm.generate_object(
        messages,
        _GeneratedTitle,
        name="submit_pr_title",
        description="Submit the pull request title.",
        temperature=0.2,
    )
    return sanitize_title(result.title, names)


# ── Orchestration ───────────────────────────────────────────────────────────


def summary_config(config: Any) -> PRSummaryConfig | None:
    """The validated section, or None for a config that does not carry one."""
    cfg = getattr(config, "pr_summary", None)
    return cfg if isinstance(cfg, PRSummaryConfig) else None


def is_first_review(owner: str, repo: str, number: int, platform: str) -> bool:
    """Whether Mira has never finished a review of this pull request.

    Anything it cannot read answers False: the one mode that asks
    (``title.mode: always``) would otherwise overwrite a human's title on a
    database hiccup.
    """
    try:
        from mira.dashboard.api import _app_db

        sha = _app_db.get_last_reviewed_sha(owner, repo, number, platform=platform)
    except Exception as exc:  # noqa: BLE001
        logger.debug("Could not read review history for %s/%s#%s: %s", owner, repo, number, exc)
        return False
    return isinstance(sha, str) and not sha


def opted_out(body: str, names: list[str]) -> bool:
    """``@mira ignore`` in the description — the same test the webhooks apply."""
    return any(
        re.search(rf"@{re.escape(n)}[ \t]+ignore\b", body or "", re.IGNORECASE) for n in names if n
    )


async def update_pr_summary(
    provider: Any,
    pr_url: str,
    *,
    config: MiraConfig,
    llm: LLMProviderProtocol,
    names: list[str],
    first_review: bool,
    walkthrough: WalkthroughResult | None = None,
    diff_text: str | None = None,
    has_new_changes: bool = True,
    force: bool = False,
) -> dict[str, bool]:
    """Write the description section and/or the title. Never raises.

    ``walkthrough`` is the review's when it covered the whole pull request;
    otherwise one is generated here over ``diff_text`` (fetched when None).
    ``has_new_changes=False`` — a push the review found nothing new in — skips
    refreshing a section that already exists rather than paying to rewrite it.

    Returns which fields were written.
    """
    written = {"description": False, "title": False}
    cfg = summary_config(config)
    if cfg is None:
        return written
    try:
        # Read fresh: the author may have edited since the webhook fired, and
        # the update replaces the whole body.
        pr_info = await provider.get_pr_info(pr_url)
        body = pr_info.description or ""
        if opted_out(body, names):
            logger.info("PR %s opted out via ignore; not writing a summary", pr_url)
            return written

        want_description = (
            cfg.description.enabled
            and (
                force
                or has_new_changes
                or _section_span(body) is None
                or _placeholder(body, names) is not None
            )
            # Would any content be written at all? Asked with a stand-in so a
            # body Mira may not touch costs no walkthrough call.
            and apply_description(body, "\u2026", names, mode=cfg.description.mode, force=force)
            is not None
        )
        want_title = should_generate_title(
            cfg.title.mode, pr_info.title, names, first_review=first_review
        )
        if not want_description and not want_title:
            return written

        if diff_text is None and (want_title or walkthrough is None):
            diff_text = await provider.get_pr_diff(pr_info)

        new_body: str | None = None
        content = ""
        if want_description:
            if walkthrough is None:
                try:
                    walkthrough = await generate_walkthrough(
                        llm, config, diff_text or "", pr_info, names
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.warning("PR summary walkthrough failed for %s: %s", pr_url, exc)
            if walkthrough is not None:
                content = render_section_content(walkthrough, names)
                new_body = apply_description(
                    body, content, names, mode=cfg.description.mode, force=force
                )

        new_title: str | None = None
        if want_title:
            try:
                generated = await generate_title(
                    llm,
                    config,
                    diff_text or "",
                    pr_info,
                    names,
                    summary=walkthrough.summary if walkthrough else "",
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning("PR title generation failed for %s: %s", pr_url, exc)
                generated = ""
            if generated and generated != pr_info.title:
                new_title = generated

        if new_body is None and new_title is None:
            return written
        # Generating took seconds, and the update replaces the whole field.
        # Read once more so an edit made meanwhile is built on, not overwritten:
        # the section is re-applied to the fresh body, and a title somebody
        # changed in the meantime is theirs.
        latest = await provider.get_pr_info(pr_url)
        latest_body = latest.description or ""
        if new_body is not None and latest_body != body:
            if opted_out(latest_body, names):
                return written
            new_body = apply_description(
                latest_body, content, names, mode=cfg.description.mode, force=force
            )
        if new_title is not None and (latest.title or "") != (pr_info.title or ""):
            new_title = None
        if new_body is None and new_title is None:
            return written
        await provider.update_pr(latest, title=new_title, body=new_body)
        written["description"] = new_body is not None
        written["title"] = new_title is not None
        logger.info(
            "Updated %s on %s",
            " and ".join(k for k, v in written.items() if v),
            pr_url,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("Could not update the PR summary for %s: %s", pr_url, exc)
    return written
