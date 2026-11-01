"""Checksum and semantic validation of offline durable queue evidence."""

import builtins
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import time

import pytest

from shared.deployments import Deployment, DeploymentRegistry
from shared.scheduling import ExecutionAttempt, MetricEstimate, PlannedCandidate, StepEstimates, StepRequest, WorkflowRequest
from workflow.queue_replay import export_workflow, load_bundle, main
from workflow.queue_store import QueueStore


NOW = datetime(2026, 10, 19, 12, tzinfo=timezone.utc)


@pytest.fixture
def queued(tmp_path):
    unknown = lambda unit: MetricEstimate(None, unit, "unknown")
    estimates = StepEstimates(*(unknown(unit) for unit in ("s", "ratio", "USD", "kWh", "gCO2e")))
    steps = tuple(StepRequest(name, name, "Template " + name, None, 128, NOW, NOW + timedelta(minutes=5),
        dependencies=(() if index == 0 else ("first",)), required_for_response=index == 0,
        enable_thinking=False) for index, name in enumerate(("first", "second")))
    request = WorkflowRequest("workflow-a", "First workflow ticket", NOW, "balanced", NOW + timedelta(minutes=5),
        NOW + timedelta(minutes=10), "fixture-v1", "constraints-v1", steps)
    plans = tuple(PlannedCandidate("candidate-" + step.step_id, step.step_id, "small", "edge", NOW,
        estimates) for step in steps)
    registry = DeploymentRegistry((Deployment("edge", "ollama", "http://localhost:11434", {"small": "exact:tag"},
                                              credential_env="OFFLINE_ENV_NAME_ONLY"),))
    store = QueueStore(tmp_path / "queue.sqlite3")
    store.enqueue(request, plans, configuration={"registry_snapshot": registry.to_dict(),
        "registry_fingerprint": registry.config_fingerprint(), "experiment_mode": "simulation"})
    yield store, request, plans
    store.close()


def complete_first(store):
    claim = store.claim("worker", now=NOW, lease_seconds=60)
    rendered = replace(claim.step_request, prompt="Actual materialized first prompt", input_tokens=31,
                       input_size_source="utf8-bytes-proxy")
    store.mark_running(claim, now=NOW, rendered_step=rendered, source_ids=("KB-fixture",),
                       input_fingerprint="fixture-input-hash", lookup_seconds=.01)
    attempt = ExecutionAttempt("attempt-first", "workflow-a", claim.candidate, "completed", actual_start=NOW,
        actual_finish=NOW + timedelta(seconds=1), executed_model_id="exact:tag", executed_deployment_id="edge",
        output="Accepted first output", input_tokens=13, output_tokens=5, output_limit_tokens=128,
        enable_thinking=False, completion_reason="stop")
    store.complete(claim, attempt, now=NOW + timedelta(seconds=1))
    return claim, attempt


def exported(queued, tmp_path):
    store, request, _ = queued
    return export_workflow(store.path, request.workflow_id, tmp_path / "bundle")


