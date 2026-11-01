"""Restart and integrity checks for the single-run connected workflow store."""

from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json

import pytest

from shared.scheduling import (
    ExecutionAttempt, MetricEstimate, PlannedCandidate, StepEstimates,
    StepRequest, WorkflowRequest,
)
from workflow.workflow_store import (
    WorkflowLockedError, WorkflowReconciliationError, WorkflowStore,
)


NOW = datetime(2026, 10, 19, 12, tzinfo=timezone.utc)
CONFIG = {"deployment_id": "ollama-a", "model_id": "local-llama3.2-3b",
          "timeout_seconds": 30, "runner_version": "connected-v1"}


@pytest.fixture
def workflow_request():
    steps = tuple(
        StepRequest(step_id=step_id, task_type=step_id, prompt=f"Template {step_id}",
                    input_tokens=None, output_limit_tokens=128, earliest_start=NOW,
                    latest_finish=NOW + timedelta(minutes=5),
                    dependencies=(() if index == 0 else (names[index - 1],)),
                    required_for_response=step_id != "summarize")
        for names in [("classify", "retrieve", "draft", "summarize")]
        for index, step_id in enumerate(names)
    )
    return WorkflowRequest("case-1", "My order is late", NOW, "balanced",
                           NOW + timedelta(minutes=5), NOW + timedelta(minutes=10),
                           "support-v2", "constraints-v2", steps)


@pytest.fixture
def store(tmp_path, workflow_request):
    return WorkflowStore.create(tmp_path / "case-1", workflow_request, "registry-abc", "workload-def", CONFIG)


def rendered(workflow_request, step_id="classify"):
    return replace(next(step for step in workflow_request.steps if step.step_id == step_id),
                   prompt="Actual dependency-aware prompt", input_tokens=7,
                   input_size_source="fixture-tokenizer")


def attempt(step_id="classify", *, index=0, status="completed", output="Valid output", **kwargs):
    unknown = lambda unit: MetricEstimate(None, unit, "unknown")
    estimates = StepEstimates(unknown("s"), unknown("ratio"), unknown("USD"),
                             unknown("kWh"), unknown("gCO2e"))
    start = NOW + timedelta(seconds=index * 2)
    plan = PlannedCandidate(f"candidate-{index}-{step_id}", step_id, CONFIG["model_id"],
                            CONFIG["deployment_id"], start, estimates)
    values = {"attempt_id": f"attempt-{index}-{step_id}", "workflow_id": "case-1",
              "planned_candidate": plan, "status": status,
              "actual_start": start, "actual_finish": start + timedelta(seconds=1),
              "executed_model_id": CONFIG["model_id"], "executed_deployment_id": CONFIG["deployment_id"],
              "output_limit_tokens": 128,
              "output": output if status == "completed" else None,
              "error": None if status == "completed" else "provider-failed"}
    values.update(kwargs)
    return ExecutionAttempt(**values)


def begin(store, workflow_request, step_id="classify"):
    store.begin_step(rendered(workflow_request, step_id), source_ids=("KB-1",),
                     lookup_seconds=0.05, input_fingerprint=f"input-{step_id}")


def test_create_persists_request_and_safe_selection(store, workflow_request):
    loaded = WorkflowStore.load(store.directory)
    assert loaded.request == workflow_request
    assert loaded.state()["status"] == "pending"
    assert loaded.state()["registry_fingerprint"] == "registry-abc"
    assert loaded.state()["workload_fingerprint"] == "workload-def"
    assert loaded.state()["execution_config"] == CONFIG
    assert loaded.completed_outputs() == {}
    # Returned values cannot edit the persisted or in-memory request/selection.
    snapshot = loaded.state()
    snapshot["execution_config"]["model_id"] = "different"
    assert loaded.state()["execution_config"] == CONFIG


def test_duplicate_creation_does_not_overwrite_existing_state(store, workflow_request):
    before = store.path.read_bytes()
    with pytest.raises(FileExistsError):
        WorkflowStore.create(store.directory, workflow_request, "different", "different")
    assert store.path.read_bytes() == before


