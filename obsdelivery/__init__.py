"""Independent observation-semantics service and decimal delivery library.

Two responsibilities with a real boundary:

* ``obsdelivery.semantics`` / ``obsdelivery.service`` interpret original
  binary32 bit patterns exactly and judge them against references.  This works
  with no delivery library present.
* ``obsdelivery.delivery_algo`` / ``obsdelivery.delivery`` take a *published*
  semantic product and an independent reader contract and decide a concrete
  decimal delivery, fail-closed and all-or-nothing.

Plus the quality-planning extension:

* ``obsdelivery.endpoint_algo`` / ``obsdelivery.retest`` collect revisable
  re-test measurements per specimen and freeze exact median endpoints.
* ``obsdelivery.planner_algo`` / ``obsdelivery.desk`` jointly pick per-line
  pass/review/reject thresholds under a shared review quota, minimising the
  worst-case expected total cost.
"""

from .contracts import ReaderContract
from .delivery import DeliveryService
from .delivery_algo import ScaleAttempt, Selection, round_half_even, select_representation
from .desk import PlannerDesk
from .endpoint_algo import median
from .errors import (
    BatchRejected,
    DeliveryIdConflict,
    DeliveryNotFound,
    DeliveryRejected,
    EvidenceMissing,
    IntegrityError,
    LineConflict,
    LineNotFound,
    PlanConflict,
    PlanInfeasible,
    PlanNotFound,
    ProductConflict,
    ProductNotFound,
    ReferenceConflict,
    ReferenceNotFound,
    ReportConflict,
    ReportNotFound,
    RetestBatchRejected,
    RetestPlanConflict,
    RetestPlanNotFound,
    SnapshotConflict,
    SnapshotNotFound,
    ValidationError,
)
from .planner_algo import LineModel, brute_force_joint, plan_lines
from .retest import EndpointService
from .semantics import decode, interpret, judge, parse_bits
from .service import SemanticsService
from .storage import Store

__all__ = [
    "ReaderContract",
    "DeliveryService",
    "ScaleAttempt",
    "Selection",
    "round_half_even",
    "select_representation",
    "PlannerDesk",
    "median",
    "BatchRejected",
    "DeliveryIdConflict",
    "DeliveryNotFound",
    "DeliveryRejected",
    "EvidenceMissing",
    "IntegrityError",
    "LineConflict",
    "LineNotFound",
    "PlanConflict",
    "PlanInfeasible",
    "PlanNotFound",
    "ProductConflict",
    "ProductNotFound",
    "ReferenceConflict",
    "ReferenceNotFound",
    "ReportConflict",
    "ReportNotFound",
    "RetestBatchRejected",
    "RetestPlanConflict",
    "RetestPlanNotFound",
    "SnapshotConflict",
    "SnapshotNotFound",
    "ValidationError",
    "LineModel",
    "brute_force_joint",
    "plan_lines",
    "EndpointService",
    "decode",
    "interpret",
    "judge",
    "parse_bits",
    "SemanticsService",
    "Store",
]
