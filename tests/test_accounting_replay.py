"""Offline replay preserves raw attempts and independently bound accounting."""

from dataclasses import replace
from datetime import timedelta
import json
import subprocess
import sys

import pytest

from data.quality_reviews import fingerprint
from shared.deployments import Deployment, DeploymentRegistry
from shared.scheduling import ExecutionAttempt, MetricEstimate, PlannedCandidate, StepEstimates, StepRequest, WorkflowRequest
from workflow.execution_accounting import enrich_attempt, make_accounting_context, unknown_energy_report
from workflow.queue_replay import export_workflow, load_bundle, main
from workflow.queue_store import QueueStore

from test_queue_replay import NOW, complete_first, rewrite_payload
from test_planned_replay import planned_inputs
from test_staged_planning import inputs


@pytest.fixture
def accounting_queue(tmp_path):
    unknown = lambda unit: MetricEstimate(None, unit, "unknown")
    estimates = StepEstimates(*(unknown(unit) for unit in ("s", "ratio", "USD", "kWh", "gCO2e")))
    steps = tuple(StepRequest(name, name, "Template " + name, None, 128, NOW, NOW + timedelta(minutes=5),
        dependencies=(() if index == 0 else ("first",)), required_for_response=index == 0,
        enable_thinking=False) for index, name in enumerate(("first", "second")))
    request = WorkflowRequest("workflow-a", "Accounting replay ticket", NOW, "fixed", NOW + timedelta(minutes=5),
        NOW + timedelta(minutes=10), "fixture-v1", "constraints-v1", steps)
    registry = DeploymentRegistry((Deployment("edge", "ollama", "http://localhost:11434", {"small": "exact:tag"}),))
    plans = tuple(PlannedCandidate("candidate-" + step.step_id, step.step_id, "small", "edge", NOW,
        estimates) for step in steps)
    context = make_accounting_context(registry, request)
    store = QueueStore(tmp_path / "accounting.sqlite3")
    store.enqueue(request, plans, configuration={"registry_snapshot": registry.to_dict(),
        "registry_fingerprint": registry.config_fingerprint(), "experiment_mode": "live",
        "accounting_version": "execution-accounting.v1"}, accounting_context=context)
    yield store, request, registry, context
    store.close()


def account(accounting_queue, attempt, *, journal_seconds=1):
    store, request, registry, context = accounting_queue
    journal = NOW + timedelta(seconds=journal_seconds)
    record = enrich_attempt(attempt, registry, context, energy_report=unknown_energy_report(),
        measurement_start=attempt.actual_start or NOW, measurement_finish=journal,
        journal_recorded_at=journal, accounted_at=journal)
    store.record_accounting(request.workflow_id, record, now=journal)
    return record


def export(accounting_queue, tmp_path):
    store, request, *_ = accounting_queue
    return export_workflow(store.path, request.workflow_id, tmp_path / "bundle")


def rehash(record):
    record["fingerprint"] = fingerprint({key: value for key, value in record.items() if key != "fingerprint"})


def test_fixed_accounting_bundle_three_retains_untouched_raw_attempt(accounting_queue, tmp_path):
    store, request, _, context = accounting_queue
    _, raw = complete_first(store)
    record = account(accounting_queue, raw)
    bundle = export(accounting_queue, tmp_path)
    payload = json.loads((bundle / "workflow.json").read_text())
    assert payload["bundle_version"] == 3
    assert json.loads((bundle / "manifest.json").read_text())["bundle_version"] == 3
    assert payload["planning_context"] is None and payload["decisions"] == []
    assert payload["attempts"] == [raw.to_dict()]
    replayed = load_bundle(bundle)
    assert replayed == store.read(request.workflow_id)
    assert replayed.accounting_context == context and replayed.accounting == (record,)
    assert replayed.attempts[0].observations == raw.observations
    assert record["metrics"]["accuracy"]["value"] is None


def test_gap_is_retained_and_cli_reports_it_without_inventing_totals(accounting_queue, tmp_path, capsys):
    _, raw = complete_first(accounting_queue[0])
    bundle = export(accounting_queue, tmp_path)
    assert load_bundle(bundle).accounting == ()
    assert main(["inspect", "--bundle", str(bundle)]) == 0
    summary = json.loads(capsys.readouterr().out)
    assert summary["accounted_attempt_count"] == 0
    assert summary["unaccounted_attempt_ids"] == [raw.attempt_id]
    assert summary["unrecorded_dispatched_step_ids"] == []
    assert "energy_kwh" not in summary


