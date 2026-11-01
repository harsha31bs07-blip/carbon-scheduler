"""Actual accounting stays separate from plans and fails closed on weak evidence."""

from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import importlib
import math

import pytest

from data.carbon_sources import HistoricalCarbonTrace, StaticCarbonEstimate
from data.cost_accounting import ApiTariff, TariffBook
from data.quality_reviews import fingerprint
from shared.deployments import Deployment, DeploymentRegistry
from shared.scheduling import (CarbonSample, ExecutionAttempt, MetricEstimate, PlannedCandidate,
                               StepEstimates, StepRequest, WorkflowRequest)
from workflow.execution_accounting import (enrich_attempt, make_accounting_context,
    unknown_energy_report, validate_accounting_context, validate_accounting_record)


NOW = datetime(2026, 10, 26, tzinfo=timezone.utc)


def at(seconds):
    return NOW + timedelta(seconds=seconds)


def unknown(unit):
    return MetricEstimate(None, unit, "unknown")


@pytest.fixture
def registry():
    return DeploymentRegistry((
        Deployment("local", "ollama", "http://127.0.0.1:11434", {"model": "model:exact"},
                   grid="explicit-grid", location_source="operator-config"),
        Deployment("cloud", "nvidia", "https://api.example.com/v1", {"model": "org/model-exact"},
                   grid="explicit-grid", location_source="operator-config"),
    ))


@pytest.fixture
def workflow_request():
    step = StepRequest("classify", "classify", "A prompt", None, 32, NOW, at(60))
    return WorkflowRequest("workflow", "A prompt", NOW, "balanced", at(60), at(120),
                           "test-workload", "test-constraints", (step,))


def attempt(registry, deployment_id="local", **changes):
    planned = StepEstimates(*(MetricEstimate(value, unit, "simulated", "fixture-plan")
        for value, unit in ((99, "s"), (.99, "ratio"), (99, "USD"), (99, "kWh"), (99, "gCO2e"))))
    candidate = PlannedCandidate("candidate", "classify", "model", deployment_id, NOW, planned,
                                 region="untrusted-candidate-grid")
    defaults = dict(actual_start=at(1), actual_finish=at(2),
        executed_model_id=registry.get(deployment_id).provider_model("model"),
        executed_deployment_id=deployment_id, input_tokens=1_000_000, output_tokens=2_000_000,
        output="An output")
    defaults.update(changes)
    status = defaults.pop("status", "completed")
    return ExecutionAttempt("attempt", "workflow", candidate, status, **defaults)


def report(*, provenance="mixed", energy_wh=2.0):
    measured = provenance == "measured"
    return {"energy_wh": energy_wh, "energy_measured": measured,
        "energy_method": "CodeCarbon explicit sensor/model composition", "energy_provenance": provenance,
        "energy_components": {
            "cpu": {"value_wh": energy_wh / 2, "provenance": "measured", "source": "codecarbon:cpu:intel_rapl"},
            "gpu": {"value_wh": energy_wh / 4, "provenance": "measured" if measured else "estimated",
                    "source": "direct-meter:gpu" if measured else "codecarbon:gpu:sensor-or-power-model-unverified"},
            "ram": {"value_wh": energy_wh / 4, "provenance": "measured" if measured else "estimated",
                    "source": "direct-meter:ram" if measured else "codecarbon:ram-power-model"}},
        "energy_scope": "host-machine-including-idle-and-other-processes", "telemetry_error": None}


def context(registry, workflow_request, **changes):
    return make_accounting_context(registry, workflow_request, energy_mode="codecarbon", energy_country="IND", **changes)


def enrich(raw, registry, retained, **changes):
    args = dict(energy_report=unknown_energy_report(), measurement_start=NOW,
                journal_recorded_at=at(3), measurement_finish=at(4), accounted_at=at(5))
    args.update(changes)
    result = enrich_attempt(raw, registry, retained, **args)
    assert validate_accounting_record(result, raw, registry, retained) == result
    return result


def trace(*, issued=at(-1), until=at(20), provenance="measured", unit="gCO2e/kWh", region="explicit-grid"):
    sample = CarbonSample(region, issued, NOW, until, 400, "explicit-grid-observation", provenance, unit)
    return HistoricalCarbonTrace("retained-grid", (sample,), timedelta(seconds=100))


def rehash(value):
    value["fingerprint"] = fingerprint({key: item for key, item in value.items() if key != "fingerprint"})
    return value


