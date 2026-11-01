"""Empirical estimates cannot leak held-out trials or manufacture measurements."""

from copy import deepcopy
import json
import math

import pytest

from data.profiles import CONTEXT_SOURCE, build_profiles, estimate_step, evaluate_profiles


def trial(trial_id="cal-1", **changes):
    record = {
        "schema_version": 1, "record_type": "CalibrationTrial", "trial_id": trial_id,
        "split": "calibration", "context_source": CONTEXT_SOURCE, "model_id": "llama",
        "deployment_id": "local", "deployment_fingerprint": "local-config-v1", "step_id": "draft",
        "task_type": "support-reply", "workload_version": "connected-v1", "input_size_estimate": 200,
        "output_limit_tokens": 128, "enable_thinking": False, "warmup": False, "cold_start": False,
        "status": "completed", "output_valid": True, "completion_reason": "stop",
        "latency_seconds": 4., "load_seconds": .1, "input_tokens": 45, "output_tokens": 20,
        "quality_score": None, "quality_source": "unknown", "energy_kwh": None,
        "energy_provenance": "unknown", "energy_source": None, "carbon_g": None,
        "carbon_provenance": "unknown", "carbon_source": None, "cost_usd": None,
        "cost_provenance": "unknown", "cost_source": None,
    }
    return {**record, **changes}


def estimate(profiles, **changes):
    kwargs = {"deployment_fingerprint": "local-config-v1", "model_id": "llama", "step_id": "draft",
              "workload_version": "connected-v1", "input_size_estimate": 200,
              "output_limit_tokens": 128, "enable_thinking": False}
    return estimate_step(profiles, **{**kwargs, **changes})


def test_statistics_preserve_failures_truncation_missing_tokens_and_start_states():
    profiles = build_profiles([
        trial("warm", latency_seconds=2., input_tokens=None, output_tokens=None, cold_start=False),
        trial("cold", latency_seconds=10., load_seconds=7., cold_start=True),
        trial("failure", status="failed", output_valid=False, completion_reason="error", latency_seconds=30., cold_start=None),
        trial("truncated", output_valid=False, completion_reason="length", latency_seconds=1.),
    ], calibration_id="pilot-v1")
    profile = profiles["profiles"][0]
    assert profile["sample_counts"] == {"attempts": 4, "completed": 3, "failed": 1,
                                         "valid_natural_completions": 2, "invalid_output": 2,
                                         "truncated": 1, "reviewed_quality": 0}
    assert profile["metrics"]["latency"]["median"] == 6.
    assert profile["metrics"]["latency"]["p90"] == pytest.approx(9.2)
    assert profile["all_attempt_latency"]["count"] == 4
    assert profile["input_tokens"]["count"] == profile["output_tokens"]["count"] == 1
    assert profile["start_states"]["cold"]["valid_latency"]["median"] == 10.
    assert profile["start_states"]["warm"]["valid_latency"]["median"] == 2.
    assert profile["start_states"]["unknown"]["attempt_count"] == 1
    result = estimate(profiles)
    assert result.latency.value == 6.
    assert result.latency.uncertainty == pytest.approx(math.sqrt(32))
    assert result.latency.provenance == "estimated"
    assert "pilot-v1" in result.latency.source
    assert all(metric.value is None and metric.provenance == "unknown" for metric in
               (result.accuracy, result.energy, result.carbon, result.cost))
    json.dumps(profiles, allow_nan=False)
    json.dumps(result.to_dict(), allow_nan=False)


def test_heldout_and_warmup_cannot_change_statistics_or_profile_fingerprints():
    base = [trial()]
    profiles = build_profiles(base, calibration_id="same")
    extra = [trial("heldout", split="held_out", latency_seconds=999., input_size_estimate=5000),
             trial("warmup", warmup=True, latency_seconds=200.)]
    with_excluded = build_profiles(base + extra, calibration_id="same")
    assert with_excluded["profiles"] == profiles["profiles"]
    assert with_excluded["fingerprint"] == profiles["fingerprint"]
    assert with_excluded["summary"] == {"retained_trials": 3, "profile_trials": 1,
                                          "held_out_excluded": 1, "calibration_warmups_excluded": 1,
                                          "profile_count": 1}
    assert estimate(with_excluded, input_size_estimate=5000).latency.value is None


