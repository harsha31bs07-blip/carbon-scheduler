"""Prepare joint decisions from actual outputs and explicit workload forecasts.

This boundary performs no inference or queue mutation. The worker must install a
decision atomically, refresh eligibility, and materialize a ready step again
before dispatch. Provisional downstream candidates are never dispatch inputs.
"""

from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import math
import time

from data.profiles import evaluate_profiles
from data.quality_reviews import fingerprint
from scheduler.joint import CommittedAssignment, PlanResult, Reservation, WorkflowPlan, replan_workflow
from scheduler.profile_candidates import generate_forecast_candidates, generate_profile_candidates
from shared.deployments import DeploymentHealth, DeploymentRegistry
from shared.scheduling import WorkloadSizeForecast, WorkflowRequest
from workflow.connected import workload_fingerprint
from workflow.planning_constraints import candidate_exclusion, pair_permitted, validate_allowed_pairs
from workflow.workload import KnowledgeBase, STEP_ORDER, render_step, validate_step_output


PLANNING_SERVICE_VERSION = "staged-connected-planning.v1"
_WEIGHTS = {
    "speed": {"latency": .6, "cost": .1, "energy": .1, "carbon": .1, "accuracy": .1},
    "balanced": {"latency": .2, "cost": .2, "energy": .2, "carbon": .2, "accuracy": .2},
    "eco": {"latency": .1, "cost": .1, "energy": .3, "carbon": .4, "accuracy": .1},
}


def _utc(value):
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("Planning requires timezone-aware timestamps")
    return value.astimezone(timezone.utc)


def _seconds(value, name):
    try:
        valid = type(value) in {int, float} and math.isfinite(value) and value >= 0
    except OverflowError:
        valid = False
    if not valid:
        raise ValueError(f"{name} must be finite nonnegative seconds")


def _weights(policy, custom):
    if policy == "custom":
        if not isinstance(custom, Mapping) or set(custom) != set(_WEIGHTS["balanced"]):
            raise ValueError("Custom weights must specify exactly the five metric names")
        weights = dict(custom)
    else:
        if policy not in _WEIGHTS or custom is not None:
            raise ValueError("Unsupported policy or unexpected custom weights")
        weights = dict(_WEIGHTS[policy])
    for name, value in weights.items():
        _seconds(value, name)
    try:
        total = math.fsum(weights.values())
    except OverflowError as exc:
        raise ValueError("Weight sum must be finite") from exc
    if not math.isfinite(total) or total <= 0:
        raise ValueError("Weights require a positive finite total")
    return {name: value / total for name, value in weights.items()}


@dataclass(frozen=True)
class PreparedPlan:
    result: PlanResult
    report: dict
    rendered_steps: dict


