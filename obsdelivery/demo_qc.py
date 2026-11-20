"""End-to-end demo of the re-test endpoint service and the planning desk.

Usage::

    python3 -m obsdelivery.demo_qc [--db /tmp/qc.sqlite] [--json]

Pipeline: register re-test positions, admit revisable measurements, freeze
exact median endpoints, register inspection lines, and jointly pick shift
thresholds under a shared review quota — including the task's small example,
a two-line trade-off checked by exhaustive enumeration, and a coexistence
check with the original bit-pattern/decimal-delivery library.
"""

from __future__ import annotations

import argparse
import json
from fractions import Fraction

from .contracts import ReaderContract
from .delivery import DeliveryService
from .desk import PlannerDesk
from .errors import EvidenceMissing, PlanConflict, SnapshotConflict, ValidationError
from .planner_algo import LineModel, brute_force_joint
from .retest import EndpointService
from .service import SemanticsService
from .storage import Store

BITS = {"0": "00000000", "1/2": "3f000000", "1": "3f800000", "2": "40000000"}
NAN_BITS = "7fc00001"


def _set_endpoint(endpoints: EndpointService, specimen: str, value: str) -> None:
    endpoints.register_plan(specimen, ["p1"], 1)
    endpoints.admit_measurements(
        [{"specimen_id": specimen, "position": "p1", "revision": 1, "bits": BITS[value]}]
    )


def _register_small_line(desk: PlannerDesk, line_id: str, review_cost: str, specimens) -> None:
    good0, good1, bad0, bad1 = specimens
    desk.register_line(
        line_id,
        shift_count=4,
        bad_ratio_interval=["1/2", "1/2"],
        costs={"false_accept": "4", "false_reject": "2", "review": review_cost},
        calibration=[
            {"specimen_id": good0, "label": "good"},
            {"specimen_id": good1, "label": "good"},
            {"specimen_id": bad0, "label": "bad"},
            {"specimen_id": bad1, "label": "bad"},
        ],
    )


def _segmentation(view: dict) -> dict:
    return {
        "pass": view["pass"],
        "review": view["review"],
        "reject": view["reject"],
    }


def _plan_or_reopen(desk: PlannerDesk, plan_id: str, line_ids, quota: str) -> dict:
    """Plan, or re-open the stored plan on a re-run over a persistent store.

    The demo corrects endpoints mid-run; on a second run the store is already
    in its terminal state, so plans computed from earlier states conflict and
    are re-opened read-only instead of recomputed against current endpoints.
    """
    try:
        return desk.plan(plan_id, line_ids, quota)
    except PlanConflict:
        return desk.read_plan(plan_id)


