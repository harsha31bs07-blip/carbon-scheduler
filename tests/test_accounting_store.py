"""Accounting is append-only evidence after the raw provider journal commits."""

from copy import deepcopy
from dataclasses import replace
from datetime import timedelta
import json
import sqlite3

import pytest

from data.quality_reviews import fingerprint
from shared.deployments import Deployment, DeploymentRegistry
from workflow.execution_accounting import enrich_attempt, make_accounting_context, unknown_energy_report
from workflow.queue_store import QueueStore

from test_queue_store import NOW, attempt, request_and_plans
from test_planned_replay import planned_inputs
from test_staged_planning import inputs


@pytest.fixture
def accounting_queue(tmp_path, request_and_plans):
    request, plans = request_and_plans
    registry = DeploymentRegistry((Deployment("local", "ollama", "http://127.0.0.1:11434", {"llama": "llama"}),))
    context = make_accounting_context(registry, request)
    config = {"registry_snapshot": registry.to_dict(), "registry_fingerprint": registry.config_fingerprint(),
              "accounting_version": "execution-accounting.v1"}
    store = QueueStore(tmp_path / "accounting.sqlite3")
    store.enqueue(request, plans, configuration=config, accounting_context=context)
    yield store, request, plans, registry, context, config
    store.close()


def complete_raw(fixture, *, status="completed"):
    store, request, _, registry, context, _ = fixture
    reservation = store.claim("worker", now=NOW, lease_seconds=60)
    reservation = store.mark_running(reservation, now=NOW)
    raw = attempt(reservation, status=status)
    journal_time = raw.actual_finish + timedelta(seconds=1)
    store.complete(reservation, raw, now=journal_time)
    record = enrich_attempt(raw, registry, context, energy_report=unknown_energy_report(),
        measurement_start=NOW, measurement_finish=journal_time + timedelta(seconds=1),
        journal_recorded_at=journal_time, accounted_at=journal_time + timedelta(seconds=2))
    return reservation, raw, record


def record_at(record):
    from datetime import datetime
    return datetime.fromisoformat(record["accounted_at"].replace("Z", "+00:00"))


def test_context_is_frozen_at_enqueue_and_survives_restart(accounting_queue):
    store, request, _, _, context, _ = accounting_queue
    context["scope"] = "mutated-caller-value"
    original = store.read(request.workflow_id).accounting_context
    assert original["scope"] == "host-provider-and-journal.v1"
    original["scope"] = "mutated-snapshot-value"
    second = QueueStore(store.path)
    try:
        assert second.read(request.workflow_id).accounting_context["scope"] == "host-provider-and-journal.v1"
        assert second.read(request.workflow_id).accounting == ()
    finally:
        second.close()


def test_record_follows_raw_preserves_jobs_attempts_and_planning_token(accounting_queue):
    store, request, *_ = accounting_queue
    _, raw, record = complete_raw(accounting_queue)
    before = store.read(request.workflow_id)
    raw_bytes = store.connection.execute("SELECT payload FROM attempts").fetchone()[0]
    token = store._planning_token(request.workflow_id)
    store.record_accounting(request.workflow_id, record, now=record_at(record))
    after = store.read(request.workflow_id)
    assert after.accounting == (record,)
    assert after.jobs == before.jobs and after.attempts == before.attempts == (raw,)
    assert after.events == before.events and after.decisions == before.decisions
    assert store._planning_token(request.workflow_id) == token
    assert store.connection.execute("SELECT payload FROM attempts").fetchone()[0] == raw_bytes
    record["errors"].append("caller-mutation")
    assert "caller-mutation" not in store.read(request.workflow_id).accounting[0]["errors"]


def test_identical_record_is_idempotent_but_replacement_rejected(accounting_queue):
    store, request, _, registry, context, _ = accounting_queue
    _, raw, record = complete_raw(accounting_queue)
    store.record_accounting(request.workflow_id, record, now=record_at(record))
    store.record_accounting(request.workflow_id, deepcopy(record), now=record_at(record) + timedelta(seconds=1))
    replacement = enrich_attempt(raw, registry, context, energy_report=unknown_energy_report(),
        measurement_start=NOW, measurement_finish=raw.actual_finish + timedelta(seconds=2),
        journal_recorded_at=raw.actual_finish + timedelta(seconds=1),
        accounted_at=record_at(record) + timedelta(seconds=1))
    with pytest.raises(ValueError, match="different facts"):
        store.record_accounting(request.workflow_id, replacement, now=record_at(replacement))
    assert store.read(request.workflow_id).accounting == (record,)


