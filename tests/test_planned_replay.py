"""Planned bundles retain decisions without weakening legacy offline replay."""

from dataclasses import replace
from datetime import timedelta
import hashlib
import json
from pathlib import Path
import subprocess
import sys

import pytest

from data.carbon_sources import HistoricalCarbonTrace
from data.profiles import build_profiles
from data.quality_reviews import fingerprint
from shared.scheduling import CarbonSample, ExecutionAttempt
from workflow.connected import workload_fingerprint
from workflow.planning import PLANNING_SERVICE_VERSION, prepare_plan
from workflow.queue_replay import export_workflow, load_bundle
from workflow.queue_store import QueueStore

from test_staged_planning import inputs, NOW, at
from test_queue_replay import queued as legacy_queued


@pytest.fixture
def planned_inputs(inputs):
    profiles = build_profiles([{**trial, "case_id": "test-case", "quality_review_fingerprint": "a" * 64}
        for trial in inputs["_trials"]], calibration_id="test-reviewed", quality_policy={
            "schema_version": 1, "record_type": "QualityCoveragePolicy", "policy_id": "test-pilot",
            "minimum_reviewed_trials": 1, "minimum_distinct_cases": 1, "minimum_reviewed_fraction": 1.,
        })
    sample = CarbonSample("simulated-grid", at(-1), NOW, at(1000), 500., "test-grid-estimate", "estimated")
    carbon = HistoricalCarbonTrace("test-estimated-trace", (sample,), timedelta(seconds=1000))
    forecasts = {key: replace(value, profile_fingerprint=profiles["fingerprint"])
                 for key, value in inputs["forecasts"].items()}
    context = {"schema_version": 1, "record_type": "DurablePlanningContext", "profiles": profiles,
        "knowledge_base": {"schema_version": 1, "kb_version": inputs["kb"].version,
            "description": inputs["kb"].description, "categories": list(inputs["kb"].categories),
            "articles": [article.to_dict() for article in inputs["kb"].articles]},
        "forecasts": [value.to_dict() for value in forecasts.values()], "carbon": carbon.to_dict(),
        "custom_weights": None, "quality_floor": .8, "start_offsets_seconds": [0], "search_limit": 100_000,
        "health_max_age_seconds": 300., "minimum_improvement": .01, "replan_interval_seconds": 10.,
        "probe_timeout_seconds": 3., "max_decision_age_seconds": 5.}
    config = {"registry_snapshot": inputs["registry"].to_dict(),
        "registry_fingerprint": inputs["registry"].config_fingerprint(),
        "kb_fingerprint": inputs["kb"].fingerprint(),
        "workload_fingerprint": workload_fingerprint(inputs["request"], inputs["kb"]),
        "worker_version": "sqlite-planned-worker-v1", "policy_version": "joint-calibrated-v1",
        "planner_version": "joint-enumeration-v1", "calibration_id": profiles["calibration_id"],
        "experiment_mode": "live", "timeout_seconds": 120., "max_retries": 3}
    arguments = {key: value for key, value in inputs.items() if key != "_trials"}
    arguments.update(profiles=profiles, forecasts=forecasts, carbon_provider=carbon, experiment_mode="live",
                     minimum_improvement=.01)
    prepared = prepare_plan(**arguments)
    assert prepared.result.winner is not None
    return arguments, context, config, prepared


@pytest.fixture
def planned_queue(tmp_path, planned_inputs):
    arguments, context, config, prepared = planned_inputs
    store = QueueStore(tmp_path / "planned.sqlite3")
    candidates = tuple(item.candidate for item in prepared.result.winner.assignments)
    store.enqueue_planned(arguments["request"], candidates, configuration=config, planning_context=context,
                          decision=prepared.report, now=NOW)
    yield store, arguments, context, config, prepared
    store.close()


