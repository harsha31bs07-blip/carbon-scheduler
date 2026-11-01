"""Staged planning uses actual outputs and keeps future workload assumptions explicit."""

from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json

import pytest

from data.carbon_sources import HistoricalCarbonTrace
from data.profiles import CONTEXT_SOURCE, build_profiles
from scheduler.joint import CommittedAssignment, Reservation
from scheduler.profile_candidates import generate_forecast_candidates
from shared.deployments import Deployment, DeploymentHealth, DeploymentRegistry
from shared.scheduling import CarbonSample, WorkloadSizeForecast
from workflow.connected import workload_fingerprint
from workflow.planning import prepare_plan
from workflow.scheduling_records import load_record, save_record
from workflow.workload import KnowledgeBase, build_workflow_request


NOW = datetime(2026, 10, 26, 12, tzinfo=timezone.utc)


def at(seconds):
    return NOW + timedelta(seconds=seconds)


@pytest.fixture
def inputs(monkeypatch):
    import requests
    from workflow import calibrate
    monkeypatch.setattr(requests, "get", lambda *a, **k: pytest.fail("Planning probed a provider"))
    monkeypatch.setattr(requests, "post", lambda *a, **k: pytest.fail("Planning performed inference"))
    monkeypatch.setattr(calibrate, "frozen_context", lambda *a, **k: pytest.fail("Planning used fixture answers"))
    deployment = Deployment("local", "ollama", "http://simulation.invalid:11434", {"small": "simulated:1"},
                            region="simulated-region", grid="simulated-grid", location_source="simulated-fixture")
    registry = DeploymentRegistry((deployment,))
    kb = KnowledgeBase.load()
    request = build_workflow_request("My CarbonDesk account is locked after five failed login attempts.",
                                    workflow_id="simulation", arrived_at=NOW, kb=kb, policy="speed")
    records = []
    for step in request.steps:
        for size in (1, 4000, 8000):
            records.append(dict(schema_version=1, record_type="CalibrationTrial", trial_id=f"{step.step_id}-{size}",
                split="calibration", context_source=CONTEXT_SOURCE, model_id="small", deployment_id="local",
                deployment_fingerprint=deployment.config_fingerprint(), step_id=step.step_id, task_type=step.task_type,
                workload_version=request.workload_version, input_size_estimate=size, output_limit_tokens=step.output_limit_tokens,
                enable_thinking=step.enable_thinking, warmup=False, cold_start=False, status="completed", output_valid=True,
                completion_reason="stop", latency_seconds=1 + size / 1000, load_seconds=0., input_tokens=1, output_tokens=1,
                quality_score=.9, quality_source="manual-review:simulated-fixture", energy_kwh=.001,
                energy_provenance="estimated", energy_source="simulated-fixture", carbon_g=.5,
                carbon_provenance="estimated", carbon_source="simulated-fixture", cost_usd=.01,
                cost_provenance="estimated", cost_source="simulated-fixture"))
    profiles = build_profiles(records, calibration_id="simulated-profiles")
    health = {"local": DeploymentHealth("local", at(-1), True, ("small",), "simulated-health")}
    sample = CarbonSample("simulated-grid", at(-1), NOW, at(1000), 500., "simulated-grid-trace", "simulated")
    feed = HistoricalCarbonTrace("simulation", (sample,), timedelta(seconds=1000))
    forecasts = {step.step_id: WorkloadSizeForecast(step.step_id, workload_fingerprint(request, kb),
        request.workload_version, profiles["fingerprint"], 300, 2500, NOW, at(500), "simulated-context-size-study")
        for step in request.steps[1:]}
    return dict(request=request, registry=registry, profiles=profiles, kb=kb, now=NOW,
                health=health, carbon_provider=feed, forecasts=forecasts, experiment_mode="simulation", _trials=records)


def plan(inputs, **changes):
    arguments = {**inputs, **changes}
    arguments.pop("_trials", None)
    return prepare_plan(**arguments)