def test_cannot_account_for_unpersisted_attempt(accounting_queue):
    store, request, _, registry, context, _ = accounting_queue
    reservation = store.claim("worker", now=NOW, lease_seconds=60)
    raw = attempt(reservation)
    record = enrich_attempt(raw, registry, context, energy_report=unknown_energy_report(),
        measurement_start=NOW, measurement_finish=raw.actual_finish,
        journal_recorded_at=raw.actual_finish, accounted_at=raw.actual_finish)
    with pytest.raises(ValueError, match="durable raw attempt"):
        store.record_accounting(request.workflow_id, record, now=raw.actual_finish)
    assert store.read(request.workflow_id).attempts == store.read(request.workflow_id).accounting == ()


def test_failure_attempt_also_retains_accounting(accounting_queue):
    store, request, *_ = accounting_queue
    _, raw, record = complete_raw(accounting_queue, status="failed")
    store.record_accounting(request.workflow_id, record, now=record_at(record))
    state = store.read(request.workflow_id)
    assert state.status == "failed" and state.attempts == (raw,) and state.accounting == (record,)


def test_invalid_raw_hash_is_rejected_even_with_new_record_fingerprint(accounting_queue):
    store, request, *_ = accounting_queue
    _, _, record = complete_raw(accounting_queue)
    record["attempt_sha256"] = "a" * 64
    record["fingerprint"] = fingerprint({key: value for key, value in record.items() if key != "fingerprint"})
    with pytest.raises(ValueError):
        store.record_accounting(request.workflow_id, record, now=record_at(record))
    assert store.read(request.workflow_id).accounting == ()


def test_future_accounting_rejected_without_affecting_raw_result(accounting_queue):
    store, request, *_ = accounting_queue
    _, raw, record = complete_raw(accounting_queue)
    with pytest.raises(ValueError, match="precede its recording"):
        store.record_accounting(request.workflow_id, record, now=record_at(record) - timedelta(seconds=1))
    assert store.read(request.workflow_id).attempts == (raw,)
    assert store.read(request.workflow_id).accounting == ()


def test_journal_time_cannot_precede_actual_raw_database_insert(accounting_queue):
    store, request, _, registry, context, _ = accounting_queue
    _, raw, _ = complete_raw(accounting_queue)
    record = enrich_attempt(raw, registry, context, energy_report=unknown_energy_report(),
        measurement_start=NOW, measurement_finish=raw.actual_finish + timedelta(seconds=2),
        journal_recorded_at=raw.actual_finish, accounted_at=raw.actual_finish + timedelta(seconds=3))
    with pytest.raises(ValueError, match="raw journal"):
        store.record_accounting(request.workflow_id, record, now=record_at(record))


def test_accounting_storage_failure_leaves_raw_completion_durable(accounting_queue):
    store, request, *_ = accounting_queue
    _, raw, record = complete_raw(accounting_queue)
    store.connection.execute("CREATE TRIGGER deny_accounting BEFORE INSERT ON attempt_accounting BEGIN SELECT RAISE(ABORT,'fixture'); END")
    with pytest.raises(sqlite3.IntegrityError):
        store.record_accounting(request.workflow_id, record, now=record_at(record))
    assert store.read(request.workflow_id).attempts == (raw,)
    assert store.read(request.workflow_id).accounting == ()
    store.connection.execute("DROP TRIGGER deny_accounting")
    store.record_accounting(request.workflow_id, record, now=record_at(record))
    assert store.read(request.workflow_id).accounting == (record,)


def test_enqueue_context_insert_failure_rolls_back_entire_workflow(accounting_queue):
    store, request, plans, _, context, config = accounting_queue
    store.connection.execute("CREATE TRIGGER deny_context BEFORE INSERT ON accounting_contexts BEGIN SELECT RAISE(ABORT,'fixture'); END")
    with pytest.raises(sqlite3.IntegrityError):
        store.enqueue(replace(request, workflow_id="rolled-back"), plans, configuration=config, accounting_context=context)
    for table, key in (("workflows", "id"), ("jobs", "workflow_id"), ("plan_history", "workflow_id"), ("accounting_contexts", "workflow_id")):
        assert store.connection.execute(f"SELECT COUNT(*) FROM {table} WHERE {key}=?", ("rolled-back",)).fetchone()[0] == 0