def test_ambiguous_dispatch_is_visible_without_a_fabricated_attempt(accounting_queue, tmp_path, capsys):
    store, request, *_ = accounting_queue
    claim = store.claim("worker", now=NOW, lease_seconds=1)
    store.mark_running(claim, now=NOW)
    store.recover(now=NOW + timedelta(seconds=1))
    bundle = export(accounting_queue, tmp_path)
    assert load_bundle(bundle).status == "needs-reconciliation"
    assert main(["inspect", "--bundle", str(bundle)]) == 0
    summary = json.loads(capsys.readouterr().out)
    assert summary["attempt_count"] == summary["accounted_attempt_count"] == 0
    assert summary["unrecorded_dispatched_step_ids"] == ["first"]


@pytest.mark.parametrize("mutate", [
    lambda value: value.update(accounting_context=None),
    lambda value: value.update(accounting={}),
    lambda value: value["accounting_context"].update(extra="unsupported"),
    lambda value: value["accounting"][0].update(attempt_id="unknown"),
    lambda value: value["accounting"].append(value["accounting"][0]),
    lambda value: value["accounting"][0].update(attempt_sha256="0" * 64),
    lambda value: value["accounting"][0].update(context_fingerprint="0" * 64),
    lambda value: value["accounting"][0].update(workflow_id="other"),
    lambda value: value["configuration"].update(accounting_version="unknown"),
    lambda value: value["configuration"].pop("accounting_version"),
])
def test_rehashed_accounting_identity_shape_and_context_tampering_fails(accounting_queue, tmp_path, mutate):
    _, raw = complete_first(accounting_queue[0])
    account(accounting_queue, raw)
    bundle = export(accounting_queue, tmp_path)
    rewrite_payload(bundle, mutate)
    with pytest.raises(ValueError):
        load_bundle(bundle)


@pytest.mark.parametrize("metric,value", [("cost", 100.), ("energy", 1.), ("carbon", 1.), ("accuracy", 1.)])
def test_forged_observation_rejected_even_with_valid_record_and_file_hashes(accounting_queue, tmp_path, metric, value):
    _, raw = complete_first(accounting_queue[0])
    account(accounting_queue, raw)
    bundle = export(accounting_queue, tmp_path)
    def mutate(payload):
        record = payload["accounting"][0]
        record["metrics"][metric].update(value=value, provenance="measured", source="forged")
        rehash(record)
    rewrite_payload(bundle, mutate)
    with pytest.raises(ValueError):
        load_bundle(bundle)


def test_rehashed_raw_usage_cannot_retain_old_accounting(accounting_queue, tmp_path):
    _, raw = complete_first(accounting_queue[0])
    account(accounting_queue, raw)
    bundle = export(accounting_queue, tmp_path)
    rewrite_payload(bundle, lambda value: value["attempts"][0].update(input_tokens=999))
    with pytest.raises(ValueError):
        load_bundle(bundle)


def test_journal_cannot_predate_persisted_job_outcome(accounting_queue, tmp_path):
    store = accounting_queue[0]
    _, raw = complete_first(store)
    account(accounting_queue, raw)
    store.connection.execute("UPDATE jobs SET finished_at=? WHERE step_id='first'",
                             ((NOW + timedelta(seconds=2)).isoformat(),))
    with pytest.raises(ValueError, match="journal"):
        export(accounting_queue, tmp_path)


def test_bundle_downgrade_cannot_strip_configured_accounting(accounting_queue, tmp_path):
    bundle = export(accounting_queue, tmp_path)
    def strip(value):
        value.update(bundle_version=1)
        for name in ("planning_context", "decisions", "accounting_context", "accounting"):
            value.pop(name)
    rewrite_payload(bundle, strip)
    manifest_path = bundle / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["bundle_version"] = 1
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(ValueError, match="strip"):
        load_bundle(bundle)


def test_multiple_accounting_records_follow_raw_attempt_order(accounting_queue, tmp_path):
    store, request, *_ = accounting_queue
    _, first = complete_first(store)
    account(accounting_queue, first)
    claim = store.claim("worker", now=NOW + timedelta(seconds=1), lease_seconds=60)
    store.mark_running(claim, now=NOW + timedelta(seconds=1))
    second = replace(first, attempt_id="attempt-second", planned_candidate=claim.candidate,
        actual_start=NOW + timedelta(seconds=1), actual_finish=NOW + timedelta(seconds=2), output="Second output")
    store.complete(claim, second, now=NOW + timedelta(seconds=2))
    account(accounting_queue, second, journal_seconds=2)
    bundle = export(accounting_queue, tmp_path)
    assert len(load_bundle(bundle).accounting) == 2
    rewrite_payload(bundle, lambda value: value["accounting"].reverse())
    with pytest.raises(ValueError, match="order"):
        load_bundle(bundle)