def test_profile_fingerprint_is_order_independent_but_changes_with_measurements():
    records = [trial("a", latency_seconds=3.), trial("b", latency_seconds=5.)]
    expected = build_profiles(records, calibration_id="v1")
    assert build_profiles(list(reversed(records)), calibration_id="v1") == expected
    records[0]["latency_seconds"] = 4.
    assert build_profiles(records, calibration_id="v1")["fingerprint"] != expected["fingerprint"]


def test_explicit_task_type_cannot_reuse_a_different_tasks_profile():
    profiles = build_profiles([trial()], calibration_id="v1")
    assert estimate(profiles, task_type="different-task").latency.value is None
    assert estimate(profiles, task_type="support-reply").latency.value == 4
    profiles = build_profiles([trial(), trial("other", task_type="different-task", latency_seconds=9)], calibration_id="v1")
    assert estimate(profiles).latency.value is None
    assert estimate(profiles, task_type="different-task").latency.value == 9
    assert estimate(profiles, task_type="support-reply").latency.value == 4


@pytest.mark.parametrize("changes", [
    {"deployment_fingerprint": "changed-endpoint"}, {"model_id": "qwen"}, {"step_id": "summarize"},
    {"workload_version": "connected-v2"}, {"output_limit_tokens": 256}, {"enable_thinking": True},
    {"enable_thinking": None}, {"input_size_estimate": 199}, {"input_size_estimate": 201},
])
def test_unknown_configuration_and_outside_workload_range_do_not_use_static_values(changes):
    result = estimate(build_profiles([trial()], calibration_id="v1"), **changes)
    assert all(metric.value is None for metric in
               (result.latency, result.accuracy, result.cost, result.energy, result.carbon))
    assert result.latency.source.startswith("calibration-unknown:")


def test_explicit_extrapolation_is_labeled():
    profiles = build_profiles([trial()], calibration_id="v1")
    result = estimate(profiles, input_size_estimate=1000, allow_extrapolation=True)
    assert result.latency.value == 4.
    assert result.latency.source.endswith(":extrapolated")


def test_input_size_latency_slope_is_fitted_only_with_three_distinct_sizes():
    profiles = build_profiles([
        trial("small", input_size_estimate=100, latency_seconds=2.),
        trial("medium", input_size_estimate=200, latency_seconds=3.),
        trial("large", input_size_estimate=300, latency_seconds=4.),
    ], calibration_id="size-v1")
    assert estimate(profiles, input_size_estimate=150).latency.value == pytest.approx(2.5)
    assert estimate(profiles, input_size_estimate=250).latency.value == pytest.approx(3.5)
    assert estimate(profiles).latency.uncertainty == pytest.approx(0.)
    assert estimate(profiles).latency.source.endswith(":input-size-linear")
    assert profiles["profiles"][0]["latency_model"]["slope_seconds_per_byte"] == pytest.approx(.01)


def test_negative_or_unsupported_size_trend_uses_median():
    declining = [trial(str(size), input_size_estimate=size, latency_seconds=5. - size / 100.)
                 for size in (100, 200, 300)]
    profiles = build_profiles(declining, calibration_id="v1")
    assert profiles["profiles"][0]["latency_model"]["method"] == "median"
    assert estimate(profiles, input_size_estimate=150).latency.value == 3.
    two_sizes = build_profiles(declining[:2], calibration_id="v1")
    assert estimate(two_sizes, input_size_estimate=150).latency.value == 3.5


