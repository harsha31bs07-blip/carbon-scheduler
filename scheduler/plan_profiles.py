"""Plan one frozen support-ticket scenario from retained calibration profiles.

This command performs no inference or network calls. Downstream prompts use the
calibration fixture's frozen context, never claimed outputs of an executed DAG.
Eligibility must be supplied as saved inventory evidence or an explicit
simulation assumption; unknown quality produces structured infeasibility.
"""

from __future__ import annotations

import argparse
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import hashlib
import json
import math
from pathlib import Path

from data.carbon_sources import HistoricalCarbonTrace, StaticCarbonEstimate
from scheduler.joint import PLANNER_VERSION, plan_workflow
from shared.deployments import DeploymentHealth, DeploymentRegistry
from shared.run_records import write_json
from workflow.calibrate import frozen_context, load_cases, load_frozen_profiles
from workflow.connected import workload_fingerprint
from workflow.workload import DEFAULT_TICKETS_PATH, KnowledgeBase, build_workflow_request, render_step


SCENARIO_MODE = "frozen-context-planning-scenario"
_WEIGHTS = {
    "speed": dict(latency=.6, accuracy=.1, cost=.1, energy=.1, carbon=.1),
    "balanced": dict(latency=.2, accuracy=.2, cost=.2, energy=.2, carbon=.2),
    "eco": dict(latency=.1, accuracy=.1, cost=.1, energy=.3, carbon=.4),
}


def _fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                     ensure_ascii=False, allow_nan=False).encode()).hexdigest()


def _unique_object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("Input JSON contains duplicate fields")
        value[key] = item
    return value


def _json(path):
    def invalid(value):
        raise ValueError("Input JSON contains a nonfinite number")
    return json.loads(Path(path).read_text(encoding="utf-8-sig"),
                      object_pairs_hook=_unique_object, parse_constant=invalid)


def _reference(value):
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("Reference time must be an explicit timezone-aware timestamp")
    return value.astimezone(timezone.utc)


def load_inventory_health(path, registry):
    """Verify saved inventory configuration without probing its endpoints."""
    payload = _json(path)
    if (not isinstance(payload, dict) or set(payload) != {"schema_version", "configuration_fingerprint", "deployments"}
            or type(payload["schema_version"]) is not int or payload["schema_version"] != 1
            or payload["configuration_fingerprint"] != registry.config_fingerprint()
            or not isinstance(payload["deployments"], list)):
        raise ValueError("Saved inventory does not match the deployment registry")
    health = {}
    for item in payload["deployments"]:
        if not isinstance(item, dict) or set(item) != {"configuration", "health"}:
            raise ValueError("Saved inventory has an unsupported entry")
        deployment = DeploymentRegistry.from_dict({"schema_version": 1, "deployments": [item["configuration"]]})
        configured = next(iter(deployment))
        observation = DeploymentHealth.from_dict(item["health"])
        if (configured != registry.get(configured.deployment_id) or observation.deployment_id != configured.deployment_id
                or observation.deployment_id in health):
            raise ValueError("Saved inventory contains mismatched or duplicate destinations")
        health[observation.deployment_id] = observation
    if set(health) != {deployment.deployment_id for deployment in registry}:
        raise ValueError("Saved inventory is incomplete")
    return health


