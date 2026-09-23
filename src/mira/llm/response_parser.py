"""Parse and validate LLM JSON output."""

from __future__ import annotations

import json
import logging
import re

from pydantic import BaseModel, Field, ValidationError

from mira.core.context import extract_hunk_lines, strip_line_gutter
from mira.exceptions import ResponseParseError
from mira.llm.utils import loads_lenient, strip_code_fences, strip_think_blocks
from mira.models import (
    FileChangeType,
    FileDiff,
    ReviewComment,
    Severity,
    WalkthroughConfidenceScore,
    WalkthroughEffort,
    WalkthroughFileEntry,
    WalkthroughResult,
)

logger = logging.getLogger(__name__)


class LLMComment(BaseModel):
    path: str
    line: int
    end_line: int | None = None
    severity: str = "suggestion"
    category: str = "other"
    title: str = ""
    body: str = ""
    confidence: float = Field(default=0.5, ge=0.0, le=1.0)
    suggestion: str | None = None
    agent_prompt: str | None = None
    existing_code: str = ""


class LLMKeyIssue(BaseModel):
    issue: str = ""
    path: str = ""
    line: int = 0


class LLMMetadata(BaseModel):
    reviewed_files: int = 0
    skipped_reason: str | None = None


class LLMReviewResponse(BaseModel):
    comments: list[LLMComment] = Field(default_factory=list)
    key_issues: list[LLMKeyIssue] = Field(default_factory=list)
    summary: str = ""
    metadata: LLMMetadata = Field(default_factory=LLMMetadata)


def parse_llm_response(raw_text: str) -> LLMReviewResponse:
    """Parse raw LLM text output into a validated LLMReviewResponse."""
    cleaned = strip_think_blocks(raw_text)
    cleaned = strip_code_fences(cleaned)

    # strict=False tolerates raw newlines from models that double-encode the
    # comments array as a pretty-printed JSON string; the repair pass salvages
    # responses with leaked tool-call XML or a missing closing brace.
    data = loads_lenient(cleaned)
    if data is None:
        raise ResponseParseError("LLM response is not valid JSON (even after repair)")

    if not isinstance(data, dict):
        raise ResponseParseError(f"Expected JSON object, got {type(data).__name__}")

    data = _unstring_nested_json(data)

    try:
        return LLMReviewResponse.model_validate(data)
    except Exception as e:
        raise ResponseParseError(f"LLM response validation failed: {e}") from e


def _build_diff_line_ranges(files: list[FileDiff]) -> dict[str, list[tuple[int, int]]]:
    """Build a map of file path → list of (start, end) line ranges from diff hunks.

    These are the target-side line ranges that GitHub will accept for review
    comments. Lines outside these ranges will cause a 422 "line could not be
    resolved" error.
    """
    ranges: dict[str, list[tuple[int, int]]] = {}
    for f in files:
        file_ranges: list[tuple[int, int]] = []
        for hunk in f.hunks:
            start = hunk.target_start
            end = start + hunk.target_length - 1
            if end < start:
                end = start
            file_ranges.append((start, end))
        if file_ranges:
            ranges[f.path] = file_ranges
    return ranges


def _snap_to_diff(line: int, ranges: list[tuple[int, int]]) -> int | None:
    """Snap a line number to the nearest diff hunk range.

    Returns the closest valid line, or None if no range is within 5 lines.
    """
    best: int | None = None
    best_dist = 6  # max snap distance
    for start, end in ranges:
        for boundary in (start, end):
            dist = abs(line - boundary)
            if dist < best_dist:
                best_dist = dist
                best = boundary
    return best


_HUNK_HEADER_RE = re.compile(r"^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@")
_ELLIPSIS_LINES = {"...", "…", "// ...", "# ...", "/* ... */"}


def _norm(text: str) -> str:
    """One line with its whitespace collapsed — what a quote is compared on."""
    return " ".join(text.split())


