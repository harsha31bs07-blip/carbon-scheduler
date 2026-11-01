"""Durable decisions reject semantic corruption even with recomputed hashes."""

from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import subprocess
import sys

import pytest

from data.carbon_sources import HistoricalCarbonTrace, StaticCarbonEstimate
from data.profiles import CONTEXT_SOURCE, build_profiles
from data.quality_reviews import fingerprint
from scheduler.joint import CommittedAssignment
from shared.deployments import Deployment, DeploymentHealth, DeploymentRegistry
from shared.scheduling import CarbonSample, WorkloadSizeForecast
from workflow.planning import prepare_plan
from workflow.planning_records import decode_carbon, decode_knowledge_base, validate_context, validate_report
from workflow.workload import KnowledgeBase, build_workflow_request


NOW = datetime(2026, 10, 26, 12, tzinfo=timezone.utc)


def seal(report):
    report["fingerprint"] = fingerprint({key: value for key, value in report.items() if key != "fingerprint"})
    return report


@pytest.fixture
def inputs():
    kb = KnowledgeBase.load()
    registry = DeploymentRegistry([Deployment("fixture-local", "ollama", "http://fixture.invalid:11434",
        {"small": "fixture:3b"}, region="fixture-region", grid="fixture-grid", location_source="test-fixture")])
    deployment = next(iter(registry))
    request = build_workflow_request("My CarbonDesk account is locked after failed login attempts.",
        workflow_id="fixture-workflow", arrived_at=NOW, policy="balanced", kb=kb)
    records = []
    for step in request.steps:
        for size in (1, 8000):
            records.append(dict(schema_version=1, record_type="CalibrationTrial", trial_id=f"{step.step_id}-{size}",
                case_id="fixture-case", split="calibration", context_source=CONTEXT_SOURCE, model_id="small",
                deployment_id=deployment.deployment_id, deployment_fingerprint=deployment.config_fingerprint(),
                step_id=step.step_id, task_type=step.task_type, workload_version=request.workload_version,
                input_size_estimate=size, output_limit_tokens=step.output_limit_tokens,
                enable_thinking=step.enable_thinking, warmup=False, cold_start=False, status="completed",
                output_valid=True, completion_reason="stop", latency_seconds=1., load_seconds=0.,
                input_tokens=1, output_tokens=1, quality_score=.9, quality_source="manual-review:test-fixture",
                quality_review_fingerprint=fingerprint({"step": step.step_id, "size": size, "mode": "test-fixture"}),
                energy_kwh=.001, energy_provenance="estimated", energy_source="test-fixture",
                carbon_g=.5, carbon_provenance="estimated", carbon_source="test-fixture",
                cost_usd=.01, cost_provenance="estimated", cost_source="test-fixture"))
    policy = dict(schema_version=1, record_type="QualityCoveragePolicy", policy_id="test-fixture",
                  minimum_reviewed_trials=1, minimum_distinct_cases=1, minimum_reviewed_fraction=1.)
    profiles = build_profiles(records, calibration_id="fixture-calibration", quality_policy=policy)
    sample = CarbonSample("fixture-grid", NOW - timedelta(seconds=1), NOW, NOW + timedelta(hours=1),
                          500., "test-fixture", "estimated")
    provider = HistoricalCarbonTrace("test-fixture", (sample,), timedelta(hours=1))
    health = {deployment.deployment_id: DeploymentHealth(deployment.deployment_id, NOW, True, ("small",), "fixture-health")}
    # Use the actual service's binding, without importing it in the validator.
    import hashlib
    workload_id = hashlib.sha256(json.dumps({"input_text": request.input_text,
        "workload_version": request.workload_version, "knowledge_base": kb.fingerprint()}, sort_keys=True).encode()).hexdigest()
    forecasts = {step.step_id: WorkloadSizeForecast(step.step_id, workload_id, request.workload_version,
        profiles["fingerprint"], 100, 4000, NOW, NOW + timedelta(minutes=10), "test-fixture") for step in request.steps[1:]}
    kb_snapshot = dict(schema_version=1, kb_version=kb.version, description=kb.description,
                       categories=list(kb.categories), articles=[article.to_dict() for article in kb.articles])
    context = dict(schema_version=1, record_type="DurablePlanningContext", profiles=profiles,
        forecasts=[item.to_dict() for item in forecasts.values()], knowledge_base=kb_snapshot,
        carbon=provider.to_dict(), custom_weights=None, quality_floor=.8, start_offsets_seconds=[0, 10],
        search_limit=1000, health_max_age_seconds=30., minimum_improvement=0., replan_interval_seconds=5.,
        probe_timeout_seconds=3., max_decision_age_seconds=5.)
    configuration = dict(experiment_mode="live", worker_version="sqlite-planned-worker-v1",
        registry_snapshot=registry.to_dict(), registry_fingerprint=registry.config_fingerprint(),
        calibration_id=profiles["calibration_id"], kb_fingerprint=kb.fingerprint(), workload_fingerprint=workload_id)
    return dict(request=request, registry=registry, profiles=profiles, kb=kb, now=NOW, health=health,
                carbon_provider=provider, forecasts=forecasts, context=context, configuration=configuration)


