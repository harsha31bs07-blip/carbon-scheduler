"""Strict offline validation for durable planning inputs and decision evidence.

No provider transports, worker modules or mutable configuration files are read.
The persisted KB, profiles and carbon snapshots suffice to check a decision.
"""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta, timezone
from functools import wraps
import hashlib
import json
import math
import re

from data.carbon_sources import HistoricalCarbonTrace, StaticCarbonEstimate
from data.profiles import evaluate_profiles
from data.quality_policy import validate_quality_policy
from data.quality_reviews import fingerprint
from scheduler.joint import CommittedAssignment, PlanResult, Reservation, plan_workflow
from scheduler.profile_candidates import generate_forecast_candidates, generate_profile_candidates
from shared.deployments import DeploymentHealth, DeploymentRegistry
from shared.scheduling import PlannedCandidate, StepRequest, WorkloadSizeForecast, WorkflowRequest
from workflow.planning_constraints import candidate_exclusion, pair_permitted, validate_allowed_pairs
from workflow.workload import KnowledgeArticle, KnowledgeBase, STEP_ORDER, _workload_version, render_step, validate_step_output


SERVICE_VERSION = "staged-connected-planning.v1"
WORKER_VERSION = "sqlite-planned-worker-v1"
_METRICS = ("latency", "accuracy", "cost", "energy", "carbon")
_POLICIES = {
    "speed": {"latency": .6, "cost": .1, "energy": .1, "carbon": .1, "accuracy": .1},
    "balanced": dict.fromkeys(_METRICS, .2),
    "eco": {"latency": .1, "cost": .1, "energy": .3, "carbon": .4, "accuracy": .1},
}
_CONTEXT_FIELDS = {
    "schema_version", "record_type", "profiles", "forecasts", "knowledge_base", "carbon",
    "custom_weights", "quality_floor", "start_offsets_seconds", "search_limit", "health_max_age_seconds",
    "minimum_improvement", "replan_interval_seconds", "probe_timeout_seconds", "max_decision_age_seconds",
}
_REPORT_FIELDS = {
    "schema_version", "record_type", "service_version", "experiment_mode", "inference_performed", "trigger",
    "now", "request", "workload_fingerprint", "registry_snapshot", "registry_fingerprint", "profile_fingerprint",
    "calibration_id", "quality_policy", "kb_fingerprint", "accepted_outputs", "minimum_improvement",
    "previous_plan_fingerprint", "start_offsets_seconds", "search_limit", "weights", "quality_floor",
    "health_max_age_seconds", "health_snapshot", "reservations", "committed_assignments", "contexts",
    "candidate_diagnostics", "result", "mode_exclusions", "dispatchable_step_ids", "preparation_seconds",
    "notes", "fingerprint",
}
_FAILURE_FIELDS = {
    "schema_version", "record_type", "service_version", "experiment_mode", "inference_performed",
    "request", "now", "trigger", "error_code", "fingerprint",
}
_SHA = re.compile(r"[0-9a-f]{64}\Z")
_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}\Z")


def _malformed_as_value_error(function):
    @wraps(function)
    def checked(*args, **kwargs):
        try:
            return function(*args, **kwargs)
        except (KeyError, TypeError, AttributeError, OverflowError) as exc:
            raise ValueError("Malformed durable planning evidence") from exc
    return checked


def _text(value, name):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be nonempty text")


def _number(value, name, *, minimum=0, maximum=None, positive=False):
    try:
        valid = (type(value) in {int, float} and math.isfinite(value) and value >= minimum
                 and (not positive or value > 0) and (maximum is None or value <= maximum))
    except OverflowError:
        valid = False
    if not valid:
        raise ValueError(f"{name} must be finite and within its allowed bounds")


def _hash(value, name):
    if not isinstance(value, str) or not _SHA.fullmatch(value):
        raise ValueError(f"{name} must be a SHA256 value")


