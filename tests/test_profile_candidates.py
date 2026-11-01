"""Synthetic profile/start-window bridge fixtures; no live inference evidence."""

from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json

import pytest

from data.carbon_sources import HistoricalCarbonTrace
from data.profiles import CONTEXT_SOURCE, build_profiles
from scheduler.joint import plan_workflow
from scheduler.profile_candidates import generate_profile_candidates
from shared.deployments import Deployment, DeploymentRegistry
from shared.scheduling import CarbonSample, StepRequest, WorkflowRequest


NOW = datetime(2026, 10, 19, 12, tzinfo=timezone.utc)


def at(seconds):
    return NOW + timedelta(seconds=seconds)


def deployment(**changes):
    values = dict(deployment_id="edge", provider="ollama", endpoint="http://localhost:11434",
        models={"llama": "llama3.2:3b"}, region="fixture-location", grid="fixture-grid",
        location_source="simulated-fixture")
    return Deployment(**{**values, **changes})


def step(**changes):
    values = dict(step_id="draft", task_type="support-reply", prompt="Materialized prompt.", input_tokens=None,
        output_limit_tokens=128, earliest_start=NOW, latest_finish=at(100), allow_deferral=True,
        enable_thinking=False)
    return StepRequest(**{**values, **changes})


def profiles(dep=None, rendered=None, **changes):
    dep, rendered = dep or deployment(), rendered or step()
    trial = dict(schema_version=1, record_type="CalibrationTrial", trial_id="fixture-trial",
        split="calibration", context_source=CONTEXT_SOURCE, model_id="llama", deployment_id=dep.deployment_id,
        deployment_fingerprint=dep.config_fingerprint(), step_id=rendered.step_id, task_type=rendered.task_type,
        workload_version="support-v1", input_size_estimate=len(rendered.prompt.encode("utf-8")),
        output_limit_tokens=rendered.output_limit_tokens, enable_thinking=rendered.enable_thinking,
        warmup=False, cold_start=False, status="completed", output_valid=True, completion_reason="stop",
        latency_seconds=4., load_seconds=.2, input_tokens=10, output_tokens=5,
        quality_score=.8, quality_source="manual-review:synthetic-fixture", energy_kwh=.02,
        energy_provenance="estimated", energy_source="fixture-energy", cost_usd=.1,
        cost_provenance="estimated", cost_source="fixture-price", carbon_g=999.,
        carbon_provenance="estimated", carbon_source="historical-fixture-only", carbon_unit="gCO2e")
    return build_profiles([{**trial, **changes}], calibration_id="fixture-calibration")


def trace(*, value=500., provenance="estimated", unit="gCO2e/kWh", issued=-1, until=200):
    sample = CarbonSample("fixture-grid", at(issued), at(0), at(until), value,
                          "synthetic-test-feed", provenance, unit)
    return HistoricalCarbonTrace("synthetic-test-trace", (sample,), timedelta(seconds=30))


def generate(rendered=None, dep=None, calibration=None, starts=None, **kwargs):
    rendered, dep = rendered or step(), dep or deployment()
    return generate_profile_candidates(rendered, "support-v1", DeploymentRegistry((dep,)),
        calibration or profiles(dep, rendered), starts or (NOW,), as_of=kwargs.pop("as_of", NOW),
        now=kwargs.pop("now", NOW), carbon_provider=kwargs.pop("carbon_provider", trace()), **kwargs)


def test_exact_profile_and_grid_intensity_produce_explicit_candidate():
    result = generate()
    candidate = result.candidates[0]
    assert candidate.deployment_id == "edge" and candidate.model_id == "llama"
    assert candidate.region == "fixture-location"
    assert candidate.estimates.latency.value == 4
    assert candidate.estimates.accuracy.value == .8
    assert candidate.estimates.carbon.value == 10  # .02 kWh * 500 gCO2e/kWh
    assert candidate.estimates.carbon.value != 999  # Never reuse historical carbon mass.
    assert candidate.estimates.carbon.provenance == "estimated"
    assert result.diagnostics[0].carbon_sample.source == "synthetic-test-feed"
    assert result.diagnostics[0].availability_source == "registry-configuration-only"
    assert result.diagnostics[0].reasons == ()
    json.dumps(result.to_dict(), allow_nan=False)


def test_each_start_uses_its_own_sample_and_preserves_simulation():
    samples = (CarbonSample("fixture-grid", at(-1), at(0), at(10), 500, "test-early", "simulated"),
               CarbonSample("fixture-grid", at(-1), at(10), at(100), 100, "test-later", "simulated"))
    feed = HistoricalCarbonTrace("synthetic", samples, timedelta(seconds=30))
    result = generate(starts=(at(10), NOW), carbon_provider=feed)
    assert [candidate.planned_start for candidate in result.candidates] == [NOW, at(10)]
    assert [candidate.estimates.carbon.value for candidate in result.candidates] == [10, 2]
    assert all(candidate.estimates.carbon.provenance == "simulated" for candidate in result.candidates)
    assert result.candidates[0].candidate_id != result.candidates[1].candidate_id


