"""The built-in API price table, and what one logged request costs at it.

This is the panel's only price source: the server prices each request as it is read, so a
session's total, its parts, its cost line and its subagents all add up from the same numbers.
The figures are an estimate at OpenAI API list prices as recorded in this table (as of
``PRICES_AS_OF``); they are not a bill, and a subscription is not charged this way.

    cost = uncached × input + cached × cache read + write × cache write + output × output

``input_tokens`` is the whole prompt; cached reads and cache writes are parts of it.
Rates are USD per 1M tokens: (input, cache read, cache write, output). A missing write rate
bills writes as input; a missing Fast or long-context rate falls back to the one listed.

Long context is a per-model rule: a model priced that way names the prompt size above which
the long-context rates apply to the whole request (``long_above``); a model without it never
pays them. The rule is applied to each request's own input, never to a session's sum.
"""

from __future__ import annotations

from typing import Any, Dict, NamedTuple, Optional, Tuple

PRICES_AS_OF = "2026-10"

Rates = Tuple[float, float, Optional[float], float]

# model -> {"std", "fast"?, "long_above"?, "std_long"?, "fast_long"?}
TABLE: Dict[str, Dict[str, Any]] = {
    "gpt-6.1-sol": {"std": (2, 0.1, 2.5, 10), "fast": (4, 0.2, 5, 20),
                    "long_above": 272_000, "std_long": (4, 0.2, 5, 15), "fast_long": (8, 0.4, 10, 30)},
    "gpt-6-astra": {"std": (10, 1, 12.5, 50), "fast": (20, 2, 25, 100),
                    "long_above": 272_000, "std_long": (20, 2, 25, 75), "fast_long": (40, 4, 50, 150)},
    "gpt-6-sol": {"std": (2, 0.2, 2.5, 10), "fast": (4, 0.4, 5, 20),
                  "long_above": 272_000, "std_long": (4, 0.4, 5, 15), "fast_long": (8, 0.8, 10, 30)},
    "gpt-6-luna": {"std": (0.1, 0.01, 0.125, 0.5), "fast": (0.2, 0.02, 0.25, 1),
                   "long_above": 272_000, "std_long": (0.2, 0.02, 0.25, 0.75), "fast_long": (0.4, 0.04, 0.5, 1.5)},
    "gpt-5.6-sol": {"std": (4, 0.4, 5, 20), "fast": (8, 0.8, 10, 40),
                    "long_above": 272_000, "std_long": (8, 0.8, 10, 30), "fast_long": (16, 1.6, 20, 60)},
    "gpt-5.6-terra": {"std": (2, 0.2, 2.5, 12), "fast": (4, 0.4, 5, 24),
                      "long_above": 272_000, "std_long": (4, 0.4, 5, 18), "fast_long": (8, 0.8, 10, 36)},
    "gpt-5.6-luna": {"std": (0.2, 0.02, 0.25, 1.2), "fast": (0.4, 0.04, 0.5, 2.4),
                     "long_above": 272_000, "std_long": (0.4, 0.04, 0.5, 1.8), "fast_long": (0.8, 0.08, 1, 3.6)},
    "gpt-5.5": {"std": (5, 0.5, None, 30), "fast": (12.5, 1.25, None, 75),
                "long_above": 272_000, "std_long": (10, 1, None, 45)},
    "gpt-5.4": {"std": (2.5, 0.25, None, 15), "fast": (5, 0.5, None, 30),
                "long_above": 272_000, "std_long": (5, 0.5, None, 22.5)},
    "gpt-5.4-mini": {"std": (0.75, 0.075, None, 4.5), "fast": (1.5, 0.15, None, 9)},
    "gpt-5.4-nano": {"std": (0.2, 0.02, None, 1.25)},
    "gpt-5.3-codex": {"std": (1.75, 0.175, None, 14), "fast": (3.5, 0.35, None, 28)},
    "gpt-5.2": {"std": (1.75, 0.175, None, 14)},
    "gpt-5.1": {"std": (1.25, 0.125, None, 10)},
    "gpt-5": {"std": (1.25, 0.125, None, 10)},
    "gpt-5-mini": {"std": (0.25, 0.025, None, 2)},
    "gpt-5-nano": {"std": (0.05, 0.005, None, 0.4)},
}


class Rate(NamedTuple):
    input: float
    read: float
    write: float
    output: float
    fast: bool
    long: bool


def is_fast(tier: Optional[str]) -> bool:
    """"priority" is the tier OpenAI renamed Fast; anything else is Standard."""
    return tier in ("priority", "fast")


def model_entry(model: Optional[str]) -> Optional[Dict[str, Any]]:
    return TABLE.get(model.strip().lower()) if isinstance(model, str) else None


def is_long(model: Optional[str], input_tokens: Optional[int]) -> bool:
    m = model_entry(model)
    limit = m.get("long_above") if m else None
    return bool(limit and isinstance(input_tokens, int) and input_tokens > limit)


def rate_for(model: Optional[str], tier: Optional[str], input_tokens: Optional[int]) -> Optional[Rate]:
    m = model_entry(model)
    if m is None:
        return None
    fast = is_fast(tier) and "fast" in m
    long = is_long(model, input_tokens)
    if long:
        r = (m.get("fast_long") or m["fast"]) if fast else (m.get("std_long") or m["std"])
    else:
        r = m["fast"] if fast else m["std"]
    return Rate(r[0], r[1], r[0] if r[2] is None else r[2], r[3], fast, long)


def request_cost(model: Optional[str], tier: Optional[str], usage: Dict[str, int]) -> Optional[Dict[str, Any]]:
    """One request at its own rates: the USD cost of each part, or None when the model is not
    in the table. Two kinds of gap in the log are priced as they stand and flagged:

    - ``cacheMissing``: the cached-read count, or the cache-write count where this model prices
      writes apart from input, was not recorded. The request is priced as if none of its prompt
      was read from or written to the cache. That is an assumption, not a bound either way: a
      read would have cost less, a write (dearer than input on some models) more.
    - ``usageMissing``: the input or the output count was not recorded. Only the parts recorded
      are priced, so the cost covers part of the request.
    """
    rate = rate_for(model, tier, usage.get("input_tokens"))
    if rate is None:
        return None
    has_input = "input_tokens" in usage
    inp = max(0, usage.get("input_tokens") or 0)
    cached_raw = usage.get("cached_input_tokens")
    cached = min(max(0, cached_raw or 0), inp)
    write = min(max(0, usage.get("cache_write_input_tokens") or 0), inp - cached)
    out = max(0, usage.get("output_tokens") or 0)
    write_apart = rate.write != rate.input
    return {
        "read": cached * rate.read / 1e6,
        "write": write * rate.write / 1e6,
        "uncached": (inp - cached - write) * rate.input / 1e6,
        "output": out * rate.output / 1e6,
        "long": rate.long,
        "cacheMissing": has_input and (cached_raw is None or (write_apart and "cache_write_input_tokens" not in usage)),
        "usageMissing": not has_input or "output_tokens" not in usage,
    }


def public_rate(model: Optional[str], tier: Optional[str], input_tokens: Optional[int]) -> Optional[Dict[str, Any]]:
    r = rate_for(model, tier, input_tokens)
    if r is None:
        return None
    return {"input": r.input, "read": r.read, "write": r.write, "output": r.output, "fast": r.fast, "long": r.long}
