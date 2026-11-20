"""Exact decimal representation selection (pure, storage-free).

Given an exact finite ``Fraction`` and an independent reader contract, decide
whether a ``coefficient / 10**scale`` representation exists and pick one.

Rounding rule required by the contract: ``coefficient`` is the nearest integer
to ``value * 10**scale``; an exact tie is resolved to the even coefficient.

Feasibility for one scale ``s``:
    coef    = round_half_even(value * 10**s)
    |coef| <= max_abs_coefficient                  (coefficient bound)
    |coef/10**s - value| <= max_abs_error          (exact error budget)

Scale-selection policy: try allowed scales in ascending order and take the
first feasible scale.  ``10**s`` grows with ``s``, so the smallest feasible
scale yields the shortest coefficient (and, among circuits that are all
feasible, the smallest error).  This is a documented policy, not a claim of
optimality.  Crucially, *every* allowed scale is examined with exact rational
arithmetic before a record is refused.
"""

from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction


def round_half_even(value: Fraction, scale: int) -> int:
    """Exact nearest integer to ``value * 10**scale``, ties to even."""
    num = value.numerator * (10 ** scale)
    den = value.denominator
    q, r = divmod(num, den)  # den > 0 for a normalised Fraction
    twice = 2 * r
    if twice < den:
        return q
    if twice > den:
        return q + 1
    return q if q % 2 == 0 else q + 1


@dataclass(frozen=True)
class ScaleAttempt:
    scale: int
    coefficient: int
    error: Fraction
    coefficient_ok: bool
    error_ok: bool

    @property
    def feasible(self) -> bool:
        return self.coefficient_ok and self.error_ok

    def canonical(self) -> dict:
        return {
            "scale": self.scale,
            "coefficient": self.coefficient,
            "error": self.error,
            "coefficient_ok": self.coefficient_ok,
            "error_ok": self.error_ok,
            "feasible": self.feasible,
        }


@dataclass(frozen=True)
class Selection:
    value: Fraction
    allowed_scales: tuple[int, ...]
    max_abs_coefficient: int
    max_abs_error: Fraction
    attempts: tuple[ScaleAttempt, ...]
    chosen: ScaleAttempt | None

    @property
    def feasible(self) -> bool:
        return self.chosen is not None

    def evidence(self) -> dict:
        return {
            "value": self.value,
            "allowed_scales": list(self.allowed_scales),
            "max_abs_coefficient": self.max_abs_coefficient,
            "max_abs_error": self.max_abs_error,
            "policy": "ascending_scale_first_feasible",
            "chosen_scale": None if self.chosen is None else self.chosen.scale,
            "attempts": [attempt.canonical() for attempt in self.attempts],
        }


def select_representation(
    value: Fraction,
    allowed_scales: tuple[int, ...],
    max_abs_coefficient: int,
    max_abs_error: Fraction,
) -> Selection:
    """Examine every allowed scale and choose the first feasible one."""
    attempts: list[ScaleAttempt] = []
    chosen: ScaleAttempt | None = None
    for scale in allowed_scales:
        coefficient = round_half_even(value, scale)
        error = abs(Fraction(coefficient, 10 ** scale) - value)
        attempt = ScaleAttempt(
            scale=scale,
            coefficient=coefficient,
            error=error,
            coefficient_ok=abs(coefficient) <= max_abs_coefficient,
            error_ok=error <= max_abs_error,
        )
        attempts.append(attempt)
        if attempt.feasible and chosen is None:
            chosen = attempt
    return Selection(value, tuple(allowed_scales), max_abs_coefficient, max_abs_error, tuple(attempts), chosen)