def prepare(inputs, **changes):
    kwargs = {key: value for key, value in inputs.items() if key not in {"context", "configuration"}}
    kwargs.update(start_offsets_seconds=inputs["context"]["start_offsets_seconds"],
                  search_limit=inputs["context"]["search_limit"], quality_floor=inputs["context"]["quality_floor"],
                  health_max_age_seconds=inputs["context"]["health_max_age_seconds"])
    kwargs.update(changes)
    return prepare_plan(**kwargs)


def validate(report, inputs):
    return validate_report(report, request=inputs["request"], configuration=inputs["configuration"], context=inputs["context"])


def test_context_and_report_round_trip_return_independent_snapshots(inputs):
    context = inputs["context"]
    restored = validate_context(context, inputs["request"], inputs["configuration"])
    assert restored == context and restored is not context
    prepared = prepare(inputs)
    assert prepared.result.winner is not None, dict(prepared.result.rejection_counts)
    accepted = validate(prepared.report, inputs)
    assert accepted == prepared.report and accepted is not prepared.report
    accepted["contexts"][0]["source_ids"].append("changed-only-on-copy")
    assert prepared.report["contexts"][0]["source_ids"] == []


def test_missing_forecasts_are_retained_as_infeasible_with_no_dispatch(inputs):
    inputs["forecasts"] = {}
    inputs["context"]["forecasts"] = []
    prepared = prepare(inputs)
    assert prepared.result.winner is None
    assert validate(prepared.report, inputs)["dispatchable_step_ids"] == []


def test_standalone_report_validation_uses_default_kb_and_retained_candidate_pools(inputs):
    report = prepare(inputs).report
    assert validate_report(report) == report
    changed = deepcopy(report)
    changed["quality_policy"] = None
    with pytest.raises(ValueError):
        validate_report(seal(changed))


def test_completed_output_and_materialized_downstream_prompt_are_revalidated(inputs):
    first = prepare(inputs)
    selected = next(item.candidate for item in first.result.winner.assignments if item.candidate.step_id == "classify")
    committed = CommittedAssignment(selected, NOW, NOW + timedelta(seconds=1))
    prepared = prepare(inputs, now=NOW + timedelta(seconds=1), outputs={"classify": "account_access"},
                       fixed_assignments=(committed,))
    assert validate(prepared.report, inputs)["dispatchable_step_ids"] == ["retrieve"]
    corrupted = deepcopy(prepared.report)
    corrupted["accepted_outputs"]["classify"] = "billing"
    with pytest.raises(ValueError, match="Materialized context"):
        validate(seal(corrupted), inputs)


def test_retained_incumbent_report_is_verified_against_current_candidates(inputs):
    first = prepare(inputs)
    prepared = prepare(inputs, previous_plan=first.result.winner)
    assert prepared.report["previous_plan_fingerprint"] is not None
    assert validate(prepared.report, inputs)["result"]["winner"] is not None


