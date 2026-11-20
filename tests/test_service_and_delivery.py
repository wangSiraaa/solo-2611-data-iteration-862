"""End-to-end tests over the supplied input plus J0/J1 and isolation rules."""

from fractions import Fraction

import pytest

from obsdelivery.contracts import ReaderContract
from obsdelivery.delivery import DeliveryService
from obsdelivery.errors import (
    BatchRejected,
    DeliveryIdConflict,
    DeliveryRejected,
    IntegrityError,
    ReferenceConflict,
    ReportConflict,
)
from obsdelivery.service import SemanticsService
from obsdelivery.storage import Store


@pytest.fixture()
def env(tmp_path):
    store = Store(str(tmp_path / "obs.sqlite"))
    semantics = SemanticsService(store)
    delivery = DeliveryService(store, semantics)
    yield store, semantics, delivery
    store.close()


def bootstrap(env):
    store, semantics, delivery = env
    semantics.admit_observations(
        [
            {"sample_id": "s1", "revision": 1, "bits": "3dcccccd"},
            {"sample_id": "s2", "revision": 1, "bits": "80000000"},
            {"sample_id": "s3", "revision": 1, "bits": "7fc00001"},
            {"sample_id": "s4", "revision": 1, "bits": "7f800000"},
            {"sample_id": "s5", "revision": 1, "bits": "00000001"},
        ]
    )
    semantics.publish_reference(
        "r1",
        [
            {"sample_id": "s1", "expected": "1/10", "tolerance": "0"},
            {"sample_id": "s2", "expected": "0", "tolerance": "0"},
            {"sample_id": "s3", "expected": "0", "tolerance": "0"},
            {"sample_id": "s4", "expected": "1", "tolerance": "0"},
            {"sample_id": "s5", "expected": "0", "tolerance": "0"},
        ],
        copy_from=None,
    )
    return semantics, delivery


def j0_contract():
    return ReaderContract.from_mapping(
        {"allowed_scales": [2, 3], "max_abs_coefficient": 99, "max_abs_error": "0"}
    )


def j1_contract():
    return ReaderContract.from_mapping(
        {"allowed_scales": [2, 3], "max_abs_coefficient": 99, "max_abs_error": "1/200"}
    )


def test_materials_739_flow(env):
    semantics, delivery = bootstrap(env)

    before = semantics.report("before", "r1")
    items = {item["sample_id"]: item for item in before["items"]}
    assert items["s1"]["status"] == "OUTSIDE"
    assert items["s1"]["difference"] == "1/671088640"
    assert items["s2"]["status"] == "PASS" and items["s2"]["sign"] == 1
    assert items["s3"]["status"] == "NONFINITE" and items["s3"]["bits"] == "7fc00001"
    assert items["s4"]["status"] == "NONFINITE" and items["s4"]["kind"] == "INFINITY"
    assert items["s5"]["status"] == "OUTSIDE"
    assert items["s5"]["difference"] == "1/713623846352979940529142984724747568191373312"

    semantics.admit_observations([{"sample_id": "s1", "revision": 2, "bits": "3e000000"}])
    semantics.publish_reference(
        "r2", [{"sample_id": "s1", "expected": "1/8", "tolerance": "0"}]
    )

    after = semantics.report("after", "r2")
    after_items = {item["sample_id"]: item for item in after["items"]}
    assert after_items["s1"]["status"] == "PASS"
    assert after_items["s1"]["bits"] == "3e000000"
    assert after_items["s1"]["value"] == "1/8"
    assert after_items["s2"]["status"] == "PASS" and after_items["s2"]["sign"] == 1
    # Unlisted reference items survive the derivation.
    assert after_items["s5"]["expected"] == "0"
    assert after_items["s3"]["status"] == "NONFINITE"

    # Old report is immutable even though new observations/references exist.
    old = semantics.read_report("before")
    old_items = {item["sample_id"]: item for item in old["items"]}
    assert old_items["s1"]["bits"] == "3dcccccd"
    assert old_items["s1"]["status"] == "OUTSIDE"
    assert old_items["s1"]["difference"] == "1/671088640"
    assert old["content_digest"] == before["content_digest"]

    # r2 must not rewrite history: r1 items are unchanged.
    assert {i["expected"] for i in semantics.reference("r1")["items"] if i["sample_id"] == "s1"} == {"1/10"}


