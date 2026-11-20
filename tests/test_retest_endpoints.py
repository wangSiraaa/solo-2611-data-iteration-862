"""Re-test endpoint service: aggregation, revision rules, snapshots."""

from fractions import Fraction

import pytest

from obsdelivery.endpoint_algo import median
from obsdelivery.errors import (
    RetestBatchRejected,
    RetestPlanConflict,
    SnapshotConflict,
    SnapshotNotFound,
    ValidationError,
)
from obsdelivery.retest import EndpointService
from obsdelivery.storage import Store

BITS_ZERO = "00000000"
BITS_ONE = "3f800000"
BITS_TWO = "40000000"
BITS_HALF = "3f000000"
BITS_ONE_PLUS = "3f800001"  # 1 + 2^-23
BITS_NAN = "7fc00001"


@pytest.fixture()
def svc(tmp_path):
    store = Store(str(tmp_path / "retest.sqlite"))
    service = EndpointService(store)
    yield service
    store.close()


def test_median_exact_odd_and_even():
    assert median([Fraction(1), Fraction(0), Fraction(2)]) == Fraction(1)
    # Even count: exact mean of the two middle values, never rounded.
    assert median([Fraction(0), Fraction(1), Fraction(1), Fraction(2)]) == Fraction(1)
    assert median([Fraction(0), Fraction(1)]) == Fraction(1, 2)
    assert median([Fraction(1, 8), Fraction(1, 4)]) == Fraction(3, 16)
    with pytest.raises(ValueError):
        median([])


def test_endpoint_reaches_contract_quantity_with_basis(svc):
    svc.register_plan("sp1", ["p1", "p2", "p3"], 2)
    # Only p1 valid so far: gap 1, shortfall explains which positions lack votes.
    svc.admit_measurements([{"specimen_id": "sp1", "position": "p1", "revision": 1, "bits": BITS_ONE}])
    status = svc.endpoint("sp1")
    assert status["reached"] is False
    assert status["valid_count"] == 1 and status["required_count"] == 2
    assert status["gap"] == 1
    assert status["endpoint"] is None
    assert status["shortfall"] == [
        {"position": "p2", "reason": "missing"},
        {"position": "p3", "reason": "missing"},
    ]

    svc.admit_measurements([{"specimen_id": "sp1", "position": "p2", "revision": 1, "bits": BITS_TWO}])
    status = svc.endpoint("sp1")
    assert status["reached"] is True and status["gap"] == 0
    # Median of all valid positions (even count -> exact mean).
    assert status["endpoint"] == "3/2"
    # The basis names the contributing positions and their bit patterns.
    assert [(b["position"], b["bits"], b["value"]) for b in status["basis"]] == [
        ("p1", BITS_ONE, "1"),
        ("p2", BITS_TWO, "2"),
    ]

    # A third valid position joins the median (all valid positions vote).
    svc.admit_measurements([{"specimen_id": "sp1", "position": "p3", "revision": 1, "bits": BITS_ZERO}])
    status = svc.endpoint("sp1")
    assert status["valid_count"] == 3
    assert status["endpoint"] == "1"


def test_each_position_votes_with_current_version_only(svc):
    svc.register_plan("sp2", ["p1", "p2"], 2)
    svc.admit_measurements(
        [
            {"specimen_id": "sp2", "position": "p1", "revision": 1, "bits": BITS_ONE},
            {"specimen_id": "sp2", "position": "p2", "revision": 1, "bits": BITS_ONE},
        ]
    )
    assert svc.endpoint("sp2")["endpoint"] == "1"
    # In-place revision: p1 revised to 2 -> one vote of value 2, not two votes.
    svc.admit_measurements([{"specimen_id": "sp2", "position": "p1", "revision": 2, "bits": BITS_TWO}])
    status = svc.endpoint("sp2")
    assert status["valid_count"] == 2
    assert status["endpoint"] == "3/2"
    basis = {row["position"]: row for row in status["basis"]}
    assert basis["p1"]["revision"] == 2 and basis["p1"]["bits"] == BITS_TWO