def test_carbon_and_kb_snapshots_decode_without_filesystem_sources(inputs):
    trace = decode_carbon(inputs["context"]["carbon"])
    assert trace.to_dict() == inputs["carbon_provider"].to_dict()
    static = StaticCarbonEstimate("fixture-grid", 500., "test-fixture", NOW, NOW, NOW + timedelta(days=1),
                                  timedelta(days=1))
    assert decode_carbon(static.to_dict()).to_dict() == static.to_dict()
    assert decode_carbon(None) is None
    assert decode_knowledge_base(inputs["context"]["knowledge_base"]).fingerprint() == inputs["kb"].fingerprint()
    with pytest.raises(ValueError):
        decode_carbon({"record_type": "ForecastFromURL", "url": "https://example.invalid"})


@pytest.mark.parametrize("field,value", [
    ("schema_version", True), ("record_type", "Other"), ("search_limit", 0),
    ("search_limit", 100001), ("search_limit", True), ("start_offsets_seconds", []),
    ("start_offsets_seconds", [0, 0]), ("start_offsets_seconds", [False]),
    ("start_offsets_seconds", [float("inf")]), ("start_offsets_seconds", [-1]),
    ("quality_floor", True), ("quality_floor", 1.01), ("quality_floor", {"draft": .8}),
    ("health_max_age_seconds", -1), ("minimum_improvement", 2),
    ("probe_timeout_seconds", 0), ("probe_timeout_seconds", 31),
    ("replan_interval_seconds", float("nan")), ("max_decision_age_seconds", 0),
    ("custom_weights", {"cost": 1}), ("credentials", "must-not-be-retained"),
])
def test_context_rejects_unknown_fields_invalid_bounds_and_unsafe_inputs(inputs, field, value):
    context = deepcopy(inputs["context"])
    context[field] = value
    with pytest.raises(ValueError):
        validate_context(context, inputs["request"], inputs["configuration"])


@pytest.mark.parametrize("change", ["duplicate", "wrong-profile", "wrong-workload", "wrong-step", "wrong-version"])
def test_forecasts_are_bound_to_exact_profile_and_workload(inputs, change):
    context = deepcopy(inputs["context"])
    if change == "duplicate":
        context["forecasts"].append(deepcopy(context["forecasts"][0]))
    else:
        key, value = {"wrong-profile": ("profile_fingerprint", "a" * 64),
                      "wrong-workload": ("workflow_fingerprint", "b" * 64),
                      "wrong-step": ("step_id", "other"), "wrong-version": ("workload_version", "other")}[change]
        context["forecasts"][0][key] = value
    with pytest.raises(ValueError):
        validate_context(context, inputs["request"], inputs["configuration"])


@pytest.mark.parametrize("change", ["profile-hash", "missing-quality-policy", "kb", "configuration", "request"])
def test_context_binds_profiles_kb_configuration_and_request(inputs, change):
    context, config, request = deepcopy(inputs["context"]), deepcopy(inputs["configuration"]), inputs["request"]
    if change == "profile-hash":
        context["profiles"]["fingerprint"] = "a" * 64
    elif change == "missing-quality-policy":
        del context["profiles"]["quality_policy"]
    elif change == "kb":
        context["knowledge_base"]["articles"][0]["body"] += " Changed KB."
    elif change == "configuration":
        config["calibration_id"] = "other"
    else:
        request = replace(request, input_text="A different ticket")
    with pytest.raises(ValueError):
        validate_context(context, request, config)