def _stamp(value, name):
    if not isinstance(value, str):
        raise ValueError(f"{name} must be a timezone-aware timestamp")
    try:
        result = datetime.fromisoformat(value)
        if result.tzinfo is None or result.utcoffset() is None:
            raise ValueError("naive timestamp")
        return result.astimezone(timezone.utc)
    except (ValueError, OverflowError) as exc:
        raise ValueError(f"{name} must be a timezone-aware timestamp") from exc


def _request(value):
    result = value if isinstance(value, WorkflowRequest) else WorkflowRequest.from_dict(value)
    if tuple(step.step_id for step in result.steps) != STEP_ORDER:
        raise ValueError("Durable planning requires the connected support workflow")
    if result.policy not in {*_POLICIES, "custom"}:
        raise ValueError("Durable planning requires an optimized policy")
    return result


def _workload_fingerprint(request, kb):
    payload = {"input_text": request.input_text, "workload_version": request.workload_version,
               "knowledge_base": kb.fingerprint()}
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode("utf-8")).hexdigest()


def _weights(policy, custom):
    if policy == "custom":
        if not isinstance(custom, dict) or set(custom) != set(_METRICS):
            raise ValueError("Custom weights require exactly the five metrics")
        values = custom
    elif policy in _POLICIES and custom is None:
        values = _POLICIES[policy]
    else:
        raise ValueError("Policy and custom weights differ")
    for key, value in values.items():
        _number(value, key)
    try:
        total = math.fsum(values.values())
    except OverflowError as exc:
        raise ValueError("Weight total must be positive and finite") from exc
    if not math.isfinite(total) or total <= 0:
        raise ValueError("Weight total must be positive and finite")
    return {key: value / total for key, value in values.items()}


def _quality_floor(value, steps=STEP_ORDER):
    if isinstance(value, dict):
        if set(value) != set(steps):
            raise ValueError("Quality floors must specify exactly the workflow steps")
        for item in value.values():
            _number(item, "quality floor", maximum=1)
    else:
        _number(value, "quality floor", maximum=1)


def _offsets(value):
    if not isinstance(value, list) or not 1 <= len(value) <= 128:
        raise ValueError("Start offsets require one to 128 values")
    for offset in value:
        _number(offset, "start offset", maximum=604800)
    if len(set(value)) != len(value):
        raise ValueError("Start offsets must be unique")


def _search_limit(value):
    if type(value) is not int or not 1 <= value <= 100_000:
        raise ValueError("Search limit must be an integer from one to 100000")


@_malformed_as_value_error
def decode_carbon(value):
    """Decode only explicit versioned carbon snapshots, without source access."""
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ValueError("Carbon must be an explicit versioned snapshot")
    fingerprint(value)
    kind = value.get("record_type")
    if kind == "HistoricalCarbonTrace":
        return HistoricalCarbonTrace.from_dict(value)
    if kind == "StaticCarbonEstimate":
        return StaticCarbonEstimate.from_dict(value)
    raise ValueError("Unsupported carbon snapshot")


@_malformed_as_value_error
def decode_knowledge_base(value):
    """Restore the exact retained KB without loading a filesystem path."""
    expected = {"schema_version", "kb_version", "description", "categories", "articles"}
    if not isinstance(value, dict) or set(value) != expected or type(value["schema_version"]) is not int or value["schema_version"] != 1:
        raise ValueError("Unsupported retained knowledge-base schema")
    if not isinstance(value["categories"], list) or not isinstance(value["articles"], list):
        raise ValueError("Knowledge-base categories and articles must be arrays")
    if len(value["articles"]) > 10000:
        raise ValueError("Retained knowledge base exceeds the supported bound")
    articles = []
    for item in value["articles"]:
        if not isinstance(item, dict) or set(item) != {"source_id", "category", "title", "body"}:
            raise ValueError("Unsupported knowledge article fields")
        articles.append(KnowledgeArticle(**item))
    fingerprint(value)
    return KnowledgeBase(value["kb_version"], tuple(value["categories"]), tuple(articles), value["description"])


