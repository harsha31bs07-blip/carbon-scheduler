"""Behavioral M2 coverage: actual dependency data, deadlines and safe restart."""

from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json
import re
from uuid import uuid4

import pytest

from shared.deployments import Deployment, DeploymentRegistry
from shared.scheduling import ExecutionAttempt
from workflow.connected import RUNNER_VERSION, execute_workflow, workload_fingerprint
from workflow.workload import KnowledgeBase, build_workflow_request
from workflow.workflow_store import WorkflowStore


NOW = datetime(2026, 10, 19, 10, tzinfo=timezone.utc)
TICKET = "My CarbonDesk account is locked after failed login attempts and I cannot reset my password."


class ManualClock:
    def __init__(self, now=NOW):
        self.time = now

    def now(self):
        return self.time


def setup_run(tmp_path, *, all_urgent=False, response_budget=30, completion_budget=90, thinking=False):
    kb = KnowledgeBase.load()
    registry = DeploymentRegistry([Deployment("edge", "ollama", "http://edge:11434", {"small": "tiny:3b"})])
    request = build_workflow_request(
        TICKET, workflow_id="ticket-1", arrived_at=NOW, kb=kb,
        response_budget_seconds=response_budget, completion_budget_seconds=completion_budget,
        all_urgent=all_urgent, enable_thinking=thinking,
    )
    store = WorkflowStore.create(
        tmp_path / "run", request, registry.config_fingerprint(), workload_fingerprint(request, kb),
        execution_config={"deployment_id": "edge", "model_id": "small", "timeout_seconds": 60,
                          "runner_version": RUNNER_VERSION},
    )
    return store, registry, kb


def fake_provider(clock, calls, *, bad_step=None, fail_step=None, seconds=1):
    def dispatch(candidate, registry, prompt, **settings):
        calls.append((candidate.step_id, prompt, settings))
        started = clock.now()
        clock.time += timedelta(seconds=seconds)
        if candidate.step_id == fail_step:
            return ExecutionAttempt(
                uuid4().hex, settings["workflow_id"], candidate, "failed", error="request-timeout",
                actual_start=started, actual_finish=clock.now(), executed_model_id="tiny:3b",
                executed_deployment_id="edge",
                output_limit_tokens=settings["output_limit_tokens"], enable_thinking=settings["enable_thinking"],
            )
        citations = sorted(set(re.findall(r"KB-[A-Z0-9]+(?:-[A-Z0-9]+)*", prompt)))
        cite = f" [{citations[0]}]" if citations else ""
        output = {"classify": "account_access", "retrieve": "retrieved-account-evidence" + cite,
                  "draft": "draft-for-actual-customer" + cite,
                  "summarize": "crm-of-actual-draft" + cite}[candidate.step_id]
        if candidate.step_id == bad_step:
            output = "unrecognized-category" if bad_step == "classify" else ""
        return ExecutionAttempt(
            uuid4().hex, settings["workflow_id"], candidate, "completed",
            actual_start=started, actual_finish=clock.now(),
            executed_model_id="tiny:3b", executed_deployment_id="edge", output=output,
            output_limit_tokens=settings["output_limit_tokens"], enable_thinking=settings["enable_thinking"],
        )
    return dispatch


def test_data_flows_to_downstream_prompts_and_saved_evidence(tmp_path):
    store, registry, kb = setup_run(tmp_path)
    calls, clock = [], ManualClock()
    result = execute_workflow(store, registry, kb, clock=clock, dispatcher=fake_provider(clock, calls))
    assert result["status"] == "completed"
    assert [call[0] for call in calls] == ["classify", "retrieve", "draft", "summarize"]
    prompts = {step: prompt for step, prompt, _ in calls}
    assert TICKET in prompts["classify"] and TICKET in prompts["retrieve"]
    assert "account_access" in prompts["retrieve"]
    assert "retrieved-account-evidence" in prompts["draft"]
    assert "draft-for-actual-customer" in prompts["summarize"]
    assert "retrieved-account-evidence" in prompts["summarize"]
    state = WorkflowStore.load(store.directory).state()
    assert len(state["attempts"]) == 4
    assert state["steps"]["retrieve"]["source_ids"]
    assert state["steps"]["retrieve"]["lookup_seconds"] >= 0
    for step, _, settings in calls:
        rendered = state["steps"][step]["rendered_request"]
        assert settings["output_limit_tokens"] == rendered["output_limit_tokens"]
        assert settings["enable_thinking"] is False
        assert settings["latest_finish"] == next(s.latest_finish for s in store.request.steps if s.step_id == step)
        assert rendered["input_tokens"] > 0
        assert rendered["input_size_source"].startswith("estimated:")
        assert state["steps"][step]["input_fingerprint"]


