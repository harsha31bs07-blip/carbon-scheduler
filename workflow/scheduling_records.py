"""Read and write individual schema-3 records without relabeling old evidence.

The legacy pipeline's schema-1/2 JSONL logs still use ``workflow.run_store``.
They cannot be loaded here or mixed into schema-3 comparisons implicitly.
"""

from __future__ import annotations

import json
from pathlib import Path

from shared.run_records import write_json
from shared.scheduling import (
    CarbonSample, ComparisonContext, ExecutionAttempt, MetricEstimate,
    PlannedCandidate, Record, RejectedCandidate, SchedulingDecision,
    StepEstimates, StepRequest, WorkflowRequest, WorkloadSizeForecast,
)


_TYPES = {record.__name__: record for record in (
    CarbonSample, ComparisonContext, ExecutionAttempt, MetricEstimate,
    PlannedCandidate, RejectedCandidate, SchedulingDecision, StepEstimates,
    StepRequest, WorkflowRequest, WorkloadSizeForecast,
)}
_JOINT_TYPES = frozenset({
    "Reservation", "CommittedAssignment", "ScheduledStep", "PlanMetrics",
    "NormalizationSnapshot", "WorkflowPlan", "PlanResult",
})


def _record_type(name: str):
    """Resolve optional planner contracts without a shared -> scheduler cycle."""
    if name in _JOINT_TYPES and name not in _TYPES:
        from scheduler.joint import (
            CommittedAssignment, NormalizationSnapshot, PlanMetrics, PlanResult,
            Reservation, ScheduledStep, WorkflowPlan,
        )
        _TYPES.update({record.__name__: record for record in (
            CommittedAssignment, NormalizationSnapshot, PlanMetrics, PlanResult,
            Reservation, ScheduledStep, WorkflowPlan,
        )})
    return _TYPES.get(name)


def save_record(path: str | Path, record: Record) -> None:
    """Atomically replace a typed record, allowing no non-finite JSON numbers."""
    if type(record) is not _record_type(type(record).__name__):
        raise ValueError("Unsupported scheduling record type")
    # Revalidate at the persistence boundary, including mutable score mappings.
    payload = record.to_dict()
    type(record).from_dict(payload)
    write_json(Path(path), payload)


def _unique_fields(pairs: list[tuple[str, object]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate field in scheduling record")
        result[key] = value
    return result


def _invalid_constant(value: str):
    raise ValueError("Scheduling records require finite JSON numbers")


def load_record(path: str | Path) -> Record:
    """Load a typed record; unknown versions/types and malformed data fail closed."""
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8-sig"),
                             object_pairs_hook=_unique_fields,
                             parse_constant=_invalid_constant)
    except json.JSONDecodeError:
        raise ValueError("Scheduling record is not valid JSON") from None
    if not isinstance(payload, dict) or not isinstance(payload.get("record_type"), str):
        raise ValueError("Scheduling record requires an explicit record_type")
    record_type = _record_type(payload["record_type"])
    if record_type is None:
        raise ValueError("Unsupported scheduling record type")
    return record_type.from_dict(payload)


def comparison_compatible(left: ComparisonContext, right: ComparisonContext) -> bool:
    """Require complete schema-3 contexts, including actual workload identity."""
    if not isinstance(left, ComparisonContext) or not isinstance(right, ComparisonContext):
        raise ValueError("Comparison requires two schema-3 ComparisonContext values")
    return left.fingerprint() == right.fingerprint()