def rewrite(bundle, mutate, *, change_manifest=None):
    path = bundle / "workflow.json"
    value = json.loads(path.read_text(encoding="utf-8"))
    mutate(value)
    data = json.dumps(value, ensure_ascii=False).encode("utf-8")
    path.write_bytes(data)
    manifest = json.loads((bundle / "manifest.json").read_text(encoding="utf-8"))
    manifest["files"]["workflow.json"] = hashlib.sha256(data).hexdigest()
    if change_manifest:
        change_manifest(manifest)
    (bundle / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")


def export(planned_queue, tmp_path):
    store, arguments, *_ = planned_queue
    return export_workflow(store.path, arguments["request"].workflow_id, tmp_path / "bundle")


def test_planned_bundle_keeps_context_decisions_and_uses_version_two(planned_queue, tmp_path):
    bundle = export(planned_queue, tmp_path)
    payload = json.loads((bundle / "workflow.json").read_text(encoding="utf-8"))
    assert payload["bundle_version"] == 2
    assert json.loads((bundle / "manifest.json").read_text())["bundle_version"] == 2
    replayed = load_bundle(bundle)
    assert replayed.status == "scheduled" and replayed.planning_context == planned_queue[2]
    assert replayed.decisions[0]["revision"] == 1
    assert replayed.decisions[0]["report"] == planned_queue[-1].report
    assert len(replayed.jobs) == 4 and not replayed.attempts


def test_infeasible_submission_replays_without_fabricated_jobs(planned_inputs, tmp_path):
    arguments, context, config, _ = planned_inputs
    context = {**context, "forecasts": []}
    report = prepare_plan(**{**arguments, "health": {}, "forecasts": {}}).report
    store = QueueStore(tmp_path / "infeasible.sqlite3")
    try:
        store.enqueue_planned(arguments["request"], None, configuration=config, planning_context=context,
                              decision=report, now=NOW)
        replayed = load_bundle(export_workflow(store.path, arguments["request"].workflow_id, tmp_path / "infeasible"))
        assert replayed.status == "infeasible"
        assert replayed.jobs == replayed.attempts == replayed.plan_history == ()
        assert replayed.decisions[0]["report"]["result"]["winner"] is None
    finally:
        store.close()


def test_preparation_failure_is_retained_without_a_fake_decision(planned_inputs, tmp_path):
    arguments, context, config, _ = planned_inputs
    report = {"schema_version": 1, "record_type": "PlanningPreparationFailure",
              "service_version": PLANNING_SERVICE_VERSION, "experiment_mode": "live", "inference_performed": False,
              "request": arguments["request"].to_dict(), "now": NOW.isoformat(), "trigger": "submission",
              "error_code": "invalid-planning-input"}
    report["fingerprint"] = fingerprint(report)
    store = QueueStore(tmp_path / "failed.sqlite3")
    try:
        store.enqueue_planned(arguments["request"], None, configuration=config, planning_context=context,
                              decision=report, now=NOW)
        replayed = load_bundle(export_workflow(store.path, arguments["request"].workflow_id, tmp_path / "failed"))
        assert replayed.status == "planning-failed" and replayed.jobs == ()
        assert replayed.decisions[0]["report"] == report
    finally:
        store.close()


def test_infeasible_submission_can_later_install_a_feasible_revision(planned_inputs, tmp_path):
    arguments, context, config, prepared = planned_inputs
    initial = prepare_plan(**{**arguments, "health": {}})
    assert initial.result.winner is None
    store = QueueStore(tmp_path / "bootstrap.sqlite3")
    try:
        store.enqueue_planned(arguments["request"], None, configuration=config, planning_context=context,
                              decision=initial.report, now=NOW)
        token = store.planning_snapshot(arguments["request"].workflow_id, now=NOW).token
        store.install_plan(arguments["request"].workflow_id,
            tuple(item.candidate for item in prepared.result.winner.assignments), prepared.report,
            expected_token=token, now=NOW)
        replayed = load_bundle(export_workflow(store.path, arguments["request"].workflow_id, tmp_path / "bootstrapped"))
        assert replayed.status == "scheduled" and len(replayed.jobs) == 4
        assert replayed.decisions[0]["report"]["result"]["winner"] is None
        assert replayed.decisions[1]["report"]["result"]["winner"] is not None
    finally:
        store.close()


def test_cancelled_infeasible_submission_keeps_its_empty_execution_history(planned_inputs, tmp_path):
    arguments, context, config, _ = planned_inputs
    report = prepare_plan(**{**arguments, "health": {}}).report
    store = QueueStore(tmp_path / "cancelled.sqlite3")
    try:
        store.enqueue_planned(arguments["request"], None, configuration=config, planning_context=context,
                              decision=report, now=NOW)
        store.cancel(arguments["request"].workflow_id, now=at(1))
        replayed = load_bundle(export_workflow(store.path, arguments["request"].workflow_id, tmp_path / "cancelled"))
        assert replayed.status == "cancelled" and replayed.jobs == replayed.attempts == ()
        assert replayed.events[-1]["kind"] == "cancelled"
    finally:
        store.close()


def test_unassigned_replay_cannot_invent_retry_events(planned_inputs, tmp_path):
    arguments, context, config, _ = planned_inputs
    report = prepare_plan(**{**arguments, "health": {}}).report
    store = QueueStore(tmp_path / "unassigned.sqlite3")
    try:
        store.enqueue_planned(arguments["request"], None, configuration=config, planning_context=context,
                              decision=report, now=NOW)
        bundle = export_workflow(store.path, arguments["request"].workflow_id, tmp_path / "unassigned")
        rewrite(bundle, lambda value: value["events"].append({"step_id": "classify",
            "kind": "retry_known_not_dispatched", "recorded_at": NOW.isoformat()}))
        with pytest.raises(ValueError, match="Unassigned"):
            load_bundle(bundle)
    finally:
        store.close()


def complete_classification(planned_queue):
    store, arguments, _, _, prepared = planned_queue
    token = store.planning_snapshot(arguments["request"].workflow_id, now=NOW).token
    claim = store.install_plan(arguments["request"].workflow_id,
        tuple(item.candidate for item in prepared.result.winner.assignments), prepared.report,
        expected_token=token, now=NOW, worker_id="planned-test", lease_seconds=60)
    assert claim is not None
    rendered = prepared.rendered_steps["classify"]
    claim = store.mark_running(claim, now=NOW, rendered_step=rendered.step, source_ids=rendered.source_ids,
                               input_fingerprint=rendered.input_fingerprint, lookup_seconds=rendered.lookup_seconds)
    attempt = ExecutionAttempt("planned-classify", arguments["request"].workflow_id, claim.candidate, "completed",
        actual_start=NOW, actual_finish=at(1), executed_model_id="simulated:1", executed_deployment_id="local",
        output="account_access", input_tokens=10, output_tokens=1, output_limit_tokens=rendered.step.output_limit_tokens,
        enable_thinking=rendered.step.enable_thinking, completion_reason="stop")
    store.complete(claim, attempt, now=at(1))
    return attempt


def test_dispatched_job_and_attempt_have_prior_materialized_decisions(planned_queue, tmp_path):
    attempt = complete_classification(planned_queue)
    replayed = load_bundle(export(planned_queue, tmp_path))
    assert replayed.jobs[0].status == "completed" and replayed.attempts == (attempt,)
    assert len(replayed.decisions) == 2 and replayed.decisions[1]["snapshot_token"]


@pytest.mark.parametrize("mutate", [
    lambda value: value.update(decisions=[]),
    lambda value: value["decisions"][0].update(revision=True),
    lambda value: value["decisions"][0].update(revision=2),
    lambda value: value["decisions"][0].update(snapshot_token="a" * 64),
    lambda value: value["decisions"][0].update(recorded_at=at(-1).isoformat()),
    lambda value: value.update(planning_context=None),
    lambda value: value["planning_context"].update(API_KEY="not-allowed"),
    lambda value: value["planning_context"]["profiles"].update(fingerprint="a" * 64),
    lambda value: value["jobs"][0]["candidate"].update(candidate_id="unselected-candidate"),
])
def test_rehashed_planning_corruption_is_rejected(planned_queue, tmp_path, mutate):
    bundle = export(planned_queue, tmp_path)
    rewrite(bundle, mutate)
    with pytest.raises(ValueError):
        load_bundle(bundle)


def test_bundle_version_cannot_strip_planning_evidence(planned_queue, tmp_path):
    bundle = export(planned_queue, tmp_path)
    def strip(value):
        value.update(bundle_version=1)
        value.pop("planning_context")
        value.pop("decisions")
    rewrite(bundle, strip, change_manifest=lambda manifest: manifest.update(bundle_version=1))
    with pytest.raises(ValueError, match="strip"):
        load_bundle(bundle)


def test_manifest_and_payload_bundle_versions_must_agree(planned_queue, tmp_path):
    bundle = export(planned_queue, tmp_path)
    rewrite(bundle, lambda value: None, change_manifest=lambda manifest: manifest.update(bundle_version=1))
    with pytest.raises(ValueError, match="versions"):
        load_bundle(bundle)


def test_edited_materialized_output_bindings_fail_even_after_bundle_rehash(planned_queue, tmp_path):
    complete_classification(planned_queue)
    bundle = export(planned_queue, tmp_path)
    rewrite(bundle, lambda value: value["jobs"][0]["rendered_step"].update(prompt="A different materialized prompt."))
    with pytest.raises(ValueError, match="materialized"):
        load_bundle(bundle)


def test_rehashed_decision_cannot_refer_to_a_nonexistent_previous_winner(planned_queue, tmp_path):
    complete_classification(planned_queue)
    bundle = export(planned_queue, tmp_path)
    def edit(value):
        report = value["decisions"][1]["report"]
        report["previous_plan_fingerprint"] = "b" * 64
        report["fingerprint"] = fingerprint({key: item for key, item in report.items() if key != "fingerprint"})
    rewrite(bundle, edit)
    with pytest.raises(ValueError, match="previous"):
        load_bundle(bundle)


def test_planned_inspection_imports_no_provider_transports_and_never_waits(planned_queue, tmp_path):
    bundle = export(planned_queue, tmp_path)
    script = """
import builtins, socket, sys, time
original_import = builtins.__import__
def guarded(name, *args, **kwargs):
    if name in {'workflow.worker', 'workflow.planned_worker', 'workflow.deployment_clients', 'workflow.model_clients', 'requests'}:
        raise AssertionError('Forbidden replay import: ' + name)
    return original_import(name, *args, **kwargs)
builtins.__import__ = guarded
def forbidden(*args, **kwargs):
    raise AssertionError('Replay attempted network/inference/wait')
socket.create_connection = socket.socket = forbidden
time.sleep = forbidden
from workflow.queue_replay import main
raise SystemExit(main(['inspect', '--bundle', sys.argv[1]]))
"""
    result = subprocess.run([sys.executable, "-c", script, str(bundle)], capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
    summary = json.loads(result.stdout)
    assert summary["scope"] == "stored planned-queue evidence; no policy comparison"


def test_curated_legacy_bundle_is_still_readable_without_modification():
    bundle = Path(__file__).resolve().parents[1] / "results/demo/a2-local-restart-20261019"
    before = {path.name: path.read_bytes() for path in bundle.iterdir()}
    replayed = load_bundle(bundle)
    assert replayed.status == "completed" and len(replayed.attempts) == 4
    assert replayed.planning_context is None and replayed.decisions == ()
    assert before == {path.name: path.read_bytes() for path in bundle.iterdir()}


def test_read_only_export_accepts_old_database_without_planning_extension(legacy_queued, tmp_path):
    store, request, _ = legacy_queued
    for table in ("planning_decisions", "planning_contexts", "planning_meta"):
        store.connection.execute(f"DROP TABLE {table}")
    replayed = load_bundle(export_workflow(store.path, request.workflow_id, tmp_path / "legacy-db"))
    assert replayed.status == "scheduled" and replayed.planning_context is None
    names = {row[0] for row in store.connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "planning_meta" not in names  # Read-only export never migrates the source.
