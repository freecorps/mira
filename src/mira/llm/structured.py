"""Structured output: one schema, enforced as hard as the endpoint allows.

A model asked for JSON can fail in two ways: it can fail to produce JSON at
all, and it can produce JSON of the wrong shape — a line number as a string,
a severity nobody offered, the array it was asked for wrapped in a string.
Tool calling and ``json_object`` mode only guard against the first; the
second used to reach the parsers, which either dropped the answer quietly or
failed the call without telling the model what was wrong.

This module holds the two pieces that close that gap:

* **The schema, in the form each strategy needs.** :func:`schema_for` turns a
  Pydantic model into a self-contained JSON Schema (no ``$ref``, no titles or
  defaults), which is what a tool definition or a prompt wants.
  :func:`strict_schema` turns any such schema into the subset OpenAI-style
  ``response_format: json_schema`` with ``strict: true`` accepts — every
  object closed, every property required, the optional ones nullable — so the
  endpoint can constrain decoding to it and the model cannot leave the shape.
  :func:`normalize` undoes the one change strict mode forces on the answer
  (nulls standing in for omitted fields) and unwraps arrays and objects a
  model sent as JSON strings, so the parsers downstream see the shapes they
  always did.

* **Validation that talks back.** :func:`validator_for` checks an answer
  against a Pydantic model and, when it does not fit, raises a
  :class:`~mira.exceptions.ToolCallFormatError` naming the fields. The
  provider's re-roll loop reads that list back to the model, which is what
  turns "try again" into "``comments.0.line`` must be an integer".
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

from pydantic import BaseModel, ValidationError

from mira.exceptions import ToolCallFormatError

# Keywords that describe a schema to people rather than constrain the value.
# Strict mode rejects ``default`` outright, and the rest are noise in a prompt.
_ANNOTATIONS = frozenset({"title", "default", "examples", "$schema"})

# Keywords strict mode has no grammar for. A schema using them is still
# offered as a tool or described in the prompt — just not enforced.
_UNSTRICTABLE = frozenset({"oneOf", "allOf", "not", "patternProperties", "if", "then", "else"})

# Value constraints strict implementations disagree about. OpenAI takes most
# of them, several gateways that also advertise ``strict`` reject the request
# over them, and a 400 there would cost the whole strategy for a keyword.
# They leave the strict schema and move into the description, where the model
# still reads them; the validator enforces them either way.
_CONSTRAINTS = {
    "minimum": ">= {}",
    "maximum": "<= {}",
    "exclusiveMinimum": "> {}",
    "exclusiveMaximum": "< {}",
    "multipleOf": "a multiple of {}",
    "minLength": "at least {} characters",
    "maxLength": "at most {} characters",
    "minItems": "at least {} items",
    "maxItems": "at most {} items",
    "pattern": "matching /{}/",
    "format": "in {} format",
}

# How many validation errors the correction names. A model that got twenty
# fields wrong needs to hear about the pattern, not read an inventory.
_MAX_ERRORS = 8

Validator = Callable[[str], None]


class UnstrictableSchema(ValueError):
    """The schema uses a construct ``strict: true`` cannot express."""


# ── Schema from a model ─────────────────────────────────────────────


def schema_for(model: type[BaseModel]) -> dict:
    """``model``'s JSON Schema, self-contained and free of annotations.

    Pydantic emits shared sub-models under ``$defs`` and points at them with
    ``$ref``. Every endpoint Mira talks to reads an inlined schema; not all of
    them resolve references (Gemini through a gateway is the usual holdout),
    and a smaller model reads a flat schema more reliably too.
    """
    raw = model.model_json_schema()
    return _clean(raw, raw.get("$defs") or {}, ())


def _clean(node: Any, defs: dict, seen: tuple[str, ...]) -> Any:
    """Inline ``$ref``s, drop annotations and collapse ``anyOf [X, null]``."""
    if not isinstance(node, dict):
        return node
    ref = node.get("$ref")
    if isinstance(ref, str) and ref.startswith("#/$defs/"):
        name = ref.rsplit("/", 1)[1]
        if name in seen:
            raise UnstrictableSchema(f"recursive schema {name!r} cannot be inlined")
        target = _clean(defs.get(name, {}), defs, (*seen, name))
        siblings = _clean({k: v for k, v in node.items() if k != "$ref"}, defs, seen)
        return {**target, **siblings}
    out: dict = {}
    for key, value in node.items():
        if key in _ANNOTATIONS or key in ("$defs", "definitions"):
            continue
        if key == "properties" and isinstance(value, dict):
            # Property *names* are data here: a field called ``title`` stays.
            out[key] = {name: _clean(sub, defs, seen) for name, sub in value.items()}
        elif key in ("items", "additionalProperties") and isinstance(value, dict):
            out[key] = _clean(value, defs, seen)
        elif key in ("anyOf", "oneOf", "allOf") and isinstance(value, list):
            out[key] = [_clean(sub, defs, seen) for sub in value]
        else:
            out[key] = value
    return _collapse_nullable(out)


def _collapse_nullable(node: dict) -> dict:
    """``anyOf: [{type: X, ...}, {type: null}]`` → ``{type: [X, null], ...}``.

    How Pydantic spells ``X | None``. The union form is valid, but a type list
    is what every strict implementation and every small model handles best.
    """
    options = node.get("anyOf")
    if not isinstance(options, list) or len(options) != 2:
        return node
    nulls = [o for o in options if o == {"type": "null"}]
    rest = [o for o in options if o != {"type": "null"}]
    if len(nulls) != 1 or len(rest) != 1:
        return node
    inner = rest[0]
    if not isinstance(inner.get("type"), str) or "anyOf" in inner:
        return node
    merged = {k: v for k, v in node.items() if k != "anyOf"}
    merged.update(inner)
    return _nullable(merged)


# ── Strict mode ─────────────────────────────────────────────────────


def strict_schema(schema: dict) -> dict:
    """``schema`` in the subset ``strict: true`` accepts.

    Every object gets ``additionalProperties: false`` and lists all of its
    properties as required; a property that was optional becomes nullable
    instead, since strict mode has no notion of "may be left out". Raises
    :class:`UnstrictableSchema` for constructs it cannot express — an open
    map (``additionalProperties`` with a schema), ``oneOf``, a recursive
    reference — so the caller can skip the strict strategy for that call.
    """
    cleaned = _clean(schema, schema.get("$defs") or {}, ())
    if not isinstance(cleaned, dict) or not _is_object(cleaned) or not cleaned.get("properties"):
        # ``{}`` says "anything"; strict mode needs an object it can close.
        raise UnstrictableSchema("strict mode needs an object with declared properties")
    return _strict(cleaned)


def _strict(node: Any) -> Any:
    if not isinstance(node, dict):
        return node
    bad = _UNSTRICTABLE.intersection(node)
    if bad:
        raise UnstrictableSchema(f"strict mode cannot express {sorted(bad)}")
    out = _constraints_to_description(node)
    if _is_object(out):
        extra = out.get("additionalProperties")
        if isinstance(extra, dict) or (not out.get("properties") and extra is not False):
            # A map, or an object whose keys are left to the model: closing
            # it would turn "any object" into "the empty object".
            raise UnstrictableSchema("strict mode cannot express an open map")
        props = out.get("properties") or {}
        required = set(out.get("required") or [])
        out["properties"] = {
            name: _strict(sub if name in required else _nullable(sub))
            for name, sub in props.items()
        }
        out["required"] = list(props)
        out["additionalProperties"] = False
    if isinstance(out.get("items"), dict):
        out["items"] = _strict(out["items"])
    if isinstance(out.get("anyOf"), list):
        out["anyOf"] = [_strict(sub) for sub in out["anyOf"]]
    return out


def _constraints_to_description(node: dict) -> dict:
    """``node`` without :data:`_CONSTRAINTS`, each restated in its description."""
    notes = [template.format(node[key]) for key, template in _CONSTRAINTS.items() if key in node]
    out = {k: v for k, v in node.items() if k not in _CONSTRAINTS}
    if notes:
        described = str(out.get("description") or "").strip()
        suffix = f"Must be {', '.join(notes)}."
        out["description"] = f"{described} {suffix}" if described else suffix
    return out


def _types(node: dict) -> list[str]:
    kind = node.get("type")
    if isinstance(kind, str):
        return [kind]
    if isinstance(kind, list):
        return [k for k in kind if isinstance(k, str)]
    return []


def _is_object(node: dict) -> bool:
    return "object" in _types(node) or ("properties" in node and "type" not in node)


def accepts_null(node: object) -> bool:
    """True when ``node`` allows ``null`` as a value."""
    if not isinstance(node, dict):
        return False
    if "null" in _types(node):
        return True
    if isinstance(node.get("enum"), list) and None in node["enum"]:
        return True
    return any(accepts_null(sub) for sub in node.get("anyOf") or [])


def _nullable(node: dict) -> dict:
    """``node`` widened to also accept ``null``."""
    if accepts_null(node):
        return node
    out = dict(node)
    kinds = _types(out)
    if kinds:
        out["type"] = [*kinds, "null"]
        if isinstance(out.get("enum"), list):
            out["enum"] = [*out["enum"], None]
        return out
    if isinstance(out.get("anyOf"), list):
        out["anyOf"] = [*out["anyOf"], {"type": "null"}]
        return out
    return {"anyOf": [out, {"type": "null"}]}


# ── Reading the answer back ─────────────────────────────────────────


def normalize(data: Any, schema: object) -> Any:
    """Bring a decoded answer back to the shape ``schema`` describes.

    Two repairs, both driven by the schema so a string field that happens to
    hold JSON is never touched:

    * a ``null`` for a property the schema neither requires nor allows to be
      null is dropped — that is strict mode's stand-in for "left out", and the
      parsers downstream were written for the key being absent;
    * an array or object that arrived as a JSON *string* where the schema asks
      for the real thing is decoded — a habit of several smaller models, which
      double-encode nested values in tool arguments.
    """
    if not isinstance(schema, dict):
        return data
    if isinstance(data, str) and ({"array", "object"} & set(_types(schema)) or _is_object(schema)):
        decoded = _decode(data)
        if isinstance(decoded, list if "array" in _types(schema) else dict):
            data = decoded
    if isinstance(data, dict):
        props = schema.get("properties")
        if not isinstance(props, dict):
            return data
        required = set(schema.get("required") or [])
        out = {}
        for key, value in data.items():
            sub = props.get(key)
            if value is None and key not in required and sub is not None and not accepts_null(sub):
                continue
            out[key] = normalize(value, sub) if sub is not None else value
        return out
    if isinstance(data, list) and isinstance(schema.get("items"), dict):
        return [normalize(item, schema["items"]) for item in data]
    return data


def _decode(text: str) -> object:
    stripped = text.strip()
    if not stripped.startswith(("[", "{")):
        return None
    try:
        return json.loads(stripped, strict=False)
    except (json.JSONDecodeError, TypeError):
        return None


def validation_errors(exc: ValidationError) -> str:
    """The first few errors as ``path: message``, for a log line and the model."""
    errors = exc.errors(include_url=False, include_input=False)
    lines = [
        f"{'.'.join(str(part) for part in err['loc']) or '(root)'}: {err['msg']}"
        for err in errors[:_MAX_ERRORS]
    ]
    if len(errors) > _MAX_ERRORS:
        lines.append(f"and {len(errors) - _MAX_ERRORS} more")
    return "; ".join(lines)


def validator_for(model: type[BaseModel], tool: str) -> Validator:
    """A check that raises :class:`ToolCallFormatError` when a reply misfits ``model``.

    Raised as a format error so the provider handles it exactly like a
    malformed tool call — a re-roll with a correction, then the fallback
    model, then JSON mode — with the correction naming the fields at fault.
    """

    def validate(payload: str) -> None:
        try:
            model.model_validate_json(payload)
        except ValidationError as exc:
            raise ToolCallFormatError(
                "invalid_structured_output", tool=tool, errors=validation_errors(exc)
            ) from exc

    return validate


def tool_for(
    model: type[BaseModel],
    name: str,
    description: str = "",
    parameters: dict | None = None,
) -> dict:
    """A function-tool definition whose arguments are ``model``.

    ``parameters`` overrides the schema derived from the model — for a
    hand-written one whose descriptions the prompt depends on.
    """
    function: dict = {"name": name, "parameters": parameters or schema_for(model)}
    if description or model.__doc__:
        function["description"] = description or " ".join((model.__doc__ or "").split())
    return {"type": "function", "function": function}
