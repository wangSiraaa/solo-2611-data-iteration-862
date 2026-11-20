"""Delivery library: turn a *published* semantic product into reader lines.

The delivery layer never re-interprets the original bits and never re-selects
a representation from the current observation state.  It consumes one explicit
semantic product, a requested sample set and an independent reader contract.

Fail-closed semantics
---------------------
* Every requested record must be present in the product.
* A finite record with a legal ``coefficient/10**scale`` under the contract is
  delivered with ``delivered_status`` derived from the *original* semantic
  status (``OUTSIDE`` stays ``OUTSIDE`` even if the delivered decimal happens
  to equal the expected value).
* Non-finite records and ``MISSING`` records have no finite decimal form and
  therefore no legal representation -> the whole task is rejected.
* If any record is not representable, no delivery row is written and no
  successful ``delivery_id`` is consumed.  A complete audit row is recorded in
  ``delivery_attempts`` instead.
"""

from __future__ import annotations

import json
from fractions import Fraction
from typing import Sequence

from . import semantics
from .canonical import canonical_json, digest, frac_str
from .contracts import ReaderContract
from .delivery_algo import Selection, select_representation
from .errors import (
    DeliveryIdConflict,
    DeliveryNotFound,
    DeliveryRejected,
    IntegrityError,
    ProductNotFound,
    ValidationError,
)
from .service import SemanticsService
from .storage import Store, utcnow

COEFFICIENT_BOUND = "max_abs_coefficient"
ERROR_BUDGET = "max_abs_error"
SIGN_BITS = "signed_zero_or_negative_bits"


def _line_from_item(
    item: dict, contract: ReaderContract
) -> tuple[dict | None, dict]:
    """Compute one delivery line; return ``(line_or_None, rejection_reason_or_None)``."""
    sample = item["sample_id"]
    status = item["status"]
    kind = item["kind"]

    if status == semantics.MISSING:
        return None, {
            "sample_id": sample,
            "reason": "missing_observation",
            "detail": "MISSING has no finite decimal representation",
        }
    if kind in semantics.NONFINITE_KINDS:
        return None, {
            "sample_id": sample,
            "reason": "nonfinite",
            "kind": kind,
            "bits": item["bits"],
            "detail": f"{kind} cannot be encoded as coefficient/10^scale",
        }
    if item["value"] is None:
        # Defensive: any record without an exact finite value is unrepresentable.
        return None, {
            "sample_id": sample,
            "reason": "nonfinite_or_missing",
            "kind": kind,
            "bits": item["bits"],
        }

    sign = item["sign"]
    original = Fraction(item["value"])
    magnitude = abs(original)
    selection: Selection = select_representation(
        magnitude,
        contract.allowed_scales,
        contract.max_abs_coefficient,
        contract.max_abs_error,
    )
    if not selection.feasible:
        return None, {
            "sample_id": sample,
            "reason": "no_legal_representation",
            "bits": item["bits"],
            "value": item["value"],
            "readable": kind == semantics.FINITE,
            "scale_trials": [attempt.canonical() for attempt in selection.attempts],
        }

    attempt = selection.chosen
    coefficient = attempt.coefficient if sign == 0 else -attempt.coefficient
    error = attempt.error  # truncation error vs. magnitude (sign-symmetric)
    signed_delivered = Fraction(coefficient, 10 ** attempt.scale)
    signed_error = signed_delivered - original
    if signed_error != -error and signed_error != error:
        raise IntegrityError(f"internal: sign error mismatch for {sample}")

    line = {
        "sample_id": sample,
        "original_bits": item["bits"],
        "kind": kind,
        "sign": sign,
        "value_sign_evidence": {
            "kind": SIGN_BITS,
            "original_bits": item["bits"],
            "sign": sign,
            "note": (
                "an integer coefficient cannot carry -0; the negative-zero "
                "identity is preserved by original_bits and sign"
            ) if (kind == semantics.ZERO and sign == 1) else None,
        },
        "exact_value": item["value"],
        "negated_value": item["negated_value"],
        "status": status,
        "delivered_status": status,
        "coefficient": coefficient,
        "scale": attempt.scale,
        "error": frac_str(signed_error),
        "abs_error": frac_str(error),
        "reader_value": frac_str(signed_delivered),
        "reader_formula": "coefficient/10^scale",
        "evidence": {
            "original_value": item["value"],
            "magnitude": frac_str(magnitude),
            "scale_trials": [t.canonical() for t in selection.attempts],
            "chosen": attempt.canonical(),
            "coefficient_bound": contract.max_abs_coefficient,
            "error_budget": frac_str(contract.max_abs_error),
            "checks": {
                "coefficient_within_bound": attempt.coefficient_ok,
                "error_within_budget": attempt.error_ok,
                # Coef 0 (including -0) has no sign to lose; the sign lives in
                # original_bits.  Non-zero magnitudes must round to a
                # same-sign coefficient.
                "sign_preserved": (
                    coefficient == 0 or (coefficient < 0) == (sign == 1)
                ),
                "negative_zero_bits_preserved": not (
                    kind == semantics.ZERO and sign == 1
                ) or item["bits"] == "80000000",
            },
            "rounding": "nearest, ties-to-even",
            "selection_policy": "ascending_scale_first_feasible",
        },
    }
    return line, None