def test_nonproduction_temperature_is_separate_and_cannot_train_default_estimates():
    profiles = build_profiles([trial("default"), trial("hot", temperature=.9, latency_seconds=999.)],
                              calibration_id="v1")
    assert len(profiles["profiles"]) == 2
    assert estimate(profiles).latency.value == 4.
    only_hot = build_profiles([trial(temperature=.9)], calibration_id="v1")
    assert estimate(only_hot).latency.value is None


@pytest.mark.parametrize("source", ["unknown", "format-check", "output-validation", "json-parser", "manual-review:"])
def test_format_validity_and_unreviewed_scores_never_become_accuracy(source):
    profiles = build_profiles([trial(quality_score=1., quality_source=source)], calibration_id="v1")
    assert estimate(profiles).accuracy.value is None
    assert profiles["profiles"][0]["sample_counts"]["reviewed_quality"] == 0


def test_reviewed_quality_uses_separate_rubric_and_never_becomes_measured_prediction():
    records = [trial("a", quality_score=.5, quality_source="manual-review:grounded-v1"),
               trial("b", quality_score=1., quality_source="manual-review:grounded-v1")]
    profiles = build_profiles(records, calibration_id="v1")
    assert estimate(profiles).accuracy.value == .75
    assert estimate(profiles).accuracy.provenance == "estimated"
    records[1]["quality_source"] = "human-review:different-v2"
    mixed = build_profiles(records, calibration_id="v1")
    assert estimate(mixed).accuracy.value is None
    assert mixed["profiles"][0]["metrics"]["accuracy"]["count"] == 2


def test_partial_energy_is_mixed_and_missing_cost_is_unknown_while_explicit_zero_is_known():
    profiles = build_profiles([
        trial("known", energy_kwh=.002, energy_provenance="measured", energy_source="host-meter-v1",
              cost_usd=0., cost_provenance="estimated", cost_source="local-api-price-v1"),
        trial("unknown"),
    ], calibration_id="v1")
    metric = profiles["profiles"][0]["metrics"]["energy"]
    assert metric["provenance"] == "mixed"
    assert metric["missing_count"] == 1 and metric["observed_count"] == 1
    assert estimate(profiles).energy.value == .002
    assert estimate(profiles).energy.provenance == "estimated"
    assert estimate(profiles).cost.value == 0.
    assert estimate(profiles).carbon.value is None


def test_simulated_energy_is_not_used_in_live_empirical_estimates():
    profiles = build_profiles([trial(energy_kwh=.01, energy_provenance="simulated", energy_source="synthetic-v1")],
                              calibration_id="v1")
    assert estimate(profiles).energy.value is None
    assert profiles["profiles"][0]["metrics"]["energy"]["provenance"] == "unknown"


@pytest.mark.parametrize("unit", ["gCO2", "gCO2e"])
def test_carbon_estimate_preserves_the_observed_scope_and_unit(unit):
    profiles = build_profiles([trial(carbon_g=3., carbon_unit=unit, carbon_provenance="estimated",
                                    carbon_source="documented-grid-v1")], calibration_id="v1")
    carbon = profiles["profiles"][0]["metrics"]["carbon"]
    assert carbon["unit"] == unit
    assert carbon["median"] == 3.
    result = estimate(profiles)
    assert result.carbon.unit == unit and result.carbon.value == 3.
    assert result.carbon.provenance == "estimated"


def test_unknown_carbon_preserves_explicit_co2_scope_without_relabeling():
    profiles = build_profiles([trial(carbon_unit="gCO2")], calibration_id="v1")
    result = estimate(profiles)
    assert result.carbon.unit == "gCO2"
    assert result.carbon.value is None


def test_carbon_scopes_form_separate_profiles_instead_of_blended_statistics():
    profiles = build_profiles([
        trial("co2", carbon_g=2., carbon_unit="gCO2", carbon_provenance="estimated", carbon_source="cea-co2"),
        trial("co2e", carbon_g=8., carbon_unit="gCO2e", carbon_provenance="estimated", carbon_source="ghg-trace"),
    ], calibration_id="v1")
    by_unit = {profile["carbon_unit"]: profile for profile in profiles["profiles"]}
    assert len(by_unit) == 2
    assert by_unit["gCO2"]["metrics"]["carbon"]["median"] == 2.
    assert by_unit["gCO2e"]["metrics"]["carbon"]["median"] == 8.
    assert by_unit["gCO2"]["fingerprint"] != by_unit["gCO2e"]["fingerprint"]
    result = estimate(profiles)
    assert result.carbon.value is None and result.latency.value is None
    assert result.latency.source.endswith("ambiguous-carbon-scope")


