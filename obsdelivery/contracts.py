"""Independent reader contract: validation and canonical identity."""

from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction

from .canonical import digest, frac_str, parse_fraction
from .errors import ValidationError

_DECODER = "coefficient/10^scale; nearest-integer coefficient, ties-to-even"


@dataclass(frozen=True)
class ReaderContract:
    allowed_scales: tuple[int, ...]
    max_abs_coefficient: int
    max_abs_error: Fraction
    rounding: str = "half_even"

    def canonical(self) -> dict:
        return {
            "allowed_scales": list(self.allowed_scales),
            "max_abs_coefficient": self.max_abs_coefficient,
            "max_abs_error": self.max_abs_error,
            "rounding": self.rounding,
            "decoder": _DECODER,
        }

    def contract_id(self) -> str:
        return digest(self.canonical())

    @staticmethod
    def from_mapping(obj) -> "ReaderContract":
        if not isinstance(obj, dict):
            raise ValidationError("reader_contract must be an object")
        allowed = obj.get("allowed_scales")
        if not isinstance(allowed, (list, tuple)) or not allowed:
            raise ValidationError("allowed_scales must be a non-empty list")
        scales = []
        for item in allowed:
            if isinstance(item, bool) or not isinstance(item, int):
                raise ValidationError(f"allowed_scales entry {item!r} is not an integer")
            if not 0 <= item <= 9:
                raise ValidationError(f"allowed_scales entry {item} outside 0..9")
            scales.append(item)
        if len(set(scales)) != len(scales):
            raise ValidationError("allowed_scales contains duplicates")
        coef = obj.get("max_abs_coefficient")
        if isinstance(coef, bool) or not isinstance(coef, int) or coef <= 0:
            raise ValidationError("max_abs_coefficient must be a positive integer")
        try:
            budget = parse_fraction(obj.get("max_abs_error"), field="max_abs_error")
        except (ValueError, TypeError) as exc:
            raise ValidationError(str(exc)) from exc
        if budget < 0:
            raise ValidationError("max_abs_error must be non-negative")
        rounding = obj.get("rounding", "half_even")
        if rounding != "half_even":
            raise ValidationError("only half_even rounding is supported by this reader")
        return ReaderContract(tuple(scales), coef, budget, rounding)

    @staticmethod
    def from_fields(allowed_scales, max_abs_coefficient, max_abs_error, rounding="half_even"):
        if isinstance(max_abs_error, int) and not isinstance(max_abs_error, bool):
            error_text = str(max_abs_error)
        else:
            error_text = max_abs_error if isinstance(max_abs_error, str) else frac_str(Fraction(max_abs_error))
        return ReaderContract.from_mapping(
            {
                "allowed_scales": list(allowed_scales),
                "max_abs_coefficient": max_abs_coefficient,
                "max_abs_error": error_text,
                "rounding": rounding,
            }
        )