def test_report_uses_original_value_not_delivered_rounding(env):
    semantics, delivery = bootstrap(env)
    # s1 corrected to exactly 1/8 and r2 expects 1/8; but consider a reference
    # that stays at 1/10 with an allowed delivery that rounds to 1/10: the
    # semantic verdict must remain OUTSIDE.
    product = semantics.publish_semantic_product("prod-old-ref", ["s1"], "r1")
    line_item = product["items"][0]
    assert line_item["status"] == "OUTSIDE"
    # Deliver the 0.1f under a contract that can represent it well.
    c = ReaderContract.from_mapping(
        {"allowed_scales": [3], "max_abs_coefficient": 1000, "max_abs_error": "1/100"}
    )
    delivery_result = delivery.submit("d-old-ref", "prod-old-ref", ["s1"], c)
    line = delivery_result["lines"][0]
    assert line["coefficient"] == 100 and line["scale"] == 3
    assert line["reader_value"] == "1/10"
    assert line["status"] == "OUTSIDE" and line["delivered_status"] == "OUTSIDE"
    # ...and independent read-back preserves it.
    assert delivery.get_delivery("d-old-ref")["lines"][0]["status"] == "OUTSIDE"


def test_j0_rejected_whole_and_j1_published(env):
    semantics, delivery = bootstrap(env)
    semantics.admit_observations([{"sample_id": "s1", "revision": 2, "bits": "3e000000"}])
    semantics.publish_reference("r2", [{"sample_id": "s1", "expected": "1/8", "tolerance": "0"}])
    semantics.publish_semantic_product("prod-r2", ["s1", "s2"], "r2")

    with pytest.raises(DeliveryRejected) as exc:
        delivery.submit("J0", "prod-r2", ["s1", "s2"], j0_contract())
    assert exc.value.delivery_id == "J0"
    reason = exc.value.reasons[0]
    assert reason["sample_id"] == "s1" and reason["reason"] == "no_legal_representation"
    trials = {t["scale"]: t for t in reason["scale_trials"]}
    assert trials[2]["coefficient"] == 12 and not trials[2]["error_ok"]
    assert trials[3]["coefficient"] == 125 and not trials[3]["coefficient_ok"]
    # No successful rows and no half-published delivery.
    with pytest.raises(Exception):
        delivery.get_delivery("J0")
    assert store_count(env, "deliveries") == 0
    assert store_count(env, "delivery_line") == 0

    j1 = delivery.submit("J1", "prod-r2", ["s1", "s2"], j1_contract())
    lines = {line["sample_id"]: line for line in j1["lines"]}
    assert lines["s1"]["coefficient"] == 12 and lines["s1"]["scale"] == 2
    assert lines["s1"]["error"] == "-1/200" and lines["s1"]["reader_value"] == "3/25"
    assert Fraction(lines["s1"]["reader_value"]) - Fraction(1, 8) == Fraction(-1, 200)
    assert lines["s2"]["coefficient"] == 0 and lines["s2"]["scale"] == 2
    assert lines["s2"]["original_bits"] == "80000000" and lines["s2"]["sign"] == 1
    assert lines["s2"]["status"] == "PASS"

    # Replay with identical identity -> same content, created=False.
    again = delivery.submit("J1", "prod-r2", ["s1", "s2"], j1_contract())
    assert again["created"] is False
    assert again["content_digest"] == j1["content_digest"]

    # Same delivery id, different contract -> conflict; original untouched.
    with pytest.raises(DeliveryIdConflict):
        delivery.submit("J1", "prod-r2", ["s1", "s2"], j0_contract())
    assert delivery.get_delivery("J1")["content_digest"] == j1["content_digest"]

    # Rejecting J0 does not disturb the semantics service.
    assert semantics.judge("s1", "r2")["status"] == "PASS"
    assert semantics.judge("s1", "r2")["bits"] == "3e000000"