def assignment(prepared, name):
    return next(item for item in prepared.result.winner.assignments if item.candidate.step_id == name)


def test_submission_renders_only_ready_step_and_forecasts_never_become_dispatch_prompts(inputs):
    prepared = plan(inputs)
    assert prepared.result.winner is not None
    contexts = prepared.report["contexts"]
    assert [item["input_mode"] for item in contexts] == ["materialized", "provisional", "provisional", "provisional"]
    assert all(item["materialized_step"] is None and item["input_fingerprint"] is None for item in contexts[1:])
    assert set(prepared.rendered_steps) == {"classify"}
    assert prepared.report["dispatchable_step_ids"] == ["classify"]
    future = prepared.report["candidate_diagnostics"][1]
    assert future["input_size_estimate"] == 2500
    assert future["workload_forecast"] is not None
    assert "provisional-size-forecast" in assignment(prepared, "draft").candidate.estimates.latency.source
    assert not prepared.report["inference_performed"]
    json.dumps(prepared.report, allow_nan=False)


def test_missing_forecasts_produces_infeasibility_with_explicit_missing_context(inputs):
    prepared = plan(inputs, forecasts={})
    assert prepared.result.winner is None
    assert [item["reason"] for item in prepared.report["contexts"][1:]] == ["missing-workload-size-forecast"] * 3
    assert prepared.report["dispatchable_step_ids"] == []


def test_completion_replaces_forecast_with_actual_prompt_and_freezes_execution(inputs):
    first = plan(inputs)
    chosen = assignment(first, "classify").candidate
    fixed = CommittedAssignment(chosen, NOW, at(2))
    next_plan = plan(inputs, now=at(2), fixed_assignments=(fixed,), outputs={"classify": "account_access"},
                     previous_plan=first.result.winner, trigger="classify-completed")
    assert next_plan.result.winner is not None
    assert next_plan.report["contexts"][1]["input_mode"] == "materialized"
    assert "account_access" in next_plan.rendered_steps["retrieve"].step.prompt
    assert assignment(next_plan, "classify").candidate == chosen
    assert assignment(next_plan, "classify").committed is True
    assert assignment(next_plan, "retrieve").start == at(2)
    assert next_plan.report["dispatchable_step_ids"] == ["retrieve"]
    assert "provisional-size-forecast" not in assignment(next_plan, "retrieve").candidate.estimates.latency.source


def test_actual_dependency_output_size_changes_downstream_latency(inputs):
    first = plan(inputs)
    classify = CommittedAssignment(assignment(first, "classify").candidate, NOW, at(2))
    second = plan(inputs, now=at(2), fixed_assignments=(classify,), outputs={"classify": "account_access"})
    retrieve = CommittedAssignment(assignment(second, "retrieve").candidate, at(2), at(5))
    common = dict(now=at(5), fixed_assignments=(classify, retrieve))
    short = {"classify": "account_access", "retrieve": "Wait 15 minutes. [KB-ACCOUNT-002]"}
    long = {**short, "retrieve": "Wait 15 minutes. " * 100 + "[KB-ACCOUNT-002]"}
    a, b = plan(inputs, outputs=short, **common), plan(inputs, outputs=long, **common)
    assert a.result.winner is not None and b.result.winner is not None
    assert assignment(a, "draft").candidate.estimates.latency.value < assignment(b, "draft").candidate.estimates.latency.value
    assert a.report["contexts"][2]["input_fingerprint"] != b.report["contexts"][2]["input_fingerprint"]


def test_forecast_cannot_cover_actual_prompt_outside_profile_range(inputs):
    first = plan(inputs)
    classify = CommittedAssignment(assignment(first, "classify").candidate, NOW, at(2))
    second = plan(inputs, now=at(2), fixed_assignments=(classify,), outputs={"classify": "account_access"})
    retrieve = CommittedAssignment(assignment(second, "retrieve").candidate, at(2), at(5))
    result = plan(inputs, now=at(5), fixed_assignments=(classify, retrieve),
                  outputs={"classify": "account_access", "retrieve": "x" * 9000 + " [KB-ACCOUNT-002]"})
    assert result.result.winner is None
    draft_report = next(item for item in result.report["candidate_diagnostics"] if item["step_id"] == "draft")
    assert "unknown-latency" in draft_report["diagnostics"][0]["reasons"]