def test_response_can_be_saved_then_background_resumed_without_regeneration(tmp_path):
    store, registry, kb = setup_run(tmp_path)
    calls, clock = [], ManualClock()
    dispatcher = fake_provider(clock, calls)
    response = execute_workflow(store, registry, kb, until_response=True, clock=clock, dispatcher=dispatcher)
    assert response["status"] == "response_ready" and response["response"]
    assert response["summary"] is None
    assert [item[0] for item in calls] == ["classify", "retrieve", "draft"]
    finished = execute_workflow(WorkflowStore.load(store.directory), registry, kb, clock=clock, dispatcher=dispatcher)
    assert finished["status"] == "completed" and finished["summary"]
    assert [item[0] for item in calls] == ["classify", "retrieve", "draft", "summarize"]
    execute_workflow(WorkflowStore.load(store.directory), registry, kb, clock=clock,
                     dispatcher=lambda *a, **k: pytest.fail("Completed run regenerated output"))


def test_all_urgent_requires_summary_before_response_ready(tmp_path):
    store, registry, kb = setup_run(tmp_path, all_urgent=True)
    calls, clock = [], ManualClock()
    result = execute_workflow(store, registry, kb, until_response=True, clock=clock,
                              dispatcher=fake_provider(clock, calls))
    assert result["status"] == "completed" and result["summary"]
    assert len(calls) == 4
    assert all(step.required_for_response and not step.allow_deferral for step in store.request.steps)


@pytest.mark.parametrize("bad_step, expected_calls", [("classify", 1), ("retrieve", 2), ("draft", 3), ("summarize", 4)])
def test_invalid_output_is_retained_but_never_used_downstream(tmp_path, bad_step, expected_calls):
    store, registry, kb = setup_run(tmp_path)
    calls, clock = [], ManualClock()
    result = execute_workflow(store, registry, kb, clock=clock,
                              dispatcher=fake_provider(clock, calls, bad_step=bad_step))
    assert result["status"] == "failed" and result["error_code"] == "step-output-invalid"
    assert len(calls) == expected_calls
    state = WorkflowStore.load(store.directory).state()
    assert state["steps"][bad_step]["status"] == "failed"
    assert state["attempts"][-1]["status"] == "completed"  # Provider succeeded; validation failed.
    assert bad_step not in store.completed_outputs()


def test_provider_failure_stops_and_does_not_automatically_retry(tmp_path):
    store, registry, kb = setup_run(tmp_path)
    calls, clock = [], ManualClock()
    result = execute_workflow(store, registry, kb, clock=clock,
                              dispatcher=fake_provider(clock, calls, fail_step="retrieve"))
    assert result["status"] == "failed" and result["error_code"] == "request-timeout"
    assert [item[0] for item in calls] == ["classify", "retrieve"]
    resumed = execute_workflow(WorkflowStore.load(store.directory), registry, kb, clock=clock,
                               dispatcher=lambda *a, **k: pytest.fail("Failed inference was retried"))
    assert resumed["status"] == "failed"


def test_crash_after_begin_requires_reconciliation_and_never_repeats_dispatch(tmp_path):
    store, registry, kb = setup_run(tmp_path)

    def crash(*args, **kwargs):
        raise RuntimeError("simulated crash after external acceptance")

    with pytest.raises(RuntimeError, match="simulated crash"):
        execute_workflow(store, registry, kb, clock=ManualClock(), dispatcher=crash)
    restarted = WorkflowStore.load(store.directory)
    assert restarted.state()["status"] == "needs-reconciliation"
    result = execute_workflow(restarted, registry, kb, clock=ManualClock(),
                              dispatcher=lambda *a, **k: pytest.fail("Ambiguous call repeated"))
    assert result["status"] == "needs-reconciliation"
    assert not restarted.completed_outputs()


def test_expired_deadline_prevents_dispatch(tmp_path):
    store, registry, kb = setup_run(tmp_path)
    result = execute_workflow(store, registry, kb, clock=ManualClock(NOW + timedelta(seconds=31)),
                              dispatcher=lambda *a, **k: pytest.fail("Expired job dispatched"))
    assert result["status"] == "deadline_missed"
    assert not store.state()["attempts"]


def test_overrun_is_visible_and_consumes_remaining_deadline(tmp_path):
    store, registry, kb = setup_run(tmp_path, response_budget=2)
    calls, clock = [], ManualClock()
    result = execute_workflow(store, registry, kb, clock=clock,
                              dispatcher=fake_provider(clock, calls, seconds=3))
    assert result["status"] == "deadline_missed"
    assert len(calls) == 1
    assert calls[0][2]["timeout_seconds"] == 2
    assert store.state()["attempts"][0]["output"] == "account_access"


