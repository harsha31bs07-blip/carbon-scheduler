"""Worker boundary tests use deterministic provider outputs, never live inference."""

from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json
import re
from uuid import uuid4

import pytest

from shared.deployments import Deployment, DeploymentRegistry
from shared.scheduling import ExecutionAttempt
from workflow.queue_store import LeaseError, QueueStore
from workflow.worker import submit_fixed, work_once
from workflow.workload import KnowledgeBase


NOW = datetime(2026, 10, 19, 12, tzinfo=timezone.utc)
TICKET = "My CarbonDesk account is locked after failed login attempts and I cannot reset my password."


class Clock:
    def __init__(self):
        self.time = NOW

    def now(self):
        return self.time


@pytest.fixture
def setup(tmp_path):
    clock = Clock()
    registry = DeploymentRegistry([Deployment("edge", "ollama", "http://edge:11434", {"small": "tiny:3b"})])
    kb, store = KnowledgeBase.load(), QueueStore(tmp_path / "queue.sqlite3")
    submit_fixed(store, registry, kb, TICKET, deployment_id="edge", model_id="small",
                 clock=clock, workflow_id="ticket", summary_delay_seconds=60)
    yield store, registry, kb, clock
    store.close()


def provider(clock, calls, *, bad=None, truncated=None, callback=None):
    def dispatch(candidate, registry, prompt, **settings):
        calls.append((candidate.step_id, prompt, settings))
        started = clock.now()
        clock.time += timedelta(seconds=1)
        citations = re.findall(r"KB-[A-Z0-9]+(?:-[A-Z0-9]+)*", prompt)
        cite = f" [{citations[0]}]" if citations else ""
        output = {"classify": "account_access", "retrieve": "actual retrieved evidence" + cite,
                  "draft": "actual draft for customer" + cite,
                  "summarize": "actual CRM summary" + cite}[candidate.step_id]
        if bad == candidate.step_id:
            output = "invalid-label" if bad == "classify" else ""
        if callback:
            callback(candidate)
        return ExecutionAttempt(uuid4().hex, settings["workflow_id"], candidate, "completed",
            actual_start=started, actual_finish=clock.now(), output=output,
            executed_model_id="tiny:3b", executed_deployment_id="edge",
            input_tokens=32, output_tokens=8, output_limit_tokens=settings["output_limit_tokens"],
            enable_thinking=settings["enable_thinking"], completion_reason="length" if truncated == candidate.step_id else "stop")
    return dispatch


def work(setup, dispatcher, **kwargs):
    store, registry, kb, clock = setup
    return work_once(store, worker_id="test-worker", registry=registry, kb=kb, clock=clock,
                     dispatcher=dispatcher, heartbeat=False, **kwargs)


def test_delayed_background_survives_closed_connection_without_regeneration(setup):
    store, registry, kb, clock = setup
    calls = []
    dispatch = provider(clock, calls)
    for _ in range(3):
        work(setup, dispatch)
    assert store.read("ticket").status == "response_ready"
    assert work(setup, dispatch) == {"status": "idle"}
    store.close()
    restarted = QueueStore(store.path)
    try:
        clock.time = NOW + timedelta(seconds=60)
        result = work_once(restarted, worker_id="new-worker", clock=clock, dispatcher=dispatch, heartbeat=False)
        state = restarted.read("ticket")
        assert result["status"] == "completed" and len(state.attempts) == 4
        assert [call[0] for call in calls] == ["classify", "retrieve", "draft", "summarize"]
        assert "actual retrieved evidence" in calls[3][1] and "actual draft for customer" in calls[3][1]
        assert state.attempts[-1].actual_start == NOW + timedelta(seconds=60)
        assert state.attempts[-1].wait_seconds == 57
        assert state.jobs[-1].input_fingerprint and state.jobs[-1].rendered_step.input_tokens
        assert work_once(restarted, worker_id="new-worker", clock=clock, heartbeat=False) == {"status": "idle"}
    finally:
        restarted.close()


def test_unexpected_dispatch_crash_never_automatically_repeats(setup):
    store, _, _, clock = setup
    def crash(*args, **kwargs):
        raise RuntimeError("accepted provider call then worker crashed")
    with pytest.raises(RuntimeError):
        work(setup, crash, lease_seconds=5)
    assert store.read("ticket").jobs[0].status == "running"
    clock.time += timedelta(seconds=6)
    assert work(setup, lambda *a, **k: pytest.fail("ambiguous inference repeated")) == {"status": "idle"}
    assert store.read("ticket").status == "needs-reconciliation"


def test_mismatched_registry_fails_before_dispatch(setup):
    store, _, kb, clock = setup
    changed = DeploymentRegistry([Deployment("edge", "ollama", "http://other:11434", {"small": "tiny:3b"})])
    result = work_once(store, worker_id="worker", registry=changed, kb=kb, clock=clock,
        dispatcher=lambda *a, **k: pytest.fail("changed configuration dispatched"), heartbeat=False)
    assert result["status"] == "preflight-failed"
    assert store.read("ticket").jobs[0].dispatched_at is None


@pytest.mark.parametrize("bad", ["classify", "retrieve", "draft", "summarize"])
def test_invalid_output_is_retained_and_blocks_downstream(setup, bad):
    store, _, _, clock = setup
    calls = []
    dispatch = provider(clock, calls, bad=bad)
    if bad == "summarize":
        clock.time += timedelta(seconds=60)
    for _ in range(4):
        work(setup, dispatch)
    state = store.read("ticket")
    assert state.status == "failed"
    assert calls[-1][0] == bad and bad not in state.completed_outputs()
    assert state.attempts[-1].status == "completed"
    assert next(job for job in state.jobs if job.step_request.step_id == bad).output_valid is False