def rewrite_payload(bundle, mutate):
    path = bundle / "workflow.json"
    value = json.loads(path.read_text(encoding="utf-8"))
    mutate(value)
    data = json.dumps(value, ensure_ascii=False).encode("utf-8")
    path.write_bytes(data)
    manifest = json.loads((bundle / "manifest.json").read_text())
    manifest["files"]["workflow.json"] = hashlib.sha256(data).hexdigest()
    (bundle / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")


def test_export_reconstructs_typed_snapshot_without_other_workflows(queued, tmp_path, monkeypatch):
    store, request, plans = queued
    complete_first(store)
    store.enqueue(replace(request, workflow_id="other-workflow", input_text="OTHER_PRIVATE_TICKET"), plans, configuration={})
    monkeypatch.setenv("OFFLINE_ENV_NAME_ONLY", "NEVER_COPY_THIS_CREDENTIAL_VALUE")
    before = store.read(request.workflow_id)
    bundle = exported(queued, tmp_path)
    replayed = load_bundle(bundle)
    assert replayed == before
    assert replayed.status == "response_ready"
    assert replayed.jobs[0].rendered_step.prompt == "Actual materialized first prompt"
    text = (bundle / "workflow.json").read_text()
    assert "OTHER_PRIVATE_TICKET" not in text and "other-workflow" not in text
    assert "NEVER_COPY_THIS_CREDENTIAL_VALUE" not in text
    assert "OFFLINE_ENV_NAME_ONLY" in text
    assert isinstance(replayed.request, WorkflowRequest)
    assert isinstance(replayed.attempts[0], ExecutionAttempt)


def test_export_uses_one_snapshot_even_if_source_advances_after_read(queued, tmp_path, monkeypatch):
    from workflow import queue_replay
    original = queue_replay._snapshot_dict
    def advance_after_snapshot(snapshot):
        complete_first(queued[0])
        return original(snapshot)
    monkeypatch.setattr(queue_replay, "_snapshot_dict", advance_after_snapshot)
    replayed = load_bundle(exported(queued, tmp_path))
    assert replayed.status == "scheduled" and replayed.attempts == ()
    assert queued[0].read("workflow-a").status == "response_ready"


def test_pending_delayed_replay_has_no_original_wait_or_database_mutation(queued, tmp_path):
    store, request, plans = queued
    delayed = tuple(replace(plan, planned_start=NOW + timedelta(seconds=200)) for plan in plans)
    store.replan(request.workflow_id, delayed, now=NOW)
    before = store.read(request.workflow_id)
    started = time.monotonic()
    replayed = load_bundle(exported(queued, tmp_path))
    assert time.monotonic() - started < 2
    assert replayed == before == store.read(request.workflow_id)
    assert replayed.attempts == ()


def test_running_and_reconciliation_states_replay_without_reclaiming_work(queued, tmp_path):
    store, request, _ = queued
    claim = store.claim("worker", now=NOW, lease_seconds=2)
    store.mark_running(claim, now=NOW)
    running = export_workflow(store.path, request.workflow_id, tmp_path / "running")
    assert load_bundle(running).status == "running"
    store.recover(now=NOW + timedelta(seconds=2))
    ambiguous = export_workflow(store.path, request.workflow_id, tmp_path / "ambiguous")
    assert load_bundle(ambiguous).status == "needs-reconciliation"
    assert store.claim("other", now=NOW + timedelta(seconds=3), lease_seconds=5) is None


def test_cancelled_late_usage_and_output_are_retained(queued, tmp_path):
    store, request, _ = queued
    claim, attempt = complete_first(store)
    claim = store.claim("worker", now=NOW + timedelta(seconds=1), lease_seconds=60)
    store.mark_running(claim, now=NOW + timedelta(seconds=1))
    store.cancel(request.workflow_id, now=NOW + timedelta(seconds=2))
    late = replace(attempt, attempt_id="attempt-second", planned_candidate=claim.candidate,
        actual_start=NOW + timedelta(seconds=1), actual_finish=NOW + timedelta(seconds=3), output="Late output")
    store.complete(claim, late, now=NOW + timedelta(seconds=3))
    replayed = load_bundle(exported(queued, tmp_path))
    assert replayed.status == "cancelled"
    assert replayed.jobs[1].output == "Late output" and replayed.jobs[1].status == "cancelled"
    assert replayed.attempts[-1] == late
    assert replayed.events[-1]["kind"] == "cancelled"


def test_preflight_failed_attempt_stays_undispatched(queued, tmp_path):
    store, _, _ = queued
    claim = store.claim("worker", now=NOW, lease_seconds=60)
    store.mark_running(claim, now=NOW)
    failed = ExecutionAttempt("preflight", "workflow-a", claim.candidate, "failed", error="missing-credential",
                              output_limit_tokens=128, enable_thinking=False)
    store.complete(claim, failed, now=NOW)
    replayed = load_bundle(exported(queued, tmp_path))
    assert replayed.status == "failed"
    assert replayed.attempts[0].actual_start is replayed.attempts[0].actual_finish is None


def test_checksum_mismatch_is_rejected(queued, tmp_path):
    bundle = exported(queued, tmp_path)
    with (bundle / "workflow.json").open("ab") as stream:
        stream.write(b" ")
    with pytest.raises(ValueError, match="checksum"):
        load_bundle(bundle)


@pytest.mark.parametrize("mutate", [
    lambda value: value.update(schema_version=True),
    lambda value: value.update(bundle_version=99),
    lambda value: value.update(status="completed"),
    lambda value: value["configuration"].update(API_KEY="not-allowed"),
    lambda value: value["jobs"][0].update(retry_index=True),
    lambda value: value["jobs"][0]["step_request"].update(prompt="Unmatched original request"),
    lambda value: value["jobs"][0]["candidate"].update(step_id="other-step"),
    lambda value: value["jobs"][0].update(output_valid=1),
    lambda value: value["jobs"][0].update(finished_at="2026-10-19T12:00:00"),
    lambda value: value.update(plan_history=[]),
    lambda value: value["jobs"].pop(),
    lambda value: value["events"].append({"step_id": "other-step", "kind": "cancelled", "recorded_at": NOW.isoformat()}),
])
def test_rehashed_malformed_evidence_still_fails_closed(queued, tmp_path, mutate):
    bundle = exported(queued, tmp_path)
    rewrite_payload(bundle, mutate)
    with pytest.raises(ValueError):
        load_bundle(bundle)


@pytest.mark.parametrize("mutate", [
    lambda value: value["attempts"][0].update(workflow_id="other-workflow"),
    lambda value: value["attempts"][0].update(executed_model_id="unrequested-model"),
    lambda value: value["attempts"][0].update(output="Different accepted output"),
    lambda value: value["jobs"][0]["rendered_step"].update(output_limit_tokens=129),
    lambda value: value["jobs"][0].update(output_valid=False),
    lambda value: value["jobs"][0].update(dispatched_at=(NOW - timedelta(seconds=1)).isoformat()),
    lambda value: value["jobs"][0].update(dispatched_at=(NOW + timedelta(microseconds=1)).isoformat()),
    lambda value: value["jobs"][0].update(dispatched_at=None),
    lambda value: value["attempts"][0].update(actual_finish=(NOW + timedelta(seconds=2)).isoformat()),
    lambda value: value["jobs"][1].update(status="running", dispatched_at=NOW.isoformat(), rendered_step=value["jobs"][1]["step_request"]),
])
def test_rehashed_attempt_output_rendering_and_dependency_mismatch_rejected(queued, tmp_path, mutate):
    complete_first(queued[0])
    bundle = exported(queued, tmp_path)
    rewrite_payload(bundle, mutate)
    with pytest.raises(ValueError):
        load_bundle(bundle)


def test_manifest_path_traversal_unknown_files_and_workflow_mismatch_rejected(queued, tmp_path):
    bundle = exported(queued, tmp_path)
    manifest_path = bundle / "manifest.json"
    original = json.loads(manifest_path.read_text())
    for mutate in (lambda m: m.update(workflow_id="other"),
                   lambda m: m.update(files={"../private.json": "0" * 64}),
                   lambda m: m.update(schema_version=True)):
        value = json.loads(json.dumps(original))
        mutate(value)
        manifest_path.write_text(json.dumps(value), encoding="utf-8")
        with pytest.raises(ValueError):
            load_bundle(bundle)
    manifest_path.write_text(json.dumps(original), encoding="utf-8")
    (bundle / "unexpected.json").write_text("{}")
    with pytest.raises(ValueError, match="only"):
        load_bundle(bundle)


def test_duplicate_fields_and_nonfinite_json_rejected_even_with_correct_checksum(queued, tmp_path):
    bundle = exported(queued, tmp_path)
    for data in (b'{"schema_version":3,"schema_version":3}', b'{"schema_version":NaN}'):
        (bundle / "workflow.json").write_bytes(data)
        manifest = json.loads((bundle / "manifest.json").read_text())
        manifest["files"]["workflow.json"] = hashlib.sha256(data).hexdigest()
        (bundle / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        with pytest.raises(ValueError):
            load_bundle(bundle)


def test_export_never_overwrites_or_creates_missing_source_database(queued, tmp_path):
    store, request, _ = queued
    output = exported(queued, tmp_path)
    before = (output / "workflow.json").read_bytes()
    with pytest.raises(FileExistsError):
        export_workflow(store.path, request.workflow_id, output)
    assert (output / "workflow.json").read_bytes() == before
    missing_db, missing_output = tmp_path / "missing.sqlite3", tmp_path / "missing-bundle"
    with pytest.raises(FileNotFoundError):
        export_workflow(missing_db, request.workflow_id, missing_output)
    assert not missing_db.exists() and not missing_output.exists()
    with pytest.raises(KeyError):
        export_workflow(store.path, "other-workflow", missing_output)
    assert not missing_output.exists()


def test_invalid_source_evidence_does_not_create_bundle(queued, tmp_path):
    store, request, _ = queued
    store.connection.execute("UPDATE jobs SET status='completed' WHERE step_id='first'")
    with pytest.raises(ValueError):
        export_workflow(store.path, request.workflow_id, tmp_path / "bad")
    assert not (tmp_path / "bad").exists()


def test_archived_superseded_usage_does_not_become_current_retry_output(queued, tmp_path):
    store, request, _ = queued
    old = store.claim("old-worker", now=NOW, lease_seconds=2)
    store.mark_running(old, now=NOW)
    store.recover(now=NOW + timedelta(seconds=2))
    store.retry(request.workflow_id, "first", now=NOW + timedelta(seconds=2), acknowledge_ambiguous=True)
    current = store.claim("current-worker", now=NOW + timedelta(seconds=2), lease_seconds=60)
    store.mark_running(current, now=NOW + timedelta(seconds=2))
    late = ExecutionAttempt("late-first", request.workflow_id, old.candidate, "completed", actual_start=NOW,
        actual_finish=NOW + timedelta(seconds=3), executed_model_id="exact:tag", executed_deployment_id="edge",
        output="Superseded output", input_tokens=13, output_tokens=5, output_limit_tokens=128,
        enable_thinking=False, completion_reason="stop")
    store.archive_late_attempt(old, late, now=NOW + timedelta(seconds=3))
    replayed = load_bundle(exported(queued, tmp_path))
    assert replayed.status == "running" and replayed.jobs[0].retry_index == 1
    assert replayed.jobs[0].output is None
    assert replayed.attempts == (late,)
    assert replayed.events[-1]["kind"] == "archived_late_superseded_attempt"


def test_cli_inspection_imports_no_worker_provider_or_network_and_never_sleeps(queued, tmp_path):
    bundle = exported(queued, tmp_path)
    script = """
import builtins, socket, sys, time
original_import = builtins.__import__
def guarded(name, *args, **kwargs):
    if name in {'workflow.worker', 'workflow.deployment_clients', 'workflow.model_clients', 'requests'}:
        raise AssertionError('Forbidden replay import: ' + name)
    return original_import(name, *args, **kwargs)
builtins.__import__ = guarded
def forbidden(*args, **kwargs):
    raise AssertionError('Replay attempted a provider/network/wait operation')
socket.create_connection = socket.socket = forbidden
time.sleep = forbidden
from workflow.queue_replay import main
raise SystemExit(main(['inspect', '--bundle', sys.argv[1]]))
"""
    result = subprocess.run([sys.executable, "-c", script, str(bundle)], capture_output=True, text=True, timeout=5)
    assert result.returncode == 0, result.stderr
    summary = json.loads(result.stdout)
    assert summary["mode"] == "replay" and summary["attempt_count"] == 0
    assert summary["scope"] == "stored fixed-queue evidence; no policy comparison"


def test_export_and_inspect_cli(queued, tmp_path, capsys):
    store, request, _ = queued
    out = tmp_path / "cli-bundle"
    assert main(["export", "--db", str(store.path), "--workflow", request.workflow_id, "--output", str(out)]) == 0
    assert json.loads(capsys.readouterr().out)["workflow_id"] == request.workflow_id
    assert main(["inspect", "--bundle", str(out)]) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "scheduled"
