"""Atomic planner installation and claims share the queue's durable boundary."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import timedelta
import json
import sqlite3
import threading
from copy import deepcopy

import pytest

from scheduler.joint import CommittedAssignment
from shared.scheduling import ExecutionAttempt
from workflow.planning import prepare_plan
from workflow.queue_store import LeaseError, PlanningConflict, QueueStore
from data.quality_reviews import fingerprint

from test_staged_planning import inputs, NOW, at
from test_planned_replay import planned_inputs, planned_queue


def assignments(prepared):
    return tuple(item.candidate for item in prepared.result.winner.assignments)


def install(store, prepared, *, snapshot=None, now=NOW, worker_id="planned-worker", **changes):
    identity = prepared.report["request"]["workflow_id"]
    snapshot = snapshot or store.planning_snapshot(identity, now=now)
    return store.install_plan(identity, assignments(prepared) if prepared.result.winner else None,
        prepared.report, expected_token=snapshot.token, now=now, worker_id=worker_id, **changes)


def test_fixed_claim_skips_planned_jobs_and_claim_requires_installed_revision(planned_queue):
    store, args, context, config, prepared = planned_queue
    assert store.claim("legacy-worker", now=NOW, lease_seconds=60) is None
    claim = install(store, prepared)
    assert claim.candidate.step_id == "classify" and claim.planning_revision == 2
    actual = prepared.rendered_steps["classify"]
    with pytest.raises(LeaseError, match="latest decision"):
        store.mark_running(replace(claim, planning_revision=None), now=NOW,
            rendered_step=actual.step, source_ids=actual.source_ids, input_fingerprint=actual.input_fingerprint)
    with pytest.raises(LeaseError, match="materialized"):
        store.mark_running(claim, now=NOW)
    running = store.mark_running(claim, now=NOW, rendered_step=actual.step,
        source_ids=actual.source_ids, input_fingerprint=actual.input_fingerprint, lookup_seconds=actual.lookup_seconds)
    assert running.planning_revision == 2
    assert store.read(args["request"].workflow_id).jobs[0].status == "running"
    with pytest.raises(LeaseError, match="undispatched"):
        store.release_undispatched(running, now=NOW)


def test_fresh_token_cannot_install_an_unknown_previous_winner(planned_queue):
    store, args, context, config, prepared = planned_queue
    identity = args["request"].workflow_id
    snapshot = store.planning_snapshot(identity, now=NOW)
    report = deepcopy(prepared.report)
    report["previous_plan_fingerprint"] = "f" * 64
    report["fingerprint"] = fingerprint({key: value for key, value in report.items() if key != "fingerprint"})
    with pytest.raises(ValueError, match="previous winner"):
        store.install_plan(identity, assignments(prepared), report, expected_token=snapshot.token,
                           now=NOW, worker_id="worker")
    assert len(store.read(identity).decisions) == 1
    assert store.read(identity).attempts == ()


def test_zero_job_infeasibility_can_bootstrap_after_health_recovers(planned_inputs, tmp_path):
    args, context, config, prepared = planned_inputs
    missing = prepare_plan(**{**args, "health": {}})
    assert missing.result.winner is None
    store = QueueStore(tmp_path / "queue.sqlite3")
    try:
        identity = store.enqueue_planned(args["request"], None, configuration=config,
            planning_context=context, decision=missing.report, now=NOW)
        state = store.read(identity)
        assert state.status == "infeasible" and state.jobs == state.attempts == state.plan_history == ()
        assert store.planned_workflow_ids() == (identity,)
        claim = install(store, prepared)
        assert claim.candidate.step_id == "classify"
        assert len(store.read(identity).jobs) == 4
        assert [value["revision"] for value in store.read(identity).decisions] == [1, 2]
    finally:
        store.close()


def test_no_winner_revision_suspends_scheduled_work_and_restart_retains_it(planned_queue):
    store, args, context, config, prepared = planned_queue
    missing = prepare_plan(**{**args, "health": {}})
    install(store, missing, worker_id=None)
    identity, path = args["request"].workflow_id, store.path
    assert store.read(identity).status == "infeasible"
    second = QueueStore(path)
    try:
        state = second.read(identity)
        assert len(state.jobs) == 4 and state.decisions[-1]["revision"] == 2
        assert second.claim("fixed", now=NOW, lease_seconds=60) is None
        assert install(second, prepared).planning_revision == 3
    finally:
        second.close()


def test_shared_capacity_change_invalidates_preparation_and_reserves_full_horizon(planned_queue):
    store, args, context, config, prepared = planned_queue
    identity = args["request"].workflow_id
    snapshot = store.planning_snapshot(identity, now=NOW)
    competitor = replace(args["request"], workflow_id="competitor")
    store.enqueue(competitor, assignments(prepared), configuration=config)
    other = store.claim("other", now=NOW, lease_seconds=60)
    assert other.workflow_id == "competitor"
    with pytest.raises(PlanningConflict, match="shared occupancy"):
        install(store, prepared, snapshot=snapshot)
    busy = store.planning_snapshot(identity, now=NOW)
    assert busy.token != snapshot.token and len(busy.reservations) == 1
    assert busy.reservations[0].start == NOW
    assert busy.reservations[0].finish == args["request"].completion_deadline
    # A fresh token does not let an old report omit newly occupied capacity.
    with pytest.raises(PlanningConflict, match="reservations"):
        install(store, prepared, snapshot=busy)
    store.release_undispatched(other, now=NOW)
    released = store.planning_snapshot(identity, now=NOW)
    assert not released.reservations and released.token != busy.token
    assert install(store, prepared, snapshot=released).workflow_id == identity


def test_two_connections_cannot_install_and_claim_the_same_revision(planned_queue):
    store, args, context, config, prepared = planned_queue
    snapshot = store.planning_snapshot(args["request"].workflow_id, now=NOW)
    barrier = threading.Barrier(2)

    def contend(owner):
        contender = QueueStore(store.path)
        try:
            barrier.wait(timeout=5)
            try:
                return install(contender, prepared, snapshot=snapshot, worker_id=owner)
            except PlanningConflict:
                return None
        finally:
            contender.close()

    with ThreadPoolExecutor(max_workers=2) as executor:
        claims = list(executor.map(contend, ("one", "two")))
    assert sum(claim is not None for claim in claims) == 1
    assert len(store.read(args["request"].workflow_id).decisions) == 2


def test_cancel_and_own_active_claim_prevent_decision_changes(planned_queue):
    store, args, context, config, prepared = planned_queue
    claim = install(store, prepared)
    snapshot = store.planning_snapshot(claim.workflow_id, now=NOW)
    with pytest.raises(PlanningConflict, match="active claim"):
        install(store, prepared, snapshot=snapshot)
    with pytest.raises(ValueError, match="install_plan"):
        store.replan(claim.workflow_id, assignments(prepared), now=NOW)
    store.cancel(claim.workflow_id, now=NOW)
    with pytest.raises(PlanningConflict):
        install(store, prepared, snapshot=snapshot)
    with pytest.raises(LeaseError):
        store.release_undispatched(claim, now=NOW)
    assert store.read(claim.workflow_id).status == "cancelled"


def test_release_invalidates_old_token_and_expired_lease_cannot_be_released(planned_queue):
    store, args, context, config, prepared = planned_queue
    claim = install(store, prepared, lease_seconds=1)
    claimed = store.planning_snapshot(claim.workflow_id, now=NOW)
    store.release_undispatched(claim, now=NOW)
    with pytest.raises(PlanningConflict):
        install(store, prepared, snapshot=claimed)
    second = install(store, prepared, lease_seconds=1)
    with pytest.raises(LeaseError):
        store.release_undispatched(second, now=at(1))
    store.recover(now=at(1))
    assert not store.planning_snapshot(claim.workflow_id, now=at(1)).reservations


def test_assignment_mismatch_and_missing_assignments_never_create_revision(planned_queue):
    store, args, context, config, prepared = planned_queue
    snapshot = store.planning_snapshot(args["request"].workflow_id, now=NOW)
    values = assignments(prepared)
    changed = (replace(values[0], candidate_id="unrecorded-choice"), *values[1:])
    for choices in (None, changed):
        with pytest.raises(ValueError):
            store.install_plan(args["request"].workflow_id, choices, prepared.report,
                expected_token=snapshot.token, now=NOW)
    assert store.planning_snapshot(args["request"].workflow_id, now=NOW).token == snapshot.token


def test_planning_elapsed_time_does_not_reject_originally_due_start(planned_queue):
    store, args, context, config, prepared = planned_queue
    claim = install(store, prepared, now=at(.1))
    assert claim is not None and claim.candidate.planned_start == NOW


def test_expired_decision_cannot_cross_provider_dispatch_boundary(planned_queue):
    store, args, context, config, prepared = planned_queue
    claim = install(store, prepared)
    rendered = prepared.rendered_steps["classify"]
    with pytest.raises(LeaseError, match="decision expired"):
        store.mark_running(claim, now=at(context["max_decision_age_seconds"] + 1),
            rendered_step=rendered.step, source_ids=rendered.source_ids,
            input_fingerprint=rendered.input_fingerprint)
    assert store.read(claim.workflow_id).jobs[0].dispatched_at is None


def test_validation_cache_is_isolated_and_changed_durable_bytes_revalidate(planned_queue):
    store, args, context, config, prepared = planned_queue
    identity = args["request"].workflow_id
    first = store.read(identity)
    first.decisions[0]["report"]["trigger"] = "only-mutated-in-caller-snapshot"
    first.planning_context["search_limit"] = 1
    second = store.read(identity)
    assert second.decisions[0]["report"]["trigger"] != first.decisions[0]["report"]["trigger"]
    assert second.planning_context["search_limit"] == context["search_limit"]
    tampered = dict(second.decisions[0]["report"], trigger="changed-without-rehash")
    store.connection.execute("UPDATE planning_decisions SET report=? WHERE workflow_id=? AND revision=1",
                             (json.dumps(tampered), identity))
    with pytest.raises(ValueError):
        store.read(identity)


def test_current_assignment_must_match_last_installed_winner(planned_queue):
    store, args, context, config, prepared = planned_queue
    chosen = replace(assignments(prepared)[0], candidate_id="not-the-selected-candidate")
    store.connection.execute("UPDATE jobs SET candidate=? WHERE workflow_id=? AND step_id=?",
        (json.dumps(chosen.to_dict()), args["request"].workflow_id, chosen.step_id))
    with pytest.raises(ValueError, match="installed decision"):
        store.read(args["request"].workflow_id)


def test_completed_execution_invalidates_old_snapshot_and_remains_frozen(planned_queue):
    store, args, context, config, prepared = planned_queue
    claim = install(store, prepared)
    actual = prepared.rendered_steps["classify"]
    claim = store.mark_running(claim, now=NOW, rendered_step=actual.step,
        source_ids=actual.source_ids, input_fingerprint=actual.input_fingerprint)
    busy = store.planning_snapshot(claim.workflow_id, now=NOW)
    attempt = ExecutionAttempt("classification", claim.workflow_id, claim.candidate, "completed",
        actual_start=NOW, actual_finish=at(2), executed_model_id="simulated:1", executed_deployment_id="local",
        output="account_access", output_limit_tokens=actual.step.output_limit_tokens,
        enable_thinking=actual.step.enable_thinking, completion_reason="stop")
    store.complete(claim, attempt, now=at(2))
    with pytest.raises(PlanningConflict):
        install(store, prepared, snapshot=busy, now=at(2))
    next_plan = prepare_plan(**{**args, "now": at(2), "outputs": {"classify": "account_access"},
        "fixed_assignments": (CommittedAssignment(claim.candidate, NOW, at(2)),),
        "previous_plan": prepared.result.winner, "trigger": "classify-completed"})
    next_claim = install(store, next_plan, now=at(2))
    assert next_claim.candidate.step_id == "retrieve"
    assert store.read(claim.workflow_id).jobs[0].candidate == claim.candidate


def test_old_database_remains_readable_without_planning_tables(planned_inputs, tmp_path):
    args, context, config, prepared = planned_inputs
    path = tmp_path / "legacy.sqlite3"
    store = QueueStore(path)
    store.enqueue(args["request"], assignments(prepared), configuration=config)
    for table in ("planning_decisions", "planning_contexts", "planning_meta"):
        store.connection.execute(f"DROP TABLE {table}")
    store.close()
    reader = object.__new__(QueueStore)
    reader.path = path
    reader.connection = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, isolation_level=None)
    reader.connection.row_factory = sqlite3.Row
    try:
        state = reader.read(args["request"].workflow_id)
        assert state.planning_context is None and state.decisions == ()
        assert len(state.jobs) == 4 and reader.planned_workflow_ids() == ()
    finally:
        reader.close()
    upgraded = QueueStore(path)
    try:
        assert upgraded.read(args["request"].workflow_id).planning_context is None
        assert upgraded.connection.execute("SELECT version FROM queue_meta").fetchone()[0] == 1
        assert upgraded.claim("fixed", now=NOW, lease_seconds=60) is not None
    finally:
        upgraded.close()
