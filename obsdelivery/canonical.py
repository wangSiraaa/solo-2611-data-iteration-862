"""Canonical JSON, stable hashing and rational-string helpers.

Nothing in here touches floating point.  ``Fraction`` is the only numeric
type used for values, tolerances and error budgets.
"""

from __future__ import annotations

import hashlib
import json
from fractions import Fraction
from typing import Any


def frac_str(value: Fraction | int) -> str:
    """Exact, locale-free decimal text for a rational (``"3/4"`` or ``"2"``)."""
    f = Fraction(value)
    if f.denominator == 1:
        return str(f.numerator)
    return f"{f.numerator}/{f.denominator}"


def parse_fraction(text: Any, *, field: str = "value") -> Fraction:
    """Parse ``"p/q"`` or ``"n"`` exactly; reject floats and empty text."""
    if isinstance(text, bool):
        raise ValueError(f"{field}: expected a rational, got {text!r}")
    if isinstance(text, int):
        return Fraction(text)
    if not isinstance(text, str):
        raise ValueError(f"{field}: expected a rational string, got {text!r}")
    raw = text.strip()
    if not raw:
        raise ValueError(f"{field}: empty rational")
    try:
        return Fraction(raw)
    except (ValueError, ZeroDivisionError) as exc:
        raise ValueError(f"{field}: bad rational {text!r}: {exc}") from exc


def _normalize_json(value: Any) -> Any:
    if isinstance(value, Fraction):
        return frac_str(value)
    if isinstance(value, dict):
        return {
            str(key): _normalize_json(item)
            for key, item in sorted(value.items(), key=lambda kv: str(kv[0]))
        }
    if isinstance(value, (list, tuple)):
        return [_normalize_json(item) for item in value]
    if isinstance(value, bool) or value is None or isinstance(value, str):
        return value
    if isinstance(value, int):
        return value
    raise TypeError(f"value of type {type(value).__name__} is not canonicalisable")


def canonical_json(value: Any) -> str:
    """Deterministic JSON: sorted keys, no whitespace, rationals as strings."""
    return json.dumps(
        _normalize_json(value), sort_keys=True, separators=(",", ":"), ensure_ascii=True
    )


def digest(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()
