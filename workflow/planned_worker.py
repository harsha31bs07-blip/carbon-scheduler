"""Install calibrated joint decisions and execute only materialized due steps.

Planning, health probes and validation happen outside queue write transactions.
Installation uses the queue's state/capacity token; provider calls use the same
durable attempt, lease and reconciliation boundary as fixed work.
"""

from collections.abc import Mapping
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import time

from data.carbon_sources import HistoricalCarbonTrace, StaticCarbonEstimate
from data.quality_reviews import fingerprint
from scheduler.joint import CommittedAssignment, PlanResult
from scheduler.profile_candidates import _carbon
from shared.deployments import DeploymentHealth, DeploymentRegistry
from shared.scheduling import SystemClock, WorkloadSizeForecast
from workflow.connected import _now, workload_fingerprint
from workflow.deployment_inventory import probe_deployment
from workflow.planning import PLANNING_SERVICE_VERSION, prepare_plan
from workflow.planning_constraints import validate_allowed_pairs
from workflow.planning_records import decode_carbon, decode_knowledge_base, validate_context
from workflow.queue_store import LeaseError, PlanningConflict
from workflow.workload import KnowledgeBase, build_workflow_request


WORKER_VERSION = "sqlite-planned-worker-v1"
_TERMINAL = {"completed", "cancelled", "failed", "deadline_missed", "needs-reconciliation"}


def knowledge_snapshot(kb):
    if not isinstance(kb, KnowledgeBase):
        raise ValueError("A retained knowledge base is required")
    return {"schema_version": 1, "kb_version": kb.version, "description": kb.description,
            "categories": list(kb.categories), "articles": [article.to_dict() for article in kb.articles]}


def planning_context(profiles, kb, *, forecasts=(), carbon_provider=None, custom_weights=None,
                     quality_floor=.8, start_offsets_seconds=(0,), search_limit=100_000,
                     health_max_age_seconds=300., minimum_improvement=.01,
                     replan_interval_seconds=10., probe_timeout_seconds=3., max_decision_age_seconds=5.,
                     allowed_pairs=None):
    """Snapshot the complete restart input; never save paths or credentials."""
    if isinstance(forecasts, Mapping):
        if any(key != value.step_id for key, value in forecasts.items() if isinstance(value, WorkloadSizeForecast)):
            raise ValueError("Forecast keys must match their steps")
        forecasts = list(forecasts.values())
    if not isinstance(forecasts, (list, tuple)) or any(not isinstance(item, WorkloadSizeForecast) for item in forecasts):
        raise ValueError("Forecasts must be typed records")
    if carbon_provider is not None and not isinstance(carbon_provider, (HistoricalCarbonTrace, StaticCarbonEstimate)):
        raise ValueError("Durable carbon inputs must be explicit versioned snapshots")
    context = {"schema_version": 1, "record_type": "DurablePlanningContext", "profiles": profiles,
        "knowledge_base": knowledge_snapshot(kb), "forecasts": [item.to_dict() for item in forecasts],
        "carbon": carbon_provider.to_dict() if carbon_provider else None, "custom_weights": custom_weights,
        "quality_floor": quality_floor, "start_offsets_seconds": list(start_offsets_seconds),
        "search_limit": search_limit, "health_max_age_seconds": health_max_age_seconds,
        "minimum_improvement": minimum_improvement, "replan_interval_seconds": replan_interval_seconds,
        "probe_timeout_seconds": probe_timeout_seconds, "max_decision_age_seconds": max_decision_age_seconds}
    if allowed_pairs is not None:
        context["allowed_pairs"] = validate_allowed_pairs(allowed_pairs)
    return validate_context(context)


def _failure(request, now, code, *, trigger, experiment_mode="live"):
    report = {"schema_version": 1, "record_type": "PlanningPreparationFailure",
        "service_version": PLANNING_SERVICE_VERSION, "experiment_mode": experiment_mode, "inference_performed": False,
        "request": request.to_dict(), "now": now.isoformat(), "trigger": trigger, "error_code": code}
    report["fingerprint"] = fingerprint(report)
    return report


def _with_probe_time(report, seconds):
    report = {**report, "health_probe_seconds": seconds}
    report["fingerprint"] = fingerprint({key: value for key, value in report.items() if key != "fingerprint"})
    return report


