"""Scale tests: 5000 observations, 100 reference versions, 1000-sample tasks."""

from __future__ import annotations

import random
import struct
import time
from fractions import Fraction

from obsdelivery.contracts import ReaderContract
from obsdelivery.delivery import DeliveryService
from obsdelivery.service import SemanticsService
from obsdelivery.storage import Store


def _finite_bits(rng: random.Random) -> str:
    """A finite pattern with moderate magnitude (roughly 2^-6 .. 2^4)."""
    while True:
        raw = rng.getrandbits(32)
        exponent = (raw >> 23) & 0xFF
        if 121 <= exponent <= 130:
            return f"{raw:08x}"


def test_scale_5000_observations_100_references_1000_sample_task(tmp_path):
    store = Store(str(tmp_path / "scale.sqlite"))
    semantics = SemanticsService(store)
    delivery = DeliveryService(store, semantics)
    rng = random.Random(739)

    ids = [f"sample-{i:04d}" for i in range(1000)]
    batch = [
        {"sample_id": sample, "revision": 1, "bits": _finite_bits(rng)}
        for sample in ids
    ]
    start = time.monotonic()
    semantics.admit_observations(batch)
    # Four more revisions per sample -> 5000 observations total.
    for revision in range(2, 6):
        semantics.admit_observations(
            [
                {"sample_id": sample, "revision": revision, "bits": _finite_bits(rng)}
                for sample in ids
            ]
        )
    admit_seconds = time.monotonic() - start
    assert len(semantics.observations()) == 5000
    assert admit_seconds < 30

    # 100 reference versions, each derived from the previous one.
    start = time.monotonic()
    semantics.publish_reference(
        "r0",
        [
            {"sample_id": sample, "expected": "0", "tolerance": "1000000000"}
            for sample in ids
        ],
        copy_from=None,
    )
    for version in range(1, 100):
        semantics.publish_reference(
            f"r{version}",
            [{"sample_id": ids[version], "expected": "0", "tolerance": "1000000000"}],
        )
    reference_seconds = time.monotonic() - start
    assert reference_seconds < 30
    assert semantics.reference("r99")["parent_version"] == "r98"
    # Unlisted items survive every derivation.
    assert len(semantics.reference("r99")["items"]) == 1000

    # A 1000-sample semantic product and delivery.
    start = time.monotonic()
    product = semantics.publish_semantic_product("big-product", ids, "r99")
    assert len(product["items"]) == 1000
    contract = ReaderContract.from_mapping(
        {
            "allowed_scales": list(range(10)),
            "max_abs_coefficient": 10**15,
            "max_abs_error": "1/1000000",
        }
    )
    result = delivery.submit("big-delivery", "big-product", ids, contract)
    big_seconds = time.monotonic() - start
    assert len(result["lines"]) == 1000
    assert big_seconds < 60
    # Re-read and re-verify every line.
    start = time.monotonic()
    reread = delivery.get_delivery("big-delivery")
    assert reread["content_digest"] == result["content_digest"]
    assert time.monotonic() - start < 30
    store.close()


def test_exact_fraction_against_host_float_reference():
    """Our exact value agrees with the host float for a spot-checked pattern."""
    bits = "3dcccccd"
    raw = int(bits, 16)
    host = struct.unpack(">f", struct.pack(">I", raw))[0]
    exact = SemanticsService.decode(bits).value
    assert Fraction(host) == exact
    assert abs(exact - Fraction(1, 10)) == Fraction(1, 671088640)