def test_generation_limit_rejects_even_valid_category(setup):
    store, _, _, clock = setup
    work(setup, provider(clock, [], truncated="classify"))
    assert store.read("ticket").jobs[0].validation_error == "step-output-truncated"


def test_preflight_attempt_has_no_fake_execution_times(setup):
    store, _, _, _ = setup
    def rejected(candidate, registry, prompt, **settings):
        return ExecutionAttempt(uuid4().hex, settings["workflow_id"], candidate, "failed",
            error="missing-credential", output_limit_tokens=settings["output_limit_tokens"], enable_thinking=False)
    work(setup, rejected)
    state = store.read("ticket")
    assert state.attempts[0].actual_start is None and state.attempts[0].wait_seconds is None
    assert state.jobs[0].dispatched_at is None and state.status == "failed"


def test_preflight_deadline_at_exact_boundary_is_a_deadline_miss(setup):
    store, _, _, clock = setup
    def rejected(candidate, registry, prompt, **settings):
        clock.time = NOW + timedelta(seconds=300)
        return ExecutionAttempt(uuid4().hex, settings["workflow_id"], candidate, "failed",
            error="step-deadline-exceeded", output_limit_tokens=settings["output_limit_tokens"], enable_thinking=False)
    result = work(setup, rejected, lease_seconds=600)
    assert result["status"] == "deadline_missed"
    assert store.read("ticket").attempts[0].actual_start is None


def test_explicit_retry_sets_correct_attempt_index(setup):
    store, _, _, clock = setup
    work(setup, provider(clock, [], bad="classify"))
    store.retry("ticket", "classify", now=clock.now(), acknowledge_ambiguous=True)
    work(setup, provider(clock, []))
    assert [attempt.retry_index for attempt in store.read("ticket").attempts] == [0, 1]


def test_cancellation_during_provider_retains_usage_and_stops_downstream(setup):
    store, _, _, clock = setup
    dispatch = provider(clock, [], callback=lambda _: store.cancel("ticket", now=clock.now()))
    assert work(setup, dispatch)["status"] == "cancelled"
    assert len(store.read("ticket").attempts) == 1
    assert work(setup, dispatch) == {"status": "idle"}


def test_explicit_ambiguous_retry_during_call_archives_old_usage(setup):
    store, _, _, clock = setup
    def supersede(_):
        clock.time += timedelta(seconds=6)
        store.recover(now=clock.now())
        store.retry("ticket", "classify", now=clock.now(), acknowledge_ambiguous=True)
    result = work(setup, provider(clock, [], callback=supersede), lease_seconds=5)
    state = store.read("ticket")
    assert result["status"] == "late-attempt-archived"
    assert state.jobs[0].status == "scheduled" and state.jobs[0].retry_index == 1
    assert state.jobs[0].output is None and len(state.attempts) == 1


def test_persistence_failure_retains_raw_output_as_orphan(setup, monkeypatch):
    store, _, _, clock = setup
    def unavailable(*args, **kwargs):
        raise OSError("simulated disk error")
    monkeypatch.setattr(store, "complete", unavailable)
    result = work(setup, provider(clock, []))
    evidence = json.loads(__import__("pathlib").Path(result["orphan_attempt"]).read_text())
    assert result["status"] == "needs-reconciliation"
    assert evidence["attempt"]["output"] == "account_access"
    assert store.read("ticket").jobs[0].status == "running"


def test_late_provider_completion_is_retained_as_deadline_missed(setup):
    store, _, _, clock = setup
    dispatch = provider(clock, [], callback=lambda _: setattr(clock, "time", NOW + timedelta(seconds=301)))
    assert work(setup, dispatch)["status"] == "deadline_missed"
    assert len(store.read("ticket").attempts) == 1
    assert work(setup, dispatch) == {"status": "idle"}


def test_deadline_already_expired_never_calls_provider(setup):
    store, _, _, clock = setup
    clock.time += timedelta(seconds=301)
    assert work(setup, lambda *a, **k: pytest.fail("late dispatch")) == {"status": "idle"}
    assert store.read("ticket").status == "deadline_missed"


@pytest.mark.parametrize("delay", [-1, float("nan"), True, 600, 1e308, 10 ** 1000])
def test_invalid_delay_never_enqueues(tmp_path, delay):
    registry = DeploymentRegistry([Deployment("edge", "ollama", "http://edge:11434", {"small": "tiny:3b"})])
    store = QueueStore(tmp_path / "queue.sqlite3")
    try:
        with pytest.raises(ValueError):
            submit_fixed(store, registry, KnowledgeBase.load(), TICKET, deployment_id="edge", model_id="small",
                         clock=Clock(), workflow_id="invalid", summary_delay_seconds=delay)
        assert store.connection.execute("SELECT COUNT(*) FROM workflows").fetchone()[0] == 0
    finally:
        store.close()


def test_saved_retry_limit_prevents_unbounded_manual_calls(setup):
    store, _, _, clock = setup
    calls = []
    dispatch = provider(clock, calls, bad="classify")
    for index in range(4):
        work(setup, dispatch)
        if index < 3:
            store.retry("ticket", "classify", now=clock.now(), acknowledge_ambiguous=True)
    with pytest.raises(ValueError, match="retry limit"):
        store.retry("ticket", "classify", now=clock.now(), acknowledge_ambiguous=True)
    assert len(calls) == 4 and len(store.read("ticket").attempts) == 4
