"""Exception types shared by the semantics service and the delivery library."""

from __future__ import annotations

from typing import Iterable, Mapping, Any


class ValidationError(ValueError):
    """A request is malformed or violates a contract."""


class BatchRejected(Exception):
    """An observation admission batch was rejected atomically."""

    def __init__(self, reasons: Iterable[Mapping[str, Any]]):
        self.reasons = list(reasons)
        super().__init__("observation batch rejected: " + repr(self.reasons))


class ReferenceConflict(Exception):
    """A reference version already exists with different immutable content."""


class ReportConflict(Exception):
    """A report id already exists with different immutable content."""


class ReferenceNotFound(KeyError):
    pass


class ReportNotFound(KeyError):
    pass


class ProductConflict(Exception):
    """A product id already exists with different immutable content."""


class ProductNotFound(KeyError):
    pass


class DeliveryNotFound(KeyError):
    pass


class DeliveryIdConflict(Exception):
    """A successful delivery id is being reused with a different request."""


class DeliveryRejected(Exception):
    """No legal delivery exists for at least one requested record.

    A rejected task publishes no delivery rows.  ``attempt_id`` identifies the
    audit entry (if one was recorded), not a successful delivery identity.
    """

    def __init__(
        self,
        delivery_id: str,
        reasons: Iterable[Mapping[str, Any]],
        attempt_id: int | None = None,
    ):
        self.delivery_id = delivery_id
        self.reasons = list(reasons)
        self.attempt_id = attempt_id
        super().__init__(
            f"delivery {delivery_id!r} rejected: " + repr(self.reasons)
        )


class IntegrityError(Exception):
    """Stored immutable content failed independent read-back verification."""


class RetestPlanConflict(Exception):
    """A re-test plan already exists for the specimen with different content."""


class RetestPlanNotFound(KeyError):
    pass


class RetestBatchRejected(Exception):
    """A re-test measurement batch was rejected atomically."""

    def __init__(self, reasons: Iterable[Mapping[str, Any]]):
        self.reasons = list(reasons)
        super().__init__("re-test measurement batch rejected: " + repr(self.reasons))


class SnapshotConflict(Exception):
    """An endpoint snapshot id already exists with different immutable content."""


class SnapshotNotFound(KeyError):
    pass


class LineConflict(Exception):
    """A desk line id already exists with different immutable content."""


class LineNotFound(KeyError):
    pass


class PlanConflict(Exception):
    """A desk plan id already exists with different immutable content."""


class PlanNotFound(KeyError):
    pass


class EvidenceMissing(Exception):
    """Registered calibration specimens lack a usable endpoint.

    The whole line is flagged: ``deficiencies`` lists, per line, every
    registered calibration specimen without a reached endpoint.
    """

    def __init__(self, deficiencies: Iterable[Mapping[str, Any]]):
        self.deficiencies = list(deficiencies)
        super().__init__("calibration evidence missing: " + repr(self.deficiencies))


class PlanInfeasible(Exception):
    """No segmentation meets the shared review quota.

    Defensive: a non-negative quota is always feasible because an empty
    review segment is legal, so this can only surface from a pure-planner
    inconsistency.
    """

    def __init__(self, detail: Mapping[str, Any]):
        self.detail = dict(detail)
        super().__init__("no feasible segmentation: " + repr(self.detail))
