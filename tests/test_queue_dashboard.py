"""The dashboard queues and inspects work without performing inference."""

import json
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

from shared.deployments import Deployment, DeploymentRegistry
from workflow.queue_store import QueueStore


ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def panel(tmp_path, monkeypatch):
    import requests
    def external_call(*args, **kwargs):
        pytest.fail("Dashboard performed an external call")
    monkeypatch.setattr(requests, "get", external_call)
    monkeypatch.setattr(requests, "post", external_call)
    registry = DeploymentRegistry([Deployment("local", "ollama", "http://localhost:11434", {"small": "tiny:3b"})])
    config, database = tmp_path / "deployments.json", tmp_path / "queue.sqlite3"
    config.write_text(json.dumps(registry.to_dict()), encoding="utf-8")
    script = tmp_path / "panel.py"
    script.write_text("from workflow.dashboard.queue_view import render_queue_view\n"
        + f"render_queue_view({str(ROOT)!r}, registry_path={str(config)!r}, database_path={str(database)!r})\n",
        encoding="utf-8")
    # The first populated view also initializes Streamlit's dataframe stack.
    # Allow the same bounded startup budget as the full dashboard tests instead
    # of treating the framework's default three seconds as a product SLA.
    return AppTest.from_file(str(script), default_timeout=20), database


def button(app, label):
    return next(item for item in app.button if item.label == label)


def test_submission_is_durable_and_cancel_does_not_call_provider(panel):
    app, database = panel
    app.run()
    assert not app.exception
    app.text_area[0].input("My CarbonDesk account is locked after failed login attempts.")
    button(app, "Queue ticket").click().run()
    assert not app.exception and any("Ticket queued" in item.value for item in app.success)
    store = QueueStore(database)
    try:
        identity = store.connection.execute("SELECT id FROM workflows").fetchone()[0]
        state = store.read(identity)
        assert state.status == "scheduled" and state.attempts == ()
        assert state.request.input_text.startswith("My CarbonDesk")
    finally:
        store.close()
    button(app, "Refresh progress").click().run()
    assert not app.exception and len(app.dataframe) == 2
    assert any("unknown" in item.value for item in app.caption)
    button(app, "Cancel remaining work").click().run()
    assert not app.exception
    store = QueueStore(database)
    try:
        assert store.read(identity).status == "cancelled" and store.read(identity).attempts == ()
    finally:
        store.close()


def test_invalid_deadlines_do_not_enqueue(panel):
    app, database = panel
    app.run()
    app.text_area[0].input("I cannot access my CarbonDesk account.")
    next(item for item in app.number_input if item.label.startswith("Final deadline")).set_value(10)
    button(app, "Queue ticket").click().run()
    assert not app.exception and app.error
    store = QueueStore(database)
    try:
        assert store.connection.execute("SELECT COUNT(*) FROM workflows").fetchone()[0] == 0
    finally:
        store.close()


def test_authenticated_app_defaults_to_queue_workspace(monkeypatch):
    from workflow.dashboard import queue_view
    visited = []
    monkeypatch.setattr(queue_view, "render_queue_view", lambda root: visited.append(root))
    monkeypatch.setenv("PYTHON_DOTENV_DISABLED", "1")
    app = AppTest.from_file(str(ROOT / "workflow" / "dashboard" / "app.py"), default_timeout=20)
    app.session_state["authenticated"] = True
    app.session_state["user_email"] = "local-test@example.invalid"
    app.run()
    assert not app.exception and visited == [ROOT]
    assert app.radio(key="dashboard_workspace").value == "Ticket workflows"


def test_unauthenticated_app_cannot_open_queue_workspace(monkeypatch):
    from workflow.dashboard import queue_view
    monkeypatch.setattr(queue_view, "render_queue_view", lambda root: pytest.fail("queue bypassed login"))
    monkeypatch.setenv("PYTHON_DOTENV_DISABLED", "1")
    app = AppTest.from_file(str(ROOT / "workflow" / "dashboard" / "app.py"), default_timeout=20)
    app.session_state["authenticated"] = False
    app.run()
    assert not app.exception
    assert any("Login" in title.value for title in app.title)