def test_retraction_cancels_the_vote_and_restores(svc):
    svc.register_plan("sp3", ["p1", "p2"], 2)
    svc.admit_measurements(
        [
            {"specimen_id": "sp3", "position": "p1", "revision": 1, "bits": BITS_ONE},
            {"specimen_id": "sp3", "position": "p2", "revision": 1, "bits": BITS_TWO},
        ]
    )
    assert svc.endpoint("sp3")["reached"] is True
    # Retract p2 at a higher revision: the vote is cancelled, gap reappears.
    svc.admit_measurements([{"specimen_id": "sp3", "position": "p2", "revision": 2, "retract": True}])
    status = svc.endpoint("sp3")
    assert status["reached"] is False and status["gap"] == 1
    assert status["shortfall"] == [{"position": "p2", "reason": "retracted"}]
    # A still-higher value revision re-enables the position.
    svc.admit_measurements([{"specimen_id": "sp3", "position": "p2", "revision": 3, "bits": BITS_HALF}])
    status = svc.endpoint("sp3")
    assert status["reached"] is True and status["endpoint"] == "3/4"


def test_nonfinite_does_not_participate(svc):
    svc.register_plan("sp4", ["p1", "p2"], 2)
    svc.admit_measurements(
        [
            {"specimen_id": "sp4", "position": "p1", "revision": 1, "bits": BITS_ONE},
            {"specimen_id": "sp4", "position": "p2", "revision": 1, "bits": BITS_NAN},
        ]
    )
    status = svc.endpoint("sp4")
    assert status["reached"] is False and status["valid_count"] == 1
    assert status["shortfall"] == [{"position": "p2", "reason": "nonfinite"}]
    row = {row["position"]: row for row in status["positions"]}["p2"]
    assert row["kind"] == "NAN" and row["participates"] is False


def test_late_lower_revision_duplicate_and_conflicting_batch(svc):
    svc.register_plan("sp5", ["p1"], 1)
    svc.admit_measurements([{"specimen_id": "sp5", "position": "p1", "revision": 2, "bits": BITS_TWO}])
    # Late lower revision with new content: accepted but ignored.
    late = svc.admit_measurements(
        [{"specimen_id": "sp5", "position": "p1", "revision": 1, "bits": BITS_ONE}]
    )
    assert late["outcome"] == "accepted"
    assert late["members"][0]["disposition"] == "ignored_older"
    assert svc.endpoint("sp5")["endpoint"] == "2"
    # Duplicate delivery of the effective revision inside a fresh batch:
    # recomputed disposition is "replayed", no new state.
    svc.register_plan("sp5b", ["p1"], 1)
    replay = svc.admit_measurements(
        [
            {"specimen_id": "sp5", "position": "p1", "revision": 2, "bits": BITS_TWO},
            {"specimen_id": "sp5b", "position": "p1", "revision": 1, "bits": BITS_ONE},
        ]
    )
    dispositions = {m["specimen_id"]: m["disposition"] for m in replay["members"]}
    assert dispositions == {"sp5": "replayed", "sp5b": "new"}
    # An exactly identical batch replays the stored admission by digest.
    identical = svc.admit_measurements(
        [{"specimen_id": "sp5", "position": "p1", "revision": 2, "bits": BITS_TWO}]
    )
    assert identical["created"] is False
    assert svc.endpoint("sp5")["endpoint"] == "2"
    # Same revision, different content: the whole batch is rejected atomically.
    svc.register_plan("sp6", ["p1"], 1)
    with pytest.raises(RetestBatchRejected):
        svc.admit_measurements(
            [
                {"specimen_id": "sp5", "position": "p1", "revision": 2, "bits": BITS_ONE},
                {"specimen_id": "sp6", "position": "p1", "revision": 1, "bits": BITS_ONE},
            ]
        )
    assert svc.endpoint("sp5")["endpoint"] == "2"
    # sp6 was never written either (atomic rejection); the ignored late
    # revision was audited but never became state.
    assert svc.measurements() == [
        {"specimen_id": "sp5", "position": "p1", "revision": 2, "action": "value", "bits": BITS_TWO},
        {"specimen_id": "sp5b", "position": "p1", "revision": 1, "action": "value", "bits": BITS_ONE},
    ]