def _health(registry, context, clock, health_probe):
    probe = health_probe or probe_deployment
    observations = {}
    for deployment in registry:
        try:
            result = probe(deployment, timeout_seconds=context["probe_timeout_seconds"])
            if not isinstance(result, DeploymentHealth) or result.deployment_id != deployment.deployment_id:
                raise ValueError("Health observation differs from its endpoint")
        except (ValueError, OSError):
            result = DeploymentHealth(deployment.deployment_id, _now(clock), None, (),
                                      "probe-failed", "verification-failed")
        observations[deployment.deployment_id] = result
    return observations


def _prepare(request, registry, context, kb, clock, health, *, outputs=None,
             fixed_assignments=(), reservations=(), previous_plan=None, trigger, experiment_mode="live"):
    return prepare_plan(request, registry, context["profiles"], kb=kb, now=_now(clock), health=health,
        carbon_provider=decode_carbon(context["carbon"]), outputs=outputs,
        fixed_assignments=fixed_assignments, reservations=reservations,
        forecasts={item.step_id: item for item in map(WorkloadSizeForecast.from_dict, context["forecasts"])},
        custom_weights=context["custom_weights"], quality_floor=context["quality_floor"],
        start_offsets_seconds=context["start_offsets_seconds"], search_limit=context["search_limit"],
        health_max_age_seconds=context["health_max_age_seconds"], minimum_improvement=context["minimum_improvement"],
        previous_plan=previous_plan, trigger=trigger, experiment_mode=experiment_mode, allowed_pairs=context.get("allowed_pairs"))


def submit_planned(store, registry, kb, ticket, profiles, *, forecasts=(), carbon_provider=None,
                   policy="balanced", custom_weights=None, clock=None, workflow_id=None,
                   response_seconds=300, completion_seconds=600, summary_delay_seconds=0,
                   timeout_seconds=120., enable_thinking=False, health_probe=None,
                   tariff_book=None, energy_mode="none", energy_country=None,
                   allow_summary_deferral=True, experiment_mode="live", submission=None, **controls):
    """Persist a feasible plan or a durable rejection, without inference."""
    from uuid import uuid4
    from workflow.worker import _seconds

    if not isinstance(registry, DeploymentRegistry):
        raise ValueError("An explicit deployment registry is required")
    if type(allow_summary_deferral) is not bool:
        raise ValueError("allow_summary_deferral must be boolean")
    if not isinstance(experiment_mode, str) or experiment_mode not in {"live", "simulation"}:
        raise ValueError("Unsupported planned execution mode")
    if experiment_mode == "simulation" and health_probe is None:
        raise ValueError("Simulation submission requires an explicit health probe")
    _seconds(timeout_seconds, "timeout_seconds", positive=True)
    _seconds(summary_delay_seconds, "summary_delay_seconds")
    if timeout_seconds > 300:
        raise ValueError("Timeout must be at most 300 seconds")
    clock = clock or SystemClock()
    arrived = _now(clock)
    request = build_workflow_request(ticket, workflow_id=workflow_id or uuid4().hex, arrived_at=arrived,
        response_budget_seconds=response_seconds, completion_budget_seconds=completion_seconds,
        kb=kb, policy=policy, enable_thinking=enable_thinking)
    if not allow_summary_deferral:
        request = replace(request, steps=tuple(replace(step, allow_deferral=False)
            if step.step_id == "summarize" else step for step in request.steps))
    if summary_delay_seconds >= completion_seconds:
        raise ValueError("Summary delay must precede the final deadline")
    if summary_delay_seconds:
        request = replace(request, steps=tuple(replace(step, earliest_start=arrived + timedelta(seconds=summary_delay_seconds))
            if step.step_id == "summarize" else step for step in request.steps))
    context = planning_context(profiles, kb, forecasts=forecasts, carbon_provider=carbon_provider,
                                custom_weights=custom_weights, **controls)
    config = {"registry_snapshot": registry.to_dict(), "registry_fingerprint": registry.config_fingerprint(),
        "kb_fingerprint": kb.fingerprint(), "workload_fingerprint": workload_fingerprint(request, kb),
        "worker_version": WORKER_VERSION, "policy_version": "joint-calibrated-v1",
        "planner_version": "joint-enumeration-v1", "calibration_id": profiles["calibration_id"],
        "experiment_mode": experiment_mode, "timeout_seconds": timeout_seconds, "max_retries": 3}
    config["accounting_version"] = "execution-accounting.v1"
    context = validate_context(context, request=request, configuration=config)
    from workflow.execution_accounting import make_accounting_context
    accounting_context = make_accounting_context(registry, request, tariff_book=tariff_book,
        carbon_provider=carbon_provider, energy_mode=energy_mode, energy_country=energy_country)
    probe_started = time.perf_counter()
    health = _health(registry, context, clock, health_probe)
    probe_seconds = time.perf_counter() - probe_started
    try:
        prepared = _prepare(request, registry, context, kb, clock, health, trigger="submission", experiment_mode=experiment_mode)
        report = prepared.report
        assignments = tuple(item.candidate for item in prepared.result.winner.assignments) if prepared.result.winner else None
    except (ValueError, KeyError, OSError):
        report, assignments = _failure(request, _now(clock), "planning-input-unavailable", trigger="submission",
                                      experiment_mode=experiment_mode), None
    report = _with_probe_time(report, probe_seconds)
    return store.enqueue_planned(request, assignments, configuration=config, planning_context=context,
                                 decision=report, now=_now(clock), accounting_context=accounting_context, submission=submission)


