"""Quality planning desk: shared review-station threshold plans.

The desk stores, per inspection line, the calibration specimen list with
independently reviewed good/bad labels, the shift count, the allowed closed
interval for the bad-part ratio and the three unit costs (false accept,
false reject, manual review).  At plan time it pulls the *current* re-test
endpoints of the registered specimens from the endpoint service — a
registered specimen without a reached endpoint flags its whole line as
evidence-missing — and freezes the joint threshold choice into an immutable
plan artefact that records exactly which endpoints were consumed, so old
plans re-open and re-verify independently of later corrections.
"""

from __future__ import annotations

import json
from fractions import Fraction
from typing import Sequence

from . import planner_algo
from .canonical import canonical_json, digest, frac_str, parse_fraction
from .errors import (
    EvidenceMissing,
    IntegrityError,
    LineConflict,
    LineNotFound,
    PlanConflict,
    PlanInfeasible,
    PlanNotFound,
    RetestPlanNotFound,
    ValidationError,
)
from .planner_algo import LineModel
from .retest import EndpointService
from .storage import Store, utcnow

LABELS = ("good", "bad")
COST_FIELDS = ("false_accept", "false_reject", "review")


# ---------------------------------------------------------------------------
# request validation helpers
# ---------------------------------------------------------------------------

def _require_line_id(value) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValidationError(f"line_id must be a non-empty string, got {value!r}")
    return value


def _require_shift_count(value) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValidationError(f"shift_count must be a positive integer, got {value!r}")
    return value


def _require_ratio_interval(value) -> tuple[Fraction, Fraction]:
    if (
        isinstance(value, (str, bytes))
        or not isinstance(value, (list, tuple))
        or len(value) != 2
    ):
        raise ValidationError("bad_ratio_interval must be a [lo, hi] pair")
    try:
        lo = parse_fraction(value[0], field="bad_ratio_interval[0]")
        hi = parse_fraction(value[1], field="bad_ratio_interval[1]")
    except ValueError as exc:
        raise ValidationError(str(exc)) from exc
    if not (Fraction(0) <= lo <= hi <= Fraction(1)):
        raise ValidationError("bad_ratio_interval must satisfy 0 <= lo <= hi <= 1")
    return lo, hi


def _require_costs(value) -> dict[str, Fraction]:
    if not isinstance(value, dict):
        raise ValidationError("costs must be an object with the three unit costs")
    unknown = set(value) - set(COST_FIELDS)
    if unknown:
        raise ValidationError(f"unknown cost field(s) {sorted(unknown)}")
    costs = {}
    for field in COST_FIELDS:
        if field not in value:
            raise ValidationError(f"costs is missing {field!r}")
        try:
            cost = parse_fraction(value[field], field=f"costs.{field}")
        except ValueError as exc:
            raise ValidationError(str(exc)) from exc
        if cost < 0:
            raise ValidationError(f"costs.{field} must be non-negative")
        costs[field] = cost
    return costs


def _require_calibration(value) -> list[dict]:
    if isinstance(value, (str, bytes)) or not isinstance(value, list) or not value:
        raise ValidationError("calibration must be a non-empty list")
    seen = set()
    entries = []
    for item in value:
        if not isinstance(item, dict):
            raise ValidationError(f"calibration entry is not an object: {item!r}")
        specimen = item.get("specimen_id")
        if not isinstance(specimen, str) or not specimen.strip():
            raise ValidationError(f"calibration specimen_id must be a non-empty string, got {specimen!r}")
        label = item.get("label")
        if label not in LABELS:
            raise ValidationError(f"calibration label must be one of {LABELS}, got {label!r}")
        if specimen in seen:
            raise ValidationError(f"calibration lists specimen {specimen!r} twice")
        seen.add(specimen)
        entries.append({"specimen_id": specimen, "label": label})
    entries.sort(key=lambda entry: entry["specimen_id"])
    labels = {entry["label"] for entry in entries}
    if labels != set(LABELS):
        raise ValidationError("calibration must cover both classes (good and bad)")
    return entries