def test_unregistered_specimen_or_position_rejected(svc):
    svc.register_plan("sp7", ["p1"], 1)
    with pytest.raises(ValidationError):
        svc.admit_measurements(
            [{"specimen_id": "ghost", "position": "p1", "revision": 1, "bits": BITS_ONE}]
        )
    with pytest.raises(ValidationError):
        svc.admit_measurements(
            [{"specimen_id": "sp7", "position": "p9", "revision": 1, "bits": BITS_ONE}]
        )
    with pytest.raises(ValidationError):
        svc.admit_measurements(
            [{"specimen_id": "sp7", "position": "p1", "revision": 1, "bits": BITS_ONE, "retract": True}]
        )


def test_plan_registration_immutable(svc):
    first = svc.register_plan("sp8", ["p1", "p2"], 1)
    again = svc.register_plan("sp8", ["p1", "p2"], 1)
    assert again["created"] is False
    assert again["content_digest"] == first["content_digest"]
    with pytest.raises(RetestPlanConflict):
        svc.register_plan("sp8", ["p1", "p2"], 2)
    with pytest.raises(ValidationError):
        svc.register_plan("sp9", ["p1"], 2)  # required beyond registered positions


def test_even_count_mean_is_exact_not_rounded(svc):
    svc.register_plan("sp10", ["p1", "p2"], 2)
    svc.admit_measurements(
        [
            {"specimen_id": "sp10", "position": "p1", "revision": 1, "bits": BITS_ONE},
            {"specimen_id": "sp10", "position": "p2", "revision": 1, "bits": BITS_ONE_PLUS},
        ]
    )
    status = svc.endpoint("sp10")
    # Exact mean of 1 and 1+2^-23 is 1+2^-24; a rounded service would read 1.
    assert status["endpoint"] == str(Fraction(1) + Fraction(1, 2**24))
    assert Fraction(status["endpoint"]) != 1


def test_freeze_reopen_and_conflict(svc):
    svc.register_plan("sp11", ["p1", "p2"], 2)
    svc.admit_measurements(
        [
            {"specimen_id": "sp11", "position": "p1", "revision": 1, "bits": BITS_ONE},
            {"specimen_id": "sp11", "position": "p2", "revision": 1, "bits": BITS_ONE},
        ]
    )
    snap = svc.freeze("snap-1", "sp11")
    assert snap["created"] is True and snap["verified"] is True
    assert snap["endpoint"] == "1"
    assert [(b["position"], b["bits"]) for b in snap["basis"]] == [
        ("p1", BITS_ONE),
        ("p2", BITS_ONE),
    ]

    # Correction changes the live endpoint; the old snapshot re-opens unchanged.
    svc.admit_measurements(
        [
            {"specimen_id": "sp11", "position": "p1", "revision": 2, "bits": BITS_TWO},
            {"specimen_id": "sp11", "position": "p2", "revision": 2, "bits": BITS_TWO},
        ]
    )
    assert svc.endpoint("sp11")["endpoint"] == "2"
    old = svc.read_snapshot("snap-1")
    assert old["endpoint"] == "1" and old["verified"] is True
    assert old["content_digest"] == snap["content_digest"]
    # Re-freezing the same id with new content conflicts; a new id freezes fine.
    with pytest.raises(SnapshotConflict):
        svc.freeze("snap-1", "sp11")
    new = svc.freeze("snap-2", "sp11")
    assert new["endpoint"] == "2"
    # Replaying the frozen content under its own id is a no-op replay.
    # (snap-1 content is no longer the live state, so it cannot be reproduced;
    #  re-reading stays the supported path.)
    with pytest.raises(SnapshotNotFound):
        svc.read_snapshot("snap-3")


def test_freeze_requires_reached_endpoint(svc):
    svc.register_plan("sp12", ["p1", "p2"], 2)
    svc.admit_measurements([{"specimen_id": "sp12", "position": "p1", "revision": 1, "bits": BITS_ONE}])
    with pytest.raises(ValidationError) as exc:
        svc.freeze("snap-x", "sp12")
    assert "gap 1" in str(exc.value)