def _committed(state):
    facts = []
    for job in state.jobs:
        if job.status != "completed":
            continue
        accepted = [attempt for attempt in state.attempts if attempt.retry_index == job.retry_index
                    and attempt.planned_candidate == job.candidate and attempt.status == "completed"
                    and attempt.output == job.output]
        if not accepted or accepted[-1].actual_start is None or job.finished_at is None:
            raise ValueError("Completed job lacks accepted execution evidence")
        facts.append(CommittedAssignment(job.candidate, accepted[-1].actual_start, job.finished_at))
    return tuple(facts)


def _previous_winner(state):
    # A failure or infeasible result suspends the old plan. Do not resurrect an
    # earlier winner as if it were the current feasible incumbent.
    report = state.decisions[-1]["report"]
    return PlanResult.from_dict(report["result"]).winner if report["record_type"] == "PreparedWorkflowPlan" else None


def _should_prepare(state, now):
    if state.status in _TERMINAL or now >= state.request.completion_deadline:
        return False
    # Reconciliation and active calls hold their destination slot. This worker
    # waits for their result instead of inventing a release/finish time.
    if any(job.status in {"running", "needs-reconciliation"} for job in state.jobs):
        return False
    latest = state.decisions[-1]
    recorded = datetime.fromisoformat(latest["recorded_at"]).astimezone(timezone.utc)
    report = latest["report"]
    if report["record_type"] != "PreparedWorkflowPlan" or report["result"]["winner"] is None:
        return (now - recorded).total_seconds() >= state.planning_context["replan_interval_seconds"]
    if report["accepted_outputs"] != state.completed_outputs():
        return True
    if any(job.status == "scheduled" and job.candidate.planned_start <= now
           and set(job.step_request.dependencies) <= state.completed_outputs().keys() for job in state.jobs):
        return True
    return (now - recorded).total_seconds() >= state.planning_context["replan_interval_seconds"]


def _final_check(state, report, context, registry, carbon_provider):
    def check(claim, rendered, now):
        decision_at = datetime.fromisoformat(report["now"]).astimezone(timezone.utc)
        if not 0 <= (now - decision_at).total_seconds() <= context["max_decision_age_seconds"]:
            return "planning-decision-expired"
        item = next(value for value in report["contexts"] if value["step_id"] == claim.candidate.step_id)
        if item["input_fingerprint"] != rendered.input_fingerprint or item["materialized_step"] != rendered.step.to_dict():
            return "planned-prompt-changed"
        health = {value["deployment_id"]: DeploymentHealth.from_dict(value) for value in report["health_snapshot"]}
        selected = health.get(claim.candidate.deployment_id)
        if (selected is None or selected.reachable is not True or claim.candidate.model_id not in selected.available_models
                or not 0 <= (now - selected.checked_at).total_seconds() <= context["health_max_age_seconds"]):
            return "planning-health-expired"
        try:
            winner = PlanResult.from_dict(report["result"]).winner
            slip = max(0., (now - claim.candidate.planned_start).total_seconds())
            for assignment in winner.assignments:
                if assignment.committed:
                    continue
                step = next(value for value in state.request.steps if value.step_id == assignment.candidate.step_id)
                if assignment.finish + timedelta(seconds=slip) > step.latest_finish:
                    return "planning-deadline-budget-exhausted"
            current, status, _sample = _carbon(claim.candidate.estimates.energy, claim.candidate.estimates.latency,
                registry.get(claim.candidate.deployment_id), now, as_of=now, provider=carbon_provider)
            planned = claim.candidate.estimates.carbon
            if status != "available" or current != planned:
                return "planning-carbon-changed"
        except (ValueError, OSError, OverflowError):
            return "planning-preflight-unavailable"
        return None
    return check