def test_actual_output_size_can_change_model_choice(inputs):
    first = plan(inputs)
    classify = CommittedAssignment(assignment(first, "classify").candidate, NOW, at(2))
    second = plan(inputs, now=at(2), fixed_assignments=(classify,), outputs={"classify": "account_access"})
    retrieve = CommittedAssignment(assignment(second, "retrieve").candidate, at(2), at(5))
    deployment = replace(next(iter(inputs["registry"])), models={"small": "simulated:1", "large": "simulated:2"})
    records = []
    for original in inputs["_trials"]:
        small = {**original, "deployment_fingerprint": deployment.config_fingerprint()}
        large = {**small, "trial_id": original["trial_id"] + "-large", "model_id": "large",
                 "latency_seconds": 3 + original["input_size_estimate"] / 10000,
                 "energy_kwh": .002, "cost_usd": .02}
        records.extend((small, large))
    profiles = build_profiles(records, calibration_id="simulated-two-models")
    forecasts = {key: replace(value, profile_fingerprint=profiles["fingerprint"])
                 for key, value in inputs["forecasts"].items()}
    common = dict(now=at(5), fixed_assignments=(classify, retrieve), registry=DeploymentRegistry((deployment,)),
                  profiles=profiles, forecasts=forecasts,
                  health={"local": DeploymentHealth("local", at(4), True, ("small", "large"), "simulated-health")})
    short = {"classify": "account_access", "retrieve": "Wait 15 minutes. [KB-ACCOUNT-002]"}
    long = {**short, "retrieve": "Wait 15 minutes. " * 200 + "[KB-ACCOUNT-002]"}
    a, b = plan(inputs, outputs=short, **common), plan(inputs, outputs=long, **common)
    assert assignment(a, "draft").candidate.model_id == "small"
    assert assignment(b, "draft").candidate.model_id == "large"


@pytest.mark.parametrize("change", ["stale", "future", "wrong-profile", "wrong-workflow", "wrong-version"])
def test_invalid_active_forecast_is_refused(inputs, change):
    forecasts = dict(inputs["forecasts"])
    original = forecasts["draft"]
    changes = {"stale": dict(issued_at=at(-10), valid_until=NOW), "future": dict(issued_at=at(1)),
               "wrong-profile": dict(profile_fingerprint="a" * 64),
               "wrong-workflow": dict(workflow_fingerprint="b" * 64), "wrong-version": dict(workload_version="changed")}
    forecasts["draft"] = replace(original, **changes[change])
    with pytest.raises(ValueError, match="forecast"):
        plan(inputs, forecasts=forecasts)


def test_entire_forecast_interval_must_be_supported_not_just_upper_endpoint(inputs):
    forecasts = dict(inputs["forecasts"])
    forecasts["draft"] = replace(forecasts["draft"], maximum_input_bytes=9000)
    assert plan(inputs, forecasts=forecasts).result.winner is None


def test_forecast_validity_covers_future_start_and_finish(inputs):
    forecast = replace(inputs["forecasts"]["summarize"], valid_until=at(2))
    step = inputs["request"].steps[-1]
    generated = generate_forecast_candidates(step, inputs["request"].workload_version, inputs["registry"], inputs["profiles"],
        (at(1), at(2)), forecast=forecast, workflow_fingerprint=forecast.workflow_fingerprint,
        as_of=NOW, now=NOW, carbon_provider=inputs["carbon_provider"])
    assert generated.candidates == ()
    assert "predicted-finish-after-workload-forecast-validity" in generated.diagnostics[0].reasons
    assert "start-after-workload-forecast-validity" in generated.diagnostics[1].reasons


