"""Accounting failures preserve durable provider facts and never call twice."""

from dataclasses import replace
from datetime import timedelta
import json
from pathlib import Path
from unittest.mock import Mock

import pytest

from data.carbon_sources import HistoricalCarbonTrace
from shared.deployments import Deployment, DeploymentRegistry
from shared.scheduling import CarbonSample
from workflow.accounting_report import execution_report
from workflow.queue_store import QueueStore
from workflow.worker import main, submit_fixed, work_once
from workflow.workload import KnowledgeBase

from test_energy import tracker
from test_worker import Clock, NOW, TICKET, provider


@pytest.fixture
def accounted(tmp_path):
    clock, kb = Clock(), KnowledgeBase.load()
    registry = DeploymentRegistry([Deployment("edge", "ollama", "http://127.0.0.1:11434",
        {"small": "tiny:3b"}, region="test-region", grid="test-grid", location_source="offline-fixture")])
    carbon = HistoricalCarbonTrace("test-grid", (CarbonSample("test-grid", NOW - timedelta(seconds=1),
        NOW, NOW + timedelta(seconds=1000), 500., "test-grid-factor", "estimated", unit="gCO2/kWh"),),
        timedelta(seconds=1000))
    store = QueueStore(tmp_path / "queue.sqlite3")
    submit_fixed(store, registry, kb, TICKET, deployment_id="edge", model_id="small", clock=clock,
        workflow_id="ticket", summary_delay_seconds=60, carbon_provider=carbon,
        energy_mode="codecarbon", energy_country="IND")
    yield store, registry, kb, clock
    store.close()


def run(accounted, *, dispatcher=None, tracker_factory=None, **kwargs):
    store, registry, kb, clock = accounted
    return work_once(store, worker_id="test", registry=registry, kb=kb, clock=clock, heartbeat=False,
        dispatcher=dispatcher or provider(clock, []), tracker_factory=tracker_factory, **kwargs)


def test_raw_output_is_committed_before_tracker_cleanup_and_enrichment(accounted):
    store, _, _, clock = accounted
    sensor = tracker()
    def stop():
        state = store.read("ticket")
        assert state.attempts[0].output == "account_access"
        assert state.jobs[0].status == "completed" and state.accounting == ()
        clock.time += timedelta(seconds=2)
    sensor.stop.side_effect = stop
    result = run(accounted, tracker_factory=Mock(return_value=sensor))
    state = store.read("ticket")
    record = state.accounting[0]
    assert result["accounting_status"] == "recorded"
    assert state.attempts[0].observations is None  # Sidecars never mutate raw facts.
    assert record["energy_report"]["energy_provenance"] == "mixed"
    assert record["metrics"]["energy"]["value"] == pytest.approx(.0031)
    assert record["metrics"]["carbon"]["value"] == pytest.approx(1.55)
    assert record["metrics"]["carbon"]["unit"] == "gCO2"
    assert record["journal_recorded_at"] == (NOW + timedelta(seconds=1)).isoformat().replace("+00:00", "Z")
    assert record["measurement_finish"] == (NOW + timedelta(seconds=3)).isoformat().replace("+00:00", "Z")
    sensor.stop.assert_called_once_with()


@pytest.mark.parametrize("phase", ["start", "stop"])
def test_telemetry_failure_does_not_lose_or_repeat_output(accounted, phase):
    store, _, _, clock = accounted
    sensor, calls = tracker(), []
    getattr(sensor, phase).side_effect = RuntimeError("private telemetry message")
    result = run(accounted, dispatcher=provider(clock, calls), tracker_factory=Mock(return_value=sensor))
    state = store.read("ticket")
    assert len(calls) == len(state.attempts) == len(state.accounting) == 1
    assert result["accounting_status"] == "recorded"
    assert state.accounting[0]["metrics"]["energy"]["value"] is None
    assert state.accounting[0]["metrics"]["cost"]["value"] == 0
    assert "private" not in json.dumps(state.accounting)
    sensor.stop.assert_called_once_with()


def test_enrichment_failure_keeps_raw_output_and_surfaces_gap(accounted, monkeypatch):
    import workflow.execution_accounting as accounting
    store, _, _, clock = accounted
    calls = []
    monkeypatch.setattr(accounting, "enrich_attempt", Mock(side_effect=RuntimeError("optional enrichment")))
    result = run(accounted, dispatcher=provider(clock, calls), tracker_factory=Mock(return_value=tracker()))
    state = store.read("ticket")
    assert result["accounting_status"] == "enrichment-failed" and len(calls) == 1
    assert state.completed_outputs()["classify"] == "account_access" and not state.accounting
    assert execution_report(state)["resources"]["cost"]["total"] is None


