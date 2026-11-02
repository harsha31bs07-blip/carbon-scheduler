"""Dashboard policy/evidence flows must never perform provider work."""

import json
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

from workflow.queue_store import QueueStore
from workflow.dashboard.queue_actions import submit_unverified
from workflow.workload import KnowledgeBase
from test_queue_dashboard import panel, button, ROOT
from test_planned_replay import planned_inputs
from test_staged_planning import inputs, NOW
from test_evaluation_protocol import protocol
from test_policy_comparison import evaluation
from test_workflow_quality import reviewed


def field(app, kind, label):
    return next(item for item in getattr(app, kind) if item.label == label)


def test_planned_submission_saves_unknown_health_without_network(tmp_path, planned_inputs, monkeypatch):
    import workflow.planned_worker as module
    args, *_ = planned_inputs
    monkeypatch.setattr(module, "probe_deployment", lambda *a, **k: pytest.fail("UI probed a provider"))
    profiles = tmp_path / "profiles.json"
    profiles.write_text(json.dumps(args["profiles"]), encoding="utf-8")
    store = QueueStore(tmp_path / "queue.sqlite3")
    try:
        identity = submit_unverified(store, args["registry"], args["kb"], args["request"].input_text,
            profiles_path=profiles)
        state = store.read(identity)
        assert state.status == "infeasible" and not state.attempts and not state.jobs
        report = state.decisions[0]["report"]
        assert all(item["reachable"] is None and not item["available_models"] for item in report["health_snapshot"])
        assert all(item["source"] == "awaiting-worker-verification" for item in report["health_snapshot"])
    finally:
        store.close()


def test_policy_form_preserves_rejection_and_inspects_zero_job_plan(tmp_path, planned_inputs, monkeypatch):
    import requests
    args, *_ = planned_inputs
    monkeypatch.setattr(requests, "get", lambda *a, **k: pytest.fail("UI GET"))
    monkeypatch.setattr(requests, "post", lambda *a, **k: pytest.fail("UI inference"))
    config, profiles, db = tmp_path / "registry.json", tmp_path / "profiles.json", tmp_path / "queue.sqlite3"
    config.write_text(json.dumps(args["registry"].to_dict()), encoding="utf-8")
    profiles.write_text(json.dumps(args["profiles"]), encoding="utf-8")
    script = tmp_path / "app.py"
    script.write_text("from workflow.dashboard.queue_view import render_queue_view\n"
        + f"render_queue_view({str(ROOT)!r},registry_path={str(config)!r},database_path={str(db)!r})\n", encoding="utf-8")
    app = AppTest.from_file(str(script), default_timeout=20).run()
    field(app, "radio", "Execution strategy").set_value("Optimized policy").run()
    field(app, "text_input", "Reviewed profiles file").set_value(str(profiles))
    field(app, "text_area", "Support ticket").set_value(args["request"].input_text)
    button(app, "Queue ticket").click().run()
    assert not app.exception and app.success
    assert any(item.label == "Completed steps" and item.value == "0/4" for item in app.metric)
    assert any("No eligible plan" in item.value for item in app.warning)
    store = QueueStore(db)
    try:
        identity = store.connection.execute("SELECT id FROM workflows").fetchone()[0]
        assert store.read(identity).request.policy == "balanced" and not store.read(identity).attempts
    finally:
        store.close()
    field(app, "selectbox", "Scheduling policy").set_value("custom").run()
    field(app, "text_input", "Custom weights (JSON)").set_value('{"latency":1}')
    button(app, "Queue ticket").click().run()
    assert not app.exception and app.error
    assert any("Custom weights:" in item.value for item in app.error)
    store = QueueStore(db)
    try:
        assert store.connection.execute("SELECT COUNT(*) FROM workflows").fetchone()[0] == 1
    finally:
        store.close()


def test_comparison_panel_loads_coverage_and_outputs_offline(tmp_path, evaluation, monkeypatch):
    import requests
    root, *_ = evaluation
    monkeypatch.setattr(requests, "get", lambda *a, **k: pytest.fail("Evidence UI network"))
    monkeypatch.setattr(requests, "post", lambda *a, **k: pytest.fail("Evidence UI inference"))
    script = tmp_path / "panel.py"
    script.write_text("from workflow.dashboard.evidence_view import render_evidence_view\n"
        + f"render_evidence_view({str(ROOT)!r})\n", encoding="utf-8")
    app = AppTest.from_file(str(script), default_timeout=30).run()
    field(app, "text_input", "Evidence directory").set_value(str(root))
    button(app, "Load evidence").click().run()
    assert not app.exception and len(app.dataframe) == 2
    assert any("Simulation evidence" in item.value for item in app.info)
    assert any("Release checks remain open" in item.value for item in app.warning)
    assert any(item.label == "Trial outputs" for item in app.selectbox)
    field(app, "text_input", "Evidence directory").set_value(str(tmp_path / "missing"))
    button(app, "Load evidence").click().run()
    assert not app.exception and app.error
    assert "validated_comparison" not in app.session_state


def test_human_review_form_requires_explicit_scores_and_saves_additively(tmp_path, reviewed, monkeypatch):
    from datetime import timedelta
    from shared.scheduling import SystemClock
    from test_evaluation_protocol import NOW as REVIEW_NOW
    from workflow.policy_comparison import load_evaluation
    monkeypatch.setattr(SystemClock, "now", lambda self: REVIEW_NOW + timedelta(hours=1))
    root, *_ = reviewed
    before = {path: path.read_bytes() for path in root.glob("bundles/*/workflow.json")}
    script = tmp_path / "review.py"
    script.write_text("from workflow.dashboard.evidence_view import render_evidence_view\n"
        + f"render_evidence_view({str(ROOT)!r})\n", encoding="utf-8")
    app = AppTest.from_file(str(script), default_timeout=30).run()
    field(app, "text_input", "Evidence directory").set_value(str(root))
    button(app, "Load evidence").click().run()
    assert not app.exception
    assert all(item.value is None for item in app.number_input)
    button(app, "Approve and save human review").click().run()
    assert not app.exception and app.error and not (root / "quality_reviews.json").exists()
    field(app, "text_input", "Reviewer attribution").set_value("UI human fixture")
    for item in app.number_input:
        item.set_value(.9)
    for item in app.text_area:
        item.set_value("Synthetic fixture rationale for UI persistence test")
    button(app, "Approve and save human review").click().run()
    assert not app.exception and any("Human approval saved" in item.value for item in app.success)
    report = load_evaluation(root)
    assert report["quality_review_coverage"]["reviewed_trials"] == 1
    assert not report["quality_review_coverage"]["complete"] and not report["release_evidence"]["eligible"]
    assert all(path.read_bytes() == content for path, content in before.items())
