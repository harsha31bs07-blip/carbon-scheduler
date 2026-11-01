"""Production prompts, retained failed trials and truthful calibration metadata."""

from datetime import datetime, timedelta, timezone
import json
import re

import pytest

from shared.deployments import Deployment, DeploymentRegistry
from workflow import calibrate
from workflow.deployment_clients import DeploymentTransientError


def registry():
    return DeploymentRegistry([Deployment("local", "ollama", "http://localhost:11434",
                                          {"small": "llama:exact"})])


def cases(tmp_path):
    path = tmp_path / "cases.json"
    path.write_text(json.dumps({"schema_version": 1, "tickets": [
        {"id": "cal-1", "split": "calibration", "ticket": "My account is locked after failed login attempts.",
         "expected_category": "account_access"},
        {"id": "held-1", "split": "held_out", "ticket": "I cannot reset my account password.",
         "expected_category": "account_access"},
    ]}), encoding="utf-8")
    return path


def response(prompt):
    citations = sorted(set(re.findall(r"KB-[A-Z0-9]+(?:-[A-Z0-9]+)*", prompt)))
    cite = " [" + citations[0] + "]" if citations else ""
    text = "account_access" if "Classify the customer's" in prompt else "Support supplied the evidence." + cite
    return {"text": text, "latency_ms": 100, "completion_reason": "stop", "input_tokens": 15,
            "output_tokens": 9, "provider_model_id": "llama:exact",
            "reported_model_id": "llama:exact",
            "provider_timings_seconds": {"load": .01, "total": .09, "decode": .05}}


def run(tmp_path, monkeypatch, **changes):
    monkeypatch.setattr(calibrate, "hardware_snapshot", lambda: {"source": "test"})
    monkeypatch.setattr(calibrate, "code_revision", lambda: "fixture-source")
    options = dict(directory=tmp_path / "out", deployment_id="local", models=["small"], trials=1,
                   cases_path=cases(tmp_path), dispatcher=lambda d, m, p, **k: response(p),
                   residency_probe=lambda *_: True, progress=False)
    options.update(changes)
    return calibrate.run_calibration(registry(), **options)


def test_production_prompts_and_exact_controls_retained_without_fake_quality(tmp_path, monkeypatch):
    calls = []

    def call(deployment, model, prompt, **kwargs):
        calls.append((deployment.deployment_id, model, prompt, kwargs))
        return response(prompt)

    result = run(tmp_path, monkeypatch, dispatcher=call)
    trials = calibrate.load_trials(tmp_path / "out")
    assert len(trials) == 8 and len(calls) == 8  # One warm-up + one retained trial for each task.
    assert len(result["profiles"]["profiles"]) == 4
    assert all(t["context_source"] == "frozen-calibration-context-v1" for t in trials)
    assert all(t["quality_score"] is None and t["quality_source"] == "unknown" for t in trials)
    assert all(t["energy_kwh"] is None and t["carbon_g"] is None for t in trials)
    assert all(t["cost_usd"] == 0 and t["cost_source"] for t in trials)
    assert all(t["load_seconds"] == .01 and t["cold_start"] is False for t in trials)
    assert all(call[0] == "local" and call[1] == "small" and call[3]["max_attempts"] == 1 for call in calls)
    assert {call[3]["output_limit_tokens"] for call in calls} == {24, 192, 256, 128}
    assert all(call[3]["enable_thinking"] is False for call in calls)
    assert result["manifest"]["status"] == "completed"
    with pytest.raises(FileExistsError):
        run(tmp_path, monkeypatch)


def test_failed_trial_does_not_remove_later_tasks_or_repeat_inference(tmp_path, monkeypatch):
    count = 0

    def call(deployment, model, prompt, **kwargs):
        nonlocal count
        count += 1
        if count == 2:
            raise DeploymentTransientError("request-timeout", request_attempted=True)
        return response(prompt)

    run(tmp_path, monkeypatch, dispatcher=call)
    records = calibrate.load_trials(tmp_path / "out")
    failed = [record for record in records if record.get("status") == "failed"]
    assert count == 8 and len(failed) == 1
    assert failed[0]["error_code"] == "request-timeout" and failed[0]["request_attempted"]
    assert failed[0]["input_tokens"] is None and failed[0]["quality_score"] is None


def test_interrupted_trial_remains_visible_and_is_never_automatically_retried(tmp_path, monkeypatch):
    def crash(*args, **kwargs):
        raise KeyboardInterrupt()

    with pytest.raises(KeyboardInterrupt):
        run(tmp_path, monkeypatch, dispatcher=crash)
    manifest = json.loads((tmp_path / "out" / "manifest.json").read_text())
    assert manifest["status"] == "interrupted"
    pending = calibrate.load_trials(tmp_path / "out")
    assert len(pending) == 1 and pending[0]["status"] == "running"


def test_held_out_does_not_train_profiles(tmp_path, monkeypatch):
    result = run(tmp_path, monkeypatch, split="held_out")
    assert result["profiles"]["profiles"] == []
    assert result["manifest"]["split"] == "held_out"


def test_tracker_factory_accepts_session_options_without_eager_import(monkeypatch):
    import sys
    from types import SimpleNamespace
    seen = []
    monkeypatch.setitem(sys.modules, "codecarbon", SimpleNamespace(
        EmissionsTracker=lambda **kwargs: seen.append(kwargs) or object()))
    factory = calibrate._tracker_factory(True)
    assert not seen
    factory(tracking_mode="machine", save_to_api=False, save_to_file=False, log_level="error")
    assert seen[0]["tracking_mode"] == "machine" and seen[0]["save_to_api"] is False


@pytest.mark.parametrize("options", [{"trials": 0}, {"trials": True}, {"models": [None]},
                                    {"models": []}, {"timeout_seconds": float("nan")}, {"kb": False}])
def test_invalid_configuration_never_dispatches(tmp_path, monkeypatch, options):
    with pytest.raises(ValueError):
        run(tmp_path, monkeypatch, dispatcher=lambda *a, **k: pytest.fail("Invalid config reached inference"), **options)