def plan_case(
    profiles, registry, case, *, reference_time, carbon_provider,
    policy="balanced", start_offsets_seconds=(0,), scenario_eligibility=False,
    health=None, health_max_age_seconds=300, quality_floor=0.0, search_limit=100_000,
    kb=None, response_seconds=300, completion_seconds=600,
):
    """Materialize frozen production prompts, generate options and plan offline."""
    from scheduler.profile_candidates import generate_profile_candidates

    reference = _reference(reference_time)
    if policy not in _WEIGHTS:
        raise ValueError("Unsupported scenario policy")
    if type(scenario_eligibility) is not bool or scenario_eligibility == (health is not None):
        raise ValueError("Choose saved health or explicit scenario eligibility, exactly one")
    if not isinstance(registry, DeploymentRegistry):
        raise ValueError("Registry must be a DeploymentRegistry")
    if not isinstance(carbon_provider, (HistoricalCarbonTrace, StaticCarbonEstimate)):
        raise ValueError("Scenario carbon must be an explicit versioned trace or static estimate")
    if not isinstance(start_offsets_seconds, (list, tuple)) or not start_offsets_seconds or len(start_offsets_seconds) > 128:
        raise ValueError("Start grid must contain 1 to 128 finite offsets")
    offsets = []
    for offset in start_offsets_seconds:
        if (isinstance(offset, bool) or not isinstance(offset, (int, float))
                or not math.isfinite(offset) or offset < 0):
            raise ValueError("Start offsets must be finite nonnegative seconds")
        offsets.append(offset)
    if len(set(offsets)) != len(offsets):
        raise ValueError("Start offsets must be unique")
    # Validate the complete frozen envelope before creating any report output.
    from data.profiles import evaluate_profiles
    evaluate_profiles(profiles, [])
    knowledge = kb or KnowledgeBase.load()
    context = frozen_context(case, knowledge)
    request = build_workflow_request(case["ticket"], workflow_id="scenario-" + case["id"], arrived_at=reference,
        response_budget_seconds=response_seconds, completion_budget_seconds=completion_seconds,
        policy=policy, kb=knowledge, enable_thinking=False)
    materialized = [render_step(request, step.step_id, context, kb=knowledge) for step in request.steps]
    request = replace(request, steps=tuple(item.step for item in materialized))
    if scenario_eligibility:
        health = {deployment.deployment_id: DeploymentHealth(deployment.deployment_id, reference, True,
            tuple(sorted(deployment.models)), "simulated-eligibility") for deployment in registry}
    try:
        grid = {reference + timedelta(seconds=offset) for offset in offsets}
    except OverflowError:
        raise ValueError("Start grid exceeds the supported timestamp range") from None
    generated, finishes, diagnostics = {}, {}, []
    for step in request.steps:
        # Close the explicit time grid with predicted prerequisite finishes.
        # This preserves exact ready times for nondeferrable steps, including
        # fractional-second profiles, without shifting forecasted candidates.
        starts = set(grid) if step.allow_deferral else set()
        base = max(reference, step.earliest_start)
        if not step.dependencies:
            starts.add(base)
        elif all(finishes.get(key) for key in step.dependencies):
            minimum_ready = max(min(finishes[key]) for key in step.dependencies)
            starts.update(max(base, instant) for key in step.dependencies for instant in finishes[key]
                          if instant >= minimum_ready)
        else:
            # Unknown prerequisite latency cannot invent a predicted finish.
            # Retain the explicit grid so unknown-profile diagnostics survive.
            starts.update(grid)
        if len(starts) > 256:
            raise ValueError("Dependency-ready start closure exceeds 256 options for one step")
        generated_step = generate_profile_candidates(step, request.workload_version, registry, profiles,
            tuple(sorted(starts)), as_of=reference, now=reference, carbon_provider=carbon_provider)
        generated[step.step_id] = list(generated_step.candidates)
        diagnostics.append(generated_step.to_dict())
        finishes[step.step_id] = set()
        for option in generated_step.candidates:
            if option.estimates.latency.value is not None:
                try:
                    finishes[step.step_id].add(option.planned_start + timedelta(seconds=option.estimates.latency.value))
                except OverflowError:
                    pass
    scopes = {option.estimates.carbon.unit for options in generated.values() for option in options
              if option.estimates.carbon.value is not None}
    if len(scopes) > 1:
        raise ValueError("Scenario carbon data cannot combine CO2 and CO2e scopes")
    result = plan_workflow(request, generated, _WEIGHTS[policy], now=reference, quality_floor=quality_floor,
        search_limit=search_limit, registry=registry, health=health, health_max_age_seconds=health_max_age_seconds,
        carbon_unit=next(iter(scopes), None))
    return {
        "schema_version": 1, "record_type": "FrozenProfilePlanningScenario", "mode": SCENARIO_MODE,
        "experiment_mode": "simulation", "inference_performed": False,
        "status": "feasible" if result.winner else "infeasible", "case_id": case["id"],
        "case_split": case["split"], "policy": policy, "reference_time": reference.isoformat(),
        "planner_version": PLANNER_VERSION, "calibration_id": profiles["calibration_id"],
        "profile_fingerprint": profiles["fingerprint"], "registry_fingerprint": registry.config_fingerprint(),
        "registry_snapshot": registry.to_dict(), "kb_fingerprint": knowledge.fingerprint(),
        "workload_fingerprint": workload_fingerprint(request, knowledge),
        "case_fingerprint": _fingerprint(case), "frozen_context_fingerprint": _fingerprint(context),
        "context_source": "frozen-calibration-context-v1", "frozen_context": context,
        "eligibility_source": "explicit-simulated-eligibility" if scenario_eligibility else "saved-inventory-health",
        "health_snapshot": [item.to_dict() for _, item in sorted(health.items())],
        "carbon_snapshot": carbon_provider.to_dict(), "carbon_fingerprint": carbon_provider.fingerprint(),
        "start_offsets_seconds": sorted(offsets), "request": request.to_dict(),
        "rendered_prompts": [{"step_id": item.step.step_id, "input_fingerprint": item.input_fingerprint,
                              "source_ids": list(item.source_ids)} for item in materialized],
        "candidate_diagnostics": diagnostics, "result": result.to_dict(),
        "limitations": [
            "Downstream prompts use frozen fixture outputs; no connected workflow was executed.",
            "Per-step calibrated quality is a scheduling proxy, not measured end-to-end quality.",
            "Eligibility/forecasts would need revalidation before real dispatch; this command never dispatches.",
            "Candidate latency includes only documented profile scope; no transfer/load overhead is invented.",
        ],
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profiles", type=Path, required=True)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--case", required=True)
    parser.add_argument("--cases", type=Path, default=DEFAULT_TICKETS_PATH)
    parser.add_argument("--split", choices=("calibration", "held_out"), default="calibration")
    parser.add_argument("--reference-utc", required=True)
    parser.add_argument("--policy", choices=tuple(_WEIGHTS), default="balanced")
    parser.add_argument("--start-offset-seconds", required=True, help="Comma-separated finite nonnegative start offsets")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--quality-floor", type=float, default=0)
    parser.add_argument("--search-limit", type=int, default=100_000)
    parser.add_argument("--health-max-age-seconds", type=float, default=300)
    parser.add_argument("--response-seconds", type=int, default=300)
    parser.add_argument("--completion-seconds", type=int, default=600)
    parser.add_argument("--kb", type=Path)
    eligibility = parser.add_mutually_exclusive_group(required=True)
    eligibility.add_argument("--health-inventory", type=Path)
    eligibility.add_argument("--scenario-eligibility", action="store_true")
    carbon = parser.add_mutually_exclusive_group(required=True)
    carbon.add_argument("--carbon-estimate", type=Path)
    carbon.add_argument("--carbon-trace", type=Path)
    args = parser.parse_args(argv)
    try:
        registry = DeploymentRegistry.load(args.config)
        cases = load_cases(args.cases, split=args.split)
        selected = next((case for case in cases if case["id"] == args.case), None)
        if selected is None:
            raise ValueError("Requested case is absent from the selected split")
        provider = (StaticCarbonEstimate.from_dict(_json(args.carbon_estimate)) if args.carbon_estimate
                    else HistoricalCarbonTrace.load(args.carbon_trace))
        result = plan_case(load_frozen_profiles(args.profiles), registry, selected,
            reference_time=args.reference_utc, carbon_provider=provider, policy=args.policy,
            start_offsets_seconds=tuple(float(item) for item in args.start_offset_seconds.split(",")),
            scenario_eligibility=args.scenario_eligibility,
            health=load_inventory_health(args.health_inventory, registry) if args.health_inventory else None,
            health_max_age_seconds=args.health_max_age_seconds, quality_floor=args.quality_floor,
            search_limit=args.search_limit, kb=KnowledgeBase.load(args.kb),
            response_seconds=args.response_seconds, completion_seconds=args.completion_seconds)
        write_json(args.output, result)
        print(json.dumps({"status": result["status"], "mode": result["mode"], "output": str(args.output),
                          "profile_fingerprint": result["profile_fingerprint"],
                          "rejection_counts": result["result"]["rejection_counts"]}, allow_nan=False))
        return 0  # Infeasibility is a successful, explicit planning result.
    except (ValueError, OSError, KeyError, TypeError, OverflowError):
        print(json.dumps({"error_code": "invalid-frozen-profile-planning-input"}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
