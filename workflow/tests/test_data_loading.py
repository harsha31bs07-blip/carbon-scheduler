"""Comparison integrity tests without Streamlit or provider calls."""

import json

import pytest

from workflow.dag import STEP_IDS
from workflow.dashboard.data_loading import (
    aggregate_run_totals, load_run, load_runs, provenance_label, select_comparison,
)


def write_run(directory, run_id="scheduled", policy="balanced", **overrides):
    metadata = {
        "schema_version": 2, "run_id": run_id, "policy": policy, "custom_weights": None,
        "comparison_id": "pair-a", "workload_id": "ticket-v1", "provider_mode": "Offline",
        "reference_utc_time": "2026-10-09T12:00:00+00:00", "calibration_id": "cal-v1",
        "execution_mode": "replay",
    }
    metadata.update(overrides)
    records = [
        {**metadata, "status": "completed", "step_id": step, "model": "local-qwen3.5",
         "location": "local-gpu", "cost": 0.0, "energy": 0.25, "carbon": 0.1,
         "actual_latency_ms": 100.0, "execution_latency_ms": 125.0, "accuracy_estimate": 0.65,
         "provenance": {"energy": "estimated", "latency": "replayed"}}
        for step in STEP_IDS
    ]
    path = directory / f"{run_id}.jsonl"
    rewrite(path, records)
    path.with_suffix(".manifest.json").write_text(
        json.dumps({**metadata, "status": "completed", "expected_steps": STEP_IDS}), encoding="utf-8",
    )
    return path, records


def rewrite(path, records):
    path.write_text("".join(json.dumps(record) + "\n" for record in records), encoding="utf-8")


def test_complete_pair_appears_with_zero_cost_and_dispatch_latency(tmp_path):
    write_run(tmp_path, "baseline", "baseline")
    write_run(tmp_path)
    pair = select_comparison(load_runs(tmp_path), "scheduled")
    assert pair["comparison_id"] == "pair-a"
    assert pair["scheduled"]["policy"] == "balanced"
    assert pair["scheduled"]["totals"] == {
        "cost": 0, "energy": 1, "carbon": 0.4, "latency": 500, "accuracy": 0.65,
    }


@pytest.mark.parametrize("field,value", [
    ("comparison_id", "different-pair"), ("workload_id", "ticket-v2"),
    ("provider_mode", "Hybrid: Gemini"), ("reference_utc_time", "2026-10-10T12:00:00+00:00"),
    ("calibration_id", "cal-v2"), ("execution_mode", "live"), ("comparison_id", ""),
    ("workload_id", None), ("calibration_id", ""),
])
def test_context_mismatches_never_pair(tmp_path, field, value):
    write_run(tmp_path, "baseline", "baseline")
    write_run(tmp_path, **{field: value})
    assert select_comparison(load_runs(tmp_path)) is None


def test_selected_run_does_not_use_another_runs_comparison(tmp_path):
    write_run(tmp_path, "baseline", "baseline")
    write_run(tmp_path)
    write_run(tmp_path, "unpaired", comparison_id="pair-b")
    assert select_comparison(load_runs(tmp_path), "unpaired") is None


def test_ambiguous_comparison_is_excluded(tmp_path):
    write_run(tmp_path, "baseline", "baseline")
    write_run(tmp_path)
    write_run(tmp_path, "duplicate", "eco")
    assert select_comparison(load_runs(tmp_path)) is None


@pytest.mark.parametrize("change", ["partial", "duplicate", "mixed_policy", "mixed_context", "mixed_version", "not_dict", "unknown_step", "wrong_run_id", "failed"])
def test_incomplete_or_inconsistent_runs_are_excluded(tmp_path, change):
    path, records = write_run(tmp_path)
    if change == "partial":
        records.pop()
    elif change == "duplicate":
        records[3] = records[0]
    elif change == "mixed_policy":
        records[2]["policy"] = "speed"
    elif change == "mixed_context":
        records[2]["workload_id"] = "other"
    elif change == "mixed_version":
        records[2]["schema_version"] = 1
    elif change == "not_dict":
        records[1] = [1, 2]
    elif change == "unknown_step":
        records[2]["step_id"] = "unknown"
    elif change == "wrong_run_id":
        for record in records:
            record["run_id"] = "other"
    elif change == "failed":
        records[3]["status"] = "failed"
    rewrite(path, records)
    assert load_run(path) is None


@pytest.mark.parametrize("field", ["cost", "energy", "carbon", "actual_latency_ms", "execution_latency_ms", "accuracy_estimate"])
@pytest.mark.parametrize("value", [-1, float("nan"), float("inf"), None, True, "1"])
def test_invalid_metrics_are_excluded(tmp_path, field, value):
    path, records = write_run(tmp_path)
    records[2][field] = value
    rewrite(path, records)
    assert load_run(path) is None


def test_accuracy_above_one_is_excluded(tmp_path):
    path, records = write_run(tmp_path)
    records[2]["accuracy_estimate"] = 1.1
    rewrite(path, records)
    assert load_run(path) is None


