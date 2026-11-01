"""Live response collection must not promote heuristic scores to accuracy."""

import json

import pytest

from data.eval import run_eval
from data.model_table import MODEL_TABLE, get_model_accuracy, get_model_base_accuracy


MODEL = "local-llama3.2"
BASE = MODEL_TABLE[MODEL].base_accuracy


def result_file(tmp_path, *, source=None, measured=True, accuracy=.97, category=.91, **entry_fields):
    entry = {"accuracy": accuracy, "category_accuracy": {"classification": category}, **entry_fields}
    if source is not None:
        entry["accuracy_source"] = source
    path = tmp_path / "eval.json"
    path.write_text(json.dumps({"is_measured": measured, "models": {MODEL: entry}}), encoding="utf-8")
    return path


@pytest.mark.parametrize("source", [None, "keyword-format-proxy-v1", "live", "measured",
    "format-validity", "capability-assumption-simulation-v1", "human-review:", "manual-review:   ", True])
def test_legacy_measured_flag_and_proxy_sources_never_establish_accuracy(tmp_path, source):
    path = result_file(tmp_path, source=source)
    assert get_model_accuracy(MODEL, eval_results_file=path) == BASE
    assert get_model_accuracy(MODEL, "classification", path) == BASE
    assert get_model_base_accuracy(MODEL, path) == BASE


@pytest.mark.parametrize("source", ["manual-review", "human-review", "manual-review:rubric-v1", "human-review:rubric-v2"])
def test_explicit_reviewed_rubric_is_eligible_without_legacy_measured_claim(tmp_path, source):
    path = result_file(tmp_path, source=source, measured=False)
    assert get_model_accuracy(MODEL, eval_results_file=path) == .97
    assert get_model_accuracy(MODEL, " Classification ", path) == .91


def test_whole_file_review_source_is_supported_but_model_proxy_source_takes_precedence(tmp_path):
    path = result_file(tmp_path)
    data = json.loads(path.read_text())
    data["score_source"] = "human-review:support-rubric-v1"
    path.write_text(json.dumps(data), encoding="utf-8")
    assert get_model_accuracy(MODEL, eval_results_file=path) == .97
    data["models"][MODEL]["accuracy_source"] = "keyword-format-proxy-v1"
    path.write_text(json.dumps(data), encoding="utf-8")
    assert get_model_accuracy(MODEL, eval_results_file=path) == BASE


def test_diagnostic_override_is_explicit_and_never_changes_default(tmp_path):
    path = result_file(tmp_path, source="capability-assumption-simulation-v1", measured=False)
    assert get_model_accuracy(MODEL, eval_results_file=path, allow_simulated=True) == .97
    assert get_model_accuracy(MODEL, eval_results_file=path) == BASE


@pytest.mark.parametrize("provenance", ["simulated", "mixed"])
def test_review_label_does_not_erase_declared_simulation(tmp_path, provenance):
    path = result_file(tmp_path, source="human-review:rubric-v1", accuracy_provenance=provenance)
    assert get_model_accuracy(MODEL, eval_results_file=path) == BASE
    assert get_model_accuracy(MODEL, eval_results_file=path, allow_simulated=True) == .97


@pytest.mark.parametrize("value", [True, "0.8", None, -1, 1.01, float("nan"), float("inf"), 10**300])
def test_invalid_score_cannot_enter_scheduler_even_with_diagnostic_override(tmp_path, value):
    path = result_file(tmp_path, source="manual-review:rubric-v1", accuracy=value, category=value)
    for diagnostic in (False, True):
        assert get_model_accuracy(MODEL, eval_results_file=path, allow_simulated=diagnostic) == BASE
        assert get_model_accuracy(MODEL, "classification", path, allow_simulated=diagnostic) == BASE


@pytest.mark.parametrize("data", [[], {"models": []}, {"models": {MODEL: []}}, {"models": None}])
def test_malformed_result_structure_falls_back_to_assumption(tmp_path, data):
    path = tmp_path / "eval.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    assert get_model_accuracy(MODEL, eval_results_file=path) == BASE


def test_live_harness_retains_text_and_rubric_without_semantic_measurement(tmp_path, monkeypatch):
    item = {"id": "one", "category": "classification", "prompt": "Classify this ticket.",
            "reference_answer": "Billing", "rubric": {"expected_keywords": ["Billing"],
            "format_constraint": "single_label"}}
    dataset = tmp_path / "items.json"
    dataset.write_text(json.dumps([item]), encoding="utf-8")
    monkeypatch.setattr(run_eval, "call_live_model", lambda *args: "Billing")
    result = run_eval.run_evaluation([MODEL], dataset, verbose=False)
    assert result["is_measured"] is False
    assert result["responses_live"] is True
    assert result["score_provenance"] == "estimated"
    record = result["models"][MODEL]["details"][0]
    assert record["response"] == "Billing" and record["rubric"] == item["rubric"]
    assert record["semantic_quality_status"] == "unreviewed"
    assert record["score_source"] == "keyword-format-proxy-v1"
    assert record["score"] == 1
    # Even historical code incorrectly toggling the old flag cannot promote it.
    result["is_measured"] = True
    path = run_eval.save_results(result, tmp_path / "result.json")
    assert get_model_accuracy(MODEL, eval_results_file=path) == BASE
    assert get_model_accuracy(MODEL, eval_results_file=path, allow_simulated=True) == 1


def test_live_failure_fallback_is_retained_as_simulation_not_measurement(tmp_path, monkeypatch):
    item = {"id": "one", "category": "qa", "prompt": "Answer.", "reference_answer": "Expected."}
    dataset = tmp_path / "items.json"
    dataset.write_text(json.dumps([item]), encoding="utf-8")
    def unavailable(*args):
        raise RuntimeError("fixture failure")
    monkeypatch.setattr(run_eval, "call_live_model", unavailable)
    result = run_eval.run_evaluation([MODEL], dataset, verbose=False, allow_fallback=True)
    assert result["is_measured"] is False
    assert result["responses_live"] is False
    assert result["models"][MODEL]["details"][0]["score_provenance"] == "simulated"
    assert result["models"][MODEL]["accuracy_provenance"] == "simulated"
    path = run_eval.save_results(result, tmp_path / "result.json")
    assert get_model_accuracy(MODEL, eval_results_file=path) == BASE
