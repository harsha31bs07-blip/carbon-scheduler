"""Durable operator intent, concurrent retries and history navigation."""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import threading

import pytest

from shared.deployments import Deployment, DeploymentRegistry
from workflow.dashboard.queue_actions import submit_fixed_ticket, submit_unverified
from workflow.dashboard.queue_actions import parse_planning_fields, PlanningFieldError
from workflow.queue_store import QueueStore, SubmissionConflict
from workflow.workload import KnowledgeBase
from test_queue_dashboard import panel, button
from test_planned_replay import planned_inputs
from test_staged_planning import inputs

NOW = datetime(2026, 11, 1, tzinfo=timezone.utc)
TOKEN = "submission-token-12345678"


@pytest.mark.parametrize("offsets,weights,field", [("nan", None, "Offered delays"),
    ("0,0", None, "Offered delays"), ("0", '{"latency":1}', "Custom weights"),
    ("0", '{"latency":0,"accuracy":0,"cost":0,"energy":0,"carbon":0}', "Custom weights")])
def test_invalid_planning_fields_explain_the_correction(offsets, weights, field):
    with pytest.raises(PlanningFieldError, match=field):
        parse_planning_fields(offsets, weights, "custom" if weights is not None else "balanced")


class Clock:
    def now(self):
        return NOW


def fixed_inputs():
    return DeploymentRegistry([Deployment("local", "ollama", "http://localhost:11434", {"small": "tiny:3b"})]), KnowledgeBase.load()


def submit(store, *, token=TOKEN, ticket="My account is locked.", **changes):
    registry, kb = fixed_inputs()
    return submit_fixed_ticket(store, registry, kb, ticket, token=token, clock=Clock(),
                               deployment_id="local", model_id="small", **changes)


def test_repeated_submission_survives_reopen_and_rejects_changed_inputs(tmp_path):
    path = tmp_path / "queue.sqlite3"
    store = QueueStore(path)
    identity = submit(store)
    store.close()
    store = QueueStore(path)
    try:
        assert submit(store) == identity
        with pytest.raises(SubmissionConflict):
            submit(store, ticket="A different issue.")
        with pytest.raises(SubmissionConflict):
            submit(store, response_seconds=200)
        assert submit(store, token="new-submission-12345678") != identity
        assert len(store.recent_workflow_ids()) == 2
    finally:
        store.close()


def test_concurrent_submission_creates_one_workflow(tmp_path):
    path = tmp_path / "queue.sqlite3"
    QueueStore(path).close()
    barrier = threading.Barrier(2)
    def concurrent(_):
        store = QueueStore(path)
        try:
            barrier.wait(timeout=10)
            return submit(store)
        finally:
            store.close()
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(concurrent, range(2)))
    assert results[0] == results[1]
    store = QueueStore(path)
    try:
        assert len(store.recent_workflow_ids()) == 1
        assert store.read(results[0]).attempts == ()
    finally:
        store.close()


def test_failed_binding_rolls_back_request_jobs_and_token(tmp_path, monkeypatch):
    store = QueueStore(tmp_path / "queue.sqlite3")
    original = store._record_submission
    def fail(*args):
        original(*args)
        raise OSError("simulated journal failure")
    monkeypatch.setattr(store, "_record_submission", fail)
    try:
        with pytest.raises(OSError):
            submit(store)
        assert not store.recent_workflow_ids()
        assert store.connection.execute("SELECT count(*) FROM queue_submissions").fetchone()[0] == 0
        monkeypatch.setattr(store, "_record_submission", original)
        assert submit(store)
    finally:
        store.close()


def test_history_pagination_search_and_status_reach_old_tickets(tmp_path):
    store = QueueStore(tmp_path / "queue.sqlite3")
    try:
        ids = [submit(store, token=f"submission-number-{index:04}", ticket=f"Customer issue {index:04}") for index in range(105)]
        store.cancel(ids[0], now=NOW)
        seen, cursor = [], None
        while True:
            rows = store.workflow_summaries(before=cursor, limit=20)
            if not rows:
                break
            seen.extend(row["workflow_id"] for row in rows)
            cursor = rows[-1]["cursor"]
        assert seen == list(reversed(ids))
        assert store.workflow_summaries(query="issue 0000")[0]["workflow_id"] == ids[0]
        assert store.workflow_summaries(query=ids[0])[0]["status"] == "cancelled"
        assert len(store.workflow_summaries(status="cancelled")) == 1
        assert not store.workflow_summaries(since=NOW + timedelta(seconds=1))
        for row in store.workflow_summaries(limit=3):
            assert row["status"] == store.read(row["workflow_id"]).status
    finally:
        store.close()


def test_legacy_database_acquires_submission_extension_without_changing_runs(tmp_path):
    path = tmp_path / "queue.sqlite3"
    store = QueueStore(path)
    identity = submit(store, token=None)
    before = store.read(identity)
    store.connection.execute("DROP TABLE queue_submissions")
    store.close()
    store = QueueStore(path)
    try:
        assert store.read(identity) == before
        assert submit(store)
    finally:
        store.close()


def test_ui_repeated_click_and_deliberate_new_submission(panel):
    app, database = panel
    app.run()
    app.text_area[0].input("My account is locked.")
    button(app, "Queue ticket").click().run()
    next(item for item in app.selectbox if item.label == "History status").set_value("completed").run()
    button(app, "Queue ticket").click().run()
    assert not app.exception
    assert app.selectbox(key="queue_history_status").value == "All"
    store = QueueStore(database)
    assert len(store.recent_workflow_ids()) == 1
    store.close()
    button(app, "Start another ticket").click().run()
    button(app, "Queue ticket").click().run()
    assert not app.exception
    store = QueueStore(database)
    assert len(store.recent_workflow_ids()) == 2
    store.close()


def test_active_refresh_remains_inspection_only_and_stops_after_cancel(panel):
    app, database = panel
    app.run()
    app.text_area[0].input("My fictional account is locked.")
    button(app, "Queue ticket").click().run()
    app.checkbox[0].check().run()
    button(app, "Refresh progress").click().run()
    assert not app.exception
    button(app, "Cancel remaining work").click().run()
    assert not app.exception
    store = QueueStore(database)
    try:
        ids = store.recent_workflow_ids()
        assert len(ids) == 1 and store.read(ids[0]).status == "cancelled"
        assert store.read(ids[0]).attempts == ()
    finally:
        store.close()


def test_planned_repeated_submission_binds_evidence_contents(tmp_path, planned_inputs):
    import json
    args, *_ = planned_inputs
    path = tmp_path / "profiles.json"
    path.write_text(json.dumps(args["profiles"]), encoding="utf-8")
    store = QueueStore(tmp_path / "queue.sqlite3")
    try:
        settings = dict(profiles_path=path, token=TOKEN)
        identity = submit_unverified(store, args["registry"], args["kb"], "Account issue", **settings)
        assert submit_unverified(store, args["registry"], args["kb"], "Account issue", **settings) == identity
        path.write_text("{}", encoding="utf-8")
        with pytest.raises(SubmissionConflict):
            submit_unverified(store, args["registry"], args["kb"], "Account issue", **settings)
        assert store.workflow_summaries(status="infeasible")[0]["workflow_id"] == identity
    finally:
        store.close()