def prepare_plan(request, registry, profiles, *, kb, now, health, carbon_provider,
                 outputs=None, fixed_assignments=(), forecasts=None, start_offsets_seconds=(0,),
                 reservations=(), custom_weights=None, quality_floor=.8, search_limit=100_000,
                 health_max_age_seconds=300, previous_plan=None, minimum_improvement=0.,
                 trigger="submission", experiment_mode="live", allowed_pairs=None):
    """Create a complete finite plan while preserving missing-input diagnostics.

    Outputs must be accepted results of completed committed assignments. Ready
    prompts are rendered from those outputs. A blocked step can only receive
    options from a current, workload-bound size forecast; no fixture answer is
    inserted. Both endpoints must fit the matching profile's observed range.
    This service makes no guarantee that future prompts fit a forecast interval.
    """
    started = time.perf_counter()
    if not isinstance(request, WorkflowRequest) or not isinstance(registry, DeploymentRegistry) or not isinstance(kb, KnowledgeBase):
        raise ValueError("Planning requires a workflow request, registry and KB")
    if tuple(step.step_id for step in request.steps) != STEP_ORDER:
        raise ValueError("This planning service supports the connected support workload")
    allowed_pairs = validate_allowed_pairs(allowed_pairs, registry)
    if experiment_mode not in {"live", "simulation"} or not isinstance(trigger, str) or not trigger.strip():
        raise ValueError("Planning mode and trigger must be explicit")
    evaluate_profiles(profiles, [])  # Validate the complete frozen envelope.
    if experiment_mode == "live" and "quality_policy" not in profiles:
        raise ValueError("Live planning requires an explicit frozen quality coverage policy")
    now = _utc(now)
    if previous_plan is not None and not isinstance(previous_plan, WorkflowPlan):
        raise ValueError("Previous plan must be a typed WorkflowPlan")
    if now < request.arrived_at:
        raise ValueError("Planning cannot precede workflow arrival")
    if not isinstance(health, Mapping) or any(not isinstance(item, DeploymentHealth) or key != item.deployment_id
                                              for key, item in health.items()):
        raise ValueError("Planning requires explicit deployment health snapshots")
    if not isinstance(reservations, (tuple, list)) or any(not isinstance(item, Reservation) for item in reservations):
        raise ValueError("Reservations must be typed occupancy intervals")
    if not isinstance(fixed_assignments, (tuple, list)) or any(not isinstance(item, CommittedAssignment) for item in fixed_assignments):
        raise ValueError("Fixed assignments must be typed committed execution facts")
    fixed = {item.candidate.step_id: item for item in fixed_assignments}
    if len(fixed) != len(fixed_assignments) or set(fixed) - set(STEP_ORDER):
        raise ValueError("Committed steps must be unique and belong to the workflow")
    if any(not pair_permitted(allowed_pairs, key, item.candidate.deployment_id, item.candidate.model_id)
           for key, item in fixed.items()):
        raise ValueError("Committed work is outside permitted candidate pairs")
    outputs = {} if outputs is None else outputs
    forecasts = {} if forecasts is None else forecasts
    if not isinstance(outputs, Mapping) or set(outputs) != {key for key, item in fixed.items() if item.status == "completed"}:
        raise ValueError("Accepted outputs must match exactly the completed committed steps")
    if not isinstance(forecasts, Mapping) or any(key not in STEP_ORDER or not isinstance(item, WorkloadSizeForecast)
            or key != item.step_id for key, item in forecasts.items()):
        raise ValueError("Forecasts must map workflow step IDs to their typed forecasts")
    if not isinstance(start_offsets_seconds, (tuple, list)) or not 1 <= len(start_offsets_seconds) <= 128:
        raise ValueError("Start grid requires one to 128 offsets")
    weights = _weights(request.policy, custom_weights)
    grid = set()
    for offset in start_offsets_seconds:
        _seconds(offset, "start offset")
        try:
            grid.add(now + timedelta(seconds=offset))
        except OverflowError as exc:
            raise ValueError("Start offset exceeds the supported timestamp range") from exc
    if len(grid) != len(start_offsets_seconds):
        raise ValueError("Start offsets must be unique")
    if previous_plan is not None:
        # Incumbent windows must remain offered for a meaningful improvement
        # threshold; eligibility/forecast/actual prompt changes can still remove it.
        grid.update(item.candidate.planned_start for item in previous_plan.assignments
                    if item.candidate.planned_start >= now)
    context_id = workload_fingerprint(request, kb)
    candidates, finishes, contexts, diagnostics, rendered, mode_exclusions = {}, {}, [], [], {}, {}
    for step in request.steps:
        committed = fixed.get(step.step_id)
        ready = all(key in outputs for key in step.dependencies)
        actual = render_step(request, step.step_id, outputs, kb=kb) if ready else None
        if committed is not None and (not ready or any(fixed[key].status != "completed" for key in step.dependencies)):
            raise ValueError("Dispatched work requires completed prerequisites")
        if committed is not None and committed.status == "completed":
            validate_step_output(step.step_id, outputs[step.step_id], source_ids=actual.source_ids)
        context = {"step_id": step.step_id, "committed": committed is not None,
                   "input_mode": "materialized" if actual else "provisional" if step.step_id in forecasts else "unavailable",
                   "input_fingerprint": actual.input_fingerprint if actual else None,
                   "materialized_step": actual.step.to_dict() if actual else None,
                   "source_ids": list(actual.source_ids) if actual else [],
                   "forecast": forecasts[step.step_id].to_dict() if actual is None and step.step_id in forecasts else None}
        contexts.append(context)
        if actual is not None:
            rendered[step.step_id] = actual
        if committed is not None:
            candidates[step.step_id] = ()
            finishes[step.step_id] = {committed.finish}
            continue
        if actual is None and step.step_id not in forecasts:
            context["reason"] = "missing-workload-size-forecast"
            candidates[step.step_id], finishes[step.step_id] = (), set()
            continue
        active_step = actual.step if actual else step
        base = max(now, step.earliest_start)
        starts = set(grid) if step.allow_deferral else {base}
        starts.update(item.finish for item in reservations if item.finish >= base)
        if step.dependencies and all(finishes.get(key) for key in step.dependencies):
            minimum_ready = max(min(finishes[key]) for key in step.dependencies)
            starts.update(max(base, instant) for key in step.dependencies for instant in finishes[key] if instant >= minimum_ready)
        starts = {instant for instant in starts if instant >= base}
        if len(starts) > 256:
            raise ValueError("Dependency/capacity start closure exceeds 256 options")
        kwargs = dict(as_of=now, now=now, carbon_provider=carbon_provider)
        if actual is not None:
            generated = generate_profile_candidates(active_step, request.workload_version, registry, profiles,
                                                     tuple(sorted(starts)), **kwargs)
        else:
            generated = generate_forecast_candidates(active_step, request.workload_version, registry, profiles,
                tuple(sorted(starts)), forecast=forecasts[step.step_id], workflow_fingerprint=context_id, **kwargs)
        exclusions = {candidate.candidate_id: reason for candidate in generated.candidates
            if (reason := candidate_exclusion(candidate, allowed_pairs, live=experiment_mode == "live")) is not None}
        mode_exclusions.update(exclusions)
        offered = tuple(candidate for candidate in generated.candidates if candidate.candidate_id not in exclusions)
        candidates[step.step_id] = offered
        diagnostics.append(generated.to_dict())
        finishes[step.step_id] = set()
        for candidate in offered:
            if candidate.estimates.latency.value is not None:
                try:
                    finishes[step.step_id].add(candidate.planned_start + timedelta(seconds=candidate.estimates.latency.value))
                except OverflowError:
                    pass  # Generation already retains the overflow rejection.
    result = replan_workflow(request, candidates, weights, now=now, fixed_assignments=fixed_assignments,
        previous_plan=previous_plan, minimum_improvement=minimum_improvement, registry=registry, health=health,
        reservations=reservations, quality_floor=quality_floor, health_max_age_seconds=health_max_age_seconds,
        search_limit=search_limit)
    dispatchable = []
    if result.winner is not None:
        dispatchable = [item.candidate.step_id for item in result.winner.assignments
                        if not item.committed and item.candidate.step_id in rendered
                        and item.candidate.planned_start <= now]
    report = {"schema_version": 1, "record_type": "PreparedWorkflowPlan", "service_version": PLANNING_SERVICE_VERSION,
              "experiment_mode": experiment_mode, "inference_performed": False, "trigger": trigger,
              "now": now.isoformat(), "request": request.to_dict(), "workload_fingerprint": context_id,
              "registry_snapshot": registry.to_dict(), "registry_fingerprint": registry.config_fingerprint(),
              "profile_fingerprint": profiles["fingerprint"], "calibration_id": profiles["calibration_id"],
              "quality_policy": deepcopy(profiles.get("quality_policy")), "kb_fingerprint": kb.fingerprint(),
              "accepted_outputs": dict(outputs),
              "minimum_improvement": minimum_improvement,
              "previous_plan_fingerprint": fingerprint(previous_plan.to_dict()) if previous_plan else None,
              "start_offsets_seconds": list(start_offsets_seconds),
              "search_limit": search_limit,
              "weights": weights, "quality_floor": dict(quality_floor) if isinstance(quality_floor, Mapping) else quality_floor,
              "health_max_age_seconds": health_max_age_seconds,
              "health_snapshot": [item.to_dict() for _, item in sorted(health.items())],
              "reservations": [item.to_dict() for item in reservations],
              "committed_assignments": [item.to_dict() for item in fixed_assignments],
              "contexts": contexts, "candidate_diagnostics": diagnostics, "result": result.to_dict(),
              "mode_exclusions": mode_exclusions,
              "dispatchable_step_ids": dispatchable, "preparation_seconds": time.perf_counter() - started,
              "notes": ["A provisional option must be re-rendered, re-estimated and revalidated before dispatch.",
                        "Forecast intervals are calibrated assumptions, not guaranteed workload bounds.",
                        "A prepared plan is not an installed queue decision or evidence of executed routing."]}
    if allowed_pairs is not None:
        report["allowed_pairs"] = allowed_pairs
    report["fingerprint"] = fingerprint(report)
    return PreparedPlan(result, report, rendered)
