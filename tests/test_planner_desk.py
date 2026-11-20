"""Planning desk: the task's small example, two-line trade-off, exactness."""

from fractions import Fraction

import pytest

from obsdelivery.desk import PlannerDesk
from obsdelivery.errors import (
    EvidenceMissing,
    LineConflict,
    PlanConflict,
    ValidationError,
)
from obsdelivery.planner_algo import LineModel, brute_force_joint, plan_lines
from obsdelivery.retest import EndpointService
from obsdelivery.storage import Store

BITS = {0: "00000000", 1: "3f800000", 2: "40000000"}


@pytest.fixture()
def env(tmp_path):
    store = Store(str(tmp_path / "desk.sqlite"))
    endpoints = EndpointService(store)
    desk = PlannerDesk(store, endpoints)
    yield store, endpoints, desk
    store.close()


def set_endpoint(endpoints, specimen, value, positions=("p1",)):
    """Give a specimen a reached endpoint of exactly ``value`` (integer)."""
    endpoints.register_plan(specimen, list(positions), len(positions))
    endpoints.admit_measurements(
        [
            {"specimen_id": specimen, "position": position, "revision": 1, "bits": BITS[value]}
            for position in positions
        ]
    )


def register_line_a(desk, line_id="lineA", review_cost="1/4", specimens=("g0", "g1", "b0", "b1")):
    good0, good1, bad0, bad1 = specimens
    return desk.register_line(
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


def bootstrap_small(env, suffix=""):
    """The task's small example: good endpoints 0,1; bad endpoints 1,2."""
    _, endpoints, desk = env
    set_endpoint(endpoints, f"g0{suffix}", 0)
    set_endpoint(endpoints, f"g1{suffix}", 1)
    set_endpoint(endpoints, f"b0{suffix}", 1)
    set_endpoint(endpoints, f"b1{suffix}", 2)
    register_line_a(
        desk,
        line_id=f"lineA{suffix}",
        specimens=(f"g0{suffix}", f"g1{suffix}", f"b0{suffix}", f"b1{suffix}"),
    )
    return f"lineA{suffix}"


def test_small_example_quota_two_and_zero(env):
    line = bootstrap_small(env)
    _, _, desk = env

    plan = desk.plan("small-q2", [line], "2")
    view = plan["lines"][0]
    assert view["buckets"] == ["0", "1", "2"]
    assert view["pass"] == ["0"]
    assert view["review"] == ["1"]
    assert view["reject"] == ["2"]
    assert plan["worst_cost"] == {"value": "1/2", "composition": {line: "1/2"}}
    assert plan["max_review"] == {"value": "2", "composition": {line: "1/2"}}
    assert plan["quota_ok"] is True and plan["verified"] is True

    plan0 = desk.plan("small-q0", [line], "0")
    view0 = plan0["lines"][0]
    # Score 1 is rejected together with score 2 when no review capacity exists.
    assert view0["pass"] == ["0"]
    assert view0["review"] == []
    assert view0["reject"] == ["1", "2"]
    assert plan0["worst_cost"] == {"value": "2", "composition": {line: "1/2"}}
    assert plan0["max_review"]["value"] == "0"


def test_correction_drops_best_cost_to_zero(env):
    line = bootstrap_small(env)
    _, endpoints, desk = env
    before = desk.plan("small-q0", [line], "0")
    assert before["worst_cost"]["value"] == "2"

    # Correct the good specimen whose endpoint was 1: both positions to 0.
    endpoints.admit_measurements(
        [
            {"specimen_id": "g1", "position": "p1", "revision": 2, "bits": BITS[0]},
        ]
    )
    assert endpoints.endpoint("g1")["endpoint"] == "0"

    fixed = desk.plan("small-q0-fixed", [line], "0")
    view = fixed["lines"][0]
    assert view["pass"] == ["0"]
    assert view["reject"] == ["1", "2"]
    assert fixed["worst_cost"] == {"value": "0", "composition": {line: "1/2"}}

    # The old plan re-opens with its original content and re-verifies.
    old = desk.read_plan("small-q0")
    assert old["worst_cost"]["value"] == "2" and old["verified"] is True
    # Re-planning under the old id after the correction conflicts.
    with pytest.raises(PlanConflict):
        desk.plan("small-q0", [line], "0")


def test_two_line_tradeoff_checked_by_brute_force(env):
    _, endpoints, desk = env
    line_a = bootstrap_small(env)
    # Line B: same endpoint pattern, cheaper manual review (1/8 per part).
    set_endpoint(endpoints, "g2", 0)
    set_endpoint(endpoints, "g3", 1)
    set_endpoint(endpoints, "b2", 1)
    set_endpoint(endpoints, "b3", 2)
    register_line_a(desk, line_id="lineB", review_cost="1/8", specimens=("g2", "g3", "b2", "b3"))
    line_b = "lineB"

    # Each line alone uses the full shared quota of 2 at its own optimum.
    solo_a = desk.plan("solo-a", [line_a], "2")
    assert solo_a["worst_cost"]["value"] == "1/2"
    assert solo_a["max_review"]["value"] == "2"
    solo_b = desk.plan("solo-b", [line_b], "2")
    assert solo_b["worst_cost"]["value"] == "1/4"
    assert solo_b["max_review"]["value"] == "2"

    # Jointly the quota forces a trade-off: only one line may review score 1.
    joint = desk.plan("joint-ab", [line_a, line_b], "2")
    views = {view["line_id"]: view for view in joint["lines"]}
    assert views[line_a]["pass"] == ["0"] and views[line_a]["review"] == []
    assert views[line_a]["reject"] == ["1", "2"]
    assert views[line_b]["pass"] == ["0"] and views[line_b]["review"] == ["1"]
    assert views[line_b]["reject"] == ["2"]
    assert joint["worst_cost"] == {
        "value": "9/4",
        "composition": {line_a: "1/2", line_b: "1/2"},
    }
    assert joint["max_review"] == {
        "value": "2",
        "composition": {line_a: "1/2", line_b: "1/2"},
    }

    # Brute-force cross-check over the full product of segmentations.
    def model(line_id, review_cost):
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

    models = [model(line_a, "1/4"), model(line_b, "1/8")]
    brute = brute_force_joint(models, Fraction(2))
    assert brute["feasible"] is True
    assert brute["best_cost"] == Fraction(9, 4)
    assert brute["best_review"] == Fraction(2)
    # The desk's scheme is among the exhaustive optima (here it is unique).
    desk_scheme = tuple(
        (views[line_id]["pass_count"], views[line_id]["review_count"])
        for line_id in (line_a, line_b)
    )
    assert brute["schemes"] == [((1, 0), (1, 1))]
    assert desk_scheme in brute["schemes"]
    # The frontier planner agrees with the exhaustive check exactly.
    direct = plan_lines(models, Fraction(2))
    assert direct["worst_cost"]["value"] == Fraction(9, 4)
    assert direct["max_review"]["value"] == Fraction(2)


def test_interval_worst_cases_have_different_compositions(env):
    _, endpoints, desk = env
    # Good endpoints 0,1,2,2 ; bad endpoints 1,1 ; ratio free in [1/4, 1/2].
    set_endpoint(endpoints, "h0", 0)
    set_endpoint(endpoints, "h1", 1)
    set_endpoint(endpoints, "h2", 2)
    set_endpoint(endpoints, "h3", 2)
    set_endpoint(endpoints, "c0", 1)
    set_endpoint(endpoints, "c1", 1)
    desk.register_line(
        "ivl",
        shift_count=4,
        bad_ratio_interval=["1/4", "1/2"],
        costs={"false_accept": "4", "false_reject": "2", "review": "1/4"},
        calibration=[
            {"specimen_id": "h0", "label": "good"},
            {"specimen_id": "h1", "label": "good"},
            {"specimen_id": "h2", "label": "good"},
            {"specimen_id": "h3", "label": "good"},
            {"specimen_id": "c0", "label": "bad"},
            {"specimen_id": "c1", "label": "bad"},
        ],
    )
    plan = desk.plan("ivl-plan", ["ivl"], "5/2")
    view = plan["lines"][0]
    assert view["pass"] == ["0"] and view["review"] == ["1"] and view["reject"] == ["2"]
    # Worst cost is attained at the LOW bad-ratio vertex ...
    assert plan["worst_cost"] == {"value": "55/16", "composition": {"ivl": "1/4"}}
    # ... while the review maximum is attained at the HIGH vertex: the two
    # worst-case compositions genuinely differ.
    assert plan["max_review"] == {"value": "5/2", "composition": {"ivl": "1/2"}}
    # Vertex expectations are exposed for audit.
    assert view["expected_cost_at_lo"] == "55/16"
    assert view["expected_cost_at_hi"] == "21/8"
    assert view["expected_review_at_lo"] == "7/4"
    assert view["expected_review_at_hi"] == "5/2"

    # Exhaustive check of the same instance.
    model = LineModel(
        line_id="ivl",
        shift_count=4,
        ratio_lo=Fraction(1, 4),
        ratio_hi=Fraction(1, 2),
        cost_false_accept=Fraction(4),
        cost_false_reject=Fraction(2),
        cost_review=Fraction(1, 4),
        good_endpoints=(Fraction(0), Fraction(1), Fraction(2), Fraction(2)),
        bad_endpoints=(Fraction(1), Fraction(1)),
    )
    brute = brute_force_joint([model], Fraction(5, 2))
    assert brute["best_cost"] == Fraction(55, 16)
    assert brute["best_review"] == Fraction(5, 2)
    assert brute["schemes"] == [((1, 1),)]


def test_distinct_endpoints_are_never_merged(env):
    _, endpoints, desk = env
    # One good specimen at exactly 1/8 ...
    endpoints.register_plan("g-lo", ["p1"], 1)
    endpoints.admit_measurements(
        [{"specimen_id": "g-lo", "position": "p1", "revision": 1, "bits": "3e000000"}]
    )
    # ... and one at 1/8 + 2^-27 (exact mean of 1/8 and 1/8+2^-26).
    endpoints.register_plan("g-hi", ["p1", "p2"], 2)
    endpoints.admit_measurements(
        [
            {"specimen_id": "g-hi", "position": "p1", "revision": 1, "bits": "3e000000"},
            {"specimen_id": "g-hi", "position": "p2", "revision": 1, "bits": "3e000001"},
        ]
    )
    assert endpoints.endpoint("g-hi")["endpoint"] == "16777217/134217728"
    set_endpoint(endpoints, "bb", 2)
    desk.register_line(
        "fine",
        shift_count=1,
        bad_ratio_interval=["0", "0"],
        costs={"false_accept": "4", "false_reject": "2", "review": "1/4"},
        calibration=[
            {"specimen_id": "g-lo", "label": "good"},
            {"specimen_id": "g-hi", "label": "good"},
            {"specimen_id": "bb", "label": "bad"},
        ],
    )
    plan = desk.plan("fine-plan", ["fine"], "0")
    view = plan["lines"][0]
    # 1/8 and 1/8+2^-27 stay two distinct buckets; no rounding merges them.
    assert view["buckets"] == ["1/8", "16777217/134217728", "2"]
    assert view["pass"] == ["1/8", "16777217/134217728"]
    assert view["reject"] == ["2"]
    assert plan["worst_cost"]["value"] == "0"


def test_evidence_missing_flags_the_whole_line(env):
    _, endpoints, desk = env
    set_endpoint(endpoints, "g0", 0)
    # g1: registered plan, but only one of two positions delivered.
    endpoints.register_plan("g1", ["p1", "p2"], 2)
    endpoints.admit_measurements(
        [{"specimen_id": "g1", "position": "p1", "revision": 1, "bits": BITS[1]}]
    )
    set_endpoint(endpoints, "b0", 1)
    # b1: no re-test plan registered at all.
    desk.register_line(
        "lineX",
        shift_count=4,
        bad_ratio_interval=["1/2", "1/2"],
        costs={"false_accept": "4", "false_reject": "2", "review": "1/4"},
        calibration=[
            {"specimen_id": "g0", "label": "good"},
            {"specimen_id": "g1", "label": "good"},
            {"specimen_id": "b0", "label": "bad"},
            {"specimen_id": "b1", "label": "bad"},
        ],
    )
    with pytest.raises(EvidenceMissing) as exc:
        desk.plan("x-plan", ["lineX"], "2")
    deficiencies = exc.value.deficiencies
    assert [entry["line_id"] for entry in deficiencies] == ["lineX"]
    missing = {entry["specimen_id"]: entry for entry in deficiencies[0]["missing"]}
    assert missing["g1"]["reason"] == "endpoint_not_reached"
    assert missing["g1"]["gap"] == 1 and missing["g1"]["valid_count"] == 1
    assert missing["b1"]["reason"] == "no_retest_plan"
    # The refusal is audited.
    assert [a["status"] for a in desk.attempts("x-plan")] == ["evidence_missing"]


def test_plan_replay_and_line_registration_conflicts(env):
    line = bootstrap_small(env)
    _, _, desk = env
    first = desk.plan("replay", [line], "2")
    again = desk.plan("replay", [line], "2")
    assert again["created"] is False
    assert again["content_digest"] == first["content_digest"]

    desk.register_line(
        "lineC",
        shift_count=4,
        bad_ratio_interval=["1/2", "1/2"],
        costs={"false_accept": "4", "false_reject": "2", "review": "1/4"},
        calibration=[
            {"specimen_id": "g0", "label": "good"},
            {"specimen_id": "b0", "label": "bad"},
        ],
    )
    with pytest.raises(LineConflict):
        desk.register_line(
            "lineC",
            shift_count=5,
            bad_ratio_interval=["1/2", "1/2"],
            costs={"false_accept": "4", "false_reject": "2", "review": "1/4"},
            calibration=[
                {"specimen_id": "g0", "label": "good"},
                {"specimen_id": "b0", "label": "bad"},
            ],
        )


def test_validation_limits(env):
    _, _, desk = env
    line = bootstrap_small(env)
    # Seven lines exceed the batch limit.
    with pytest.raises(ValidationError):
        desk.plan("too-many", [line] * 1 + [f"missing-{i}" for i in range(6)], "2")
    # Duplicate lines are rejected.
    with pytest.raises(ValidationError):
        desk.plan("dup", [line, line], "2")
    # Negative quota is rejected.
    with pytest.raises(ValidationError):
        desk.plan("neg", [line], "-1")
    # Registration validation: interval, costs, calibration classes.
    with pytest.raises(ValidationError):
        desk.register_line(
            "bad-interval",
            shift_count=1,
            bad_ratio_interval=["3/4", "1/2"],
            costs={"false_accept": "1", "false_reject": "1", "review": "1"},
            calibration=[
                {"specimen_id": "g0", "label": "good"},
                {"specimen_id": "b0", "label": "bad"},
            ],
        )
    with pytest.raises(ValidationError):
        desk.register_line(
            "bad-cost",
            shift_count=1,
            bad_ratio_interval=["0", "1"],
            costs={"false_accept": "-1", "false_reject": "1", "review": "1"},
            calibration=[
                {"specimen_id": "g0", "label": "good"},
                {"specimen_id": "b0", "label": "bad"},
            ],
        )
    with pytest.raises(ValidationError):
        desk.register_line(
            "one-class",
            shift_count=1,
            bad_ratio_interval=["0", "1"],
            costs={"false_accept": "1", "false_reject": "1", "review": "1"},
            calibration=[{"specimen_id": "g0", "label": "good"}],
        )
    # Thirteen distinct endpoints exceed the twelve-bucket limit (checked by
    # the pure planner, which the desk delegates to).
    models = [
        LineModel(
            line_id="wide",
            shift_count=1,
            ratio_lo=Fraction(0),
            ratio_hi=Fraction(1),
            cost_false_accept=Fraction(1),
            cost_false_reject=Fraction(1),
            cost_review=Fraction(1),
            good_endpoints=tuple(Fraction(i) for i in range(13)),
            bad_endpoints=(Fraction(0),),
        )
    ]
    with pytest.raises(ValueError):
        plan_lines(models, Fraction(0))
    with pytest.raises(ValueError):
        plan_lines(models * 7, Fraction(0))


def test_frontier_matches_brute_force_on_random_instances():
    """Fixed-seed random instances: the Pareto frontier agrees with the
    exhaustive product on cost, review and scheme membership."""
    import random

    rng = random.Random(20261120)
    for trial in range(60):
        line_count = rng.randint(1, 3)
        models = []
        for index in range(line_count):
            bucket_pool = sorted({Fraction(rng.randint(-2, 6), rng.choice([1, 2, 4])) for _ in range(4)})
            good = tuple(rng.choice(bucket_pool) for _ in range(rng.randint(1, 5)))
            bad = tuple(rng.choice(bucket_pool) for _ in range(rng.randint(1, 5)))
            lo = Fraction(rng.randint(0, 4), 8)
            hi = lo + Fraction(rng.randint(0, 4), 8)
            models.append(
                LineModel(
                    line_id=f"L{index}",
                    shift_count=rng.randint(1, 8),
                    ratio_lo=lo,
                    ratio_hi=hi,
                    cost_false_accept=Fraction(rng.randint(0, 8), rng.choice([1, 2])),
                    cost_false_reject=Fraction(rng.randint(0, 8), rng.choice([1, 2])),
                    cost_review=Fraction(rng.randint(0, 4), 4),
                    good_endpoints=good,
                    bad_endpoints=bad,
                )
            )
        quota = Fraction(rng.randint(0, 10), 2)
        planned = plan_lines(models, quota)
        brute = brute_force_joint(models, quota)
        assert brute["feasible"]  # an empty review segment always fits
        assert planned["worst_cost"]["value"] == brute["best_cost"], trial
        assert planned["max_review"]["value"] == brute["best_review"], trial
        scheme = tuple(
            (view["pass_count"], view["review_count"]) for view in planned["lines"]
        )
        assert scheme in brute["schemes"], trial
        # The quota is honoured exactly for every allowed composition.
        assert planned["max_review"]["value"] <= quota
        # The two worst-case compositions are vertex choices per line.
        for view in planned["lines"]:
            model = models[planned["lines"].index(view)]
            assert planned["worst_cost"]["composition"][model.line_id] in (
                model.ratio_lo,
                model.ratio_hi,
            )


def test_desk_enforces_bucket_limit(env):
    """Thirteen distinct endpoints through the full stack -> ValidationError."""
    import struct

    _, endpoints, desk = env

    def bits_of(integer):
        return f"{struct.unpack('>I', struct.pack('>f', float(integer)))[0]:08x}"

    calibration = []
    for i in range(13):
        specimen = f"w{i}"
        endpoints.register_plan(specimen, ["p1"], 1)
        endpoints.admit_measurements(
            [{"specimen_id": specimen, "position": "p1", "revision": 1, "bits": bits_of(i)}]
        )
        calibration.append({"specimen_id": specimen, "label": "good" if i < 12 else "bad"})
    desk.register_line(
        "wide-line",
        shift_count=4,
        bad_ratio_interval=["0", "1"],
        costs={"false_accept": "1", "false_reject": "1", "review": "1"},
        calibration=calibration,
    )
    with pytest.raises(ValidationError):
        desk.plan("wide-plan", ["wide-line"], "0")


def test_store_reopens_with_old_and_new_content(tmp_path):
    """A closed store re-opens with old-library and new artefacts intact."""
    from obsdelivery.service import SemanticsService

    path = str(tmp_path / "persist.sqlite")
    store = Store(path)
    semantics = SemanticsService(store)
    semantics.admit_observations([{"sample_id": "s1", "revision": 1, "bits": "3e000000"}])
    endpoints = EndpointService(store)
    endpoints.register_plan("sp", ["p1"], 1)
    endpoints.admit_measurements(
        [{"specimen_id": "sp", "position": "p1", "revision": 1, "bits": "3f800000"}]
    )
    endpoints.freeze("snap", "sp")
    desk = PlannerDesk(store, endpoints)
    set_endpoint(endpoints, "g", 0)
    set_endpoint(endpoints, "b", 2)
    desk.register_line(
        "ln",
        shift_count=2,
        bad_ratio_interval=["0", "1"],
        costs={"false_accept": "1", "false_reject": "1", "review": "1"},
        calibration=[
            {"specimen_id": "g", "label": "good"},
            {"specimen_id": "b", "label": "bad"},
        ],
    )
    plan = desk.plan("persist-plan", ["ln"], "1")
    store.close()

    reopened = Store(path)
    try:
        semantics2 = SemanticsService(reopened)
        assert semantics2.effective_observation("s1")[1].bits == "3e000000"
        endpoints2 = EndpointService(reopened)
        assert endpoints2.read_snapshot("snap")["endpoint"] == "1"
        desk2 = PlannerDesk(reopened, endpoints2)
        reread = desk2.read_plan("persist-plan")
        assert reread["content_digest"] == plan["content_digest"]
        assert reread["verified"] is True
    finally:
        reopened.close()


def test_old_library_unaffected_on_same_store(env):
    """Retrieval by bit pattern and decimal delivery still work on this store."""
    from obsdelivery.contracts import ReaderContract
    from obsdelivery.delivery import DeliveryService
    from obsdelivery.service import SemanticsService

    store, _, desk = env
    bootstrap_small(env)
    desk.plan("coexist", ["lineA"], "2")

    semantics = SemanticsService(store)
    view = semantics.interpret("3dcccccd")
    assert view["value"] == Fraction(13421773, 134217728)
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
    out = delivery.submit("J1", "prod", ["s1", "s2"], contract)
    lines = {line["sample_id"]: line for line in out["lines"]}
    assert lines["s1"]["coefficient"] == 12 and lines["s1"]["scale"] == 2
    assert lines["s2"]["original_bits"] == "80000000" and lines["s2"]["sign"] == 1