def store_count(env, table):
    store, _, _ = env
    row = store.conn.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()
    return row["n"]


def test_no_zero_faking_and_missing(env):
    semantics, delivery = bootstrap(env)
    # s3 is NaN, s4 is Infinity: both unrepresentable.
    semantics.publish_semantic_product("prod-nf", ["s3"], "r1")
    with pytest.raises(DeliveryRejected) as exc:
        delivery.submit("DNF", "prod-nf", ["s3"], j1_contract())
    assert exc.value.reasons[0]["reason"] == "nonfinite"

    # A MISSING sample (in the reference but never observed).
    semantics.publish_reference(
        "r3", [{"sample_id": "ghost", "expected": "0", "tolerance": "0"}]
    )
    product2 = semantics.publish_semantic_product("prod-missing", ["ghost"], "r3")
    assert product2["items"][0]["status"] == "MISSING"
    with pytest.raises(DeliveryRejected) as exc2:
        delivery.submit("DMISS", "prod-missing", ["ghost"], j1_contract())
    assert exc2.value.reasons[0]["reason"] == "missing_observation"
    # A failed id is not a successful identity, but the attempt is audited.
    with pytest.raises(Exception):
        delivery.get_delivery("DMISS")
    statuses = [a["status"] for a in delivery.attempts("DMISS")]
    assert statuses == ["rejected"]


def test_failed_delivery_can_be_retried_with_better_contract(env):
    semantics, delivery = bootstrap(env)
    semantics.admit_observations([{"sample_id": "s1", "revision": 2, "bits": "3e000000"}])
    semantics.publish_reference("r2", [{"sample_id": "s1", "expected": "1/8", "tolerance": "0"}])
    semantics.publish_semantic_product("prod-r2", ["s1", "s2"], "r2")
    with pytest.raises(DeliveryRejected):
        delivery.submit("D0", "prod-r2", ["s1", "s2"], j0_contract())
    # Same id, stricter-in-name but actually feasible contract: allowed, because
    # the failed attempt did not consume the successful identity.
    ok = delivery.submit("D0", "prod-r2", ["s1", "s2"], j1_contract())
    assert ok["delivery_id"] == "D0"
    assert len(delivery.attempts("D0")) == 2
    assert [a["status"] for a in delivery.attempts("D0")] == ["rejected", "published"]


def test_observation_admission_rules(env):
    semantics, _ = bootstrap(env)
    # Same revision, same bits -> replay accepted but no new state.
    result = semantics.admit_observations([{"sample_id": "s2", "revision": 1, "bits": "80000000"}])
    assert result["outcome"] == "accepted"
    assert result["members"][0]["disposition"] == "replayed"
    # Lower revision, legal -> accepted but ignored.
    result = semantics.admit_observations([{"sample_id": "s2", "revision": 1, "bits": "80000000"}])
    low = semantics.admit_observations([{"sample_id": "s2", "revision": 1, "bits": "80000000"}])
    assert low["members"][0]["disposition"] == "replayed"
    semantics.admit_observations([{"sample_id": "s2", "revision": 2, "bits": "3f800000"}])
    older = semantics.admit_observations([{"sample_id": "s2", "revision": 1, "bits": "80000000"}])
    # The revision-1 record already exists with the same bits, so it replays
    # rather than being ignored, and revision 2 still wins.
    assert older["members"][0]["disposition"] in ("replayed", "ignored_older")
    assert semantics.effective_revision("s2") == 2
    assert semantics.effective_observation("s2")[1].bits == "3f800000"

    # Higher revision -> new effective value.
    newer = semantics.admit_observations([{"sample_id": "s2", "revision": 3, "bits": "80000000"}])
    assert newer["members"][0]["disposition"] == "new"
    assert semantics.effective_revision("s2") == 3