@pytest.mark.parametrize("unit,expected", [("gCO2/kWh", "gCO2"), ("gCO2e/kWh", "gCO2e")])
def test_carbon_gas_scope_comes_from_current_sample_without_conversion(unit, expected):
    result = generate(carbon_provider=trace(unit=unit))
    assert result.candidates[0].estimates.carbon.unit == expected
    assert result.candidates[0].estimates.carbon.value == 10


def test_sample_must_cover_full_predicted_duration_and_exact_finish_is_allowed():
    result = generate(starts=(at(6), at(7)), carbon_provider=trace(until=10))
    assert result.candidates[0].estimates.carbon.value == 10
    assert result.candidates[1].estimates.carbon.value is None
    assert result.diagnostics[1].carbon_status == "insufficient-duration-coverage"
    assert result.diagnostics[1].excluded is False  # Joint planner sees explicit unknown.


@pytest.mark.parametrize("changes", [{"output_limit_tokens": 129}, {"enable_thinking": True},
    {"task_type": "unrelated-task"}, {"prompt": "Larger input outside the observed input range."}])
def test_settings_task_or_size_changes_do_not_reuse_profile(changes):
    result = generate(rendered=step(**changes), calibration=profiles())
    assert all(metric.value is None for metric in (
        result.candidates[0].estimates.latency, result.candidates[0].estimates.accuracy,
        result.candidates[0].estimates.energy, result.candidates[0].estimates.cost,
        result.candidates[0].estimates.carbon))


def test_deployment_fingerprint_and_supported_model_identity_are_exact():
    changed = deployment(endpoint="http://other.test:11434")
    result = generate(dep=changed, calibration=profiles())
    assert result.candidates[0].estimates.latency.value is None
    changed = deployment(models={"qwen": "qwen3.5:4b"})
    result = generate(dep=changed, calibration=profiles())
    assert {candidate.model_id for candidate in result.candidates} == {"qwen"}
    assert result.candidates[0].estimates.accuracy.value is None


def test_unreviewed_quality_remains_unknown_and_planner_reports_infeasibility():
    result = generate(calibration=profiles(quality_source="format-validity-proxy"))
    assert result.candidates[0].estimates.accuracy.value is None
    assert "unknown-accuracy" in result.diagnostics[0].reasons
    request = WorkflowRequest("fixture", "ticket", NOW, "balanced", at(100), at(100),
                              "support-v1", "constraints-v1", (step(),))
    planned = plan_workflow(request, {"draft": result.candidates},
        {name: 1 for name in ("latency", "accuracy", "cost", "energy", "carbon")}, now=NOW)
    assert planned.winner is None
    assert planned.rejected_candidates[result.candidates[0].candidate_id] == ("unknown-accuracy",)


@pytest.mark.parametrize("feed", [None, trace(issued=1), trace(issued=-60)])
def test_missing_future_and_stale_feed_never_falls_back_to_historical_carbon(feed):
    result = generate(carbon_provider=feed)
    assert result.candidates[0].estimates.carbon.value is None
    assert "unknown-carbon" in result.diagnostics[0].reasons


def test_unknown_grid_is_not_inferred_from_region_or_localhost():
    dep = deployment(grid=None)
    result = generate(dep=dep)
    assert result.candidates[0].estimates.carbon.value is None
    assert result.diagnostics[0].carbon_status == "unknown-grid"


def test_materialized_utf8_size_is_recomputed_in_profile_units():
    rendered = step(prompt="Résumé café", input_tokens=1, input_size_source="provider-tokenizer")
    result = generate(rendered=rendered)
    assert result.input_size_estimate == len(rendered.prompt.encode("utf-8")) > len(rendered.prompt)
    assert result.candidates[0].estimates.latency.value == 4


def test_hard_start_and_predicted_deadline_exclusions_are_retained():
    result = generate(starts=(at(-1), at(97), at(100)))
    assert result.candidates == ()
    assert all(item.excluded for item in result.diagnostics)
    assert "predicted-finish-after-step-deadline" in result.diagnostics[1].reasons


def test_candidate_ids_and_results_are_deterministic_and_grid_order_independent():
    first = generate(starts=(at(10), NOW))
    second = generate(starts=(NOW, at(10)))
    assert first == second
    assert first.to_dict() == second.to_dict()


@pytest.mark.parametrize("starts,as_of,now", [((NOW,), at(1), NOW),
    ((NOW.replace(tzinfo=None),), NOW, NOW), ((NOW, NOW), NOW, NOW)])
def test_invalid_or_hindsight_reference_is_rejected(starts, as_of, now):
    with pytest.raises(ValueError):
        generate(starts=starts, as_of=as_of, now=now)


def test_bad_provider_sample_cannot_bypass_region_and_issue_time_checks():
    class BadFeed:
        def sample(self, region, instant, *, as_of):
            return CarbonSample("wrong-grid", NOW, NOW, at(100), 500, "test-invalid", "estimated")
    result = generate(carbon_provider=BadFeed())
    assert result.candidates[0].estimates.carbon.value is None
    assert result.diagnostics[0].carbon_status == "invalid-sample-region-or-issue-time"