def _hunk_views(file: FileDiff) -> tuple[list[tuple[int, str]], ...]:
    """The file's hunks as line sequences, each line with a new-file anchor.

    The *new* view is what the file reads after the change (context and added
    lines, at their own line numbers); the *old* view is what it read before
    (context and removed lines). A removed line has no new-file number, so it
    is anchored at the new-file line that took its place — the one a comment
    about the removal has to be posted on. The *mixed* view is every line in
    diff order, for a quote that spans both sides of a change.
    """
    new_view: list[tuple[int, str]] = []
    old_view: list[tuple[int, str]] = []
    mixed_view: list[tuple[int, str]] = []
    for hunk in file.hunks:
        new = hunk.target_start
        for line in hunk.content.rstrip("\n").split("\n"):
            line = line.rstrip("\r")
            header = _HUNK_HEADER_RE.match(line)
            if header:
                new = int(header.group(2))
                continue
            if line.startswith("\\"):
                continue
            if line.startswith("+"):
                entry = (new, _norm(line[1:]))
                new_view.append(entry)
                mixed_view.append(entry)
                new += 1
            elif line.startswith("-"):
                entry = (max(new, 1), _norm(line[1:]))
                old_view.append(entry)
                mixed_view.append(entry)
            else:
                entry = (new, _norm(line[1:] if line.startswith(" ") else line))
                new_view.append(entry)
                old_view.append(entry)
                mixed_view.append(entry)
                new += 1
    return new_view, old_view, mixed_view


def _match_at(view: list[tuple[int, str]], start: int, quote: list[str]) -> int | None:
    """Index of the last view line when ``quote`` matches from ``start``, else None.

    The first quoted line may be the tail of its line and the last one the
    head of its line — models quote ``foo(bar)`` out of ``x = foo(bar)`` — and
    the lines between must match whole. Blank lines on either side are skipped.
    """
    i = start
    for n, want in enumerate(quote):
        while i < len(view) and not view[i][1]:
            i += 1
        if i >= len(view):
            return None
        have = view[i][1]
        if len(quote) == 1:
            ok = want in have
        elif n == 0:
            ok = have.endswith(want)
        elif n == len(quote) - 1:
            ok = have.startswith(want)
        else:
            ok = have == want
        if not ok:
            return None
        i += 1
    return i - 1


def _locate_quote(quote: str, views: tuple) -> list[tuple[int, int]]:
    """Every ``(first, last)`` new-file line span where ``quote`` appears in the hunks."""
    lines = [_norm(line) for line in quote.splitlines()]
    lines = [line for line in lines if line]
    if not lines:
        return []
    spans: list[tuple[int, int]] = []
    if any(line in _ELLIPSIS_LINES for line in lines):
        # An elided quote ("first line / … / last line") can only be placed by
        # its first line: what the ellipsis stands for cannot be checked.
        lines = [line for line in lines if line not in _ELLIPSIS_LINES]
        if not lines:
            return []
        lines = lines[:1]
    for view in views:
        for start, (_line, text) in enumerate(view):
            if not text:
                continue
            end = _match_at(view, start, lines)
            if end is not None:
                spans.append((view[start][0], view[end][0]))
    # A context line is in every view; one place in the file is one span.
    return sorted(set(spans))


# A quote shorter than this says too little to overrule the model's own line:
# `x` or `}` is in the diff a dozen times.
_MIN_ANCHORING_QUOTE = 8


def _anchor(
    line: int, end_line: int | None, spans: list[tuple[int, int]], quote: str, in_diff: bool
) -> tuple[int, int | None]:
    """Where to file a comment, given where its quote was found.

    The model's line stands when the quote is there, and when the quote is
    too ambiguous to argue with a line that is itself in the diff. Otherwise
    the quote wins: its only occurrence when it has one, else the occurrence
    nearest the line the model gave.
    """
    if any(first <= line <= last for first, last in spans):
        return line, end_line
    distinctive = len(_norm(quote)) >= _MIN_ANCHORING_QUOTE
    if in_diff and (len(spans) > 1 or not distinctive):
        return line, end_line
    first, last = min(spans, key=lambda s: min(abs(line - s[0]), abs(line - s[1])))
    return first, (last if last > first else None)


