"""Matched reports retain failures, unknowns, pairing and immutable evidence."""

from copy import deepcopy
from dataclasses import replace
from datetime import timedelta
import hashlib
import json
from pathlib import Path
import subprocess
import sys

import pytest

from data.carbon_sources import HistoricalCarbonTrace
from data.quality_reviews import fingerprint
from shared.deployments import DeploymentRegistry
from shared.scheduling import CarbonSample, ExecutionAttempt
from workflow.evaluation_protocol import schedule_trials, seal_protocol
from workflow.execution_accounting import enrich_attempt, make_accounting_context, unknown_energy_report
from workflow.planning_records import decode_carbon, decode_knowledge_base
from workflow.policy_comparison import (
    comparison_report, export_comparison, load_comparison, load_evaluation,
    main, validate_evaluation_journal, _delta, _stats,
)
from workflow.queue_replay import export_workflow
from workflow.queue_store import QueueStore
from workflow.workload import render_step

from test_evaluation_protocol import NOW, fixed_snapshot, protocol


SOURCE = b"retained source fixture\n"


def encoded(value):
    return (json.dumps(value, indent=2, allow_nan=False) + "\n").encode()


def seal(value):
    value = deepcopy(value)
    value.pop("fingerprint", None)
    value["fingerprint"] = fingerprint(value)
    return value


def _snapshot(protocol, trial, arrived, directory, *, fail=False):
    base = fixed_snapshot(protocol, trial)
    difference = arrived - NOW
    request = replace(base.request, arrived_at=arrived,
        response_deadline=base.request.response_deadline + difference,
        completion_deadline=base.request.completion_deadline + difference,
        steps=tuple(replace(step, earliest_start=step.earliest_start + difference,
                            latest_finish=step.latest_finish + difference) for step in base.request.steps))
    registry = DeploymentRegistry.from_dict(protocol["registry"])
    kb = decode_knowledge_base(protocol["knowledge_base"])
    plans = tuple(replace(candidate, planned_start=arrived) for candidate in base.plan_history)
    context = make_accounting_context(registry, request, carbon_provider=decode_carbon(protocol["carbon"]),
        energy_mode=protocol["energy_mode"], energy_country=protocol["energy_country"])
    store = QueueStore(directory / (trial["trial_id"] + ".sqlite3"))
    now = arrived
    duration = 2 if trial["arm_id"] == "baseline" else 1
    try:
        store.enqueue(request, plans, configuration=base.configuration, accounting_context=context)
        for index, _step in enumerate(request.steps):
            claim = store.claim("fixture", now=now, lease_seconds=60)
            rendered = render_step(request, claim.candidate.step_id, store.read(request.workflow_id).completed_outputs(), kb=kb)
            store.mark_running(claim, now=now, rendered_step=rendered.step, source_ids=rendered.source_ids,
                               input_fingerprint=rendered.input_fingerprint, lookup_seconds=rendered.lookup_seconds)
            started = now
            now += timedelta(seconds=duration)
            citation = " [" + rendered.source_ids[0] + "]" if rendered.source_ids else ""
            category = "billing" if trial["case_id"] == "billing" else "account_access"
            output = category if claim.candidate.step_id == "classify" else "Retained output" + citation
            attempt = ExecutionAttempt(trial["trial_id"] + "-" + str(index), request.workflow_id, claim.candidate,
                "failed" if fail else "completed", actual_start=started, actual_finish=now,
                executed_model_id=registry.get(claim.candidate.deployment_id).provider_model(claim.candidate.model_id),
                executed_deployment_id=claim.candidate.deployment_id, output=None if fail else output,
                input_tokens=None if fail else 10, output_tokens=None if fail else 5,
                output_limit_tokens=rendered.step.output_limit_tokens, enable_thinking=False,
                completion_reason=None if fail else "stop", error="request-timeout" if fail else None)
            store.complete(claim, attempt, now=now)
            energy = unknown_energy_report()
            if protocol["energy_mode"] == "codecarbon":
                energy = {"energy_wh": 3., "energy_measured": False, "energy_method": "fixture counter",
                    "energy_provenance": "estimated", "energy_components": {
                        name: {"value_wh": 1., "provenance": "estimated", "source": "fixture-model"}
                        for name in ("cpu", "gpu", "ram")},
                    "energy_scope": "host-machine-including-idle-and-other-processes", "telemetry_error": None}
            record = enrich_attempt(attempt, registry, context, energy_report=energy, measurement_start=started,
                measurement_finish=now, journal_recorded_at=now, accounted_at=now)
            store.record_accounting(request.workflow_id, record, now=now)
            if fail:
                break
        return store.read(request.workflow_id), now, store.path
    finally:
        store.close()


