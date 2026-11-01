"""Matched execution uses explicit synthetic providers and simulated clocks."""

from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timedelta
import hashlib
import json
import re
import subprocess
import sys
from uuid import uuid4

import pytest

from data.quality_reviews import load_json
from shared.deployments import DeploymentHealth, DeploymentRegistry
from shared.scheduling import ExecutionAttempt
from workflow.evaluation_protocol import seal_protocol, schedule_trials
from workflow.evaluate_policies import prepare_evaluation, run_evaluation, default_arms
from workflow.policy_comparison import export_comparison, load_comparison, load_evaluation
from workflow.workload import NO_EVIDENCE

from test_evaluation_protocol import protocol, planned_protocol, NOW
from test_planned_replay import planned_inputs
from test_staged_planning import inputs


class Clock:
    def __init__(self, instant=NOW):
        self.time = instant

    def now(self):
        return self.time

    def sleep(self, seconds):
        self.time += timedelta(seconds=seconds)


@pytest.fixture
def prepared(tmp_path, protocol):
    source = tmp_path / "implementation"
    path = source / "workflow/evaluation_protocol.py"
    path.parent.mkdir(parents=True)
    path.write_bytes(b"# Frozen fixture implementation.\n")
    protocol = seal_protocol({**protocol, "source_manifest": {
        "workflow/evaluation_protocol.py": hashlib.sha256(path.read_bytes()).hexdigest()}})
    directory = prepare_evaluation(protocol, tmp_path / "evaluation", source_root=source, clock=Clock())
    return protocol, directory, source


def provider(clock, calls, *, bad=None, callback=None):
    def dispatch(candidate, registry, prompt, **settings):
        calls.append((candidate, prompt, settings))
        started = clock.now()
        duration = 2 if candidate.model_id == "large" else 1
        clock.sleep(duration)
        ticket = json.JSONDecoder().raw_decode(prompt[prompt.index('{'):])[0]["ticket"]
        sources = re.findall(r"KB-[A-Z0-9]+(?:-[A-Z0-9]+)*", prompt)
        citation = " [" + sources[0] + "]" if sources else ""
        output = {"classify": "billing" if "invoice" in ticket else "account_access",
                  "retrieve": "Fixture evidence" + citation if citation else NO_EVIDENCE,
                  "draft": "Fixture response" + citation, "summarize": "Fixture CRM summary"}[candidate.step_id]
        if bad == candidate.step_id:
            output = "invalid-category" if bad == "classify" else ""
        if callback:
            callback(candidate)
        return ExecutionAttempt(uuid4().hex, settings["workflow_id"], candidate, "completed",
            actual_start=started, actual_finish=clock.now(), output=output,
            executed_model_id=registry.get(candidate.deployment_id).provider_model(candidate.model_id),
            executed_deployment_id=candidate.deployment_id, input_tokens=32, output_tokens=12,
            provider_seconds=duration, output_limit_tokens=settings["output_limit_tokens"],
            enable_thinking=settings["enable_thinking"], completion_reason="stop")
    return dispatch


def run(prepared, clock, calls, **changes):
    _, directory, source = prepared
    settings = {"clock": clock, "sleeper": clock.sleep, "dispatcher": provider(clock, calls),
                "heartbeat": False, "progress": False, "source_root": source}
    settings.update(changes)
    return run_evaluation(directory, **settings)


def test_freeze_retains_pending_randomized_schedule_without_queue_or_inference(prepared):
    protocol, directory, _ = prepared
    assert not (directory / "queue.sqlite3").exists()
    journal = load_json(directory / "evaluation.json")
    assert journal["status"] == "prepared" and all(row["state"] == "pending" for row in journal["trials"])
    assert [row["trial_id"] for row in journal["trials"]] == [row["trial_id"] for row in schedule_trials(protocol)]
    report = load_evaluation(directory)
    assert report["finished_trials"] == 0 and report["scheduled_trials"] == 8