def _configuration(value, *, request=None, profiles=None, kb=None):
    if value is None:
        return None
    if (not isinstance(value, dict) or value.get("experiment_mode") not in {"live", "simulation"}
            or value.get("worker_version") != WORKER_VERSION):
        raise ValueError("Durable planning configuration must identify the planned worker and execution mode")
    registry = DeploymentRegistry.from_dict(value.get("registry_snapshot"))
    if len(tuple(registry)) > 128:
        raise ValueError("Durable registry exceeds the supported destination bound")
    if value.get("registry_fingerprint") != registry.config_fingerprint():
        raise ValueError("Configuration registry fingerprint differs from its snapshot")
    if profiles is not None and value.get("calibration_id") != profiles["calibration_id"]:
        raise ValueError("Configuration calibration differs from its frozen profiles")
    if kb is not None and value.get("kb_fingerprint") != kb.fingerprint():
        raise ValueError("Configuration knowledge base differs from its snapshot")
    if request is not None and kb is not None and value.get("workload_fingerprint") != _workload_fingerprint(request, kb):
        raise ValueError("Configuration workload fingerprint differs from its request")
    return registry


@_malformed_as_value_error
def validate_context(value, request=None, configuration=None):
    """Validate the complete restart snapshot and return an independent copy."""
    if not isinstance(value, dict) or set(value) not in (_CONTEXT_FIELDS, _CONTEXT_FIELDS | {"allowed_pairs"}):
        raise ValueError("Unsupported DurablePlanningContext fields")
    if type(value["schema_version"]) is not int or value["schema_version"] != 1 or value["record_type"] != "DurablePlanningContext":
        raise ValueError("Unsupported DurablePlanningContext version")
    fingerprint(value)
    profiles = value["profiles"]
    evaluate_profiles(profiles, [])
    if len(profiles["profiles"]) > 1024:
        raise ValueError("Frozen profile set exceeds the supported bound")
    if "quality_policy" not in profiles:
        raise ValueError("Durable live planning requires a frozen quality coverage policy")
    kb = decode_knowledge_base(value["knowledge_base"])
    decode_carbon(value["carbon"])
    _quality_floor(value["quality_floor"])
    _offsets(value["start_offsets_seconds"])
    _search_limit(value["search_limit"])
    _number(value["minimum_improvement"], "minimum improvement", maximum=1)
    _number(value["health_max_age_seconds"], "health age", maximum=86400)
    _number(value["replan_interval_seconds"], "replan interval", maximum=86400)
    _number(value["probe_timeout_seconds"], "probe timeout", maximum=30, positive=True)
    _number(value["max_decision_age_seconds"], "decision age", maximum=86400, positive=True)
    if value["custom_weights"] is not None:
        _weights("custom", value["custom_weights"])
    if not isinstance(value["forecasts"], list) or len(value["forecasts"]) > len(STEP_ORDER):
        raise ValueError("Forecasts must be a bounded list")
    forecasts = [WorkloadSizeForecast.from_dict(item) for item in value["forecasts"]]
    if len({item.step_id for item in forecasts}) != len(forecasts):
        raise ValueError("Forecasts must have unique step IDs")
    for item in forecasts:
        if item.step_id not in STEP_ORDER or item.profile_fingerprint != profiles["fingerprint"]:
            raise ValueError("Forecast differs from its workflow/profile context")
    active_request = _request(request) if request is not None else None
    if active_request is not None:
        if active_request.workload_version != _workload_version(kb):
            raise ValueError("Request workload version differs from its retained KB/prompt version")
        _weights(active_request.policy, value["custom_weights"])
        workflow_id = _workload_fingerprint(active_request, kb)
        for item in forecasts:
            if item.workload_version != active_request.workload_version or item.workflow_fingerprint != workflow_id:
                raise ValueError("Forecast differs from its workflow/profile context")
    registry = _configuration(configuration, request=active_request, profiles=profiles, kb=kb)
    if "allowed_pairs" in value:
        if value["allowed_pairs"] is None:
            raise ValueError("An explicit pair restriction cannot be absent")
        if value["allowed_pairs"] != validate_allowed_pairs(value["allowed_pairs"], registry):
            raise ValueError("Pair restrictions must retain canonical lists")
    for profile in profiles["profiles"]:
        if active_request is not None and profile["workload_version"] != active_request.workload_version:
            raise ValueError("Profiles differ from the request workload version")
        if registry is not None:
            deployment = registry.require_model(profile["deployment_id"], profile["model_id"])
            if profile["deployment_fingerprint"] != deployment.config_fingerprint():
                raise ValueError("Profile deployment differs from the retained registry")
    return deepcopy(value)