def _build_evaluation(root, protocol):
    protocol = seal_protocol({**protocol, "source_manifest": {
        "workflow/test_source.py": hashlib.sha256(SOURCE).hexdigest()}})
    root.mkdir()
    (root / "protocol.json").write_bytes(encoded(protocol))
    source = root / "source/workflow/test_source.py"
    source.parent.mkdir(parents=True)
    source.write_bytes(SOURCE)
    now, trials = NOW, []
    for identity in schedule_trials(protocol):
        snapshot, finished, database = _snapshot(protocol, identity, now, root)
        destination = "bundles/" + identity["trial_id"]
        export_workflow(database, identity["workflow_id"], root / destination)
        entry = {**identity, "state": "finished", "bundle": destination,
                 "started_at": now.isoformat(), "finished_at": finished.isoformat(), "stop_reason": "completed"}
        trials.append({"entry": entry, "snapshot": snapshot})
        now = finished + timedelta(seconds=1)
    journal = seal({"schema_version": 1, "record_type": "MatchedPolicyEvaluation",
        "protocol_fingerprint": protocol["fingerprint"], "status": "completed",
        "created_at": NOW.isoformat(), "updated_at": now.isoformat(),
        "trials": [item["entry"] for item in trials]})
    (root / "evaluation.json").write_bytes(encoded(journal))
    return root, protocol, journal, trials


_EVALUATION_TEMPLATE = None


@pytest.fixture
def evaluation(tmp_path, protocol):
    # Queue construction performs many fsync-backed transactions. Reuse its
    # immutable bytes and copy each test's data so corruption tests stay isolated.
    global _EVALUATION_TEMPLATE
    if _EVALUATION_TEMPLATE is None:
        root, frozen, journal, trials = _build_evaluation(tmp_path / "template", protocol)
        files = {path.relative_to(root): path.read_bytes() for path in root.rglob("*")
                 if path.is_file() and not path.name.endswith((".sqlite3", "-wal", "-shm"))}
        _EVALUATION_TEMPLATE = files, frozen, journal, trials
    files, frozen, journal, trials = _EVALUATION_TEMPLATE
    root = tmp_path / "evaluation"
    root.mkdir()
    for relative, content in files.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    return root, deepcopy(frozen), deepcopy(journal), deepcopy(trials)


def rewrite_journal(root, mutate):
    value = json.loads((root / "evaluation.json").read_text())
    mutate(value)
    (root / "evaluation.json").write_bytes(encoded(seal(value)))


def rewrite_package(bundle, path, mutate):
    target = bundle / path
    value = json.loads(target.read_text())
    mutate(value)
    target.write_bytes(encoded(value))
    manifest = json.loads((bundle / "manifest.json").read_text())
    manifest["files"][path] = hashlib.sha256(target.read_bytes()).hexdigest()
    (bundle / "manifest.json").write_bytes(encoded(seal(manifest)))