def test_default_and_explicit_carbon_scope_are_isolated_even_without_observed_emissions():
    profiles = build_profiles([trial("default"), trial("co2", carbon_unit="gCO2")], calibration_id="v1")
    assert {profile["carbon_unit"] for profile in profiles["profiles"]} == {"gCO2", "gCO2e"}
    assert estimate(profiles).latency.source.endswith("ambiguous-carbon-scope")


@pytest.mark.parametrize("unit", ["gCO2", "gCO2e"])
def test_heldout_evaluation_accepts_matching_carbon_scope(unit):
    profiles = build_profiles([trial(carbon_unit=unit)], calibration_id="v1")
    result = evaluate_profiles(profiles, [trial("held", split="held_out", carbon_unit=unit, latency_seconds=5.)])
    assert result["evaluated_count"] == 1 and result["excluded_count"] == 0
    assert result["mean_absolute_error_seconds"] == 1.


def test_heldout_evaluation_excludes_a_different_carbon_scope():
    profiles = build_profiles([trial(carbon_unit="gCO2")], calibration_id="v1")
    result = evaluate_profiles(profiles, [trial("held", split="held_out", carbon_unit="gCO2e")])
    assert result["evaluated_count"] == 0 and result["excluded_count"] == 1
    assert result["trials"][0]["excluded_reason"] == "carbon-unit-mismatch"
    assert result["trials"][0]["estimated_seconds"] is None


@pytest.mark.parametrize("unit", ["g", "CO2", "kgCO2", None, [], "gCO2/kWh"])
def test_invalid_or_intensity_carbon_units_cannot_be_emission_profiles(unit):
    with pytest.raises(ValueError, match="carbon_unit"):
        build_profiles([trial(carbon_unit=unit)], calibration_id="v1")


def test_all_failed_profiles_are_retained_but_explicitly_unknown():
    profiles = build_profiles([trial(status="failed", output_valid=False, completion_reason="error")],
                              calibration_id="v1")
    assert profiles["profiles"][0]["sample_counts"]["failed"] == 1
    result = estimate(profiles)
    assert result.latency.value is None
    assert result.latency.source.endswith("no-valid-completions")


def test_same_step_with_inconsistent_task_types_is_ambiguous_instead_of_silently_selected():
    profiles = build_profiles([trial("a"), trial("b", task_type="different-task")], calibration_id="v1")
    assert len(profiles["profiles"]) == 2
    assert estimate(profiles).latency.source.endswith("ambiguous-task-profile")


def test_deployment_labels_cannot_disagree_for_same_fingerprint():
    with pytest.raises(ValueError, match="inconsistent deployment"):
        build_profiles([trial("a"), trial("b", deployment_id="different")], calibration_id="v1")


def test_heldout_evaluation_reports_error_and_retains_unknown_exclusions():
    profiles = build_profiles([trial()], calibration_id="v1")
    before = deepcopy(profiles)
    evaluation = evaluate_profiles(profiles, [
        trial("held-1", split="held_out", latency_seconds=5.),
        trial("held-2", split="held_out", latency_seconds=2.),
        trial("held-outside", split="held_out", input_size_estimate=201),
        trial("held-failed", split="held_out", status="failed", output_valid=False, completion_reason="error"),
        trial("held-warmup", split="held_out", warmup=True),
    ])
    assert profiles == before
    assert evaluation["profile_fingerprint"] == profiles["fingerprint"]
    assert evaluation["evaluated_count"] == 2 and evaluation["excluded_count"] == 3
    assert evaluation["mean_absolute_error_seconds"] == 1.5
    assert evaluation["mean_signed_error_seconds"] == .5
    by_id = {row["trial_id"]: row for row in evaluation["trials"]}
    assert by_id["held-1"]["signed_error_seconds"] == -1.
    assert by_id["held-outside"]["excluded_reason"] == "outside-observed-input-range"
    assert by_id["held-failed"]["excluded_reason"] == "invalid-or-incomplete-output"
    assert by_id["held-warmup"]["excluded_reason"] == "warmup"
    json.dumps(evaluation, allow_nan=False)


