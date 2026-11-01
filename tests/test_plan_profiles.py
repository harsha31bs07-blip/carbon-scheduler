"""Frozen-profile planning bridge never invokes inference or invents quality."""

from datetime import datetime, timedelta, timezone
import json

import pytest

from data.carbon_sources import StaticCarbonEstimate
from data.profiles import CONTEXT_SOURCE, build_profiles
from scheduler.plan_profiles import load_inventory_health, main, plan_case
from shared.deployments import Deployment, DeploymentHealth, DeploymentRegistry
from workflow.calibrate import frozen_context, load_cases
from workflow.workload import KnowledgeBase, build_workflow_request, render_step


NOW = datetime(2026, 10, 19, 14, tzinfo=timezone.utc)


@pytest.fixture
def inputs(monkeypatch):
    import requests
    from workflow import deployment_clients
    monkeypatch.setattr(requests, "get", lambda *a, **k: pytest.fail("planning made an HTTP GET"))
    monkeypatch.setattr(requests, "post", lambda *a, **k: pytest.fail("planning made an HTTP POST"))
    monkeypatch.setattr(deployment_clients, "call_deployment", lambda *a, **k: pytest.fail("planning inferred"))
    registry = DeploymentRegistry([Deployment("simulated", "ollama", "http://simulated.invalid:11434",
        {"small": "simulated:1"}, region="simulated-region", grid="simulated-grid",
        location_source="simulated-fixture")])
    kb, case = KnowledgeBase.load(), load_cases()[0]
    request = build_workflow_request(case["ticket"], workflow_id="fixture", arrived_at=NOW, kb=kb)
    context = frozen_context(case, kb)
    trials = []
    for step in request.steps:
        rendered = render_step(request, step.step_id, context, kb=kb).step
        trials.append({
            "schema_version": 1, "record_type": "CalibrationTrial", "trial_id": step.step_id,
            "split": "calibration", "context_source": CONTEXT_SOURCE, "model_id": "small",
            "deployment_id": "simulated", "deployment_fingerprint": registry.get("simulated").config_fingerprint(),
            "step_id": step.step_id, "task_type": step.task_type, "workload_version": request.workload_version,
            "input_size_estimate": len(rendered.prompt.encode("utf-8")),
            "output_limit_tokens": step.output_limit_tokens, "enable_thinking": False,
            "warmup": False, "cold_start": False, "status": "completed", "output_valid": True,
            "completion_reason": "stop", "latency_seconds": 1.25, "load_seconds": .1,
            "input_tokens": 30, "output_tokens": 10, "quality_score": None, "quality_source": "unknown",
            "energy_kwh": .001, "energy_provenance": "estimated", "energy_source": "test-fixture-estimate",
            "carbon_g": 1, "carbon_unit": "gCO2e", "carbon_provenance": "estimated", "carbon_source": "test-fixture-estimate",
            "cost_usd": 0, "cost_provenance": "estimated", "cost_source": "test-fixture-estimate",
        })
    carbon = StaticCarbonEstimate("simulated-grid", 100, "simulated-static-fixture", NOW,
        NOW, NOW + timedelta(hours=1), timedelta(hours=1))
    return registry, kb, case, trials, carbon


def report(inputs, **kwargs):
    registry, kb, case, trials, carbon = inputs
    return plan_case(build_profiles(trials, calibration_id="fixture-calibration"), registry, case,
        reference_time=NOW, carbon_provider=carbon, kb=kb,
        **{"scenario_eligibility": True, "start_offsets_seconds": (0, 60), **kwargs})


def test_unknown_reviewed_quality_yields_structured_infeasibility_with_diagnostics(inputs):
    result = report(inputs)
    assert result["status"] == "infeasible"
    assert result["inference_performed"] is False and result["experiment_mode"] == "simulation"
    assert result["eligibility_source"] == "explicit-simulated-eligibility"
    assert result["result"]["winner"] is None
    assert result["result"]["rejection_counts"]["unknown-accuracy"] > 0
    assert len(result["candidate_diagnostics"]) == 4
    assert result["context_source"] == CONTEXT_SOURCE
    assert result["frozen_context"]["classify"] == inputs[2]["expected_category"]
    assert all(item["input_fingerprint"] for item in result["rendered_prompts"])
    assert len(result["profile_fingerprint"]) == len(result["workload_fingerprint"]) == 64