def _envelope(value):
    if not isinstance(value, dict):
        raise ValueError("Planning report must be an object")
    failure = value.get("record_type") == "PlanningPreparationFailure"
    required = _FAILURE_FIELDS if failure else _REPORT_FIELDS
    optional = {"health_probe_seconds"} if failure else {"health_probe_seconds", "allowed_pairs"}
    if not required <= set(value) or set(value) - required - optional:
        raise ValueError("Unsupported planning report fields")
    if "health_probe_seconds" in value:
        _number(value["health_probe_seconds"], "health probe duration")
    if type(value["schema_version"]) is not int or value["schema_version"] != 1:
        raise ValueError("Unsupported planning report version")
    if value["record_type"] != ("PlanningPreparationFailure" if failure else "PreparedWorkflowPlan"):
        raise ValueError("Unsupported planning report type")
    if (value["service_version"] != SERVICE_VERSION or value["experiment_mode"] not in {"live", "simulation"}
            or value["inference_performed"] is not False):
        raise ValueError("Durable planning reports require the supported preparation service and execution mode")
    _text(value["trigger"], "planning trigger")
    _hash(value["fingerprint"], "report fingerprint")
    if value["fingerprint"] != fingerprint({key: item for key, item in value.items() if key != "fingerprint"}):
        raise ValueError("Planning report fingerprint differs from its content")
    return failure


def _materialized_contexts(value, request, kb, fixed, now, forecasts):
    contexts = value["contexts"]
    if not isinstance(contexts, list) or len(contexts) != len(request.steps):
        raise ValueError("Planning contexts must cover every workflow step")
    outputs = value["accepted_outputs"]
    if not isinstance(outputs, dict) or set(outputs) != {key for key, item in fixed.items() if item.status == "completed"}:
        raise ValueError("Accepted outputs differ from completed committed steps")
    rendered = {}
    for step, item in zip(request.steps, contexts):
        if not isinstance(item, dict):
            raise ValueError("Planning step context must be an object")
        fields = {"step_id", "committed", "input_mode", "input_fingerprint", "materialized_step", "source_ids", "forecast"}
        if "reason" in item:
            fields.add("reason")
        if set(item) != fields or item["step_id"] != step.step_id or type(item["committed"]) is not bool or item["committed"] != (step.step_id in fixed):
            raise ValueError("Planning step context identity differs from the request")
        ready = all(key in outputs for key in step.dependencies)
        if step.step_id in fixed:
            if not ready or any(key not in fixed or fixed[key].status != "completed" for key in step.dependencies):
                raise ValueError("Committed work requires completed prerequisites")
            committed = fixed[step.step_id]
            if committed.actual_start < request.arrived_at or committed.actual_start > now:
                raise ValueError("Committed dispatch time lies outside observed time")
            if committed.status == "completed" and committed.finish > now:
                raise ValueError("Completed work cannot finish after the planning time")
            if any(committed.actual_start < fixed[key].finish for key in step.dependencies):
                raise ValueError("Committed work started before its prerequisites finished")
        if ready:
            actual = render_step(request, step.step_id, outputs, kb=kb)
            rendered[step.step_id] = actual
            expected = dict(step_id=step.step_id, committed=step.step_id in fixed, input_mode="materialized",
                            input_fingerprint=actual.input_fingerprint, materialized_step=actual.step.to_dict(),
                            source_ids=list(actual.source_ids), forecast=None)
            if item != expected:
                raise ValueError("Materialized context differs from the retained actual inputs")
            if step.step_id in outputs:
                validate_step_output(step.step_id, outputs[step.step_id], source_ids=actual.source_ids)
        else:
            expected_forecast = forecasts.get(step.step_id)
            mode = "provisional" if expected_forecast is not None else "unavailable"
            expected = dict(step_id=step.step_id, committed=False, input_mode=mode, input_fingerprint=None,
                            materialized_step=None, source_ids=[], forecast=expected_forecast.to_dict() if expected_forecast else None)
            if expected_forecast is None:
                expected["reason"] = "missing-workload-size-forecast"
            if item != expected:
                raise ValueError("Provisional context differs from its bound forecast")
    return rendered


