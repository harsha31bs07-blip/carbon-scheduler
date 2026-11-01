"""Validated A2 scheduling contracts, independent of legacy schema-2 run records.

Timestamps are timezone-aware and normalized to UTC. Durations use seconds;
unknown observations remain None. No contract stores credential values.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, fields, is_dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import json
import math
import types
from types import MappingProxyType
from typing import Any, Protocol, Union, get_args, get_origin, get_type_hints

from shared.schema import Candidate as LegacyCandidate


SCHEMA_VERSION = 3
MAX_OUTPUT_LIMIT_TOKENS = 32768
COMPLETION_REASONS = frozenset({
    "stop", "length", "content-filter", "tool-calls", "invalid-tool-call",
    "load", "unload", "cancelled", "error", "other",
})
PROVENANCE = frozenset({"measured", "estimated", "simulated", "mixed", "unknown"})
METRICS = frozenset({"latency", "accuracy", "cost", "energy", "carbon"})


def _text(value: str, name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a nonempty string")


def _number(value: float, name: str, *, minimum: float | None = 0, maximum: float | None = None) -> None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a finite number")
    try:
        finite = math.isfinite(value)
    except OverflowError:
        finite = False
    if not finite or (minimum is not None and value < minimum) or (maximum is not None and value > maximum):
        raise ValueError(f"{name} must be finite and within its allowed range")


def _integer(value: int, name: str, *, minimum: int = 0) -> None:
    if type(value) is not int or value < minimum:
        raise ValueError(f"{name} must be an integer >= {minimum}")


def _utc(value: datetime, name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be a timezone-aware datetime")
    return value.astimezone(timezone.utc)


def _times(record: Any, *names: str) -> None:
    for name in names:
        value = getattr(record, name)
        object.__setattr__(record, name, _utc(value, name))


def _optional_times(record: Any, *names: str) -> None:
    for name in names:
        if getattr(record, name) is not None:
            _times(record, name)


def _encode(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat().replace("+00:00", "Z")
    if is_dataclass(value):
        return {field.name: _encode(getattr(value, field.name)) for field in fields(value)}
    if isinstance(value, (tuple, list)):
        return [_encode(item) for item in value]
    if isinstance(value, Mapping):
        return {key: _encode(item) for key, item in value.items()}
    return value


def _decode(annotation: Any, value: Any) -> Any:
    origin, args = get_origin(annotation), get_args(annotation)
    if origin in (types.UnionType, Union):
        if value is None and type(None) in args:
            return None
        annotation = next(item for item in args if item is not type(None))
        return _decode(annotation, value)
    if annotation is datetime:
        if not isinstance(value, str):
            raise ValueError("Serialized timestamps must be ISO-8601 strings")
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    if origin is tuple:
        if not isinstance(value, (tuple, list)):
            raise ValueError("Serialized collections must be arrays")
        return tuple(_decode(args[0], item) for item in value)
    if origin in (dict, Mapping):
        if not isinstance(value, dict):
            raise ValueError("Serialized mappings must be objects")
        return {key: _decode(args[1], item) for key, item in value.items()}
    if isinstance(annotation, type) and issubclass(annotation, Record):
        return annotation._from_fields(value)
    return value


class Record:
    """JSON-safe schema-3 serialization; old record formats need explicit migration."""

    def to_dict(self) -> dict:
        return {"schema_version": SCHEMA_VERSION, "record_type": type(self).__name__, **_encode(self)}

    @classmethod
    def from_dict(cls, value: dict):
        if not isinstance(value, dict):
            raise ValueError("Record must be an object")
        data = dict(value)
        version = data.pop("schema_version", None)
        if type(version) is not int or version != SCHEMA_VERSION:
            raise ValueError(f"Unsupported scheduling schema version: {version}")
        if data.pop("record_type", None) != cls.__name__:
            raise ValueError(f"Expected {cls.__name__} record")
        return cls._from_fields(data)

    @classmethod
    def _from_fields(cls, data: dict):
        """Decode a nested record, whose parent already supplies its schema."""
        if not isinstance(data, dict):
            raise ValueError(f"{cls.__name__} must be an object")
        names = {field.name for field in fields(cls)}
        if data.keys() - names:
            raise ValueError(f"Unknown {cls.__name__} fields")
        annotations = get_type_hints(cls)
        try:
            return cls(**{key: _decode(annotations[key], item) for key, item in data.items()})
        except TypeError as exc:
            raise ValueError(f"Invalid {cls.__name__} record: {exc}") from exc


@dataclass(frozen=True)
class MetricEstimate(Record):
    value: float | None
    unit: str
    provenance: str
    source: str | None = None
    uncertainty: float | None = None

    def __post_init__(self):
        _text(self.unit, "unit")
        if not isinstance(self.provenance, str) or self.provenance not in PROVENANCE:
            raise ValueError("Unsupported metric provenance")
        if self.value is None:
            if self.provenance != "unknown" or self.uncertainty is not None:
                raise ValueError("Absent values must have unknown provenance and uncertainty")
        else:
            _number(self.value, "value")
            if self.provenance == "unknown":
                raise ValueError("Known values require explicit provenance")
            _text(self.source, "source")
        if self.source is not None:
            _text(self.source, "source")
        if self.uncertainty is not None:
            _number(self.uncertainty, "uncertainty")


@dataclass(frozen=True)
class StepEstimates(Record):
    latency: MetricEstimate
    accuracy: MetricEstimate
    cost: MetricEstimate
    energy: MetricEstimate
    carbon: MetricEstimate

    def __post_init__(self):
        for name, unit in (("latency", "s"), ("accuracy", "ratio"), ("cost", "USD"),
                           ("energy", "kWh")):
            metric = getattr(self, name)
            if not isinstance(metric, MetricEstimate) or metric.unit != unit:
                raise ValueError(f"{name} must be a MetricEstimate in {unit}")
        if not isinstance(self.carbon, MetricEstimate) or self.carbon.unit not in {"gCO2e", "gCO2"}:
            raise ValueError("carbon must explicitly use gCO2e or CO2-only gCO2; scopes cannot be pooled")
        if self.accuracy.value is not None:
            _number(self.accuracy.value, "accuracy", maximum=1)


@dataclass(frozen=True)
class StepRequest(Record):
    step_id: str
    task_type: str
    prompt: str
    input_tokens: int | None
    output_limit_tokens: int
    earliest_start: datetime
    latest_finish: datetime
    dependencies: tuple[str, ...] = ()
    allow_deferral: bool = False
    required_for_response: bool = True
    input_size_source: str = "unknown"
    enable_thinking: bool | None = None

    def __post_init__(self):
        for name in ("step_id", "task_type", "prompt", "input_size_source"):
            _text(getattr(self, name), name)
        _times(self, "earliest_start", "latest_finish")
        if self.latest_finish <= self.earliest_start:
            raise ValueError("latest_finish must be after earliest_start")
        if self.input_tokens is not None:
            _integer(self.input_tokens, "input_tokens")
            if self.input_size_source == "unknown":
                raise ValueError("Known token counts require input_size_source")
        _integer(self.output_limit_tokens, "output_limit_tokens", minimum=1)
        if self.output_limit_tokens > MAX_OUTPUT_LIMIT_TOKENS:
            raise ValueError(f"output_limit_tokens must be <= {MAX_OUTPUT_LIMIT_TOKENS}")
        if self.enable_thinking is not None and type(self.enable_thinking) is not bool:
            raise ValueError("enable_thinking must be a boolean or None for provider default")
        if type(self.allow_deferral) is not bool or type(self.required_for_response) is not bool:
            raise ValueError("Deferral and response flags must be booleans")
        if not isinstance(self.dependencies, tuple):
            raise ValueError("dependencies must be a tuple")
        for dependency in self.dependencies:
            _text(dependency, "dependency")
        if self.step_id in self.dependencies or len(set(self.dependencies)) != len(self.dependencies):
            raise ValueError("Dependencies must be unique and cannot include the step itself")


@dataclass(frozen=True)
class WorkflowRequest(Record):
    workflow_id: str
    input_text: str
    arrived_at: datetime
    policy: str
    response_deadline: datetime
    completion_deadline: datetime
    workload_version: str
    constraint_version: str
    steps: tuple[StepRequest, ...]

    def __post_init__(self):
        for name in ("workflow_id", "input_text", "policy", "workload_version", "constraint_version"):
            _text(getattr(self, name), name)
        _times(self, "arrived_at", "response_deadline", "completion_deadline")
        if not self.arrived_at < self.response_deadline <= self.completion_deadline:
            raise ValueError("Deadlines must follow arrival; response cannot follow completion")
        if not isinstance(self.steps, tuple) or not self.steps or any(not isinstance(s, StepRequest) for s in self.steps):
            raise ValueError("steps must be a nonempty tuple of StepRequest values")
        by_id = {step.step_id: step for step in self.steps}
        if len(by_id) != len(self.steps):
            raise ValueError("Step IDs must be unique")
        for step in self.steps:
            limit = self.response_deadline if step.required_for_response else self.completion_deadline
            if step.earliest_start < self.arrived_at or step.latest_finish > limit:
                raise ValueError(f"Step {step.step_id} lies outside its workflow deadline window")
            if set(step.dependencies) - by_id.keys():
                raise ValueError(f"Step {step.step_id} has unknown dependencies")
            if step.required_for_response and any(not by_id[key].required_for_response for key in step.dependencies):
                raise ValueError("Response steps require response-marked prerequisites")
        remaining, done = set(by_id), set()
        while remaining:
            ready = {key for key in remaining if set(by_id[key].dependencies) <= done}
            if not ready:
                raise ValueError("Workflow dependencies contain a cycle")
            done.update(ready)
            remaining.difference_update(ready)


@dataclass(frozen=True)
class ComparisonContext(Record):
    """Immutable comparison conditions; policy_version identifies shared code.

    workload_id fingerprints the actual input. configuration_id fingerprints the
    invariant requested configuration and constraints, excluding the policy choice
    and its weights. These are IDs, never raw secrets. Policy names, weights and
    run IDs are deliberately absent so matched policy runs share a fingerprint;
    changing any reference condition creates a different cohort.
    """

    workload_id: str
    workload_version: str
    configuration_id: str
    deployment_snapshot_id: str
    calibration_id: str
    policy_version: str
    reference_time: datetime
    experiment_mode: str

    def __post_init__(self):
        for name in ("workload_id", "workload_version", "configuration_id", "deployment_snapshot_id", "calibration_id", "policy_version"):
            _text(getattr(self, name), name)
        _times(self, "reference_time")
        if not isinstance(self.experiment_mode, str) or self.experiment_mode not in {"live", "simulation", "replay"}:
            raise ValueError("Unsupported experiment_mode")

    def fingerprint(self) -> str:
        """Policies/runs share a comparison only when all reference conditions match."""
        encoded = json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":"), allow_nan=False)
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class PlannedCandidate(Record):
    candidate_id: str
    step_id: str
    model_id: str
    deployment_id: str
    planned_start: datetime
    estimates: StepEstimates
    region: str | None = None
    legacy_location: str | None = None

    def __post_init__(self):
        for name in ("candidate_id", "step_id", "model_id", "deployment_id"):
            _text(getattr(self, name), name)
        for name in ("region", "legacy_location"):
            if getattr(self, name) is not None:
                _text(getattr(self, name), name)
        _times(self, "planned_start")
        if not isinstance(self.estimates, StepEstimates):
            raise ValueError("estimates must be StepEstimates")


@dataclass(frozen=True)
class RejectedCandidate(Record):
    candidate_id: str
    constraints: tuple[str, ...]

    def __post_init__(self):
        _text(self.candidate_id, "candidate_id")
        if not isinstance(self.constraints, tuple) or not self.constraints:
            raise ValueError("Rejected candidates require constraint reasons")
        for reason in self.constraints:
            _text(reason, "constraint")


@dataclass(frozen=True)
class SchedulingDecision(Record):
    """Feasible winner and complete alternatives with a signed score explanation.

    normalized_metrics are bounded ratios. weighted_contributions may be negative
    when a larger metric (for example accuracy) lowers a minimization score.
    weights retain the exact requested scale; normalization belongs to the scorer.
    """
    decision_id: str
    workflow_id: str
    step_id: str
    policy: str
    weights: Mapping[str, float]
    decided_at: datetime
    context: ComparisonContext
    winner: PlannedCandidate | None
    alternatives: tuple[PlannedCandidate, ...]
    rejected: tuple[RejectedCandidate, ...]
    feasible_count: int
    normalized_metrics: Mapping[str, float]
    weighted_contributions: Mapping[str, float]

    def __post_init__(self):
        for name in ("decision_id", "workflow_id", "step_id", "policy"):
            _text(getattr(self, name), name)
        if not isinstance(self.weights, Mapping) or set(self.weights) != METRICS:
            raise ValueError("weights must specify all five scheduling metrics")
        for metric, value in self.weights.items():
            _number(value, f"{metric} weight")
        try:
            total_weight = math.fsum(self.weights.values())
        except OverflowError:
            total_weight = math.inf
        if not math.isfinite(total_weight) or total_weight <= 0:
            raise ValueError("weights must have a finite positive total")
        object.__setattr__(self, "weights", MappingProxyType(dict(self.weights)))
        _times(self, "decided_at")
        if not isinstance(self.context, ComparisonContext):
            raise ValueError("context must be ComparisonContext")
        _integer(self.feasible_count, "feasible_count")
        if not isinstance(self.alternatives, tuple) or any(not isinstance(c, PlannedCandidate) for c in self.alternatives):
            raise ValueError("alternatives must be a tuple of PlannedCandidate values")
        if not isinstance(self.rejected, tuple) or any(not isinstance(c, RejectedCandidate) for c in self.rejected):
            raise ValueError("rejected must be a tuple of RejectedCandidate values")
        if self.winner is not None and not isinstance(self.winner, PlannedCandidate):
            raise ValueError("winner must be a PlannedCandidate or None")
        feasible = ((self.winner,) if self.winner is not None else ()) + self.alternatives
        if self.feasible_count != len(feasible) or (self.winner is None and self.feasible_count):
            raise ValueError("feasible_count must match the winner and feasible alternatives")
        if any(candidate.step_id != self.step_id for candidate in feasible):
            raise ValueError("Decision candidates must belong to its step")
        ids = [candidate.candidate_id for candidate in feasible] + [candidate.candidate_id for candidate in self.rejected]
        if len(ids) != len(set(ids)):
            raise ValueError("Decision candidate IDs must be unique")
        for name in ("normalized_metrics", "weighted_contributions"):
            values = getattr(self, name)
            if not isinstance(values, Mapping) or set(values) - METRICS:
                raise ValueError(f"{name} contains unsupported metrics")
            for metric, value in values.items():
                _number(value, metric, minimum=0 if name == "normalized_metrics" else None,
                        maximum=1 if name == "normalized_metrics" else None)
            object.__setattr__(self, name, MappingProxyType(dict(values)))
        if set(self.normalized_metrics) != set(self.weighted_contributions):
            raise ValueError("Score explanations must cover the same metrics")
        if self.winner is None and self.normalized_metrics:
            raise ValueError("Infeasible decisions cannot contain winner scores")


@dataclass(frozen=True)
class ExecutionAttempt(Record):
    attempt_id: str
    workflow_id: str
    planned_candidate: PlannedCandidate
    status: str
    retry_index: int = 0
    planned_finish: datetime | None = None
    actual_start: datetime | None = None
    actual_finish: datetime | None = None
    executed_model_id: str | None = None
    executed_deployment_id: str | None = None
    provider_reported_model_id: str | None = None
    wait_seconds: float | None = None
    provider_seconds: float | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    observations: StepEstimates | None = None
    output: str | None = None
    error: str | None = None
    # Requested controls are valid before dispatch; None preserves legacy records
    # without recorded configuration and denotes provider-default thinking.
    output_limit_tokens: int | None = None
    enable_thinking: bool | None = None
    completion_reason: str | None = None

    def __post_init__(self):
        _text(self.attempt_id, "attempt_id")
        _text(self.workflow_id, "workflow_id")
        if not isinstance(self.planned_candidate, PlannedCandidate):
            raise ValueError("planned_candidate must be a PlannedCandidate")
        if not isinstance(self.status, str) or self.status not in {"pending", "scheduled", "running", "completed", "failed", "cancelled", "needs-reconciliation"}:
            raise ValueError("Unsupported execution status")
        if self.output_limit_tokens is not None:
            _integer(self.output_limit_tokens, "output_limit_tokens", minimum=1)
            if self.output_limit_tokens > MAX_OUTPUT_LIMIT_TOKENS:
                raise ValueError(f"output_limit_tokens must be <= {MAX_OUTPUT_LIMIT_TOKENS}")
        if self.enable_thinking is not None and type(self.enable_thinking) is not bool:
            raise ValueError("enable_thinking must be a boolean or None for provider default")
        if self.completion_reason is not None and (
                not isinstance(self.completion_reason, str) or self.completion_reason not in COMPLETION_REASONS):
            raise ValueError("Unsupported normalized completion reason")
        _integer(self.retry_index, "retry_index")
        _optional_times(self, "planned_finish", "actual_start", "actual_finish")
        if self.planned_finish is not None and self.planned_finish < self.planned_candidate.planned_start:
            raise ValueError("planned_finish cannot precede planned_start")
        if self.actual_finish is not None and self.actual_start is not None and self.actual_finish < self.actual_start:
            raise ValueError("actual_finish cannot precede actual_start")
        if self.actual_finish is not None and self.actual_start is None:
            raise ValueError("actual_finish requires actual_start")
        for name in ("executed_model_id", "executed_deployment_id", "provider_reported_model_id", "error"):
            if getattr(self, name) is not None:
                _text(getattr(self, name), name)
        if (self.executed_model_id is None) != (self.executed_deployment_id is None):
            raise ValueError("Executed model and deployment must both be recorded or both be unknown")
        if self.actual_start is not None and self.executed_model_id is None:
            raise ValueError("Dispatched attempts require executed identities")
        if self.actual_start is None and (self.executed_model_id is not None or self.provider_reported_model_id is not None):
            raise ValueError("Executed/reported identities require actual_start")
        if self.completion_reason is not None and self.actual_finish is None:
            raise ValueError("Completion reason requires actual_finish")
        for name in ("wait_seconds", "provider_seconds"):
            if getattr(self, name) is not None:
                _number(getattr(self, name), name)
        for name in ("input_tokens", "output_tokens"):
            if getattr(self, name) is not None:
                _integer(getattr(self, name), name)
        if self.observations is not None and not isinstance(self.observations, StepEstimates):
            raise ValueError("observations must be StepEstimates")
        if self.output is not None and not isinstance(self.output, str):
            raise ValueError("output must be text or unknown")
        if self.status in {"running", "completed"} and (self.actual_start is None or self.executed_model_id is None):
            raise ValueError("Running/completed attempts require actual start and executed identities")
        if self.status == "completed" and (self.actual_finish is None or self.output is None):
            raise ValueError("Completed attempts require finish and output")
        if self.status == "completed" and self.error is not None:
            raise ValueError("Completed attempts cannot have an error")
        if self.status == "running" and any(value is not None for value in (self.actual_finish, self.output, self.error, self.completion_reason)):
            raise ValueError("Running attempts cannot have terminal results")
        if self.status in {"pending", "scheduled"} and any(value is not None for value in
                (self.actual_start, self.actual_finish, self.executed_model_id, self.output,
                 self.wait_seconds, self.provider_seconds, self.input_tokens, self.output_tokens,
                 self.observations, self.error)):
            raise ValueError("Undispatched attempts cannot have execution facts")


@dataclass(frozen=True)
class CarbonSample(Record):
    region: str
    issued_at: datetime
    valid_from: datetime
    valid_until: datetime
    value: float
    source: str
    provenance: str
    unit: str = "gCO2e/kWh"

    def __post_init__(self):
        _text(self.region, "region")
        _text(self.source, "source")
        _times(self, "issued_at", "valid_from", "valid_until")
        if self.valid_until <= self.valid_from:
            raise ValueError("Carbon validity interval must be nonempty")
        _number(self.value, "carbon intensity")
        if self.unit not in {"gCO2e/kWh", "gCO2/kWh"}:
            raise ValueError("Carbon intensity must explicitly use gCO2e/kWh or CO2-only gCO2/kWh")
        if not isinstance(self.provenance, str) or self.provenance not in PROVENANCE - {"unknown"}:
            raise ValueError("Carbon samples require known provenance")

    def is_valid_at(self, instant: datetime, *, as_of: datetime) -> bool:
        """Half-open validity and issue-time check prevent future-data leakage."""
        return self.issued_at <= _utc(as_of, "as_of") and self.valid_from <= _utc(instant, "instant") < self.valid_until


class Clock(Protocol):
    def now(self) -> datetime: ...


class CarbonProvider(Protocol):
    def sample(self, region: str, instant: datetime, *, as_of: datetime) -> CarbonSample | None:
        """Return a valid, available sample, or None when coverage is missing/stale."""
        ...


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(timezone.utc)


def legacy_candidate_to_planned(
    candidate: LegacyCandidate, *, deployment_id: str, reference_time: datetime,
    region: str | None = None,
) -> PlannedCandidate:
    """Explicit migration of an old scenario; region labels are never endpoint proof.

    Integral numeric hours are accepted, fractional hours rejected rather than
    truncated. The caller must resolve a real deployment ID and may supply a known
    region; the old location is retained separately without geographic inference.
    Legacy values are labeled estimated because the old schema cannot prove sensors.
    """
    if not isinstance(candidate, LegacyCandidate):
        raise ValueError("candidate must be a legacy Candidate")
    for name in ("step_id", "model", "location"):
        _text(getattr(candidate, name), f"legacy {name}")
    _text(deployment_id, "deployment_id")
    hours = candidate.time_offset_hr
    _number(hours, "legacy time_offset_hr")
    if int(hours) != hours:
        raise ValueError("Fractional legacy hour offsets are unsupported")
    try:
        start = _utc(reference_time, "reference_time") + timedelta(seconds=int(hours) * 3600)
    except OverflowError:
        raise ValueError("Legacy offset exceeds the supported datetime range") from None
    _number(candidate.est_latency_ms, "legacy latency")
    source = "legacy-candidate-v1-unverified"
    estimates = StepEstimates(
        latency=MetricEstimate(candidate.est_latency_ms / 1000, "s", "estimated", source),
        accuracy=MetricEstimate(candidate.est_accuracy, "ratio", "estimated", source),
        cost=MetricEstimate(candidate.est_cost_usd, "USD", "estimated", source),
        energy=MetricEstimate(candidate.est_energy_kwh, "kWh", "estimated", source),
        carbon=MetricEstimate(candidate.est_carbon_g, "gCO2e", "estimated", source),
    )
    identity = json.dumps([candidate.step_id, candidate.model, deployment_id, start.isoformat()], separators=(",", ":"))
    return PlannedCandidate(
        candidate_id="legacy-" + hashlib.sha256(identity.encode()).hexdigest()[:20],
        step_id=candidate.step_id, model_id=candidate.model, deployment_id=deployment_id,
        planned_start=start, estimates=estimates, region=region, legacy_location=candidate.location,
    )