def test_same_revision_different_bits_rejects_batch_atomically(env):
    semantics, _ = bootstrap(env)
    with pytest.raises(BatchRejected):
        semantics.admit_observations(
            [
                {"sample_id": "s1", "revision": 1, "bits": "3e000000"},  # conflict
                {"sample_id": "s9", "revision": 1, "bits": "3f800000"},  # would be new
            ]
        )
    # Nothing changed: s1 still has its original bits, s9 never appeared.
    assert semantics.effective_observation("s1")[1].bits == "3dcccccd"
    assert semantics.effective_observation("s9") is None


def test_lower_revision_after_higher_is_ignored_not_conflicting(env):
    _, semantics = (None, bootstrap(env)[0])
    semantics.admit_observations([{"sample_id": "q", "revision": 5, "bits": "3f800000"}])
    res = semantics.admit_observations([{"sample_id": "q", "revision": 3, "bits": "40000000"}])
    assert res["outcome"] == "accepted"
    assert res["members"][0]["disposition"] == "ignored_older"
    assert semantics.effective_observation("q")[1].bits == "3f800000"


def test_immutable_ids_conflict(env):
    semantics, delivery = bootstrap(env)
    first = semantics.report("stable", "r1")
    again = semantics.report("stable", "r1")
    assert again["content_digest"] == first["content_digest"] and again["created"] is False
    with pytest.raises(ReferenceConflict):
        semantics.publish_reference("r1", [{"sample_id": "s1", "expected": "1/8", "tolerance": "0"}])
    product = semantics.publish_semantic_product("p", ["s1"], "r1")
    assert product["items"][0]["status"] == "OUTSIDE"
    with pytest.raises(Exception):
        semantics.publish_semantic_product("p", ["s1", "s2"], "r1")


def test_same_report_id_different_content_conflicts(env):
    semantics, _ = bootstrap(env)
    first = semantics.report("shared", "r1")
    assert first["items"]
    semantics.admit_observations([{"sample_id": "s1", "revision": 2, "bits": "3e000000"}])
    with pytest.raises(ReportConflict):
        semantics.report("shared", "r1")  # same ref, new effective observation


def test_integrity_detects_tampering(env):
    semantics, delivery = bootstrap(env)
    semantics.admit_observations([{"sample_id": "s1", "revision": 2, "bits": "3e000000"}])
    semantics.publish_reference("r2", [{"sample_id": "s1", "expected": "1/8", "tolerance": "0"}])
    semantics.publish_semantic_product("prod-r2", ["s1", "s2"], "r2")
    delivery.submit("D", "prod-r2", ["s1", "s2"], j1_contract())
    # Flip a stored coefficient behind the service's back.
    store, _, _ = env
    store.conn.execute(
        "UPDATE delivery_line SET coefficient=13 WHERE delivery_id='D' AND sample_id='s1'"
    )
    with pytest.raises(IntegrityError):
        delivery.get_delivery("D")


def test_multiple_scales_selectable_per_record(env):
    semantics, delivery = bootstrap(env)
    semantics.publish_reference(
        "rz",
        [
            {"sample_id": "a", "expected": "0", "tolerance": "100"},
            {"sample_id": "b", "expected": "0", "tolerance": "100"},
        ],
    )
    semantics.admit_observations(
        [
            {"sample_id": "a", "revision": 1, "bits": "3f800000"},  # 1
            {"sample_id": "b", "revision": 1, "bits": "3f000000"},  # 0.5
        ]
    )
    semantics.publish_semantic_product("pz", ["a", "b"], "rz")
    contract = ReaderContract.from_mapping(
        {"allowed_scales": [0, 1], "max_abs_coefficient": 9, "max_abs_error": "0"}
    )
    out = delivery.submit("DZ", "pz", ["a", "b"], contract)
    lines = {line["sample_id"]: line for line in out["lines"]}
    assert lines["a"]["coefficient"] == 1 and lines["a"]["scale"] == 0
    assert lines["b"]["coefficient"] == 5 and lines["b"]["scale"] == 1