def convert_to_review_comments(
    response: LLMReviewResponse,
    valid_paths: set[str] | None = None,
    diff_files: list[FileDiff] | None = None,
) -> list[ReviewComment]:
    """Convert LLM response comments to ReviewComment models.

    Filters out comments with hallucinated file paths if valid_paths is provided.
    When diff_files is given, validates existing_code against actual hunk content,
    checks for no-op suggestions, and ensures line numbers are within diff ranges.

    The quote is also what places the comment. A model that quoted the code
    right but counted its line wrong — the commonest slip of smaller models —
    has its comment moved onto the quoted lines rather than snapped to the
    nearest hunk edge or dropped. A quote is compared with its whitespace
    collapsed, and again without a copied line-number gutter or diff marker,
    before it is judged not to be in the diff.
    """
    hunk_index: dict[str, str] = (
        {f.path: extract_hunk_lines(f) for f in diff_files} if diff_files else {}
    )
    views: dict[str, tuple] = {f.path: _hunk_views(f) for f in diff_files} if diff_files else {}
    diff_ranges: dict[str, list[tuple[int, int]]] = (
        _build_diff_line_ranges(diff_files) if diff_files else {}
    )
    result: list[ReviewComment] = []

    for c in response.comments:
        if valid_paths is not None and c.path not in valid_paths:
            continue

        if c.line < 1:
            continue

        # Drop hallucinated citations (present existing_code that isn't in the diff),
        # and let a citation that is there fix the line it is filed on.
        quote = (c.existing_code or "").strip()
        if hunk_index and quote and c.path in views:
            spans = _locate_quote(quote, views[c.path]) or _locate_quote(
                strip_line_gutter(quote), views[c.path]
            )
            if not spans:
                continue
            in_diff = any(start <= c.line <= end for start, end in diff_ranges.get(c.path, []))
            line, end_line = _anchor(c.line, c.end_line, spans, quote, in_diff)
            if line != c.line:
                logger.debug(
                    "Re-anchored %s:%d to line %d, where its quoted code is", c.path, c.line, line
                )
            c.line, c.end_line = line, end_line

        if diff_ranges and c.path in diff_ranges:
            file_ranges = diff_ranges[c.path]
            if not any(start <= c.line <= end for start, end in file_ranges):
                snapped = _snap_to_diff(c.line, file_ranges)
                if snapped is not None:
                    c.line = snapped
                    c.end_line = None
                else:
                    continue

        if c.suggestion and not c.body.strip():
            continue

        suggestion = c.suggestion
        if suggestion and c.existing_code and _norm(suggestion) == _norm(c.existing_code):
            suggestion = None

        result.append(
            ReviewComment(
                path=c.path,
                line=c.line,
                end_line=c.end_line if c.end_line and c.end_line > c.line else None,
                severity=Severity.from_str(c.severity),
                category=c.category,
                title=c.title[:80] if c.title else "",
                body=c.body,
                confidence=c.confidence,
                suggestion=suggestion,
                agent_prompt=c.agent_prompt,
                existing_code=c.existing_code,
            )
        )

    return result


class LLMWalkthroughFileChange(BaseModel):
    path: str
    change_type: str = "modified"
    description: str = ""


class LLMWalkthroughChangeGroup(BaseModel):
    label: str
    files: list[LLMWalkthroughFileChange] = Field(default_factory=list)


class LLMWalkthroughEffort(BaseModel):
    level: int = 3
    label: str = "Moderate"
    minutes: int = 15


class LLMWalkthroughConfidenceScore(BaseModel):
    score: int = 3
    label: str = ""
    reason: str = ""


class LLMWalkthroughResponse(BaseModel):
    summary: str = ""
    change_groups: list[LLMWalkthroughChangeGroup] = Field(default_factory=list)
    effort: LLMWalkthroughEffort | None = None
    confidence_score: LLMWalkthroughConfidenceScore | None = None
    sequence_diagram: str | None = None


_CHANGE_TYPE_MAP: dict[str, FileChangeType] = {
    "added": FileChangeType.ADDED,
    "modified": FileChangeType.MODIFIED,
    "deleted": FileChangeType.DELETED,
    "renamed": FileChangeType.RENAMED,
}


def _unstring_nested_json(data: dict) -> dict:
    """Recursively parse string values that are valid JSON objects/arrays.

    Some models (via tool calling) double-encode nested objects as JSON
    strings — for example returning the ``comments`` array as
    ``"[{...}, {...}]"`` instead of a real list. We try strict JSON first,
    then ``strict=False`` to tolerate raw control chars (newlines inside
    strings).
    """
    result = {}
    for key, value in data.items():
        if isinstance(value, str):
            stripped = value.strip()
            if stripped.startswith(("[", "{")):
                parsed = _try_load_json(stripped)
                if isinstance(parsed, (dict, list)):
                    value = parsed
        if isinstance(value, dict):
            value = _unstring_nested_json(value)
        elif isinstance(value, list):
            value = [
                _unstring_nested_json(item) if isinstance(item, dict) else item for item in value
            ]
        result[key] = value
    return result


