import pytest
from test_dashboard_submission import submit, NOW
from workflow.queue_store import QueueStore


def test_history_filters_use_constant_query_count_as_queue_grows(tmp_path):
    store = QueueStore(tmp_path / "queue.sqlite3")
    try:
        statements = []
        counts = []
        for start in (0, 100):
            for index in range(start, start + 100):
                submit(store, token=f"query-scaling-{index:06d}")
            store.connection.set_trace_callback(statements.append)
            statements.clear()
            assert store.workflow_summaries(status="completed", limit=20) == []
            counts.append(len(statements))
            store.connection.set_trace_callback(None)
        assert counts[0] == counts[1] and counts[0] < 8
    finally:
        store.close()


@pytest.mark.parametrize("states,cancelled,report", [
    (["scheduled"] * 4, False, None),
    (["completed"] * 3 + ["scheduled"], False, None),
    (["completed"] * 4, False, None),
    (["running", "failed", "scheduled", "scheduled"], False, None),
    (["running", "deadline_missed", "failed", "scheduled"], False, None),
    (["needs-reconciliation", "deadline_missed", "failed", "scheduled"], False, None),
    (["cancelled"] * 4, True, None),
    (["scheduled"] * 4, False, {"record_type": "PlanningPreparationFailure"}),
    (["scheduled"] * 4, False, {"result": {"winner": None}}),
])
def test_sql_history_status_matches_store_precedence(tmp_path, states, cancelled, report):
    from workflow.queue_store import _workflow_status, _dump
    store = QueueStore(tmp_path / "queue.sqlite3")
    try:
        identity = submit(store)
        request = store.read(identity).request
        statuses = dict(zip((step.step_id for step in request.steps), states))
        for step, state in statuses.items():
            store.connection.execute("UPDATE jobs SET status=? WHERE workflow_id=? AND step_id=?", (state, identity, step))
        if cancelled:
            store.connection.execute("UPDATE workflows SET cancelled_at=? WHERE id=?", (NOW.isoformat(), identity))
        if report is not None:
            store.connection.execute("INSERT INTO planning_decisions VALUES (?,1,?,?,NULL)", (identity, NOW.isoformat(), _dump(report)))
        expected = _workflow_status(statuses, [step.step_id for step in request.steps if step.required_for_response],
                                    NOW if cancelled else None, report)
        assert store.workflow_summaries()[0]["status"] == expected
        assert store.workflow_summaries(status=expected)[0]["workflow_id"] == identity
    finally:
        store.close()