def test_mutations_require_exclusive_lock(store, workflow_request):
    with pytest.raises(WorkflowLockedError):
        begin(store, workflow_request)
    with pytest.raises(WorkflowLockedError):
        store.mark_status("running")
    assert store.state()["status"] == "pending"


def test_lock_conflict_and_stale_lock_are_never_broken(store):
    other = WorkflowStore.load(store.directory)
    with store.exclusive():
        with pytest.raises(WorkflowLockedError):
            with other.exclusive():
                pytest.fail("conflicting runner obtained the lock")
        with pytest.raises(WorkflowLockedError):
            with store.exclusive():
                pytest.fail("nested lock obtained")
    lock = store.directory / ".workflow.lock"
    lock.write_text("previous-process", encoding="ascii")
    with pytest.raises(WorkflowLockedError):
        with other.exclusive():
            pytest.fail("stale lock automatically broken")
    assert lock.read_text(encoding="ascii") == "previous-process"


def test_completed_output_and_attempt_are_atomic_and_survive_restart(store, workflow_request):
    with store.exclusive():
        begin(store, workflow_request)
        persisted = json.loads(store.path.read_text(encoding="utf-8"))
        assert persisted["steps"]["classify"]["status"] == "running"
        assert persisted["steps"]["classify"]["rendered_request"]["prompt"] == rendered(workflow_request).prompt
        store.finish_step(attempt())
    loaded = WorkflowStore.load(store.directory)
    assert loaded.completed_outputs() == {"classify": "Valid output"}
    with loaded.exclusive():
        with pytest.raises(ValueError, match="pending or explicitly failed"):
            begin(loaded, workflow_request)
    assert len(loaded.state()["attempts"]) == 1
    metadata = loaded.state()["steps"]["classify"]
    assert metadata["source_ids"] == ["KB-1"]
    assert metadata["lookup_seconds"] == 0.05
    assert metadata["input_fingerprint"] == "input-classify"


def test_loaded_running_step_requires_reconciliation_and_keeps_completed_output(store, workflow_request):
    with store.exclusive():
        begin(store, workflow_request)
        store.finish_step(attempt())
        begin(store, workflow_request, "retrieve")
    # Releasing the lock without a terminal attempt models a process interruption.
    loaded = WorkflowStore.load(store.directory)
    assert loaded.state()["status"] == "needs-reconciliation"
    assert loaded.completed_outputs() == {"classify": "Valid output"}
    with loaded.exclusive():
        with pytest.raises(WorkflowReconciliationError):
            begin(loaded, workflow_request, "retrieve")
        with pytest.raises(WorkflowReconciliationError):
            loaded.mark_status("running")
        loaded.mark_status("needs-reconciliation", error_code="interrupted-step")
    assert WorkflowStore.load(store.directory).state()["error_code"] == "interrupted-step"


def test_exclusive_refresh_prevents_stale_store_overwriting_completed_output(store, workflow_request):
    stale = WorkflowStore.load(store.directory)
    with store.exclusive():
        begin(store, workflow_request)
        store.finish_step(attempt())
    with stale.exclusive():
        assert stale.completed_outputs() == {"classify": "Valid output"}
        begin(stale, workflow_request, "retrieve")
        stale.finish_step(attempt("retrieve", index=1))
    assert WorkflowStore.load(store.directory).completed_outputs() == {
        "classify": "Valid output", "retrieve": "Valid output",
    }


def test_invalid_model_output_is_evidence_but_never_a_completed_dependency(store, workflow_request):
    with store.exclusive():
        begin(store, workflow_request)
        store.finish_step(attempt(output="not-valid-json"), output_valid=False,
                          validation_error="invalid-classification")
        with pytest.raises(ValueError, match="incomplete dependency"):
            begin(store, workflow_request, "retrieve")
    loaded = WorkflowStore.load(store.directory)
    assert loaded.state()["status"] == "failed"
    assert loaded.completed_outputs() == {}
    assert loaded.state()["attempts"][0]["output"] == "not-valid-json"
    assert loaded.state()["steps"]["classify"]["validation_error"] == "invalid-classification"