def test_default_context_only_prices_exact_ollama_api_fees(registry, workflow_request):
    retained = make_accounting_context(registry, workflow_request)
    assert set(retained) == {"schema_version", "record_type", "tariff_book", "carbon",
                             "energy_mode", "energy_country", "scope"}
    book = TariffBook.from_dict(retained["tariff_book"])
    assert len(book.tariffs) == 1
    row = book.tariffs[0]
    assert row.provider == "ollama" and row.model_id == "model:exact"
    assert row.deployment_fingerprint == registry.get("local").config_fingerprint()
    assert row.verified_at == row.valid_from == workflow_request.arrived_at
    assert row.valid_until == workflow_request.completion_deadline
    assert row.scope == "api-only" and row.input_usd_per_million == row.output_usd_per_million == 0
    copy = validate_accounting_context(retained, workflow_request, registry)
    copy["tariff_book"]["tariffs"].clear()
    assert retained["tariff_book"]["tariffs"]


@pytest.mark.parametrize("settings", [dict(energy_mode="codecarbon"), dict(energy_mode="watts"),
    dict(energy_mode="codecarbon", energy_country="India"), dict(energy_country="ind")])
def test_context_rejects_implicit_or_malformed_tracker_configuration(registry, workflow_request, settings):
    with pytest.raises(ValueError):
        make_accounting_context(registry, workflow_request, **settings)


def test_context_rejects_rehashed_configuration_drift_and_extra_fields(registry, workflow_request):
    retained = context(registry, workflow_request)
    bad = deepcopy(retained)
    bad["tariff_book"]["tariffs"][0]["deployment_fingerprint"] = "a" * 64
    with pytest.raises(ValueError):
        validate_accounting_context(bad, workflow_request, registry)
    bad = {**retained, "fallback_power_watts": 100}
    with pytest.raises(ValueError):
        validate_accounting_context(bad, workflow_request, registry)
    bad = {**retained, "carbon": {"record_type": "network-provider"}}
    with pytest.raises(ValueError):
        validate_accounting_context(bad, workflow_request, registry)


def test_raw_is_immutable_quality_unknown_and_plan_metrics_are_never_observed(registry, workflow_request):
    raw, retained = attempt(registry), context(registry, workflow_request)
    before = raw.to_dict()
    result = enrich(raw, registry, retained)
    metrics = StepEstimates.from_dict(result["metrics"])
    assert raw.to_dict() == before
    assert result["attempt_sha256"] == fingerprint(before)
    assert result["context_fingerprint"] == fingerprint(retained)
    assert metrics.latency.value == 1 and metrics.latency.provenance == "measured"
    assert metrics.cost.value == 0 and "electricity-excluded" in metrics.cost.source
    assert all(item.value is None for item in (metrics.accuracy, metrics.energy, metrics.carbon))
    copy = validate_accounting_record(result, raw, registry, retained)
    copy["energy_report"]["energy_components"]["cpu"]["source"] = "changed"
    assert result["energy_report"]["energy_components"]["cpu"]["source"] is None


def test_actual_raw_latency_and_provider_elapsed_are_retained(registry, workflow_request):
    observed = StepEstimates(MetricEstimate(.25, "s", "measured", "provider-observation"),
                            unknown("ratio"), unknown("USD"), unknown("kWh"), unknown("gCO2e"))
    result = enrich(attempt(registry, observations=observed), registry, context(registry, workflow_request))
    assert StepEstimates.from_dict(result["metrics"]).latency == observed.latency
    result = enrich(attempt(registry, provider_seconds=.75), registry, context(registry, workflow_request))
    assert StepEstimates.from_dict(result["metrics"]).latency.value == .75


def test_exact_actual_token_fees_and_unknown_cloud_usage(registry, workflow_request):
    tariff = ApiTariff("nvidia", "org/model-exact", 2, 4, "operator-verified", NOW, NOW, at(120),
                       registry.get("cloud").config_fingerprint())
    retained = context(registry, workflow_request, tariff_book=TariffBook((tariff,)))
    result = enrich(attempt(registry, "cloud"), registry, retained, energy_report=report())
    metrics = StepEstimates.from_dict(result["metrics"])
    assert metrics.cost.value == 10 and metrics.energy.value is None
    failed = attempt(registry, "cloud", status="failed", output=None, input_tokens=None, output_tokens=None,
                     error="transport-failed")
    result = enrich(failed, registry, retained)
    assert StepEstimates.from_dict(result["metrics"]).cost.value is None
    free = replace(tariff, input_usd_per_million=0, output_usd_per_million=0)
    result = enrich(failed, registry, context(registry, workflow_request, tariff_book=TariffBook((free,))))
    assert StepEstimates.from_dict(result["metrics"]).cost.value == 0
    result = enrich(attempt(registry, "cloud"), registry, context(registry, workflow_request))
    assert StepEstimates.from_dict(result["metrics"]).cost.value is None


