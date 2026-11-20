"""Re-test endpoint computation (pure, storage-free).

A specimen's re-test endpoint is the exact median of the valid votes of its
pre-registered positions.  Each position votes with its current effective
version only; a retraction cancels that position's vote and a non-finite
value never participates.  Once the number of valid positions reaches the
contract quantity, the endpoint is the median of *all* valid positions; for
an even count the two middle values are averaged exactly.

Everything is exact (``Fraction``).  No rounding is ever applied, so two
different endpoints are never merged into one bucket downstream.
"""

from __future__ import annotations

from fractions import Fraction
from typing import Any, Mapping

ACTION_VALUE = "value"
ACTION_RETRACT = "retract"

REASON_MISSING = "missing"        # registered position never delivered
REASON_RETRACTED = "retracted"    # effective version is a retraction
REASON_NONFINITE = "nonfinite"    # effective version is NaN/Inf


def median(values) -> Fraction:
    """Exact median; an even count averages the two middle values exactly."""
    ordered = sorted(Fraction(v) for v in values)
    if not ordered:
        raise ValueError("median requires at least one value")
    mid = len(ordered) // 2
    if len(ordered) % 2 == 1:
        return ordered[mid]
    return (ordered[mid - 1] + ordered[mid]) / 2


def evaluate(
    specimen_id: str,
    positions: list[str],
    required_count: int,
    effective: Mapping[str, tuple[int, str, Any]],
) -> dict[str, Any]:
    """Endpoint status from the effective version of each registered position.

    ``effective`` maps a registered position to ``(revision, action, decoded)``
    where ``decoded`` is a ``semantics.Decoded`` for ``action == "value"`` and
    ``None`` for a retraction.  Positions absent from the map never delivered.

    The result carries exact ``Fraction`` values; callers stringify.
    """
    position_rows: list[dict[str, Any]] = []
    basis: list[dict[str, Any]] = []
    shortfall: list[dict[str, Any]] = []
    values: list[Fraction] = []
    for position in positions:
        entry = effective.get(position)
        if entry is None:
            position_rows.append(
                {
                    "position": position,
                    "revision": None,
                    "action": None,
                    "bits": None,
                    "kind": None,
                    "sign": None,
                    "value": None,
                    "participates": False,
                    "reason": REASON_MISSING,
                }
            )
            shortfall.append({"position": position, "reason": REASON_MISSING})
            continue
        revision, action, decoded = entry
        participates = False
        reason = None
        bits = kind = sign = value = None
        if action == ACTION_RETRACT:
            reason = REASON_RETRACTED
        else:
            bits = decoded.bits
            kind = decoded.kind
            sign = decoded.sign
            if decoded.finite:
                participates = True
                value = decoded.value
            else:
                reason = REASON_NONFINITE
        position_rows.append(
            {
                "position": position,
                "revision": revision,
                "action": action,
                "bits": bits,
                "kind": kind,
                "sign": sign,
                "value": value,
                "participates": participates,
                "reason": reason,
            }
        )
        if participates:
            values.append(value)
            basis.append(
                {
                    "position": position,
                    "revision": revision,
                    "bits": bits,
                    "sign": sign,
                    "value": value,
                }
            )
        else:
            shortfall.append({"position": position, "reason": reason})

    valid_count = len(values)
    reached = valid_count >= required_count
    return {
        "specimen_id": specimen_id,
        "required_count": required_count,
        "valid_count": valid_count,
        "reached": reached,
        "endpoint": median(values) if reached else None,
        "gap": 0 if reached else required_count - valid_count,
        "positions": position_rows,
        "basis": basis,
        "shortfall": shortfall,
    }
