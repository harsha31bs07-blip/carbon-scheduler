"""Invalid calibration/evaluation inputs cannot start expensive inference."""

import json

import pytest

from data.profiles import build_profiles
from shared.deployments import Deployment, DeploymentRegistry
from workflow import calibrate


def local_registry():
    return DeploymentRegistry([Deployment("local-ollama", "ollama", "http://localhost:11434",
                                          {"small": "llama:exact"})])


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Preflight must not contact a provider")
    monkeypatch.setattr(calibrate.requests, "post", forbidden)
    monkeypatch.setattr(calibrate.requests, "get", forbidden)


def test_profiles_from_requires_held_out_before_creating_any_run(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(calibrate, "run_calibration", lambda *a, **k: pytest.fail("Invalid split started a run"))
    output = tmp_path / "must-not-exist"
    code = calibrate.main(["--output", str(output), "--profiles-from", str(tmp_path / "missing.json")])
    assert code == 2 and not output.exists()
    assert json.loads(capsys.readouterr().out)["error_code"] == "invalid-calibration-configuration-or-evidence"


@pytest.mark.parametrize("kind", ["missing", "malformed", "wrong-schema", "tampered", "duplicate-fields", "not-object"])
def test_invalid_frozen_file_fails_before_any_run(tmp_path, monkeypatch, kind):
    path = tmp_path / "profiles.json"
    if kind == "malformed":
        path.write_text("not-json", encoding="utf-8")
    elif kind == "not-object":
        path.write_text("[]", encoding="utf-8")
    elif kind == "duplicate-fields":
        path.write_text('{"schema_version": 1, "schema_version": 1}', encoding="utf-8")
    elif kind != "missing":
        profiles = build_profiles([], calibration_id="fixture-training")
        if kind == "wrong-schema":
            profiles["schema_version"] = 99
        else:
            profiles["fingerprint"] = "a" * 64
        path.write_text(json.dumps(profiles), encoding="utf-8")
    monkeypatch.setattr(calibrate, "run_calibration", lambda *a, **k: pytest.fail("Invalid profiles started inference"))
    output = tmp_path / "must-not-exist"
    assert calibrate.main(["--split", "held_out", "--profiles-from", str(path), "--output", str(output)]) == 2
    assert not output.exists()


def test_evaluation_uses_validated_snapshot_even_if_input_file_changes_during_run(tmp_path, monkeypatch):
    profiles = build_profiles([], calibration_id="fixture-training")
    path = tmp_path / "profiles.json"
    path.write_text(json.dumps(profiles), encoding="utf-8")
    monkeypatch.setattr(calibrate, "load_deployments", lambda _: local_registry())
    calls = []

    def run(registry, **options):
        calls.append(options)
        # A long inference run must not reread an unvalidated replacement file.
        path.write_text("changed-during-inference", encoding="utf-8")
        options["directory"].mkdir()
        return {"directory": str(options["directory"]), "manifest": {"status": "completed"},
                "profiles": {"summary": {}}, "trials": []}

    monkeypatch.setattr(calibrate, "run_calibration", run)
    output = tmp_path / "held-out-run"
    assert calibrate.main(["--split", "held_out", "--models", "small", "--profiles-from", str(path),
                           "--output", str(output)]) == 0
    assert len(calls) == 1 and calls[0]["split"] == "held_out"
    result = json.loads((output / "held_out_errors.json").read_text(encoding="utf-8"))
    assert result["profile_fingerprint"] == profiles["fingerprint"]
    assert result["evaluated_count"] == result["excluded_count"] == 0


@pytest.mark.parametrize("options", [
    {"timeout_seconds": 0}, {"timeout_seconds": float("nan")}, {"timeout_seconds": 301},
    {"timeout_seconds": 10 ** 1000}, {"split": "training"},
    {"models": ["not-configured"]}, {"models": ["small", "small"]}, {"models": [None]},
    {"steps": []}, {"steps": ["classify", "classify"]}, {"steps": ["not-supported"]}, {"steps": [[]]},
    {"steps": 1}, {"steps": "classify"},
    {"energy": "codecarbon"}, {"progress": 1}, {"energy_country_iso_code": "india"},
    {"energy_country_iso_code": "ind"}, {"energy_country_iso_code": True},
])
def test_invalid_programmatic_settings_fail_before_output_directory_or_dispatch(tmp_path, options):
    output = tmp_path / "must-not-exist"
    arguments = {"directory": output, "deployment_id": "local-ollama", "models": ["small"],
                 "dispatcher": lambda *a, **k: pytest.fail("Invalid settings reached inference"),
                 "cases_path": tmp_path / "missing-cases.json"}
    with pytest.raises(ValueError):
        calibrate.run_calibration(local_registry(), **{**arguments, **options})
    assert not output.exists()


@pytest.mark.parametrize("arguments", [["--timeout-seconds", "nan"], ["--timeout-seconds", "0"],
                                        ["--models", "unconfigured"], ["--energy-country", "india"]])
def test_invalid_cli_settings_never_start_inference(tmp_path, monkeypatch, arguments):
    monkeypatch.setattr(calibrate, "load_deployments", lambda _: local_registry())
    monkeypatch.setattr(calibrate, "call_deployment", lambda *a, **k: pytest.fail("Invalid CLI settings dispatched"))
    output = tmp_path / "must-not-exist"
    assert calibrate.main(["--output", str(output), *arguments]) == 2
    assert not output.exists()