def test_confirmed_preflight_failure_has_zero_api_fee_without_fabricated_execution(registry, workflow_request):
    raw = ExecutionAttempt("preflight", "workflow", attempt(registry, "cloud").planned_candidate,
                           "failed", error="credentials-unavailable")
    result = enrich(raw, registry, context(registry, workflow_request))
    metrics = StepEstimates.from_dict(result["metrics"])
    assert metrics.cost.value == 0 and metrics.latency.value is None
    assert "confirmed-no-provider-dispatch" in metrics.cost.source
    bad = replace(raw, input_tokens=3)
    with pytest.raises(ValueError):
        enrich(bad, registry, context(registry, workflow_request))


@pytest.mark.parametrize("status", ["failed", "cancelled", "needs-reconciliation"])
def test_incurring_terminal_attempts_survive_without_current_job_state(registry, workflow_request, status):
    raw = attempt(registry, status=status, output=None, error="interrupted", retry_index=3)
    result = enrich(raw, registry, context(registry, workflow_request), energy_report=report())
    assert result["attempt_id"] == raw.attempt_id
    assert StepEstimates.from_dict(result["metrics"]).energy.value == .002


def test_mixed_host_energy_and_grid_carbon_keep_provenance_and_co2_scope(registry, workflow_request):
    retained = context(registry, workflow_request, carbon_provider=trace(unit="gCO2/kWh"))
    result = enrich(attempt(registry), registry, retained, energy_report=report())
    metrics = StepEstimates.from_dict(result["metrics"])
    assert metrics.energy.value == .002 and metrics.energy.provenance == "mixed"
    assert metrics.carbon.value == pytest.approx(.8) and metrics.carbon.unit == "gCO2"
    assert metrics.carbon.provenance == "mixed"
    assert result["carbon_sample"]["region"] == "explicit-grid"
    assert result["energy_report"]["energy_measured"] is False


@pytest.mark.parametrize("carbon,finish,accounted,expected", [
    (trace(until=at(3.5)), at(4), at(5), "carbon-interval-not-covered"),
    (trace(issued=at(6)), at(4), at(5), "carbon-not-yet-issued"),
    (trace(issued=at(-101)), at(4), at(5), "carbon-stale"),
    (trace(region="somewhere-else"), at(4), at(5), "carbon-missing-region"),
    (trace(provenance="simulated"), at(4), at(5), "simulated-carbon-disallowed"),
])
def test_whole_measurement_interval_issue_freshness_grid_and_simulation_checks(
        registry, workflow_request, carbon, finish, accounted, expected):
    result = enrich(attempt(registry), registry, context(registry, workflow_request, carbon_provider=carbon),
                    energy_report=report(), measurement_finish=finish, accounted_at=accounted)
    assert StepEstimates.from_dict(result["metrics"]).carbon.value is None
    assert result["carbon_sample"] is None and expected in result["errors"]


def test_retrospective_observations_and_half_open_interval_coverage(registry, workflow_request):
    retained = context(registry, workflow_request, carbon_provider=trace(issued=at(4.5), until=at(4)))
    result = enrich(attempt(registry), registry, retained, energy_report=report())
    assert StepEstimates.from_dict(result["metrics"]).carbon.value == pytest.approx(.8)
    assert result["carbon_sample"]["issued_at"] == "2026-10-26T00:00:04.500000Z"


def test_static_carbon_is_estimated_even_with_wholly_measured_energy(registry, workflow_request):
    static = StaticCarbonEstimate("explicit-grid", 400, "operator-estimate", NOW, NOW, at(20),
                                  timedelta(seconds=100))
    result = enrich(attempt(registry), registry, context(registry, workflow_request, carbon_provider=static),
                    energy_report=report(provenance="measured"))
    metrics = StepEstimates.from_dict(result["metrics"])
    assert metrics.energy.provenance == "measured" and metrics.carbon.provenance == "mixed"


@pytest.mark.parametrize("mutate", [
    lambda value: value.update(energy_wh=math.nan),
    lambda value: value.update(energy_wh=True),
    lambda value: value.update(energy_provenance=[]),
    lambda value: value.update(energy_measured=True),
    lambda value: value.update(energy_provenance="measured", energy_measured=True),
    lambda value: value["energy_components"]["cpu"].update(source=None),
    lambda value: value["energy_components"]["cpu"].update(value_wh=200),
    lambda value: value.pop("energy_scope"),
    lambda value: value.update(unexpected="private details"),
])
def test_malformed_or_promoted_telemetry_stays_unknown_and_self_validates(registry, workflow_request, mutate):
    value = report()
    mutate(value)
    result = enrich(attempt(registry), registry, context(registry, workflow_request), energy_report=value)
    assert result["energy_report"] == unknown_energy_report("energy-report-invalid")
    assert StepEstimates.from_dict(result["metrics"]).energy.value is None