@pytest.mark.parametrize("change", [
    "hash", "extra-field", "simulation", "false-inference-claim", "dispatch-provisional",
    "materialized-prompt", "forecast-context", "profile-id", "weight", "deadline", "fake-winner",
    "diagnostic-metric", "diagnostic-omission", "health-model", "quality-policy", "grid-omission",
])
def test_report_rejects_corruption_even_when_outer_hash_is_recomputed(inputs, change):
    report = deepcopy(prepare(inputs).report)
    if change == "hash":
        report["trigger"] = "changed-without-hash"
        with pytest.raises(ValueError, match="fingerprint"):
            validate(report, inputs)
        return
    if change == "extra-field":
        report["endpoint_password"] = "not-allowed"
    elif change == "simulation":
        report["experiment_mode"] = "simulation"
    elif change == "false-inference-claim":
        report["inference_performed"] = True
    elif change == "dispatch-provisional":
        report["dispatchable_step_ids"].append("draft")
    elif change == "materialized-prompt":
        report["contexts"][0]["materialized_step"]["prompt"] = "Injected prompt"
    elif change == "forecast-context":
        report["contexts"][1]["forecast"]["minimum_input_bytes"] += 1
    elif change == "profile-id":
        report["profile_fingerprint"] = "a" * 64
    elif change == "weight":
        report["weights"]["cost"] = .1
    elif change == "deadline":
        report["request"]["response_deadline"] = (NOW + timedelta(seconds=1)).isoformat()
    elif change == "fake-winner":
        report["result"]["winner"]["assignments"][0]["candidate"]["candidate_id"] = "unoffered"
    elif change == "diagnostic-metric":
        report["candidate_diagnostics"][0]["candidates"][0]["estimates"]["cost"]["value"] = 999
    elif change == "diagnostic-omission":
        report["candidate_diagnostics"].pop()
    elif change == "health-model":
        report["health_snapshot"][0]["available_models"] = ["unconfigured"]
    elif change == "quality-policy":
        report["quality_policy"]["minimum_reviewed_trials"] += 1
    else:
        report["candidate_diagnostics"][-1]["diagnostics"] = [item for item in report["candidate_diagnostics"][-1]["diagnostics"]
            if item["planned_start"] != (NOW + timedelta(seconds=10)).isoformat()]
    with pytest.raises(ValueError):
        validate(seal(report), inputs)


def test_failure_envelope_retains_safe_infeasibility_without_fabricating_result(inputs):
    report = seal(dict(schema_version=1, record_type="PlanningPreparationFailure",
        service_version="staged-connected-planning.v1", experiment_mode="live", inference_performed=False,
        request=inputs["request"].to_dict(), now=NOW.isoformat(), trigger="dispatch-preflight",
        error_code="workload-forecast-stale"))
    assert validate(report, inputs) == report
    for key, value in (("error_code", "raw provider failure with credentials"), ("result", {}),
                       ("now", (NOW - timedelta(seconds=1)).isoformat())):
        broken = deepcopy(report)
        broken[key] = value
        with pytest.raises(ValueError):
            validate(seal(broken), inputs)


def test_planning_record_module_does_not_import_provider_transports_or_workers():
    code = """import builtins
original = builtins.__import__
forbidden = ('requests', 'streamlit', 'workflow.deployment_clients', 'workflow.worker', 'workflow.connected')
def guarded(name, *args, **kwargs):
    if any(name == prefix or name.startswith(prefix + '.') for prefix in forbidden):
        raise AssertionError('Forbidden transport import: ' + name)
    return original(name, *args, **kwargs)
builtins.__import__ = guarded
from workflow.planning_records import validate_context, validate_report, decode_carbon, decode_knowledge_base
print('offline-import-ok')
"""
    result = subprocess.run([sys.executable, "-c", code], cwd=Path(__file__).resolve().parents[1],
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "offline-import-ok"


@pytest.mark.parametrize("change", ["context-list", "diagnostic-step-list", "missing-result-default", "health-null"])
def test_malformed_nested_values_raise_value_error_instead_of_runtime_errors(inputs, change):
    report = deepcopy(prepare(inputs).report)
    if change == "context-list":
        report["contexts"][0] = []
    elif change == "diagnostic-step-list":
        report["candidate_diagnostics"][0]["step_id"] = []
    elif change == "missing-result-default":
        del report["result"]["planner_version"]
    else:
        report["health_snapshot"] = None
    with pytest.raises(ValueError):
        validate(seal(report), inputs)