def test_complete_report_keeps_fair_pairing_zero_denominators_and_unknown_quality(evaluation):
    root, protocol, _, trials = evaluation
    report = comparison_report(protocol, trials)
    assert load_evaluation(root) == report
    assert report["scheduled_trials"] == report["finished_trials"] == 8
    assert report["experiment_mode"] == "simulation" and report["release_evidence"]["eligible"] is False
    assert "end-to-end-quality-unreviewed" in report["release_evidence"]["reasons"]
    baseline, small = report["arms"]
    assert baseline["timing"]["completion_seconds"]["mean"] == 8
    assert small["timing"]["completion_seconds"]["mean"] == 4
    assert small["resources"]["cost"]["total"] == 0
    assert small["resources"]["energy"]["total"] is None
    assert baseline["quality"]["value"] is None and baseline["quality"]["unknown_trials"] == 4
    comparison = report["paired_comparisons"][0]
    assert comparison["metrics"]["completion_seconds"]["matched_pairs"] == 4
    assert comparison["metrics"]["completion_seconds"]["absolute_delta"]["mean"] == -4
    assert comparison["metrics"]["completion_seconds"]["reduction_percent"]["mean"] == 50
    assert comparison["metrics"]["cost"]["absolute_delta"]["mean"] == 0
    assert comparison["metrics"]["cost"]["reduction_percent"]["count"] == 0
    assert all(pair["metrics"]["cost"]["percentage_reason"] == "zero-baseline" for pair in comparison["pairs"])
    assert comparison["metrics"]["energy"]["matched_pairs"] == 0


def test_missing_trials_and_failed_work_remain_coverage_denominators(evaluation, tmp_path):
    _, protocol, _, trials = evaluation
    first, now, _ = _snapshot(protocol, {key: trials[0]["entry"][key] for key in
        ("trial_id", "case_id", "repeat", "arm_id", "workflow_id")}, NOW, tmp_path, fail=True)
    trials[0]["snapshot"] = first
    trials[0]["entry"]["finished_at"] = now.isoformat()
    trials[0]["entry"]["stop_reason"] = "failed"
    for item in trials[1:]:
        item["snapshot"] = None
        item["entry"].update(state="pending", bundle=None, started_at=None, finished_at=None, stop_reason=None)
    report = comparison_report(protocol, trials)
    assert report["finished_trials"] == 1 and report["scheduled_trials"] == 8
    assert sum(arm["status_counts"].get("failed", 0) for arm in report["arms"]) == 1
    assert sum(arm["status_counts"].get("pending", 0) for arm in report["arms"]) == 7
    assert report["paired_comparisons"][0]["metrics"]["completion_seconds"]["unmatched_pairs"] == 4
    assert "unfinished-trials" in report["release_evidence"]["reasons"]


def test_replay_package_excludes_runtime_databases_and_recomputes_identically(evaluation, tmp_path):
    root, _, _, _ = evaluation
    original = load_evaluation(root)
    output = export_comparison(root, tmp_path / "portable")
    assert load_comparison(output) == original
    manifest = json.loads((output / "manifest.json").read_text())
    assert not any(name.endswith(".sqlite3") for name in manifest["files"])
    assert "source/workflow/test_source.py" in manifest["files"]
    assert len([name for name in manifest["files"] if name.endswith("workflow.json")]) == 8
    assert len([name for name in manifest["files"] if name.endswith("manifest.json")]) == 8
    with pytest.raises(FileExistsError):
        export_comparison(root, output)


@pytest.mark.parametrize("mutate", [
    lambda value: value.update(extra=True),
    lambda value: value.update(status="completed-again"),
    lambda value: value.update(protocol_fingerprint="0" * 64),
    lambda value: value.update(created_at="2026-11-01T12:00:00"),
    lambda value: value["trials"].pop(),
    lambda value: value["trials"].reverse(),
    lambda value: value["trials"][0].update(workflow_id="different-workflow"),
    lambda value: value["trials"][0].update(repeat=bool(value["trials"][0]["repeat"])),
    lambda value: value["trials"][0].update(bundle="../outside"),
    lambda value: value["trials"][0].update(started_at="2026-11-01T11:59:59+00:00"),
    lambda value: value["trials"][0].update(finished_at="2026-11-01T11:59:59+00:00"),
    lambda value: value["trials"][0].update(state="pending"),
    lambda value: value["trials"][1].update(started_at=value["trials"][0]["started_at"]),
])
def test_rehashed_journal_shape_identity_schedule_and_chronology_tampering_fails(evaluation, mutate):
    root, _, _, _ = evaluation
    rewrite_journal(root, mutate)
    with pytest.raises(ValueError):
        load_evaluation(root)