def test_explicit_failed_step_retry_does_not_erase_original_attempt(store, workflow_request):
    with store.exclusive():
        begin(store, workflow_request)
        store.finish_step(attempt(status="failed"))
        begin(store, workflow_request)
        store.finish_step(attempt(index=1))
    loaded = WorkflowStore.load(store.directory)
    assert loaded.completed_outputs() == {"classify": "Valid output"}
    assert [item["status"] for item in loaded.state()["attempts"]] == ["failed", "completed"]


def test_explicit_retry_cannot_replace_the_failed_attempts_input(store, workflow_request):
    with store.exclusive():
        begin(store, workflow_request)
        store.finish_step(attempt(status="failed"))
        with pytest.raises(ValueError, match="preserve.*input identity"):
            store.begin_step(replace(rendered(workflow_request), prompt="New prompt"),
                             source_ids=("KB-1",), lookup_seconds=0.05,
                             input_fingerprint="different-input")
    assert store.state()["steps"]["classify"]["rendered_request"]["prompt"] == rendered(workflow_request).prompt


@pytest.mark.parametrize("change", [
    {"output_limit_tokens": 500}, {"enable_thinking": True},
    {"latest_finish": NOW + timedelta(minutes=4)}, {"dependencies": ("retrieve",)},
])
def test_rendering_cannot_change_frozen_execution_constraints(store, workflow_request, change):
    with store.exclusive():
        with pytest.raises(ValueError, match="immutable request constraints"):
            store.begin_step(replace(rendered(workflow_request), **change), source_ids=(),
                             lookup_seconds=0, input_fingerprint="input-a")
    assert store.state()["steps"]["classify"]["status"] == "pending"


def test_finish_cannot_record_an_unrelated_workflow(store, workflow_request):
    with store.exclusive():
        begin(store, workflow_request)
        with pytest.raises(ValueError, match="different workflow"):
            store.finish_step(attempt(workflow_id="some-other-case"))
        assert store.state()["attempts"] == []
        store.finish_step(attempt())


@pytest.mark.parametrize("changes", [
    {"executed_deployment_id": "elsewhere"}, {"output_limit_tokens": 1024},
    {"enable_thinking": True},
])
def test_finish_cannot_change_saved_routing_or_generation_settings(store, workflow_request, changes):
    with store.exclusive():
        begin(store, workflow_request)
        with pytest.raises(ValueError):
            store.finish_step(attempt(**changes))
        assert store.state()["attempts"] == []
        store.finish_step(attempt())


def test_completion_and_response_status_require_their_actual_outputs(store, workflow_request):
    with store.exclusive():
        with pytest.raises(ValueError, match="incomplete steps"):
            store.mark_status("completed")
        with pytest.raises(ValueError, match="incomplete response"):
            store.mark_status("response_ready")
        for index, step_id in enumerate(("classify", "retrieve", "draft")):
            begin(store, workflow_request, step_id)
            store.finish_step(attempt(step_id, index=index))
        store.mark_status("response_ready")
        assert store.state()["status"] == "response_ready"
        begin(store, workflow_request, "summarize")
        store.finish_step(attempt("summarize", index=3))
        store.mark_status("completed")
        with pytest.raises(ValueError, match="cannot be reopened"):
            store.mark_status("running")
    assert len(WorkflowStore.load(store.directory).completed_outputs()) == 4


def test_failed_atomic_replace_retains_last_good_state(store, workflow_request, monkeypatch):
    import workflow.workflow_store as module

    before = store.path.read_bytes()
    with store.exclusive():
        def unavailable(*args):
            raise OSError("fixture storage failure")
        monkeypatch.setattr(module.os, "replace", unavailable)
        with pytest.raises(OSError):
            begin(store, workflow_request)
        assert store.state()["status"] == "pending"
        assert store.path.read_bytes() == before
    assert list(store.directory.glob(".workflow-*.tmp")) == []


@pytest.mark.parametrize("payload", [
    '{"schema_version": 3, "schema_version": 3}',
    '{"value": NaN}', '{"value": Infinity}', '[1, 2]', '{not-json}',
])
def test_invalid_json_fails_closed_before_resume(store, payload):
    store.path.write_text(payload, encoding="utf-8")
    with pytest.raises(ValueError):
        WorkflowStore.load(store.directory)


