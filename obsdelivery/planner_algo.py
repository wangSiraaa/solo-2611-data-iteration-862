"""Joint shift-threshold planning across inspection lines (pure, storage-free).

Model
-----
Each line sorts its distinct calibration endpoints — exact rationals, two
different endpoints are never merged into one bucket — into ascending score
buckets.  A segmentation passes the first ``k`` buckets directly, sends the
next ``m`` buckets to manual review and rejects the rest.  Empty segments are
allowed and specimens with equal scores are never split.

Within each class (good/bad) the empirical distribution of the registered
calibration endpoints is the shift score distribution of that class.  Manual
review is always correct, so a reviewed part costs only the review fee; a
bad part in a passed bucket costs the false-accept fee and a good part in a
rejected bucket costs the false-reject fee.  The bad-part ratio of each line
varies independently inside its closed interval, so both the expected review
count and the expected cost of a segmentation are affine in the ratio and
attain their worst case at a vertex (the lower ratio wins ties, a documented
deterministic choice).

The joint problem — minimise the worst-case expected total cost while the
worst-case expected total review stays within the shared quota for every
allowed incoming composition — decomposes across lines, so an exact
Pareto-frontier sweep over the lines is sufficient.  Capacity is compared
exactly (``Fraction``); expected counts are never rounded per line.

Limits: at most ``MAX_LINES`` lines per batch and ``MAX_BUCKETS`` distinct
score buckets per line.
"""

from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction
from itertools import product
from typing import Sequence

MAX_LINES = 6
MAX_BUCKETS = 12


@dataclass(frozen=True)
class LineModel:
    line_id: str
    shift_count: int
    ratio_lo: Fraction
    ratio_hi: Fraction
    cost_false_accept: Fraction
    cost_false_reject: Fraction
    cost_review: Fraction
    good_endpoints: tuple[Fraction, ...]
    bad_endpoints: tuple[Fraction, ...]


@dataclass(frozen=True)
class SegmentOption:
    """One segmentation of one line with its vertex evaluations."""

    pass_count: int
    review_count: int
    review_at_lo: Fraction
    review_at_hi: Fraction
    cost_at_lo: Fraction
    cost_at_hi: Fraction
    review_max: Fraction
    review_worst_ratio: Fraction
    cost_max: Fraction
    cost_worst_ratio: Fraction


def _validate(lines: list[LineModel], shared_quota: Fraction) -> None:
    if not 1 <= len(lines) <= MAX_LINES:
        raise ValueError(f"a batch plans 1..{MAX_LINES} lines, got {len(lines)}")
    for line in lines:
        if not isinstance(line.line_id, str) or not line.line_id:
            raise ValueError(f"line_id must be a non-empty string, got {line.line_id!r}")
        if isinstance(line.shift_count, bool) or not isinstance(line.shift_count, int) or line.shift_count < 1:
            raise ValueError(f"{line.line_id}: shift_count must be a positive integer")
        if not (Fraction(0) <= line.ratio_lo <= line.ratio_hi <= Fraction(1)):
            raise ValueError(
                f"{line.line_id}: bad-part ratio interval must satisfy 0 <= lo <= hi <= 1"
            )
        for name, cost in (
            ("false_accept", line.cost_false_accept),
            ("false_reject", line.cost_false_reject),
            ("review", line.cost_review),
        ):
            if cost < 0:
                raise ValueError(f"{line.line_id}: cost {name} must be non-negative")
        if not line.good_endpoints or not line.bad_endpoints:
            raise ValueError(
                f"{line.line_id}: both classes need calibration data "
                "(good and bad endpoints must be non-empty)"
            )
        distinct = set(line.good_endpoints) | set(line.bad_endpoints)
        if len(distinct) > MAX_BUCKETS:
            raise ValueError(
                f"{line.line_id}: {len(distinct)} distinct endpoints exceed "
                f"the {MAX_BUCKETS}-bucket limit"
            )
    if shared_quota < 0:
        raise ValueError("shared quota must be non-negative")