def test_context_requires_registry_and_invalid_context_has_no_partial_workflow(accounting_queue):
    store, request, plans, _, context, config = accounting_queue
    with pytest.raises(ValueError, match="registry snapshot"):
        store.enqueue(replace(request, workflow_id="missing-registry"), plans,
            configuration={"accounting_version": "execution-accounting.v1"}, accounting_context=context)
    with pytest.raises(ValueError):
        store.enqueue(replace(request, workflow_id="bad-context"), plans, configuration=config, accounting_context={**context, "scope": "invalid"})
    assert store.connection.execute("SELECT COUNT(*) FROM workflows").fetchone()[0] == 1


def test_accounting_can_be_added_to_archived_superseded_usage(accounting_queue):
    store, request, _, registry, context, _ = accounting_queue
    old = store.claim("worker", now=NOW, lease_seconds=1)
    old = store.mark_running(old, now=NOW)
    store.recover(now=NOW + timedelta(seconds=2))
    store.retry(request.workflow_id, "classify", now=NOW + timedelta(seconds=2), acknowledge_ambiguous=True)
    raw = attempt(old, finish=NOW + timedelta(seconds=4))
    store.archive_late_attempt(old, raw, now=raw.actual_finish)
    record = enrich_attempt(raw, registry, context, energy_report=unknown_energy_report(),
        measurement_start=NOW, measurement_finish=raw.actual_finish,
        journal_recorded_at=raw.actual_finish, accounted_at=raw.actual_finish)
    before = store.read(request.workflow_id)
    store.record_accounting(request.workflow_id, record, now=raw.actual_finish)
    after = store.read(request.workflow_id)
    assert after.accounting == (record,) and after.attempts == (raw,)
    assert after.jobs == before.jobs and after.jobs[0].retry_index == 1


def test_read_rejects_tampered_storage_chronology(accounting_queue):
    store, request, *_ = accounting_queue
    _, _, record = complete_raw(accounting_queue)
    store.record_accounting(request.workflow_id, record, now=record_at(record))
    store.connection.execute("UPDATE attempt_accounting SET recorded_at=?", (NOW.isoformat(),))
    with pytest.raises(ValueError, match="chronology"):
        store.read(request.workflow_id)


def test_read_rejects_sidecar_without_context(accounting_queue):
    store, request, *_ = accounting_queue
    _, _, record = complete_raw(accounting_queue)
    store.record_accounting(request.workflow_id, record, now=record_at(record))
    store.connection.execute("DELETE FROM accounting_contexts")
    with pytest.raises(ValueError, match="lacks an accounting context"):
        store.read(request.workflow_id)


def test_read_returns_records_in_raw_attempt_order_with_gaps_allowed(accounting_queue):
    store, request, _, registry, context, _ = accounting_queue
    _, first, first_record = complete_raw(accounting_queue)
    second_claim = store.claim("worker", now=first.actual_finish + timedelta(seconds=4), lease_seconds=60)
    second_claim = store.mark_running(second_claim, now=first.actual_finish + timedelta(seconds=4))
    second = attempt(second_claim, actual_start=first.actual_finish + timedelta(seconds=4), finish=first.actual_finish + timedelta(seconds=5))
    store.complete(second_claim, second, now=second.actual_finish)
    second_record = enrich_attempt(second, registry, context, energy_report=unknown_energy_report(),
        measurement_start=second.actual_start, measurement_finish=second.actual_finish,
        journal_recorded_at=second.actual_finish, accounted_at=second.actual_finish)
    store.record_accounting(request.workflow_id, second_record, now=second.actual_finish)
    assert store.read(request.workflow_id).accounting == (second_record,)
    store.record_accounting(request.workflow_id, first_record, now=second.actual_finish)
    assert store.read(request.workflow_id).accounting == (first_record, second_record)


def test_readonly_legacy_database_without_extension_still_reads(accounting_queue):
    store, request, *_ = accounting_queue
    config = dict(store.read(request.workflow_id).configuration)
    config.pop("accounting_version")
    store.connection.execute("UPDATE workflows SET configuration=?", (json.dumps(config),))
    store.connection.executescript("DROP TABLE attempt_accounting; DROP TABLE accounting_contexts; DROP TABLE accounting_meta;")
    readonly = QueueStore.__new__(QueueStore)
    readonly.path = store.path
    readonly.connection = sqlite3.connect(store.path.as_uri() + "?mode=ro", uri=True, isolation_level=None)
    readonly.connection.row_factory = sqlite3.Row
    try:
        snapshot = readonly.read(request.workflow_id)
        assert snapshot.accounting_context is None and snapshot.accounting == ()
        assert not readonly.connection.execute("SELECT name FROM sqlite_master WHERE name='accounting_meta'").fetchall()
    finally:
        readonly.close()