@pytest.mark.parametrize("corrupt", [
    lambda value: value["steps"]["classify"].update(status="completed"),
    lambda value: value["steps"].update(extra=value["steps"]["classify"]),
    lambda value: value.update(status="completed"),
    lambda value: value.update(registry_fingerprint=""),
    lambda value: value["execution_config"].update(api_key="must-never-be-persisted"),
])
def test_inconsistent_or_credential_bearing_state_is_rejected(store, corrupt):
    value = store.state()
    corrupt(value)
    store.path.write_text(json.dumps(value), encoding="utf-8")
    with pytest.raises(ValueError):
        WorkflowStore.load(store.directory)


def test_corrupt_attempt_or_blank_completed_output_cannot_be_resumed(store, workflow_request):
    with store.exclusive():
        begin(store, workflow_request)
        store.finish_step(attempt())
    value = store.state()
    value["attempts"][0]["output"] = "  "
    store.path.write_text(json.dumps(value), encoding="utf-8")
    with pytest.raises(ValueError, match="successful, validated"):
        WorkflowStore.load(store.directory)


def test_attempt_chronology_must_follow_completed_prerequisites(store, workflow_request):
    with store.exclusive():
        begin(store, workflow_request)
        store.finish_step(attempt())
        begin(store, workflow_request, "retrieve")
        with pytest.raises(ValueError, match="before its dependency"):
            store.finish_step(attempt("retrieve", index=0))
        store.finish_step(attempt("retrieve", index=1))


@pytest.mark.parametrize("code", ["provider failed: secret", "API_KEY=abc", "", 8])
def test_status_error_codes_are_machine_identifiers(store, code):
    with store.exclusive():
        with pytest.raises(ValueError, match="safe machine code"):
            store.mark_status("failed", error_code=code)


def test_late_accepted_output_and_missed_deadline_share_one_atomic_write(store, workflow_request):
    late = NOW + timedelta(minutes=5, seconds=1)
    with store.exclusive():
        begin(store, workflow_request)
        store.finish_step(attempt(actual_finish=late))
        # Simulate stopping immediately after finish_step, before any runner
        # status update. Both the output and missed deadline are already durable.
        persisted = json.loads(store.path.read_text(encoding="utf-8"))
        assert persisted["status"] == "deadline_missed"
        assert persisted["error_code"] == "step-deadline-exceeded"
        assert persisted["steps"]["classify"]["status"] == "completed"
    loaded = WorkflowStore.load(store.directory)
    assert loaded.state()["status"] == "deadline_missed"
    assert loaded.completed_outputs() == {"classify": "Valid output"}
    with loaded.exclusive():
        with pytest.raises(ValueError, match="does not permit"):
            begin(loaded, workflow_request, "retrieve")


def test_final_step_cannot_turn_a_known_missed_deadline_into_completed(store, workflow_request):
    with store.exclusive():
        for index, step_id in enumerate(("classify", "retrieve", "draft", "summarize")):
            begin(store, workflow_request, step_id)
            overrides = {"actual_finish": NOW + timedelta(minutes=5, seconds=1)} if step_id == "summarize" else {}
            store.finish_step(attempt(step_id, index=index, **overrides))
        assert len(store.completed_outputs()) == 4
        with pytest.raises(ValueError, match="Deadline-missed workflows"):
            store.mark_status("completed")
    loaded = WorkflowStore.load(store.directory)
    assert loaded.state()["status"] == "deadline_missed"
    assert len(loaded.completed_outputs()) == 4


def test_late_failed_attempt_preserves_resource_evidence_and_deadline_status(store, workflow_request):
    with store.exclusive():
        begin(store, workflow_request)
        store.finish_step(attempt(status="failed", error="request-failed", provider_seconds=301,
                                  input_tokens=25, actual_finish=NOW + timedelta(seconds=301)),
                          output_valid=False)
    loaded = WorkflowStore.load(store.directory)
    state = loaded.state()
    assert state["status"] == "deadline_missed"
    assert state["error_code"] == "step-deadline-exceeded"
    assert state["steps"]["classify"]["status"] == "failed"
    assert state["attempts"][0]["error"] == "request-failed"
    assert state["attempts"][0]["provider_seconds"] == 301
    assert state["attempts"][0]["input_tokens"] == 25
    assert loaded.completed_outputs() == {}