def test_serial_paired_execution_roundtrip_and_zero_baseline(prepared, tmp_path):
    _, directory, _ = prepared
    clock, calls = Clock(), []
    report = run(prepared, clock, calls)
    assert report["finished_trials"] == report["scheduled_trials"] == 8 and len(calls) == 32
    assert report["experiment_mode"] == "simulation" and not report["release_evidence"]["eligible"]
    assert load_json(directory / "evaluation.json")["status"] == "completed"
    assert all(arm["status_counts"] == {"completed": 4} for arm in report["arms"])
    paired = report["paired_comparisons"][0]
    assert paired["metrics"]["response_seconds"]["absolute_delta"]["mean"] == -3
    assert all(row["metrics"]["cost"]["percentage_reason"] == "zero-baseline" for row in paired["pairs"])
    assert all(row["metrics"]["energy"]["absolute_delta"] is None for row in paired["pairs"])
    bundle = export_comparison(directory, tmp_path / "portable")
    assert not (bundle / "queue.sqlite3").exists() and load_comparison(bundle) == report
    assert run(prepared, clock, calls) == report and len(calls) == 32


def test_batched_restart_preserves_completed_output_and_order(prepared):
    _, directory, _ = prepared
    clock, calls = Clock(), []
    assert run(prepared, clock, calls, max_trials=1)["finished_trials"] == 1
    first = load_json(directory / "evaluation.json")["trials"][0]
    original = (directory / first["bundle"] / "workflow.json").read_bytes()
    assert run(prepared, clock, calls)["finished_trials"] == 8 and len(calls) == 32
    assert (directory / first["bundle"] / "workflow.json").read_bytes() == original


def test_reset_clock_is_rejected_before_resumed_inference(prepared):
    clock, calls = Clock(), []
    run(prepared, clock, calls, max_trials=1)
    with pytest.raises(ValueError, match="Resume clock precedes"):
        run(prepared, Clock(), calls)
    assert len(calls) == 4


def test_unexpected_provider_crash_never_repeats_and_halts_serial_experiment(prepared):
    _, directory, _ = prepared
    clock, calls = Clock(), []
    def crash(*args, **kwargs):
        calls.append("accepted")
        raise RuntimeError("Interrupted after provider acceptance")
    with pytest.raises(RuntimeError):
        run(prepared, clock, calls, dispatcher=crash)
    assert load_json(directory / "evaluation.json")["status"] == "interrupted"
    clock.sleep(61)
    report = run(prepared, clock, calls, dispatcher=lambda *a, **k: pytest.fail("Ambiguous inference repeated"))
    assert report["finished_trials"] == len(calls) == 1
    assert report["trials"][0]["workflow_status"] == "needs-reconciliation"
    with pytest.raises(ValueError, match="Unresolved"):
        run(prepared, clock, calls)


def test_missing_submitted_trial_queue_cannot_regenerate_work(prepared):
    _, directory, _ = prepared
    clock, calls = Clock(), []
    def crash(*args, **kwargs):
        raise RuntimeError("Unknown provider outcome")
    with pytest.raises(RuntimeError):
        run(prepared, clock, calls, dispatcher=crash)
    first = load_json(directory / "evaluation.json")["trials"][0]
    (directory / "queues" / (first["trial_id"] + ".sqlite3")).unlink()
    with pytest.raises(ValueError, match="original durable queue"):
        run(prepared, clock, calls)


def test_submission_committed_before_journal_interruption_is_adopted(prepared, monkeypatch):
    import workflow.evaluate_policies as module
    _, directory, _ = prepared
    clock, calls = Clock(), []
    original = module._submit_trial
    def interrupted(*args, **kwargs):
        original(*args, **kwargs)
        raise RuntimeError("Stopped after atomic submission")
    with monkeypatch.context() as patch:
        patch.setattr(module, "_submit_trial", interrupted)
        with pytest.raises(RuntimeError):
            run(prepared, clock, calls)
    assert load_json(directory / "evaluation.json")["trials"][0]["state"] == "pending"
    assert run(prepared, clock, calls)["finished_trials"] == 8 and len(calls) == 32


def test_optional_accounting_failure_stops_trial_and_preserves_gap(prepared, monkeypatch):
    import workflow.execution_accounting as accounting
    clock, calls = Clock(), []
    def unavailable(*args, **kwargs):
        raise RuntimeError("optional enrichment failed")
    monkeypatch.setattr(accounting, "enrich_attempt", unavailable)
    report = run(prepared, clock, calls)
    assert report["finished_trials"] == len(calls) == 8
    assert all(arm["status_counts"] == {"cancelled": 4} for arm in report["arms"])
    assert all(arm["resources"]["cost"]["total"] is None for arm in report["arms"])


