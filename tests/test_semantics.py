"""Exact tests for the token-level semantics and rounding rules."""

from fractions import Fraction

import pytest

from obsdelivery.semantics import (
    FINITE,
    INFINITY,
    NAN,
    NONFINITE,
    OUTSIDE,
    PASS,
    ZERO,
    decode,
    interpret,
    judge,
    parse_bits,
)
from obsdelivery.delivery_algo import round_half_even, select_representation
from obsdelivery.contracts import ReaderContract
from obsdelivery.errors import ValidationError


BIG = 1 << 149
assert BIG == 713623846352979940529142984724747568191373312


def test_spec_pinned_values():
    # 3dcccccd is the float32 nearest to 0.1, 1/671088640 above it.
    d = decode(parse_bits("3dcccccd"))
    assert d.kind == FINITE
    assert d.value == Fraction(13421773, 134217728)
    assert d.value - Fraction(1, 10) == Fraction(1, 671088640)
    assert d.sign == 0

    # negative zero
    z = decode(parse_bits("80000000"))
    assert z.kind == ZERO and z.sign == 1 and z.value == 0

    # NaN with payload 4194305
    n = decode(parse_bits("7fc00001"))
    assert n.kind == NAN and n.payload == 4194305 and n.sign == 0

    # +infinity
    i = decode(parse_bits("7f800000"))
    assert i.kind == INFINITY and i.value is None and i.sign == 0

    # smallest subnormal
    s = decode(parse_bits("00000001"))
    assert s.value == Fraction(1, BIG)

    # exact 1/8
    assert decode(parse_bits("3e000000")).value == Fraction(1, 8)


def test_negative_subnormal_and_negative_infinity():
    d = decode(parse_bits("80000001"))
    assert d.value == Fraction(-1, BIG) and d.sign == 1
    inf = decode(parse_bits("ff800000"))
    assert inf.kind == INFINITY and inf.sign == 1


def test_large_finite_value():
    d = decode(parse_bits("7f7fffff"))  # max finite
    assert d.value == Fraction((1 << 24) - 1) * Fraction(2) ** 104
    d2 = decode(parse_bits("00800000"))  # smallest normal
    assert d2.value == Fraction(1, 1 << 126)


def test_judge_statuses():
    s1 = decode(parse_bits("3dcccccd"))
    assert judge(s1, Fraction(1, 10), Fraction(0)) == (OUTSIDE, Fraction(1, 671088640))
    assert judge(s1, Fraction(1, 10), Fraction(1, 671088640))[0] == PASS
    # Strictly tighter than the actual gap must stay OUTSIDE.
    assert judge(s1, Fraction(1, 10), Fraction(1, 671088641))[0] == OUTSIDE
    # Any tolerance at or above the exact gap is PASS.
    assert judge(s1, Fraction(1, 10), Fraction(1, 10**5))[0] == PASS

    s2 = decode(parse_bits("80000000"))
    status, diff = judge(s2, Fraction(0), Fraction(0))
    assert status == PASS and diff == 0 and s2.sign == 1

    s5 = decode(parse_bits("00000001"))
    assert judge(s5, Fraction(0), Fraction(0)) == (OUTSIDE, Fraction(1, BIG))

    assert judge(decode(parse_bits("7fc00001")), Fraction(0), Fraction(0)) == (NONFINITE, None)
    assert judge(decode(parse_bits("7f800000")), Fraction(1), Fraction(0)) == (NONFINITE, None)


def test_signed_zero_distinctness_of_identity():
    p = decode(parse_bits("00000000"))
    n = decode(parse_bits("80000000"))
    assert p.value == n.value == 0
    assert p.sign == 0 and n.sign == 1
    assert p.bits != n.bits


def test_bits_validation():
    for bad in ["3dcccc", "3dcccccd0", "zzzzzzzz", "", None, 123]:
        with pytest.raises((ValueError, TypeError)):
            parse_bits(bad)