def run(db_path: str | None = None) -> dict:
    store = Store(db_path or ":memory:")
    endpoints = EndpointService(store)
    desk = PlannerDesk(store, endpoints)
    log: dict = {"db": db_path or ":memory:", "sections": []}

    # ------------------------------------------------------------------
    # 1. Re-test endpoint mechanics on one scratch specimen.
    # ------------------------------------------------------------------
    endpoints.register_plan("demo-x", ["a", "b", "c"], 2)
    endpoints.admit_measurements(
        [
            {"specimen_id": "demo-x", "position": "a", "revision": 1, "bits": BITS["1"]},
            {"specimen_id": "demo-x", "position": "b", "revision": 1, "bits": BITS["1"]},
        ]
    )
    reached = endpoints.endpoint("demo-x")
    # In-place revision of position a, then a late lower revision (ignored).
    endpoints.admit_measurements(
        [{"specimen_id": "demo-x", "position": "a", "revision": 2, "bits": BITS["1/2"]}]
    )
    late = endpoints.admit_measurements(
        [{"specimen_id": "demo-x", "position": "a", "revision": 1, "bits": BITS["1"]}]
    )
    # Retraction of b cancels its vote; a NaN at c never participates.
    endpoints.admit_measurements(
        [
            {"specimen_id": "demo-x", "position": "b", "revision": 2, "retract": True},
            {"specimen_id": "demo-x", "position": "c", "revision": 1, "bits": NAN_BITS},
        ]
    )
    gap = endpoints.endpoint("demo-x")
    # b delivers a higher value revision and votes again; freeze the endpoint.
    endpoints.admit_measurements(
        [{"specimen_id": "demo-x", "position": "b", "revision": 3, "bits": BITS["2"]}]
    )
    try:
        snap1 = endpoints.freeze("demo-x-snap-1", "demo-x")
    except SnapshotConflict:
        # A re-run over a persistent store already completed the specimen's
        # history, so the mid-history snapshot exists: re-open it read-only.
        snap1 = endpoints.read_snapshot("demo-x-snap-1")
    # A later correction changes the live endpoint; the old snapshot re-opens.
    endpoints.admit_measurements(
        [{"specimen_id": "demo-x", "position": "b", "revision": 4, "bits": BITS["0"]}]
    )
    snap2 = endpoints.freeze("demo-x-snap-2", "demo-x")
    reopened = endpoints.read_snapshot("demo-x-snap-1")
    try:
        endpoints.freeze("demo-x-snap-1", "demo-x")
        conflict = False
    except SnapshotConflict:
        conflict = True
    log["sections"].append(
        {
            "section": "endpoint_mechanics",
            "first_reached": {"endpoint": reached["endpoint"], "basis": reached["basis"]},
            "late_low_revision_disposition": late["members"][0]["disposition"],
            "after_retraction_and_nan": {
                "reached": gap["reached"],
                "valid_count": gap["valid_count"],
                "gap": gap["gap"],
                "shortfall": gap["shortfall"],
            },
            "snapshot_1": {"endpoint": snap1["endpoint"], "basis": snap1["basis"]},
            "snapshot_2_endpoint": snap2["endpoint"],
            "reopened_snapshot_1_endpoint": reopened["endpoint"],
            "reopened_verified": reopened["verified"],
            "refreeze_same_id_conflicts": conflict,
        }
    )

    # ------------------------------------------------------------------
    # 2. The task's small example: one line, quota 2 and quota 0.
    # ------------------------------------------------------------------
    for specimen, value in (("g0", "0"), ("g1", "1"), ("b0", "1"), ("b1", "2")):
        _set_endpoint(endpoints, specimen, value)
    _register_small_line(desk, "lineA", "1/4", ("g0", "g1", "b0", "b1"))

    small_q2 = _plan_or_reopen(desk, "small-q2", ["lineA"], "2")
    small_q0 = _plan_or_reopen(desk, "small-q0", ["lineA"], "0")
    log["sections"].append(
        {
            "section": "small_example",
            "quota_2": {
                "segmentation": _segmentation(small_q2["lines"][0]),
                "worst_cost": small_q2["worst_cost"],
                "max_review": small_q2["max_review"],
            },
            "quota_0": {
                "segmentation": _segmentation(small_q0["lines"][0]),
                "worst_cost": small_q0["worst_cost"],
            },
        }
    )

    # ------------------------------------------------------------------
    # 3. Two lines: each alone spends the full quota, jointly they trade off.
    # ------------------------------------------------------------------
    for specimen, value in (("g2", "0"), ("g3", "1"), ("b2", "1"), ("b3", "2")):
        _set_endpoint(endpoints, specimen, value)
    _register_small_line(desk, "lineB", "1/8", ("g2", "g3", "b2", "b3"))
    solo_a = _plan_or_reopen(desk, "solo-a", ["lineA"], "2")
    solo_b = _plan_or_reopen(desk, "solo-b", ["lineB"], "2")
    joint = _plan_or_reopen(desk, "joint-ab", ["lineA", "lineB"], "2")

    def model(line_id: str, review_cost: str) -> LineModel:
        return LineModel(
            line_id=line_id,
            shift_count=4,
            ratio_lo=Fraction(1, 2),
            ratio_hi=Fraction(1, 2),
            cost_false_accept=Fraction(4),
            cost_false_reject=Fraction(2),
            cost_review=Fraction(review_cost),
            good_endpoints=(Fraction(0), Fraction(1)),
            bad_endpoints=(Fraction(1), Fraction(2)),
        )

    brute = brute_force_joint([model("lineA", "1/4"), model("lineB", "1/8")], Fraction(2))
    joint_scheme = tuple(
        (view["pass_count"], view["review_count"]) for view in joint["lines"]
    )
    log["sections"].append(
        {
            "section": "two_line_tradeoff",
            "solo_a": {
                "worst_cost": solo_a["worst_cost"],
                "max_review": solo_a["max_review"],
            },
            "solo_b": {
                "worst_cost": solo_b["worst_cost"],
                "max_review": solo_b["max_review"],
            },
            "joint": {
                "segmentations": {
                    view["line_id"]: _segmentation(view) for view in joint["lines"]
                },
                "worst_cost": joint["worst_cost"],
                "max_review": joint["max_review"],
            },
            "brute_force": {
                "best_cost": str(brute["best_cost"]),
                "best_review": str(brute["best_review"]),
                "optimal_scheme_count": len(brute["schemes"]),
                "desk_scheme_among_optima": joint_scheme in brute["schemes"],
            },
        }
    )

    # ------------------------------------------------------------------
    # 4. Correction: the good specimen at score 1 is re-tested to 0, and
    #    the best zero-quota cost drops to zero; old plans stay readable.
    # ------------------------------------------------------------------
    endpoints.admit_measurements(
        [{"specimen_id": "g1", "position": "p1", "revision": 2, "bits": BITS["0"]}]
    )
    small_q0_fixed = _plan_or_reopen(desk, "small-q0-fixed", ["lineA"], "0")
    reopened_q0 = desk.read_plan("small-q0")
    log["sections"].append(
        {
            "section": "correction",
            "g1_endpoint": endpoints.endpoint("g1")["endpoint"],
            "quota_0_after_correction": {
                "segmentation": _segmentation(small_q0_fixed["lines"][0]),
                "worst_cost": small_q0_fixed["worst_cost"],
            },
            "reopened_quota_0_worst_cost": reopened_q0["worst_cost"],
        }
    )

    # ------------------------------------------------------------------
    # 5. Evidence-missing: a registered specimen without a reached endpoint
    #    flags its whole line.
    # ------------------------------------------------------------------
    _set_endpoint(endpoints, "h0", "0")
    endpoints.register_plan("h1", ["p1", "p2"], 2)
    endpoints.admit_measurements(
        [{"specimen_id": "h1", "position": "p1", "revision": 1, "bits": BITS["1"]}]
    )
    _set_endpoint(endpoints, "c0", "1")
    desk.register_line(
        "lineC",
        shift_count=4,
        bad_ratio_interval=["1/2", "1/2"],
        costs={"false_accept": "4", "false_reject": "2", "review": "1/4"},
        calibration=[
            {"specimen_id": "h0", "label": "good"},
            {"specimen_id": "h1", "label": "good"},
            {"specimen_id": "c0", "label": "bad"},
            {"specimen_id": "c1", "label": "bad"},
        ],
    )
    try:
        desk.plan("c-plan", ["lineC"], "2")
        evidence = {"raised": False}
    except EvidenceMissing as exc:
        evidence = {"raised": True, "deficiencies": exc.deficiencies}
    log["sections"].append({"section": "evidence_missing", **evidence})

    # ------------------------------------------------------------------
    # 6. The original library keeps working on the same store.
    # ------------------------------------------------------------------
    semantics = SemanticsService(store)
    interpretation = semantics.interpret("3dcccccd")
    semantics.admit_observations(
        [
            {"sample_id": "s1", "revision": 1, "bits": "3e000000"},
            {"sample_id": "s2", "revision": 1, "bits": "80000000"},
        ]
    )
    semantics.publish_reference(
        "r1",
        [
            {"sample_id": "s1", "expected": "1/8", "tolerance": "0"},
            {"sample_id": "s2", "expected": "0", "tolerance": "0"},
        ],
        copy_from=None,
    )
    semantics.publish_semantic_product("prod", ["s1", "s2"], "r1")
    delivery = DeliveryService(store, semantics)
    contract = ReaderContract.from_mapping(
        {"allowed_scales": [2, 3], "max_abs_coefficient": 99, "max_abs_error": "1/200"}
    )
    j1 = delivery.submit("J1", "prod", ["s1", "s2"], contract)
    log["sections"].append(
        {
            "section": "original_library",
            "interpret_3dcccccd": interpretation["value_text"],
            "delivery_lines": [
                {
                    "sample_id": line["sample_id"],
                    "coefficient": line["coefficient"],
                    "scale": line["scale"],
                    "reader_value": line["reader_value"],
                    "status": line["status"],
                }
                for line in j1["lines"]
            ],
        }
    )

    store.close()
    return log