class DeliveryService:
    """Second responsibility: contract-driven decimal production."""

    def __init__(self, store: Store, semantics_service: SemanticsService | None = None):
        self.store = store
        self.semantics = semantics_service or SemanticsService(store)

    def plan(
        self,
        product_id: str,
        sample_ids: Sequence[str],
        contract: ReaderContract,
    ) -> dict:
        """Compute the full selection without publishing anything."""
        product = self.semantics.get_semantic_product(product_id)
        items = {item["sample_id"]: item for item in product["items"]}
        if isinstance(sample_ids, (str, bytes)) or not isinstance(sample_ids, Sequence):
            raise ValidationError("sample_ids must be a sequence")
        samples = list(sample_ids)
        if not samples:
            raise ValidationError("sample_ids must be non-empty")
        if len(set(samples)) != len(samples):
            raise ValidationError("sample_ids contains duplicates")

        missing = [sample for sample in samples if sample not in items]
        if missing:
            raise ValidationError(
                f"product {product_id!r} does not contain sample(s) {missing}"
            )

        lines: list[dict] = []
        reasons: list[dict] = []
        for sample in sorted(samples):
            line, reason = _line_from_item(items[sample], contract)
            if line is not None:
                lines.append(line)
            if reason is not None:
                reasons.append(reason)
        request_body = self._request_body(product_id, sorted(samples), contract)
        return {
            "product_id": product_id,
            "product_digest": product["content_digest"],
            "reference_version": product["reference_version"],
            "reference_digest": product["reference_digest"],
            "content_digest": digest({"request": request_body, "lines": lines}),
            "contract": contract,
            "lines": lines,
            "reasons": reasons,
            "request_body": request_body,
        }

    @staticmethod
    def _request_body(product_id: str, samples: Sequence[str], contract: ReaderContract) -> dict:
        return {
            "product_id": product_id,
            "sample_ids": list(samples),
            "contract": contract.canonical(),
            "contract_id": contract.contract_id(),
        }

    def submit(
        self,
        delivery_id: str,
        product_id: str,
        sample_ids: Sequence[str],
        contract: ReaderContract,
    ) -> dict:
        """All-or-nothing delivery.  Raises ``DeliveryRejected`` on failure."""
        if not isinstance(delivery_id, str) or not delivery_id:
            raise ValidationError("delivery_id must be a non-empty string")
        plan = self.plan(product_id, sample_ids, contract)
        request_digest = digest(plan["request_body"])

        existing = self.store.conn.execute(
            "SELECT * FROM deliveries WHERE delivery_id=?", (delivery_id,)
        ).fetchone()
        if existing is not None:
            if existing["request_digest"] != request_digest:
                raise DeliveryIdConflict(
                    f"delivery {delivery_id!r} exists for a different product/sample set/contract"
                )
            return self._view(existing, created=False)

        if plan["reasons"]:
            attempt_id = self._record_attempt(
                delivery_id, product_id, request_digest, "rejected", plan["reasons"]
            )
            raise DeliveryRejected(delivery_id, plan["reasons"], attempt_id=attempt_id)

        lines = plan["lines"]
        content_body = {
            "product_id": product_id,
            "product_digest": plan["product_digest"],
            "reference_version": plan["reference_version"],
            "reference_digest": plan["reference_digest"],
            "sample_ids": sorted(sample_ids),
            "contract": contract.canonical(),
            "contract_id": contract.contract_id(),
            "lines": lines,
        }
        content_digest = digest(content_body)
        with self.store.transaction() as conn:
            conn.execute(
                "INSERT INTO deliveries "
                "(delivery_id, product_id, reference_version, sample_ids_json, "
                " contract_json, contract_id, request_digest, lines_json, "
                " content_digest, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    delivery_id,
                    product_id,
                    plan["reference_version"],
                    canonical_json(sorted(sample_ids)),
                    canonical_json(contract.canonical()),
                    contract.contract_id(),
                    request_digest,
                    canonical_json(lines),
                    content_digest,
                    utcnow(),
                ),
            )
            for ordinal, line in enumerate(lines):
                conn.execute(
                    "INSERT INTO delivery_line "
                    "(delivery_id, ordinal, sample_id, original_bits, kind, value_sign, "
                    " exact_value, status, delivered_status, coefficient, scale, error, "
                    " reader_value, evidence_json) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        delivery_id,
                        ordinal,
                        line["sample_id"],
                        line["original_bits"],
                        line["kind"],
                        line["sign"],
                        line["exact_value"],
                        line["status"],
                        line["delivered_status"],
                        line["coefficient"],
                        line["scale"],
                        line["error"],
                        line["reader_value"],
                        canonical_json(line["evidence"]),
                    ),
                )
            conn.execute(
                "INSERT INTO delivery_attempts "
                "(delivery_id, product_id, request_digest, status, reasons_json, created_at) "
                "VALUES (?, ?, ?, 'published', ?, ?)",
                (delivery_id, product_id, request_digest, canonical_json([]), utcnow()),
            )
        row = self.store.conn.execute(
            "SELECT * FROM deliveries WHERE delivery_id=?", (delivery_id,)
        ).fetchone()
        return self._view(row, created=True)

    def _record_attempt(
        self, delivery_id: str, product_id: str, request_digest: str, status: str, reasons: list
    ) -> int:
        with self.store.transaction() as conn:
            cur = conn.execute(
                "INSERT INTO delivery_attempts "
                "(delivery_id, product_id, request_digest, status, reasons_json, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (delivery_id, product_id, request_digest, status, canonical_json(reasons), utcnow()),
            )
            return cur.lastrowid

    def get_delivery(self, delivery_id: str) -> dict:
        row = self.store.conn.execute(
            "SELECT * FROM deliveries WHERE delivery_id=?", (delivery_id,)
        ).fetchone()
        if row is None:
            raise DeliveryNotFound(delivery_id)
        return self._view(row, created=False)

    def _view(self, row, created: bool) -> dict:
        """Independently read back and verify every stored line."""
        lines = self._load_lines(row)
        contract = ReaderContract.from_mapping(json.loads(row["contract_json"]))
        content_body = {
            "product_id": row["product_id"],
            "product_digest": None,
            "reference_version": row["reference_version"],
            "reference_digest": None,
            "sample_ids": json.loads(row["sample_ids_json"]),
            "contract": contract.canonical(),
            "contract_id": contract.contract_id(),
            "lines": lines,
        }
        product = self._product_header(row["product_id"])
        content_body["product_digest"] = product["content_digest"]
        content_body["reference_digest"] = product["reference_digest"]
        actual = digest(content_body)
        if actual != row["content_digest"]:
            raise IntegrityError(
                f"delivery {row['delivery_id']!r} digest mismatch ({actual} != {row['content_digest']})"
            )
        for line in lines:
            self._verify_line(line, contract)
        return {
            "delivery_id": row["delivery_id"],
            "product_id": row["product_id"],
            "product_digest": product["content_digest"],
            "reference_version": row["reference_version"],
            "reference_digest": product["reference_digest"],
            "sample_ids": json.loads(row["sample_ids_json"]),
            "contract": contract.canonical(),
            "contract_id": contract.contract_id(),
            "lines": lines,
            "content_digest": row["content_digest"],
            "created": created,
        }

    def _product_header(self, product_id: str) -> dict:
        row = self.store.conn.execute(
            "SELECT content_digest, reference_version FROM semantic_products WHERE product_id=?",
            (product_id,),
        ).fetchone()
        if row is None:
            raise ProductNotFound(product_id)
        ref = self.store.conn.execute(
            "SELECT content_digest FROM reference_version WHERE version=?",
            (row["reference_version"],),
        ).fetchone()
        return {
            "content_digest": row["content_digest"],
            "reference_digest": ref["content_digest"],
            "reference_version": row["reference_version"],
        }

    def _load_lines(self, delivery_row) -> list[dict]:
        """Authoritative immutable line payload, plus a projection cross-check."""
        lines = json.loads(delivery_row["lines_json"])
        rows = self.store.conn.execute(
            "SELECT sample_id, original_bits, kind, value_sign, exact_value, status, "
            "delivered_status, coefficient, scale, error, reader_value "
            "FROM delivery_line WHERE delivery_id=? ORDER BY ordinal",
            (delivery_row["delivery_id"],),
        ).fetchall()
        if len(rows) != len(lines):
            raise IntegrityError(
                f"delivery {delivery_row['delivery_id']!r}: line table/count mismatch"
            )
        for row, line in zip(rows, lines):
            projection = {
                "sample_id": row["sample_id"],
                "original_bits": row["original_bits"],
                "kind": row["kind"],
                "sign": row["value_sign"],
                "exact_value": row["exact_value"],
                "status": row["status"],
                "delivered_status": row["delivered_status"],
                "coefficient": row["coefficient"],
                "scale": row["scale"],
                "error": row["error"],
                "reader_value": row["reader_value"],
            }
            for key, value in projection.items():
                if line.get(key) != value:
                    raise IntegrityError(
                        f"delivery {delivery_row['delivery_id']!r}: column {key!r} "
                        f"disagrees with canonical line for {line.get('sample_id')!r}"
                    )
        return lines

    @staticmethod
    def _verify_line(line: dict, contract: ReaderContract) -> None:
        """Independent reader check: recompute the delivered decimal exactly."""
        coefficient = line["coefficient"]
        scale = line["scale"]
        if scale not in contract.allowed_scales:
            raise IntegrityError(f"{line['sample_id']}: scale {scale} not allowed")
        if abs(coefficient) > contract.max_abs_coefficient:
            raise IntegrityError(f"{line['sample_id']}: coefficient exceeds bound")
        if isinstance(coefficient, bool) or not isinstance(coefficient, int):
            raise IntegrityError(f"{line['sample_id']}: coefficient is not an integer")
        delivered = Fraction(coefficient, 10 ** scale)
        original = Fraction(line["exact_value"])
        exact_error = delivered - original
        if frac_str(exact_error) != line["error"]:
            raise IntegrityError(f"{line['sample_id']}: stored error disagrees with reader value")
        if abs(exact_error) > contract.max_abs_error:
            raise IntegrityError(f"{line['sample_id']}: error exceeds contract budget")
        if frac_str(delivered) != line["reader_value"]:
            raise IntegrityError(f"{line['sample_id']}: reader value mismatch")
        # Delivering must not launder an OUTSIDE verdict into PASS.
        if line["status"] != line["delivered_status"]:
            raise IntegrityError(f"{line['sample_id']}: semantic status changed on delivery")

    def delivery_lines(self, delivery_id: str) -> list[dict]:
        return self.get_delivery(delivery_id)["lines"]

    def attempts(self, delivery_id: str) -> list[dict]:
        rows = self.store.conn.execute(
            "SELECT attempt_id, delivery_id, product_id, request_digest, status, "
            "reasons_json, created_at FROM delivery_attempts "
            "WHERE delivery_id=? ORDER BY attempt_id",
            (delivery_id,),
        ).fetchall()
        out = []
        for row in rows:
            out.append(
                {
                    "attempt_id": row["attempt_id"],
                    "delivery_id": row["delivery_id"],
                    "product_id": row["product_id"],
                    "request_digest": row["request_digest"],
                    "status": row["status"],
                    "reasons": json.loads(row["reasons_json"]),
                    "created_at": row["created_at"],
                }
            )
        return out