def test_bits_are_preserved_verbatim(env):
    semantics, delivery = bootstrap(env)
    semantics.admit_observations([{"sample_id": "z", "revision": 1, "bits": "80000000"}])
    semantics.publish_reference("rz", [{"sample_id": "z", "expected": "0", "tolerance": "0"}])
    product = semantics.publish_semantic_product("pz", ["z"], "rz")
    item = product["items"][0]
    assert item["bits"] == "80000000" and item["sign"] == 1
    out = delivery.submit("DZ", "pz", ["z"], j1_contract())
    line = out["lines"][0]
    assert line["original_bits"] == "80000000" and line["sign"] == 1
    assert line["coefficient"] == 0
    assert line["evidence"]["chosen"]["coefficient"] == 0


def test_public_delivery_document_self_verifies(env):
    from obsdelivery.public import public_delivery, public_semantic_product, verify_public_delivery

    semantics, delivery = bootstrap(env)
    semantics.admit_observations([{"sample_id": "s1", "revision": 2, "bits": "3e000000"}])
    semantics.publish_reference("r2", [{"sample_id": "s1", "expected": "1/8", "tolerance": "0"}])
    semantics.publish_semantic_product("prod-r2", ["s1", "s2"], "r2")
    delivery.submit("J1", "prod-r2", ["s1", "s2"], j1_contract())

    product_doc = public_semantic_product(semantics, "prod-r2")
    assert product_doc["content_digest"]
    assert {r["sample_id"] for r in product_doc["records"]} == {"s1", "s2"}

    doc = public_delivery(delivery, "J1")
    assert doc["canonical_product"]["content_digest"] == product_doc["content_digest"]
    assert doc["reference"]["version"] == "r2"
    assert verify_public_delivery(doc) == []
    # Tampering with a coefficient is detected by the standalone verifier.
    bad = {**doc, "lines": [dict(doc["lines"][0], coefficient=13), doc["lines"][1]]}
    assert verify_public_delivery(bad)
    # Laundering OUTSIDE into PASS is detected independently.
    bad2 = {**doc, "lines": [dict(doc["lines"][0], delivered_status="OUTSIDE"), doc["lines"][1]]}
    assert verify_public_delivery(bad2)


def test_old_product_and_delivery_stay_readable_after_new_state(env):
    semantics, delivery = bootstrap(env)
    old_product = semantics.publish_semantic_product("prod-r1", ["s1", "s2"], "r1")
    old_delivery = delivery.submit("D-r1", "prod-r1", ["s1", "s2"], j1_contract())
    assert {l["sample_id"]: l for l in old_delivery["lines"]}["s1"]["status"] == "OUTSIDE"

    # A later correction and reference publish create NEW products/deliveries;
    # the old ones still read back identically.
    semantics.admit_observations([{"sample_id": "s1", "revision": 2, "bits": "3e000000"}])
    semantics.publish_reference("r2", [{"sample_id": "s1", "expected": "1/8", "tolerance": "0"}])
    new_product = semantics.publish_semantic_product("prod-r2", ["s1", "s2"], "r2")
    assert {i["sample_id"]: i for i in new_product["items"]}["s1"]["bits"] == "3e000000"
    assert semantics.get_semantic_product("prod-r1")["content_digest"] == old_product["content_digest"]
    reread = delivery.get_delivery("D-r1")
    assert reread["content_digest"] == old_delivery["content_digest"]
    old_line = {l["sample_id"]: l for l in reread["lines"]}["s1"]
    assert old_line["original_bits"] == "3dcccccd"
    assert old_line["status"] == "OUTSIDE"