def test_started_and_finished_trial_span_must_enclose_actual_attempts(evaluation):
    root, _, _, _ = evaluation
    rewrite_journal(root, lambda value: value["trials"][0].update(
        finished_at=(NOW + timedelta(seconds=.5)).isoformat()))
    with pytest.raises(ValueError, match="enclose"):
        load_evaluation(root)


def test_unknown_source_bytes_and_extra_portable_files_are_rejected(evaluation, tmp_path):
    root, _, _, _ = evaluation
    output = export_comparison(root, tmp_path / "portable")
    (output / "unexpected.txt").write_text("not listed")
    with pytest.raises(ValueError, match="unlisted"):
        load_comparison(output)
    (root / "source/workflow/test_source.py").write_bytes(b"changed source")
    with pytest.raises(ValueError, match="source"):
        load_evaluation(root)


def test_rehashed_fabricated_comparison_values_fail_semantic_recompute(evaluation, tmp_path):
    root, _, _, _ = evaluation
    output = export_comparison(root, tmp_path / "portable")
    def fabricate(value):
        value["arms"][0]["resources"]["energy"]["total"] = 0.
        value.update(fingerprint=fingerprint({key: item for key, item in value.items() if key != "fingerprint"}))
    rewrite_package(output, "comparison.json", fabricate)
    with pytest.raises(ValueError, match="recomputed"):
        load_comparison(output)


def test_unrelated_valid_workflow_cannot_join_an_expected_trial(evaluation, tmp_path):
    _, protocol, _, trials = evaluation
    trials[0]["snapshot"] = trials[1]["snapshot"]
    with pytest.raises(ValueError):
        comparison_report(protocol, trials)


def test_invalid_resource_sidecar_is_not_trusted_by_direct_report_call(evaluation):
    _, protocol, _, trials = evaluation
    snapshot = trials[0]["snapshot"]
    records = deepcopy(snapshot.accounting)
    records[0]["metrics"]["energy"]["value"] = 0.
    records[0]["metrics"]["energy"].update(provenance="measured", source="invented")
    records[0]["fingerprint"] = fingerprint({key: item for key, item in records[0].items() if key != "fingerprint"})
    trials[0]["snapshot"] = replace(snapshot, accounting=records)
    with pytest.raises(ValueError):
        comparison_report(protocol, trials)


def test_pending_journal_is_reportable_and_exportable_without_inference(evaluation, tmp_path):
    root, protocol, journal, _ = evaluation
    for entry in journal["trials"]:
        entry.update(state="pending", bundle=None, started_at=None, finished_at=None, stop_reason=None)
    journal.update(status="prepared", updated_at=journal["created_at"])
    (root / "evaluation.json").write_bytes(encoded(seal(journal)))
    report = load_evaluation(root)
    assert report["finished_trials"] == 0 and report["scheduled_trials"] == 8
    for arm in report["arms"]:
        assert arm["resources"]["cost"]["total"] is None
        assert arm["timing"]["completion_seconds"]["count"] == 0
    output = export_comparison(root, tmp_path / "prepared")
    assert load_comparison(output) == report
    assert not any("bundles/" in name for name in json.loads((output / "manifest.json").read_text())["files"])