def line_options(line: LineModel) -> tuple[tuple[Fraction, ...], list[SegmentOption]]:
    """All segmentations of one line with exact vertex evaluations."""
    buckets = tuple(sorted(set(line.good_endpoints) | set(line.bad_endpoints)))
    good_total = len(line.good_endpoints)
    bad_total = len(line.bad_endpoints)
    good_mass = [
        Fraction(sum(1 for v in line.good_endpoints if v == bucket), good_total)
        for bucket in buckets
    ]
    bad_mass = [
        Fraction(sum(1 for v in line.bad_endpoints if v == bucket), bad_total)
        for bucket in buckets
    ]
    n = line.shift_count
    lo, hi = line.ratio_lo, line.ratio_hi
    options: list[SegmentOption] = []
    count = len(buckets)
    for k in range(count + 1):
        for m in range(count + 1 - k):
            g_pass = sum(good_mass[:k])
            g_review = sum(good_mass[k : k + m])
            g_reject = sum(good_mass[k + m :])
            d_pass = sum(bad_mass[:k])
            d_review = sum(bad_mass[k : k + m])

            def expected_review(rho: Fraction) -> Fraction:
                return n * (1 - rho) * g_review + n * rho * d_review

            def expected_cost(rho: Fraction) -> Fraction:
                return (
                    n
                    * (1 - rho)
                    * (g_reject * line.cost_false_reject + g_review * line.cost_review)
                    + n
                    * rho
                    * (d_pass * line.cost_false_accept + d_review * line.cost_review)
                )

            review_lo, review_hi = expected_review(lo), expected_review(hi)
            cost_lo, cost_hi = expected_cost(lo), expected_cost(hi)
            options.append(
                SegmentOption(
                    pass_count=k,
                    review_count=m,
                    review_at_lo=review_lo,
                    review_at_hi=review_hi,
                    cost_at_lo=cost_lo,
                    cost_at_hi=cost_hi,
                    review_max=max(review_lo, review_hi),
                    review_worst_ratio=lo if review_lo >= review_hi else hi,
                    cost_max=max(cost_lo, cost_hi),
                    cost_worst_ratio=lo if cost_lo >= cost_hi else hi,
                )
            )
    return buckets, options


def _pareto_prune(points: list[tuple[Fraction, Fraction, tuple]]) -> list:
    """Drop (review, cost) points dominated in both coordinates.

    Equal pairs collapse to one representative (ties are broken arbitrarily,
    as specified); the result is sorted by ascending review with strictly
    descending cost.
    """
    ordered = sorted(points, key=lambda point: (point[0], point[1]))
    kept = []
    best_cost = None
    for point in ordered:
        if best_cost is None or point[1] < best_cost:
            kept.append(point)
            best_cost = point[1]
    return kept


def _joint_frontier(per_line_options: list[list[SegmentOption]]) -> list:
    """Exact Pareto frontier of (total max review, total max cost)."""
    frontier: list[tuple[Fraction, Fraction, tuple]] = [(Fraction(0), Fraction(0), ())]
    for options in per_line_options:
        frontier = _pareto_prune(
            [
                (review + option.review_max, cost + option.cost_max, choices + (index,))
                for review, cost, choices in frontier
                for index, option in enumerate(options)
            ]
        )
    return frontier


def _select(frontier: list, shared_quota: Fraction):
    feasible = [point for point in frontier if point[0] <= shared_quota]
    if not feasible:
        return None
    # Minimum worst cost; ties resolve to the smaller review, then to
    # whichever scheme the frontier kept (ties are broken arbitrarily).
    return min(feasible, key=lambda point: (point[1], point[0]))


