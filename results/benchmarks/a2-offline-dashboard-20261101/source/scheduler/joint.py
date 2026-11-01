"""Finite, auditable whole-workflow planning for small A2 DAGs.

The caller supplies calibrated model/deployment/start options. Their latency
must include expected load, transfer and inference time. This module performs
no inference, network access, carbon extrapolation or quality estimation.
Unknown metrics are infeasible; synthetic fixtures remain synthetic evidence.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import itertools
import json
import math
import time
from types import MappingProxyType

from shared.deployments import DeploymentHealth, DeploymentRegistry
from shared.scheduling import METRICS, PlannedCandidate, Record, WorkflowRequest


PLANNER_VERSION = "joint-enumeration-v1"
QUALITY_SEMANTICS = "mean-per-step-quality-estimate; not measured end-to-end quality"
_METRIC_NAMES = ("latency", "accuracy", "cost", "energy", "carbon")


def _number(value, name, *, minimum=0, maximum=None):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be finite and numeric")
    try:
        valid = math.isfinite(value) and value >= minimum and (maximum is None or value <= maximum)
    except OverflowError:
        valid = False
    if not valid:
        raise ValueError(f"{name} is outside its finite allowed range")


def _utc(value, name):
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be a timezone-aware datetime")
    return value.astimezone(timezone.utc)


def _text(value, name):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be nonempty text")


def _mapping(value):
    return MappingProxyType(dict(value))


@dataclass(frozen=True)
class Reservation(Record):
    """Existing occupancy, using half-open intervals [start, finish)."""

    deployment_id: str
    start: datetime
    finish: datetime
    slots: int = 1

    def __post_init__(self):
        _text(self.deployment_id, "deployment_id")
        object.__setattr__(self, "start", _utc(self.start, "start"))
        object.__setattr__(self, "finish", _utc(self.finish, "finish"))
        if self.finish <= self.start:
            raise ValueError("Reservation finish must follow start")
        if type(self.slots) is not int or self.slots < 1:
            raise ValueError("Reservation slots must be a positive integer")


@dataclass(frozen=True)
class CommittedAssignment(Record):
    """Frozen dispatched work; finish is actual or an explicit running estimate.

    The selected candidate is retained unchanged. An overrun is represented by
    finish, rather than by moving its original selection to a different model.
    Resource/quality estimates remain forecasts until the caller replaces them
    with honest observations in a separately retained decision/attempt history.
    """

    candidate: PlannedCandidate
    actual_start: datetime
    finish: datetime
    status: str = "completed"

    def __post_init__(self):
        if not isinstance(self.candidate, PlannedCandidate):
            raise ValueError("Committed assignment requires a PlannedCandidate")
        object.__setattr__(self, "actual_start", _utc(self.actual_start, "actual_start"))
        object.__setattr__(self, "finish", _utc(self.finish, "finish"))
        if self.finish < self.actual_start:
            raise ValueError("Committed finish cannot precede start")
        if not isinstance(self.status, str) or self.status not in {"running", "completed"}:
            raise ValueError("Only running/completed assignments can be committed")


@dataclass(frozen=True)
class ScheduledStep(Record):
    candidate: PlannedCandidate
    start: datetime
    finish: datetime
    committed: bool = False

    def __post_init__(self):
        if not isinstance(self.candidate, PlannedCandidate):
            raise ValueError("Scheduled step requires a PlannedCandidate")
        object.__setattr__(self, "start", _utc(self.start, "start"))
        object.__setattr__(self, "finish", _utc(self.finish, "finish"))
        if self.finish < self.start or type(self.committed) is not bool:
            raise ValueError("Invalid scheduled interval or committed flag")
        if not self.committed:
            duration = self.candidate.estimates.latency.value
            try:
                expected_finish = self.start + timedelta(seconds=duration) if duration is not None else None
            except OverflowError:
                expected_finish = None
            if self.start != self.candidate.planned_start or self.finish != expected_finish:
                raise ValueError("Undispatched schedule must match its candidate time and known duration")


@dataclass(frozen=True)
class PlanMetrics(Record):
    latency: float
    accuracy: float
    cost: float
    energy: float
    carbon: float
    carbon_unit: str
    quality_semantics: str = QUALITY_SEMANTICS

    def __post_init__(self):
        for name in _METRIC_NAMES:
            _number(getattr(self, name), name, maximum=1 if name == "accuracy" else None)
        if not isinstance(self.carbon_unit, str) or self.carbon_unit not in {"gCO2", "gCO2e"}:
            raise ValueError("Plan carbon must retain its CO2/CO2e scope")
        if self.quality_semantics != QUALITY_SEMANTICS:
            raise ValueError("Plan accuracy is a step-quality scheduling proxy")

    def values(self):
        return {name: getattr(self, name) for name in _METRIC_NAMES}


@dataclass(frozen=True)
class NormalizationSnapshot(Record):
    reference_id: str
    bounds: Mapping[str, tuple[float, float]]
    carbon_unit: str
    complete_reference: bool

    def __post_init__(self):
        _text(self.reference_id, "reference_id")
        if len(self.reference_id) != 64 or any(char not in "0123456789abcdef" for char in self.reference_id):
            raise ValueError("Normalization reference_id must be a SHA-256 fingerprint")
        if not isinstance(self.bounds, Mapping) or set(self.bounds) != METRICS:
            raise ValueError("Normalization requires all five metric bounds")
        copy = {}
        for name, limits in self.bounds.items():
            if not isinstance(limits, tuple) or len(limits) != 2:
                raise ValueError("Normalization bounds must be (minimum, maximum)")
            low, high = limits
            _number(low, name)
            _number(high, name)
            if low > high or (name == "accuracy" and high > 1):
                raise ValueError("Invalid normalization range")
            copy[name] = (low, high)
        object.__setattr__(self, "bounds", _mapping(copy))
        if (not isinstance(self.carbon_unit, str) or self.carbon_unit not in {"gCO2", "gCO2e"}
                or type(self.complete_reference) is not bool):
            raise ValueError("Invalid normalization scope")


@dataclass(frozen=True)
class WorkflowPlan(Record):
    assignments: tuple[ScheduledStep, ...]
    metrics: PlanMetrics
    response_at: datetime
    completion_at: datetime
    score: float
    normalized_metrics: Mapping[str, float]
    weighted_contributions: Mapping[str, float]

    def __post_init__(self):
        if not isinstance(self.assignments, tuple) or not self.assignments or any(not isinstance(x, ScheduledStep) for x in self.assignments):
            raise ValueError("Assignments must be a nonempty tuple of ScheduledStep")
        if len({x.candidate.step_id for x in self.assignments}) != len(self.assignments):
            raise ValueError("Plan cannot assign a step more than once")
        if len({x.candidate.candidate_id for x in self.assignments}) != len(self.assignments):
            raise ValueError("Plan candidate IDs must be unique")
        if not isinstance(self.metrics, PlanMetrics):
            raise ValueError("Plan requires typed metrics")
        object.__setattr__(self, "response_at", _utc(self.response_at, "response_at"))
        object.__setattr__(self, "completion_at", _utc(self.completion_at, "completion_at"))
        if (self.response_at > self.completion_at
                or self.completion_at != max(item.finish for item in self.assignments)):
            raise ValueError("Plan completion must match its assignments and follow its response")
        minimum_latency = (self.completion_at - min(item.start for item in self.assignments)).total_seconds()
        if self.metrics.latency < minimum_latency:
            raise ValueError("Arrival-to-completion latency cannot be shorter than its scheduled span")
        if any(getattr(item.candidate.estimates, metric).value is None
               for item in self.assignments for metric in _METRIC_NAMES):
            raise ValueError("A feasible workflow plan cannot contain unknown metrics")
        if any(item.candidate.estimates.carbon.unit != self.metrics.carbon_unit for item in self.assignments):
            raise ValueError("Plan cannot combine different carbon scopes")
        for metric in ("accuracy", "cost", "energy", "carbon"):
            try:
                expected = math.fsum(getattr(item.candidate.estimates, metric).value for item in self.assignments)
                if metric == "accuracy":
                    expected /= len(self.assignments)
            except OverflowError:
                raise ValueError("Plan metric aggregate overflow") from None
            if not math.isclose(getattr(self.metrics, metric), expected, rel_tol=1e-12, abs_tol=1e-15):
                raise ValueError("Plan metric aggregate does not match its assignments")
        _number(self.score, "score", minimum=-1, maximum=1)
        for name in ("normalized_metrics", "weighted_contributions"):
            value = getattr(self, name)
            if not isinstance(value, Mapping) or set(value) != METRICS:
                raise ValueError("Score explanation requires all five metrics")
            for metric, number in value.items():
                _number(number, metric, minimum=-1 if name == "weighted_contributions" else 0, maximum=1)
                if name == "weighted_contributions" and (number > 0 if metric == "accuracy" else number < 0):
                    raise ValueError("Weighted contributions have an invalid metric direction")
            object.__setattr__(self, name, _mapping(value))
        if not math.isclose(self.score, math.fsum(self.weighted_contributions.values()), rel_tol=1e-12, abs_tol=1e-15):
            raise ValueError("Plan score must equal its weighted contributions")


@dataclass(frozen=True)
class PlanResult(Record):
    workflow_id: str
    winner: WorkflowPlan | None
    weights: Mapping[str, float]
    normalization: NormalizationSnapshot | None
    feasible_count: int
    examined_count: int
    total_combinations: int
    search_limit: int
    truncated: bool
    globally_optimal_within_candidates: bool
    planner_seconds: float
    rejected_candidates: Mapping[str, tuple[str, ...]]
    rejection_counts: Mapping[str, int]
    notes: tuple[str, ...]
    planner_version: str = PLANNER_VERSION

    def __post_init__(self):
        _text(self.workflow_id, "workflow_id")
        if self.planner_version != PLANNER_VERSION:
            raise ValueError("Unsupported planner version")
        scaled = _weights(self.weights)
        for name in ("feasible_count", "examined_count", "total_combinations", "search_limit"):
            value = getattr(self, name)
            if type(value) is not int or value < (1 if name == "search_limit" else 0):
                raise ValueError("Search counts must be nonnegative integers and limit must be positive")
        if (self.examined_count != min(self.total_combinations, self.search_limit)
                or self.feasible_count > self.examined_count):
            raise ValueError("Search counts are inconsistent with the bounded enumeration")
        if type(self.truncated) is not bool or self.truncated != (self.examined_count < self.total_combinations):
            raise ValueError("Truncation must match the examined search space")
        if type(self.globally_optimal_within_candidates) is not bool:
            raise ValueError("Optimality must be a boolean")
        if self.winner is not None and not isinstance(self.winner, WorkflowPlan):
            raise ValueError("Winner must be a WorkflowPlan or None")
        if (self.winner is None) != (self.feasible_count == 0):
            raise ValueError("Winner presence must match the feasible count")
        if (self.normalization is None) != (self.winner is None):
            raise ValueError("A feasible result requires a normalization snapshot")
        if self.normalization is not None:
            if not isinstance(self.normalization, NormalizationSnapshot):
                raise ValueError("Normalization must be a typed snapshot")
            if self.normalization.complete_reference != (not self.truncated):
                raise ValueError("Reference completeness must match truncation")
            if self.normalization.carbon_unit != self.winner.metrics.carbon_unit:
                raise ValueError("Winner and normalization carbon scopes must match")
            for metric, value in self.winner.metrics.values().items():
                low, high = self.normalization.bounds[metric]
                if not low <= value <= high:
                    raise ValueError("Winner lies outside its normalization reference")
                normalized = 0.0 if low == high else (value - low) / (high - low)
                contribution = normalized * scaled[metric] * (-1 if metric == "accuracy" else 1)
                if (not math.isclose(self.winner.normalized_metrics[metric], normalized, rel_tol=1e-12, abs_tol=1e-15)
                        or not math.isclose(self.winner.weighted_contributions[metric], contribution,
                                            rel_tol=1e-12, abs_tol=1e-15)):
                    raise ValueError("Winner score does not match the saved weights/reference")
        if self.globally_optimal_within_candidates and (self.winner is None or self.truncated):
            raise ValueError("An absent winner or truncated search cannot claim global optimality")
        _number(self.planner_seconds, "planner_seconds")
        if not isinstance(self.notes, tuple) or not self.notes:
            raise ValueError("Result requires nonempty explanatory notes")
        for note in self.notes:
            _text(note, "note")
        if not isinstance(self.rejected_candidates, Mapping) or not isinstance(self.rejection_counts, Mapping):
            raise ValueError("Rejection details must be mappings")
        static_counts = Counter()
        for candidate_id, reasons in self.rejected_candidates.items():
            _text(candidate_id, "rejected candidate ID")
            if not isinstance(reasons, tuple) or not reasons:
                raise ValueError("Rejected candidates require unique reason tuples")
            for reason in reasons:
                _text(reason, "rejection reason")
            if len(set(reasons)) != len(reasons):
                raise ValueError("Rejected candidates require unique reason tuples")
            static_counts.update(reasons)
        for reason, count in self.rejection_counts.items():
            _text(reason, "rejection reason")
            if type(count) is not int or count < 1:
                raise ValueError("Rejection counts must be positive integers")
        if any(self.rejection_counts.get(reason, 0) < count for reason, count in static_counts.items()):
            raise ValueError("Rejection counts must include all statically rejected candidates")
        if self.winner and any(item.candidate.candidate_id in self.rejected_candidates for item in self.winner.assignments):
            raise ValueError("Winner cannot contain a rejected candidate")
        object.__setattr__(self, "weights", _mapping(self.weights))
        object.__setattr__(self, "rejected_candidates", _mapping(self.rejected_candidates))
        object.__setattr__(self, "rejection_counts", _mapping(self.rejection_counts))


def _weights(weights):
    if not isinstance(weights, Mapping) or set(weights) != METRICS:
        raise ValueError("Weights must specify latency, accuracy, cost, energy and carbon")
    for name, value in weights.items():
        _number(value, f"{name} weight")
    try:
        total = math.fsum(weights.values())
    except OverflowError:
        total = math.inf
    if not math.isfinite(total) or total <= 0:
        raise ValueError("Weights must have a finite positive total")
    return {name: weights[name] / total for name in _METRIC_NAMES}


def _topological(request):
    remaining = {step.step_id: step for step in request.steps}
    result, done = [], set()
    while remaining:
        ready = sorted(key for key, step in remaining.items() if set(step.dependencies) <= done)
        if not ready:
            raise ValueError("Workflow dependencies contain a cycle")
        for key in ready:
            result.append(remaining.pop(key))
            done.add(key)
    return tuple(result)


def _capacity_ok(start, finish, others, capacity, *, slots=1):
    if finish == start:
        return True
    events = defaultdict(int)
    events[start] += slots
    events[finish] -= slots
    for left, right, count in others:
        if right <= start or left >= finish or left == right:
            continue
        events[max(start, left)] += count
        events[min(finish, right)] -= count
    active = 0
    for instant in sorted(events):
        active += events[instant]
        if active > capacity:
            return False
    return True


def _earliest_slot(ready, duration, others, capacity):
    # Capacity changes only at reservation ends; a nonpreemptive task that cannot
    # fit at ready cannot first become feasible at a reservation start.
    starts = sorted({ready, *(right for left, right, count in others if right >= ready)})
    for start in starts:
        try:
            finish = start + duration
        except OverflowError:
            continue
        if _capacity_ok(start, finish, others, capacity):
            return start
    return None


def _evaluate(request, ordered, assignments, now, capacities, reservations):
    by_id = {item.candidate.step_id: item for item in assignments}
    reasons = set()
    occupancy = defaultdict(list)
    for item in reservations:
        occupancy[item.deployment_id].append((item.start, item.finish, item.slots))
    for item in assignments:
        occupancy[item.candidate.deployment_id].append((item.start, item.finish, 1))
    for step in ordered:
        item = by_id[step.step_id]
        ready = max([step.earliest_start, *(by_id[key].finish for key in step.dependencies)]
                    + ([] if item.committed else [now]))
        if item.start < ready:
            reasons.add("dependency-or-earliest-start")
        if item.finish > step.latest_finish:
            reasons.add("step-deadline")
        if item.finish > (request.response_deadline if step.required_for_response else request.completion_deadline):
            reasons.add("workflow-deadline")
        deployment = item.candidate.deployment_id
        others = list(occupancy[deployment])
        others.remove((item.start, item.finish, 1))
        if not _capacity_ok(item.start, item.finish, others, capacities[deployment]):
            reasons.add("deployment-capacity")
        if not step.allow_deferral and not item.committed:
            earliest = _earliest_slot(ready, item.finish - item.start, others, capacities[deployment])
            if earliest != item.start:
                reasons.add("deferral-not-permitted")
    if reasons:
        return None, tuple(sorted(reasons))
    response = max((by_id[step.step_id].finish for step in ordered if step.required_for_response),
                   default=request.arrived_at)
    completion = max(item.finish for item in assignments)
    try:
        metrics = PlanMetrics(
            latency=(completion - request.arrived_at).total_seconds(),
            accuracy=math.fsum(item.candidate.estimates.accuracy.value for item in assignments) / len(assignments),
            cost=math.fsum(item.candidate.estimates.cost.value for item in assignments),
            energy=math.fsum(item.candidate.estimates.energy.value for item in assignments),
            carbon=math.fsum(item.candidate.estimates.carbon.value for item in assignments),
            carbon_unit=assignments[0].candidate.estimates.carbon.unit,
        )
    except (ValueError, OverflowError):
        return None, ("aggregate-metric-overflow",)
    return (assignments, metrics, response, completion), ()


def _rank(raw, normalization, scaled_weights):
    assignments, metrics, response, completion = raw
    normalized, contributions = {}, {}
    for name, value in metrics.values().items():
        low, high = normalization.bounds[name]
        if value < low or value > high:
            raise ValueError("Plan lies outside supplied normalization reference")
        normalized[name] = 0.0 if high == low else (value - low) / (high - low)
        contributions[name] = scaled_weights[name] * normalized[name] * (-1 if name == "accuracy" else 1)
    return WorkflowPlan(assignments, metrics, response, completion, math.fsum(contributions.values()),
                        normalized, contributions)


def _tie_key(plan):
    return (plan.score, tuple((item.candidate.step_id, item.candidate.candidate_id)
                             for item in plan.assignments))


def plan_workflow(
    request: WorkflowRequest,
    candidates_by_step: Mapping[str, Sequence[PlannedCandidate]],
    weights: Mapping[str, float],
    *,
    now: datetime,
    quality_floor: float | Mapping[str, float] = 0.0,
    search_limit: int = 100_000,
    registry: DeploymentRegistry | None = None,
    health: Mapping[str, DeploymentHealth] | None = None,
    health_max_age_seconds: float = 300,
    capacities: Mapping[str, int] | None = None,
    reservations: Sequence[Reservation] = (),
    normalization: NormalizationSnapshot | None = None,
    fixed_assignments: Sequence[CommittedAssignment] = (),
    carbon_unit: str | None = None,
) -> PlanResult:
    """Enumerate complete finite assignments and return explicit infeasibility.

    Every metric must be known, including zero-weight metrics, so policies rank
    the same eligible reference set. A zero quality floor does not authorize
    unknown quality. Registry/health checks are optional only for callers that
    already screened eligibility; that boundary is retained in result notes.
    No health observation predicts that a future endpoint will remain healthy.

    A search limit counts complete assignments examined, including rejected
    assignments. Enumeration order and ties do not depend on policy weights.
    Optimality is only within the supplied finite candidate set after exhaustive
    search; it is never a claim about continuous time or unconfigured hardware.
    """
    started = time.perf_counter()
    if not isinstance(request, WorkflowRequest):
        raise ValueError("request must be a WorkflowRequest")
    scaled = _weights(weights)
    now = _utc(now, "now")
    if now < request.arrived_at:
        raise ValueError("Planning time cannot precede workflow arrival")
    if type(search_limit) is not int or search_limit < 1:
        raise ValueError("search_limit must be a positive integer")
    _number(health_max_age_seconds, "health_max_age_seconds")
    if registry is not None and not isinstance(registry, DeploymentRegistry):
        raise ValueError("registry must be a DeploymentRegistry")
    if health is not None and (not isinstance(health, Mapping) or any(
            not isinstance(item, DeploymentHealth) or key != item.deployment_id for key, item in health.items())):
        raise ValueError("health must map deployment IDs to matching health observations")
    if capacities is not None and not isinstance(capacities, Mapping):
        raise ValueError("capacities must map deployment IDs to positive integers")
    capacity_map = dict(capacities or {})
    for key, value in capacity_map.items():
        _text(key, "capacity deployment ID")
        if type(value) is not int or value < 1:
            raise ValueError("Capacity must be a positive integer")
        if registry is not None and value > registry.get(key).max_concurrency:
            raise ValueError("Capacity cannot exceed the registry concurrency limit")
    if not isinstance(reservations, (tuple, list)) or any(not isinstance(x, Reservation) for x in reservations):
        raise ValueError("reservations must contain Reservation records")
    if not isinstance(fixed_assignments, (tuple, list)) or any(not isinstance(x, CommittedAssignment) for x in fixed_assignments):
        raise ValueError("fixed_assignments must contain CommittedAssignment records")
    ordered = _topological(request)
    by_step = {step.step_id: step for step in ordered}
    if not isinstance(candidates_by_step, Mapping) or set(candidates_by_step) - by_step.keys():
        raise ValueError("Candidate groups must refer to workflow steps")
    if isinstance(quality_floor, Mapping):
        if set(quality_floor) - by_step.keys():
            raise ValueError("Quality floors refer to an unknown workflow step")
        floors = {key: quality_floor.get(key, 0.0) for key in by_step}
    else:
        floors = {key: quality_floor for key in by_step}
    for value in floors.values():
        _number(value, "quality_floor", maximum=1)
    fixed = {}
    for item in fixed_assignments:
        key = item.candidate.step_id
        if key not in by_step or key in fixed:
            raise ValueError("Committed assignments require unique workflow steps")
        if item.actual_start > now or (item.status == "completed" and item.finish > now):
            raise ValueError("Committed actual times cannot be in the future")
        if item.status == "running" and item.finish < now:
            raise ValueError("Running finish estimate must cover current time")
        fixed[key] = item
    candidate_ids = set()
    all_candidates = []
    pools = {}
    for step in ordered:
        supplied = candidates_by_step.get(step.step_id, ())
        if not isinstance(supplied, (tuple, list)):
            raise ValueError("Each candidate group must be a list or tuple")
        if step.step_id in fixed:
            # Inputs for committed steps cannot propose replacements, even if
            # they promise a better score. Their original selection is frozen.
            if supplied and tuple(supplied) != (fixed[step.step_id].candidate,):
                raise ValueError("Cannot replace an already-dispatched assignment")
            supplied = (fixed[step.step_id].candidate,)
        pools[step.step_id] = []
        for candidate in supplied:
            if not isinstance(candidate, PlannedCandidate) or candidate.step_id != step.step_id:
                raise ValueError("Candidate must belong to its workflow step")
            if candidate.candidate_id in candidate_ids:
                raise ValueError("Candidate IDs must be unique within a planning problem")
            candidate_ids.add(candidate.candidate_id)
            all_candidates.append(candidate)
            pools[step.step_id].append(candidate)
    if carbon_unit is not None and carbon_unit not in {"gCO2", "gCO2e"}:
        raise ValueError("carbon_unit must explicitly use gCO2 or gCO2e")
    units = {item.estimates.carbon.unit for item in all_candidates if item.estimates.carbon.value is not None}
    if carbon_unit is None and len(units) > 1:
        raise ValueError("Mixed carbon scopes require an explicit carbon_unit selection")
    scope = carbon_unit or next(iter(units), "gCO2e")
    rejected, counts = {}, Counter()
    for step in ordered:
        accepted = []
        for candidate in sorted(pools[step.step_id], key=lambda item: item.candidate_id):
            reasons = []
            for metric in _METRIC_NAMES:
                if getattr(candidate.estimates, metric).value is None:
                    reasons.append(f"unknown-{metric}")
            if candidate.estimates.accuracy.value is not None and candidate.estimates.accuracy.value < floors[step.step_id]:
                reasons.append("minimum-quality")
            if candidate.estimates.carbon.unit != scope:
                reasons.append("carbon-scope")
            deployment = None
            if registry is not None:
                try:
                    deployment = registry.require_model(candidate.deployment_id, candidate.model_id)
                except ValueError:
                    reasons.append("unsupported-deployment-model")
                if deployment is not None and candidate.region != deployment.region:
                    reasons.append("deployment-region-mismatch")
            capacity_map.setdefault(candidate.deployment_id, deployment.max_concurrency if deployment else 1)
            if health is not None and step.step_id not in fixed:
                observation = health.get(candidate.deployment_id)
                if observation is None or observation.reachable is not True:
                    reasons.append("deployment-unavailable-or-unknown")
                elif observation.checked_at > now or (now - observation.checked_at).total_seconds() > health_max_age_seconds:
                    reasons.append("deployment-health-stale-or-future")
                elif candidate.model_id not in observation.available_models:
                    reasons.append("model-unavailable")
            if step.step_id not in fixed:
                if candidate.planned_start < max(now, step.earliest_start):
                    reasons.append("past-or-before-earliest-start")
                if candidate.estimates.latency.value is not None:
                    try:
                        finish = candidate.planned_start + timedelta(seconds=candidate.estimates.latency.value)
                        if finish > step.latest_finish:
                            reasons.append("step-deadline")
                    except OverflowError:
                        reasons.append("duration-overflow")
            if reasons:
                rejected[candidate.candidate_id] = tuple(sorted(set(reasons)))
                counts.update(set(reasons))
            else:
                accepted.append(candidate)
        pools[step.step_id] = accepted
        if not accepted:
            counts["empty-step-pool"] += 1
    for reservation in reservations:
        if registry is not None:
            capacity_map.setdefault(reservation.deployment_id, registry.get(reservation.deployment_id).max_concurrency)
        else:
            capacity_map.setdefault(reservation.deployment_id, 1)
    total = math.prod(len(pools[step.step_id]) for step in ordered)
    feasible, examined = [], 0
    for choices in itertools.islice(itertools.product(*(pools[step.step_id] for step in ordered)), search_limit):
        examined += 1
        assignments = []
        for candidate in choices:
            committed = fixed.get(candidate.step_id)
            if committed:
                assignments.append(ScheduledStep(candidate, committed.actual_start, committed.finish, True))
            else:
                assignments.append(ScheduledStep(candidate, candidate.planned_start,
                                                 candidate.planned_start + timedelta(seconds=candidate.estimates.latency.value)))
        raw, reasons = _evaluate(request, ordered, tuple(assignments), now, capacity_map, reservations)
        if reasons:
            counts.update(reasons)
        else:
            feasible.append(raw)
    truncated = examined < total
    snapshot, winner = None, None
    if feasible:
        bounds = {name: (min(getattr(item[1], name) for item in feasible),
                         max(getattr(item[1], name) for item in feasible)) for name in _METRIC_NAMES}
        request_record = request.to_dict()
        request_record.pop("policy")
        request_record.pop("workflow_id")
        reference = {
            "planner_version": PLANNER_VERSION, "request": request_record, "now": now.isoformat(),
            "candidates": [item.to_dict() for item in sorted(all_candidates, key=lambda x: x.candidate_id)],
            "quality_floors": floors, "capacity": capacity_map,
            "reservations": [item.to_dict() for item in reservations],
            "committed": [item.to_dict() for item in fixed_assignments],
            "registry": registry.config_fingerprint() if registry else None,
            "health": [item.to_dict() for _, item in sorted(health.items())] if health is not None else None,
            "health_max_age_seconds": health_max_age_seconds, "carbon_unit": scope,
            "examined_count": examined, "truncated": truncated, "bounds": bounds,
        }
        reference_id = hashlib.sha256(json.dumps(reference, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()
        snapshot = NormalizationSnapshot(reference_id, bounds, scope, not truncated)
        if normalization is not None:
            if not isinstance(normalization, NormalizationSnapshot) or normalization != snapshot:
                raise ValueError("Normalization snapshot does not match this planning reference")
            snapshot = normalization
        winner = min((_rank(raw, snapshot, scaled) for raw in feasible), key=_tie_key)
    elif normalization is not None:
        raise ValueError("An infeasible problem cannot reuse a feasible normalization snapshot")
    notes = [QUALITY_SEMANTICS, "Latency is workflow arrival-to-completion, including waits; parallel durations are not summed."]
    notes.append("Eligibility checked against registry and health." if registry is not None and health is not None
                 else "Caller is responsible for eligibility not covered by supplied registry/health.")
    notes.append("Committed resource/quality values remain the retained candidate estimates; actual timings are frozen." if fixed else
                 "Candidate latency must include transfer/load/inference overhead; no overhead is invented.")
    if truncated:
        notes.append("Search truncated: winner and normalization cover only the examined finite assignments.")
    if any(getattr(item.estimates, metric).provenance == "simulated" for item in all_candidates for metric in _METRIC_NAMES):
        notes.append("Contains simulated estimates; this result is not live measurement evidence.")
    return PlanResult(request.workflow_id, winner, weights, snapshot, len(feasible), examined, total, search_limit,
                      truncated, bool(winner and not truncated), time.perf_counter() - started,
                      rejected, dict(sorted(counts.items())), tuple(notes))


def replan_workflow(
    request: WorkflowRequest,
    candidates_by_step: Mapping[str, Sequence[PlannedCandidate]],
    weights: Mapping[str, float],
    *,
    now: datetime,
    fixed_assignments: Sequence[CommittedAssignment],
    previous_plan: WorkflowPlan | None = None,
    minimum_improvement: float = 0.0,
    **kwargs,
) -> PlanResult:
    """Replan unfinished work, retaining a feasible incumbent for small gains.

    The threshold is measured in normalized weighted-score units. An incumbent
    must still be offered in the candidate pools and pass current constraints;
    a threshold cannot retain obsolete capacity, deadlines or eligibility.
    """
    _number(minimum_improvement, "minimum_improvement")
    if previous_plan is not None and not isinstance(previous_plan, WorkflowPlan):
        raise ValueError("previous_plan must be a WorkflowPlan")
    result = plan_workflow(request, candidates_by_step, weights, now=now,
                           fixed_assignments=fixed_assignments, **kwargs)
    if previous_plan is None or result.winner is None:
        return result
    offered = {item.candidate_id: item for values in candidates_by_step.values() for item in values}
    frozen = {item.candidate.step_id: item for item in fixed_assignments}
    incumbent = []
    for item in previous_plan.assignments:
        step_id = item.candidate.step_id
        if step_id in frozen:
            fixed = frozen[step_id]
            if item.candidate != fixed.candidate:
                return result
            incumbent.append(ScheduledStep(fixed.candidate, fixed.actual_start, fixed.finish, True))
        elif offered.get(item.candidate.candidate_id) == item.candidate and item.candidate.candidate_id not in result.rejected_candidates:
            incumbent.append(ScheduledStep(item.candidate, item.candidate.planned_start,
                                           item.candidate.planned_start + timedelta(seconds=item.candidate.estimates.latency.value)))
        else:
            return result
    if {item.candidate.step_id for item in incumbent} != {step.step_id for step in request.steps}:
        return result
    # Check membership in the examined Cartesian-product prefix, rather than
    # merely checking that the incumbent's metrics happen to fit its bounds.
    # Otherwise an unexamined incumbent could become a winner while feasible
    # counts and the normalization still described only the bounded prefix.
    incumbent_ids = {item.candidate.step_id: item.candidate.candidate_id for item in incumbent}
    combination_index = 0
    for step in _topological(request):
        options = ([frozen[step.step_id].candidate] if step.step_id in frozen else
                   [item for item in candidates_by_step.get(step.step_id, ())
                    if item.candidate_id not in result.rejected_candidates])
        ids = sorted(item.candidate_id for item in options)
        if incumbent_ids[step.step_id] not in ids:
            return result
        combination_index = combination_index * len(ids) + ids.index(incumbent_ids[step.step_id])
    if combination_index >= result.examined_count:
        return result
    capacity_map = dict(kwargs.get("capacities") or {})
    registry = kwargs.get("registry")
    for item in incumbent:
        key = item.candidate.deployment_id
        capacity_map.setdefault(key, registry.get(key).max_concurrency if registry else 1)
    raw, reasons = _evaluate(request, _topological(request), tuple(incumbent), _utc(now, "now"),
                             capacity_map, kwargs.get("reservations", ()))
    if reasons:
        return result
    try:
        ranked = _rank(raw, result.normalization, _weights(weights))
    except ValueError:
        # A truncated search may not have included the incumbent; its score
        # cannot be compared outside that bounded normalization reference.
        return result
    if ranked.score - result.winner.score <= minimum_improvement:
        from dataclasses import replace
        return replace(result, winner=ranked, globally_optimal_within_candidates=False,
                       notes=result.notes + ("Retained feasible incumbent: improvement did not exceed the replan threshold.",))
    return result