def test_version_marker_and_context_are_paired(accounting_queue):
    store, request, plans, _, context, config = accounting_queue
    with pytest.raises(ValueError, match="requires an accounting context"):
        store.enqueue(replace(request, workflow_id="missing-context"), plans, configuration=config)
    without_marker = {key: value for key, value in config.items() if key != "accounting_version"}
    with pytest.raises(ValueError, match="requires an execution accounting version"):
        store.enqueue(replace(request, workflow_id="missing-marker"), plans,
            configuration=without_marker, accounting_context=context)
    with pytest.raises(ValueError, match="Unsupported execution accounting version"):
        store.enqueue(replace(request, workflow_id="wrong-marker"), plans,
            configuration={**config, "accounting_version": "future-version"}, accounting_context=context)
    store.connection.execute("DELETE FROM accounting_contexts")
    with pytest.raises(ValueError, match="requires an accounting context"):
        store.read(request.workflow_id)


def test_marker_cannot_be_downgraded_by_removing_all_extension_tables(accounting_queue):
    store, request, *_ = accounting_queue
    store.connection.executescript("DROP TABLE attempt_accounting; DROP TABLE accounting_contexts; DROP TABLE accounting_meta;")
    with pytest.raises(ValueError, match="requires an accounting context"):
        store.read(request.workflow_id)


@pytest.mark.parametrize("corruption", ["partial", "wrong-version", "missing-version"])
def test_bad_extension_metadata_rejected_on_read_and_writer_open(accounting_queue, corruption):
    store, request, *_ = accounting_queue
    if corruption == "partial":
        store.connection.execute("DROP TABLE attempt_accounting")
    elif corruption == "wrong-version":
        store.connection.execute("UPDATE accounting_meta SET version=2")
    else:
        store.connection.execute("DELETE FROM accounting_meta")
    with pytest.raises(ValueError, match="accounting metadata"):
        store.read(request.workflow_id)
    with pytest.raises(ValueError, match="accounting metadata"):
        QueueStore(store.path)


def test_planned_enqueue_keeps_accounting_context_separate(planned_inputs, tmp_path):
    args, context, config, prepared = planned_inputs
    accounting_context = make_accounting_context(args["registry"], args["request"])
    config = {**config, "accounting_version": "execution-accounting.v1"}
    store = QueueStore(tmp_path / "planned-accounting.sqlite3")
    try:
        identity = store.enqueue_planned(args["request"], tuple(item.candidate for item in prepared.result.winner.assignments),
            configuration=config, planning_context=context, decision=prepared.report, now=args["request"].arrived_at,
            accounting_context=accounting_context)
        state = store.read(identity)
        assert state.accounting_context == accounting_context and state.accounting == ()
        assert state.planning_context == context and len(state.decisions) == 1
    finally:
        store.close()


def test_planned_accounting_context_failure_rolls_back_decision_and_jobs(planned_inputs, tmp_path):
    args, context, config, prepared = planned_inputs
    accounting_context = make_accounting_context(args["registry"], args["request"])
    config = {**config, "accounting_version": "execution-accounting.v1"}
    store = QueueStore(tmp_path / "planned-rollback.sqlite3")
    try:
        store.connection.execute("CREATE TRIGGER deny_context BEFORE INSERT ON accounting_contexts BEGIN SELECT RAISE(ABORT,'fixture'); END")
        with pytest.raises(sqlite3.IntegrityError):
            store.enqueue_planned(args["request"], tuple(item.candidate for item in prepared.result.winner.assignments),
                configuration=config, planning_context=context, decision=prepared.report,
                now=args["request"].arrived_at, accounting_context=accounting_context)
        for table in ("workflows", "jobs", "plan_history", "planning_contexts", "planning_decisions", "accounting_contexts"):
            assert store.connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0
    finally:
        store.close()


def test_read_rejects_sidecar_rebound_to_unknown_raw_attempt(accounting_queue):
    store, request, *_ = accounting_queue
    _, _, record = complete_raw(accounting_queue)
    store.record_accounting(request.workflow_id, record, now=record_at(record))
    store.connection.execute("PRAGMA foreign_keys=OFF")
    store.connection.execute("UPDATE attempt_accounting SET attempt_id='missing-attempt'")
    with pytest.raises(ValueError, match="Unknown or duplicate accounting attempt"):
        store.read(request.workflow_id)