class PlannerDesk:
    """Joint shift-threshold planning over the shared review station."""

    def __init__(self, store: Store, endpoints: EndpointService):
        self.store = store
        self.endpoints = endpoints

    # -- line registration ---------------------------------------------------
    def register_line(
        self,
        line_id: str,
        *,
        shift_count: int,
        bad_ratio_interval,
        costs,
        calibration,
    ) -> dict:
        """Register the immutable configuration of one inspection line."""
        line = _require_line_id(line_id)
        count = _require_shift_count(shift_count)
        lo, hi = _require_ratio_interval(bad_ratio_interval)
        unit_costs = _require_costs(costs)
        entries = _require_calibration(calibration)
        body = {
            "line_id": line,
            "shift_count": count,
            "ratio_lo": frac_str(lo),
            "ratio_hi": frac_str(hi),
            "costs": {field: frac_str(unit_costs[field]) for field in COST_FIELDS},
            "calibration": entries,
        }
        content_digest = digest(body)
        existing = self.store.conn.execute(
            "SELECT * FROM desk_line WHERE line_id=?", (line,)
        ).fetchone()
        if existing is not None:
            if existing["content_digest"] != content_digest:
                raise LineConflict(f"line {line!r} exists with different content")
            return self._line_view(existing, created=False)
        with self.store.transaction() as conn:
            conn.execute(
                "INSERT INTO desk_line"
                "(line_id, shift_count, ratio_lo, ratio_hi, cost_false_accept, "
                " cost_false_reject, cost_review, calibration_json, content_digest, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    line,
                    count,
                    body["ratio_lo"],
                    body["ratio_hi"],
                    body["costs"]["false_accept"],
                    body["costs"]["false_reject"],
                    body["costs"]["review"],
                    canonical_json(entries),
                    content_digest,
                    utcnow(),
                ),
            )
        row = self.store.conn.execute(
            "SELECT * FROM desk_line WHERE line_id=?", (line,)
        ).fetchone()
        return self._line_view(row, created=True)

    def _line_view(self, row, created: bool) -> dict:
        return {
            "line_id": row["line_id"],
            "shift_count": row["shift_count"],
            "bad_ratio_interval": [row["ratio_lo"], row["ratio_hi"]],
            "costs": {
                "false_accept": row["cost_false_accept"],
                "false_reject": row["cost_false_reject"],
                "review": row["cost_review"],
            },
            "calibration": json.loads(row["calibration_json"]),
            "content_digest": row["content_digest"],
            "created": created,
        }

    def line(self, line_id: str) -> dict:
        row = self.store.conn.execute(
            "SELECT * FROM desk_line WHERE line_id=?", (line_id,)
        ).fetchone()
        if row is None:
            raise LineNotFound(line_id)
        return self._line_view(row, created=False)

    def _line_row(self, line_id: str):
        row = self.store.conn.execute(
            "SELECT * FROM desk_line WHERE line_id=?", (line_id,)
        ).fetchone()
        if row is None:
            raise LineNotFound(line_id)
        return row

    # -- planning ------------------------------------------------------------
    def plan(self, plan_id: str, line_ids: Sequence[str], shared_quota) -> dict:
        """Freeze a joint threshold plan for the given lines.

        Raises ``EvidenceMissing`` when any registered calibration specimen
        lacks a reached endpoint (the whole line is flagged), and
        ``PlanConflict`` when the id already holds different content.
        """
        if not isinstance(plan_id, str) or not plan_id:
            raise ValidationError("plan_id must be a non-empty string")
        if isinstance(line_ids, (str, bytes)) or not isinstance(line_ids, Sequence):
            raise ValidationError("line_ids must be a sequence")
        ids = [_require_line_id(line_id) for line_id in line_ids]
        if not 1 <= len(ids) <= planner_algo.MAX_LINES:
            raise ValidationError(
                f"a batch plans 1..{planner_algo.MAX_LINES} lines, got {len(ids)}"
            )
        if len(set(ids)) != len(ids):
            raise ValidationError("line_ids contains duplicates")
        try:
            quota = parse_fraction(shared_quota, field="shared_quota")
        except ValueError as exc:
            raise ValidationError(str(exc)) from exc
        if quota < 0:
            raise ValidationError("shared_quota must be non-negative")

        models: list[LineModel] = []
        input_lines: list[dict] = []
        deficiencies: list[dict] = []
        for line_id in ids:
            row = self._line_row(line_id)
            calibration = json.loads(row["calibration_json"])
            good_endpoints: list[Fraction] = []
            bad_endpoints: list[Fraction] = []
            calibration_view = []
            missing = []
            for entry in calibration:
                specimen = entry["specimen_id"]
                try:
                    status = self.endpoints.endpoint(specimen)
                except RetestPlanNotFound:
                    missing.append({"specimen_id": specimen, "reason": "no_retest_plan"})
                    continue
                if not status["reached"]:
                    missing.append(
                        {
                            "specimen_id": specimen,
                            "reason": "endpoint_not_reached",
                            "valid_count": status["valid_count"],
                            "required_count": status["required_count"],
                            "gap": status["gap"],
                        }
                    )
                    continue
                endpoint = Fraction(status["endpoint"])
                calibration_view.append(
                    {
                        "specimen_id": specimen,
                        "label": entry["label"],
                        "endpoint": frac_str(endpoint),
                    }
                )
                if entry["label"] == "good":
                    good_endpoints.append(endpoint)
                else:
                    bad_endpoints.append(endpoint)
            if missing:
                deficiencies.append({"line_id": line_id, "missing": missing})
            costs = {
                "false_accept": Fraction(row["cost_false_accept"]),
                "false_reject": Fraction(row["cost_false_reject"]),
                "review": Fraction(row["cost_review"]),
            }
            models.append(
                LineModel(
                    line_id=line_id,
                    shift_count=row["shift_count"],
                    ratio_lo=Fraction(row["ratio_lo"]),
                    ratio_hi=Fraction(row["ratio_hi"]),
                    cost_false_accept=costs["false_accept"],
                    cost_false_reject=costs["false_reject"],
                    cost_review=costs["review"],
                    good_endpoints=tuple(good_endpoints),
                    bad_endpoints=tuple(bad_endpoints),
                )
            )
            input_lines.append(
                {
                    "line_id": line_id,
                    "shift_count": row["shift_count"],
                    "ratio_lo": row["ratio_lo"],
                    "ratio_hi": row["ratio_hi"],
                    "costs": {field: frac_str(costs[field]) for field in COST_FIELDS},
                    "calibration": calibration_view,
                    "good_endpoints": [frac_str(value) for value in good_endpoints],
                    "bad_endpoints": [frac_str(value) for value in bad_endpoints],
                }
            )

        if deficiencies:
            self._record_attempt(plan_id, "evidence_missing", deficiencies)
            raise EvidenceMissing(deficiencies)

        try:
            output = planner_algo.plan_lines(models, quota)
        except ValueError as exc:
            raise ValidationError(str(exc)) from exc
        if output["status"] != "ok":
            detail = {"status": output["status"], "min_review": frac_str(output["min_review"])}
            self._record_attempt(plan_id, "infeasible", [detail])
            raise PlanInfeasible(detail)

        input_body = {
            "plan_id": plan_id,
            "shared_quota": frac_str(quota),
            "lines": input_lines,
        }
        output_plain = json.loads(canonical_json(output))
        content = {"input": input_body, "output": output_plain}
        content_digest = digest(content)

        existing = self.store.conn.execute(
            "SELECT * FROM desk_plan WHERE plan_id=?", (plan_id,)
        ).fetchone()
        if existing is not None:
            if existing["content_digest"] != content_digest:
                raise PlanConflict(f"plan {plan_id!r} exists with different content")
            return self._plan_view(existing, created=False)

        with self.store.transaction() as conn:
            conn.execute(
                "INSERT INTO desk_plan"
                "(plan_id, shared_quota, input_json, output_json, content_digest, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    plan_id,
                    frac_str(quota),
                    canonical_json(input_body),
                    canonical_json(output_plain),
                    content_digest,
                    utcnow(),
                ),
            )
            conn.execute(
                "INSERT INTO desk_attempt(plan_id, status, reasons_json, created_at) "
                "VALUES (?, 'published', ?, ?)",
                (plan_id, canonical_json([]), utcnow()),
            )
        row = self.store.conn.execute(
            "SELECT * FROM desk_plan WHERE plan_id=?", (plan_id,)
        ).fetchone()
        return self._plan_view(row, created=True)

    def _record_attempt(self, plan_id: str, status: str, reasons) -> None:
        with self.store.transaction() as conn:
            conn.execute(
                "INSERT INTO desk_attempt(plan_id, status, reasons_json, created_at) "
                "VALUES (?, ?, ?, ?)",
                (plan_id, status, canonical_json(reasons), utcnow()),
            )

    def _plan_view(self, row, created: bool) -> dict:
        input_body = json.loads(row["input_json"])
        stored_output = json.loads(row["output_json"])
        # Independent recompute: re-run the pure planner on the stored input
        # and require it to reproduce the stored output exactly.
        models = [
            LineModel(
                line_id=line["line_id"],
                shift_count=line["shift_count"],
                ratio_lo=Fraction(line["ratio_lo"]),
                ratio_hi=Fraction(line["ratio_hi"]),
                cost_false_accept=Fraction(line["costs"]["false_accept"]),
                cost_false_reject=Fraction(line["costs"]["false_reject"]),
                cost_review=Fraction(line["costs"]["review"]),
                good_endpoints=tuple(Fraction(value) for value in line["good_endpoints"]),
                bad_endpoints=tuple(Fraction(value) for value in line["bad_endpoints"]),
            )
            for line in input_body["lines"]
        ]
        recomputed = planner_algo.plan_lines(models, Fraction(input_body["shared_quota"]))
        if canonical_json(recomputed) != canonical_json(stored_output):
            raise IntegrityError(f"desk_plan:{row['plan_id']} recomputation differs")
        if digest({"input": input_body, "output": stored_output}) != row["content_digest"]:
            raise IntegrityError(f"desk_plan:{row['plan_id']} digest mismatch")
        result = dict(stored_output)
        result["plan_id"] = row["plan_id"]
        result["input"] = input_body
        result["content_digest"] = row["content_digest"]
        result["verified"] = True
        result["created"] = created
        return result

    def read_plan(self, plan_id: str) -> dict:
        """Re-open an old plan, recomputing it from its stored input."""
        row = self.store.conn.execute(
            "SELECT * FROM desk_plan WHERE plan_id=?", (plan_id,)
        ).fetchone()
        if row is None:
            raise PlanNotFound(plan_id)
        return self._plan_view(row, created=False)

    def attempts(self, plan_id: str) -> list[dict]:
        rows = self.store.conn.execute(
            "SELECT attempt_id, plan_id, status, reasons_json, created_at "
            "FROM desk_attempt WHERE plan_id=? ORDER BY attempt_id",
            (plan_id,),
        ).fetchall()
        return [
            {
                "attempt_id": row["attempt_id"],
                "plan_id": row["plan_id"],
                "status": row["status"],
                "reasons": json.loads(row["reasons_json"]),
                "created_at": row["created_at"],
            }
            for row in rows
        ]
