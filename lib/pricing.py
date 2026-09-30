"""Current public API list-price estimates, never subscription spend or invoices."""

from dataclasses import dataclass
from math import isfinite


VERIFIED_ON = "2026-09-30"
OPENAI_SOURCE = "https://developers.openai.com/api/docs/pricing"
CLAUDE_SOURCE = "https://platform.claude.com/docs/en/about-claude/pricing"


@dataclass(frozen=True)
class Price:
    # USD per million tokens. None means the token class is not priced.
    input: float
    cached: float
    output: float
    cache_write: float | None = None
    threshold: int | None = None
    fast_multiplier: float | None = None


# Exact model IDs only. Model pages document the 272K OpenAI threshold:
# https://developers.openai.com/api/docs/models/{model}
OPENAI = {
    "gpt-5.3-codex": Price(1.75, 0.175, 14, fast_multiplier=2),
    "gpt-5.4": Price(2.5, 0.25, 15, threshold=272_000),
    "gpt-5.4-2026-03-05": Price(2.5, 0.25, 15, threshold=272_000),
    "gpt-6-astra": Price(10, 1, 50, 12.5, 272_000, 2),
    "gpt-6-sol": Price(2, 0.2, 10, 2.5, 272_000, 2),
    "gpt-6.1-sol": Price(2, 0.1, 10, 2.5, 272_000, 2),
    "gpt-6-luna": Price(0.1, 0.01, 0.5, 0.125, 272_000, 2),
}
# Claude cache_write is the documented five-minute rate. One-hour writes use
# 2x input. Older Sonnet long-context requests remain unpriced in this table.
CLAUDE = {
    "claude-sonnet-4-5": Price(3, 0.3, 15, 3.75, 200_000),
    "claude-sonnet-4-6": Price(3, 0.3, 15, 3.75),
    "claude-sonnet-5": Price(2, 0.2, 10, 2.5),
    "claude-sonnet-5-5": Price(2, 0.2, 10, 2.5),
    "claude-haiku-4-5": Price(1, 0.1, 5, 1.25),
    "claude-opus-4-5": Price(5, 0.5, 25, 6.25, 200_000),
    "claude-opus-4-6": Price(5, 0.5, 25, 6.25, fast_multiplier=1),
    "claude-opus-4-7": Price(5, 0.5, 25, 6.25),
    "claude-opus-4-8": Price(5, 0.5, 25, 6.25, fast_multiplier=2),
    "claude-opus-5": Price(5, 0.5, 25, 6.25, fast_multiplier=2),
    "claude-opus-5-5": Price(4, 0.2, 20, 5, fast_multiplier=2),
    "claude-fable-5-1": Price(10, 0.25, 50, 12.5),
}
# Explicit snapshot/alias pair from https://platform.claude.com/docs/en/models/overview
CLAUDE["claude-haiku-4-5-20251001"] = CLAUDE["claude-haiku-4-5"]
# https://platform.claude.com/docs/en/api/cli/completions/create
CLAUDE["claude-sonnet-4-5-20250929"] = CLAUDE["claude-sonnet-4-5"]
CLAUDE["claude-opus-4-5-20251101"] = CLAUDE["claude-opus-4-5"]


def amount(value) -> float | None:
    """Validate amounts from external client records, including a real zero."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        number = float(value)
    except OverflowError:
        return None
    return number if isfinite(number) and number >= 0 else None


def counts_known(*values) -> bool:
    return all(amount(value) is not None and int(value) == value for value in values)


def event_cost(event: dict, harness: str) -> tuple[float | None, str]:
    recorded = amount(event.get("cost"))
    if recorded is not None:
        return recorded, "recorded"
    estimate = _estimate(event, harness)
    return (estimate, "estimated") if estimate is not None else (None, "unavailable")


def _estimate(event: dict, harness: str) -> float | None:
    provider = event.get("provider")
    catalog = {"openai": OPENAI, "claude": CLAUDE}.get(provider)
    if catalog is None or not event.get("pricing_complete", True):
        return None
    model = event.get("model", "")
    if not isinstance(model, str):
        return None
    prefix = "openai/" if provider == "openai" else "anthropic/"
    if model.startswith(prefix):
        model = model[len(prefix):]
    price = catalog.get(model)
    if price is None:
        return None
    write = event.get("cache_write", 0)
    if provider == "openai" and price.cache_write is not None:
        # Newer OpenAI models charge writes separately. An older client that
        # omits that counter cannot establish the ordinary-input token count.
        # https://developers.openai.com/api/docs/guides/prompt-caching
        write = event.get("pricing_cache_write")
    elif harness == "codex" and event.get("pricing_cache_write") is not None:
        write = event["pricing_cache_write"]
    values = [event.get("input"), event.get("output"), event.get("cached", 0), write]
    if not counts_known(*values):
        return None
    input_tokens, output, cached, write = values
    if harness == "codex":
        # Codex input includes cached tokens; reasoning is already in output.
        if cached + write > input_tokens:
            return None
        context = event.get("pricing_context_input")
        input_tokens -= cached + write
    else:
        context = input_tokens + cached + write
    if harness == "opencode":
        reasoning = event.get("reasoning", 0)
        if not counts_known(reasoning):
            return None
        output += reasoning
    long_context = False
    if price.threshold is not None:
        if not counts_known(context):
            return None
        long_context = context > price.threshold
        if long_context and provider == "claude":
            return None
    tier = event.get("service_tier")
    speed = event.get("speed")
    # This is a current API-price comparison, not a bill: missing/auto lanes
    # use Standard reference rates; an explicit unsupported lane stays unknown.
    if tier not in (None, "standard", "default", "auto", "priority", "fast", "flex", "batch"):
        return None
    if speed not in (None, "standard", "fast"):
        return None
    multiplier = 1.0
    if speed == "fast" or tier in ("priority", "fast"):
        if price.fast_multiplier is None:
            return None
        multiplier = price.fast_multiplier
    elif tier in ("flex", "batch"):
        multiplier = 0.5
    if event.get("inference_geo") not in (None, "global"):
        return None
    write_cost = 0.0
    if write:
        if price.cache_write is None:
            return None
        if provider == "claude":
            short_write = event.get("cache_write_5m")
            long_write = event.get("cache_write_1h")
            if not counts_known(short_write, long_write) or short_write + long_write != write:
                return None
            write_cost = short_write * price.cache_write + long_write * price.input * 2
        else:
            write_cost = write * price.cache_write
    input_cost = input_tokens * price.input + cached * price.cached + write_cost
    output_cost = output * price.output
    if long_context:
        input_cost *= 2
        output_cost *= 1.5
    return amount((input_cost + output_cost) * multiplier / 1_000_000)


def summarize(costs) -> dict:
    entries = list(costs)
    known = [(value, source) for value, source in entries if value is not None]
    total = amount(sum(value for value, _ in known)) if known else None
    if total is None:
        known = []
    sources = {source for _, source in known}
    return {
        "cost": total,
        "currency": "USD",
        "cost_source": next(iter(sources)) if len(sources) == 1 else "mixed" if sources else "unavailable",
        "cost_coverage": {"priced_events": len(known), "total_events": len(entries)},
    }
