"""Accounting failures preserve completed local inference and honest metrics."""

from datetime import datetime, timedelta, timezone
import json

import pytest

from data.carbon_sources import CarbonAvailability, HistoricalCarbonTrace
from shared.deployments import Deployment, DeploymentRegistry
from shared.scheduling import CarbonSample
from workflow import calibrate


NOW = datetime(2026, 10, 19, 8, tzinfo=timezone.utc)
GRID = "IN-national-grid"


def carbon_trace(*, value=675, unit="gCO2/kWh", provenance="estimated"):
    sample = CarbonSample(GRID, NOW, NOW, NOW + timedelta(days=1), value,
                          "explicit-test-intensity", provenance, unit)
    return HistoricalCarbonTrace("test-trace", (sample,), timedelta(days=1))


def run_accounting(tmp_path, monkeypatch, *, carbon_provider, energy_wh=2):
    case_path = tmp_path / "cases.json"
    case_path.write_text(json.dumps({"schema_version": 1, "tickets": [{
        "id": "cal-account", "split": "calibration", "expected_category": "account_access",
        "ticket": "My account is locked after failed login attempts.",
    }]}), encoding="utf-8")
    registry = DeploymentRegistry([Deployment(
        "local", "ollama", "http://localhost:11434", {"small": "llama:exact"},
        region="IN-KA", grid=GRID, location_source="explicit-test-location",
    )])
    calls = []
    instant = NOW

    def now():
        nonlocal instant
        instant += timedelta(seconds=1)
        return instant

    class KnownHostEnergy:
        def __init__(self, endpoint, *, tracker_factory=None):
            assert endpoint == "http://localhost:11434"

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def report(self, duration_ms):
            return {"energy_wh": energy_wh, "energy_provenance": "estimated",
                    "energy_method": "test-host-power-model", "energy_measured": False,
                    "energy_scope": "host-machine-including-idle-and-other-processes"}

    def dispatch(deployment, model, prompt, **controls):
        calls.append((deployment.deployment_id, model, prompt, controls))
        return {"text": "account_access", "latency_ms": 100,
                "completion_reason": "stop", "input_tokens": 29, "output_tokens": 3,
                "provider_model_id": "llama:exact", "reported_model_id": "llama:exact"}

    monkeypatch.setattr(calibrate, "_now", now)
    monkeypatch.setattr(calibrate, "STEP_ORDER", ("classify",))
    monkeypatch.setattr(calibrate, "EnergySession", KnownHostEnergy)
    monkeypatch.setattr(calibrate, "hardware_snapshot", lambda: {"source": "test-capability"})
    monkeypatch.setattr(calibrate, "code_revision", lambda: "test-source-revision")
    result = calibrate.run_calibration(
        registry, directory=tmp_path / "out", deployment_id="local", models=["small"], trials=1,
        cases_path=case_path, carbon_provider=carbon_provider, dispatcher=dispatch,
        residency_probe=lambda *_: True, progress=False,
    )
    records = calibrate.load_trials(tmp_path / "out")
    assert len(calls) == len(records) == 2  # Warm-up plus one included trial.
    return result, records, calls


def test_carbon_source_failure_preserves_completed_raw_result_usage_and_cost(tmp_path, monkeypatch):
    records_seen_before_enrichment = []

    class BrokenSource:
        def sample(self, *args, **kwargs):
            # Read durable evidence at the failure boundary, before the runner's
            # final accounting write. Assertions live below so the runner cannot
            # accidentally swallow a failed assertion as a provider exception.
            saved = calibrate.load_trials(tmp_path / "out")
            records_seen_before_enrichment.append(saved)
            raise RuntimeError("provider-private-error")

    result, records, calls = run_accounting(tmp_path, monkeypatch, carbon_provider=BrokenSource())
    assert result["manifest"]["status"] == "completed"
    for record in records:
        assert record["status"] == "completed" and record["output_valid"]
        assert record["result"]["text"] == "account_access"
        assert record["input_tokens"] == 29 and record["output_tokens"] == 3
        assert record["cost_usd"] == 0 and "api-only" in record["cost_source"]
        assert record["carbon_g"] is None and record["carbon_provenance"] == "unknown"
        assert record["carbon_status"] == "source-failed"
        assert record["enrichment_errors"] == ["carbon-source-failed"]
        assert "provider-private-error" not in json.dumps(record)
    assert all(call[3]["max_attempts"] == 1 for call in calls)
    assert len(records_seen_before_enrichment) == 2
    assert all(record["status"] == "completed" and record["result"]["text"] == "account_access"
               and record["input_tokens"] == 29 and record["output_tokens"] == 3
               for snapshot in records_seen_before_enrichment for record in snapshot)


