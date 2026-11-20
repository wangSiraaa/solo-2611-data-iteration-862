"""Run the supplied materials/739 input plus the two new delivery requests.

Usage::

    python3 -m obsdelivery.demo [--db /tmp/obs.sqlite] [path/to/input.json]

Everything is exact (``Fraction``); no host floating point is used for values,
tolerances, coefficients or error budgets.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from .contracts import ReaderContract
from .delivery import DeliveryService
from .errors import (
    DeliveryIdConflict,
    DeliveryRejected,
    ReferenceConflict,
    ReportConflict,
    ValidationError,
)
from .public import public_delivery, verify_public_delivery
from .service import SemanticsService
from .storage import Store

DEFAULT_INPUT = Path(__file__).resolve().parent.parent / "materials" / "739" / "input.json"

# The two appended requests from the task statement.
J0_CONTRACT = {"allowed_scales": [2, 3], "max_abs_coefficient": 99, "max_abs_error": "0"}
J1_CONTRACT = {"allowed_scales": [2, 3], "max_abs_coefficient": 99, "max_abs_error": "1/200"}
J_SAMPLES = ["s1", "s2"]


def _report_summary(report: dict) -> dict:
    return {
        "report_id": report["id"],
        "reference_version": report["reference_version"],
        "reference_digest": report["reference_digest"],
        "content_digest": report["content_digest"],
        "items": [
            {
                "sample_id": item["sample_id"],
                "bits": item["bits"],
                "kind": item["kind"],
                "sign": item["sign"],
                "value": item["value"],
                "expected": item["expected"],
                "tolerance": item["tolerance"],
                "status": item["status"],
                "difference": item["difference"],
            }
            for item in report["items"]
        ],
    }


def _interpretation_summary(semantics: SemanticsService) -> list[dict]:
    out = []
    for bits in ["3dcccccd", "80000000", "7fc00001", "7f800000", "00000001", "3e000000"]:
        view = semantics.interpret(bits)
        out.append(
            {
                "bits": view["bits"],
                "kind": view["kind"],
                "sign": view["sign"],
                "payload": view["payload"],
                "value": view["value_text"],
            }
        )
    return out


def run(input_path: Path, db_path: str | None = None) -> dict:
    payload = json.loads(input_path.read_text(encoding="utf-8"))
    store = Store(db_path or ":memory:")
    semantics = SemanticsService(store)
    deliveries = DeliveryService(store, semantics)
    log: dict = {"db": db_path or ":memory:", "interpretation": [], "steps": []}

    log["interpretation"] = _interpretation_summary(semantics)

    for record in payload["observations"]:
        try:
            result = semantics.admit_observations([record])
            log["steps"].append(
                {
                    "op": "admit_initial",
                    "sample_id": record["sample_id"],
                    "revision": record["revision"],
                    "bits": record["bits"],
                    "outcome": result["outcome"],
                }
            )
        except DeliveryRejected:  # pragma: no cover - impossible on init
            raise

    reference = payload["references"]
    if reference.get("version"):
        semantics.publish_reference(reference["version"], reference["items"], copy_from=None)

    for operation in payload["operations"]:
        op = operation["op"]
        if op == "report":
            try:
                report = semantics.report(operation["report_id"], operation["reference_version"])
                log["steps"].append({"op": "report", **_report_summary(report)})
            except ReportConflict:
                # The database is already in its terminal state (a second run
                # over the same store).  A fixed report is re-opened read-only
                # rather than recomputed against current observations.
                report = semantics.read_report(operation["report_id"])
                log["steps"].append(
                    {"op": "reopen_report", **_report_summary(report)}
                )
        elif op == "correct_observation":
            record = operation["record"]
            result = semantics.admit_observations([record])
            log["steps"].append(
                {
                    "op": "correct_observation",
                    "sample_id": record["sample_id"],
                    "revision": record["revision"],
                    "bits": record["bits"],
                    "outcome": result["outcome"],
                    "members": result["members"],
                }
            )
        elif op == "publish_reference":
            # The r2 derivation explicitly copies r1 (the version named in the
            # input); relying on "latest" would be ambiguous on a re-run.
            try:
                published = semantics.publish_reference(
                    operation["version"],
                    operation.get("replace", []),
                    copy_from="r1",
                )
            except ReferenceConflict:
                published = semantics.reference(operation["version"])
            log["steps"].append(
                {
                    "op": "publish_reference",
                    "version": published["version"],
                    "parent_version": published["parent_version"],
                    "content_digest": published["content_digest"],
                    "items": published["items"],
                }
            )
        elif op == "read_old_report":
            report = semantics.read_report(operation["report_id"])
            log["steps"].append({"op": "read_old_report", **_report_summary(report)})
        else:
            raise ValidationError(f"unknown operation {op!r}")

    # Separate service queries after all corrections must still see the
    # original ON the corrected value.
    log["steps"].append(
        {
            "op": "query_semantics",
            "s1_r2": semantics.judge("s1", "r2"),
            "s1_r1": semantics.judge("s1", "r1"),
            "s2_r2": semantics.judge("s2", "r2"),
        }
    )

    # Publish a canonical observation product that both delivery requests consume.
    product = semantics.publish_semantic_product("prod-r2-s1s2", J_SAMPLES, "r2")
    log["steps"].append(
        {
            "op": "publish_semantic_product",
            "product_id": product["id"],
            "reference_version": product["reference_version"],
            "reference_digest": product["reference_digest"],
            "content_digest": product["content_digest"],
            "items": product["items"],
        }
    )

    # J0: expected to be rejected as a whole.
    j0 = ReaderContract.from_mapping(J0_CONTRACT)
    try:
        deliveries.submit("J0", product["id"], J_SAMPLES, j0)
        log["steps"].append({"op": "delivery", "request": "J0", "published": True})
    except DeliveryRejected as exc:
        log["steps"].append(
            {
                "op": "delivery",
                "request": "J0",
                "published": False,
                "delivery_id": exc.delivery_id,
                "attempt_id": exc.attempt_id,
                "reasons": exc.reasons,
                "attempts": deliveries.attempts("J0"),
            }
        )

    # J1: expected to succeed; s1 -> 12/scale2, s2 -> 0 keeping negative-zero bits.
    j1 = ReaderContract.from_mapping(J1_CONTRACT)
    j1_delivery = deliveries.submit("J1", product["id"], J_SAMPLES, j1)
    log["steps"].append({"op": "delivery", "request": "J1", **_delivery_summary(j1_delivery)})

    # Replaying J1 with the same identity must return the same stored result.
    replay = deliveries.submit("J1", product["id"], J_SAMPLES, j1)
    log["steps"].append(
        {
            "op": "delivery_replay",
            "request": "J1",
            "created": replay["created"],
            "same_content_digest": replay["content_digest"] == j1_delivery["content_digest"],
        }
    )

    # A different contract under the same delivery id must be refused.
    try:
        deliveries.submit("J1", product["id"], J_SAMPLES, ReaderContract.from_mapping(J0_CONTRACT))
        conflict = False
    except DeliveryIdConflict:
        conflict = True
    log["steps"].append({"op": "delivery_id_conflict", "request": "J1", "refused": conflict})

    # A standalone public document that an independent reader can verify.
    public_doc = public_delivery(deliveries, "J1")
    log["steps"].append(
        {
            "op": "public_delivery",
            "delivery_id": public_doc["delivery_id"],
            "content_digest": public_doc["content_digest"],
            "reader_problems": verify_public_delivery(public_doc),
            "lines": [
                {
                    "sample_id": line["sample_id"],
                    "original_bits": line["original_bits"],
                    "coefficient": line["coefficient"],
                    "scale": line["scale"],
                    "reader_value": line["reader_value"],
                    "exact_error": line["exact_error"],
                    "semantic_status": line["semantic_status"],
                }
                for line in public_doc["lines"]
            ],
        }
    )

    # Rejecting J0 must not disturb the semantics service: s1 still PASS under r2.
    log["steps"].append(
        {
            "op": "post_rejection_semantics",
            "s1_r2_status": semantics.judge("s1", "r2")["status"],
            "s1_r2_bits": semantics.judge("s1", "r2")["bits"],
        }
    )

    store.close()
    return log


def _delivery_summary(delivery: dict) -> dict:
    return {
        "delivery_id": delivery["delivery_id"],
        "product_id": delivery["product_id"],
        "product_digest": delivery["product_digest"],
        "reference_version": delivery["reference_version"],
        "reference_digest": delivery["reference_digest"],
        "contract_id": delivery["contract_id"],
        "content_digest": delivery["content_digest"],
        "created": delivery["created"],
        "lines": [
            {
                "sample_id": line["sample_id"],
                "original_bits": line["original_bits"],
                "kind": line["kind"],
                "sign": line["sign"],
                "exact_value": line["exact_value"],
                "negated_value": line["negated_value"],
                "status": line["status"],
                "delivered_status": line["delivered_status"],
                "coefficient": line["coefficient"],
                "scale": line["scale"],
                "error": line["error"],
                "reader_value": line["reader_value"],
            }
            for line in delivery["lines"]
        ],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", nargs="?", default=str(DEFAULT_INPUT))
    parser.add_argument("--db", help="SQLite path (default: in-memory)")
    parser.add_argument("--json", action="store_true", help="emit JSON instead of text")
    args = parser.parse_args(argv)
    result = run(Path(args.input), args.db)
    if args.json:
        print(json.dumps(result, indent=2, ensure_ascii=False, default=str))
        return 0
    _print_text(result)
    return 0


def _print_text(log: dict) -> None:
    print(f"store: {log['db']}")
    print("\ninterpretation of raw bit patterns")
    for item in log["interpretation"]:
        print(
            f"  {item['bits']}  kind={item['kind']:<8} sign={item['sign']} "
            f"payload={item['payload']}  value={item['value']}"
        )
    print("\noperations")
    for step in log["steps"]:
        op = step["op"]
        if op in ("report", "reopen_report"):
            tag = "report" if op == "report" else "reopen report"
            print(f"\n[{tag} {step['report_id']}] reference={step['reference_version']} "
                  f"digest={step['content_digest'][:12]}")
            for item in step["items"]:
                print(
                    f"  {item['sample_id']}: bits={item['bits']} kind={item['kind']} "
                    f"sign={item['sign']} value={item['value']} expected={item['expected']} "
                    f"status={item['status']} diff={item['difference']}"
                )
        elif op in ("correct_observation", "admit_initial"):
            print(f"\n[{op}] {step['sample_id']} r{step['revision']} bits={step['bits']} "
                  f"-> {step['outcome']}")
        elif op == "publish_reference":
            print(f"\n[publish_reference] {step['version']} (parent {step['parent_version']}) "
                  f"digest={step['content_digest'][:12]}")
            for item in step["items"]:
                print(f"  {item['sample_id']}: expected={item['expected']} "
                      f"tolerance={item['tolerance']}")
        elif op == "read_old_report":
            print(f"\n[read_old_report {step['report_id']}] immutable contents:")
            for item in step["items"]:
                print(f"  {item['sample_id']}: bits={item['bits']} value={item['value']} "
                      f"expected={item['expected']} status={item['status']} diff={item['difference']}")
        elif op == "query_semantics":
            print("\n[independent semantics queries]")
            for label in ("s1_r1", "s1_r2", "s2_r2"):
                item = step[label]
                print(f"  {label}: bits={item['bits']} status={item['status']} "
                      f"value={item['value']} expected={item['expected']} "
                      f"diff={item['difference']}")
        elif op == "publish_semantic_product":
            print(f"\n[publish_semantic_product] {step['product_id']} "
                  f"digest={step['content_digest'][:12]}")
            for item in step["items"]:
                print(f"  {item['sample_id']}: bits={item['bits']} value={item['value']} "
                      f"sign={item['sign']} status={item['status']}")
        elif op == "delivery" and not step.get("published", True):
            print(f"\n[delivery {step['request']}] REJECTED (no rows published)")
            for reason in step["reasons"]:
                print(f"  {reason['sample_id']}: {reason['reason']}")
                for trial in reason.get("scale_trials", []):
                    print(f"    scale={trial['scale']} coef={trial['coefficient']} "
                          f"error={trial['error']} coef_ok={trial['coefficient_ok']} "
                          f"err_ok={trial['error_ok']}")
        elif op == "delivery":
            print(f"\n[delivery {step['request']}] PUBLISHED {step['delivery_id']} "
                  f"digest={step['content_digest'][:12]}")
            for line in step["lines"]:
                print(
                    f"  {line['sample_id']}: bits={line['original_bits']} sign={line['sign']} "
                    f"value={line['exact_value']} status={line['status']} -> "
                    f"coefficient={line['coefficient']} scale={line['scale']} "
                    f"reader={line['reader_value']} error={line['error']}"
                )
        elif op == "delivery_replay":
            print(f"\n[delivery replay {step['request']}] created={step['created']} "
                  f"same_digest={step['same_content_digest']}")
        elif op == "delivery_id_conflict":
            print(f"\n[delivery id reuse {step['request']}] refused={step['refused']}")
        elif op == "public_delivery":
            print(f"\n[public delivery {step['delivery_id']}] "
                  f"independent reader problems={step['reader_problems'] or 'none'}")
            for line in step["lines"]:
                print(f"  {line['sample_id']}: {line['coefficient']}/{10**line['scale']} "
                      f"error={line['exact_error']} status={line['semantic_status']} "
                      f"bits={line['original_bits']}")
        elif op == "post_rejection_semantics":
            print(f"\n[post-rejection] s1/r2 status={step['s1_r2_status']} "
                  f"bits={step['s1_r2_bits']}")
        else:
            print(f"\n[{op}] {step}")


if __name__ == "__main__":
    raise SystemExit(main())
