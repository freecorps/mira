"""Model registry — labels, capabilities, and pricing for known models.

Backed by ``models.json``. Adding a new model is a one-line registry entry
plus a release note; no other code needs to change. Entries supply the
dashboard's labels, per-purpose lists, Recommended badges, and cost-estimate
pricing, and serve as the dropdown fallback when the backend's live catalog
can't be fetched (see ``dashboard/model_catalog.py``). The registry does not
restrict selection — the dashboard accepts free-form ids, like ``mira.yaml``.

Operators can extend or override the bundled list at runtime by pointing
``MIRA_MODELS_JSON_PATH`` at their own ``models.json`` (e.g. a volume mount):
its entries are overlaid onto the bundled ones by model id, so you can add a
custom model (``deepseek/...``, a local endpoint, …) or tweak an existing one
without forking the package. A missing/invalid override file is ignored with a
warning.

**One id, two endpoints.** The registry is keyed by model id, and the same id
can be served by two subscription endpoints at different prices
(``deepseek-v4.1-flash`` on OpenCode Go and on OpenDesign Cloud). An entry
keyed ``<id>@<endpoint>`` belongs to that one provider profile: looked up with
that endpoint it wins over the plain ``<id>`` entry, and everywhere else it is
reported under its bare id with ``endpoint`` filled in, exactly as if it had
been written ``"endpoint": "<endpoint>"``. A plain entry keeps meaning what it
always meant, so no existing lookup changes. :func:`entries` is the iteration
that understands both forms; :func:`all_models` still returns the raw keys.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import lru_cache
from pathlib import Path

logger = logging.getLogger(__name__)

_BUNDLED_PATH = Path(__file__).parent / "models.json"
_OVERRIDE_ENV = "MIRA_MODELS_JSON_PATH"


def _read(path: Path) -> dict[str, dict]:
    """Parse a models.json file, dropping the leading ``_*`` doc keys."""
    raw = json.loads(path.read_text())
    return {k: v for k, v in raw.items() if not k.startswith("_")}


@lru_cache(maxsize=1)
def _load() -> dict[str, dict]:
    """Load the registry once per process.

    Starts from the bundled ``models.json``, then overlays the file at
    ``MIRA_MODELS_JSON_PATH`` (if set) by model id — user entries add new models
    or override bundled ones. The env var is read on first use (after process
    startup), so a container's environment is in effect.
    """
    models = _read(_BUNDLED_PATH)
    override = os.environ.get(_OVERRIDE_ENV)
    if override:
        path = Path(override)
        try:
            models = {**models, **_read(path)}
            logger.info("Loaded model overrides from %s (%s)", _OVERRIDE_ENV, path)
        except FileNotFoundError:
            logger.warning("%s=%s not found; using bundled models only", _OVERRIDE_ENV, path)
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning(
                "Failed to read %s=%s (%s); using bundled models only",
                _OVERRIDE_ENV,
                path,
                exc,
            )
    return models


# The separator between a model id and the endpoint an entry is scoped to.
# "@" never appears in an OpenRouter, Bedrock or bare id, while "/" does.
SCOPE = "@"

# Context and output limits an endpoint's live catalogue reported for models
# the registry has no entry of its own for there: ``{(endpoint, model):
# {"max_input_tokens": …, "max_output_tokens": …}}``. Filled by the model
# catalogue when it fetches a list (OpenDesign publishes its budgets there).
_live_limits: dict[tuple[str, str], dict[str, int]] = {}


def split_key(key: str) -> tuple[str, str]:
    """``"deepseek-v4.1-flash@open-design"`` → ``("deepseek-v4.1-flash", "open-design")``.

    A key with no scope comes back with an empty endpoint.
    """
    model_id, sep, endpoint = key.rpartition(SCOPE)
    if not sep or not model_id or not endpoint:
        return key, ""
    return model_id, endpoint


def all_models() -> dict[str, dict]:
    """Return the full registry as ``{key: info}``, keys as written.

    An endpoint-scoped entry appears under its ``<id>@<endpoint>`` key here;
    use :func:`entries` to iterate models by the id a request would carry.
    """
    return _load()


def entries() -> list[tuple[str, dict]]:
    """Every entry as ``(model_id, info)``, scoped ones under their bare id.

    A scoped entry's info carries ``endpoint`` (its scope), so code that
    filters by endpoint treats both spellings alike. The same id can appear
    twice, once per endpoint that serves it.
    """
    out: list[tuple[str, dict]] = []
    for key, info in _load().items():
        model_id, endpoint = split_key(key)
        out.append((model_id, {**info, "endpoint": endpoint} if endpoint else info))
    return out


def get(model_id: str, endpoint: str | None = None) -> dict | None:
    """Return the registry entry for ``model_id``, or None if unknown.

    ``endpoint`` names the provider profile the call goes to. An entry scoped
    to it (``<id>@<endpoint>``) wins; otherwise the plain entry is returned —
    which may be pinned to another endpoint, and says so in its own
    ``endpoint`` field — and an id that exists only scoped is found under its
    single scope. Limits the endpoint's live catalogue reported overlay what
    the entry says (or stand alone when there is no entry).
    """
    models = _load()
    if endpoint:
        scoped = models.get(f"{model_id}{SCOPE}{endpoint}")
        if scoped is not None:
            return {**scoped, "endpoint": endpoint}
    entry = models.get(model_id)
    if entry is None:
        for key, info in models.items():
            bare, scope = split_key(key)
            if scope and bare == model_id:
                entry = {**info, "endpoint": scope}
                break
    live = _live_limits.get((endpoint, model_id)) if endpoint else None
    if live:
        return {**(entry or {"label": model_id}), **live}
    return entry


def remember_limits(endpoint: str, model_id: str, *, max_input: int, max_output: int) -> None:
    """Record limits an endpoint's live catalogue reported for one of its models.

    Only for a model the registry has no entry of its own for there — none
    scoped to the endpoint, and no plain one that is unpinned or pinned to
    it: a curated entry is the authority, and the catalogue fills the gap for
    the dozens of models a subscription serves that nobody wrote down (or
    that the registry knows only from another endpoint, at its limits).
    """
    models = _load()
    if not endpoint or not model_id or f"{model_id}{SCOPE}{endpoint}" in models:
        return
    plain = models.get(model_id)
    if plain is not None and (plain.get("endpoint") or endpoint) == endpoint:
        return
    limits = {}
    if max_input > 0:
        limits["max_input_tokens"] = int(max_input)
    if max_output > 0:
        limits["max_output_tokens"] = int(max_output)
    if limits:
        _live_limits[(endpoint, model_id)] = limits


def is_supported(model_id: str, purpose: str | None = None) -> bool:
    """``True`` iff ``model_id`` is in the registry. If ``purpose`` is given,
    additionally require that the model is allowed for that purpose
    (``"indexing"`` or ``"review"``)."""
    info = get(model_id)
    if info is None:
        return False
    if purpose is None:
        return True
    return purpose in (info.get("purposes") or [])


def models_for_purpose(purpose: str) -> list[dict]:
    """Return all models allowed for ``purpose``, formatted for the
    dashboard dropdown: ``[{value, label, recommended}]``."""
    out: list[dict] = []
    seen: set[str] = set()
    for model_id, info in entries():
        if purpose not in (info.get("purposes") or []) or model_id in seen:
            continue
        seen.add(model_id)
        out.append(
            {
                "value": model_id,
                "label": info.get("label", model_id),
                "recommended": purpose in (info.get("recommended_for") or []),
            }
        )
    # Recommended first, then alphabetical.
    out.sort(key=lambda m: (not m["recommended"], m["label"].lower()))
    return out


def default_for_purpose(purpose: str) -> str | None:
    """The recommended model id for a purpose (first recommended, else first
    allowed). ``models_for_purpose`` is already sorted recommended-first."""
    opts = models_for_purpose(purpose)
    return opts[0]["value"] if opts else None


def max_output_tokens(model_id: str, default: int = 4096, endpoint: str | None = None) -> int:
    """Return ``max_output_tokens`` for the model, or ``default`` if unknown."""
    info = get(model_id, endpoint)
    if info is None:
        return default
    return int(info.get("max_output_tokens", default))


def pricing(model_id: str) -> tuple[float, float]:
    """Return ``(input_cost_per_1m, output_cost_per_1m)`` USD for the model.

    Falls back to Sonnet pricing for unknown models — and for any cost field a
    (possibly user-supplied) entry omits — so cost estimates aren't silently
    zero and a partial custom entry can't crash registry load.
    """
    info = get(model_id) or {}
    return (
        float(info.get("input_cost_per_1m", 3.00)),
        float(info.get("output_cost_per_1m", 15.00)),
    )


# ── Per-call cost estimates ─────────────────────────────────────────

# DeepSeek's published off-peak window (UTC), the default for an entry whose
# ``off_peak`` block does not name its own hours.
_OFF_PEAK_UTC = "16:30-00:30"

_PRICE_FIELDS = (
    "input_cost_per_1m",
    "output_cost_per_1m",
    "cache_read_cost_per_1m",
    "credit_multiplier",
)


@dataclass(frozen=True)
class Price:
    """What one million tokens of each kind cost on one endpoint, right now."""

    input: float
    output: float
    cache_read: float
    # Plan credits consumed per USD of usage value (OpenDesign's "Coding Plan
    # multiplier"). 1.0 where the provider has no such notion.
    credit_multiplier: float = 1.0
    off_peak: bool = False


@dataclass(frozen=True)
class Estimate:
    """A call's estimated cost. ``priced`` is False when the registry had no
    price for the model, in which case both figures are zero rather than a
    guess — a local model is free, and an unknown one is unknown."""

    cost_usd: float = 0.0
    credits: float = 0.0
    priced: bool = False


def _minutes(text: str) -> int:
    hours, _, minutes = text.strip().partition(":")
    return int(hours) * 60 + int(minutes or 0)


def in_window(hours_utc: str, at: float) -> bool:
    """Whether ``at`` falls in a ``"HH:MM-HH:MM"`` UTC window (which may wrap midnight)."""
    try:
        start_text, _, end_text = hours_utc.partition("-")
        start, end = _minutes(start_text), _minutes(end_text)
    except ValueError:
        return False
    now = datetime.fromtimestamp(at, UTC)
    minute = now.hour * 60 + now.minute
    if start <= end:
        return start <= minute < end
    return minute >= start or minute < end


def _number(value: object, default: float) -> float:
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default


def price(
    model_id: str, endpoint: str | None = None, *, at: float, prompt_tokens: int = 0
) -> Price | None:
    """The per-1M prices a call to ``model_id`` on ``endpoint`` pays at ``at``.

    None when the registry has no price for the model. Three optional layers
    sit over an entry's base prices, applied in this order:

    * ``long_context`` — ``{"above_tokens": N, …prices}``, for a model billed
      at a higher rate once the prompt passes N tokens;
    * ``off_peak`` — ``{"hours_utc": "16:30-00:30", …prices}``, the discounted
      rates inside that window (DeepSeek's, when no hours are given);
    * ``cache_read_cost_per_1m`` defaults to the input price, which is what a
      cached token costs on an endpoint that does not discount them.
    """
    info = get(model_id, endpoint)
    if not info or "input_cost_per_1m" not in info:
        return None
    fields = {key: info[key] for key in _PRICE_FIELDS if key in info}
    tier = info.get("long_context")
    if isinstance(tier, dict) and prompt_tokens > _number(tier.get("above_tokens"), 0) > 0:
        fields.update({key: tier[key] for key in _PRICE_FIELDS if key in tier})
    off_peak = info.get("off_peak")
    discounted = isinstance(off_peak, dict) and in_window(
        str(off_peak.get("hours_utc") or _OFF_PEAK_UTC), at
    )
    if discounted:
        assert isinstance(off_peak, dict)
        fields.update({key: off_peak[key] for key in _PRICE_FIELDS if key in off_peak})
    input_price = _number(fields.get("input_cost_per_1m"), 0.0)
    return Price(
        input=input_price,
        output=_number(fields.get("output_cost_per_1m"), 0.0),
        cache_read=_number(fields.get("cache_read_cost_per_1m"), input_price),
        credit_multiplier=_number(fields.get("credit_multiplier"), 1.0),
        off_peak=discounted,
    )


def estimate(
    model_id: str,
    endpoint: str | None,
    *,
    prompt_tokens: int,
    cached_tokens: int,
    completion_tokens: int,
    at: float,
) -> Estimate:
    """Estimated USD cost and plan credits of one call.

    ``prompt_tokens`` counts every input token, cached ones included (the
    OpenAI convention every parser here normalises to); the cached part is
    billed at the cache-read price and the rest at the input price.
    """
    rates = price(model_id, endpoint, at=at, prompt_tokens=prompt_tokens)
    if rates is None:
        return Estimate()
    cached = max(0, min(cached_tokens, prompt_tokens))
    cost = (
        (prompt_tokens - cached) * rates.input
        + cached * rates.cache_read
        + completion_tokens * rates.output
    ) / 1_000_000
    return Estimate(cost_usd=cost, credits=cost * rates.credit_multiplier, priced=True)
