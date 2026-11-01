"""Only attributable, content-bound human judgments can enrich profile inputs."""

from copy import deepcopy
from datetime import datetime, timedelta, timezone
import hashlib
import json

import pytest

from data.quality_reviews import apply_reviews, fingerprint, load_json, load_rubric, review_template


def trial(identity="trial-a", **changes):
    return {
        "schema_version": 1, "record_type": "CalibrationTrial", "trial_id": identity,
        "case_id": "cal-account-01", "split": "calibration", "warmup": False,
        "step_id": "draft", "status": "completed", "output_valid": True,
        "completion_reason": "stop", "prompt": "Actual ticket and evidence [KB-ACCOUNT-001]",
        "result": {"text": "Use the reset flow and check spam. [KB-ACCOUNT-001]", "input_tokens": 50},
        "quality_score": None, "quality_source": "unknown", **changes,
    }


def approved(record, rubric, *, score=1):
    review = review_template(record, rubric)
    review.update(status="approved", review_method="human", reviewer="Reviewer A",
                  reviewed_at=(datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat(),
                  score=score, rationale="The reply agrees with the provided account guidance.")
    for judgment in review["criteria"].values():
        judgment.update(score=score, rationale="Checked against the retained ticket and supplied KB.")
    return review


def test_template_is_pending_unscored_and_binds_entire_trial_and_exact_output():
    record, rubric = trial(), load_rubric()
    template = review_template(record, rubric)
    assert template["status"] == "pending"
    assert template["review_method"] is template["reviewer"] is template["reviewed_at"] is None
    assert template["score"] is None and template["rationale"] == ""
    assert all(item == {"score": None, "rationale": ""} for item in template["criteria"].values())
    assert template["trial_sha256"] == fingerprint(record)
    assert template["output_sha256"] == hashlib.sha256(record["result"]["text"].encode()).hexdigest()
    with pytest.raises(ValueError, match="approved human"):
        apply_reviews([record], [template], rubric=rubric)


def test_apply_copies_trials_and_retains_full_deterministic_review_evidence():
    rubric = load_rubric()
    records = [trial("b"), trial("a"), trial("warm", warmup=True)]
    reviews = [approved(records[0], rubric), approved(records[1], rubric, score=.5)]
    original_records, original_reviews, original_rubric = map(deepcopy, (records, reviews, rubric))
    copied, evidence = apply_reviews(records, reviews, rubric=rubric)
    assert (records, reviews, rubric) == (original_records, original_reviews, original_rubric)
    assert [item["quality_score"] for item in copied] == [1, .5, None]
    assert copied[0]["quality_source"] == copied[1]["quality_source"]
    assert copied[0]["quality_source"] == f"human-review:{rubric['rubric_id']}:{fingerprint(rubric)}"
    assert copied[0]["quality_review_fingerprint"] == fingerprint(reviews[0])
    assert evidence["coverage"] == {"retained_trials": 3, "eligible_trials": 2,
        "reviewed_trials": 2, "unreviewed_eligible_trials": 0}
    assert [review["trial_id"] for review in evidence["reviews"]] == ["a", "b"]
    assert apply_reviews(records, list(reversed(reviews)), rubric=rubric)[1] == evidence
    assert evidence["fingerprint"] == fingerprint({k: v for k, v in evidence.items() if k != "fingerprint"})
    copied[0]["result"]["text"] = "Changed only on copy"
    evidence["rubric"]["description"] = "Changed only on copy"
    assert records == original_records and rubric == original_rubric


def test_review_rationale_and_reviewer_changes_change_enrichment_identity():
    record, rubric = trial(), load_rubric()
    review = approved(record, rubric)
    changed = deepcopy(review)
    changed["reviewer"] = "Reviewer B"
    changed["rationale"] = "An independently recorded judgment."
    left, left_evidence = apply_reviews([record], [review], rubric=rubric)
    right, right_evidence = apply_reviews([record], [changed], rubric=rubric)
    assert left[0]["quality_score"] == right[0]["quality_score"]
    assert left[0]["quality_review_fingerprint"] != right[0]["quality_review_fingerprint"]
    assert left_evidence["fingerprint"] != right_evidence["fingerprint"]


def test_empty_reviews_preserve_unknown_quality_and_ineligible_trials():
    records, rubric = [trial(), trial("failed", status="failed", result={})], load_rubric()
    copied, evidence = apply_reviews(records, [], rubric=rubric)
    assert copied == records and copied is not records
    assert evidence["reviews"] == []
    assert evidence["coverage"]["unreviewed_eligible_trials"] == 1


@pytest.mark.parametrize("changes", [
    {"split": "held_out"}, {"warmup": True}, {"warmup": 0}, {"output_valid": False},
    {"output_valid": 1}, {"status": "failed"}, {"completion_reason": "length"},
    {"schema_version": True}, {"record_type": "Other"}, {"step_id": "unknown"}, {"step_id": []},
    {"result": {"text": ""}}, {"prompt": ""}, {"case_id": ""},
    {"quality_score": .5, "quality_source": "human-review:existing"},
    {"quality_score": .5, "quality_source": "format-check"},
    {"quality_review_fingerprint": "a" * 64},
])
def test_only_unreviewed_eligible_calibration_outputs_can_receive_reviews(changes):
    with pytest.raises(ValueError):
        review_template(trial(**changes), load_rubric())


@pytest.mark.parametrize("field,value", [
    ("trial_sha256", "0" * 64), ("output_sha256", "0" * 64), ("case_id", "different"),
    ("split", "held_out"), ("step_id", "retrieve"), ("rubric_id", "other.v1"),
    ("rubric_sha256", "0" * 64), ("record_type", "Other"), ("schema_version", True),
    ("status", "pending"), ("review_method", "automated"), ("reviewer", " "),
    ("rationale", ""), ("reviewed_at", "2026-01-01T00:00:00"),
    ("reviewed_at", "not-a-date"), ("reviewed_at", "9999-01-01T00:00:00Z"),
    ("score", True), ("score", float("nan")), ("score", float("inf")),
    ("score", -1), ("score", 1.1), ("score", .5), ("unexpected", "field"),
])
def test_invalid_or_mismatched_review_fields_are_rejected(field, value):
    record, rubric = trial(), load_rubric()
    review = approved(record, rubric)
    review[field] = value
    with pytest.raises(ValueError):
        apply_reviews([record], [review], rubric=rubric)


@pytest.mark.parametrize("changed", ["prompt", "usage", "output", "metadata"])
def test_changes_anywhere_in_trial_invalidate_existing_review(changed):
    record, rubric = trial(), load_rubric()
    review = approved(record, rubric)
    altered = deepcopy(record)
    if changed == "prompt":
        altered["prompt"] += " extra"
    elif changed == "usage":
        altered["result"]["input_tokens"] += 1
    elif changed == "output":
        altered["result"]["text"] += " extra"
    else:
        altered["incidental_metadata"] = "also bound"
    with pytest.raises(ValueError, match="immutable evidence"):
        apply_reviews([altered], [review], rubric=rubric)


def test_missing_extra_or_invalid_criterion_judgments_fail_closed():
    record, rubric = trial(), load_rubric()
    valid = approved(record, rubric)
    first = next(iter(valid["criteria"]))
    malformed = []
    missing = deepcopy(valid)
    del missing["criteria"][first]
    malformed.append(missing)
    extra = deepcopy(valid)
    extra["criteria"]["unknown"] = {"score": 1, "rationale": "Not in rubric"}
    malformed.append(extra)
    for value in (True, None, -1, 2, float("nan"), float("inf")):
        item = deepcopy(valid)
        item["criteria"][first]["score"] = value
        malformed.append(item)
    for key, value in (("rationale", ""), ("extra", "unknown")):
        item = deepcopy(valid)
        item["criteria"][first][key] = value
        malformed.append(item)
    for review in malformed:
        with pytest.raises(ValueError):
            apply_reviews([record], [review], rubric=rubric)


def test_aggregate_may_be_exact_mean_of_different_criterion_scores():
    record, rubric = trial(), load_rubric()
    review = approved(record, rubric)
    for value, criterion in zip((0, .5, 1, 1), review["criteria"].values()):
        criterion["score"] = value
    review["score"] = .625
    assert apply_reviews([record], [review], rubric=rubric)[0][0]["quality_score"] == .625


def test_duplicate_unknown_and_conflicting_trial_or_review_ids_rejected():
    record, rubric = trial(), load_rubric()
    review = approved(record, rubric)
    changed = deepcopy(review)
    changed["reviewer"] = "Second reviewer"
    for records, reviews in (([record, record], []), ([record], [review, review]),
                            ([record], [review, changed]), ([], [review])):
        with pytest.raises(ValueError):
            apply_reviews(records, reviews, rubric=rubric)


@pytest.mark.parametrize("payload", [
    '{"score": 0, "score": 1}', '{"nested": {"score": 0, "score": 1}}',
    '{"score": NaN}', '{"score": Infinity}', '{"score": -Infinity}', '{"score": 1e9999}',
])
def test_json_loading_rejects_duplicates_and_nonfinite_numbers(tmp_path, payload):
    source = tmp_path / "review.json"
    source.write_text(payload, encoding="utf-8")
    with pytest.raises(ValueError):
        load_json(source)


def test_json_and_fingerprints_preserve_unicode_and_ignore_object_order(tmp_path):
    value = {"rationale": "Évidence ✓", "score": .5}
    source = tmp_path / "review.json"
    source.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8-sig")
    assert load_json(source) == value
    assert fingerprint(value) == fingerprint(dict(reversed(list(value.items()))))
    with pytest.raises(ValueError):
        fingerprint({1: "not a JSON string field"})


@pytest.mark.parametrize("alteration", ["extra", "version", "missing-step", "duplicate-criterion", "empty-description"])
def test_invalid_rubrics_cannot_supply_quality(tmp_path, alteration):
    rubric = load_rubric()
    if alteration == "extra":
        rubric["unknown"] = True
    elif alteration == "version":
        rubric["schema_version"] = True
    elif alteration == "missing-step":
        del rubric["steps"]["draft"]
    elif alteration == "duplicate-criterion":
        rubric["steps"]["draft"].append(deepcopy(rubric["steps"]["draft"][0]))
    else:
        rubric["steps"]["draft"][0]["description"] = ""
    source = tmp_path / "rubric.json"
    source.write_text(json.dumps(rubric), encoding="utf-8")
    with pytest.raises(ValueError):
        load_rubric(source)


def test_changed_rubric_content_invalidates_existing_judgment():
    record, rubric = trial(), load_rubric()
    review = approved(record, rubric)
    rubric["steps"]["draft"][0]["description"] += " Altered criterion."
    with pytest.raises(ValueError, match="rubric_sha256"):
        apply_reviews([record], [review], rubric=rubric)


@pytest.mark.parametrize("finished", ["not-a-date", "2026-01-01T00:00:00", None, 123])
def test_review_refuses_invalid_trial_completion_timestamp(finished):
    record, rubric = trial(finished_at=finished), load_rubric()
    review = approved(record, rubric)
    with pytest.raises(ValueError, match="finished_at|trial completion"):
        apply_reviews([record], [review], rubric=rubric)


def test_review_cannot_precede_trial_completion_and_accepts_equivalent_offset_times():
    instant = datetime.now(timezone.utc) - timedelta(minutes=5)
    record, rubric = trial(finished_at=instant.isoformat()), load_rubric()
    review = approved(record, rubric)
    review["reviewed_at"] = (instant - timedelta(seconds=1)).isoformat()
    with pytest.raises(ValueError, match="trial completion"):
        apply_reviews([record], [review], rubric=rubric)
    review["reviewed_at"] = instant.astimezone(timezone(timedelta(hours=5, minutes=30))).isoformat()
    copied, _ = apply_reviews([record], [review], rubric=rubric)
    assert copied[0]["quality_score"] == 1