def test_simulated_fallback_and_tracker_failure_are_safe_unknowns(registry, workflow_request):
    retained = context(registry, workflow_request)
    for change, code in (({"energy_provenance": "simulated"}, "simulated-energy-disallowed"),
            ({"energy_scope": "assumed-inference-power"}, "energy-scope-unsupported"),
            ({"telemetry_error": "tracker-stop-failed"}, "tracker-stop-failed")):
        result = enrich(attempt(registry), registry, retained, energy_report={**report(), **change})
        assert result["energy_report"] == unknown_energy_report(code)
    assert unknown_energy_report("secret traceback") == unknown_energy_report("telemetry-unavailable")
    result = enrich(attempt(registry), registry, make_accounting_context(registry, workflow_request), energy_report=report())
    assert result["energy_report"] == unknown_energy_report("energy-tracking-disabled")


def test_modeled_component_sources_cannot_be_relabelled_measured(registry, workflow_request):
    value = report(provenance="measured")
    value["energy_components"]["ram"]["source"] = "codecarbon:ram-power-model"
    result = enrich(attempt(registry), registry, context(registry, workflow_request), energy_report=value)
    assert result["energy_report"] == unknown_energy_report("energy-report-invalid")


@pytest.mark.parametrize("endpoint", ["https://remote.example", "http://192.168.1.2:11434", "http://local-host:11434"])
def test_remote_host_telemetry_cannot_account_remote_inference(registry, workflow_request, endpoint):
    remote = replace(registry.get("local"), endpoint=endpoint)
    inventory = DeploymentRegistry((remote,))
    retained = context(inventory, workflow_request, carbon_provider=trace())
    result = enrich(attempt(inventory), inventory, retained, energy_report=report())
    assert result["energy_report"] == unknown_energy_report("remote-energy-unobservable")
    metrics = StepEstimates.from_dict(result["metrics"])
    assert metrics.energy.value is None and metrics.carbon.value is None


@pytest.mark.parametrize("changes", [dict(measurement_start=at(1.5)), dict(journal_recorded_at=at(1.5)),
    dict(measurement_finish=at(2.5)), dict(accounted_at=at(3.5)), dict(measurement_start=NOW.replace(tzinfo=None))])
def test_measurement_encloses_provider_journal_and_cleanup(registry, workflow_request, changes):
    with pytest.raises(ValueError):
        enrich(attempt(registry), registry, context(registry, workflow_request), **changes)


@pytest.mark.parametrize("mutate", [
    lambda value: value["metrics"]["energy"].update(value=100),
    lambda value: value["metrics"]["accuracy"].update(value=1, provenance="measured", source="made-up"),
    lambda value: value["metrics"]["latency"].update(value=True),
    lambda value: value.update(attempt_sha256="0" * 64),
    lambda value: value.update(context_fingerprint="0" * 64),
    lambda value: value.update(errors=[]),
    lambda value: value.update(schema_version=True),
    lambda value: value.update(carbon_sample=None),
])
def test_rehashed_accounting_tampering_cannot_change_logical_metrics(registry, workflow_request, mutate):
    raw = attempt(registry, "cloud")
    retained = context(registry, workflow_request, carbon_provider=trace())
    result = enrich(raw, registry, retained)
    mutate(result)
    rehash(result)
    with pytest.raises(ValueError):
        validate_accounting_record(result, raw, registry, retained)


def test_raw_content_binding_and_execution_identity_are_strict(registry, workflow_request):
    raw, retained = attempt(registry), context(registry, workflow_request)
    result = enrich(raw, registry, retained)
    with pytest.raises(ValueError):
        validate_accounting_record(result, replace(raw, output="Other output"), registry, retained)
    with pytest.raises(ValueError):
        enrich(replace(raw, executed_model_id="alias-is-not-exact"), registry, retained)
    with pytest.raises(ValueError):
        enrich(replace(raw, actual_finish=None, status="failed", output=None), registry, retained)


def test_module_has_no_worker_or_provider_transport_dependencies():
    module = importlib.import_module("workflow.execution_accounting")
    assert all(not getattr(value, "__module__", "").startswith(("workflow.worker", "models."))
               for value in vars(module).values())