def test_comparison_cli_never_imports_transports_or_waits(evaluation, tmp_path):
    output = export_comparison(evaluation[0], tmp_path / "portable")
    script = """
import builtins, socket, sys, time
original_import = builtins.__import__
def guarded(name, *args, **kwargs):
    if name in {'workflow.worker', 'workflow.planned_worker', 'workflow.deployment_clients',
                'workflow.model_clients', 'requests', 'codecarbon'}:
        raise AssertionError('Forbidden comparison import: ' + name)
    return original_import(name, *args, **kwargs)
builtins.__import__ = guarded
def forbidden(*args, **kwargs):
    raise AssertionError('Comparison cannot call network or wait')
socket.socket = forbidden
time.sleep = forbidden
from workflow.policy_comparison import main
raise SystemExit(main(['inspect', '--bundle', sys.argv[1]]))
"""
    result = subprocess.run([sys.executable, "-c", script, str(output)], capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["finished_trials"] == 8


def test_cli_reports_valid_evidence_and_invalid_input_cleanly(evaluation, capsys):
    root, _, _, _ = evaluation
    assert main(["report", "--evaluation", str(root)]) == 0
    assert json.loads(capsys.readouterr().out)["record_type"] == "MatchedPolicyComparison"
    (root / "evaluation.json").write_text("{broken")
    assert main(["report", "--evaluation", str(root)]) == 2
    assert json.loads(capsys.readouterr().out)["error_code"] == "invalid-policy-comparison-evidence"


def test_deltas_refuse_incompatible_carbon_scopes_and_percentage_overflow():
    def observed(value, unit):
        return {"status": "completed", "resources": {"carbon": {"total": value, "unit": unit, "complete": True}}}
    value = _delta(observed(1., "gCO2"), observed(1., "gCO2e"), "carbon")
    assert value["comparison_reason"] == "incompatible-units" and value["absolute_delta"] is None
    value = _delta(observed(1e-308, "gCO2"), observed(1e308, "gCO2"), "carbon")
    assert value["absolute_delta"] == 1e308 and value["reduction_percent"] is None
    assert value["percentage_reason"] == "percentage-overflow"
    assert _stats([1.])["sample_standard_deviation"] is None


def test_exact_source_and_workflow_checksums_cannot_be_dropped_by_rehash(evaluation, tmp_path):
    output = export_comparison(evaluation[0], tmp_path / "portable")
    manifest_path = output / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["files"].pop("source/workflow/test_source.py")
    manifest_path.write_bytes(encoded(seal(manifest)))
    with pytest.raises(ValueError):
        load_comparison(output)


def test_raw_bundle_extras_are_not_silently_published(evaluation):
    root, _, journal, _ = evaluation
    extra = root / journal["trials"][0]["bundle"] / "extra.txt"
    extra.write_text("not evidence")
    with pytest.raises(ValueError, match="exactly"):
        load_evaluation(root)


def test_unhashable_malformed_journal_values_are_validation_errors(evaluation):
    _, protocol, journal, _ = evaluation
    journal["status"] = []
    with pytest.raises(ValueError):
        validate_evaluation_journal(seal(journal), protocol)


def test_accounting_interval_cannot_begin_before_its_trial(evaluation):
    _, protocol, _, trials = evaluation
    snapshot = trials[0]["snapshot"]
    records = list(deepcopy(snapshot.accounting))
    records[0]["measurement_start"] = (NOW - timedelta(seconds=1)).isoformat().replace("+00:00", "Z")
    records[0] = seal(records[0])
    trials[0]["snapshot"] = replace(snapshot, accounting=records)
    with pytest.raises(ValueError, match="enclose"):
        comparison_report(protocol, trials)


def test_failed_export_cleans_only_its_newly_created_evidence(evaluation, tmp_path, monkeypatch):
    import workflow.policy_comparison as module
    output = tmp_path / "portable"
    sibling = tmp_path / "keep.txt"
    sibling.write_text("keep")
    monkeypatch.setattr(module.os, "fsync", lambda _descriptor: (_ for _ in ()).throw(OSError("disk full")))
    with pytest.raises(OSError):
        export_comparison(evaluation[0], output)
    assert sibling.read_text() == "keep" and not output.exists()