def test_accounting_write_can_be_reconciled_offline_without_inference(accounted, monkeypatch, capsys):
    store, _, _, clock = accounted
    calls = []
    with monkeypatch.context() as patch:
        patch.setattr(store, "record_accounting", Mock(side_effect=OSError("disk unavailable")))
        result = run(accounted, dispatcher=provider(clock, calls), tracker_factory=Mock(return_value=tracker()))
    assert result["accounting_status"] == "needs-reconciliation"
    assert store.read("ticket").completed_outputs()["classify"] == "account_access"
    path = Path(result["orphan_accounting"])
    assert path.is_file()
    assert main(["--db", str(store.path), "reconcile-accounting", str(path)]) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "accounting-reconciled"
    assert len(calls) == len(store.read("ticket").accounting) == 1
    assert main(["--db", str(store.path), "reconcile-accounting", str(path)]) == 0  # Idempotent.


def test_raw_journal_failure_retains_raw_and_accounting_orphans(accounted, monkeypatch):
    store, _, _, _ = accounted
    sensor = tracker()
    monkeypatch.setattr(store, "complete", Mock(side_effect=OSError("disk unavailable")))
    result = run(accounted, tracker_factory=Mock(return_value=sensor))
    assert result["status"] == "needs-reconciliation" and result["accounting_status"] == "needs-reconciliation"
    assert json.loads(Path(result["orphan_attempt"]).read_text())["attempt"]["output"] == "account_access"
    assert json.loads(Path(result["orphan_accounting"]).read_text())["accounting"]["metrics"]["energy"]["value"] > 0
    assert not store.read("ticket").attempts and not store.read("ticket").accounting
    sensor.stop.assert_called_once_with()


def test_unexpected_provider_crash_stops_tracker_and_marks_resource_gap(accounted):
    store, _, _, _ = accounted
    sensor = tracker()
    def crash(*args, **kwargs):
        raise RuntimeError("provider accepted then connection lost")
    with pytest.raises(RuntimeError):
        run(accounted, dispatcher=crash, tracker_factory=Mock(return_value=sensor))
    sensor.stop.assert_called_once_with()
    report = execution_report(store.read("ticket"))
    assert report["coverage"]["unrepresented_retry_indices"] == [{"step_id": "classify", "retry_index": 0}]
    assert report["resources"]["cost"]["total"] is None


def test_cancellation_and_superseded_attempts_keep_usage(accounted):
    store, _, _, clock = accounted
    calls = []
    def supersede(_):
        clock.time += timedelta(seconds=6)
        store.recover(now=clock.now())
        store.retry("ticket", "classify", now=clock.now(), acknowledge_ambiguous=True)
    result = run(accounted, dispatcher=provider(clock, calls, callback=supersede),
        tracker_factory=Mock(return_value=tracker()), lease_seconds=5)
    assert result["status"] == "late-attempt-archived" and result["accounting_status"] == "recorded"
    result = run(accounted, dispatcher=provider(clock, calls,
        callback=lambda _: store.cancel("ticket", now=clock.now())), tracker_factory=Mock(return_value=tracker()))
    report = execution_report(store.read("ticket"))
    assert result["status"] == "cancelled" and len(calls) == 2
    assert report["resources"]["energy"]["total"] == pytest.approx(.0062)
    assert report["coverage"]["accounted_attempts"] == 2
    assert run(accounted)["status"] == "idle"


def test_energy_disabled_by_default_and_remote_tracker_never_started(tmp_path):
    clock, kb = Clock(), KnowledgeBase.load()
    for endpoint, mode, country in (("http://127.0.0.1:11434", "none", None),
                                     ("http://edge:11434", "codecarbon", "IND")):
        registry = DeploymentRegistry([Deployment("edge", "ollama", endpoint, {"small": "tiny:3b"})])
        store = QueueStore(tmp_path / (mode + ".sqlite3"))
        try:
            submit_fixed(store, registry, kb, TICKET, deployment_id="edge", model_id="small",
                clock=clock, workflow_id="ticket", energy_mode=mode, energy_country=country)
            factory = Mock(side_effect=AssertionError("coordinator tracking forbidden"))
            work_once(store, worker_id="test", clock=clock, dispatcher=provider(clock, []), heartbeat=False,
                      tracker_factory=factory)
            factory.assert_not_called()
            assert store.read("ticket").accounting[0]["metrics"]["energy"]["value"] is None
        finally:
            store.close()


