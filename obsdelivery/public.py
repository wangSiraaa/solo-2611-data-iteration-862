"""Public artefact rendering and verification.

* ``public_semantic_product`` — immutable canonical observation product.
* ``public_delivery`` — standalone delivery document with enough information
  for an independent reader to re-check the actual decimal representation
  without consulting the current observation state.

Both builders re-verify the stored content digests before rendering.
"""

from __future__ import annotations

import json
from fractions import Fraction
from pathlib import Path

from .delivery import DeliveryService
from .service import SemanticsService


def public_semantic_product(semantics: SemanticsService, product_id: str) -> dict:
    product = semantics.get_semantic_product(product_id)
    return {
        "kind": "semantic_product",
        "product_id": product["id"],
        "reference_version": product["reference_version"],
        "reference_digest": product["reference_digest"],
        "content_digest": product["content_digest"],
        "sample_ids": product["sample_ids"],
        "records": [
            {
                "sample_id": item["sample_id"],
                "received_revision": item["received_revision"],
                "original_bits": item["bits"],
                "kind": item["kind"],
                "sign": item["sign"],
                "payload": item["payload"],
                "exact_value": item["value"],
                "negated_value": item["negated_value"],
                "expected": item["expected"],
                "tolerance": item["tolerance"],
                "status": item["status"],
                "difference": item["difference"],
                "abs_difference": item["abs_difference"],
            }
            for item in product["items"]
        ],
    }


def public_delivery(delivery: DeliveryService, delivery_id: str) -> dict:
    view = delivery.get_delivery(delivery_id)
    return {
        "kind": "delivery",
        "delivery_id": view["delivery_id"],
        "canonical_product": {
            "product_id": view["product_id"],
            "content_digest": view["product_digest"],
        },
        "reference": {
            "version": view["reference_version"],
            "content_digest": view["reference_digest"],
        },
        "reader_contract": view["contract"],
        "contract_id": view["contract_id"],
        "content_digest": view["content_digest"],
        "lines": [
            {
                "sample_id": line["sample_id"],
                "original_bits": line["original_bits"],
                "kind": line["kind"],
                "sign": line["sign"],
                "original_exact_value": line["exact_value"],
                "original_negated_value": line["negated_value"],
                "semantic_status": line["status"],
                "delivered_status": line["delivered_status"],
                "coefficient": line["coefficient"],
                "scale": line["scale"],
                "reader_value": line["reader_value"],
                "exact_error": line["error"],
                "decoder": "coefficient / 10**scale",
                "evidence": line["evidence"],
            }
            for line in view["lines"]
        ],
    }


def verify_public_delivery(document: dict) -> list[str]:
    """Re-derive all checks from a standalone public document.

    Returns a list of textual problems (empty means the document is
    internally consistent).  It does not need a database: it recomputes the
    coefficient bound, error budget, scale legality and reader value.
    """
    problems: list[str] = []
    contract = document["reader_contract"]
    allowed = set(contract["allowed_scales"])
    bound = contract["max_abs_coefficient"]
    budget = Fraction(contract["max_abs_error"])
    required = {
        "sample_id", "original_bits", "kind", "sign", "original_exact_value",
        "semantic_status", "delivered_status", "coefficient", "scale",
        "reader_value", "exact_error",
    }
    for line in document["lines"]:
        missing = required - line.keys()
        if missing:
            problems.append(f"{line.get('sample_id')}: missing {sorted(missing)}")
            continue
        sample = line["sample_id"]
        scale = line["scale"]
        coefficient = line["coefficient"]
        if scale not in allowed:
            problems.append(f"{sample}: scale {scale} not allowed")
        if abs(coefficient) > bound:
            problems.append(f"{sample}: coefficient {coefficient} exceeds {bound}")
        if line["semantic_status"] != line["delivered_status"]:
            problems.append(f"{sample}: semantic status changed on delivery")
        value = Fraction(line["original_exact_value"])
        delivered = Fraction(coefficient, 10 ** scale)
        error = delivered - value
        if str(Fraction(line["exact_error"])) != str(error):
            problems.append(f"{sample}: stored error {line['exact_error']} != {error}")
        if abs(error) > budget:
            problems.append(f"{sample}: error {error} exceeds budget {budget}")
        if str(Fraction(line["reader_value"])) != str(delivered):
            problems.append(f"{sample}: reader value mismatch")
    return problems


def write_json(path: str | Path, document: dict) -> Path:
    target = Path(path)
    target.write_text(json.dumps(document, indent=2, ensure_ascii=False), encoding="utf-8")
    return target