def test_exact_dependency_finish_closure_handles_fractional_profiles(inputs):
    registry, kb, case, trials, carbon = inputs
    for trial in trials:
        trial.update(quality_score=.9, quality_source="manual-review:simulated-rubric")
    result = report(inputs)
    assert result["status"] == "feasible", result["result"]["rejection_counts"]
    chosen = result["result"]["winner"]["assignments"]
    assert [item["start"] for item in chosen] == [
        NOW.isoformat().replace("+00:00", "Z"),
        (NOW + timedelta(seconds=1.25)).isoformat().replace("+00:00", "Z"),
        (NOW + timedelta(seconds=2.5)).isoformat().replace("+00:00", "Z"),
        (NOW + timedelta(seconds=3.75)).isoformat().replace("+00:00", "Z"),
    ]
    assert result["result"]["winner"]["metrics"]["latency"] == 5
    assert result["mode"] == "frozen-context-planning-scenario"


def test_missing_eligibility_is_rejected_and_saved_outage_remains_infeasible(inputs):
    registry = inputs[0]
    with pytest.raises(ValueError, match="exactly one"):
        report(inputs, scenario_eligibility=False)
    unhealthy = {"simulated": DeploymentHealth("simulated", NOW, False, (), "saved-observation")}
    result = report(inputs, scenario_eligibility=False, health=unhealthy)
    assert result["eligibility_source"] == "saved-inventory-health"
    assert result["result"]["rejection_counts"]["deployment-unavailable-or-unknown"] > 0
    with pytest.raises(ValueError, match="exactly one"):
        report(inputs, health=unhealthy)


def test_saved_inventory_requires_exact_configuration_and_complete_unique_health(inputs, tmp_path):
    registry = inputs[0]
    deployment = next(iter(registry))
    health = DeploymentHealth(deployment.deployment_id, NOW, True, ("small",), "saved-observation")
    payload = {"schema_version": 1, "configuration_fingerprint": registry.config_fingerprint(),
               "deployments": [{"configuration": deployment.to_dict(), "health": health.to_dict()}]}
    path = tmp_path / "inventory.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    assert load_inventory_health(path, registry) == {deployment.deployment_id: health}
    payload["deployments"].append(payload["deployments"][0])
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValueError):
        load_inventory_health(path, registry)


@pytest.mark.parametrize("offsets", [(), (0, 0), (-1,), (float("nan"),), (True,)])
def test_invalid_start_grids_fail_before_any_plan(inputs, offsets):
    with pytest.raises(ValueError):
        report(inputs, start_offsets_seconds=offsets)


def test_cli_writes_auditable_infeasible_report_without_inference(inputs, tmp_path, capsys):
    registry, kb, case, trials, carbon = inputs
    profiles = build_profiles(trials, calibration_id="fixture-calibration")
    paths = {}
    for name, payload in (("profiles", profiles), ("registry", registry.to_dict()), ("carbon", carbon.to_dict())):
        path = tmp_path / f"{name}.json"
        path.write_text(json.dumps(payload), encoding="utf-8")
        paths[name] = path
    output = tmp_path / "report.json"
    args = ["--profiles", str(paths["profiles"]), "--config", str(paths["registry"]),
            "--case", case["id"], "--carbon-estimate", str(paths["carbon"]),
            "--reference-utc", NOW.isoformat(), "--start-offset-seconds", "0,60",
            "--scenario-eligibility", "--output", str(output)]
    assert main(args) == 0
    result = json.loads(output.read_text())
    assert result["status"] == "infeasible" and result["profile_fingerprint"] == profiles["fingerprint"]
    assert json.loads(capsys.readouterr().out)["status"] == "infeasible"
    original = output.read_bytes()
    args[args.index("--case") + 1] = "missing-case"
    assert main(args) == 2
    assert output.read_bytes() == original
