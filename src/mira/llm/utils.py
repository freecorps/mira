"""Shared utilities for LLM output processing."""

from __future__ import annotations

import json
import re

# Match a full <think>…</think> block. MiniMax has been seen to close with
# either </think> or </thinking>, so accept both.
_THINK_RE = re.compile(r"<think>.*?</think(?:ing)?>", re.DOTALL)


def strip_think_blocks(text: str | None) -> str:
    """Remove <think>… reasoning blocks from model output.

    Some models (e.g. MiniMax) output <think>… blocks as part of their
    thinking process before the actual response. These must be stripped
    before JSON parsing.
    """
    if not text:
        return ""
    result = _THINK_RE.sub("", text).strip()
    try:
        idx = next(i for i, c in enumerate(result) if c in "{[")
        obj, _ = json.JSONDecoder().raw_decode(result[idx:])
        return json.dumps(obj)
    except (StopIteration, json.JSONDecodeError):
        return result


def strip_code_fences(text: str | None) -> str:
    """Remove markdown code fences wrapping JSON.

    Handles ``None`` input, leading text before the opening fence
    (e.g. LLM analysis preamble), and trailing text after the closing fence.

    When the response contains multiple code blocks (e.g. ``python`` snippets
    in an analysis section followed by a ``json`` result block), only the
    explicitly-tagged ``json`` block is extracted.
    """
    if not text:
        return ""
    text = text.strip()
    # Prefer an explicitly-tagged ```json block anywhere in the response,
    # so we skip unrelated code blocks (```python, etc.) in LLM analysis.
    # Note: re.search scans the entire text, which may be slower for very large
    # responses, but is acceptable for typical LLM output sizes.
    json_match = re.search(r"```json\s*\n?(.*?)\n?\s*```", text, re.DOTALL)
    if json_match:
        return json_match.group(1).strip()
    # Fall back to a generic code fence at the start of the response
    match = re.match(r"^```\s*\n?(.*?)\n?\s*```", text, re.DOTALL)
    return match.group(1).strip() if match else text


# Anthropic-style tool-call XML delimiters some models leak into the JSON
# arguments string (seen on Haiku via OpenRouter), e.g. a valid object
# followed by ``</parameter></invoke>``. We cut the response at the first such
# tag, then re-balance braces.
_TOOL_XML_TAGS = ("</parameter>", "</invoke>", "</function_calls>", "<parameter", "<invoke")


def _balance_json(text: str) -> str:
    """Close any unclosed strings/brackets so a truncated object parses."""
    stack: list[str] = []
    in_string = False
    escape = False
    for ch in text:
        if in_string:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch in "{[":
            stack.append(ch)
        elif (ch == "}" and stack and stack[-1] == "{") or (
            ch == "]" and stack and stack[-1] == "["
        ):
            stack.pop()
    out = text.rstrip().rstrip(",").rstrip()
    if in_string:
        out += '"'
    closers = {"{": "}", "[": "]"}
    return out + "".join(closers[c] for c in reversed(stack))


def _repair_json(text: str) -> str:
    """Best-effort repair: drop leaked tool-call XML, then re-balance brackets."""
    cut = min((i for i in (text.find(t) for t in _TOOL_XML_TAGS) if i != -1), default=-1)
    if cut != -1:
        text = text[:cut]
    return _balance_json(text)


def loads_lenient(text: str) -> object | None:
    """Parse JSON, repairing leaked tool-call XML / missing braces. None on failure."""
    for candidate in (text, _repair_json(text)):
        try:
            return json.loads(candidate, strict=False)
        except (json.JSONDecodeError, TypeError):
            continue
    return None