def test_round_half_even():
    # exact ties at integer boundaries
    assert round_half_even(Fraction(1, 2), 0) == 0
    assert round_half_even(Fraction(3, 2), 0) == 2
    assert round_half_even(Fraction(5, 2), 0) == 2
    assert round_half_even(Fraction(-1, 2), 0) == 0
    assert round_half_even(Fraction(-3, 2), 0) == -2
    assert round_half_even(Fraction(-5, 2), 0) == -2
    # 1/8 at scales 2 and 3
    assert round_half_even(Fraction(1, 8), 2) == 12   # 12.5 -> 12 (even)
    assert round_half_even(Fraction(1, 8), 3) == 125
    # 0.1f at scale 3: 13421773*1000/134217728 = 100.000000745...
    assert round_half_even(Fraction(13421773, 134217728), 3) == 100


def test_select_picks_another_scale_after_first_fails():
    # s1 = 1/8 under J1: scale 2 is feasible (12, error 1/200), picked first.
    sel = select_representation(Fraction(1, 8), (2, 3), 99, Fraction(1, 200))
    assert sel.feasible and sel.chosen.scale == 2 and sel.chosen.coefficient == 12
    assert sel.chosen.error == Fraction(1, 200)

    # Under J0 the scale-2 coefficient is fine but the error budget is zero,
    # so a naive "first scale only" would wrongly reject; scale 3 has zero
    # error but breaks the coefficient bound -> genuinely infeasible.
    sel0 = select_representation(Fraction(1, 8), (2, 3), 99, Fraction(0))
    assert not sel0.feasible
    assert [(a.scale, a.coefficient, a.error_ok, a.coefficient_ok) for a in sel0.attempts] == [
        (2, 12, False, True),
        (3, 125, True, False),
    ]

    # A case where scale 0 fails the error budget but scale 2 works must not be
    # rejected just because the first tried scale failed.
    sel2 = select_representation(Fraction(1, 8), (0, 2), 99, Fraction(1, 200))
    assert sel2.feasible and sel2.chosen.scale == 2 and sel2.chosen.coefficient == 12
    assert not sel2.attempts[0].feasible

    # A case where scale 0 fails the coefficient bound is impossible to repair
    # at a *larger* scale (10**s only grows), so a bound-driven refusal is
    # genuine; verify the exact reason.
    sel4 = select_representation(Fraction(1234, 1), (0, 2), 1233, Fraction(0))
    assert not sel4.feasible
    assert [a.coefficient_ok for a in sel4.attempts] == [False, False]

    # Ordering of allowed_scales as given is honoured for the policy choice.
    sel3 = select_representation(Fraction(1, 8), (3, 2), 999, Fraction(0))
    assert sel3.feasible and sel3.chosen.scale == 3


def test_contract_validation():
    c = ReaderContract.from_mapping(
        {"allowed_scales": [2, 3], "max_abs_coefficient": 99, "max_abs_error": "0"}
    )
    assert c.allowed_scales == (2, 3)
    assert c.max_abs_error == 0
    for bad in [
        {"allowed_scales": [], "max_abs_coefficient": 1, "max_abs_error": "0"},
        {"allowed_scales": [10], "max_abs_coefficient": 1, "max_abs_error": "0"},
        {"allowed_scales": [2, 2], "max_abs_coefficient": 1, "max_abs_error": "0"},
        {"allowed_scales": [2], "max_abs_coefficient": 0, "max_abs_error": "0"},
        {"allowed_scales": [2], "max_abs_coefficient": 1, "max_abs_error": "-1"},
        {"allowed_scales": [2], "max_abs_coefficient": 1, "max_abs_error": "1.5.2"},
        {"allowed_scales": [2], "max_abs_coefficient": 1, "max_abs_error": 0.1},
    ]:
        with pytest.raises(ValidationError):
            ReaderContract.from_mapping(bad)


def test_interpret_shape():
    view = interpret("80000000")
    assert view["kind"] == ZERO and view["sign"] == 1
    assert view["payload"] is None and view["value"] == 0
    assert view["zero_sign"] == "negative"
    n = interpret("ffc00001")
    assert n["kind"] == NAN and n["sign"] == 1 and n["payload"] == 4194305