def _try_load_json(text: str) -> object | None:
    """Best-effort parse: strict, then lenient (control chars allowed)."""
    try:
        return json.loads(text)
    except (json.JSONDecodeError, TypeError):
        pass
    try:
        return json.loads(text, strict=False)
    except (json.JSONDecodeError, TypeError):
        return None


_ARG_KEY_VALUE_RE = re.compile(
    r"<arg_key>\s*([^<]+?)\s*</arg_key>\s*<arg_value>\s*(.*?)\s*</arg_value>",
    re.S,
)

# Nested object fields occasionally arrive as leaked tool-arg XML fragments
# instead of objects (``"effort": "level</arg_key><arg_value>2"``). Rebuild
# what we can; drop the field otherwise — both are optional with defaults,
# so one bad field shouldn't skip the whole walkthrough (issue #162).
_WALKTHROUGH_FIELD_MODELS = {
    "effort": LLMWalkthroughEffort,
    "confidence_score": LLMWalkthroughConfidenceScore,
}


def _coerce_xml_value(raw: str) -> object:
    """Coerce an XML-argument value string to a JSON-ish scalar."""
    if raw == "null":
        return None
    if raw in ("true", "false"):
        return raw == "true"
    try:
        return int(raw)
    except ValueError:
        pass
    try:
        return float(raw)
    except ValueError:
        pass
    return raw


def _salvage_arg_xml_fragment(text: str) -> dict | None:
    """Rebuild a dict from leaked Anthropic-style tool-arg XML.

    Some models emit nested-object fields as an ``<arg_key>k</arg_key>
    <arg_value>v</arg_value>`` fragment *inside* a quoted JSON string
    (observed on ``effort``/``confidence_score`` via OpenRouter), e.g.
    ``"effort": "level</arg_key><arg_value>2"``. The transport may drop the
    opening ``<arg_key>`` / trailing ``</arg_value>`` tags, so normalize before
    matching. Returns ``None`` when the string isn't a key/value fragment.
    """
    fragment = text.strip()
    if not fragment.startswith("<arg_key>"):
        fragment = "<arg_key>" + fragment
    if not fragment.endswith("</arg_value>"):
        fragment = fragment + "</arg_value>"
    pairs = _ARG_KEY_VALUE_RE.findall(fragment)
    if not pairs:
        return None
    result: dict = {}
    for key, raw in pairs:
        key = key.strip()
        if key in result:
            return None
        result[key] = _coerce_xml_value(raw.strip())
    return result


def _validate_change_groups(raw_groups: list) -> list[LLMWalkthroughChangeGroup]:
    """Validate change groups one at a time, skipping malformed entries.

    LLMs occasionally omit a required ``path`` or ``label`` on a single
    entry in a large diff; one bad entry shouldn't drop the whole
    walkthrough (issue #162).
    """
    groups: list[LLMWalkthroughChangeGroup] = []
    for item in raw_groups:
        if not isinstance(item, dict):
            logger.warning("Skipping malformed walkthrough change group: %r", item)
            continue
        raw_files = item.get("files")
        if isinstance(raw_files, list):
            files = []
            for f in raw_files:
                try:
                    files.append(LLMWalkthroughFileChange.model_validate(f))
                except Exception as exc:
                    logger.warning("Skipping malformed walkthrough file entry: %s", exc)
            try:
                group = LLMWalkthroughChangeGroup.model_validate({**item, "files": []})
                group.files = files
                groups.append(group)
            except Exception as exc:
                logger.warning("Skipping malformed walkthrough change group: %s", exc)
            continue
        try:
            groups.append(LLMWalkthroughChangeGroup.model_validate(item))
        except Exception as exc:
            logger.warning("Skipping malformed walkthrough change group: %s", exc)
    return groups


