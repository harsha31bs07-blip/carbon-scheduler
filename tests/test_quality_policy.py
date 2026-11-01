"""Coverage gates distinguish repeated runs from independent reviewed cases."""

from copy import deepcopy
import json

import pytest

from data.profiles import build_profiles, estimate_step, evaluate_profiles
from data.quality_policy import load_quality_policy, quality_coverage, validate_quality_policy
from data.quality_reviews import apply_reviews, load_rubric, review_template
from test_quality_workflow import approved
from test_profiles import trial


def reviewed_trials(count, case_count):
    records = [trial(f"trial-{index}", case_id=f"case-{index % case_count}", step_id="draft",
                     prompt="Retained support prompt", result={"text": "Retained response"})
               for index in range(count)]
    reviews = [approved(record) for record in records]
    return apply_reviews(records, reviews, rubric=load_rubric())[0]


def estimate(profiles):
    record = trial()
    return estimate_step(profiles, **{key: record[key] for key in (
        "deployment_fingerprint", "model_id", "step_id", "workload_version", "input_size_estimate",
        "output_limit_tokens", "enable_thinking", "task_type")})


def test_full_pilot_review_coverage_enables_quality_and_is_persisted():
    policy = load_quality_policy()
    profiles = build_profiles(reviewed_trials(10, 5), calibration_id="reviewed-v1", quality_policy=policy)
    assert estimate(profiles).accuracy.value == 1.
    quality = profiles["profiles"][0]["metrics"]["accuracy"]
    assert quality["coverage"]["eligible"] is True
    assert len(quality["coverage"]["distinct_case_ids"]) == 5
    assert profiles["quality_policy"] == policy
    evaluate_profiles(profiles, [])


@pytest.mark.parametrize("count,cases,missing", [
    (1, 1, "insufficient-reviewed-trials"), (10, 1, "insufficient-distinct-cases"),
])
def test_insufficient_coverage_keeps_scores_but_not_schedulable_quality(count, cases, missing):
    profiles = build_profiles(reviewed_trials(count, cases), calibration_id="pilot",
                              quality_policy=load_quality_policy())
    quality = profiles["profiles"][0]["metrics"]["accuracy"]
    assert quality["mean"] == 1. and missing in quality["coverage"]["reasons"]
    prediction = estimate(profiles)
    assert prediction.accuracy.value is None
    assert "quality-coverage-insufficient" in prediction.accuracy.source
    assert prediction.latency.value is not None


def test_unreviewed_valid_trials_reduce_coverage_but_failed_trials_do_not():
    records = reviewed_trials(10, 5)
    unreviewed = trial("extra", step_id="draft", case_id="extra")
    partial = build_profiles(records + [unreviewed], calibration_id="partial", quality_policy=load_quality_policy())
    assert estimate(partial).accuracy.value is None
    assert "insufficient-reviewed-fraction" in partial["profiles"][0]["metrics"]["accuracy"]["coverage"]["reasons"]
    failed = trial("failed", step_id="draft", status="failed", output_valid=False, completion_reason=None)
    with_failure = build_profiles(records + [failed], calibration_id="with-failure", quality_policy=load_quality_policy())
    assert estimate(with_failure).accuracy.value == 1.
    assert with_failure["profiles"][0]["sample_counts"]["failed"] == 1


def test_different_policy_changes_identity_and_cannot_be_mutated_after_freezing():
    records = reviewed_trials(10, 5)
    policy = load_quality_policy()
    first = build_profiles(records, calibration_id="same", quality_policy=policy)
    stricter = deepcopy(policy)
    stricter.update(policy_id="stricter-v1", minimum_reviewed_trials=20)
    second = build_profiles(records, calibration_id="same", quality_policy=stricter)
    assert first["fingerprint"] != second["fingerprint"]
    assert estimate(first).accuracy.value == 1. and estimate(second).accuracy.value is None
    first["quality_policy"]["minimum_reviewed_trials"] = 1
    with pytest.raises(ValueError, match="fingerprint"):
        evaluate_profiles(first, [])


def test_missing_case_attribution_cannot_satisfy_case_coverage():
    records = [trial(str(index), quality_score=1., quality_source="human-review:legacy") for index in range(10)]
    profiles = build_profiles(records, calibration_id="legacy", quality_policy=load_quality_policy())
    assert estimate(profiles).accuracy.value is None
    assert profiles["profiles"][0]["metrics"]["accuracy"]["coverage"]["distinct_case_ids"] == []


@pytest.mark.parametrize("changes", [
    {"minimum_reviewed_trials": True}, {"minimum_distinct_cases": 0},
    {"minimum_reviewed_fraction": False}, {"minimum_reviewed_fraction": 0},
    {"minimum_reviewed_fraction": float("nan")}, {"minimum_reviewed_fraction": 1.1},
    {"schema_version": True}, {"extra": 1},
])
def test_malformed_policy_is_rejected(changes):
    policy = load_quality_policy()
    policy.update(changes)
    with pytest.raises(ValueError):
        validate_quality_policy(policy)


def test_claimed_coverage_is_revalidated_even_if_all_hashes_are_recomputed():
    import data.profiles as module
    profiles = build_profiles(reviewed_trials(10, 1), calibration_id="pilot", quality_policy=load_quality_policy())
    profile = profiles["profiles"][0]
    profile["metrics"]["accuracy"]["coverage"]["eligible"] = True
    profile["fingerprint"] = module._fingerprint({k: v for k, v in profile.items() if k != "fingerprint"})
    profiles["fingerprint"] = module._fingerprint(module._envelope_identity(profiles))
    with pytest.raises(ValueError, match="coverage"):
        evaluate_profiles(profiles, [])