@pytest.mark.parametrize("failure", ["provider", "invalid-output"])
def test_failed_or_invalid_late_output_still_reports_missed_deadline(tmp_path, failure):
    store, registry, kb = setup_run(tmp_path, response_budget=2)
    calls, clock = [], ManualClock()
    result = execute_workflow(store, registry, kb, clock=clock, dispatcher=fake_provider(
        clock, calls, seconds=3, fail_step="classify" if failure == "provider" else None,
        bad_step="classify" if failure == "invalid-output" else None,
    ))
    assert result["status"] == "deadline_missed"
    assert len(calls) == 1
    assert store.state()["steps"]["classify"]["status"] == "failed"
    assert len(store.state()["attempts"]) == 1


def test_journal_overhead_cannot_dispatch_after_deadline(tmp_path, monkeypatch):
    store, registry, kb = setup_run(tmp_path, response_budget=2)
    clock = ManualClock()
    original = store.begin_step

    def slow_journal(*args, **kwargs):
        original(*args, **kwargs)
        clock.time += timedelta(seconds=3)

    monkeypatch.setattr(store, "begin_step", slow_journal)
    result = execute_workflow(store, registry, kb, clock=clock,
                              dispatcher=lambda *a, **k: pytest.fail("Journal consumed dispatch budget"))
    assert result["status"] == "deadline_missed"
    attempt = store.state()["attempts"][0]
    assert attempt["error"] == "step-deadline-exceeded" and attempt["actual_start"] is None
    assert attempt["executed_deployment_id"] is None


def test_validation_and_persistence_overhead_count_toward_completion(tmp_path, monkeypatch):
    store, registry, kb = setup_run(tmp_path, response_budget=2)
    calls, clock = [], ManualClock()
    original = store.finish_step

    def slow_finish(*args, **kwargs):
        original(*args, **kwargs)
        clock.time += timedelta(seconds=3)

    monkeypatch.setattr(store, "finish_step", slow_finish)
    result = execute_workflow(store, registry, kb, clock=clock, dispatcher=fake_provider(clock, calls))
    assert result["status"] == "deadline_missed" and len(calls) == 1


def test_truncated_generation_retains_usage_and_text_but_blocks_downstream(tmp_path):
    store, registry, kb = setup_run(tmp_path)
    calls, clock = [], ManualClock()
    provider = fake_provider(clock, calls)

    def truncated(*args, **kwargs):
        return replace(provider(*args, **kwargs), completion_reason="length", output_tokens=24)

    result = execute_workflow(store, registry, kb, clock=clock, dispatcher=truncated)
    assert result["status"] == "failed" and result["error_code"] == "step-output-truncated"
    assert len(calls) == 1 and not store.completed_outputs()
    assert store.state()["attempts"][0]["output"] == "account_access"
    assert store.state()["attempts"][0]["output_tokens"] == 24


def test_background_deadline_is_independent_of_successful_response(tmp_path):
    store, registry, kb = setup_run(tmp_path)
    calls, clock = [], ManualClock()
    execute_workflow(store, registry, kb, until_response=True, clock=clock, dispatcher=fake_provider(clock, calls))
    clock.time = NOW + timedelta(seconds=91)
    result = execute_workflow(WorkflowStore.load(store.directory), registry, kb, clock=clock,
                              dispatcher=lambda *a, **k: pytest.fail("Late background step dispatched"))
    assert result["status"] == "deadline_missed" and result["response"]
    assert result["summary"] is None


def test_resume_rejects_changed_deployment_or_kb(tmp_path):
    store, registry, kb = setup_run(tmp_path)
    changed = DeploymentRegistry([replace(registry.get("edge"), endpoint="http://elsewhere:11434")])
    with pytest.raises(ValueError, match="Deployment configuration"):
        execute_workflow(store, changed, kb, clock=ManualClock())
    changed_kb = replace(kb, version="different-kb")
    with pytest.raises(ValueError, match="Workload or knowledge base"):
        execute_workflow(store, registry, changed_kb, clock=ManualClock())


def test_cli_resume_rejects_changed_settings_without_dispatch(tmp_path, capsys, monkeypatch):
    from workflow import connected
    store, registry, kb = setup_run(tmp_path)
    monkeypatch.setattr(connected, "load_deployments", lambda *a: registry)
    assert connected.main(["--resume", str(store.directory), "--model", "different"]) == 2
    assert json.loads(capsys.readouterr().out)["error_code"] == "invalid-workflow-configuration-or-state"