@pytest.mark.parametrize("change", ["absent", "started", "failed", "wrong_context", "duplicate_steps", "malformed"])
def test_manifest_must_confirm_same_complete_run(tmp_path, change):
    path, records = write_run(tmp_path)
    manifest_path = path.with_suffix(".manifest.json")
    if change == "absent":
        manifest_path.unlink()
    elif change == "malformed":
        manifest_path.write_text("{", encoding="utf-8")
    else:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if change in {"started", "failed"}:
            manifest["status"] = change
        elif change == "wrong_context":
            manifest["reference_utc_time"] = "other"
        else:
            manifest["expected_steps"] = ["classify"] * 4
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    assert load_run(path) is None


def test_malformed_line_invalidates_whole_run(tmp_path):
    path, records = write_run(tmp_path)
    with path.open("a", encoding="utf-8") as stream:
        stream.write("{incomplete\n")
    assert load_runs(tmp_path) == []


def test_legacy_missing_metadata_is_unknown_and_never_compared(tmp_path):
    path, records = write_run(tmp_path, "baseline_legacy", "baseline")
    for record in records:
        for key in ("schema_version", "policy", "comparison_id", "provider_mode", "reference_utc_time", "calibration_id", "workload_id", "execution_mode"):
            record.pop(key)
    rewrite(path, records)
    run = load_run(path)
    assert run["policy"] == "unknown"
    assert run["execution_mode"] is None
    assert run["provider_mode"] is None
    assert select_comparison([run]) is None


def test_empty_directory_and_empty_aggregate(tmp_path):
    assert load_runs(tmp_path) == []
    assert select_comparison([]) is None
    with pytest.raises(ValueError):
        aggregate_run_totals([])


def test_provenance_preserves_estimates_mixed_and_unknown(tmp_path):
    path, records = write_run(tmp_path)
    assert provenance_label(records, "energy") == "Estimated"
    assert provenance_label(records, "latency") == "Replayed"
    assert provenance_label(records, "cost") == "Unknown"
    records[0]["provenance"]["energy"] = "measured"
    assert provenance_label(records, "energy") == "Mixed sources"
    records[0]["provenance"]["energy"] = {"source": "sensor"}
    assert provenance_label(records, "energy") == "Unknown"


@pytest.mark.parametrize("state", ["empty", "complete", "partial"])
def test_dashboard_renders_validated_run_states(tmp_path, monkeypatch, state):
    from pathlib import Path

    AppTest = pytest.importorskip("streamlit.testing.v1").AppTest
    from workflow.dashboard import data_loading

    if state != "empty":
        write_run(tmp_path, "baseline", "baseline")
        path, records = write_run(tmp_path)
        if state == "partial":
            rewrite(path, records[:-1])
    monkeypatch.setattr(data_loading, "load_runs", lambda directory: load_runs(tmp_path))
    app = AppTest.from_file(str(Path(__file__).parents[1] / "dashboard" / "app.py"), default_timeout=20)
    app.session_state["authenticated"] = True
    app.session_state["dashboard_workspace"] = "Legacy policy comparisons"
    app.session_state["run_id"] = "scheduled" if state != "empty" else None
    app.run()
    assert not app.exception
    if state == "complete":
        assert any("Matched comparison: pair-a" in caption.value for caption in app.caption)
        assert any(metric.label == "Latency" and metric.value == "500 ms" for metric in app.metric)
        assert any("Carbon unchanged" in markdown.value for markdown in app.markdown)
    else:
        assert not any("Matched comparison" in caption.value for caption in app.caption)


def test_dashboard_comparison_action_uses_paired_runner(tmp_path, monkeypatch):
    from pathlib import Path

    AppTest = pytest.importorskip("streamlit.testing.v1").AppTest
    from workflow import executor
    from workflow.dashboard import data_loading

    calls = []

    def fake_comparison(**kwargs):
        calls.append(kwargs)
        write_run(tmp_path, "baseline", "baseline")
        write_run(tmp_path, policy=kwargs["policy"])
        return {"baseline_run_id": "baseline", "scheduled_run_id": "scheduled", "comparison_id": "pair-a"}

    monkeypatch.setattr(executor, "run_comparison", fake_comparison)
    monkeypatch.setattr(data_loading, "load_runs", lambda directory: load_runs(tmp_path))
    app = AppTest.from_file(str(Path(__file__).parents[1] / "dashboard" / "app.py"), default_timeout=20)
    app.session_state["authenticated"] = True
    app.session_state["dashboard_workspace"] = "Legacy policy comparisons"
    app.session_state["run_id"] = None
    app.run()
    app.button(key="btn_run_comparison").click().run()
    assert not app.exception
    assert calls == [{"policy": "eco", "custom_weights": None, "replay_mode": True}]
    assert app.session_state["run_id"] == "scheduled"
    assert any("Matched comparison: pair-a" in caption.value for caption in app.caption)