def test_returned_timeout_stops_before_another_potentially_overlapping_trial(prepared):
    clock, calls = Clock(), []
    dispatch = provider(clock, calls)
    def timeout(*args, **kwargs):
        return replace(dispatch(*args, **kwargs), status="failed", output=None, error="request-timeout",
                       completion_reason=None, input_tokens=None, output_tokens=None)
    report = run(prepared, clock, calls, dispatcher=timeout)
    assert report["finished_trials"] == len(calls) == 1
    assert report["trials"][0]["execution"]["coverage"]["uncertain_provider_attempt_ids"]


def test_invalid_output_failure_remains_in_all_paired_denominators(prepared):
    clock, calls = Clock(), []
    report = run(prepared, clock, calls, dispatcher=provider(clock, calls, bad="classify"))
    assert report["finished_trials"] == len(calls) == 8
    assert all(arm["status_counts"] == {"failed": 4} for arm in report["arms"])
    assert report["paired_comparisons"][0]["metrics"]["cost"]["matched_pairs"] == 0


def test_changed_source_cannot_resume(prepared):
    _, directory, source = prepared
    clock, calls = Clock(), []
    run(prepared, clock, calls, max_trials=1)
    path = source / "workflow/evaluation_protocol.py"
    original = path.read_bytes()
    path.write_bytes(b"changed")
    with pytest.raises(ValueError, match="source differs"):
        run(prepared, clock, calls)
    path.write_bytes(original)
    assert len(calls) == 4


def test_explicit_simulation_required_for_fixture_dispatch_and_clock(prepared):
    protocol, directory, _ = prepared
    with pytest.raises(ValueError, match="Simulation requires"):
        run_evaluation(directory)
    (directory / "protocol.json").write_text(json.dumps(seal_protocol({**protocol, "experiment_mode": "live"})), encoding="utf-8")
    with pytest.raises(ValueError, match="Injected execution"):
        run_evaluation(directory, dispatcher=lambda *a, **k: pytest.fail("Fixture labelled live"))


def test_existing_output_and_incorrect_source_never_overwrite(prepared, tmp_path):
    protocol, directory, source = prepared
    with pytest.raises(FileExistsError):
        prepare_evaluation(protocol, directory, source_root=source, clock=Clock())
    wrong = seal_protocol({**protocol, "source_manifest": {"workflow/evaluation_protocol.py": "0" * 64}})
    with pytest.raises(ValueError, match="source differs"):
        prepare_evaluation(wrong, tmp_path / "wrong", source_root=source, clock=Clock())
    assert not (tmp_path / "wrong").exists()


def test_portable_comparison_cannot_be_mutated_by_runner(prepared, tmp_path):
    bundle = export_comparison(prepared[1], tmp_path / "portable")
    original = (bundle / "manifest.json").read_bytes()
    with pytest.raises(ValueError, match="Portable comparisons are immutable"):
        run_evaluation(bundle)
    assert (bundle / "manifest.json").read_bytes() == original
    assert not (bundle / "queues").exists()


def test_default_ablations_and_fixed_per_step_assignments(prepared):
    protocol, directory, source = prepared
    arms = default_arms(DeploymentRegistry.from_dict(protocol["registry"]))
    assert {arm["arm_id"] for arm in arms} >= {"joint-speed", "joint-balanced", "joint-eco", "model-only",
                                             "without-time-shift", "destination-time-only"}
    assert all(not arm["allow_summary_deferral"] for arm in arms if arm["arm_id"] in {"model-only", "without-time-shift"})
    value = deepcopy(protocol)
    value["arms"][1]["allowed_pairs"]["draft"] = [{"deployment_id": "local", "model_id": "large"}]
    value = seal_protocol(value)
    second = prepare_evaluation(value, directory.parent / "per-step", source_root=source, clock=Clock())
    clock, calls = Clock(), []
    report = run((value, second, source), clock, calls)
    small_ids = {row["workflow_id"] for row in report["trials"] if row["arm_id"] == "small"}
    assert [candidate.model_id for candidate, _, settings in calls
            if candidate.step_id == "draft" and settings["workflow_id"] in small_ids] == ["large"] * 4


def test_planned_trials_chain_actual_outputs_and_retain_simulation_mode(tmp_path, planned_protocol):
    value = {**planned_protocol, "repeats": 1, "source_manifest": {"fixture.py": hashlib.sha256(b"# fixture\n").hexdigest()}}
    value = seal_protocol(value)
    source = tmp_path / "source"
    source.mkdir()
    (source / "fixture.py").write_bytes(b"# fixture\n")
    clock = Clock(datetime.fromisoformat(value["created_at"]))
    directory = prepare_evaluation(value, tmp_path / "planned", source_root=source, clock=clock)
    calls = []
    probe = lambda deployment, **kwargs: DeploymentHealth(deployment.deployment_id, clock.now(), True,
        tuple(deployment.models), "simulation-fixture")
    report = run((value, directory, source), clock, calls, health_probe=probe)
    assert report["finished_trials"] == 2 and len(calls) == 8
    assert all(row["workflow_status"] == "completed" for row in report["trials"])