def parse_walkthrough_response(raw_text: str) -> LLMWalkthroughResponse:
    """Parse raw LLM text output into a validated LLMWalkthroughResponse."""
    cleaned = strip_think_blocks(raw_text)
    cleaned = strip_code_fences(cleaned)

    # Some models leak Anthropic-style tool-call XML around/inside the JSON
    # arguments (e.g. a trailing ``</parameter></invoke>``). Use the lenient
    # loader the review path uses so an external leak truncates cleanly instead
    # of killing the walkthrough.
    data = loads_lenient(cleaned)
    if data is None:
        raise ResponseParseError("Walkthrough response is not valid JSON")
    if not isinstance(data, dict):
        raise ResponseParseError(f"Expected JSON object, got {type(data).__name__}")

    data = _unstring_nested_json(data)

    # Rebuild any fields that arrived as leaked XML fragments (issue #162)
    for key, model in _WALKTHROUGH_FIELD_MODELS.items():
        value = data.get(key)
        if isinstance(value, dict):
            try:
                if hasattr(model, "model_validate"):
                    model.model_validate(value)
                elif hasattr(model, "parse_obj"):
                    model.parse_obj(value)
                else:
                    model(**value)
            except ValidationError as exc:
                logger.warning("Dropping malformed walkthrough %s field: %s", key, exc)
                data.pop(key, None)
            continue

        rebuilt = _salvage_arg_xml_fragment(value) if isinstance(value, str) else None
        if rebuilt is not None:
            try:
                if hasattr(model, "model_validate"):
                    model.model_validate(rebuilt)
                elif hasattr(model, "parse_obj"):
                    model.parse_obj(rebuilt)
                else:
                    model(**rebuilt)
            except ValidationError as exc:
                logger.warning("Dropping malformed walkthrough %s field: %s", key, exc)
                data.pop(key, None)
                continue
            data[key] = rebuilt
        elif value is not None:
            logger.warning("Dropping malformed walkthrough %s field: %r", key, value)
            data.pop(key)

    raw_groups = data.pop("change_groups", None)

    try:
        response = LLMWalkthroughResponse.model_validate(data)
    except Exception as e:
        logger.debug("Raw walkthrough response: %s", raw_text)
        raise ResponseParseError(f"Walkthrough response validation failed: {e}") from e

    if isinstance(raw_groups, list):
        response.change_groups = _validate_change_groups(raw_groups)
    return response


_MERMAID_LABEL_RE = re.compile(r"\[([^\[\]]+)\]")


def _sanitize_mermaid(diagram: str) -> str:
    """Repair nested-quote labels that break Mermaid's parser.

    LLMs occasionally produce ``engine["core/"engine.py""]`` even when
    the prompt explicitly forbids it. The nested ``"`` closes the label
    early and Mermaid bails. We rewrite each ``[...]`` group whose
    content has malformed quotes — strip every ``"`` inside, leave one
    pair around the cleaned text — and pass through well-formed groups
    untouched.
    """
    if not diagram or '"' not in diagram:
        return diagram

    def fix(match: re.Match[str]) -> str:
        content = match.group(1)
        if '"' not in content:
            return match.group(0)
        # Well-formed: starts and ends with ", no internal " marks.
        if content.startswith('"') and content.endswith('"') and content.count('"') == 2:
            return match.group(0)
        cleaned = content.replace('"', "").strip()
        return f'["{cleaned}"]'

    return _MERMAID_LABEL_RE.sub(fix, diagram)


def convert_to_walkthrough_result(response: LLMWalkthroughResponse) -> WalkthroughResult:
    """Convert an LLM walkthrough response to a WalkthroughResult model."""
    entries: list[WalkthroughFileEntry] = []
    for group in response.change_groups:
        for fc in group.files:
            change_type = _CHANGE_TYPE_MAP.get(fc.change_type.lower(), FileChangeType.MODIFIED)
            entries.append(
                WalkthroughFileEntry(
                    path=fc.path,
                    change_type=change_type,
                    description=fc.description,
                    group=group.label,
                )
            )
    effort: WalkthroughEffort | None = None
    if response.effort:
        effort = WalkthroughEffort(
            level=response.effort.level,
            label=response.effort.label,
            minutes=response.effort.minutes,
        )
    confidence_score: WalkthroughConfidenceScore | None = None
    if response.confidence_score:
        confidence_score = WalkthroughConfidenceScore(
            score=response.confidence_score.score,
            label=response.confidence_score.label,
            reason=response.confidence_score.reason,
        )
    return WalkthroughResult(
        summary=response.summary,
        file_changes=entries,
        effort=effort,
        confidence_score=confidence_score,
        sequence_diagram=_sanitize_mermaid(response.sequence_diagram)
        if response.sequence_diagram
        else None,
    )