def _diagnostics(value, request, registry, profiles, provider, forecasts, rendered, fixed, reservations, now):
    reports = value["candidate_diagnostics"]
    if not isinstance(reports, list) or len(reports) > len(request.steps):
        raise ValueError("Candidate diagnostics must be a bounded list")
    by_step = {}
    for report in reports:
        if not isinstance(report, dict) or report.get("step_id") not in STEP_ORDER or report["step_id"] in by_step:
            raise ValueError("Candidate diagnostics must have unique workflow step IDs")
        by_step[report["step_id"]] = report
    pools, finishes, exclusions = {}, {}, {}
    base_grid = {now + timedelta(seconds=offset) for offset in value["start_offsets_seconds"]}
    for step in request.steps:
        key = step.step_id
        if key in fixed:
            if key in by_step:
                raise ValueError("Committed work cannot have fresh candidate diagnostics")
            pools[key], finishes[key] = (), {fixed[key].finish}
            continue
        actual = rendered.get(key)
        if actual is None and key not in forecasts:
            if key in by_step:
                raise ValueError("Unavailable input cannot have generated candidates")
            pools[key], finishes[key] = (), set()
            continue
        if key not in by_step:
            raise ValueError("Planning is missing required candidate diagnostics")
        report = by_step[key]
        diagnostic_values = report.get("diagnostics")
        if not isinstance(diagnostic_values, list) or not diagnostic_values or len(diagnostic_values) > 16384:
            raise ValueError("Candidate diagnostics exceed their supported bound")
        starts = sorted({_stamp(item.get("planned_start"), "candidate start") for item in diagnostic_values if isinstance(item, dict)})
        if len(starts) > 256 or not starts:
            raise ValueError("Candidate time grid exceeds its supported bound")
        base = max(now, step.earliest_start)
        required = set(base_grid) if step.allow_deferral else {base}
        required.update(item.finish for item in reservations if item.finish >= base)
        if step.dependencies and all(finishes.get(dep) for dep in step.dependencies):
            minimum_ready = max(min(finishes[dep]) for dep in step.dependencies)
            required.update(max(base, instant) for dep in step.dependencies for instant in finishes[dep] if instant >= minimum_ready)
        required = {instant for instant in required if instant >= base}
        if not required <= set(starts) or (value["previous_plan_fingerprint"] is None and set(starts) != required):
            raise ValueError("Candidate start grid omits or changes its declared preparation windows")
        if actual is not None:
            generated = generate_profile_candidates(actual.step, request.workload_version, registry, profiles, starts,
                as_of=now, now=now, carbon_provider=provider)
        else:
            generated = generate_forecast_candidates(step, request.workload_version, registry, profiles, starts,
                forecast=forecasts[key], workflow_fingerprint=value["workload_fingerprint"],
                as_of=now, now=now, carbon_provider=provider)
        if report != generated.to_dict():
            raise ValueError("Retained candidate diagnostics differ from calibrated preparation")
        offered = []
        for candidate in generated.candidates:
            reason = candidate_exclusion(candidate, value.get("allowed_pairs"), live=value["experiment_mode"] == "live")
            if reason is None:
                offered.append(candidate)
            else:
                exclusions[candidate.candidate_id] = reason
        pools[key] = offered
        finishes[key] = {candidate.planned_start + timedelta(seconds=candidate.estimates.latency.value)
                         for candidate in offered if candidate.estimates.latency.value is not None}
    if value["mode_exclusions"] != exclusions:
        raise ValueError("Candidate exclusions differ from generated candidates")
    return pools


