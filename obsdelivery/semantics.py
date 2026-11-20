"""Binary32 interpretation and reference judging (pure, independent of storage).

Bit layout (the task's 32-bit format)::

    bit 31        sign
    bits 30..23   exponent (8 bits)
    bits 22..0    fraction (23 bits)

    exponent == 0 : fraction == 0 -> signed zero (value 0, sign preserved)
                    otherwise    -> (-1)^sign * fraction * 2**-149
    1 <= exponent <= 254        -> (-1)^sign * (2**23 + fraction) * 2**(exponent-150)
    exponent == 255 : fraction == 0 -> signed infinity
                      otherwise    -> NaN (23-bit payload = fraction bits)

No host floating point is involved: the exact value is a ``Fraction``.
"""

from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction
from typing import Any

FINITE = "FINITE"
ZERO = "ZERO"
INFINITY = "INFINITY"
NAN = "NAN"
NONFINITE_KINDS = (INFINITY, NAN)

PASS = "PASS"
OUTSIDE = "OUTSIDE"
NONFINITE = "NONFINITE"
MISSING = "MISSING"

_BITS_RE = frozenset("0123456789abcdefABCDEF")


def parse_bits(value: Any) -> int:
    """Validate an 8-hex-digit unsigned 32-bit original and return it."""
    if not isinstance(value, str):
        raise ValueError(f"bits must be an 8-hex-digit string, got {value!r}")
    if len(value) != 8 or not set(value) <= _BITS_RE:
        raise ValueError(f"bits must be exactly 8 hex digits, got {value!r}")
    return int(value, 16)


def format_bits(raw: int) -> str:
    return f"{raw:08x}"


@dataclass(frozen=True)
class Decoded:
    raw: int
    bits: str
    raw_sign: int
    exponent: int
    fraction: int
    kind: str  # FINITE | ZERO | INFINITY | NAN
    sign: int
    payload: int | None  # NaN fraction payload, else None
    value: Fraction | None  # exact value, None iff non-finite

    @property
    def finite(self) -> bool:
        return self.value is not None

    def canonical(self) -> dict[str, Any]:
        """Immutable canonical observation fields (sign-inclusive)."""
        # ~x is the exact value with the sign bit flipped, including signed
        # zero: ~+0 is -0 (which is what makes negative zero representable).
        negated = None if self.value is None else -self.value
        if self.kind == NAN:
            return {
                "kind": NAN,
                "bits": self.bits,
                "sign": self.sign,
                "payload": self.payload,
                "bits_negated": False,
                "value": None,
                "negated_value": None,
            }
        return {
            "kind": self.kind,
            "bits": self.bits,
            "sign": self.sign,
            "payload": None,
            "bits_negated": self.sign == 1,
            "value": self.value,
            "negated_value": negated,
        }


def decode(raw: int) -> Decoded:
    sign = (raw >> 31) & 1
    exponent = (raw >> 23) & 0xFF
    fraction = raw & 0x7FFFFF
    bits = format_bits(raw)
    if exponent == 0:
        if fraction == 0:
            return Decoded(raw, bits, sign, exponent, fraction, ZERO, sign, None, Fraction(0))
        value = Fraction(fraction * (-1 if sign else 1), 1 << 149)
        return Decoded(raw, bits, sign, exponent, fraction, FINITE, sign, None, value)
    if exponent == 255:
        if fraction == 0:
            return Decoded(raw, bits, sign, exponent, fraction, INFINITY, sign, None, None)
        return Decoded(raw, bits, sign, exponent, fraction, NAN, sign, fraction, None)
    mantissa = (1 << 23) | fraction
    scale = exponent - 150
    magnitude = Fraction(mantissa * (1 << scale), 1) if scale >= 0 else Fraction(mantissa, 1 << -scale)
    value = -magnitude if sign else magnitude
    return Decoded(raw, bits, sign, exponent, fraction, FINITE, sign, None, value)


def interpret(bits: Any) -> dict[str, Any]:
    """Public semantic interpretation of an original bit pattern."""
    decoded = decode(parse_bits(bits))
    result: dict[str, Any] = {
        "bits": decoded.bits,
        "kind": decoded.kind,
        "finite": decoded.finite,
        "sign": decoded.sign,
        "exponent": decoded.exponent,
        "fraction": decoded.fraction,
        "payload": decoded.payload,
    }
    if decoded.finite:
        result["value"] = decoded.value
        result["value_text"] = _frac_text(decoded.value)
        result["zero_sign"] = "negative" if (decoded.kind == ZERO and decoded.sign) else (
            "positive" if decoded.kind == ZERO else None
        )
    else:
        result["value"] = None
        result["value_text"] = None
        result["zero_sign"] = None
    return result


def _frac_text(value: Fraction) -> str:
    if value.denominator == 1:
        return str(value.numerator)
    return f"{value.numerator}/{value.denominator}"


def judge(decoded: Decoded, expected: Fraction, tolerance: Fraction) -> tuple[str, Fraction | None]:
    """Reference judgement on the ORIGINAL exact value.

    Returns ``(status, exact_abs_diff_or_None)``.  Non-finite values never
    compare numerically, so a NaN/Inf with an arbitrary reference is
    NONFINITE; a missing observation is MISSING and is handled by the caller.
    """
    if tolerance < 0:
        raise ValueError("tolerance must be non-negative")
    if not decoded.finite:
        return NONFINITE, None
    delta = decoded.value - expected
    distance = abs(delta)
    return (PASS if distance <= tolerance else OUTSIDE), distance