def _print_text(log: dict) -> None:
    print(f"store: {log['db']}")
    for section in log["sections"]:
        name = section["section"]
        if name == "endpoint_mechanics":
            print("\n[1] re-test endpoint mechanics (specimen demo-x)")
            first = section["first_reached"]
            print(f"  two positions at 1 -> endpoint {first['endpoint']}")
            print(f"  late low revision disposition: {section['late_low_revision_disposition']}")
            gap = section["after_retraction_and_nan"]
            print(
                f"  after retract(b) + NaN(c): reached={gap['reached']} "
                f"valid={gap['valid_count']} gap={gap['gap']} shortfall={gap['shortfall']}"
            )
            snap1 = section["snapshot_1"]
            print(f"  frozen snapshot 1: endpoint {snap1['endpoint']} basis={snap1['basis']}")
            print(f"  snapshot 2 after correction: endpoint {section['snapshot_2_endpoint']}")
            print(
                f"  re-opened snapshot 1: endpoint {section['reopened_snapshot_1_endpoint']} "
                f"verified={section['reopened_verified']} "
                f"refreeze-conflict={section['refreeze_same_id_conflicts']}"
            )
        elif name == "small_example":
            print("\n[2] small example (lineA: good 0,1 / bad 1,2; 4 parts; ratio 1/2)")
            q2 = section["quota_2"]
            print(f"  quota 2: {q2['segmentation']} worst_cost={q2['worst_cost']} "
                  f"max_review={q2['max_review']}")
            q0 = section["quota_0"]
            print(f"  quota 0: {q0['segmentation']} worst_cost={q0['worst_cost']}")
        elif name == "two_line_tradeoff":
            print("\n[3] two-line trade-off (shared quota 2)")
            print(f"  lineA alone: {section['solo_a']['worst_cost']} review={section['solo_a']['max_review']}")
            print(f"  lineB alone: {section['solo_b']['worst_cost']} review={section['solo_b']['max_review']}")
            joint = section["joint"]
            for line_id, seg in joint["segmentations"].items():
                print(f"  joint {line_id}: {seg}")
            print(f"  joint worst_cost={joint['worst_cost']} max_review={joint['max_review']}")
            brute = section["brute_force"]
            print(
                f"  brute force: best_cost={brute['best_cost']} best_review={brute['best_review']} "
                f"optima={brute['optimal_scheme_count']} "
                f"desk_among_optima={brute['desk_scheme_among_optima']}"
            )
        elif name == "correction":
            print("\n[4] correction: good specimen at score 1 re-tested to "
                  f"{section['g1_endpoint']}")
            fixed = section["quota_0_after_correction"]
            print(f"  quota 0 after correction: {fixed['segmentation']} "
                  f"worst_cost={fixed['worst_cost']}")
            print(f"  re-opened old quota-0 plan: worst_cost={section['reopened_quota_0_worst_cost']}")
        elif name == "evidence_missing":
            print("\n[5] evidence missing")
            print(f"  raised={section['raised']} deficiencies={section.get('deficiencies')}")
        elif name == "original_library":
            print("\n[6] original library on the same store")
            print(f"  interpret(3dcccccd) = {section['interpret_3dcccccd']}")
            for line in section["delivery_lines"]:
                print(
                    f"  {line['sample_id']}: coefficient={line['coefficient']} "
                    f"scale={line['scale']} reader={line['reader_value']} status={line['status']}"
                )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", help="SQLite path (default: in-memory)")
    parser.add_argument("--json", action="store_true", help="emit JSON instead of text")
    args = parser.parse_args(argv)
    result = run(args.db)
    if args.json:
        print(json.dumps(result, indent=2, ensure_ascii=False, default=str))
        return 0
    _print_text(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