def test_custom_frozen_knowledge_survives_restart_without_default_file(prepared, monkeypatch):
    from workflow.workload import KnowledgeBase
    protocol, directory, source = prepared
    value = deepcopy(protocol)
    value["knowledge_base"]["description"] = "A separately frozen comparison knowledge base"
    value["repeats"] = 1
    value = seal_protocol(value)
    target = prepare_evaluation(value, directory.parent / "custom-kb", source_root=source, clock=Clock())
    monkeypatch.setattr(KnowledgeBase, "load", lambda *a, **k: pytest.fail("Read mutable default KB"))
    clock, calls = Clock(), []
    run((value, target, source), clock, calls, max_trials=1)
    report = run((value, target, source), clock, calls)
    assert report["finished_trials"] == 4 and len(calls) == 16
    assert all(row["workflow_status"] == "completed" for row in report["trials"])


def test_later_trial_never_revives_previously_exported_infeasible_work(tmp_path, planned_protocol):
    value = deepcopy(planned_protocol)
    planned = value["arms"][1]
    value.update(repeats=1, baseline_arm_id="unavailable", arms=[{**planned, "arm_id": "unavailable"},
                                                               {**planned, "arm_id": "available"}],
                 source_manifest={"fixture.py": hashlib.sha256(b"# isolated trial queues\n").hexdigest()})
    for seed in range(20):
        value = seal_protocol({**value, "seed": seed})
        if schedule_trials(value)[0]["arm_id"] == "unavailable":
            break
    source = tmp_path / "implementation"
    source.mkdir()
    (source / "fixture.py").write_bytes(b"# isolated trial queues\n")
    clock = Clock(datetime.fromisoformat(value["created_at"]))
    directory = prepare_evaluation(value, tmp_path / "evaluation", source_root=source, clock=clock)
    observations = []
    def probe(deployment, **kwargs):
        healthy = bool(observations)
        observations.append(healthy)
        return DeploymentHealth(deployment.deployment_id, clock.now(), healthy,
            tuple(deployment.models) if healthy else (), "test-fixture", None if healthy else "connection-failed")
    calls = []
    report = run((value, directory, source), clock, calls, health_probe=probe,
        dispatcher=provider(clock, calls, callback=lambda _: clock.sleep(10)))
    assert report["finished_trials"] == 2 and len(calls) == 4
    first = report["trials"][0]
    assert first["workflow_status"] == "infeasible"
    assert all(settings["workflow_id"] != first["workflow_id"] for _, _, settings in calls)
    from workflow.queue_store import QueueStore
    store = QueueStore(directory / "queues" / (first["trial_id"] + ".sqlite3"))
    try:
        assert len(store.read(first["workflow_id"]).decisions) == 1
    finally:
        store.close()


def test_report_export_inspection_import_no_transports_or_waits(prepared, tmp_path):
    _, directory, _ = prepared
    script = """
import builtins, socket, sys, time
original = builtins.__import__
def guarded(name, *args, **kwargs):
    if name in {'workflow.worker', 'workflow.planned_worker', 'workflow.dispatch',
                'workflow.deployment_clients', 'requests', 'codecarbon'}:
        raise AssertionError('Offline command imported transport: ' + name)
    return original(name, *args, **kwargs)
builtins.__import__ = guarded
def forbidden(*args, **kwargs):
    raise AssertionError('Offline command contacted a provider or waited')
socket.socket = forbidden
time.sleep = forbidden
from workflow.evaluate_policies import main
raise SystemExit(main(sys.argv[1:]))
"""
    bundle = tmp_path / "portable"
    for args in (["report", "--evaluation", str(directory)],
                 ["export", "--evaluation", str(directory), "--output", str(bundle)],
                 ["inspect", "--bundle", str(bundle)]):
        result = subprocess.run([sys.executable, "-c", script, *args], capture_output=True, text=True, timeout=10)
        assert result.returncode == 0, result.stderr
        assert json.loads(result.stdout)
