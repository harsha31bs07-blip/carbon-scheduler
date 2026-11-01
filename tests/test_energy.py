"""Energy telemetry cannot change inference or promote modeled components."""

from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from shared.energy import EnergySession, aggregate_energy_measured, is_loopback_endpoint


def tracker(*, cpu_mode="intel_rapl"):
    cpu = type("CPU", (), {"_mode": cpu_mode})()
    return SimpleNamespace(
        start=Mock(), stop=Mock(), _hardware=[cpu],
        _total_energy=SimpleNamespace(kWh=.0031),
        _total_cpu_energy=SimpleNamespace(kWh=.001),
        _total_gpu_energy=SimpleNamespace(kWh=.002),
        _total_ram_energy=SimpleNamespace(kWh=.0001),
    )


@pytest.mark.parametrize("endpoint", [
    "http://localhost:11434", "http://127.0.0.1:11434", "http://127.0.0.8",
    "https://[::1]:11434/api/generate",
])
def test_literal_loopback_can_include_ollama_process(endpoint):
    assert is_loopback_endpoint(endpoint)


@pytest.mark.parametrize("endpoint", [
    "http://worker.example", "http://192.168.0.2:11434", "http://localhost.example",
    "http://user:secret@localhost", "file:///localhost", "http://[bad", None,
])
def test_other_hosts_cannot_measure_inference_energy_at_coordinator(endpoint):
    factory = Mock(side_effect=AssertionError("must not start coordinator tracker"))
    with EnergySession(endpoint, tracker_factory=factory, fallback_power_watts=48) as session:
        pass
    report = session.report(1000)
    assert report["energy_wh"] is None
    assert report["energy_provenance"] == "unknown"
    factory.assert_not_called()


def test_cpu_sensor_and_modeled_ram_make_total_mixed():
    sensor = tracker()
    factory = Mock(return_value=sensor)
    with EnergySession("http://localhost", tracker_factory=factory) as session:
        pass
    report = session.report(1000)
    assert report["energy_wh"] == pytest.approx(3.1)
    assert report["energy_measured"] is False
    assert report["energy_provenance"] == "mixed"
    assert report["energy_components"]["cpu"] == {
        "value_wh": 1, "provenance": "measured", "source": "codecarbon:cpu:intel_rapl"}
    assert report["energy_components"]["gpu"]["provenance"] == "estimated"
    assert report["energy_components"]["ram"]["provenance"] == "estimated"
    assert report["energy_scope"] == "host-machine-including-idle-and-other-processes"
    factory.assert_called_once_with(tracking_mode="machine", measure_power_secs=1,
                                    save_to_file=False, save_to_api=False, log_level="error", pue=1.0)
    sensor.stop.assert_called_once_with()


@pytest.mark.parametrize("mode", ["constant", "cpu_load", "sensor_future_unknown"])
def test_cpu_model_or_unknown_backend_never_claims_sensor_measurement(mode):
    with EnergySession("http://localhost", tracker_factory=Mock(return_value=tracker(cpu_mode=mode))) as session:
        pass
    report = session.report(10)
    assert report["energy_components"]["cpu"]["provenance"] == "estimated"
    assert report["energy_provenance"] == "estimated"
    assert report["energy_measured"] is False


@pytest.mark.parametrize("phase", ["start", "stop"])
def test_failed_telemetry_preserves_original_call_and_stops_partial_tracker(phase):
    sensor = tracker()
    getattr(sensor, phase).side_effect = RuntimeError("sensitive device detail")
    inference = Mock(return_value="answer")
    with EnergySession("http://localhost", tracker_factory=Mock(return_value=sensor),
                       fallback_power_watts=48) as session:
        result = inference()
    report = session.report(1000)
    assert result == "answer"
    inference.assert_called_once_with()
    sensor.stop.assert_called_once_with()
    assert report["energy_wh"] == pytest.approx(48 / 3600)
    assert report["energy_provenance"] == "estimated"
    assert report["telemetry_error"] == f"tracker-{phase}-failed"
    assert "sensitive" not in str(report)


def test_tracker_stop_error_does_not_replace_primary_inference_failure():
    sensor = tracker()
    sensor.stop.side_effect = RuntimeError("telemetry failure")
    error = TimeoutError("primary provider failure")
    session = EnergySession("http://localhost", tracker_factory=Mock(return_value=sensor))
    with pytest.raises(TimeoutError) as raised:
        with session:
            raise error
    assert raised.value is error
    assert session.report(100)["energy_wh"] is None
    sensor.stop.assert_called_once_with()


def test_interrupted_tracker_start_releases_partial_tracker_and_lock():
    interrupted = tracker()
    interrupted.start.side_effect = KeyboardInterrupt()
    with pytest.raises(KeyboardInterrupt):
        with EnergySession("http://localhost", tracker_factory=Mock(return_value=interrupted)):
            pytest.fail("inference must not start after cancellation")
    interrupted.stop.assert_called_once_with()
    resumed = tracker()
    with EnergySession("http://localhost", tracker_factory=Mock(return_value=resumed)):
        pass
    resumed.start.assert_called_once_with()


def test_overflowed_power_assumption_remains_json_safe_unknown():
    import json

    with EnergySession("http://localhost", fallback_power_watts=1e308) as session:
        pass
    report = session.report(1e308)
    assert report["energy_wh"] is None
    assert report["telemetry_error"] == "fallback-overflow"
    json.dumps(report, allow_nan=False)


def test_nested_session_declines_overlapping_tracker_and_releases_lock():
    outer = tracker()
    skipped = Mock(side_effect=AssertionError("nested tracker must not start"))
    with EnergySession("http://localhost", tracker_factory=Mock(return_value=outer)):
        with EnergySession("http://localhost", tracker_factory=skipped) as inner:
            pass
    assert inner.report(100)["telemetry_error"] == "another-host-tracker-active"
    skipped.assert_not_called()
    resumed = tracker()
    with EnergySession("http://localhost", tracker_factory=Mock(return_value=resumed)):
        pass
    resumed.start.assert_called_once_with()


@pytest.mark.parametrize("reading", [None, -1, float("nan"), float("inf"), True])
def test_invalid_tracker_total_is_unknown_without_explicit_fallback(reading):
    sensor = tracker()
    sensor._total_energy.kWh = reading
    with EnergySession("http://localhost", tracker_factory=Mock(return_value=sensor)) as session:
        pass
    report = session.report(1000)
    assert report["energy_wh"] is None
    assert report["energy_provenance"] == "unknown"
    assert report["telemetry_error"] == "tracker-reading-unavailable"


def test_aggregate_requires_all_trials_and_all_components_measured():
    measured = {"energy_wh": 1, "energy_measured": True, "energy_provenance": "measured"}
    estimated = {"energy_wh": 2, "energy_measured": False, "energy_provenance": "estimated"}
    assert aggregate_energy_measured([measured, dict(measured)]) is True
    assert aggregate_energy_measured([measured, estimated]) is False
    assert aggregate_energy_measured([]) is False
    assert aggregate_energy_measured([{**measured, "energy_wh": None}]) is False
    assert aggregate_energy_measured([{**measured, "energy_wh": float("nan")}]) is False
    assert aggregate_energy_measured([{**measured, "energy_provenance": "mixed"}]) is False
    assert aggregate_energy_measured([{**measured, "energy_components": {
        "ram": {"value_wh": 0, "provenance": "estimated"}}}]) is False
