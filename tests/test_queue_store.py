"""Durability and dispatch boundaries for the SQLite queue (no inference)."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json
import sqlite3
import threading

import pytest

from shared.deployments import Deployment, DeploymentRegistry
from shared.scheduling import ExecutionAttempt, MetricEstimate, PlannedCandidate, StepEstimates, StepRequest, WorkflowRequest
from workflow.queue_store import Claim, LeaseError, QueueStore
from workflow.dispatch import dispatch_planned_candidate


NOW = datetime(2026, 10, 19, 12, tzinfo=timezone.utc)


@pytest.fixture
def request_and_plans():
    unknown = lambda unit: MetricEstimate(None, unit, "unknown")
    estimates = StepEstimates(unknown("s"), unknown("ratio"), unknown("USD"), unknown("kWh"), unknown("gCO2e"))
    steps = tuple(StepRequest(name, name, f"Template for {name}", None, 128, NOW,
                  NOW + timedelta(minutes=5), dependencies=(() if index == 0 else ("classify",)),
                  required_for_response=index == 0)
                  for index, name in enumerate(("classify", "summarize")))
    request = WorkflowRequest("ticket-a", "Original ticket", NOW, "balanced",
                NOW + timedelta(minutes=5), NOW + timedelta(minutes=10), "support-v2", "constraints-v1", steps)
    plans = tuple(PlannedCandidate("candidate-" + step.step_id, step.step_id, "llama", "local", NOW, estimates)
                  for step in steps)
    return request, plans


@pytest.fixture
def queue(tmp_path, request_and_plans):
    store = QueueStore(tmp_path / "queue.sqlite3")
    request, plans = request_and_plans
    store.enqueue(request, plans, configuration={"calibration_id": "fixture-v1"})
    yield store
    store.close()


def claim(queue, *, at=NOW, owner="worker-a", seconds=60):
    return queue.claim(owner, now=at, lease_seconds=seconds)


def attempt(reservation, *, finish=NOW + timedelta(seconds=2), status="completed", **changes):
    values = dict(attempt_id=f"attempt-{reservation.candidate.step_id}-{reservation.retry_index}",
        workflow_id=reservation.workflow_id, planned_candidate=reservation.candidate,
        status=status, retry_index=reservation.retry_index, actual_start=NOW + timedelta(seconds=1),
        actual_finish=finish, executed_model_id=reservation.candidate.model_id,
        executed_deployment_id=reservation.candidate.deployment_id, input_tokens=17, output_tokens=6,
        output="Actual output" if status == "completed" else None,
        error=None if status == "completed" else "provider_failed", output_limit_tokens=128)
    values.update(changes)
    return ExecutionAttempt(**values)


def test_enqueue_restart_and_no_dispatch_before_due(tmp_path, request_and_plans):
    request, plans = request_and_plans
    later = NOW + timedelta(seconds=20)
    plans = tuple(replace(p, planned_start=later) for p in plans)
    first = QueueStore(tmp_path / "queue.sqlite3")
    assert first.enqueue(request, plans, configuration={}) == request.workflow_id
    assert claim(first) is None
    first.close()
    second = QueueStore(tmp_path / "queue.sqlite3")
    try:
        reservation = claim(second, at=later)
        assert reservation.step_request.step_id == "classify"
        assert second.read("ticket-a").jobs[0].dispatched_at is None
    finally:
        second.close()


def test_duplicate_workflow_never_overwrites(queue, request_and_plans):
    request, plans = request_and_plans
    with pytest.raises(sqlite3.IntegrityError):
        queue.enqueue(replace(request, input_text="Replacement"), plans, configuration={})
    assert queue.read("ticket-a").request.input_text == "Original ticket"


def test_two_connections_race_claim_exactly_once(queue, request_and_plans):
    request, plans = request_and_plans
    # Two independently eligible workflows compete for one deployment slot.
    queue.enqueue(replace(request, workflow_id="ticket-b"), plans, configuration={})
    barrier = threading.Barrier(2)
    def competing_worker(owner):
        store = QueueStore(queue.path)
        try:
            barrier.wait(timeout=5)
            return claim(store, owner=owner)
        finally:
            store.close()
    with ThreadPoolExecutor(max_workers=2) as pool:
        reservations = list(pool.map(competing_worker, ("worker-a", "worker-b")))
    assert sum(value is not None for value in reservations) == 1
    assert claim(queue) is None


def test_dependencies_and_completed_job_are_not_repeated(queue):
    first = claim(queue)
    assert first.dependency_outputs == {}
    assert claim(queue, owner="worker-b") is None
    queue.mark_running(first, now=NOW + timedelta(seconds=1))
    result = attempt(first)
    queue.complete(first, result, now=result.actual_finish)
    second = claim(queue, at=result.actual_finish)
    assert second.step_request.step_id == "summarize"
    assert second.dependency_outputs == {"classify": "Actual output"}
    queue.mark_running(second, now=result.actual_finish)
    second_result = attempt(second, actual_start=result.actual_finish,
                            finish=result.actual_finish + timedelta(seconds=1))
    queue.complete(second, second_result, now=second_result.actual_finish)
    assert queue.read("ticket-a").status == "completed"
    assert claim(queue, at=NOW + timedelta(minutes=1)) is None
    with pytest.raises(LeaseError):
        queue.complete(first, result, now=result.actual_finish)


def test_rendered_prompt_is_durable_before_dispatch(queue):
    reservation = claim(queue)
    rendered = replace(reservation.step_request, prompt="Actual dependency-aware prompt", input_tokens=15,
                       input_size_source="fixture-tokenizer")
    updated = queue.mark_running(reservation, now=NOW, rendered_step=rendered,
                source_ids=("KB-A",), input_fingerprint="prompt-sha", lookup_seconds=0.04)
    persisted = queue.read("ticket-a").jobs[0]
    assert updated.step_request == persisted.rendered_step == rendered
    assert persisted.source_ids == ("KB-A",)
    assert persisted.input_fingerprint == "prompt-sha"
    assert persisted.lookup_seconds == 0.04
    with pytest.raises(ValueError, match="preserve"):
        queue.mark_running(reservation, now=NOW, rendered_step=replace(rendered, output_limit_tokens=129))


def test_undispatched_expired_lease_releases_safely(queue):
    previous = claim(queue, seconds=5)
    queue.recover(now=NOW + timedelta(seconds=5))
    new = claim(queue, at=NOW + timedelta(seconds=5), owner="worker-b")
    assert new.lease_token != previous.lease_token
    assert new.retry_index == 0
    with pytest.raises(LeaseError):
        queue.mark_running(previous, now=NOW + timedelta(seconds=5))


def test_running_lease_expiration_requires_reconciliation_and_accepts_late_usage(queue):
    reservation = claim(queue, seconds=3)
    queue.mark_running(reservation, now=NOW + timedelta(seconds=1))
    queue.recover(now=NOW + timedelta(seconds=4))
    assert queue.read("ticket-a").status == "needs-reconciliation"
    assert claim(queue, at=NOW + timedelta(seconds=4), owner="worker-b") is None
    with pytest.raises(ValueError, match="acknowledgement"):
        queue.retry("ticket-a", "classify", now=NOW + timedelta(seconds=4))
    late = attempt(reservation, finish=NOW + timedelta(seconds=5))
    queue.complete(reservation, late, now=late.actual_finish)
    assert queue.read("ticket-a").attempts == (late,)
    assert queue.read("ticket-a").status == "response_ready"


def test_explicit_ambiguous_retry_invalidates_old_result(queue):
    old = claim(queue, seconds=3)
    queue.mark_running(old, now=NOW + timedelta(seconds=1))
    queue.recover(now=NOW + timedelta(seconds=4))
    queue.retry("ticket-a", "classify", now=NOW + timedelta(seconds=4), acknowledge_ambiguous=True)
    new = claim(queue, at=NOW + timedelta(seconds=4), owner="worker-b")
    assert new.retry_index == 1
    with pytest.raises(LeaseError):
        queue.complete(old, attempt(old, finish=NOW + timedelta(seconds=5)), now=NOW + timedelta(seconds=5))


def test_owner_token_and_renewal(queue):
    reservation = claim(queue, seconds=5)
    for changed in (replace(reservation, worker_id="intruder"), replace(reservation, lease_token="wrong")):
        with pytest.raises(LeaseError):
            queue.renew(changed, now=NOW, lease_seconds=30)
    renewed = queue.renew(reservation, now=NOW + timedelta(seconds=4), lease_seconds=30)
    assert renewed.lease_expires_at == NOW + timedelta(seconds=34)
    queue.mark_running(renewed, now=NOW + timedelta(seconds=6))
    with pytest.raises(LeaseError):
        queue.renew(renewed, now=NOW + timedelta(seconds=34), lease_seconds=30)


def test_cancellation_invalidates_undispatched_claim(queue):
    reservation = claim(queue)
    queue.cancel("ticket-a", now=NOW)
    assert queue.read("ticket-a").status == "cancelled"
    assert claim(queue) is None
    with pytest.raises(LeaseError):
        queue.mark_running(reservation, now=NOW)


def test_running_cancellation_retains_late_output_and_usage_and_blocks_dependents(queue):
    reservation = claim(queue)
    queue.mark_running(reservation, now=NOW + timedelta(seconds=1))
    queue.cancel("ticket-a", now=NOW + timedelta(seconds=1))
    result = attempt(reservation)
    queue.complete(reservation, result, now=result.actual_finish)
    state = queue.read("ticket-a")
    assert state.status == "cancelled"
    assert state.attempts == (result,)
    assert state.jobs[0].output == "Actual output"
    assert state.jobs[0].status == "cancelled"
    assert claim(queue, at=result.actual_finish) is None


def test_missed_undispatched_deadline_does_not_dispatch(queue):
    end = NOW + timedelta(minutes=5)
    assert claim(queue, at=end) is None
    state = queue.read("ticket-a")
    assert state.status == "deadline_missed"
    assert state.attempts == ()
    assert all(job.status == "deadline_missed" for job in state.jobs)


def test_late_persistence_keeps_attempt_and_deadline_violation(queue):
    reservation = claim(queue)
    queue.mark_running(reservation, now=NOW + timedelta(seconds=1))
    result = attempt(reservation)
    queue.complete(reservation, result, now=NOW + timedelta(minutes=5, seconds=1))
    state = queue.read("ticket-a")
    assert state.status == "deadline_missed"
    assert state.attempts == (result,)
    assert state.jobs[0].error_code == "deadline_missed"
    assert claim(queue, at=NOW + timedelta(minutes=5, seconds=1)) is None


def test_failed_prerequisite_blocks_downstream_and_explicit_retry_retains_failure(queue):
    reservation = claim(queue)
    queue.mark_running(reservation, now=NOW + timedelta(seconds=1))
    result = attempt(reservation, status="failed")
    queue.complete(reservation, result, now=result.actual_finish)
    assert claim(queue, at=result.actual_finish) is None
    assert queue.read("ticket-a").status == "failed"
    with pytest.raises(ValueError, match="acknowledgement"):
        queue.retry("ticket-a", "classify", now=result.actual_finish)
    queue.retry("ticket-a", "classify", now=result.actual_finish, acknowledge_ambiguous=True)
    assert queue.read("ticket-a").attempts == (result,)
    assert claim(queue, at=result.actual_finish).retry_index == 1


def test_known_undispatched_failure_can_retry_without_ambiguous_acknowledgement(queue):
    reservation = claim(queue)
    queue.fail_before_dispatch(reservation, "health_unavailable", now=NOW)
    assert queue.read("ticket-a").attempts == ()
    queue.retry("ticket-a", "classify", now=NOW)
    assert claim(queue).retry_index == 1


@pytest.mark.parametrize("change", [
    {"workflow_id": "different"}, {"retry_index": 7}, {"executed_model_id": "other"},
    {"executed_deployment_id": "other"}, {"output_limit_tokens": 64},
    {"actual_start": NOW - timedelta(seconds=1)},
])
def test_attempt_identity_or_controls_mismatch_fails_atomically(queue, change):
    reservation = claim(queue)
    queue.mark_running(reservation, now=NOW)
    with pytest.raises(ValueError):
        queue.complete(reservation, attempt(reservation, **change), now=NOW + timedelta(seconds=2))
    assert queue.read("ticket-a").attempts == ()
    assert queue.read("ticket-a").jobs[0].status == "running"


@pytest.mark.parametrize("config", [{"GOOGLE_API_KEY": "secret"}, {"environment": {}},
    {"registry_snapshot": {"schema_version": 1, "deployments": [{"api_key": "secret"}]}},
    {"experiment_mode": "pretend-live"}])
def test_unknown_configuration_and_credentials_rejected(tmp_path, request_and_plans, config):
    store = QueueStore(tmp_path / "queue.sqlite3")
    try:
        with pytest.raises(ValueError):
            store.enqueue(*request_and_plans, configuration=config)
        with pytest.raises(KeyError):
            store.read("ticket-a")
    finally:
        store.close()


def test_valid_registry_snapshot_checks_selected_model(tmp_path, request_and_plans):
    store = QueueStore(tmp_path / "queue.sqlite3")
    registry = DeploymentRegistry((Deployment("local", "ollama", "http://localhost:11434", {"llama": "llama3.2:3b"}),))
    config = {"registry_snapshot": registry.to_dict(), "registry_fingerprint": registry.config_fingerprint()}
    try:
        store.enqueue(*request_and_plans, configuration=config)
        assert store.read("ticket-a").configuration == config
    finally:
        store.close()


@pytest.mark.parametrize("field,value", [("status", "pretend-completed"),
    ("candidate", '{"schema_version":true}'), ("metadata", '{"source_ids":[],"lookup_seconds":NaN}'),
    ("planned_start", "2026-10-19T11:00:00+00:00"), ("retry_index", -1)])
def test_malformed_state_fails_closed_before_claim(queue, field, value):
    queue.connection.execute(f"UPDATE jobs SET {field}=? WHERE step_id='classify'", (value,))
    with pytest.raises(ValueError):
        queue.read("ticket-a")
    with pytest.raises(ValueError):
        claim(queue)


@pytest.mark.parametrize("seconds", [0, -1, True, float("nan"), float("inf"), 10**300])
def test_invalid_lease_durations_rejected(queue, seconds):
    with pytest.raises(ValueError):
        claim(queue, seconds=seconds)


def test_replan_only_undispatched_jobs_and_rolls_back_entire_change(queue, request_and_plans):
    _, plans = request_and_plans
    delayed = tuple(replace(p, candidate_id=p.candidate_id + "-new", planned_start=NOW + timedelta(seconds=10)) for p in plans)
    queue.replan("ticket-a", delayed, now=NOW)
    assert claim(queue) is None
    first = claim(queue, at=NOW + timedelta(seconds=10))
    revised = tuple(replace(p, candidate_id=p.candidate_id + "-revised", planned_start=NOW + timedelta(seconds=20)) for p in delayed)
    with pytest.raises(ValueError, match="unclaimed"):
        queue.replan("ticket-a", revised, now=NOW + timedelta(seconds=10))
    assert tuple(job.candidate for job in queue.read("ticket-a").jobs) == delayed
    queue.mark_running(first, now=NOW + timedelta(seconds=10))
    with pytest.raises(ValueError, match="unclaimed"):
        queue.replan("ticket-a", revised, now=NOW + timedelta(seconds=10))


def test_schema_version_is_checked_on_restart(queue):
    queue.connection.execute("UPDATE queue_meta SET schema_version=99")
    with pytest.raises(ValueError, match="version"):
        QueueStore(queue.path)


def test_rejected_output_retains_transport_attempt_and_blocks_dependents(queue):
    reservation = claim(queue)
    queue.mark_running(reservation, now=NOW)
    result = attempt(reservation)
    queue.complete(reservation, result, now=result.actual_finish, output_valid=False,
                   validation_error="unknown_citation")
    state = queue.read("ticket-a")
    assert state.attempts == (result,)
    assert state.jobs[0].status == "failed"
    assert state.jobs[0].output_valid is False
    assert state.jobs[0].validation_error == "unknown_citation"
    assert state.completed_outputs() == {}
    assert claim(queue, at=result.actual_finish) is None


def test_rejected_late_output_preserves_deadline_miss(queue):
    reservation = claim(queue)
    queue.mark_running(reservation, now=NOW)
    result = attempt(reservation)
    queue.complete(reservation, result, now=NOW + timedelta(minutes=5, seconds=1),
                   output_valid=False, validation_error="truncated_output")
    job = queue.read("ticket-a").jobs[0]
    assert job.status == "deadline_missed"
    assert job.error_code == "deadline_missed"
    assert job.validation_error == "truncated_output"


def test_blank_completed_transport_output_is_not_accepted(queue):
    reservation = claim(queue)
    queue.mark_running(reservation, now=NOW)
    result = attempt(reservation, output="   ")
    queue.complete(reservation, result, now=result.actual_finish)
    assert queue.read("ticket-a").jobs[0].validation_error == "empty_output"
    assert queue.read("ticket-a").completed_outputs() == {}


def test_plan_revisions_and_retry_acknowledgement_are_retained(queue, request_and_plans):
    _, plans = request_and_plans
    revised = tuple(replace(p, candidate_id=p.candidate_id + "-new") for p in plans)
    queue.replan("ticket-a", revised, now=NOW)
    assert queue.read("ticket-a").plan_history == plans + revised
    reservation = claim(queue, seconds=3)
    queue.mark_running(reservation, now=NOW)
    queue.recover(now=NOW + timedelta(seconds=3))
    queue.retry("ticket-a", "classify", now=NOW + timedelta(seconds=3), acknowledge_ambiguous=True)
    events = queue.read("ticket-a").events
    assert events[-1] == {"step_id": "classify", "kind": "retry_acknowledged_possible_duplicate_usage",
                          "recorded_at": NOW + timedelta(seconds=3)}


@pytest.mark.parametrize("timeout", [0, -1, 301, True, float("nan"), "30"])
def test_invalid_timeout_configuration_rejected(tmp_path, request_and_plans, timeout):
    store = QueueStore(tmp_path / "queue.sqlite3")
    try:
        with pytest.raises(ValueError):
            store.enqueue(*request_and_plans, configuration={"timeout_seconds": timeout})
    finally:
        store.close()


def test_worker_configuration_is_persisted_without_environment_values(tmp_path, request_and_plans):
    store = QueueStore(tmp_path / "queue.sqlite3")
    configuration = {"timeout_seconds": 120, "kb_fingerprint": "kb-sha", "worker_version": "worker-v1"}
    try:
        store.enqueue(*request_and_plans, configuration=configuration)
        assert claim(store).configuration == configuration
    finally:
        store.close()


def test_failure_during_preflight_preserves_elapsed_deadline(queue):
    reservation = claim(queue, seconds=600)
    queue.fail_before_dispatch(reservation, "preflight_failed", now=NOW + timedelta(minutes=5))
    job = queue.read("ticket-a").jobs[0]
    assert job.status == job.error_code == "deadline_missed"
    assert queue.read("ticket-a").attempts == ()


def test_provider_cancelled_attempt_cannot_leave_workflow_silently_scheduled(queue):
    reservation = claim(queue)
    queue.mark_running(reservation, now=NOW)
    result = attempt(reservation, status="cancelled")
    queue.complete(reservation, result, now=result.actual_finish)
    assert queue.read("ticket-a").status == "failed"
    assert claim(queue, at=result.actual_finish) is None


class FixedClock:
    def __init__(self, instant):
        self.instant = instant

    def now(self):
        return self.instant


def test_real_dispatch_missing_credential_records_known_nondispatch(tmp_path, request_and_plans, monkeypatch):
    request, plans = request_and_plans
    monkeypatch.delenv("A2_QUEUE_TEST_KEY", raising=False)
    registry = DeploymentRegistry((Deployment("local", "nvidia", "https://example.test/chat/completions",
                    {"llama": "provider-llama"}, credential_env="A2_QUEUE_TEST_KEY"),))
    store = QueueStore(tmp_path / "queue.sqlite3")
    try:
        store.enqueue(request, plans, configuration={"registry_snapshot": registry.to_dict()})
        reservation = claim(store)
        store.mark_running(reservation, now=NOW)
        result = dispatch_planned_candidate(reservation.candidate, registry, "Actual prompt",
                    workflow_id=request.workflow_id, clock=FixedClock(NOW + timedelta(seconds=1)), output_limit_tokens=128)
        assert result.status == "failed" and result.error == "missing-credential"
        assert result.actual_start is result.actual_finish is result.executed_model_id is None
        store.complete(reservation, result, now=NOW + timedelta(seconds=1))
        state = store.read(request.workflow_id)
        assert state.attempts == (result,)
        assert state.jobs[0].dispatched_at is None
        assert state.jobs[0].finished_at == NOW + timedelta(seconds=1)
        store.retry(request.workflow_id, "classify", now=NOW + timedelta(seconds=1))
        assert claim(store, at=NOW + timedelta(seconds=1)).retry_index == 1
    finally:
        store.close()


def test_real_dispatch_deadline_preflight_has_unknown_provider_timestamps(queue, request_and_plans):
    _, plans = request_and_plans
    registry = DeploymentRegistry((Deployment("local", "ollama", "http://example.test:11434", {"llama": "llama"}),))
    reservation = claim(queue, seconds=600)
    queue.mark_running(reservation, now=NOW)
    expired = NOW + timedelta(minutes=5, seconds=1)
    result = dispatch_planned_candidate(plans[0], registry, "Actual prompt", workflow_id="ticket-a",
                clock=FixedClock(expired), output_limit_tokens=128,
                latest_finish=reservation.step_request.latest_finish)
    assert result.error == "step-deadline-exceeded"
    assert result.actual_start is result.actual_finish is None
    queue.complete(reservation, result, now=expired)
    assert queue.read("ticket-a").attempts == (result,)
    assert queue.read("ticket-a").jobs[0].status == "deadline_missed"


def test_actual_dispatch_provider_model_alias_is_strictly_preserved(tmp_path, request_and_plans, monkeypatch):
    request, plans = request_and_plans
    registry = DeploymentRegistry((Deployment("local", "ollama", "http://example.test:11434",
                    {"llama": "llama3.2:3b"}),))
    class Response:
        status_code = 200
        def json(self):
            return {"response": "Valid output", "model": "llama3.2:3b", "done": True,
                    "done_reason": "stop", "prompt_eval_count": 13, "eval_count": 5}
        def close(self):
            pass
    monkeypatch.setattr("workflow.deployment_clients.requests.post", lambda *args, **kwargs: Response())
    store = QueueStore(tmp_path / "queue.sqlite3")
    try:
        store.enqueue(request, plans, configuration={"registry_snapshot": registry.to_dict()})
        reservation = claim(store)
        store.mark_running(reservation, now=NOW)
        result = dispatch_planned_candidate(reservation.candidate, registry, "Actual prompt",
                    workflow_id=request.workflow_id, clock=FixedClock(NOW + timedelta(seconds=1)), output_limit_tokens=128)
        assert result.executed_model_id == "llama3.2:3b"
        assert result.planned_candidate.model_id == "llama"
        store.complete(reservation, result, now=NOW + timedelta(seconds=1))
        assert store.read(request.workflow_id).attempts == (result,)
    finally:
        store.close()


@pytest.mark.parametrize("extra", [{"input_tokens": 1}, {"provider_seconds": 0}, {"wait_seconds": 1}])
def test_partial_preflight_execution_facts_are_rejected(queue, extra):
    reservation = claim(queue)
    queue.mark_running(reservation, now=NOW)
    result = ExecutionAttempt("no-dispatch", "ticket-a", reservation.candidate, "failed",
                error="preflight_failed", output_limit_tokens=128, **extra)
    with pytest.raises(ValueError):
        queue.complete(reservation, result, now=NOW)


def test_runtime_capacity_blocks_other_workflow_after_overrun_and_cancellation(queue, request_and_plans):
    request, plans = request_and_plans
    queue.enqueue(replace(request, workflow_id="ticket-b"), plans, configuration={})
    reservation = claim(queue, seconds=3)
    assert claim(queue, owner="worker-b") is None
    queue.mark_running(reservation, now=NOW)
    queue.cancel("ticket-a", now=NOW + timedelta(seconds=1))
    assert queue.read("ticket-a").jobs[0].finished_at is None
    assert claim(queue, owner="worker-b", at=NOW + timedelta(seconds=4)) is None
    queue.complete(reservation, attempt(reservation, actual_start=NOW), now=NOW + timedelta(seconds=4))
    assert claim(queue, owner="worker-b", at=NOW + timedelta(seconds=4)).workflow_id == "ticket-b"


def test_late_superseded_usage_is_archived_without_changing_current_job(queue):
    old = claim(queue, seconds=3)
    queue.mark_running(old, now=NOW)
    queue.recover(now=NOW + timedelta(seconds=3))
    queue.retry("ticket-a", "classify", now=NOW + timedelta(seconds=3), acknowledge_ambiguous=True)
    current = claim(queue, at=NOW + timedelta(seconds=3), owner="worker-b")
    queue.mark_running(current, now=NOW + timedelta(seconds=3))
    late = attempt(old, actual_start=NOW, finish=NOW + timedelta(seconds=4))
    with pytest.raises(LeaseError):
        queue.complete(old, late, now=late.actual_finish)
    queue.archive_late_attempt(old, late, now=late.actual_finish)
    queue.archive_late_attempt(old, late, now=late.actual_finish)
    state = queue.read("ticket-a")
    assert state.attempts == (late,)
    assert state.jobs[0].status == "running"
    assert state.jobs[0].retry_index == 1
    assert state.jobs[0].output is None
    assert state.events[-1]["kind"] == "archived_late_superseded_attempt"
    with pytest.raises(ValueError, match="different facts"):
        queue.archive_late_attempt(old, replace(late, output="Different"), now=late.actual_finish)
    with pytest.raises(LeaseError):
        queue.archive_late_attempt(replace(old, worker_id="intruder"), late, now=late.actual_finish)


def test_registry_capacity_two_can_reserve_two_jobs_only(tmp_path, request_and_plans):
    request, plans = request_and_plans
    registry = DeploymentRegistry((Deployment("local", "ollama", "http://example.test:11434", {"llama": "llama"}, max_concurrency=2),))
    store = QueueStore(tmp_path / "queue.sqlite3")
    try:
        for suffix in "abc":
            store.enqueue(replace(request, workflow_id="ticket-" + suffix), plans,
                          configuration={"registry_snapshot": registry.to_dict()})
        assert claim(store, owner="worker-a") is not None
        assert claim(store, owner="worker-b") is not None
        assert claim(store, owner="worker-c") is None
    finally:
        store.close()

