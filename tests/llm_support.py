"""Test helpers for code that asks an LLM for structured output."""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from typing import Any

from pydantic import BaseModel


def object_from(payload: str | dict) -> Callable[..., Awaitable[BaseModel]]:
    """A ``generate_object`` side effect that answers with ``payload``.

    ``payload`` is validated into whichever schema the code under test asked
    for, as the real provider does, so a test fixture that drifts from the
    schema fails here rather than passing on a shape production would refuse.
    """
    text = payload if isinstance(payload, str) else json.dumps(payload)

    async def generate_object(messages: Any, schema: type[BaseModel], **_kwargs: Any) -> BaseModel:
        return schema.model_validate_json(text)

    return generate_object
