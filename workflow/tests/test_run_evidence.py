"""Integration checks for persisted experiments and strict offline replay."""

import json
from pathlib import Path

import pytest

from shared.run_records import write_json
from workflow import executor
from workflow.dag import STEP_IDS
from workflow.replay import ReplayError, export_comparison, load_replay_run
from workflow.run_store import load_run, load_runs, select_comparison


def read_records(path):
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


@pytest.fixture
def live_pair(tmp_path, monkeypatch):
    results = tmp_path / "runs"
    monkeypatch.setattr(executor, "RESULTS_DIR", results)
    calls = []

    def dispatch(candidate, prompt, context):
        calls.append(candidate)
        return {"text": f"Output for {candidate.step_id}", "latency_ms": 1234.0,
                "input_tokens": 50, "output_tokens": 20, "model": candidate.model,
                "token_source": "provider_reported", "energy_wh": 0.012,
                "energy_measured": True, "energy_method": "test sensor"}

    monkeypatch.setattr(executor, "_dispatch", dispatch)
    pair = executor.run_comparison("eco")
    return results, pair, calls


def test_executor_records_are_loaded_as_a_complete_matching_pair(live_pair):
    directory, ids, calls = live_pair
    pair = select_comparison(load_runs(directory), ids["scheduled_run_id"])
    assert pair is not None
    assert len(calls) == 8
    assert all(candidate.time_offset_hr == 0 for candidate in calls)
    base, scheduled = pair["baseline"], pair["scheduled"]
    assert base["policy"] == "baseline" and scheduled["policy"] == "eco"
    assert base["reference_utc_time"] == scheduled["reference_utc_time"]
    assert base["comparison_id"] == scheduled["comparison_id"] == ids["comparison_id"]
    record = scheduled["records"][0]
    assert record["output_text"] == "Output for classify"
    assert record["execution_energy_wh"] == 0.012
    assert record["energy_measured"] is True
    assert record["provenance"]["energy"] == "estimated"
    assert record["provenance"]["execution_energy"] == "client_reported_measurement"
    assert record["execution_latency_ms"] >= 0


def test_export_and_replay_keep_original_outputs_and_metrics(live_pair, tmp_path, monkeypatch):
    directory, ids, calls = live_pair
    bundle = export_comparison(directory, ids["baseline_run_id"], ids["scheduled_run_id"], tmp_path / "fixture")
    source = read_records(directory / f"{ids['scheduled_run_id']}.jsonl")

    def no_calls(*args, **kwargs):
        pytest.fail("Strict replay attempted model inference or scheduling")

    monkeypatch.setattr(executor, "_dispatch", no_calls)
    monkeypatch.setattr(executor, "schedule_step", no_calls)
    replay = executor.run_comparison("eco", replay_mode=True, replay_dir=bundle)
    copied = read_records(directory / f"{replay['scheduled_run_id']}.jsonl")
    assert len(calls) == 8
    for old, new in zip(source, copied):
        for key in ("output_text", "actual_latency_ms", "execution_latency_ms", "cost", "energy",
                    "carbon", "input_tokens", "output_tokens", "execution_energy_wh", "calibration_id"):
            assert new[key] == old[key]
        assert new["replay_used"] is True
        assert new["provenance"]["latency"] == "replayed"
        assert new["replay_source_run_id"] == old["run_id"]
    assert select_comparison(load_runs(directory), replay["scheduled_run_id"]) is not None
    with pytest.raises(ReplayError, match="live evidence"):
        export_comparison(directory, replay["baseline_run_id"], replay["scheduled_run_id"], tmp_path / "recursive")


def test_replay_rejects_tampered_recording(live_pair, tmp_path):
    directory, ids, _ = live_pair
    bundle = export_comparison(directory, ids["baseline_run_id"], ids["scheduled_run_id"], tmp_path / "fixture")
    recording = bundle / f"{ids['scheduled_run_id']}.jsonl"
    recording.write_text(recording.read_text(encoding="utf-8") + "\n", encoding="utf-8")
    with pytest.raises(ReplayError, match="checksum"):
        executor.run_pipeline("eco", replay_mode=True, replay_dir=bundle)


def test_replay_rejects_wrong_policy_or_workload(live_pair, tmp_path):
    directory, ids, _ = live_pair
    bundle = export_comparison(directory, ids["baseline_run_id"], ids["scheduled_run_id"], tmp_path / "fixture")
    with pytest.raises(ReplayError):
        executor.run_pipeline("speed", replay_mode=True, replay_dir=bundle)
    with pytest.raises(ReplayError, match="workload"):
        load_replay_run(bundle, "eco", None, "different-prompt-version")


def test_failed_pipeline_has_failed_manifest_and_cannot_be_compared(tmp_path, monkeypatch):
    monkeypatch.setattr(executor, "RESULTS_DIR", tmp_path)

    def fail(candidate, prompt, context):
        if candidate.step_id == "retrieve":
            raise TimeoutError("provider timeout")
        return {"text": "ok", "latency_ms": 10, "input_tokens": 1, "output_tokens": 1}

    monkeypatch.setattr(executor, "_dispatch", fail)
    with pytest.raises(RuntimeError, match="both failed"):
        executor.run_pipeline("eco", comparison_id="failed_pair")
    manifest = json.loads(next(tmp_path.glob("*.manifest.json")).read_text(encoding="utf-8"))
    assert manifest["status"] == "failed"
    assert manifest["completed_steps"] == ["classify"]
    assert load_runs(tmp_path) == []


def test_custom_weights_are_preserved(tmp_path, monkeypatch):
    monkeypatch.setattr(executor, "RESULTS_DIR", tmp_path)
    monkeypatch.setattr(executor, "_dispatch", lambda *args: {
        "text": "ok", "latency_ms": 10, "input_tokens": 1, "output_tokens": 1})
    weights = {"w_carbon": 0.7, "w_latency": 0.3}
    run_id = executor.run_pipeline("custom", weights)
    run = load_run(tmp_path / f"{run_id}.jsonl")
    assert run is not None
    assert all(record["custom_weights"] == weights for record in run["records"])


def test_hybrid_execution_does_not_choose_fictional_regions(hybrid):
    from scheduler.candidate_gen import generate_candidates
    hybrid(gemini=True, nvidia=True)
    candidates = generate_candidates("classify", execution_only=True)
    assert len(candidates) == len({candidate.model for candidate in candidates})
    assert all(candidate.time_offset_hr == 0 for candidate in candidates)


def test_run_id_rejects_path_traversal(tmp_path, monkeypatch):
    monkeypatch.setattr(executor, "RESULTS_DIR", tmp_path)
    with pytest.raises(ValueError, match="IDs"):
        executor.execute_step("classify", "../outside", "eco")