def test_simulated_intensity_never_trains_empirical_carbon_profile(tmp_path, monkeypatch):
    result, records, _ = run_accounting(tmp_path, monkeypatch,
                                        carbon_provider=carbon_trace(provenance="simulated"))
    for record in records:
        assert record["carbon_g"] == pytest.approx(.002 * 675)
        assert record["carbon_provenance"] == "simulated"
        assert record["carbon_sample"]["provenance"] == "simulated"
    profile = result["profiles"]["profiles"][0]
    assert profile["metrics"]["carbon"]["count"] == 0
    assert profile["metrics"]["carbon"]["provenance"] == "unknown"
    assert profile["metrics"]["energy"]["count"] == 1


@pytest.mark.parametrize("unit, expected", [("gCO2/kWh", "gCO2"), ("gCO2e/kWh", "gCO2e")])
def test_carbon_gas_scope_retained_from_sample_through_profile(tmp_path, monkeypatch, unit, expected):
    result, records, _ = run_accounting(tmp_path, monkeypatch, carbon_provider=carbon_trace(unit=unit))
    for record in records:
        assert record["carbon_g"] == pytest.approx(1.35)
        assert record["carbon_unit"] == expected
        assert record["carbon_sample"]["unit"] == unit
        assert record["carbon_provenance"] == "estimated"
        assert record["carbon_source"] == "energy-times-explicit-test-intensity"
    assert result["profiles"]["profiles"][0]["metrics"]["carbon"]["unit"] == expected


def test_carbon_overflow_becomes_unknown_without_losing_trial(tmp_path, monkeypatch):
    result, records, _ = run_accounting(tmp_path, monkeypatch,
                                        carbon_provider=carbon_trace(value=1e308), energy_wh=1e308)
    assert result["manifest"]["status"] == "completed"
    for record in records:
        assert record["status"] == "completed" and record["result"]["text"] == "account_access"
        assert record["carbon_g"] is None and record["carbon_provenance"] == "unknown"
        assert record["carbon_status"] == "carbon-calculation-overflow"
    assert result["profiles"]["profiles"][0]["metrics"]["carbon"]["count"] == 0


def test_sample_covering_start_but_not_full_call_cannot_establish_carbon(tmp_path, monkeypatch):
    class BriefCoverage:
        def sample(self, region, instant, *, as_of):
            assert region == GRID and as_of == instant
            return CarbonSample(region, NOW, instant, instant + timedelta(milliseconds=500),
                                675, "brief-interval", "estimated", "gCO2/kWh")

        def availability(self, region, instant, *, as_of):
            return CarbonAvailability("available", self.sample(region, instant, as_of=as_of))

    result, records, _ = run_accounting(tmp_path, monkeypatch, carbon_provider=BriefCoverage())
    for record in records:
        assert record["status"] == "completed" and record["energy_kwh"] == .002
        assert record["carbon_g"] is None and record["carbon_provenance"] == "unknown"
        assert record["carbon_status"] == "execution-outside-sample-coverage"
        assert record["carbon_unit"] == "gCO2"
    assert result["profiles"]["profiles"][0]["metrics"]["carbon"]["count"] == 0
