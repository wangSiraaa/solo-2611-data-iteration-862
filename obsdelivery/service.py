"""Observation semantics service: admission, references, reports, products.

This module is deliberately usable without the delivery library.  It answers
"what does this original bit pattern mean and how does it compare to the
reference" and produces immutable semantic products; it never rounds and never
touches a decimal reader contract.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from fractions import Fraction
from typing import Any, Iterable, Sequence

from . import semantics
from .canonical import canonical_json, digest, frac_str, parse_fraction
from .errors import (
    BatchRejected,
    IntegrityError,
    ProductConflict,
    ProductNotFound,
    ReferenceConflict,
    ReferenceNotFound,
    ReportConflict,
    ReportNotFound,
    ValidationError,
)
from .storage import Store, utcnow


# ---------------------------------------------------------------------------
# request validation helpers
# ---------------------------------------------------------------------------

def _require_sample_id(value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValidationError(f"sample_id must be a non-empty string, got {value!r}")
    return value


def _require_revision(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValidationError(f"revision must be a positive integer, got {value!r}")
    return value


def _require_bits(value: Any) -> tuple[str, int]:
    try:
        raw = semantics.parse_bits(value)
    except ValueError as exc:
        raise ValidationError(str(exc)) from exc
    return f"{raw:08x}", raw


@dataclass(frozen=True)
class ObservationEntry:
    sample_id: str
    revision: int
    bits: str
    raw: int


#: Sentinel meaning "derive from the latest published reference version".
LATEST = object()


def _normalise_entries(records: Iterable[dict]) -> list[ObservationEntry]:
    if isinstance(records, (str, bytes)) or not isinstance(records, Iterable):
        raise ValidationError("observations must be an iterable of objects")
    entries: dict[tuple[str, int], ObservationEntry] = {}
    for record in records:
        if not isinstance(record, dict):
            raise ValidationError(f"observation record is not an object: {record!r}")
        sample = _require_sample_id(record.get("sample_id"))
        revision = _require_revision(record.get("revision"))
        bits, raw = _require_bits(record.get("bits"))
        key = (sample, revision)
        if key in entries:
            if entries[key].bits != bits:
                raise BatchRejected(
                    [
                        {
                            "reason": "duplicate_revision_conflict",
                            "sample_id": sample,
                            "revision": revision,
                            "bits": [entries[key].bits, bits],
                        }
                    ]
                )
            continue
        entries[key] = ObservationEntry(sample, revision, bits, raw)
    if not entries:
        raise ValidationError("observation batch must contain at least one record")
    return list(entries.values())


# ---------------------------------------------------------------------------
# canonical row builders
# ---------------------------------------------------------------------------

def _item_canonical(
    sample_id: str,
    effective: tuple[int, semantics.Decoded] | None,
    expected: Fraction,
    tolerance: Fraction,
    *,
    negated: bool = False,
) -> dict[str, Any]:
    expected_text = frac_str(expected)
    tolerance_text = frac_str(tolerance)
    if effective is None:
        return {
            "sample_id": sample_id,
            "received_revision": None,
            "bits": None,
            "kind": semantics.MISSING,
            "sign": None,
            "payload": None,
            "value": None,
            "negated_value": None,
            "expected": expected_text,
            "tolerance": tolerance_text,
            "status": semantics.MISSING,
            "difference": None,
            "abs_difference": None,
        }
    revision, decoded = effective
    status, distance = semantics.judge(decoded, expected, tolerance)
    value = decoded.value
    difference = None if value is None else value - expected
    return {
        "sample_id": sample_id,
        "received_revision": revision,
        "bits": decoded.bits,
        "kind": decoded.kind,
        "sign": decoded.sign,
        "payload": decoded.payload,
        "value": None if value is None else frac_str(value),
        "negated_value": None if value is None else frac_str(-value),
        "expected": expected_text,
        "tolerance": tolerance_text,
        "status": status,
        "difference": None if difference is None else frac_str(difference),
        "abs_difference": None if distance is None else frac_str(distance),
    }


class SemanticsService:
    """Service over one SQLite store."""


    def __init__(self, store: Store):
        self.store = store

    # -- pure interpretation -------------------------------------------------
    @staticmethod
    def interpret(bits: str) -> dict:
        return semantics.interpret(bits)

    @staticmethod
    def decode(bits: str) -> semantics.Decoded:
        return semantics.decode(semantics.parse_bits(bits))

    # -- observation state ---------------------------------------------------
    def effective_observation(self, sample_id: str) -> tuple[int, semantics.Decoded] | None:
        row = self.store.conn.execute(
            "SELECT revision, bits FROM observations WHERE sample_id=? "
            "ORDER BY revision DESC LIMIT 1",
            (sample_id,),
        ).fetchone()
        if row is None:
            return None
        return row["revision"], semantics.decode(semantics.parse_bits(row["bits"]))

    def effective_revision(self, sample_id: str) -> int | None:
        row = self.store.conn.execute(
            "SELECT MAX(revision) AS rev FROM observations WHERE sample_id=?",
            (sample_id,),
        ).fetchone()
        return None if row is None or row["rev"] is None else row["rev"]

    def observations(self) -> list[dict]:
        rows = self.store.conn.execute(
            "SELECT sample_id, revision, bits FROM observations ORDER BY sample_id, revision"
        ).fetchall()
        return [dict(row) for row in rows]

    # -- admission -----------------------------------------------------------
    def admit_observations(self, records: Iterable[dict]) -> dict:
        """Admit a batch atomically under highest-revision-wins rules."""
        entries = _normalise_entries(records)
        ordered = sorted(entries, key=lambda e: (e.sample_id, e.revision))
        batch_body = [
            {"sample_id": e.sample_id, "revision": e.revision, "bits": e.bits}
            for e in ordered
        ]
        batch_digest = digest(batch_body)
        existing = self.store.conn.execute(
            "SELECT * FROM obs_admissions WHERE batch_digest=?", (batch_digest,)
        ).fetchone()
        if existing is not None:
            return self._admission_view(existing, created=False)

        conflicts = []
        plans: list[dict] = []
        for entry in ordered:
            current_rev = self.effective_revision(entry.sample_id)
            same = self.store.conn.execute(
                "SELECT bits FROM observations WHERE sample_id=? AND revision=?",
                (entry.sample_id, entry.revision),
            ).fetchone()
            if same is not None and same["bits"] != entry.bits:
                conflicts.append(
                    {
                        "reason": "revision_conflict",
                        "sample_id": entry.sample_id,
                        "revision": entry.revision,
                        "stored_bits": same["bits"],
                        "offered_bits": entry.bits,
                    }
                )
                continue
            if current_rev is None or entry.revision > current_rev:
                disposition = "new"
            elif entry.revision == current_rev:
                disposition = "replayed"
            else:
                disposition = "ignored_older"
            plans.append(
                {
                    "sample_id": entry.sample_id,
                    "revision": entry.revision,
                    "bits": entry.bits,
                    "raw": entry.raw,
                    "disposition": disposition,
                }
            )

        canonical = {"batch": batch_body, "conflicts": conflicts}
        with self.store.transaction() as conn:
            if conflicts:
                # Whole batch rejected: record the admission and every member
                # (including the conflicting offer) for audit, mutate nothing.
                cur = conn.execute(
                    "INSERT INTO obs_admissions"
                    "(batch_digest, accepted, outcome, canonical, created_at) "
                    "VALUES (?, 0, 'rejected', ?, ?)",
                    (batch_digest, canonical_json(canonical), utcnow()),
                )
                admission_id = cur.lastrowid
                for entry in ordered:
                    conn.execute(
                        "INSERT INTO obs_batch_member"
                        "(admission_id, sample_id, revision, bits, disposition) "
                        "VALUES (?, ?, ?, ?, 'rejected')",
                        (admission_id, entry.sample_id, entry.revision, entry.bits),
                    )
            else:
                cur = conn.execute(
                    "INSERT INTO obs_admissions"
                    "(batch_digest, accepted, outcome, canonical, created_at) "
                    "VALUES (?, ?, 'accepted', ?, ?)",
                    (batch_digest, len(plans), canonical_json(canonical), utcnow()),
                )
                admission_id = cur.lastrowid
                for plan in plans:
                    conn.execute(
                        "INSERT INTO obs_batch_member"
                        "(admission_id, sample_id, revision, bits, disposition) "
                        "VALUES (?, ?, ?, ?, ?)",
                        (
                            admission_id,
                            plan["sample_id"],
                            plan["revision"],
                            plan["bits"],
                            plan["disposition"],
                        ),
                    )
                    if plan["disposition"] == "new":
                        conn.execute(
                            "INSERT INTO observations"
                            "(sample_id, revision, bits, raw, admission_id) "
                            "VALUES (?, ?, ?, ?, ?)",
                            (
                                plan["sample_id"],
                                plan["revision"],
                                plan["bits"],
                                plan["raw"],
                                admission_id,
                            ),
                        )
        row = self.store.conn.execute(
            "SELECT * FROM obs_admissions WHERE admission_id=?", (admission_id,)
        ).fetchone()
        result = self._admission_view(row, created=True)
        if conflicts:
            raise _AdmissionRejected(result)
        return result

    def _admission_view(self, row, created: bool) -> dict:
        members = self.store.conn.execute(
            "SELECT sample_id, revision, bits, disposition FROM obs_batch_member "
            "WHERE admission_id=? ORDER BY sample_id, revision",
            (row["admission_id"],),
        ).fetchall()
        return {
            "admission_id": row["admission_id"],
            "batch_digest": row["batch_digest"],
            "outcome": row["outcome"],
            "accepted_count": row["accepted"],
            "created": created,
            "members": [dict(m) for m in members],
        }

    def admit_operation(self, record: dict) -> dict:
        """Admission for a single correction operation.

        A rejected correction raises with its audit details; nothing is left
        half-applied.
        """
        try:
            return self.admit_observations([record])
        except _AdmissionRejected as exc:
            return exc.result

    # -- references ----------------------------------------------------------
    def latest_reference_version(self) -> str | None:
        row = self.store.conn.execute(
            "SELECT version FROM reference_version ORDER BY rowid DESC LIMIT 1"
        ).fetchone()
        return None if row is None else row["version"]

    def _reference_items(self, version: str) -> dict[str, dict]:
        rows = self.store.conn.execute(
            "SELECT sample_id, expected, tolerance FROM reference_item WHERE version=?",
            (version,),
        ).fetchall()
        return {
            row["sample_id"]: {
                "expected": parse_fraction(row["expected"], field="expected"),
                "tolerance": parse_fraction(row["tolerance"], field="tolerance"),
            }
            for row in rows
        }

    def reference(self, version: str) -> dict:
        row = self.store.conn.execute(
            "SELECT * FROM reference_version WHERE version=?", (version,)
        ).fetchone()
        if row is None:
            raise ReferenceNotFound(version)
        return self._reference_view(row)

    def _reference_view(self, row) -> dict:
        items = self.store.conn.execute(
            "SELECT sample_id, expected, tolerance FROM reference_item "
            "WHERE version=? ORDER BY sample_id",
            (row["version"],),
        ).fetchall()
        return {
            "version": row["version"],
            "parent_version": row["parent_version"],
            "content_digest": row["content_digest"],
            "items": [dict(item) for item in items],
        }

    def publish_reference(
        self,
        version: str,
        replace: Sequence[dict] | None = None,
        copy_from: str | None | object = LATEST,
    ) -> dict:
        """Publish a complete reference.

        ``copy_from`` selects the parent: the sentinel default copies the
        latest published version, an explicit version name copies that one,
        and an explicit ``None`` creates a version with no parent.  Listed
        items are replaced, unlisted items are kept.
        """
        if not isinstance(version, str) or not version:
            raise ValidationError("reference version must be a non-empty string")
        replace = list(replace or [])
        if copy_from is LATEST:
            parent = self.latest_reference_version()
        else:
            parent = copy_from
        if parent is not None and not isinstance(parent, str):
            raise ValidationError("copy_from must be a version string or None")

        base: dict[str, dict[str, str]] = {}
        if parent is not None:
            parent_row = self.store.conn.execute(
                "SELECT version FROM reference_version WHERE version=?", (parent,)
            ).fetchone()
            if parent_row is None:
                raise ReferenceNotFound(parent)
            items = self._reference_items(parent)
            base = {
                sample: {
                    "expected": frac_str(value["expected"]),
                    "tolerance": frac_str(value["tolerance"]),
                }
                for sample, value in items.items()
            }

        for item in replace:
            if not isinstance(item, dict):
                raise ValidationError(f"reference replacement is not an object: {item!r}")
            sample = _require_sample_id(item.get("sample_id"))
            try:
                expected = parse_fraction(item.get("expected"), field="expected")
                tolerance = parse_fraction(item.get("tolerance"), field="tolerance")
            except ValueError as exc:
                raise ValidationError(str(exc)) from exc
            if tolerance < 0:
                raise ValidationError(f"tolerance for {sample} must be non-negative")
            base[sample] = {
                "expected": frac_str(expected),
                "tolerance": frac_str(tolerance),
            }

        items_sorted = [
            {"sample_id": sample, "expected": base[sample]["expected"], "tolerance": base[sample]["tolerance"]}
            for sample in sorted(base)
        ]
        body = {"version": version, "parent_version": parent, "items": items_sorted}
        content_digest = digest(body)

        existing = self.store.conn.execute(
            "SELECT * FROM reference_version WHERE version=?", (version,)
        ).fetchone()
        if existing is not None:
            if existing["content_digest"] != content_digest:
                raise ReferenceConflict(
                    f"reference {version!r} exists with different content"
                )
            return self._reference_view(existing)

        with self.store.transaction() as conn:
            conn.execute(
                "INSERT INTO reference_version"
                "(version, parent_version, items_json, content_digest, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (version, parent, canonical_json(items_sorted), content_digest, utcnow()),
            )
            for item in items_sorted:
                conn.execute(
                    "INSERT INTO reference_item(version, sample_id, expected, tolerance) "
                    "VALUES (?, ?, ?, ?)",
                    (version, item["sample_id"], item["expected"], item["tolerance"]),
                )
        return self.reference(version)

    # -- judging / reports ---------------------------------------------------
    def judge(self, sample_id: str, reference_version: str) -> dict:
        reference = self._reference_items(reference_version)
        row = self._reference_row(reference_version)
        if sample_id not in reference:
            raise ValidationError(
                f"sample {sample_id!r} is not covered by reference {reference_version!r}"
            )
        item = reference[sample_id]
        effective = self.effective_observation(sample_id)
        view = _item_canonical(
            sample_id, effective, item["expected"], item["tolerance"]
        )
        view["reference_version"] = reference_version
        view["reference_digest"] = row["content_digest"]
        return view

    def _reference_row(self, version: str):
        row = self.store.conn.execute(
            "SELECT * FROM reference_version WHERE version=?", (version,)
        ).fetchone()
        if row is None:
            raise ReferenceNotFound(version)
        return row

    def _build_body(self, kind: str, ident: str, version: str, sample_ids: Sequence[str]) -> dict:
        reference = self._reference_items(version)
        row = self._reference_row(version)
        missing_ref = [sample for sample in sample_ids if sample not in reference]
        if missing_ref:
            raise ValidationError(
                f"reference {version!r} does not cover sample(s) {missing_ref}"
            )
        items = []
        for sample in sorted(sample_ids):
            item = reference[sample]
            effective = self.effective_observation(sample)
            items.append(
                _item_canonical(sample, effective, item["expected"], item["tolerance"])
            )
        return {
            "kind": kind,
            "id": ident,
            "reference_version": version,
            "reference_digest": row["content_digest"],
            "items": items,
        }

    @staticmethod
    def _store_report_item(conn, ident: str, item: dict) -> None:
        conn.execute(
            "INSERT INTO report_item "
            "(report_id, sample_id, received_revision, original_bits, status, kind, "
            " value_sign, exact_value, expected, tolerance, difference, abs_difference, "
            " canonical_json) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                ident,
                item["sample_id"],
                item["received_revision"],
                item["bits"],
                item["status"],
                item["kind"],
                item["sign"],
                item["value"],
                item["expected"],
                item["tolerance"],
                item["difference"],
                item["abs_difference"],
                canonical_json(item),
            ),
        )

    @staticmethod
    def _store_product_item(conn, ident: str, item: dict) -> None:
        conn.execute(
            "INSERT INTO semantic_product_item "
            "(product_id, sample_id, received_revision, original_bits, status, kind, "
            " value_sign, exact_value, expected, tolerance, difference, abs_difference, "
            " canonical_json) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                ident,
                item["sample_id"],
                item["received_revision"],
                item["bits"],
                item["status"],
                item["kind"],
                item["sign"],
                item["value"],
                item["expected"],
                item["tolerance"],
                item["difference"],
                item["abs_difference"],
                canonical_json(item),
            ),
        )

    @staticmethod
    def _load_items(conn, table: str, id_column: str, ident: str) -> list[dict]:
        rows = conn.execute(
            f"SELECT canonical_json FROM {table} WHERE {id_column}=? ORDER BY sample_id",
            (ident,),
        ).fetchall()
        return [json.loads(row["canonical_json"]) for row in rows]

    def _readback(self, body: dict, stored_digest: str, table: str, id_column: str, ident: str) -> dict:
        expected_body = {
            "kind": body["kind"],
            "id": body["id"],
            "reference_version": body["reference_version"],
            "reference_digest": body["reference_digest"],
            "items": body["items"],
        }
        actual = digest(expected_body)
        if actual != stored_digest:
            raise IntegrityError(
                f"{table}:{ident} digest mismatch ({actual} != {stored_digest})"
            )
        loaded = self._load_items(self.store.conn, table, id_column, ident)
        if canonical_json(loaded) != canonical_json(body["items"]):
            raise IntegrityError(f"{table}:{ident} reloaded items differ")
        result = dict(expected_body)
        result["content_digest"] = stored_digest
        result["verified"] = True
        return result

    def report(self, report_id: str, reference_version: str | None = None) -> dict:
        """Compute and persist (or replay) an immutable reference report."""
        if not isinstance(report_id, str) or not report_id:
            raise ValidationError("report_id must be a non-empty string")
        version = reference_version if reference_version is not None else self.latest_reference_version()
        if version is None:
            raise ReferenceNotFound("<none>")
        reference = self._reference_items(version)
        sample_ids = sorted(reference)
        body = self._build_body("report", report_id, version, sample_ids)
        content_digest = digest(body)

        existing = self.store.conn.execute(
            "SELECT * FROM reports WHERE report_id=?", (report_id,)
        ).fetchone()
        if existing is not None:
            if existing["content_digest"] != content_digest:
                raise ReportConflict(f"report {report_id!r} exists with different content")
            return self._report_view(existing, created=False)

        with self.store.transaction() as conn:
            conn.execute(
                "INSERT INTO reports"
                "(report_id, reference_version, content_digest, created_at) "
                "VALUES (?, ?, ?, ?)",
                (report_id, version, content_digest, utcnow()),
            )
            for item in body["items"]:
                self._store_report_item(conn, report_id, item)
        row = self.store.conn.execute(
            "SELECT * FROM reports WHERE report_id=?", (report_id,)
        ).fetchone()
        return self._report_view(row, created=True)

    def _report_view(self, row, created: bool) -> dict:
        items = self._load_items(self.store.conn, "report_item", "report_id", row["report_id"])
        body = {
            "kind": "report",
            "id": row["report_id"],
            "reference_version": row["reference_version"],
            "reference_digest": self._reference_row(row["reference_version"])["content_digest"],
            "items": items,
        }
        result = self._readback(body, row["content_digest"], "report_item", "report_id", row["report_id"])
        result["created"] = created
        return result

    def read_report(self, report_id: str) -> dict:
        """Read an old report back, re-verifying its stored content."""
        row = self.store.conn.execute(
            "SELECT * FROM reports WHERE report_id=?", (report_id,)
        ).fetchone()
        if row is None:
            raise ReportNotFound(report_id)
        return self._report_view(row, created=False)

    # -- semantic products ---------------------------------------------------
    def publish_semantic_product(
        self,
        product_id: str,
        sample_ids: Sequence[str],
        reference_version: str | None = None,
    ) -> dict:
        """Publish an immutable canonical observation product."""
        if not isinstance(product_id, str) or not product_id:
            raise ValidationError("product_id must be a non-empty string")
        if isinstance(sample_ids, (str, bytes)) or not isinstance(sample_ids, Sequence):
            raise ValidationError("sample_ids must be a sequence")
        samples = [_require_sample_id(sample) for sample in sample_ids]
        if len(set(samples)) != len(samples):
            raise ValidationError("sample_ids contains duplicates")
        samples = sorted(samples)
        version = reference_version if reference_version is not None else self.latest_reference_version()
        if version is None:
            raise ReferenceNotFound("<none>")

        body = self._build_body("semantic_product", product_id, version, samples)
        content_digest = digest(body)

        existing = self.store.conn.execute(
            "SELECT * FROM semantic_products WHERE product_id=?", (product_id,)
        ).fetchone()
        if existing is not None:
            if existing["content_digest"] != content_digest:
                raise ProductConflict(
                    f"semantic product {product_id!r} exists with different content"
                )
            return self._product_view(existing, created=False)

        with self.store.transaction() as conn:
            conn.execute(
                "INSERT INTO semantic_products"
                "(product_id, reference_version, sample_ids_json, content_digest, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (product_id, version, canonical_json(samples), content_digest, utcnow()),
            )
            for item in body["items"]:
                self._store_product_item(conn, product_id, item)
        row = self.store.conn.execute(
            "SELECT * FROM semantic_products WHERE product_id=?", (product_id,)
        ).fetchone()
        return self._product_view(row, created=True)

    def _product_view(self, row, created: bool) -> dict:
        items = self._load_items(
            self.store.conn, "semantic_product_item", "product_id", row["product_id"]
        )
        body = {
            "kind": "semantic_product",
            "id": row["product_id"],
            "reference_version": row["reference_version"],
            "reference_digest": self._reference_row(row["reference_version"])["content_digest"],
            "items": items,
        }
        result = self._readback(
            body, row["content_digest"], "semantic_product_item", "product_id", row["product_id"]
        )
        result["sample_ids"] = json.loads(row["sample_ids_json"])
        result["created"] = created
        return result

    def get_semantic_product(self, product_id: str) -> dict:
        row = self.store.conn.execute(
            "SELECT * FROM semantic_products WHERE product_id=?", (product_id,)
        ).fetchone()
        if row is None:
            raise ProductNotFound(product_id)
        return self._product_view(row, created=False)


class _AdmissionRejected(BatchRejected):
    """Internal: carries the audited rejected admission result."""

    def __init__(self, result: dict):
        self.result = result
        reasons = [
            {
                "reason": "revision_conflict",
                "admission_id": result["admission_id"],
                "members": result["members"],
            }
        ]
        super().__init__(reasons)