def _build_output(
    lines: list[LineModel],
    buckets_per_line: list[tuple[Fraction, ...]],
    options_per_line: list[list[SegmentOption]],
    shared_quota: Fraction,
    choices: tuple[int, ...],
    status: str,
) -> dict:
    line_views = []
    total_cost = Fraction(0)
    total_review = Fraction(0)
    cost_composition: dict[str, Fraction] = {}
    review_composition: dict[str, Fraction] = {}
    for line, buckets, options, index in zip(
        lines, buckets_per_line, options_per_line, choices
    ):
        option = options[index]
        k, m = option.pass_count, option.review_count
        line_views.append(
            {
                "line_id": line.line_id,
                "buckets": list(buckets),
                "pass": list(buckets[:k]),
                "review": list(buckets[k : k + m]),
                "reject": list(buckets[k + m :]),
                "pass_count": k,
                "review_count": m,
                "expected_review_at_lo": option.review_at_lo,
                "expected_review_at_hi": option.review_at_hi,
                "expected_cost_at_lo": option.cost_at_lo,
                "expected_cost_at_hi": option.cost_at_hi,
                "review_max": option.review_max,
                "review_worst_ratio": option.review_worst_ratio,
                "cost_max": option.cost_max,
                "cost_worst_ratio": option.cost_worst_ratio,
            }
        )
        total_cost += option.cost_max
        total_review += option.review_max
        cost_composition[line.line_id] = option.cost_worst_ratio
        review_composition[line.line_id] = option.review_worst_ratio
    return {
        "status": status,
        "shared_quota": shared_quota,
        "lines": line_views,
        "worst_cost": {"value": total_cost, "composition": cost_composition},
        "max_review": {"value": total_review, "composition": review_composition},
        "quota_ok": total_review <= shared_quota,
    }


def plan_lines(lines: Sequence[LineModel], shared_quota: Fraction) -> dict:
    """Jointly optimal thresholds under the shared review quota.

    Returns a dict of exact ``Fraction`` values: per-line segmentation, the
    worst expected total cost with the incoming composition attaining it, and
    the maximum expected total review with its (possibly different)
    composition.
    """
    models = list(lines)
    _validate(models, shared_quota)
    buckets_per_line = []
    options_per_line = []
    for model in models:
        buckets, options = line_options(model)
        buckets_per_line.append(buckets)
        options_per_line.append(options)
    frontier = _joint_frontier(options_per_line)
    chosen = _select(frontier, shared_quota)
    if chosen is None:
        # Unreachable for a non-negative quota (an empty review segment always
        # fits); kept for completeness.
        review, _, choices = frontier[0]
        output = _build_output(
            models, buckets_per_line, options_per_line, shared_quota, choices, "infeasible"
        )
        output["min_review"] = review
        return output
    return _build_output(
        models, buckets_per_line, options_per_line, shared_quota, chosen[2], "ok"
    )


def brute_force_joint(lines: Sequence[LineModel], shared_quota: Fraction) -> dict:
    """Exhaustive cross-check over the full product of segmentations.

    Returns the best feasible (cost, review) and *every* scheme attaining the
    best cost, so a frontier-chosen scheme can be confirmed against the whole
    optimal set on small instances.
    """
    models = list(lines)
    _validate(models, shared_quota)
    options_per_line = [line_options(model)[1] for model in models]
    best_cost = None
    best_review = None
    schemes: list[tuple[tuple[int, int], ...]] = []
    for combo in product(*(range(len(options)) for options in options_per_line)):
        review = sum(
            options_per_line[i][index].review_max for i, index in enumerate(combo)
        )
        if review > shared_quota:
            continue
        cost = sum(
            options_per_line[i][index].cost_max for i, index in enumerate(combo)
        )
        scheme = tuple(
            (options_per_line[i][index].pass_count, options_per_line[i][index].review_count)
            for i, index in enumerate(combo)
        )
        if best_cost is None or cost < best_cost:
            best_cost, best_review, schemes = cost, review, [scheme]
        elif cost == best_cost:
            best_review = min(best_review, review)
            schemes.append(scheme)
    return {
        "feasible": best_cost is not None,
        "best_cost": best_cost,
        "best_review": best_review,
        "schemes": schemes,
    }