def test_report_uses_critical_path_and_disjoint_delay_fields(accounted, capsys):
    store, _, _, clock = accounted
    for _ in range(3):
        run(accounted, tracker_factory=Mock(return_value=tracker()))
    clock.time = NOW + timedelta(seconds=60)
    run(accounted, tracker_factory=Mock(return_value=tracker()))
    report = execution_report(store.read("ticket"))
    assert report["timing"]["response_seconds"] == 3 and report["timing"]["completion_seconds"] == 61
    assert report["timing"]["response_deadline_met"] and report["timing"]["completion_deadline_met"]
    assert report["attempts"][-1]["deliberate_delay_seconds"] == 57
    assert report["attempts"][-1]["queue_and_preparation_wait_seconds"] == 0
    assert report["resources"]["energy"]["total"] == pytest.approx(.0124)
    assert report["resources"]["carbon"]["unit"] == "gCO2"
    assert report["quality"]["value"] is None
    assert main(["--db", str(store.path), "report", "ticket"]) == 0
    assert json.loads(capsys.readouterr().out) == report


def test_mixed_carbon_units_and_missing_accounting_cannot_produce_total(accounted):
    store, _, _, _ = accounted
    run(accounted, tracker_factory=Mock(return_value=tracker()))
    run(accounted, tracker_factory=Mock(return_value=tracker()))
    snapshot = store.read("ticket")
    # Aggregation receives already validated snapshots; construct this fixture
    # directly to exercise independent unit pooling and missing-data behavior.
    records = json.loads(json.dumps(snapshot.accounting))
    records[1]["metrics"]["carbon"]["unit"] = "gCO2e"
    report = execution_report(replace(snapshot, accounting=tuple(records)))
    assert report["resources"]["carbon"]["total"] is None
    assert set(report["resources"]["carbon"]["known_subtotals"]) == {"gCO2", "gCO2e"}
    report = execution_report(replace(snapshot, accounting=(snapshot.accounting[0],)))
    assert report["resources"]["energy"]["total"] is None
    assert report["resources"]["energy"]["known_subtotals"]["kWh"] == pytest.approx(.0031)
    assert report["coverage"]["missing_attempt_ids"] == [snapshot.attempts[1].attempt_id]


def test_returned_timeout_keeps_observed_energy_but_cannot_claim_full_coverage(accounted):
    store, _, _, clock = accounted
    dispatch = provider(clock, [])
    def timeout(*args, **kwargs):
        return replace(dispatch(*args, **kwargs), status="failed", error="request-timeout",
                       output=None, input_tokens=None, output_tokens=None, completion_reason=None)
    run(accounted, dispatcher=timeout, tracker_factory=Mock(return_value=tracker()))
    state = store.read("ticket")
    report = execution_report(state)
    assert report["coverage"]["uncertain_provider_attempt_ids"] == [state.attempts[0].attempt_id]
    assert report["resources"]["energy"]["total"] is None
    assert report["resources"]["energy"]["known_subtotals"]["kWh"] == pytest.approx(.0031)
    assert report["resources"]["carbon"]["total"] is None
    assert report["resources"]["cost"]["total"] == 0  # Exact zero API tariff remains valid.


def test_confirmed_undispatched_retry_does_not_imply_missing_provider_usage(accounted):
    store, _, _, clock = accounted
    claim = store.claim("old-worker", now=clock.now(), lease_seconds=60)
    store.fail_before_dispatch(claim, "worker-input-configuration-invalid", now=clock.now())
    store.retry("ticket", "classify", now=clock.now())
    run(accounted, tracker_factory=Mock(return_value=tracker()))
    report = execution_report(store.read("ticket"))
    assert report["coverage"]["confirmed_undispatched_retries"] == [{"step_id": "classify", "retry_index": 0}]
    assert not report["coverage"]["unrepresented_retry_indices"]
    assert report["resources"]["cost"]["total"] == 0


def test_overlap_preserves_window_subtotals_and_refuses_complete_host_total(accounted):
    store, _, _, _ = accounted
    for _ in range(2):
        run(accounted, tracker_factory=Mock(return_value=tracker()))
    snapshot = store.read("ticket")
    records = json.loads(json.dumps(snapshot.accounting))
    records[1]["measurement_start"] = records[0]["measurement_start"]
    report = execution_report(replace(snapshot, accounting=tuple(records)))
    assert report["coverage"]["overlapping_host_windows"]
    assert report["resources"]["energy"]["total"] is None
    assert report["resources"]["energy"]["known_subtotals"]["kWh"] == pytest.approx(.0062)


def test_tracker_report_error_still_records_known_api_cost(accounted, monkeypatch):
    import workflow.worker as worker
    store, _, _, _ = accounted
    monkeypatch.setattr(worker.EnergySession, "report", Mock(side_effect=RuntimeError("device detail")))
    result = run(accounted, tracker_factory=Mock(return_value=tracker()))
    record = store.read("ticket").accounting[0]
    assert result["accounting_status"] == "recorded"
    assert record["metrics"]["cost"]["value"] == 0
    assert record["metrics"]["energy"]["value"] is None
    assert "device detail" not in json.dumps(record)
