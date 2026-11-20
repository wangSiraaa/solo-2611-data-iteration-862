"""Re-test endpoint service: revisable measurements, freezable endpoints.

Endpoints are collected per specimen over its pre-registered re-test
positions.  Measurements arrive as ``(specimen, position, revision)`` records
— either a bit-pattern value or a retraction — under the same admission rules
as the observation service: highest revision wins, identical replays write
nothing, a late lower revision is accepted but ignored, and the same revision
with different content rejects the whole batch atomically.

``endpoint()`` reports the live status: the contributing positions and their
bit patterns when the contract quantity is reached, or the shortfall (gap and
per-position reasons) when it is not.  ``freeze()`` pins a reached endpoint
into an immutable snapshot that stays readable after later corrections.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from fractions import Fraction
from typing import Any, Iterable

from . import endpoint_algo, semantics
from .canonical import canonical_json, digest, frac_str
from .errors import (
    IntegrityError,
    RetestBatchRejected,
    RetestPlanConflict,
    RetestPlanNotFound,
    SnapshotConflict,
    SnapshotNotFound,
    ValidationError,
)
from .storage import Store, utcnow


# ---------------------------------------------------------------------------
# request validation helpers
# ---------------------------------------------------------------------------

def _require_specimen_id(value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValidationError(f"specimen_id must be a non-empty string, got {value!r}")
    return value


def _require_position(value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValidationError(f"position must be a non-empty string, got {value!r}")
    return value


def _require_revision(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValidationError(f"revision must be a positive integer, got {value!r}")
    return value


@dataclass(frozen=True)
class MeasurementEntry:
    specimen_id: str
    position: str
    revision: int
    action: str  # endpoint_algo.ACTION_VALUE | ACTION_RETRACT
    bits: str | None
    raw: int | None

    def canonical(self) -> dict:
        return {
            "specimen_id": self.specimen_id,
            "position": self.position,
            "revision": self.revision,
            "action": self.action,
            "bits": self.bits,
        }


def _normalise_entries(records: Iterable[dict]) -> list[MeasurementEntry]:
    if isinstance(records, (str, bytes)) or not isinstance(records, Iterable):
        raise ValidationError("measurements must be an iterable of objects")
    entries: dict[tuple[str, str, int], MeasurementEntry] = {}
    for record in records:
        if not isinstance(record, dict):
            raise ValidationError(f"measurement record is not an object: {record!r}")
        specimen = _require_specimen_id(record.get("specimen_id"))
        position = _require_position(record.get("position"))
        revision = _require_revision(record.get("revision"))
        retract = record.get("retract", False)
        if not isinstance(retract, bool):
            raise ValidationError(f"retract must be a boolean, got {retract!r}")
        has_bits = record.get("bits") is not None
        if retract and has_bits:
            raise ValidationError("a retraction cannot carry bits")
        if not retract and not has_bits:
            raise ValidationError("a value measurement must carry bits")
        if retract:
            action, bits, raw = endpoint_algo.ACTION_RETRACT, None, None
        else:
            try:
                raw_value = semantics.parse_bits(record.get("bits"))
            except ValueError as exc:
                raise ValidationError(str(exc)) from exc
            action, bits, raw = endpoint_algo.ACTION_VALUE, f"{raw_value:08x}", raw_value
        key = (specimen, position, revision)
        if key in entries:
            previous = entries[key]
            if previous.action != action or previous.bits != bits:
                raise RetestBatchRejected(
                    [
                        {
                            "reason": "duplicate_revision_conflict",
                            "specimen_id": specimen,
                            "position": position,
                            "revision": revision,
                        }
                    ]
                )
            continue
        entries[key] = MeasurementEntry(specimen, position, revision, action, bits, raw)
    if not entries:
        raise ValidationError("measurement batch must contain at least one record")
    return list(entries.values())


class EndpointService:
    """Re-test plans, measurement admissions and endpoint snapshots."""

    def __init__(self, store: Store):
        self.store = store

    # -- re-test plans -------------------------------------------------------
    def register_plan(
        self, specimen_id: str, positions: list[str], required_count: int
    ) -> dict:
        """Register the immutable re-test plan of a specimen.

        Re-registering identical content replays (``created=False``); the same
        specimen with different content raises ``RetestPlanConflict``.
        """
        specimen = _require_specimen_id(specimen_id)
        if isinstance(positions, (str, bytes)) or not isinstance(positions, list):
            raise ValidationError("positions must be a list of position names")
        names = [_require_position(name) for name in positions]
        if not names:
            raise ValidationError("positions must be non-empty")
        if len(set(names)) != len(names):
            raise ValidationError("positions contain duplicates")
        names = sorted(names)
        if (
            isinstance(required_count, bool)
            or not isinstance(required_count, int)
            or not 1 <= required_count <= len(names)
        ):
            raise ValidationError(
                f"required_count must be an integer in 1..{len(names)}, got {required_count!r}"
            )
        body = {
            "specimen_id": specimen,
            "positions": names,
            "required_count": required_count,
        }
        content_digest = digest(body)
        existing = self.store.conn.execute(
            "SELECT * FROM retest_plan WHERE specimen_id=?", (specimen,)
        ).fetchone()
        if existing is not None:
            if existing["content_digest"] != content_digest:
                raise RetestPlanConflict(
                    f"re-test plan for {specimen!r} exists with different content"
                )
            return self._plan_view(existing, created=False)
        with self.store.transaction() as conn:
            conn.execute(
                "INSERT INTO retest_plan"
                "(specimen_id, positions_json, required_count, content_digest, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (specimen, canonical_json(names), required_count, content_digest, utcnow()),
            )
        row = self.store.conn.execute(
            "SELECT * FROM retest_plan WHERE specimen_id=?", (specimen,)
        ).fetchone()
        return self._plan_view(row, created=True)

    def _plan_view(self, row, created: bool) -> dict:
        return {
            "specimen_id": row["specimen_id"],
            "positions": json.loads(row["positions_json"]),
            "required_count": row["required_count"],
            "content_digest": row["content_digest"],
            "created": created,
        }

    def plan(self, specimen_id: str) -> dict:
        row = self.store.conn.execute(
            "SELECT * FROM retest_plan WHERE specimen_id=?", (specimen_id,)
        ).fetchone()
        if row is None:
            raise RetestPlanNotFound(specimen_id)
        return self._plan_view(row, created=False)

    # -- measurement state ---------------------------------------------------
    def effective_measurement(self, specimen_id: str, position: str):
        """The current effective (highest-revision) measurement, if any."""
        row = self.store.conn.execute(
            "SELECT revision, action, bits FROM retest_measurements "
            "WHERE specimen_id=? AND position=? ORDER BY revision DESC LIMIT 1",
            (specimen_id, position),
        ).fetchone()
        return None if row is None else dict(row)

    def measurements(self) -> list[dict]:
        rows = self.store.conn.execute(
            "SELECT specimen_id, position, revision, action, bits FROM retest_measurements "
            "ORDER BY specimen_id, position, revision"
        ).fetchall()
        return [dict(row) for row in rows]

    # -- admission -----------------------------------------------------------
    def admit_measurements(self, records: Iterable[dict]) -> dict:
        """Admit a batch atomically under highest-revision-wins rules."""
        entries = _normalise_entries(records)
        ordered = sorted(entries, key=lambda e: (e.specimen_id, e.position, e.revision))
        # Request validation against the registered plans happens before any
        # write: every specimen must have a plan and every position must be
        # pre-registered in it.
        for entry in ordered:
            plan_row = self.store.conn.execute(
                "SELECT positions_json FROM retest_plan WHERE specimen_id=?",
                (entry.specimen_id,),
            ).fetchone()
            if plan_row is None:
                raise ValidationError(
                    f"no re-test plan registered for specimen {entry.specimen_id!r}"
                )
            if entry.position not in json.loads(plan_row["positions_json"]):
                raise ValidationError(
                    f"position {entry.position!r} is not registered for "
                    f"specimen {entry.specimen_id!r}"
                )

        batch_body = [entry.canonical() for entry in ordered]
        batch_digest = digest(batch_body)
        existing = self.store.conn.execute(
            "SELECT * FROM retest_admissions WHERE batch_digest=?", (batch_digest,)
        ).fetchone()
        if existing is not None:
            return self._admission_view(existing, created=False)

        conflicts = []
        plans: list[dict] = []
        for entry in ordered:
            current = self.store.conn.execute(
                "SELECT MAX(revision) AS rev FROM retest_measurements "
                "WHERE specimen_id=? AND position=?",
                (entry.specimen_id, entry.position),
            ).fetchone()
            current_rev = None if current["rev"] is None else current["rev"]
            same = self.store.conn.execute(
                "SELECT action, bits FROM retest_measurements "
                "WHERE specimen_id=? AND position=? AND revision=?",
                (entry.specimen_id, entry.position, entry.revision),
            ).fetchone()
            if same is not None and (same["action"] != entry.action or same["bits"] != entry.bits):
                conflicts.append(
                    {
                        "reason": "revision_conflict",
                        "specimen_id": entry.specimen_id,
                        "position": entry.position,
                        "revision": entry.revision,
                        "stored_action": same["action"],
                        "stored_bits": same["bits"],
                        "offered_action": entry.action,
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
            plans.append({**entry.canonical(), "raw": entry.raw, "disposition": disposition})

        canonical = {"batch": batch_body, "conflicts": conflicts}
        with self.store.transaction() as conn:
            if conflicts:
                # Whole batch rejected: audit the admission and every member,
                # mutate nothing.
                cur = conn.execute(
                    "INSERT INTO retest_admissions"
                    "(batch_digest, accepted, outcome, canonical, created_at) "
                    "VALUES (?, 0, 'rejected', ?, ?)",
                    (batch_digest, canonical_json(canonical), utcnow()),
                )
                admission_id = cur.lastrowid
                for entry in ordered:
                    conn.execute(
                        "INSERT INTO retest_batch_member"
                        "(admission_id, specimen_id, position, revision, action, bits, disposition) "
                        "VALUES (?, ?, ?, ?, ?, ?, 'rejected')",
                        (
                            admission_id,
                            entry.specimen_id,
                            entry.position,
                            entry.revision,
                            entry.action,
                            entry.bits,
                        ),
                    )
            else:
                cur = conn.execute(
                    "INSERT INTO retest_admissions"
                    "(batch_digest, accepted, outcome, canonical, created_at) "
                    "VALUES (?, ?, 'accepted', ?, ?)",
                    (batch_digest, len(plans), canonical_json(canonical), utcnow()),
                )
                admission_id = cur.lastrowid
                for plan in plans:
                    conn.execute(
                        "INSERT INTO retest_batch_member"
                        "(admission_id, specimen_id, position, revision, action, bits, disposition) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?)",
                        (
                            admission_id,
                            plan["specimen_id"],
                            plan["position"],
                            plan["revision"],
                            plan["action"],
                            plan["bits"],
                            plan["disposition"],
                        ),
                    )
                    if plan["disposition"] == "new":
                        conn.execute(
                            "INSERT INTO retest_measurements"
                            "(specimen_id, position, revision, action, bits, raw, admission_id) "
                            "VALUES (?, ?, ?, ?, ?, ?, ?)",
                            (
                                plan["specimen_id"],
                                plan["position"],
                                plan["revision"],
                                plan["action"],
                                plan["bits"],
                                plan["raw"],
                                admission_id,
                            ),
                        )
        row = self.store.conn.execute(
            "SELECT * FROM retest_admissions WHERE admission_id=?", (admission_id,)
        ).fetchone()
        result = self._admission_view(row, created=True)
        if conflicts:
            raise _RetestAdmissionRejected(result)
        return result

    def _admission_view(self, row, created: bool) -> dict:
        members = self.store.conn.execute(
            "SELECT specimen_id, position, revision, action, bits, disposition "
            "FROM retest_batch_member WHERE admission_id=? "
            "ORDER BY specimen_id, position, revision",
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
        """Admission for a single correction/retraction operation.

        A rejected operation returns its audited result instead of raising;
        nothing is left half-applied.
        """
        try:
            return self.admit_measurements([record])
        except _RetestAdmissionRejected as exc:
            return exc.result

    # -- endpoints -----------------------------------------------------------
    def endpoint(self, specimen_id: str) -> dict:
        """Live endpoint status: basis positions/bit patterns, or the gap."""
        plan = self.plan(specimen_id)
        effective = {}
        for position in plan["positions"]:
            row = self.store.conn.execute(
                "SELECT revision, action, bits FROM retest_measurements "
                "WHERE specimen_id=? AND position=? ORDER BY revision DESC LIMIT 1",
                (specimen_id, position),
            ).fetchone()
            if row is not None:
                decoded = (
                    semantics.decode(semantics.parse_bits(row["bits"]))
                    if row["action"] == endpoint_algo.ACTION_VALUE
                    else None
                )
                effective[position] = (row["revision"], row["action"], decoded)
        status = endpoint_algo.evaluate(
            specimen_id, plan["positions"], plan["required_count"], effective
        )
        return self._status_view(status, plan["content_digest"])

    @staticmethod
    def _status_view(status: dict, plan_digest: str) -> dict:
        def frac_or_none(value):
            return None if value is None else frac_str(value)

        return {
            "specimen_id": status["specimen_id"],
            "plan_digest": plan_digest,
            "required_count": status["required_count"],
            "valid_count": status["valid_count"],
            "reached": status["reached"],
            "endpoint": frac_or_none(status["endpoint"]),
            "gap": status["gap"],
            "positions": [
                {**row, "value": frac_or_none(row["value"])}
                for row in status["positions"]
            ],
            "basis": [
                {**row, "value": frac_str(row["value"])} for row in status["basis"]
            ],
            "shortfall": status["shortfall"],
        }

    # -- snapshots -----------------------------------------------------------
    def freeze(self, snapshot_id: str, specimen_id: str) -> dict:
        """Pin the current reached endpoint into an immutable snapshot."""
        if not isinstance(snapshot_id, str) or not snapshot_id:
            raise ValidationError("snapshot_id must be a non-empty string")
        status = self.endpoint(specimen_id)
        if not status["reached"]:
            raise ValidationError(
                f"endpoint for {specimen_id!r} has not reached the contract "
                f"quantity: {status['valid_count']}/{status['required_count']} "
                f"valid, gap {status['gap']}"
            )
        body = {
            "kind": "endpoint_snapshot",
            "snapshot_id": snapshot_id,
            "specimen_id": specimen_id,
            "plan_digest": status["plan_digest"],
            "required_count": status["required_count"],
            "valid_count": status["valid_count"],
            "endpoint": status["endpoint"],
            "basis": status["basis"],
        }
        content_digest = digest(body)
        existing = self.store.conn.execute(
            "SELECT * FROM endpoint_snapshots WHERE snapshot_id=?", (snapshot_id,)
        ).fetchone()
        if existing is not None:
            if existing["content_digest"] != content_digest:
                raise SnapshotConflict(
                    f"snapshot {snapshot_id!r} exists with different content"
                )
            return self._snapshot_view(existing, created=False)
        with self.store.transaction() as conn:
            conn.execute(
                "INSERT INTO endpoint_snapshots"
                "(snapshot_id, specimen_id, content_digest, canonical, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (snapshot_id, specimen_id, content_digest, canonical_json(body), utcnow()),
            )
        row = self.store.conn.execute(
            "SELECT * FROM endpoint_snapshots WHERE snapshot_id=?", (snapshot_id,)
        ).fetchone()
        return self._snapshot_view(row, created=True)

    def _snapshot_view(self, row, created: bool) -> dict:
        body = json.loads(row["canonical"])
        if digest(body) != row["content_digest"]:
            raise IntegrityError(
                f"endpoint_snapshots:{row['snapshot_id']} digest mismatch"
            )
        # Independent recompute: the stored endpoint must be the exact median
        # of the stored basis values.
        values = [Fraction(entry["value"]) for entry in body["basis"]]
        if body["valid_count"] != len(values):
            raise IntegrityError(
                f"endpoint_snapshots:{row['snapshot_id']} basis/valid_count mismatch"
            )
        if endpoint_algo.median(values) != Fraction(body["endpoint"]):
            raise IntegrityError(
                f"endpoint_snapshots:{row['snapshot_id']} endpoint is not the basis median"
            )
        result = dict(body)
        result["content_digest"] = row["content_digest"]
        result["verified"] = True
        result["created"] = created
        return result

    def read_snapshot(self, snapshot_id: str) -> dict:
        """Re-open an old snapshot, re-verifying its stored content."""
        row = self.store.conn.execute(
            "SELECT * FROM endpoint_snapshots WHERE snapshot_id=?", (snapshot_id,)
        ).fetchone()
        if row is None:
            raise SnapshotNotFound(snapshot_id)
        return self._snapshot_view(row, created=False)


class _RetestAdmissionRejected(RetestBatchRejected):
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