def test_preflight_expiry_is_durable_without_claiming_provider_execution(store, workflow_request):
    with store.exclusive():
        begin(store, workflow_request)
        expired = attempt(status="failed", error="step-deadline-exceeded", actual_start=None,
                          actual_finish=None, executed_model_id=None, executed_deployment_id=None)
        store.finish_step(expired, output_valid=False)
    loaded = WorkflowStore.load(store.directory)
    assert loaded.state()["status"] == "deadline_missed"
    assert loaded.state()["attempts"][0]["actual_start"] is None
    assert loaded.state()["attempts"][0]["executed_deployment_id"] is None


def test_observed_validation_overrun_is_persisted_with_ontime_provider_attempt(store, workflow_request):
    with store.exclusive():
        begin(store, workflow_request)
        store.finish_step(attempt(), deadline_exceeded=True)
    loaded = WorkflowStore.load(store.directory)
    assert loaded.state()["status"] == "deadline_missed"
    assert loaded.completed_outputs() == {"classify": "Valid output"}


@pytest.mark.parametrize("invalid_flag", [None, 1, "true"])
def test_deadline_overrun_flag_must_be_boolean(store, workflow_request, invalid_flag):
    with store.exclusive():
        begin(store, workflow_request)
        with pytest.raises(ValueError, match="deadline_exceeded must be boolean"):
            store.finish_step(attempt(), deadline_exceeded=invalid_flag)
        assert store.state()["attempts"] == []
        store.finish_step(attempt())


@pytest.mark.parametrize("old_status", ["running", "completed", "failed"])
def test_legacy_known_late_final_attempt_is_derived_safely_without_rewriting(store, workflow_request, old_status):
    with store.exclusive():
        for index, step_id in enumerate(("classify", "retrieve", "draft", "summarize")):
            begin(store, workflow_request, step_id)
            store.finish_step(attempt(step_id, index=index))
    historical = store.state()
    historical["status"] = old_status
    historical["error_code"] = None
    historical["attempts"][-1]["actual_finish"] = "2026-10-19T12:05:01Z"
    store.path.write_text(json.dumps(historical), encoding="utf-8")
    before = store.path.read_bytes()
    loaded = WorkflowStore.load(store.directory)
    assert loaded.state()["status"] == "deadline_missed"
    assert loaded.state()["error_code"] == "step-deadline-exceeded"
    assert len(loaded.completed_outputs()) == 4
    with loaded.exclusive():
        assert loaded.state()["status"] == "deadline_missed"
        with pytest.raises(ValueError, match="Deadline-missed workflows"):
            loaded.mark_status("completed")
    assert store.path.read_bytes() == before


def test_legacy_failed_late_attempt_is_derived_as_deadline_missed(store, workflow_request):
    with store.exclusive():
        begin(store, workflow_request)
        store.finish_step(attempt(status="failed"))
    historical = store.state()
    historical["attempts"][0]["actual_finish"] = "2026-10-19T12:05:01Z"
    store.path.write_text(json.dumps(historical), encoding="utf-8")
    loaded = WorkflowStore.load(store.directory)
    assert loaded.state()["status"] == "deadline_missed"
    assert loaded.state()["steps"]["classify"]["status"] == "failed"
    assert loaded.completed_outputs() == {}


def test_legacy_running_ambiguity_takes_precedence_over_a_known_late_attempt(store, workflow_request):
    with store.exclusive():
        begin(store, workflow_request)
        store.finish_step(attempt())
        begin(store, workflow_request, "retrieve")
    historical = store.state()
    historical["attempts"][0]["actual_finish"] = "2026-10-19T12:05:01Z"
    store.path.write_text(json.dumps(historical), encoding="utf-8")
    loaded = WorkflowStore.load(store.directory)
    assert loaded.state()["status"] == "needs-reconciliation"
    assert loaded.completed_outputs() == {"classify": "Valid output"}