@pytest.mark.parametrize("record", [trial(), trial("cal-1", split="held_out")])
def test_evaluation_rejects_training_records_and_reused_training_ids(record):
    with pytest.raises(ValueError):
        evaluate_profiles(build_profiles([trial()], calibration_id="v1"), [record])


def test_empty_profiles_and_empty_evaluation_preserve_unknowns():
    profiles = build_profiles([], calibration_id="empty")
    assert estimate(profiles).latency.value is None
    evaluation = evaluate_profiles(profiles, [])
    assert evaluation["evaluated_count"] == 0
    assert evaluation["mean_absolute_error_seconds"] is None


@pytest.mark.parametrize("field,value", [
    ("schema_version", True), ("schema_version", 3), ("record_type", "Other"), ("trial_id", ""),
    ("split", "test"), ("split", []), ("context_source", "generated-live-context"),
    ("model_id", 5), ("input_size_estimate", -1), ("input_size_estimate", True),
    ("output_limit_tokens", 0), ("output_limit_tokens", 32769), ("enable_thinking", "false"),
    ("warmup", 0), ("cold_start", 1), ("output_valid", 1), ("status", "cancelled"), ("status", []),
    ("completion_reason", "provider-arbitrary-token"), ("latency_seconds", float("nan")),
    ("load_seconds", float("inf")), ("input_tokens", -1), ("output_tokens", 1.5),
    ("quality_score", 1.1), ("quality_score", -1), ("quality_source", None),
    ("energy_kwh", -.1), ("energy_provenance", "actual"), ("carbon_g", float("inf")),
    ("cost_usd", .1),
])
def test_invalid_trials_fail_before_profiles_are_built(field, value):
    with pytest.raises(ValueError):
        build_profiles([trial(**{field: value})], calibration_id="v1")


def test_duplicate_ids_are_rejected_even_across_excluded_splits():
    with pytest.raises(ValueError, match="unique"):
        build_profiles([trial(), trial(split="held_out")], calibration_id="v1")


def test_metric_provenance_requires_source_and_observation():
    for changes in ({"energy_kwh": .1, "energy_provenance": "measured"},
                    {"energy_provenance": "measured"},
                    {"carbon_g": .1, "carbon_provenance": "unknown"}):
        with pytest.raises(ValueError):
            build_profiles([trial(**changes)], calibration_id="v1")


def test_unknown_raw_metadata_does_not_get_copied_to_profile_snapshot():
    profiles = build_profiles([trial(raw_attempt={"text": "private-input"}, hardware={"os": "windows"})],
                              calibration_id="v1")
    assert "private-input" not in json.dumps(profiles)


def test_tampered_or_nonfinite_profile_snapshots_are_rejected():
    base = build_profiles([trial()], calibration_id="v1")
    for value in (float("nan"), 17.):
        profiles = deepcopy(base)
        profiles["profiles"][0]["metrics"]["latency"]["median"] = value
        with pytest.raises(ValueError):
            estimate(profiles)


@pytest.mark.parametrize("changes", [{"input_size_estimate": True}, {"output_limit_tokens": -1},
                                     {"enable_thinking": 1}, {"allow_extrapolation": 1},
                                     {"model_id": ""}])
def test_invalid_estimate_requests_fail(changes):
    with pytest.raises(ValueError):
        estimate(build_profiles([trial()], calibration_id="v1"), **changes)