def _retain_preflight_failure(store, identity, clock, code):
    try:
        current = store.planning_snapshot(identity, now=_now(clock))
        report = _failure(current.workflow.request, _now(clock), code, trigger="dispatch-preflight",
                          experiment_mode=current.workflow.configuration["experiment_mode"])
        report = _with_probe_time(report, 0.)
        store.install_plan(identity, None, report, expected_token=current.token, now=_now(clock))
    except (PlanningConflict, LeaseError):
        # Cancellation/another worker supersedes this pre-dispatch diagnostic;
        # its durable state wins. No external request has been made.
        pass


def work_planned_once(store, *, worker_id, registry=None, kb=None, clock=None, dispatcher=None,
                       lease_seconds=60, heartbeat=True, health_probe=None, tracker_factory=None):
    """Prepare outside locks, install/claim atomically, then dispatch once."""
    from workflow.worker import _execute_claim

    clock = clock or SystemClock()
    last_event = {"status": "idle"}
    for identity in store.planned_workflow_ids():
        snapshot = store.planning_snapshot(identity, now=_now(clock))
        state, context = snapshot.workflow, snapshot.workflow.planning_context
        mode = state.configuration["experiment_mode"]
        if not _should_prepare(state, _now(clock)):
            continue
        if mode == "simulation" and (dispatcher is None or health_probe is None):
            raise ValueError("Simulation execution requires explicit provider and health fixtures")
        probe_seconds = 0.
        try:
            active_registry = registry or DeploymentRegistry.from_dict(state.configuration["registry_snapshot"])
            active_kb = kb or decode_knowledge_base(context["knowledge_base"])
            if (active_registry.config_fingerprint() != state.configuration["registry_fingerprint"]
                    or active_kb.fingerprint() != state.configuration["kb_fingerprint"]):
                raise ValueError("Saved execution configuration changed")
            probe_started = time.perf_counter()
            health = _health(active_registry, context, clock, health_probe)
            probe_seconds = time.perf_counter() - probe_started
            prepared = _prepare(state.request, active_registry, context, active_kb, clock, health,
                outputs=state.completed_outputs(), fixed_assignments=_committed(state),
                reservations=snapshot.reservations, previous_plan=_previous_winner(state), trigger="dispatch-revalidation",
                experiment_mode=mode)
            report = prepared.report
            assignments = tuple(item.candidate for item in prepared.result.winner.assignments) if prepared.result.winner else None
        except (ValueError, KeyError, OSError):
            report, assignments = _failure(state.request, _now(clock), "planning-input-unavailable", trigger="dispatch-revalidation",
                                          experiment_mode=mode), None
        report = _with_probe_time(report, probe_seconds)
        try:
            claim = store.install_plan(identity, assignments, report, expected_token=snapshot.token,
                                       now=_now(clock), worker_id=worker_id, lease_seconds=lease_seconds)
        except PlanningConflict:
            last_event = {"status": "planning-conflict", "workflow_id": identity, "job_processed": False}
            continue
        if claim is None:
            last_event = {"status": "planned-wait" if assignments else "planning-infeasible",
                          "workflow_id": identity, "job_processed": False}
            continue
        result = _execute_claim(store, claim, registry=active_registry, kb=active_kb, clock=clock,
            dispatcher=dispatcher, lease_seconds=lease_seconds, heartbeat=heartbeat, worker_version=WORKER_VERSION,
            pre_dispatch=_final_check(state, report, context, active_registry, decode_carbon(context["carbon"])),
            tracker_factory=tracker_factory)
        if result["status"] == "replan-required":
            _retain_preflight_failure(store, identity, clock, result["reason"])
        result.setdefault("job_processed", result["status"] not in {"replan-required", "claim-lost"})
        return result
    return last_event
