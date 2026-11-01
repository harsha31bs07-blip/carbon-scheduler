"""Setup checks must not turn configuration into availability or dispatch approval."""

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import subprocess
import sys

import pytest
from streamlit.testing.v1 import AppTest

from shared.deployments import Deployment, DeploymentHealth, DeploymentRegistry
from workflow.dashboard.services import local_paths, startup_commands
from workflow.readiness import inspect_readiness, main, observe_destinations

ROOT = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 11, 1, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def forbid_network(monkeypatch):
    import requests
    monkeypatch.setattr(requests.sessions.Session, "request",
                        lambda *a, **k: pytest.fail("Setup contacted a provider"))
    monkeypatch.delenv("CARBON_DEPLOYMENTS_FILE", raising=False)


def check(report, component):
    return next(item for item in report["checks"] if item["component"] == component)


def test_current_evidence_reports_quality_blocked_without_network_or_queue_creation(tmp_path):
    db = tmp_path / "not-created" / "queue.sqlite3"
    report = inspect_readiness(ROOT, database_path=db, now=NOW)
    assert not report["dispatch_approved"]
    assert check(report, "Reviewed profiles")["status"] == "blocked"
    assert check(report, "Worker")["status"] == "unknown"
    assert check(report, "Knowledge base")["status"] == "valid"
    assert all(item["availability"] == "unknown" for item in report["destinations"])
    assert not db.parent.exists()
    assert '"endpoint":' not in json.dumps(report) and "credential_env" not in json.dumps(report)


def test_invalid_inputs_are_visible_without_traceback_or_raw_content(tmp_path):
    secret = tmp_path / "bad.json"
    secret.write_text('{"secret": "do-not-display"}', encoding="utf-8")
    report = inspect_readiness(tmp_path, registry_path=secret, profiles_path=secret,
                              forecasts_path=secret, carbon_path=secret)
    assert not report["destinations"] and not report["dispatch_approved"]
    assert check(report, "Reviewed profiles")["status"] == "missing-or-invalid"
    assert check(report, "Workload forecasts")["status"] == "invalid"
    assert "do-not-display" not in json.dumps(report)


@pytest.mark.parametrize("age,reachable,expected", [(0, True, "reachable"),
    (0, False, "unavailable"), (0, None, "unknown"), (61, True, "stale"), (-1, True, "stale")])
def test_availability_requires_fresh_matching_observation(tmp_path, age, reachable, expected):
    registry = DeploymentRegistry([Deployment("local", "ollama", "http://localhost:11434", {"small": "tiny:3b"})])
    path = tmp_path / "registry.json"
    path.write_text(json.dumps(registry.to_dict()), encoding="utf-8")
    observation = DeploymentHealth("local", NOW - timedelta(seconds=age), reachable, (), "test-inventory")
    observations = {"registry_fingerprint": registry.config_fingerprint(), "observations": [observation.to_dict()]}
    report = inspect_readiness(ROOT, registry_path=path, observations=observations, now=NOW)
    assert report["destinations"][0]["availability"] == expected
    observations["registry_fingerprint"] = "changed"
    report = inspect_readiness(ROOT, registry_path=path, observations=observations, now=NOW)
    assert not report["destinations"]


def test_explicit_probe_uses_inventory_only(monkeypatch):
    from workflow import deployment_inventory
    calls = []
    def fake(deployment, **kwargs):
        calls.append((deployment.deployment_id, kwargs))
        return DeploymentHealth(deployment.deployment_id, NOW, None, (), "not-observed")
    monkeypatch.setattr(deployment_inventory, "probe_deployment", fake)
    result = observe_destinations(ROOT / "config/deployments.example.json")
    assert len(calls) == 1 and result["observations"][0]["reachable"] is None
    with pytest.raises(ValueError):
        observe_destinations(ROOT / "config/deployments.example.json", timeout_seconds=10)


def test_cli_is_offline_by_default_and_reports_invalid_root(tmp_path, capsys):
    assert main(["--root", str(ROOT), "--config", "config/deployments.example.json"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert not report["dispatch_approved"]
    assert main(["--root", str(tmp_path)]) == 2
    assert json.loads(capsys.readouterr().out)["checks"]
    assert main(["--root", str(ROOT), "--profiles", str(tmp_path / "missing.json")]) == 2
    assert check(json.loads(capsys.readouterr().out), "Reviewed profiles")["status"] == "missing-or-invalid"


def test_relative_paths_use_project_root_and_commands_quote_literal_arguments(tmp_path, monkeypatch):
    monkeypatch.setenv("CARBON_DEPLOYMENTS_FILE", "config/custom.json")
    paths = local_paths(tmp_path, database_path="queue.sqlite3")
    assert paths["registry"] == tmp_path / "config/custom.json"
    assert paths["database"] == tmp_path / "queue.sqlite3"
    commands = startup_commands(tmp_path, paths["database"], executable="C:/Bob's tools/python.exe", powershell=True)
    assert "Bob''s tools" in commands["worker"]
    assert "Set-Location -LiteralPath" in commands["worker"]
    commands = startup_commands(tmp_path, paths["database"], executable="/a weird/python", powershell=False)
    assert "'/a weird/python'" in commands["worker"] and "--server.address 127.0.0.1" in commands["dashboard"]


def test_unauthenticated_shell_does_not_import_legacy_execution():
    script = """
import os, sys
os.environ['PYTHON_DOTENV_DISABLED']='1'
from streamlit.testing.v1 import AppTest
app = AppTest.from_file('workflow/dashboard/app.py', default_timeout=20).run()
assert not app.exception
assert 'workflow.executor' not in sys.modules
assert 'plotly.express' not in sys.modules
"""
    result = subprocess.run([sys.executable, "-c", script], cwd=ROOT, capture_output=True, text=True, timeout=40)
    assert result.returncode == 0, result.stderr


def test_overview_opens_validated_simulation_without_typing_paths():
    app = AppTest.from_file(str(ROOT / "workflow/dashboard/app.py"), default_timeout=30)
    app.session_state["authenticated"] = True
    app.session_state["user_email"] = "local@example.invalid"
    app.run()
    assert not app.exception and app.radio(key="dashboard_workspace").value == "Overview"
    assert any("Local readiness" in item.value for item in app.subheader)
    next(button for button in app.button if button.label == "Open offline demonstration").click().run()
    assert not app.exception
    assert any("Simulation evidence" in item.value for item in app.info)
    assert app.session_state["validated_comparison"][2]["finished_trials"] == 4