def test_capacity_reservation_end_is_included_for_urgent_step(inputs):
    result = plan(inputs, reservations=(Reservation("local", NOW, at(10)),))
    assert result.result.winner is not None
    assert assignment(result, "classify").start == at(10)
    assert result.report["dispatchable_step_ids"] == []


def test_stale_health_and_elapsed_deadlines_remove_eligibility(inputs):
    assert plan(inputs, health={}).result.winner is None
    assert plan(inputs, now=at(301), health_max_age_seconds=300).result.winner is None


@pytest.mark.parametrize("outputs", [{"classify": "account_access"}, {"draft": "Fabricated result"}])
def test_uncommitted_outputs_are_never_accepted_as_observations(inputs, outputs):
    with pytest.raises(ValueError, match="outputs"):
        plan(inputs, outputs=outputs)


def test_live_service_requires_frozen_review_coverage_policy(inputs):
    with pytest.raises(ValueError, match="coverage policy"):
        plan(inputs, experiment_mode="live")


def test_live_preparation_excludes_simulated_metrics_and_retains_the_reason(inputs):
    records = [{**record, "quality_review_fingerprint": "a" * 64, "case_id": "simulated-case"}
               for record in inputs["_trials"]]
    policy = {"schema_version": 1, "record_type": "QualityCoveragePolicy", "policy_id": "simulation-test-policy",
              "minimum_reviewed_trials": 1, "minimum_distinct_cases": 1, "minimum_reviewed_fraction": 1.}
    profiles = build_profiles(records, calibration_id="simulated-reviewed", quality_policy=policy)
    forecasts = {key: replace(value, profile_fingerprint=profiles["fingerprint"])
                 for key, value in inputs["forecasts"].items()}
    rejected = plan(inputs, profiles=profiles, forecasts=forecasts, experiment_mode="live")
    assert rejected.result.winner is None
    assert "simulation-only-metrics" in rejected.report["mode_exclusions"].values()
    sample = CarbonSample("simulated-grid", at(-1), NOW, at(1000), 500., "test-static-estimate", "estimated")
    feed = HistoricalCarbonTrace("test-estimated-trace", (sample,), timedelta(seconds=1000))
    eligible = plan(inputs, profiles=profiles, forecasts=forecasts, experiment_mode="live", carbon_provider=feed)
    assert eligible.result.winner is not None and eligible.report["mode_exclusions"] == {}
    # A simulated workload forecast alone also keeps its candidates out of live planning.
    forecasts["draft"] = replace(forecasts["draft"], provenance="simulated")
    forecast_rejected = plan(inputs, profiles=profiles, forecasts=forecasts, experiment_mode="live", carbon_provider=feed)
    assert forecast_rejected.result.winner is None and forecast_rejected.report["mode_exclusions"]


@pytest.mark.parametrize("weights", [None, {"latency": 1}, {"latency": True, "accuracy": 0, "energy": 0, "cost": 0, "carbon": 0},
    {name: 0 for name in ("latency", "accuracy", "energy", "cost", "carbon")}])
def test_invalid_custom_weights_fail_before_planning(inputs, weights):
    request = replace(inputs["request"], policy="custom")
    with pytest.raises(ValueError):
        plan(inputs, request=request, custom_weights=weights)


def test_forecast_contract_roundtrips_and_rejects_unknown_or_malformed_fields(inputs, tmp_path):
    forecast = inputs["forecasts"]["draft"]
    path = tmp_path / "forecast.json"
    save_record(path, forecast)
    assert load_record(path) == forecast
    invalid = forecast.to_dict()
    invalid["claimed_measured"] = True
    with pytest.raises(ValueError):
        WorkloadSizeForecast.from_dict(invalid)
    for change in ({"minimum_input_bytes": True}, {"maximum_input_bytes": 0},
                   {"issued_at": datetime(2026, 10, 26)}, {"source": ""}):
        with pytest.raises(ValueError):
            replace(forecast, **change)