def _logical_result(result):
    content = result.to_dict()
    content.pop("planner_seconds")
    return content


def _validate_result(result, request, pools, fixed, registry, health, reservations, now, value):
    expected = plan_workflow(request, pools, value["weights"], now=now, fixed_assignments=tuple(fixed.values()),
        registry=registry, health=health, reservations=reservations, quality_floor=value["quality_floor"],
        health_max_age_seconds=value["health_max_age_seconds"], search_limit=value["search_limit"])
    actual, logical = _logical_result(result), _logical_result(expected)
    if actual == logical:
        return
    # prepare_plan retains only the incumbent fingerprint. Its complete original
    # report is linked by queue history. Independently prove any retained winner
    # is an examined feasible assignment inside the stated improvement threshold.
    if (value["previous_plan_fingerprint"] is None or result.winner is None or expected.winner is None
            or result.globally_optimal_within_candidates
            or result.winner.score - expected.winner.score > value["minimum_improvement"] + 1e-12):
        raise ValueError("Retained plan result differs from bounded deterministic planning")
    retained_note = "Retained feasible incumbent: improvement did not exceed the replan threshold."
    if result.notes != expected.notes + (retained_note,):
        raise ValueError("Retained incumbent requires its explicit explanation")
    for field in ("winner", "globally_optimal_within_candidates", "notes"):
        actual.pop(field)
        logical.pop(field)
    if actual != logical:
        raise ValueError("Retained incumbent changed planning counts or reference")
    # Re-evaluate exactly the saved assignment against the complete reference.
    selected = {item.candidate.step_id: [item.candidate] for item in result.winner.assignments if not item.committed}
    check = plan_workflow(request, selected, value["weights"], now=now, fixed_assignments=tuple(fixed.values()),
        registry=registry, health=health, reservations=reservations, quality_floor=value["quality_floor"],
        health_max_age_seconds=value["health_max_age_seconds"], search_limit=1)
    if check.winner is None:
        raise ValueError("Retained incumbent is no longer feasible")
    for left, right in zip(check.winner.assignments, result.winner.assignments):
        if left != right:
            raise ValueError("Retained incumbent timings differ from feasible execution")
    if check.winner.metrics != result.winner.metrics or check.winner.response_at != result.winner.response_at:
        raise ValueError("Retained incumbent outcomes differ from its execution")
    offered = {candidate.candidate_id: candidate for options in pools.values() for candidate in options}
    incumbent_ids = {item.candidate.step_id: item.candidate.candidate_id for item in result.winner.assignments}
    index = 0
    for step in request.steps:
        options = [fixed[step.step_id].candidate] if step.step_id in fixed else [
            item for item in pools[step.step_id] if item.candidate_id not in result.rejected_candidates]
        ids = sorted(item.candidate_id for item in options)
        if incumbent_ids[step.step_id] not in ids:
            raise ValueError("Retained incumbent candidate was not offered")
        if step.step_id not in fixed:
            item = next(x.candidate for x in result.winner.assignments if x.candidate.step_id == step.step_id)
            if offered.get(item.candidate_id) != item:
                raise ValueError("Retained incumbent candidate identity differs from its pool")
        index = index * len(ids) + ids.index(incumbent_ids[step.step_id])
    if index >= result.examined_count:
        raise ValueError("Retained incumbent was outside the examined search prefix")