def test_superseded_late_attempt_keeps_accounting_without_supplying_retry_output(accounting_queue, tmp_path):
    store, request, *_ = accounting_queue
    old = store.claim("old-worker", now=NOW, lease_seconds=2)
    store.mark_running(old, now=NOW)
    store.recover(now=NOW + timedelta(seconds=2))
    store.retry(request.workflow_id, "first", now=NOW + timedelta(seconds=2), acknowledge_ambiguous=True)
    current = store.claim("new-worker", now=NOW + timedelta(seconds=2), lease_seconds=60)
    store.mark_running(current, now=NOW + timedelta(seconds=2))
    late = ExecutionAttempt("late-first", request.workflow_id, old.candidate, "completed", actual_start=NOW,
        actual_finish=NOW + timedelta(seconds=3), executed_model_id="exact:tag", executed_deployment_id="edge",
        output="Superseded output", input_tokens=13, output_tokens=5, output_limit_tokens=128,
        enable_thinking=False, completion_reason="stop")
    store.archive_late_attempt(old, late, now=NOW + timedelta(seconds=3))
    record = account(accounting_queue, late, journal_seconds=3)
    replayed = load_bundle(export(accounting_queue, tmp_path))
    assert replayed.status == "running" and replayed.jobs[0].output is None
    assert replayed.attempts == (late,) and replayed.accounting == (record,)


def test_planned_accounting_version_three_preserves_full_decision_history(planned_inputs, tmp_path):
    arguments, planning, configuration, prepared = planned_inputs
    registry, request = arguments["registry"], arguments["request"]
    context = make_accounting_context(registry, request, carbon_provider=arguments["carbon_provider"])
    store = QueueStore(tmp_path / "planned-accounting.sqlite3")
    try:
        store.enqueue_planned(request, tuple(item.candidate for item in prepared.result.winner.assignments),
            configuration={**configuration, "accounting_version": "execution-accounting.v1"},
            planning_context=planning, decision=prepared.report, now=request.arrived_at, accounting_context=context)
        bundle = export_workflow(store.path, request.workflow_id, tmp_path / "planned-bundle")
        payload = json.loads((bundle / "workflow.json").read_text())
        assert payload["bundle_version"] == 3
        replayed = load_bundle(bundle)
        assert replayed == store.read(request.workflow_id)
        assert replayed.planning_context == planning and replayed.decisions[0]["report"] == prepared.report
        assert replayed.accounting_context == context and replayed.accounting == ()
        def strip(value):
            value.update(bundle_version=2)
            value.pop("accounting_context")
            value.pop("accounting")
        rewrite_payload(bundle, strip)
        manifest_path = bundle / "manifest.json"
        manifest = json.loads(manifest_path.read_text())
        manifest["bundle_version"] = 2
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        with pytest.raises(ValueError, match="strip"):
            load_bundle(bundle)
    finally:
        store.close()


@pytest.mark.parametrize("command", ["inspect", "report"])
def test_accounting_cli_imports_no_transports_and_never_waits(accounting_queue, tmp_path, command):
    _, raw = complete_first(accounting_queue[0])
    account(accounting_queue, raw)
    bundle = export(accounting_queue, tmp_path)
    script = """
import builtins, socket, sys, time
original_import = builtins.__import__
def guarded(name, *args, **kwargs):
    if name in {'workflow.worker', 'workflow.planned_worker', 'workflow.deployment_clients',
                'workflow.model_clients', 'requests', 'codecarbon'}:
        raise AssertionError('Forbidden replay import: ' + name)
    return original_import(name, *args, **kwargs)
builtins.__import__ = guarded
def forbidden(*args, **kwargs):
    raise AssertionError('Replay must never call network or wait')
socket.socket = forbidden
time.sleep = forbidden
from workflow.queue_replay import main
raise SystemExit(main([sys.argv[2], '--bundle', sys.argv[1]]))
"""
    result = subprocess.run([sys.executable, "-c", script, str(bundle), command], capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
    value = json.loads(result.stdout)
    if command == "report":
        assert value["coverage"]["accounted_attempts"] == 1
        assert value["resources"]["cost"]["total"] == 0
    else:
        assert value["accounted_attempt_count"] == 1