@_malformed_as_value_error
def validate_report(value, request=None, configuration=None, context=None):
    """Validate a durable decision or retained preparation failure, offline."""
    failure = _envelope(value)
    recorded_request = _request(value["request"])
    if request is not None and recorded_request != _request(request):
        raise ValueError("Planning report differs from its workflow request")
    now = _stamp(value["now"], "planning time")
    if now < recorded_request.arrived_at:
        raise ValueError("Planning cannot precede workflow arrival")
    active_context = validate_context(context, recorded_request, configuration) if context is not None else None
    if configuration is not None and configuration.get("experiment_mode") != value["experiment_mode"]:
        raise ValueError("Planning report execution mode differs from its saved configuration")
    if failure:
        if not isinstance(value["error_code"], str) or not _IDENTIFIER.fullmatch(value["error_code"]):
            raise ValueError("Preparation failure requires a safe error code")
        _configuration(configuration, request=recorded_request)
        return deepcopy(value)
    registry = DeploymentRegistry.from_dict(value["registry_snapshot"])
    allowed_pairs = validate_allowed_pairs(value.get("allowed_pairs"), registry)
    if "allowed_pairs" in value and allowed_pairs is None:
        raise ValueError("An explicit pair restriction cannot be absent")
    if active_context is not None and value.get("allowed_pairs") != active_context.get("allowed_pairs"):
        raise ValueError("Report pair restrictions differ from its durable context")
    if value["registry_fingerprint"] != registry.config_fingerprint():
        raise ValueError("Report registry fingerprint differs from its snapshot")
    if configuration is not None and (configuration.get("registry_fingerprint") != value["registry_fingerprint"]
            or configuration.get("calibration_id") != value["calibration_id"]
            or configuration.get("kb_fingerprint") != value["kb_fingerprint"]
            or configuration.get("workload_fingerprint") != value["workload_fingerprint"]):
        raise ValueError("Report differs from the saved workflow configuration")
    for name in ("profile_fingerprint", "kb_fingerprint", "workload_fingerprint"):
        _hash(value[name], name)
    _text(value["calibration_id"], "calibration id")
    validate_quality_policy(value["quality_policy"])
    _quality_floor(value["quality_floor"])
    _offsets(value["start_offsets_seconds"])
    _search_limit(value["search_limit"])
    _number(value["minimum_improvement"], "minimum improvement", maximum=1)
    _number(value["health_max_age_seconds"], "health age", maximum=86400)
    _number(value["preparation_seconds"], "preparation duration")
    if value["previous_plan_fingerprint"] is not None:
        _hash(value["previous_plan_fingerprint"], "previous plan fingerprint")
    if not isinstance(value["notes"], list) or not value["notes"]:
        raise ValueError("Report requires explanatory notes")
    for note in value["notes"]:
        _text(note, "note")
    expected_weights = _weights(recorded_request.policy, active_context["custom_weights"] if active_context else
                                value["weights"] if recorded_request.policy == "custom" else None)
    if value["weights"] != expected_weights:
        raise ValueError("Reported weights differ from the requested policy")
    if active_context is not None:
        profiles = active_context["profiles"]
        kb = decode_knowledge_base(active_context["knowledge_base"])
        provider = decode_carbon(active_context["carbon"])
        for key in ("quality_floor", "start_offsets_seconds", "search_limit", "health_max_age_seconds", "minimum_improvement"):
            if value[key] != active_context[key]:
                raise ValueError(f"Report {key} differs from its durable context")
        if (value["profile_fingerprint"] != profiles["fingerprint"] or value["calibration_id"] != profiles["calibration_id"]
                or value["quality_policy"] != profiles["quality_policy"]):
            raise ValueError("Report differs from its frozen profiles and quality policy")
        forecasts = {item["step_id"]: WorkloadSizeForecast.from_dict(item) for item in active_context["forecasts"]}
    else:
        # Standalone checks can prove request and prompt integrity for the
        # shipped KB; persisted durable reports always carry their context.
        kb = KnowledgeBase.load()
        profiles, provider = None, None
        forecasts = {}
        for item in value["contexts"] if isinstance(value["contexts"], list) else ():
            if isinstance(item, dict) and item.get("forecast") is not None:
                forecast = WorkloadSizeForecast.from_dict(item["forecast"])
                forecasts[forecast.step_id] = forecast
    if value["kb_fingerprint"] != kb.fingerprint() or recorded_request.workload_version != _workload_version(kb):
        raise ValueError("Report differs from its retained knowledge base and prompt version")
    if value["workload_fingerprint"] != _workload_fingerprint(recorded_request, kb):
        raise ValueError("Report workload fingerprint differs from its actual request")
    _configuration(configuration, request=recorded_request, profiles=profiles, kb=kb)
    if not isinstance(value["health_snapshot"], list) or len(value["health_snapshot"]) > 128:
        raise ValueError("Health snapshot must be a bounded list")
    observations = [DeploymentHealth.from_dict(item) for item in value["health_snapshot"]]
    health = {item.deployment_id: item for item in observations}
    if len(health) != len(observations):
        raise ValueError("Duplicate health observations")
    for item in observations:
        deployment = registry.get(item.deployment_id)
        if set(item.available_models) - set(deployment.models):
            raise ValueError("Health observation reports unconfigured models")
    if not isinstance(value["reservations"], list) or len(value["reservations"]) > 10000:
        raise ValueError("Reservations must be a bounded list")
    reservations = [Reservation.from_dict(item) for item in value["reservations"]]
    for item in reservations:
        if item.slots > registry.get(item.deployment_id).max_concurrency:
            raise ValueError("Reservation exceeds destination capacity")
    if not isinstance(value["committed_assignments"], list) or len(value["committed_assignments"]) > len(STEP_ORDER):
        raise ValueError("Committed assignments must be a bounded list")
    committed = [CommittedAssignment.from_dict(item) for item in value["committed_assignments"]]
    fixed = {item.candidate.step_id: item for item in committed}
    if len(fixed) != len(committed) or set(fixed) - set(STEP_ORDER):
        raise ValueError("Committed assignments must have unique workflow steps")
    for item in committed:
        deployment = registry.require_model(item.candidate.deployment_id, item.candidate.model_id)
        if (item.candidate.region != deployment.region
                or value["experiment_mode"] == "live" and any(getattr(item.candidate.estimates, key).provenance == "simulated" for key in _METRICS)
                or not pair_permitted(allowed_pairs, item.candidate.step_id, item.candidate.deployment_id, item.candidate.model_id)):
            raise ValueError("Committed work differs from deployment eligibility or permitted pairs")
    rendered = _materialized_contexts(value, recorded_request, kb, fixed, now, forecasts)
    result = PlanResult.from_dict(value["result"])
    if result.to_dict() != value["result"]:
        raise ValueError("Planner result must retain its complete typed representation")
    if result.workflow_id != recorded_request.workflow_id or dict(result.weights) != value["weights"] or result.search_limit != value["search_limit"]:
        raise ValueError("Planner result differs from its request/search policy")
    if profiles is not None:
        pools = _diagnostics(value, recorded_request, registry, profiles, provider, forecasts, rendered, fixed, reservations, now)
        _validate_result(result, recorded_request, pools, fixed, registry, health, reservations, now, value)
    else:
        pools = {step.step_id: [] for step in recorded_request.steps}
        exclusions = {}
        for diagnostic in value["candidate_diagnostics"]:
            for item in diagnostic.get("candidates", []):
                candidate = PlannedCandidate.from_dict(item)
                reason = candidate_exclusion(candidate, allowed_pairs, live=value["experiment_mode"] == "live")
                if reason is None:
                    pools[candidate.step_id].append(candidate)
                else:
                    exclusions[candidate.candidate_id] = reason
        if value["mode_exclusions"] != exclusions:
            raise ValueError("Candidate exclusions differ from retained candidates")
        _validate_result(result, recorded_request, pools, fixed, registry, health, reservations, now, value)
    dispatchable = []
    if result.winner is not None:
        for item in result.winner.assignments:
            if (value["experiment_mode"] == "live" and item.candidate.step_id not in fixed
                    and any(getattr(item.candidate.estimates, metric).provenance == "simulated" for metric in _METRICS)):
                raise ValueError("A live winner cannot contain simulated metrics")
            if not item.committed and item.candidate.step_id in rendered and item.candidate.planned_start <= now:
                dispatchable.append(item.candidate.step_id)
    if value["dispatchable_step_ids"] != dispatchable:
        raise ValueError("Dispatchable steps must be due and actually materialized")
    return deepcopy(value)
